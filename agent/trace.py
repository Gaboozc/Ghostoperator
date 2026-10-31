"""Traza por tarea: cada paso con captura, acción y razón. Es la evidencia para el debrief y el pitch."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from . import config


class Trace:
    def __init__(self, task_id: str, tarea: str):
        self.task_id = task_id
        self.dir: Path = config.TRACE_DIR / task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.t0 = time.time()
        self.steps: list[dict] = []
        self.secrets_seen: list[str] = []  # valores que la app marca como secretos (nunca van al resumen)
        self.meta = {"task_id": task_id, "tarea": tarea, "inicio": self.t0}
        self.llm_calls = 0
        self.actions = 0
        self._n = 0

    def elapsed(self) -> float:
        return time.time() - self.t0

    def event(self, kind: str, detail: str = "", image=None, data: dict | None = None) -> None:
        self._n += 1
        rec = {"n": self._n, "t": round(self.elapsed(), 2), "kind": kind, "detail": detail}
        if data:
            rec["data"] = data
        if image is not None:
            try:
                name = f"{self._n:03d}.jpg"
                img = image.convert("RGB")
                img.save(self.dir / name, quality=70)
                rec["img"] = name
            except Exception:
                pass
        self.steps.append(rec)
        self._flush()

    def finish(self, estado: str, resumen: str) -> None:
        self.meta.update({
            "estado": estado,
            "resumen": resumen,
            "duracion_s": round(self.elapsed(), 1),
            "llm_calls": self.llm_calls,
            "acciones": self.actions,
        })
        self._flush()

    def _flush(self) -> None:
        try:
            (self.dir / "trace.json").write_text(
                json.dumps({"meta": self.meta, "steps": self.steps}, ensure_ascii=False, indent=1)
            )
        except Exception:
            pass


def render_trace_html(task_id: str, token: str) -> str:
    p = config.TRACE_DIR / task_id / "trace.json"
    if not p.exists():
        return "<p>Sin traza.</p>"
    d = json.loads(p.read_text())
    m = d["meta"]
    q = f"?token={html.escape(token)}" if token else ""
    rows = []
    for s in d["steps"]:
        img = f'<img src="/traces/{task_id}/{s["img"]}{q}" loading="lazy">' if s.get("img") else ""
        data = f'<pre>{html.escape(json.dumps(s.get("data"), ensure_ascii=False, indent=1))}</pre>' if s.get("data") else ""
        rows.append(
            f'<div class="s"><div class="h"><b>#{s["n"]}</b> <span>{s["t"]} s</span> '
            f'<code>{html.escape(s["kind"])}</code> {html.escape(s["detail"])}</div>{data}{img}</div>'
        )
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Traza {task_id}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;margin:16px;background:#f6f7f9;color:#1f2a37}}
.top{{background:#fff;border:1px solid #d6dbe3;border-radius:8px;padding:12px;margin-bottom:12px}}
.s{{background:#fff;border:1px solid #d6dbe3;border-radius:8px;padding:10px;margin:8px 0}}
.h span{{color:#5b6575}} img{{max-width:100%;border:1px solid #d6dbe3;margin-top:6px}}
pre{{background:#f2f4f7;padding:6px;overflow:auto;font-size:12px}} code{{background:#eef2f8;padding:1px 4px}}</style></head><body>
<div class="top"><h2>Tarea {html.escape(task_id)}</h2><p><b>Instrucción:</b> {html.escape(m.get("tarea", ""))}</p>
<p><b>Estado:</b> {html.escape(str(m.get("estado", "en_curso")))} · <b>Duración:</b> {m.get("duracion_s", "-")} s ·
<b>Llamadas al modelo:</b> {m.get("llm_calls", "-")} · <b>Acciones:</b> {m.get("acciones", "-")}</p>
<pre>{html.escape(str(m.get("resumen", "")))}</pre></div>{''.join(rows)}</body></html>"""
