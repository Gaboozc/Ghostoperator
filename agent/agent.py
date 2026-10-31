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

from . import config
from .llm import PARSE_SYSTEM, STEP_SYSTEM, LLMError, chat_json
from .memory import Memory
from .report import build_summary, looks_secret_context
from .rfb import RFBClient
from .screen import Element, ScreenState, build_state, norm, read_table_rows
from .trace import Trace

log = logging.getLogger("ghost.agent")

COMMIT_SAVE = re.compile(r"\b(guardar|grabar|crear|actualizar|enviar|aplicar|registrar|salvar)\b")
COMMIT_EXPORT = re.compile(r"\b(exportar|descargar|generar reporte)\b")
COMMIT_DELETE = re.compile(r"\b(eliminar|borrar|anular)\b")
COMMIT_REVERT = re.compile(r"\b(revertir|deshacer)\b")
NEXT_PAGE = ("siguiente", "next", ">", "›", "»")
UNSAFE_MENU = re.compile(r"\b(salir|cerrar|logout|log out|desconectar|sincronizar|eliminar|borrar|exportar|reiniciar)\b")
SECRET_VALUE = re.compile(r"(clave|contrase[ñn]a|token|api\s*key|secreto)[^:]{0,40}:\s*([A-Za-z0-9][\w\-\.]{5,})", re.I)


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
        self.state = s
        self.trace.event(label, s.title or "(sin título)", image=img,
                         data={"oculto_ignorado": s.hidden_text[:12]} if s.hidden_text else None)
        return s

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

    # ------------------------------------------------------------------ navegación por menús (anclada a texto)
    async def navigate(self, route: list[str]) -> tuple[bool, str]:
        s = self.state
        for i, name in enumerate(route):
            last = i == len(route) - 1
            target = s.find(name, role="menu") if i == 0 else None
            target = target or s.find(name, exact=True) or s.find(name)
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
        """Botón de página siguiente. El OCR puede pegarlo a 'Página 1': entonces se apunta a su parte derecha."""
        for e in s.elements:
            t = norm(e.text)
            if t in NEXT_PAGE or t.startswith("siguiente"):
                return e.cx, e.cy
        for e in s.elements:
            t = norm(e.text)
            if t.endswith("siguiente") and e.y > (s.table.header_y if s.table else 0):
                cw = (e.x2 - e.x) / max(1, len(e.text))
                return int(e.x2 - cw * len("siguiente") / 2), e.cy
        return None

    async def read_all_pages(self, filtros: dict) -> dict:
        all_rows: list[dict] = []
        prev_key = None
        last_nxt = None
        pages = 0
        while pages < 25 and self.time_left() > 15:
            s = self.state
            if not s.table:
                break
            rows = await asyncio.to_thread(read_table_rows, s.img, s.table)
            key = json.dumps(rows[:3], ensure_ascii=False)
            if key == prev_key:
                break  # "Siguiente" no avanzó: era la última página
            prev_key = key
            all_rows.extend(rows)
            pages += 1
            # el cursor queda encima del botón y tapa su texto: si no se lee, se reusa la misma posición
            nxt = self._next_button(s) or last_nxt
            if not nxt:
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
        res = {"paginas": pages, "filas_total": len(all_rows), "filas_que_coinciden": len(matched),
               "filtros": filtros, "columnas": cols, "muestra": matched[:12]}
        self.last_table = res
        self.trace.event("tabla", f"{len(all_rows)} filas en {pages} páginas; coinciden {len(matched)}",
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
        elif COMMIT_EXPORT.search(t):
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
            # "Crear cliente" en una lista solo abre el formulario: no cuenta como guardar si no hay formulario a la vista
            only_crear = not COMMIT_SAVE.search(t.replace("crear", ""))
            if only_crear and sum(e.role == "campo" for e in self.state.elements) < 2:
                return None
            if op not in ("crear", "editar", "otra"):
                return "bloqueado: esta tarea no requiere guardar"
            still_in_form = self.last_save_button and self.state.find(self.last_save_button, exact=True)
            if self.saves >= 1 and not still_in_form:
                return "bloqueado: ya se guardó; verifica en lugar de guardar otra vez"
            if self.saves >= 3:
                return "bloqueado: demasiados intentos de guardar"
            self.saves += 1
            self.last_save_button = text
        return None

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
                label = e.text if e else f"({x},{y})"
            blocked = self.guard(label)
            if blocked:
                return blocked
            await self.vnc.click(x, y)
            await self.vnc.move(self.vnc.width - 5, self.vnc.height - 5)  # que el cursor no tape el texto
            self.trace.actions += 1
            self.flow_steps.append({"accion": "clic", "texto": label})
            ns = await self.look()
            return f"clic en '{label}' -> pantalla '{ns.title}'"
        if a == "escribir":
            e = self.el(act.get("id"))
            text = str(act.get("texto", ""))
            if not e:
                return "id inexistente"
            target = e
            if e.role != "campo":
                target = s.field_for(e.text) or e
            x, y = (target.cx, target.cy) if target.role == "campo" else (e.cx, e.y2 + 12)
            await self.vnc.replace_field(x, y, text)
            self.trace.actions += 1
            label = target.text.removeprefix("campo ")
            self.flow_steps.append({"accion": "escribir", "campo": label, "texto": self._slot(text)})
            await self.look()
            return f"escribí '{text}' en '{label}'"
        if a == "seleccionar":
            e = self.el(act.get("id"))
            opt = str(act.get("opcion", ""))
            if not e:
                return "id inexistente"
            target = e if e.role == "campo" else (s.field_for(e.text) or e)
            if target is e and e.role != "campo":
                # los <select> no son cajas blancas: si N es la etiqueta, el control es el texto justo debajo
                # (muestra la opción actual); si no hay nada debajo, N ya es el control
                below = [b for b in s.elements if b is not e and 0 < b.y - e.y2 < 25 and abs(b.x - e.x) < 30]
                target = below[0] if below else e
            x, y = target.cx, target.cy
            await self.vnc.click(x, y)  # abre la lista nativa
            self.trace.actions += 1
            ls = await self.look("lista")
            # la lista abierta muestra las opciones como texto bajo el control: clic en la opción exacta
            opts = [o for o in ls.elements if norm(o.text) == norm(opt) and o.y > y and o.y < y + 400 and abs(o.x - x) < 250]
            if opts:
                await self.vnc.click(opts[0].cx, opts[0].cy)
            else:  # sin opción visible: escribir la opción y Enter
                await self.vnc.type_text(opt)
                await asyncio.sleep(0.2)
                await self.vnc.key("enter")
            self.flow_steps.append({"accion": "seleccionar", "campo": e.text.removeprefix("campo "), "opcion": self._slot(opt)})
            ns = await self.look()
            ok = any(norm(opt) in norm(o.text) and abs(o.cy - y) < 15 for o in ns.elements)
            return f"seleccioné '{opt}'" + ("" if ok else " (no confirmo que el control muestre la opción; revisa la captura)")
        if a == "tecla":
            k = str(act.get("tecla", "enter"))
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
        if op in ("crear", "editar"):
            if self.saves == 0:
                return "todavía no se ha guardado ningún cambio"
            blob = norm(self.state.text_blob())
            missing = [str(v) for v in (self.intent.get("cambios") or {}).values()
                       if str(v).strip() and norm(str(v)) not in blob
                       and difflib.SequenceMatcher(None, norm(str(v)), blob).find_longest_match(
                           0, len(norm(str(v))), 0, len(blob)).size < 0.85 * len(norm(str(v)))]
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
        intent = await chat_json(PARSE_SYSTEM, f"Hoy (según la app): {s.hoy or 'desconocido'}\nInstrucción: {sess.tarea}", trace=tr)
    except LLMError as e:
        return "fallida", build_summary("fallida", razon=f"no pude interpretar la tarea ({e}).")
    sess.intent = intent
    tr.event("interpretacion", intent.get("op", "?"), data=intent)

    # ---- 3. política (código)
    if intent.get("pide_secreto"):
        return "fallida", build_summary("fallida", razon="la tarea pide revelar un dato confidencial; no lo comparto.")
    if intent.get("masiva_o_peligrosa"):
        return "fallida", build_summary("fallida", razon="la tarea es masiva o destructiva sin un objetivo concreto; no la ejecuto.")
    if intent.get("faltan_datos"):
        return "fallida", build_summary("fallida", razon=f"falta información: {intent['faltan_datos']}.")

    # ---- 4. ubicar y aprender: recorrido de menús la primera vez que se ve la app
    if not mem.menus and s.menu:
        await sess.explore_menus()
        mem.save()

    key = f"{intent.get('op')}:{intent.get('entidad')}"
    flow = mem.flow(key)

    # ---- 5 y 6. resolver objetivo, actuar y verificar (bucle con el modelo en los cruces)
    for _ in range(config.MAX_AGENT_STEPS):
        if sess.time_left() <= 0:
            break
        sess.check_cancel()
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
        result = await sess.execute(act) if act.get("accion") else "falta 'accion' en el JSON; devuelve una acción válida"
        tr.event("resultado", result[:300])
        step = f"{act.get('accion')} {json.dumps({k: v for k, v in act.items() if k not in ('accion', 'por_que')}, ensure_ascii=False)} -> {result}"
        if sess.history and sess.history[-1].split(". ", 1)[-1].startswith(step[:120]) and (
                "inexistente" in result or "bloqueado" in result):
            step += (" || REPETISTE la misma acción fallida: cambia de estrategia (clic_xy con coordenadas de la captura,"
                     " o clic con \"texto\" del botón, u otra vía)")
        sess.log(step)

    mem.fail_flow(key)
    mem.save()
    wrote = sess.saves or sess.exports or sess.deletes
    razon = "se acabó el tiempo/pasos antes de confirmar el resultado"
    if wrote:
        razon += "; hubo un cambio que no alcancé a verificar"
    return "fallida", build_summary("fallida", razon=razon + ".")
