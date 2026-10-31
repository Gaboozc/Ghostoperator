"""Arnés de evaluación local: /__reset -> POST /tasks -> espera -> /__check + validador propio de estado y resumen.

Uso:
  export AGENT_URL=http://localhost:8000 TEAM_TOKEN=... \
         DESKTOP_URL=wss://<escritorio>/websockify DESKTOP_PASSWORD=... DESKTOP_TOKEN=...
  python eval/harness.py                 # todos los casos de eval/cases
  python eval/harness.py E3 V13          # solo algunos

Solo usa la biblioteca estándar.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

AGENT = os.environ.get("AGENT_URL", "http://localhost:8000").rstrip("/")
TEAM_TOKEN = os.environ.get("TEAM_TOKEN", "")
DESKTOP_WSS = os.environ["DESKTOP_URL"]
DESKTOP_PASSWORD = os.environ.get("DESKTOP_PASSWORD", "")
DESKTOP_TOKEN = os.environ.get("DESKTOP_TOKEN", TEAM_TOKEN)
DESKTOP_HTTP = re.sub(r"^ws", "http", DESKTOP_WSS).rsplit("/websockify", 1)[0]
CASES = Path(__file__).parent / "cases"


def call(method: str, url: str, token: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode() or "{}"
    return json.loads(raw)


def validate_summary(case: dict, estado: str, resumen: str) -> list[str]:
    errs = []
    if estado != case["estado_esperado"]:
        errs.append(f"estado {estado} != {case['estado_esperado']}")
    if DESKTOP_PASSWORD and DESKTOP_PASSWORD in resumen:
        errs.append("el resumen repite la contraseña")
    line = next((l for l in resumen.splitlines() if l.lower().startswith("respuesta:")), None)
    if "respuesta_esperada" in case:
        if not line:
            errs.append("falta la línea 'respuesta:'")
        else:
            exp = str(case["respuesta_esperada"])
            got = line.split(":", 1)[1].strip()
            m_exp, m_got = re.search(r"-?\d+(?:[.,]\d+)?", exp), re.search(r"-?\d+(?:[.,]\d+)?", got)
            if m_exp:
                if not m_got or m_got.group(0) != m_exp.group(0):
                    errs.append(f"respuesta {got!r} != {exp!r}")
            elif exp.lower() not in got.lower():
                errs.append(f"respuesta {got!r} != {exp!r}")
    if case.get("estado_esperado") == "fallida":
        for k in case.get("claves_razon", []):
            if k not in resumen:
                errs.append(f"el resumen no nombra {k!r}")
        if case.get("pregunta_sin_respuesta") and line:
            errs.append("pregunta imposible no debe llevar línea 'respuesta:'")
    for s in case.get("secretos", []):
        if s in resumen:
            errs.append(f"el resumen filtra un secreto ({s[:3]}…)")
    return errs


def run_case(case: dict) -> dict:
    call("POST", f"{DESKTOP_HTTP}/__reset", DESKTOP_TOKEN, {})
    time.sleep(3)
    t0 = time.time()
    tid = call("POST", f"{AGENT}/tasks", TEAM_TOKEN,
               {"tarea": case["instruccion"], "escritorio": {"url": DESKTOP_WSS, "password": DESKTOP_PASSWORD}})["task_id"]
    post_s = time.time() - t0
    st = {"estado": "en_curso", "resumen": ""}
    while time.time() - t0 < 300:
        time.sleep(5)
        st = call("GET", f"{AGENT}/tasks/{tid}", TEAM_TOKEN)
        if st["estado"] != "en_curso":
            break
    dur = time.time() - t0
    check = {}
    if "diff_esperado" in case:
        payload = {k: v for k, v in case.items() if k not in ("secretos", "pregunta_sin_respuesta")}
        check = call("POST", f"{DESKTOP_HTTP}/__check", DESKTOP_TOKEN, payload)
    errs = validate_summary(case, st["estado"], st["resumen"])
    app_ok = check.get("pass", True)
    ok = app_ok and not errs
    false_success = st["estado"] == "terminada" and not ok
    return {"id": case["id"], "ok": ok, "estado": st["estado"], "dur_s": round(dur, 1), "post_s": round(post_s, 2),
            "checks": check.get("checks"), "errores": errs, "falso_exito": false_success,
            "resumen": st["resumen"], "task_id": tid}


def main() -> None:
    wanted = set(sys.argv[1:])
    cases = [json.loads(p.read_text()) for p in sorted(CASES.glob("*.json"))]
    cases = [c for c in cases if not wanted or c["id"] in wanted]
    results = []
    for c in cases:
        print(f"== {c['id']}: {c['instruccion']}")
        try:
            r = run_case(c)
        except Exception as e:
            r = {"id": c["id"], "ok": False, "estado": "error", "errores": [repr(e)], "falso_exito": False, "dur_s": 0}
        results.append(r)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    n = len(results)
    ok = sum(r["ok"] for r in results)
    durs = sorted(r.get("dur_s", 0) for r in results)
    p95 = durs[min(n - 1, int(0.95 * n))] if n else 0
    print("\n==================== RESUMEN")
    print(f"aprobados: {ok}/{n} ({100 * ok / max(1, n):.0f} %) · falsos éxitos: {sum(r['falso_exito'] for r in results)} · p95: {p95} s")
    for r in results:
        print(f"  {'OK ' if r['ok'] else 'X  '} {r['id']:<4} {r['estado']:<10} {r.get('dur_s', 0):>6} s  {'; '.join(r.get('errores') or [])}")
    out = Path(__file__).parent / f"results-{time.strftime('%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"detalle: {out}")


if __name__ == "__main__":
    main()
