"""Memoria persistente por app (huella). Es lo que el agente 'aprende' y reutiliza entre tareas:

- menus:     rutas de menú descubiertas en el recorrido de solo lectura, ancladas a texto
- screens:   pantallas vistas (título -> ruta, columnas, campos)
- flows:     flujos probados por tipo de tarea ("crear:clientes"), con pasos anclados a texto y contador de éxitos
- rules:     reglas descubiertas ("la búsqueda distingue tildes", "al guardar pide motivo")
- incidents: cambios detectados en la app (deriva), con causa y qué se re-aprendió
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from . import config


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "app").lower()).strip("-")[:60] or "app"


class Memory:
    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint
        self.path: Path = config.MEMORY_DIR / f"{_slug(fingerprint)}.json"
        self.data: dict = {
            "fingerprint": fingerprint, "version": "", "map_version": 1, "menus": [], "screens": {},
            "flows": {}, "rules": [], "incidents": [], "tasks_seen": 0, "updated": 0,
        }
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except Exception:
                pass

    # ---------------------------------------------------------------- persistencia
    def save(self) -> None:
        self.data["updated"] = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    # ---------------------------------------------------------------- menús y pantallas
    @property
    def menus(self) -> list[list[str]]:
        return self.data["menus"]

    def set_menus(self, paths: list[list[str]]) -> None:
        self.data["menus"] = paths

    def remember_screen(self, title: str, route: Optional[list[str]] = None, columns: Optional[list[str]] = None,
                        fields: Optional[list[str]] = None) -> None:
        if not title:
            return
        sc = self.data["screens"].setdefault(title, {"visto": 0})
        sc["visto"] += 1
        if route:
            sc["ruta"] = route
        if columns:
            sc["columnas"] = columns
        if fields:
            sc["campos"] = sorted(set(sc.get("campos", []) + fields))

    # ---------------------------------------------------------------- flujos y reglas
    def flow(self, key: str) -> Optional[dict]:
        return self.data["flows"].get(key)

    def save_flow(self, key: str, steps: list[dict]) -> None:
        f = self.data["flows"].setdefault(key, {"pasos": [], "ok": 0})
        f["pasos"] = steps[-25:]
        f["ok"] += 1
        f["ultima"] = time.time()

    def fail_flow(self, key: str) -> None:
        f = self.data["flows"].get(key)
        if f:
            f["fallos"] = f.get("fallos", 0) + 1

    def add_rule(self, rule: str) -> None:
        rule = (rule or "").strip()
        if rule and rule not in self.data["rules"]:
            self.data["rules"] = (self.data["rules"] + [rule])[-30:]

    # ---------------------------------------------------------------- deriva / incidente
    def record_incident(self, cause: str, relearned: str = "") -> None:
        self.data["incidents"].append({"t": time.time(), "causa": cause, "re_aprendido": relearned,
                                       "map_version_antes": self.data["map_version"]})
        self.data["map_version"] += 1

    def summary(self) -> str:
        d = self.data
        lines = [f"App: {d['fingerprint']} {d.get('version', '')} (mapa v{d['map_version']}, tareas vistas: {d['tasks_seen']})"]
        if d["menus"]:
            lines.append("Rutas de menú conocidas: " + "; ".join(" > ".join(p) for p in d["menus"]))
        for t, sc in list(d["screens"].items())[:20]:
            extra = []
            if sc.get("ruta"):
                extra.append("ruta " + " > ".join(sc["ruta"]))
            if sc.get("columnas"):
                extra.append("columnas " + ", ".join(sc["columnas"]))
            if sc.get("campos"):
                extra.append("campos " + ", ".join(sc["campos"][:15]))
            lines.append(f"Pantalla '{t}': " + " | ".join(extra))
        if d["rules"]:
            lines.append("Reglas descubiertas: " + " · ".join(d["rules"]))
        return "\n".join(lines)
