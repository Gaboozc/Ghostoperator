"""Resumen final: plantilla de código + redacción de secretos.

Reglas del README que se aplican aquí:
- En preguntas: una línea "respuesta: <valor>"; si son dos datos, "respuesta: a; b" en el orden pedido.
- Si el valor es numérico, el número va primero en la línea.
- Si la pregunta no se puede responder, NO hay línea "respuesta:".
- El resumen nunca repite la contraseña ni otros secretos (incluidos los que la app marca como secretos).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

_SECRET_HINTS = re.compile(r"(clave|contraseña|password|token|secret|api[\s_-]?key|no comparta)", re.I)


def redact(text: str, secrets: Iterable[str]) -> str:
    out = text or ""
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(s, "[oculto]")
        # también variantes sin el esquema de la URL (wss://host/...)
        if "://" in s:
            out = out.replace(s.split("://", 1)[1], "[oculto]")
    return out


def looks_secret_context(line: str) -> bool:
    return bool(_SECRET_HINTS.search(line or ""))


def normalize_answer(values: list[str]) -> str:
    vals = [str(v).strip() for v in values if str(v).strip() != ""]
    return "; ".join(vals)


def build_summary(estado: str, *, respuesta: Optional[list[str]] = None, hecho: str = "", razon: str = "",
                  detalles: Optional[list[str]] = None) -> str:
    lines: list[str] = []
    if respuesta:
        lines.append(f"respuesta: {normalize_answer(respuesta)}")
    if estado == "terminada":
        if hecho:
            lines.append(hecho)
    else:
        lines.append(f"No se completó la tarea. Motivo: {razon or 'no se pudo confirmar el resultado.'}")
    for d in detalles or []:
        lines.append(f"- {d}")
    return "\n".join(lines).strip()
