"""Canaria: cada CANARY_INTERVAL_S revisa (solo lectura) nuestro escritorio de pruebas y compara con la memoria.
Si cambió la versión, el menú superior o alguna ruta conocida, registra un incidente y marca el mapa para re-explorar.
Escribe data/incident_log.json: causa detectada, qué se re-aprendió y cuándo (evidencia para el debrief y el pitch)."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import config
from .memory import Memory
from .rfb import RFBClient
from .screen import build_state, norm

log = logging.getLogger("ghost.canary")
LOG_FILE = config.DATA_DIR / "incident_log.json"


def _append_log(entry: dict) -> None:
    data = []
    if LOG_FILE.exists():
        try:
            data = json.loads(LOG_FILE.read_text())
        except Exception:
            data = []
    data.append(entry)
    LOG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1))


async def check_once() -> dict:
    vnc = RFBClient(config.CANARY_DESKTOP_URL, config.CANARY_DESKTOP_PASSWORD)
    t0 = time.time()
    await vnc.connect()
    try:
        img = await vnc.capture_stable()
        s = await asyncio.to_thread(build_state, img)
        latency = time.time() - t0
        mem = Memory(s.fingerprint)
        causes = []
        if mem.data.get("version") and s.version and s.version != mem.data["version"]:
            causes.append(f"versión {mem.data['version']} -> {s.version}")
        known_tops = {norm(p[0]) for p in mem.menus}
        visible_tops = {norm(m.text) for m in s.menu}
        if known_tops and not known_tops.issubset(visible_tops):
            causes.append(f"menú superior cambió: faltan {sorted(known_tops - visible_tops)}")
        entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "app": s.fingerprint, "version": s.version,
                 "latencia_s": round(latency, 2), "cambios": causes}
        if causes:
            mem.record_incident("; ".join(causes), "mapa marcado para re-explorar en la siguiente tarea")
            mem.set_menus([])
            mem.save()
            _append_log(entry)
            log.warning("CANARIA: deriva detectada: %s", causes)
        return entry
    finally:
        await vnc.close()


async def canary_loop() -> None:
    while True:
        try:
            await check_once()
        except Exception as e:
            log.warning("canaria falló: %s", e)
            _append_log({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "error": str(e)})
        await asyncio.sleep(config.CANARY_INTERVAL_S)
