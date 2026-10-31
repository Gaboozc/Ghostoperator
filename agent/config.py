"""Configuración central. Todo se lee de variables de entorno para poder desplegar sin tocar código."""
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Token del equipo: todas las llamadas al entrypoint deben traer "Authorization: Bearer <TEAM_TOKEN>".
TEAM_TOKEN = _env("TEAM_TOKEN")

# Modelo (solo razona: interpretar la tarea, elegir elementos, leer modales, explorar).
# OpenRouter expone una API compatible con OpenAI; cualquier proveedor compatible sirve cambiando LLM_BASE_URL.
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
LLM_BASE_URL = _env("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = _env("LLM_MODEL", "anthropic/claude-sonnet-4.5")  # debe aceptar imágenes
LLM_TIMEOUT_S = float(_env("LLM_TIMEOUT_S", "40"))
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "1200"))
ANTHROPIC_API_KEY = OPENROUTER_API_KEY  # alias usado por el redactor de secretos

# Carpetas persistentes: memoria por app (lo aprendido) y trazas (evidencia por tarea).
DATA_DIR = Path(_env("DATA_DIR", "./data")).resolve()
MEMORY_DIR = DATA_DIR / "memory"
TRACE_DIR = DATA_DIR / "traces"
TASKS_FILE = DATA_DIR / "tasks.json"
for _d in (MEMORY_DIR, TRACE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Presupuesto de tiempo por tarea (el evaluador deja de consultar a los 300 s).
ACTION_DEADLINE_S = float(_env("ACTION_DEADLINE_S", "255"))  # después de esto no se inicia ninguna acción nueva
REPORT_DEADLINE_S = float(_env("REPORT_DEADLINE_S", "285"))  # watchdog: reporte forzado y desconexión
MAX_AGENT_STEPS = int(_env("MAX_AGENT_STEPS", "45"))

# Física del escritorio (README): repintado hasta 1 s, clic < 100 ms se ignora.
CLICK_HOLD_S = float(_env("CLICK_HOLD_S", "0.15"))
KEY_DELAY_S = float(_env("KEY_DELAY_S", "0.03"))
STABLE_POLL_S = float(_env("STABLE_POLL_S", "0.25"))
STABLE_MIN_WAIT_S = float(_env("STABLE_MIN_WAIT_S", "0.35"))
STABLE_TIMEOUT_S = float(_env("STABLE_TIMEOUT_S", "3.0"))

# Exploración inicial de una app nunca vista (solo lectura).
EXPLORE_BUDGET_S = float(_env("EXPLORE_BUDGET_S", "45"))

# OCR
TESSERACT_CMD = _env("TESSERACT_CMD", "tesseract")
OCR_LANG = _env("OCR_LANG", "spa+eng")
OCR_SCALE = float(_env("OCR_SCALE", "3.0"))
MIN_TEXT_CONTRAST = float(_env("MIN_TEXT_CONTRAST", "60"))  # diferencia de luminancia texto/fondo; menos = texto oculto

# Canaria (detector de deriva / incidente). 0 = apagada. Usa CANARY_DESKTOP_URL y CANARY_DESKTOP_PASSWORD.
CANARY_INTERVAL_S = float(_env("CANARY_INTERVAL_S", "0"))
CANARY_DESKTOP_URL = _env("CANARY_DESKTOP_URL")
CANARY_DESKTOP_PASSWORD = _env("CANARY_DESKTOP_PASSWORD")
