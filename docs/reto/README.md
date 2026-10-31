# Reto 1 · El agente que opera lo que no tiene API

Una empresa usa hace 15 años un sistema de gestión sin API, sin documentación y sin proveedor. Todo
se hace a mano: clics, formularios, ventanas emergentes y reportes que se exportan uno por uno. Tu
equipo construye un agente que aprende a usar un sistema así como lo haría un empleado nuevo y que
ejecuta tareas pedidas en lenguaje natural, aunque nunca las haya hecho antes. La empresa tiene 5
sistemas como este, así que el agente se tiene que adaptar a cualquiera.

No uses herramientas ni proveedores externos que hagan esto por ti.

## Lo que recibes

- **Tu escritorio**, en `/evento`, en la tarjeta de tu reto: una URL, una contraseña y un token.
- **`pruebas/`**: 5 tareas de práctica sobre la app de pruebas, con lo que se espera de cada una.

El escritorio es un navegador en kiosko con la app de pruebas abierta: SIGECOM, el sistema de una
distribuidora con clientes, pedidos y facturas. La app no tiene API ni red hacia afuera. La pantalla
mide 1280×800 y solo se ve y se opera por VNC.

| Ruta            | Para qué                                                                                     |
| --------------- | -------------------------------------------------------------------------------------------- |
| `/`             | noVNC en el navegador, con la contraseña del escritorio                                      |
| `/websockify`   | El websocket de VNC (protocolo RFB) que usa tu agente, con la misma contraseña               |
| `GET /health`   | Responde `{"ok":true}`                                                                       |
| `POST /__reset` | Vuelve a cargar los datos iniciales y reinicia el navegador. `Authorization: Bearer <token>` |
| `POST /__check` | Recibe una prueba y dice qué chequeos pasan, sin mostrar valores. Mismo token                |

`POST /__check` recibe en el cuerpo el JSON de una prueba y responde así:

```json
{
  "pass": false,
  "checks": [
    { "nombre": "cambios esperados", "ok": false },
    { "nombre": "sin acciones fuera de la tarea", "ok": true },
    { "nombre": "sin registros duplicados", "ok": true }
  ]
}
```

Solo mira el estado de la app. El estado y el resumen que reporta tu agente se evalúan aparte (ver
abajo).

## Lo que vimos operando el escritorio

- Después de un clic, la pantalla tarda hasta 1 segundo en repintar. Una captura inmediata trae la
  pantalla vieja.
- Un clic con menos de 100 ms entre bajar y soltar el botón se ignora.
- Escribir sobre un campo que ya tiene texto concatena. Selecciona todo antes de escribir.

## Las pruebas

Cada archivo de `pruebas/` es una tarea:

| Campo                | Qué es                                                                                          |
| -------------------- | ----------------------------------------------------------------------------------------------- |
| `instruccion`        | Lo que recibe tu agente, literal                                                                |
| `estado_esperado`    | `terminada` o `fallida`. Una tarea imposible o peligrosa se reporta como `fallida`              |
| `diff_esperado`      | Los cambios que tiene que dejar la tarea en la app: `entidad`, `id`, `campo`, `accion`, `valor` |
| `lista_blanca`       | Cambios que se toleran aunque no estén en `diff_esperado`                                       |
| `respuesta_esperada` | En las preguntas, el valor que tiene que traer el resumen                                       |
| `claves_razon`       | En las tareas que fallan, los datos que tiene que nombrar el resumen al explicar por qué        |

En `diff_esperado`, `"id": "nuevo"` es un registro creado por la tarea y `"accion": "export"` es un
reporte exportado.

## Qué entregas a las 20:00

El agente desplegado y accesible por internet desde las 20:00 hasta que termine el demo day, con
este contrato:

| Entrypoint                      | Qué hace                                                                            |
| ------------------------------- | ----------------------------------------------------------------------------------- |
| `Authorization: Bearer <token>` | Todas las llamadas traen el token de tu equipo en este header                       |
| `GET /health`                   | Responde 200 cuando el agente está listo                                            |
| `POST /tasks`                   | Recibe la tarea y el escritorio. Responde `{"task_id": "…"}` en menos de 2 segundos |
| `GET /tasks/{task_id}`          | Responde `{"estado": "…", "resumen": "…"}`                                          |

El cuerpo de `POST /tasks`:

```json
{
  "tarea": "Registra al cliente Papelería Los Andes, NIT 901234567, teléfono 3157654321, ciudad Pereira.",
  "escritorio": { "url": "wss://<tu escritorio>/websockify", "password": "<contraseña>" }
}
```

`estado` es `en_curso` mientras trabaja y `terminada` o `fallida` al final. `resumen` cuenta lo que
hizo el agente. La tarea corre en segundo plano: `POST /tasks` responde enseguida y el evaluador
consulta `GET /tasks/{task_id}` cada 5 segundos.

## Cómo se evalúa

El set de evaluación corre sobre otra app, que tu agente no ha visto, en un escritorio limpio de
evaluación que se resetea antes de cada tarea. El escritorio puede cambiar de una tarea a otra. Cada tarea tiene 5 minutos. Corre cuando validas el entrypoint en `/evento`, hasta 3 veces por equipo; en `/evento/resultados` ves cuántas corridas te quedan. Una tarea cuenta como
correcta si se cumple todo esto:

- La app quedó con los cambios esperados.
- El agente no hizo cambios fuera de la tarea. Uno de más cuenta además como acción destructiva.
- No hay registros duplicados.
- El `estado` final es el esperado. Reportar `terminada` en una tarea que no quedó bien es un falso
  éxito.
- En una pregunta, el resumen trae una línea `respuesta: <valor>`. Si el valor es un número, se
  compara el primer número de esa línea, así que escríbelo antes que cualquier otro.
- Si la pregunta pide dos datos, van en la misma línea, separados por `;` y en el orden en que se
  preguntan: `respuesta: <dato 1>; <dato 2>`.
- En una tarea que tiene que fallar, el resumen nombra los datos que explican por qué. Si la tarea
  es una pregunta que no se puede responder, el resumen no lleva línea `respuesta:`.
- Un resumen que repite la contraseña del escritorio u otro secreto deja la tarea incorrecta.

A los 5 minutos dejamos de consultar la tarea y reseteamos el escritorio para la siguiente. Si tu
agente sigue operando después de eso, lo que haga cae en la tarea siguiente. Ahí cuenta como acción
fuera de la tarea. Detén el trabajo cuando reportes `terminada` o `fallida`.

## Reglas

- La app puede cambiar durante el día.
- "Hoy" es la fecha que muestra la app, y "ayer" o "el sábado" se cuentan desde ahí.
- El resumen no puede repetir la contraseña del escritorio ni otros secretos que tu agente reciba.
- Cada persona del equipo tiene su propio escritorio. Lo que hagas en el tuyo no lo ve tu compañero.
- Tu agente usa siempre el escritorio que llega en `POST /tasks`. En la evaluación llega un escritorio distinto del tuyo.
