# GhostOperator

Agente que opera por pantalla sistemas de gestión **sin API**, como lo haría un empleado nuevo, y ejecuta tareas en lenguaje natural en apps que nunca ha visto.

**Idea central:** el código percibe, navega, cuenta y verifica; el modelo decide solo en los cruces (interpretar la tarea, elegir un elemento, leer un modal, explorar). Lo aprendido de cada app queda en un mapa reutilizable (`data/memory/<app>.json`) que la siguiente tarea usa y mejora.

> La app legacy no tiene API ni red: el agente la opera **solo por VNC**. El servicio HTTP de este repo es el *entrypoint del agente* que exige el contrato del reto.

## Cómo correrlo (local)

Requisitos: Python 3.11+ y Tesseract con español (`apt install tesseract-ocr tesseract-ocr-spa` o `brew install tesseract tesseract-lang`).

```bash
pip install -r requirements.txt
export TEAM_TOKEN=<token del equipo>
export OPENROUTER_API_KEY=<key de OpenRouter>
export LLM_MODEL=anthropic/claude-sonnet-4.5   # cualquier modelo con visión de OpenRouter
uvicorn agent.api:app --port 8000 --workers 1
```

Probar el contrato:

```bash
curl localhost:8000/health
curl -X POST localhost:8000/tasks -H "Authorization: Bearer $TEAM_TOKEN" -H "Content-Type: application/json" \
  -d '{"tarea":"¿Cuántas facturas vencidas tiene Ferretería Ruiz?","escritorio":{"url":"wss://<escritorio>/websockify","password":"<contraseña>"}}'
curl localhost:8000/tasks/<task_id> -H "Authorization: Bearer $TEAM_TOKEN"
```

Ver qué hizo el agente paso a paso (capturas, decisiones, verificación): `http://localhost:8000/traces/<task_id>?token=<TEAM_TOKEN>`.
Ver lo aprendido de cada app: `GET /memory`.

Con Docker:

```bash
docker build -t ghostoperator .
docker run -p 8000:8000 -e TEAM_TOKEN -e OPENROUTER_API_KEY -v $PWD/data:/data ghostoperator
```

## Arnés de evaluación

Corre los casos de `eval/cases/` (E1–E5 del reto + variantes V*) contra el escritorio: `/__reset` → `POST /tasks` → espera → `/__check`, y valida estado y resumen con las reglas del README del reto (línea `respuesta:`, claves de la razón, sin secretos).

```bash
export AGENT_URL=http://localhost:8000 TEAM_TOKEN=... \
       DESKTOP_URL=wss://<escritorio>/websockify DESKTOP_PASSWORD=... DESKTOP_TOKEN=...
python eval/harness.py           # todos
python eval/harness.py E3 E5     # algunos
```

Imprime aprobados, **falsos éxitos** (meta 0) y p95 de tiempo, y guarda el detalle en `eval/results-*.json`.

## Despliegue (Railway)

1. New Project → Deploy from GitHub repo (usa el `Dockerfile`).
2. Variables: `TEAM_TOKEN`, `OPENROUTER_API_KEY`, `LLM_MODEL`.
3. **Agregar un Volume montado en `/data`** para que la memoria aprendida sobreviva a redeploys.
4. Opcional, canaria del incidente: `CANARY_INTERVAL_S=300`, `CANARY_DESKTOP_URL`, `CANARY_DESKTOP_PASSWORD` (usar un escritorio propio, nunca el de evaluación).
5. Una sola réplica (las tareas viven en memoria del proceso).

## Arquitectura

