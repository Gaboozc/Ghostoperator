"""Entrypoint HTTP del agente (contrato del reto).

GET  /health           -> 200 cuando está listo
POST /tasks            -> {"task_id": "..."} en < 2 s; la tarea corre en segundo plano
GET  /tasks/{task_id}  -> {"estado": "en_curso|terminada|fallida", "resumen": "..."}
GET  /traces/{task_id} -> visor HTML de pasos (evidencia para debrief y pitch), ?token=<TEAM_TOKEN>

Ojo: esta API es NUESTRA (el evaluador habla con el agente por aquí). La app legacy no tiene API;
el agente solo la opera por pantalla vía VNC.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from . import config
from .report import redact
from .trace import Trace, render_trace_html

log = logging.getLogger("ghost.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="GhostOperator", version="1.0")


class Escritorio(BaseModel):
    url: str
    password: str = ""


class TaskIn(BaseModel):
    tarea: str
    escritorio: Escritorio


# ---------------------------------------------------------------- estado en memoria (+ copia en disco)
TASKS: dict[str, dict] = {}
RUNNING_BY_DESKTOP: dict[str, tuple[str, asyncio.Task, asyncio.Event]] = {}


def _persist() -> None:
    try:
        tmp = config.TASKS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(TASKS, ensure_ascii=False, indent=1))
        tmp.replace(config.TASKS_FILE)
    except Exception as e:  # la persistencia nunca debe tumbar el servicio
        log.warning("no se pudo persistir tasks: %s", e)


def _load() -> None:
    if config.TASKS_FILE.exists():
        try:
            TASKS.update(json.loads(config.TASKS_FILE.read_text()))
            for t in TASKS.values():  # tareas que quedaron a medias por un reinicio
                if t.get("estado") == "en_curso":
                    t["estado"] = "fallida"
                    t["resumen"] = "El servicio se reinició durante la tarea; no se pudo confirmar el resultado."
        except Exception:
            pass


_load()


def auth(authorization: Optional[str] = Header(default=None)) -> None:
    if not config.TEAM_TOKEN:
        return  # modo desarrollo local sin token
    if authorization != f"Bearer {config.TEAM_TOKEN}":
        raise HTTPException(status_code=401, detail="token inválido")


# ---------------------------------------------------------------- endpoints
@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/tasks")
async def create_task(body: TaskIn, _: None = Depends(auth)):
    task_id = uuid.uuid4().hex[:12]
    desktop = body.escritorio.url.strip()
    TASKS[task_id] = {
        "task_id": task_id,
        "estado": "en_curso",
        "resumen": "Tarea recibida; en curso.",
        "tarea": body.tarea,
        "creada": time.time(),
    }
    # Un solo trabajador por escritorio: si había otra tarea viva en este escritorio, se cancela.
    prev = RUNNING_BY_DESKTOP.get(desktop)
    if prev and not prev[1].done():
        prev[2].set()
        prev[1].cancel()
        log.info("cancelada tarea previa %s en el mismo escritorio", prev[0])
    cancel = asyncio.Event()
    job = asyncio.create_task(_run(task_id, body.tarea, desktop, body.escritorio.password, cancel))
    RUNNING_BY_DESKTOP[desktop] = (task_id, job, cancel)
    _persist()
    return {"task_id": task_id}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, _: None = Depends(auth)):
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="tarea no encontrada")
    return {"estado": t["estado"], "resumen": t["resumen"]}


@app.get("/traces/{task_id}", response_class=HTMLResponse)
async def trace_view(task_id: str, token: str = Query(default="")):
    if config.TEAM_TOKEN and token != config.TEAM_TOKEN:
        raise HTTPException(status_code=401, detail="token inválido")
    return HTMLResponse(render_trace_html(task_id, token))


@app.get("/traces/{task_id}/{name}")
async def trace_file(task_id: str, name: str, token: str = Query(default="")):
    if config.TEAM_TOKEN and token != config.TEAM_TOKEN:
        raise HTTPException(status_code=401, detail="token inválido")
    if "/" in name or ".." in name:
        raise HTTPException(status_code=400)
    p = config.TRACE_DIR / task_id / name
    if not p.exists():
        raise HTTPException(status_code=404)
    return FileResponse(p)


@app.get("/memory")
async def memory_view(_: None = Depends(auth)):
    """Lo que el agente ha aprendido de cada app (mapa, flujos, reglas, incidentes)."""
    out = {}
    for p in config.MEMORY_DIR.glob("*.json"):
        try:
            out[p.stem] = json.loads(p.read_text())
        except Exception:
            pass
    return JSONResponse(out)


# ---------------------------------------------------------------- ejecución con watchdog
async def _run(task_id: str, tarea: str, desktop: str, password: str, cancel: asyncio.Event) -> None:
    from .agent import run_task  # import diferido: el servicio arranca aunque falte algo del agente

    secrets = [password, config.TEAM_TOKEN, desktop, config.ANTHROPIC_API_KEY]
    trace = Trace(task_id, tarea)
    estado, resumen = "fallida", "No se pudo completar la tarea."
    try:
        estado, resumen = await asyncio.wait_for(
            run_task(tarea, desktop, password, cancel, trace),
            timeout=config.REPORT_DEADLINE_S,
        )
    except asyncio.TimeoutError:
        estado = "fallida"
        resumen = "No terminé dentro del tiempo límite; no puedo confirmar que la tarea haya quedado aplicada."
        trace.event("watchdog", "corte por tiempo")
    except asyncio.CancelledError:
        estado = "fallida"
        resumen = "La tarea fue cancelada porque llegó otra tarea para el mismo escritorio."
    except Exception as e:  # nunca dejar la tarea en en_curso
        log.exception("error en tarea %s", task_id)
        estado = "fallida"
        resumen = f"Error interno del agente: {type(e).__name__}."
        trace.event("error", repr(e))
    finally:
        resumen = redact(resumen, secrets + trace.secrets_seen)
        TASKS[task_id].update({"estado": estado, "resumen": resumen, "terminada": time.time()})
        trace.finish(estado, resumen)
        cur = RUNNING_BY_DESKTOP.get(desktop)
        if cur and cur[0] == task_id:
            RUNNING_BY_DESKTOP.pop(desktop, None)
        _persist()
        log.info("tarea %s -> %s", task_id, estado)


@app.on_event("startup")
async def _startup():
    if config.CANARY_INTERVAL_S > 0 and config.CANARY_DESKTOP_URL:
        from .canary import canary_loop

        asyncio.create_task(canary_loop())
