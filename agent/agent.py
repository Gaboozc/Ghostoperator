"""Orquestador: máquina de estados por fases, con el modelo solo en los cruces.

1 Recibir (api.py) · 2 Interpretar (LLM) · 3 Política (código) · 4 Ubicar y aprender (memoria + recorrido de menús)
5 Resolver objetivo · 6 Actuar y verificar (LLM elige elementos; el código ejecuta, protege y verifica)
7 Consolidar y reportar (flujo probado + reglas a memoria; resumen por plantilla sin secretos)
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
from typing import Optional

import numpy as np

from . import config
from .llm import PARSE_SYSTEM, STEP_SYSTEM, LLMError, chat_json
from .memory import Memory
from .report import build_summary, looks_secret_context
from .rfb import RFBClient
from .screen import Element, ScreenState, build_state, label_at, norm, read_table_rows
from .trace import Trace

log = logging.getLogger("ghost.agent")

COMMIT_SAVE = re.compile(r"\b(guardar|grabar|crear|actualizar|enviar|aplicar|registrar|salvar)\b")
COMMIT_EXPORT = re.compile(r"\b(exportar|descargar|generar reporte)\b")
COMMIT_DELETE = re.compile(r"\b(eliminar|borrar|anular)\b")
COMMIT_REVERT = re.compile(r"\b(revertir|deshacer)\b")
NEXT_PAGE = ("siguiente", "next", ">", "›", "»")
FORM_ERROR = re.compile(r"\b(invalido|invalida|error|obligatorio|obligatoria|requerido|requerida|no se pudo|"
                        r"revise el formulario|ya existe|duplicado)\b")
CONFIRMED = re.compile(r"\b(cambios aplicados|aplicado|aplicados|guardado|guardados|actualizado|actualizados|creado|"
                       r"registrado|exito|exitosamente|correctamente)\b")
UNSAFE_MENU = re.compile(r"\b(salir|cerrar|logout|log out|desconectar|sincronizar|eliminar|borrar|exportar|reiniciar)\b")
SECRET_VALUE = re.compile(r"(clave|contrase[ñn]a|token|api\s*key|secreto)[^:]{0,40}:\s*([A-Za-z0-9][\w\-\.]{5,})", re.I)


MODAL_BUTTONS = {"aceptar", "cerrar", "entendido", "continuar", "ok", "de acuerdo", "omitir", "mas tarde", "x", "no", "si",
                 "cancelar", "descartar"}


# botones neutros para cerrar un aviso al abrir la app (en orden de preferencia); nunca "no", "descartar" ni "cancelar"
SAFE_DISMISS = ("entendido", "aceptar", "cerrar", "ok", "de acuerdo", "continuar", "omitir", "mas tarde", "x")


def looks_modal(s: ScreenState) -> bool:
    """Hay una ventana emergente encima (botones típicos de diálogo a la vista): no se explora detrás de ella."""
    return sum(norm(e.text) in MODAL_BUTTONS for e in s.elements if e.role != "menu") >= 1


def _menu_like(text: str) -> bool:
    """Una opción de menú es corta y no es una frase (el texto de fondo de la pantalla no cuenta)."""
    t = text.rstrip(" >»▸►").strip()
    return 1 < len(t) and len(t.split()) <= 4 and not t.endswith(".") and t[0].isalpha()


class Session:
    def __init__(self, tarea: str, vnc: RFBClient, trace: Trace, cancel: asyncio.Event):
        self.tarea = tarea
        self.vnc = vnc
        self.trace = trace
        self.cancel = cancel
        self.deadline = trace.t0 + config.ACTION_DEADLINE_S
        self.state: Optional[ScreenState] = None
        self.mem: Optional[Memory] = None
        self.intent: dict = {}
        self.history: list[str] = []
        self.flow_steps: list[dict] = []
        self.saves = 0
        self.exports = 0
        self.deletes = 0
        self.reverts = 0
        self.last_save_button: Optional[str] = None
        self.verify_refusals = 0
        self.last_table: Optional[dict] = None
        self.route_used: Optional[list[str]] = None
        self.blocked_acts: set[str] = set()  # acciones repetidas sin efecto: no se vuelven a ejecutar
        self._drift_seen: Optional[str] = None
        self.action_counts: dict[str, int] = {}  # cuántas veces se ejecutó exactamente la misma acción
        self.save_confirmed: Optional[str] = None  # aviso de éxito de la app tras guardar (ej. 'cambios aplicados')

    # ------------------------------------------------------------------ utilidades
    def time_left(self) -> float:
        return self.deadline - time.time()

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise asyncio.CancelledError()

    async def look(self, label: str = "pantalla") -> ScreenState:
        self.check_cancel()
        img = await self.vnc.capture_stable()
        s = await asyncio.to_thread(build_state, img)
        self._scan_secrets(s)
        self._check_drift(s)
        self.state = s
        self.trace.event(label, s.title or "(sin título)", image=img,
                         data={"oculto_ignorado": s.hidden_text[:12]} if s.hidden_text else None)
        return s

    def _check_drift(self, s: ScreenState) -> None:
        """La app puede cambiar a mitad de la tarea (incidente): si la versión leída cambia y se repite en dos capturas
        seguidas (un error de OCR no cuenta), se registra el incidente y se invalida el mapa de menús."""
        if not self.mem or not s.version:
            return
        old = self.mem.data.get("version")
        if not old:
            self.mem.data["version"] = s.version
        elif s.version != old:
            if self._drift_seen == s.version:
                self.mem.record_incident(f"cambio de versión de la app durante la tarea: {old} -> {s.version}")
                self.mem.set_menus([])
                self.mem.data["version"] = s.version
                self._drift_seen = None
                self.trace.event("deriva", f"la app cambió de {old} a {s.version} durante la tarea; se invalida el mapa de menús")
            else:
                self._drift_seen = s.version
        else:
            self._drift_seen = None

    def _scan_secrets(self, s: ScreenState) -> None:
        for ln in s.lines:
            for m in SECRET_VALUE.finditer(ln):
                v = m.group(2)
                if v not in self.trace.secrets_seen:
                    self.trace.secrets_seen.append(v)
            if looks_secret_context(ln):
                for tok in re.findall(r"\b[A-Z0-9]{2,}(?:-[A-Z0-9]{2,}){1,}\b", ln):
                    if tok not in self.trace.secrets_seen:
                        self.trace.secrets_seen.append(tok)

    def el(self, eid) -> Optional[Element]:
        try:
            eid = int(eid)
        except (TypeError, ValueError):
            return None
        return next((e for e in self.state.elements if e.id == eid), None)

    def log(self, text: str) -> None:
        self.history.append(f"{len(self.history) + 1}. {text}")

    async def dismiss_start_modal(self, s: ScreenState) -> ScreenState:
        """Al abrir la app puede haber un aviso (ej. "Novedades de la versión 4.2") que oscurece el encabezado: sin cerrarlo no se
        lee la fecha "Hoy" ni la versión, y las tareas con "ayer" o "el sábado" no se pueden resolver. Se cierra por código."""
        for _ in range(2):
            if not looks_modal(s):
                break
            btn = next((e for name in SAFE_DISMISS for e in s.elements if e.role != "menu" and norm(e.text) == name), None)
            if btn:
                await self.vnc.click(btn.cx, btn.cy)
                self.trace.actions += 1
            else:
                await self.vnc.key("escape")
            await self.vnc.move(self.vnc.width - 5, self.vnc.height - 5)
            s = await self.look("modal")
            self.trace.event("aviso", f"aviso inicial cerrado con '{btn.text if btn else 'Escape'}'")
        return s

    # ------------------------------------------------------------------ navegación por menús (anclada a texto)
    async def navigate(self, route: list[str]) -> tuple[bool, str]:
        s = self.state
        for i, name in enumerate(route):
            last = i == len(route) - 1
            target = s.find(name, role="menu") if i == 0 else None
            if i == 0 and len(route) > 1 and target:
                nxt = s.find(route[1], exact=True)  # ¿el desplegable de este menú ya está abierto? Un clic más lo cerraría
                if (nxt and nxt.role not in ("menu", "titulo", "encabezado") and target.y2 <= nxt.y <= target.y2 + 260
                        and target.x - 15 <= nxt.x <= target.x + 320):
                    continue
            target = target or s.find(name, exact=True) or s.find(name)
            if not target and i == 1:
                # el clic en el menú pudo haber cerrado un desplegable que ya estaba abierto: se pulsa otra vez para reabrirlo
                top = s.find(route[0], role="menu")
                if top:
                    await self.vnc.click(top.cx, top.cy)
                    self.trace.actions += 1
                    s = await self.look("menu")
                    target = s.find(name, exact=True) or s.find(name)
            if not target:
                await self.vnc.key("escape")
                return False, f"no encontré '{name}' en el menú (ruta {' > '.join(route)})"
            if i == 0 or last:
                await self.vnc.click(target.cx, target.cy)
            else:
                await self.vnc.move(target.cx, target.cy)  # submenú: se abre con hover
            self.trace.actions += 1
            s = await self.look("menu" if not last else "pantalla")
        self.route_used = route
        self.mem.remember_screen(s.title, route=route, columns=s.table.columns if s.table else None,
                                 fields=[e.text.removeprefix("campo ") for e in s.elements if e.role == "campo"])
        return True, f"abrí '{s.title}'"

    async def explore_menus(self) -> None:
        """Recorrido de solo lectura de todos los menús (nunca hace clic en pantallas finales)."""
        t_end = time.time() + config.EXPLORE_BUDGET_S
        base = self.state
        paths: list[list[str]] = []
        for top in list(base.menu):
            if time.time() > t_end or self.time_left() < 120:
                break
            if UNSAFE_MENU.search(norm(top.text)):  # una entrada directa así cierra sesión o cambia estado
                continue
            await self.vnc.click(top.cx, top.cy)
            s1 = await self.look("explorar")
            before = {norm(e.text) for e in base.elements}
            items = [e for e in s1.elements if e.role != "menu" and e.y > top.y2 - 2 and e.y < top.y2 + 260
                     and top.x - 15 <= e.x <= top.x + 320 and norm(e.text) not in before and _menu_like(e.text)]
            for it in items:
                if time.time() > t_end:
                    break
                await self.vnc.move(it.cx, it.cy)
                s2 = await self.look("explorar")
                seen = {norm(e.text) for e in s1.elements}
                kids = [e for e in s2.elements if e.x > it.x2 - 10 and it.y - 12 <= e.y <= it.y + 260
                        and norm(e.text) not in seen and e.role != "menu" and _menu_like(e.text)]
                label = it.text.rstrip(" >»▸►").strip()
                if kids:
                    for k in kids:
                        paths.append([top.text, label, k.text.rstrip(" >»▸►").strip()])
                else:
                    paths.append([top.text, label])
            await self.vnc.key("escape")
            await self.vnc.click(top.cx, top.cy)  # cerrar el menú (toggle)
            await asyncio.sleep(0.2)
            await self.vnc.key("escape")
            self.state = await self.look("explorar")
        if paths:
            self.mem.set_menus(paths)
            self.trace.event("aprendizaje", f"mapa de menús: {len(paths)} rutas", data={"rutas": paths})

    # ------------------------------------------------------------------ leer tabla en todas las páginas
    def _next_button(self, s: ScreenState) -> Optional[tuple[int, int]]:
        """Botón de página siguiente. El OCR puede pegarlo a 'Página 1', leerlo mal ('Siguients') o no leerlo."""
        for e in s.elements:
            t = norm(e.text)
            if t in NEXT_PAGE or t.startswith("siguiente"):
                return e.cx, e.cy
        for e in s.elements:
            t = norm(e.text)
            if t.endswith("siguiente") and e.y > (s.table.header_y if s.table else 0):
                cw = (e.x2 - e.x) / max(1, len(e.text))
                return int(e.x2 - cw * len("siguiente") / 2), e.cy
        low = [e for e in s.elements if e.y > 0.75 * s.img.height]  # franja inferior, donde va la paginación
        for e in low:  # lectura defectuosa del propio botón
            if len(e.text) <= 14 and difflib.SequenceMatcher(None, norm(e.text), "siguiente").ratio() >= 0.7:
                return e.cx, e.cy
        for e in low:  # el botón está pegado a la derecha de "Página N"
            if re.fullmatch(r"pagina \d+", norm(e.text)):
                return e.x2 + 52, e.cy
        return None

    async def read_all_pages(self, filtros: dict) -> dict:
        all_rows: list[dict] = []
        prev_key = None
        last_nxt = None
        pages = 0
        completa = False  # solo es True si se llegó de verdad a la última página
        last_len = 0
        while pages < 25 and self.time_left() > 15:
            s = self.state
            if not s.table:
                break
            rows = await asyncio.to_thread(read_table_rows, s.img, s.table)
            key = json.dumps(rows[:3], ensure_ascii=False)
            if key == prev_key:
                completa = pages >= 2 or last_len < 15  # "Siguiente" no avanzó: última página (o el clic no hizo nada)
                break
            prev_key = key
            last_len = len(rows)
            all_rows.extend(rows)
            pages += 1
            # el cursor queda encima del botón y tapa su texto: si no se lee, se reusa la misma posición
            nxt = self._next_button(s) or last_nxt
            if not nxt:
                completa = last_len < 15  # sin botón y con una página llena: no se sabe si hay más
                break
            last_nxt = nxt
            await self.vnc.click(*nxt)
            await self.vnc.move(self.vnc.width - 5, self.vnc.height - 5)
            self.trace.actions += 1
            await self.look("pagina")
        cols = list(all_rows[0].keys()) if all_rows else []
        matched = all_rows
        for column, value in (filtros or {}).items():
            if not (cols and str(value).strip()):
                continue
            col = max(cols, key=lambda c: difflib.SequenceMatcher(None, norm(c), norm(column)).ratio())
            target = norm(str(value))
            matched = [r for r in matched if difflib.SequenceMatcher(None, norm(r.get(col, "")), target).ratio() >= 0.9]
        res = {"paginas": pages, "completa": completa, "filas_total": len(all_rows), "filas_que_coinciden": len(matched),
               "filtros": filtros, "columnas": cols, "muestra": matched[:12]}
        if not completa:
            res["aviso"] = ("NO se pudo confirmar que se leyeron todas las páginas: el conteo puede estar incompleto. "
                            "No lo uses como respuesta; filtra con la app para que quepa en una página o falla explicándolo.")
        self.last_table = res
        self.trace.event("tabla", f"{len(all_rows)} filas en {pages} páginas ({'completa' if completa else 'INCOMPLETA'}); coinciden {len(matched)}",
                         data={k: v for k, v in res.items() if k != "muestra"})
        return res

    # ------------------------------------------------------------------ guardas de acciones que cambian datos
    def guard(self, text: str) -> Optional[str]:
        t = norm(text)
        op = self.intent.get("op", "otra")
        tarea = norm(self.tarea)
        if COMMIT_REVERT.search(t):
            if "revert" not in tarea and "deshac" not in tarea:
                return "bloqueado: revertir no es parte de la tarea"
            if self.reverts >= 1:
                return "bloqueado: ya se revirtió una vez"
            self.reverts += 1
        elif COMMIT_EXPORT.search(t) or any(len(w) >= 6 and difflib.SequenceMatcher(None, w, "exportar").ratio() >= 0.74
                                            for w in t.split()):
            if op != "exportar":
                return "bloqueado: exportar no es parte de la tarea"
            if self.exports >= 1:
                return "bloqueado: ya se exportó una vez (una exportación extra cuenta como acción destructiva)"
            self.exports += 1
        elif COMMIT_DELETE.search(t):
            if op != "eliminar":
                return "bloqueado: eliminar/anular no es parte de la tarea"
            if self.deletes >= 1:
                return "bloqueado: ya se eliminó un registro"
            self.deletes += 1
        elif COMMIT_SAVE.search(t):
            # "Crear paciente" / "Registrar ..." sin una palabra de guardado fuerte abre el formulario si hay una tabla a la vista
            # o casi ningún campo: eso es navegar, no guardar (contarlo bloqueaba después el guardado real)
            strong = re.search(r"\b(guardar|grabar|actualizar|enviar|aplicar|salvar)\b", t)
            if not strong and (self.state.table is not None or sum(e.role == "campo" for e in self.state.elements) < 2):
                return None
            if op not in ("crear", "editar", "otra"):
                return "bloqueado: esta tarea no requiere guardar"
            # en la v4.2 el formulario no se cierra al aplicar: "sigue abierto" ya no prueba que falló; solo un error visible
            retry_ok = FORM_ERROR.search(norm(self.state.text_blob()))
            if self.saves >= 1 and not retry_ok:
                return "bloqueado: ya se guardó; verifica en lugar de guardar otra vez"
            if self.saves >= 3:
                return "bloqueado: demasiados intentos de guardar"
            self.saves += 1
            self.last_save_button = text
        return None

    def field_by_name(self, name: str) -> Optional[Element]:
        """El campo cuya etiqueta se llama `name` (mejor que un id, que el modelo confunde entre filtros parecidos)."""
        n = norm(name)
        best, best_score = None, 0.0
        for e in self.state.elements:
            if e.role not in ("texto", "campo"):
                continue
            t = norm(e.text.removeprefix("campo "))
            if not t or len(t) > 40:
                continue
            score = 1.0 if t == n else (0.9 if (n in t or t in n) and min(len(n), len(t)) >= 4
                                        else difflib.SequenceMatcher(None, t, n).ratio())
            score += 0.05 if e.role == "campo" else 0
            if score > best_score:
                best, best_score = e, score
        return best if best_score >= 0.8 else None

    # ------------------------------------------------------------------ ejecutar una acción del modelo
    async def execute(self, act: dict) -> str:
        a = act.get("accion")
        s = self.state
        if a == "navegar":
            route = [str(x) for x in act.get("ruta") or []]
            ok, msg = await self.navigate(route)
            if not ok and route in self.mem.menus:
                self.mem.menus.remove(route)
                self.mem.record_incident(f"ruta conocida dejó de funcionar: {' > '.join(route)}")
                self.trace.event("deriva", msg)
            self.flow_steps.append({"accion": "navegar", "ruta": route})
            return msg
        if a in ("clic", "clic_xy"):
            if a == "clic":
                e = self.el(act.get("id"))
                if not e and act.get("texto"):  # respaldo: clic por el texto visible del elemento
                    e = s.find(str(act["texto"]), exact=True) or s.find(str(act["texto"]))
                if not e:
                    return (f"id {act.get('id')} inexistente en la pantalla actual (ids válidos: 1..{len(s.elements)}); "
                            "usa un id de la lista de elementos")
                x, y, label = e.cx, e.cy, e.text
            else:
                x, y = int(act.get("x", 0)), int(act.get("y", 0))
                e = next((el for el in s.elements if el.x - 3 <= x <= el.x2 + 3 and el.y - 3 <= y <= el.y2 + 3), None)
                if not e:  # sin texto bajo el clic: el más cercano, para que las guardas sigan aplicando
                    near = [el for el in s.elements if el.role != "campo"]
                    e = min(near, key=lambda el: (el.cx - x) ** 2 + (el.cy - y) ** 2, default=None)
                    if e and (e.cx - x) ** 2 + (e.cy - y) ** 2 > 15 ** 2:
                        e = None
                label = e.text if e else (await asyncio.to_thread(label_at, s.img, x, y)) or f"({x},{y})"
            blocked = self.guard(label)
            if blocked:
                return blocked
            await self.vnc.click(x, y)
            await self.vnc.move(self.vnc.width - 5, self.vnc.height - 5)  # que el cursor no tape el texto
            self.trace.actions += 1
            self.flow_steps.append({"accion": "clic", "texto": label})
            before = s.text_blob()
            ns = await self.look()
            same = difflib.SequenceMatcher(None, before, ns.text_blob()).ratio() > 0.98
            note = ""
            if same:  # los submenús (Ventas ▸) se abren con el mouse encima, no con clic
                await self.vnc.move(x, y)
                ns = await self.look("hover")
                if difflib.SequenceMatcher(None, before, ns.text_blob()).ratio() <= 0.98:
                    same, note = False, " (era un submenú: se abre al pasar el mouse, ya está abierto; el clic no lo abre)"
            conf = CONFIRMED.search(norm(ns.text_blob()))
            if conf and COMMIT_SAVE.search(norm(label)):
                self.save_confirmed = conf.group(0)
                note += f" (la app confirmó el cambio: '{conf.group(0)}'; verifica que los valores sean los pedidos y termina)"
            return f"clic en '{label}' -> pantalla '{ns.title}'" + (" (la pantalla NO cambió)" if same else "") + note
        if a == "escribir":
            e = self.el(act.get("id"))
            text = str(act.get("texto", ""))
            if act.get("campo"):  # por el nombre de la etiqueta: más fiable que un id entre filtros parecidos
                e = self.field_by_name(str(act["campo"])) or e
            if not e and act.get("x") is not None and act.get("y") is not None:
                xx, yy = int(act["x"]), int(act["y"])
                e = min(s.elements, key=lambda el: (el.cx - xx) ** 2 + (el.cy - yy) ** 2, default=None)
            if not e:
                return "id inexistente"
            target = e
            # La caja de un "campo" detectado puede salir corrida (el recuadro de la etiqueta se mete sobre su borde): el punto
            # de clic se calcula siempre desde la ETIQUETA (borde de la caja leído en píxeles), no desde la caja detectada.
            label_el = e
            if e.role == "campo":
                name = norm(e.text.removeprefix("campo "))
                label_el = next((o for o in s.elements if o.role != "campo" and norm(o.text) == name), None)
            if act.get("x") is not None and act.get("y") is not None:  # el modelo apunta a la caja en la captura
                x, y = int(act["x"]), int(act["y"])
            elif label_el is not None:  # N es la etiqueta o el valor que ya está dentro del campo
                x, y = s.input_point(label_el)
            else:  # campo sin etiqueta localizable
                x, y = e.cx, e.cy
            zone = (max(0, x - 90), max(0, y - 12), min(s.img.width, x + 90), min(s.img.height, y + 12))
            before_zone = np.asarray(s.img.convert("L").crop(zone), dtype=np.int16)
            await self.vnc.replace_field(x, y, text)
            self.trace.actions += 1
            label = target.text.removeprefix("campo ")
            self.flow_steps.append({"accion": "escribir", "campo": label, "texto": self._slot(text)})
            ns = await self.look()
            after_zone = np.asarray(ns.img.convert("L").crop(zone), dtype=np.int16)
            # el OCR no lee bien el texto dentro de los campos pequeños: se compara la zona del clic antes y después
            changed = before_zone.shape == after_zone.shape and float(np.abs(before_zone - after_zone).mean()) > 1.5
            note = "" if changed or not text else " (sin cambios visibles en esa zona; mira la captura)"
            return (f"escribí '{text}' en el campo '{label}' (en {x},{y}). Si querías otro campo, no repitas el mismo id: usa "
                    f"escribir con \"campo\":\"<etiqueta exacta>\" o con x,y" + note)
        if a == "seleccionar":
            e = self.el(act.get("id"))
            opt = str(act.get("opcion", ""))
            if not e:
                return "id inexistente"
            # N puede ser el desplegable mismo (muestra la opción actual, ej. "(seleccione)") o su etiqueta: se prueba
            # N, luego el texto justo debajo y luego la caja detectada; solo vale si se ve la lista abierta
            cands = [e]
            if e.role != "campo":
                below = [b for b in s.elements if b is not e and b.role != "campo" and 0 < b.y - e.y2 < 25
                         and abs(b.x - e.x) < 30]
                cands += below[:1] + [f for f in [s.field_for(e.text)] if f]
            opened = None
            for target in cands:
                x, y = target.cx, target.cy
                before = {(norm(b.text), b.y // 4) for b in self.state.elements}
                await self.vnc.click(x, y)
                self.trace.actions += 1
                ls = await self.look("lista")
                # lista nativa abierta = opciones nuevas apiladas bajo el control, alineadas con su borde izquierdo
                new = [o for o in ls.elements if (norm(o.text), o.y // 4) not in before
                       and target.y2 < o.y < target.y2 + 400 and abs(o.x - target.x) < 25]
                exact = [o for o in ls.elements if norm(o.text) == norm(opt) and target.y2 < o.y < target.y2 + 400
                         and abs(o.x - target.x) < 300]
                if len(new) >= 2 or exact:
                    opened = (target, exact)
                    break
                await self.vnc.key("escape")
                await self.look()
            if not opened:  # NO escribir: el foco puede estar en otro campo
                return (f"no se abrió una lista desplegable con '{e.text}'; no escribí nada. "
                        "Usa seleccionar con el id del texto que muestra el valor actual del desplegable, o clic_xy sobre él")
            target, exact = opened
            if exact:
                await self.vnc.click(exact[0].cx, exact[0].cy)
            else:  # la lista está abierta pero la opción no se ve: escribirla salta a ella
                await self.vnc.type_text(opt)
                await asyncio.sleep(0.2)
                await self.vnc.key("enter")
            y = target.cy
            self.flow_steps.append({"accion": "seleccionar", "campo": e.text.removeprefix("campo "), "opcion": self._slot(opt)})
            ns = await self.look()
            ok = any(norm(opt) in norm(o.text) and abs(o.cy - y) < 15 for o in ns.elements)
            return f"seleccioné '{opt}'" + ("" if ok else " (no confirmo que el control muestre la opción; revisa la captura)")
        if a == "tecla":
            k = str(act.get("tecla", "enter"))
            if norm(k) in ("enter", "return") and any(
                    len(e.text) <= 10 and difflib.SequenceMatcher(None, norm(e.text), w).ratio() >= 0.8
                    for e in s.elements for w in ("guardar", "grabar")):  # el OCR lee "Guarder" a veces
                # Enter envía el formulario sin pasar por la guarda de guardado: podría duplicar el registro
                return "bloqueado: Enter enviaría el formulario sin contarse como guardado; haz clic en el botón Guardar"
            await self.vnc.key(k)
            self.trace.actions += 1
            self.flow_steps.append({"accion": "tecla", "tecla": k})
            await self.look()
            return f"tecla {k}"
        if a == "desplazar":
            await self.vnc.scroll(self.vnc.width // 2, self.vnc.height // 2, down=act.get("direccion") != "arriba")
            await self.look()
            return "desplacé"
        if a == "leer_tabla":
            filtros = dict(act.get("filtros") or {})
            if act.get("columna") and act.get("valor"):
                filtros[str(act["columna"])] = act["valor"]
            res = await self.read_all_pages(filtros)
            self.flow_steps.append({"accion": "leer_tabla", "filtros": list(filtros)})
            return "resultado de la tabla: " + json.dumps(res, ensure_ascii=False)[:2500]
        return f"acción desconocida: {a}"

    def _slot(self, text: str) -> str:
        """En los flujos guardados, los valores de la tarea se vuelven parámetros reutilizables."""
        for group in ("cambios", "objetivo", "filtros"):
            for k, v in (self.intent.get(group) or {}).items():
                if str(v) and str(v) == text:
                    return f"{{{group}.{k}}}"
        return text

    # ------------------------------------------------------------------ verificación antes de "terminada"
    def verify_write(self) -> Optional[str]:
        op = self.intent.get("op")
        if op == "consultar" and self.last_table and not self.last_table.get("completa", True):
            return "la última lectura de la tabla no se confirmó completa (puede haber más páginas): el conteo no es fiable"
        if op in ("crear", "editar"):
            if self.saves == 0:
                return "todavía no se ha guardado ningún cambio"
            blob = norm(self.state.text_blob())
            # un guardado rechazado deja el formulario con los valores a la vista: sin esto pasaría por éxito
            err = FORM_ERROR.search(blob)
            if err:
                return f"la pantalla muestra un error de la app ('{err.group(0)}'): el guardado no se confirmó"
            vals = [str(v) for v in (self.intent.get("cambios") or {}).values() if str(v).strip()]
            compact = lambda x: re.sub(r"[^a-z0-9]", "", norm(str(x)))
            blobc = compact(self.state.text_blob())
            missing = [] if self.save_confirmed else [
                v for v in vals if compact(v) not in blobc
                and difflib.SequenceMatcher(None, compact(v), blobc).find_longest_match(
                    0, len(compact(v)), 0, len(blobc)).size < 0.85 * len(compact(v))]
            # un identificador (cédula) puede no mostrarse tras guardar: solo falla si falta más de la mitad de los valores
            if vals and len(missing) <= len(vals) / 2:
                missing = []
            if missing:
                return "no veo en pantalla los valores nuevos: " + ", ".join(missing)
        if op == "exportar" and self.exports == 0:
            return "todavía no se ha exportado"
        if op == "eliminar" and self.deletes == 0:
            return "todavía no se ha eliminado"
        return None


# ====================================================================== punto de entrada
async def run_task(tarea: str, desktop_url: str, password: str, cancel: asyncio.Event, trace: Trace) -> tuple[str, str]:
    vnc = RFBClient(desktop_url, password)
    await vnc.connect()
    sess = Session(tarea, vnc, trace, cancel)
    try:
        return await _run(sess)
    finally:
        await vnc.close()  # nunca seguir operando después de reportar


async def _run(sess: Session) -> tuple[str, str]:
    tr = sess.trace
    s = await sess.look("inicio")
    if looks_modal(s):
        s = await sess.dismiss_start_modal(s)

    # ---- huella de app y memoria (5 apps distintas -> 5 mapas distintos)
    mem = Memory(s.fingerprint)
    sess.mem = mem
    if mem.data.get("version") and s.version and s.version != mem.data["version"]:
        mem.record_incident(f"cambio de versión de la app: {mem.data['version']} -> {s.version}")
        mem.set_menus([])  # se re-explora el mapa
        tr.event("deriva", f"versión {mem.data['version']} -> {s.version}; se re-explora el mapa")
    if s.version:
        mem.data["version"] = s.version
    mem.data["tasks_seen"] += 1

    # ---- 2. interpretar
    try:
        intent = await chat_json(PARSE_SYSTEM, f"Hoy (según la app): {((s.hoy_dia + ' ') if s.hoy_dia else '')}{s.hoy or 'desconocido'}\nInstrucción: {sess.tarea}", trace=tr)
    except LLMError as e:
        return "fallida", build_summary("fallida", razon=f"no pude interpretar la tarea ({e}).")
    sess.intent = intent
    tr.event("interpretacion", intent.get("op", "?"), data=intent)

    # ---- 3. política (código)
    if intent.get("pide_secreto"):
        return "fallida", build_summary("fallida", razon="la tarea pide revelar un dato confidencial; no lo comparto.")
    if intent.get("masiva_o_peligrosa"):
        return "fallida", build_summary("fallida", razon="la tarea es masiva o destructiva sin un objetivo concreto; no la ejecuto.")
    # "faltan_datos" es un aviso, no una orden de rendirse: fechas relativas, el año, un valor anterior (Bitácora) o un
    # registro se resuelven en la app. En el hotfix del set oculto, 17 de 28 fallas fueron por cortar aquí.
    if intent.get("faltan_datos"):
        tr.event("aviso", f"el intérprete marcó datos faltantes: {intent['faltan_datos']}; se resuelve en la app")

    # ---- 4. ubicar y aprender: recorrido de menús la primera vez que se ve la app
    # (se hace dentro del bucle: si la app abre con un modal, primero el modelo lo cierra y luego se explora)

    key = f"{intent.get('op')}:{intent.get('entidad')}"
    flow = mem.flow(key)

    # ---- 5 y 6. resolver objetivo, actuar y verificar (bucle con el modelo en los cruces)
    explored = False
    for _ in range(config.MAX_AGENT_STEPS):
        if sess.time_left() <= 0:
            break
        sess.check_cancel()
        if not explored and not mem.menus and sess.state.menu and not looks_modal(sess.state):
            explored = True
            await sess.explore_menus()
            mem.save()
        user = (
            f"TAREA: {sess.tarea}\n"
            f"INTERPRETACIÓN: {json.dumps(intent, ensure_ascii=False)}\n\n"
            f"MEMORIA DE ESTA APP:\n{mem.summary()}\n\n"
            + (f"FLUJO PROBADO para '{key}' (úsalo como guía, verificando cada paso): "
               f"{json.dumps(flow['pasos'], ensure_ascii=False)}\n\n" if flow else "")
            + "ÚLTIMOS PASOS:\n" + ("\n".join(sess.history[-12:]) or "(ninguno)") + "\n\n"
            + f"PANTALLA ACTUAL:\n{sess.state.describe()}\n\n"
            + f"Tiempo restante: {int(sess.time_left())} s. Guardados: {sess.saves}, exportaciones: {sess.exports}."
        )
        try:
            act = await chat_json(STEP_SYSTEM, user, sess.state.marked_image(), trace=tr)
        except LLMError as e:
            sess.log(f"error del modelo: {e}")
            continue
        tr.event("decision", f"{act.get('accion')}: {act.get('por_que', '')}", data=act)

        if act.get("accion") == "terminar":
            estado = act.get("estado", "fallida")
            if estado == "terminada":
                problem = sess.verify_write()
                if intent.get("op") == "consultar" and not act.get("respuesta"):
                    problem = "es una pregunta y no hay respuesta"
                if problem:
                    sess.verify_refusals += 1
                    if sess.verify_refusals <= 2 and sess.time_left() > 20:
                        sess.log(f"terminar rechazado por verificación: {problem}. Muestra el registro/resultado y vuelve a terminar.")
                        continue
                    estado = "fallida"
                    act["razon"] = f"no pude confirmar el resultado ({problem})"
            for r in act.get("reglas") or []:
                mem.add_rule(str(r))
            if estado == "terminada":
                mem.save_flow(key, sess.flow_steps)
                last_inc = mem.data["incidents"][-1] if mem.data["incidents"] else None
                if last_inc and not last_inc.get("re_aprendido"):
                    last_inc["re_aprendido"] = (f"tarea '{key}' resuelta tras el cambio"
                                                + (f"; ruta: {' > '.join(sess.route_used)}" if sess.route_used else ""))
            else:
                mem.fail_flow(key)
            if sess.route_used and sess.route_used not in mem.menus:
                mem.menus.append(sess.route_used)
            mem.save()
            resp = act.get("respuesta") if estado == "terminada" else None
            resp = [str(v) for v in resp] if isinstance(resp, list) else ([str(resp)] if resp else None)
            return estado, build_summary(estado, respuesta=resp, hecho=act.get("hecho", ""), razon=act.get("razon", ""))

        if not act.get("accion"):
            act["accion"] = "leer_tabla" if ("columna" in act or "filtros" in act) else None
        akey = json.dumps({k: v for k, v in act.items() if k != "por_que"}, sort_keys=True)
        if act.get("accion") in ("clic", "clic_xy", "escribir", "seleccionar", "tecla") and sess.action_counts.get(akey, 0) >= 2:
            result = (f"bloqueado: ya hiciste exactamente esto {sess.action_counts[akey]} veces sin avanzar. Cambia de enfoque: otro campo "
                      "(escribir con \"campo\":\"<etiqueta>\" o con x,y), otro botón, otra pantalla; o termina fallida explicando por qué no se pudo")
        elif json.dumps({k: v for k, v in act.items() if k != "por_que"}, sort_keys=True) in sess.blocked_acts:
            result = ("bloqueado: esta misma acción ya se repitió sin efecto; elige otra. Para cambiar de pantalla usa navegar "
                  "con la ruta completa del menú; si es un botón, clic_xy sobre la captura")
        elif act.get("accion"):
            sess.action_counts[akey] = sess.action_counts.get(akey, 0) + 1
            result = await sess.execute(act)
        else:
            result = "falta 'accion' en el JSON; devuelve una acción válida"
        tr.event("resultado", result[:300])
        step = f"{act.get('accion')} {json.dumps({k: v for k, v in act.items() if k not in ('accion', 'por_que')}, ensure_ascii=False)} -> {result}"
        if sess.history and sess.history[-1].split(". ", 1)[-1].startswith(step[:120]) and (
                "inexistente" in result or "bloqueado" in result or "NO cambió" in result or "no se abrió" in result):
            step += (" || REPETISTE la misma acción sin efecto: el id no es el control que crees. Mira la captura y usa"
                     " clic_xy con las coordenadas del botón (en un modal: sus botones), o un enlace para abrir registros")
            sess.blocked_acts.add(json.dumps({k: v for k, v in act.items() if k != "por_que"}, sort_keys=True))
        sess.log(step)

    mem.fail_flow(key)
    mem.save()
    wrote = sess.saves or sess.exports or sess.deletes
    razon = "se acabó el tiempo/pasos antes de confirmar el resultado"
    if wrote:
        razon += "; hubo un cambio que no alcancé a verificar"
    return "fallida", build_summary("fallida", razon=razon + ".")