| Módulo | Qué hace |
|---|---|
| `agent/api.py` | Contrato: `/health`, `POST /tasks` (< 200 ms), `GET /tasks/{id}`; 1 tarea por escritorio (la nueva cancela la anterior); watchdog a 285 s; redacción de secretos |
| `agent/rfb.py` | Cliente VNC/RFB propio sobre websocket: auth DES, framebuffer Raw/CopyRect, clic con 150 ms de pulsación, teclas Unicode, `Ctrl+A` antes de escribir, captura estable (2 frames iguales) |
| `agent/ocr.py` | OCR local con Tesseract (2 pasadas en paralelo: texto oscuro y texto claro sobre color) y filtro de contraste contra texto oculto |
| `agent/screen.py` | Pantalla estructurada: elementos numerados, campos junto a su etiqueta, menú, título, huella y versión de la app, fecha "Hoy", tabla principal; conteo de filas por píxeles + lectura fila por fila |
| `agent/memory.py` | Memoria por huella de app: rutas de menú, pantallas, flujos probados con parámetros, reglas descubiertas, incidentes |
| `agent/agent.py` | Orquestador por fases; recorrido de menús de solo lectura en la primera tarea; guardas de acciones que cambian datos; verificación antes de `terminada` |
| `agent/llm.py` | Llamadas al modelo vía OpenRouter y prompts (interpretar tarea; elegir siguiente acción) |
| `agent/report.py` | Resumen por plantilla: `respuesta: <valor>` (número primero, `;` para dos datos), sin secretos |
| `agent/canary.py` | Detector de deriva: compara versión y menú con la memoria; escribe `data/incident_log.json` |
| `agent/trace.py` | Traza por tarea con capturas, y visor HTML |

### Flujo de una tarea

1. **Recibir**: responde `task_id` de inmediato y corre en segundo plano.
2. **Interpretar** (modelo): instrucción → JSON (`op`, entidad, objetivo, cambios, filtros, pregunta).
3. **Política** (código): si pide un secreto, es masiva/destructiva o faltan datos → `fallida` sin tocar la app.
4. **Ubicar y aprender**: huella de la app (título + versión). Si es nueva, recorre todos los menús sin pulsar nada que cambie datos y guarda las rutas.
5. **Resolver objetivo**: busca con los filtros de la app y lee la tabla en código; 0 resultados → `fallida`; varios sin desempate → `fallida` nombrando el documento/NIT de cada uno; al crear, si ya existe → `fallida`.
6. **Actuar y verificar**: el modelo elige elementos numerados; el código ejecuta. Guardas: exportar/eliminar/revertir solo si la tarea lo pide y una sola vez; no se vuelve a guardar sin verificar. `terminada` solo si los valores nuevos se ven en pantalla; si no, `fallida` explicando.
7. **Consolidar y reportar**: guarda el flujo probado y las reglas; resumen por plantilla sin secretos; cierra la conexión VNC (nunca opera después de reportar).

### Incidente (la app cambia durante el día)

- Los elementos se buscan **por su texto**, nunca por coordenadas guardadas.
- Al empezar cada tarea se compara la versión de la app con la memoria; si cambió, se registra el incidente y se re-explora el mapa.
- Si una ruta conocida deja de funcionar, se marca, se registra el incidente y el modelo explora; al resolver la tarea se anota qué se re-aprendió.
- La canaria revisa cada 5 min y deja `data/incident_log.json` con causa y hora.

## Variables de entorno

| Variable | Default | Uso |
|---|---|---|
| `TEAM_TOKEN` | — | token Bearer del equipo |
| `OPENROUTER_API_KEY` | — | key del modelo |
| `LLM_MODEL` | `anthropic/claude-sonnet-4.5` | modelo con visión |
| `DATA_DIR` | `./data` | memoria, trazas, tareas |
| `ACTION_DEADLINE_S` / `REPORT_DEADLINE_S` | 255 / 285 | presupuesto de tiempo |
| `CLICK_HOLD_S` | 0.15 | pulsación del clic (el escritorio ignora < 100 ms) |
| `STABLE_TIMEOUT_S` | 3.0 | tope de espera a que la pantalla deje de cambiar |
| `OCR_SCALE` / `MIN_TEXT_CONTRAST` | 3.0 / 60 | calidad de OCR / umbral de texto oculto |
| `CANARY_*` | apagada | detector de deriva |

## Límites conocidos

- OCR: fuentes muy pequeñas o íconos sin texto se leen peor; el modelo ve también la captura y puede usar `clic_xy`.
- Listas desplegables nativas se operan escribiendo la opción y Enter.
- Una tarea por escritorio a la vez.
