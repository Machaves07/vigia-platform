# Manual de soporte U-05 · El código de correlación que copia la persona

**Unidad**: U-05 · **Tarea**: TASK-458 (VIG-331) · **Requisitos**: NFR-APP-29, NFR-APP-40 y NFR-APP-41; BR-APP-75; NFR-NUC-17, NFR-NUC-28, NFR-NUC-41 a 44; SECURITY-14; RESILIENCY-05.
**Diseño**: `plataforma-aplicacion/nfr-requirements/nfr-requirements.md` §4 y §7, y `tech-stack-decisions.md` §7 (decisión 12), en `aidlc-docs/` del espacio de trabajo de Vigía, fuera de este repositorio.
**Ejecuta**: el dueño o quien dé soporte, con la identidad administrativa con segundo factor (lectura de CloudWatch).
**Índice**: [runbooks de operación](README.md).

Cuando la aplicación muestra un error de la plataforma, muestra también su **código de correlación** con la indicación «Indique este código al reportar» y un botón para copiarlo (UI-19). Con ese código, soporte encuentra en los registros del servidor exactamente la petición que falló, sin pedir capturas, nombres ni datos de la persona.

## Qué ve la persona

| Dónde | Qué muestra |
|---|---|
| Aviso sobre la pantalla (pila de avisos) | El mensaje del error, «Indique este código al reportar: `<código>`» y el botón «Copiar código» |
| Pantalla de error global | «Algo falló», «Indique este código al reportar: `<código>`» y el botón «Copiar» |
| Error interno | «Algo falló. Indique el código de correlación al reportarlo.» |

El código es un **UUID v7** (36 caracteres, p. ej. `0192f3a4-5b6c-7d8e-9f01-23456789abcd`). Lo genera `vigia-api` en cada petición, al principio de la cadena de middleware; nunca lo acepta del navegador (BR-NUC-96). Llega a la interfaz solo en el cuerpo JSON del error (`correlation_id`), no en una cabecera, y la interfaz nunca lo pone en la URL ni en el historial.

**Un error sin código** no salió de `vigia-api`:

- un `403` del cortafuegos o un error del balanceador: [runbook 6.3](u05-6.3-cortafuegos-bloquea-una-planta.md) o [6.1](u05-6.1-la-aplicacion-no-carga.md);
- la red de la planta cortada o el navegador sin conexión;
- un fallo de la propia interfaz en el navegador (la pantalla de error global sin código);
- «Hay una versión nueva; recargue.», que es local: [runbook 6.2](u05-6.2-version-nueva-no-aparece.md).

## Qué pedir a la persona

Solo esto, por el canal de soporte de la planta:

1. el **código copiado** (botón «Copiar código»), pegado tal cual;
2. la **hora aproximada** y la **pantalla** en la que estaba («bandeja», «cierre del hallazgo»…);
3. qué intentaba hacer, en una frase.

Nunca se piden contraseñas, códigos del segundo factor, capturas con datos de personas ni el contenido de los textos libres. Un código de correlación no identifica a nadie y se puede compartir.

## Pasos

1. **Acotar la hora.** Un UUID v7 lleva en sus primeros 48 bits el instante de la petición en milisegundos UTC, así que no hace falta adivinar la ventana:

   ```text
   python3 -c 'import sys,datetime; u=sys.argv[1].replace("-",""); print(datetime.datetime.fromtimestamp(int(u[:12],16)/1000, datetime.timezone.utc).isoformat())' <código>
   ```

   Si el tercer bloque del código no empieza por `7`, no es un código de la plataforma: pide a la persona que lo copie con el botón.
2. **Buscar la petición** en CloudWatch Logs Insights, en el grupo de registros de la API, `/vigia/pilot/api` (en una instancia dedicada, `/vigia/<despliegue>/api`; retención de 180 días), con una ventana de unos minutos alrededor de esa hora:

   ```text
   fields @timestamp, level, component, message, route, status, duration_ms, organization_id, actor_id
   | filter correlation_id = "<código>"
   | sort @timestamp asc
   ```

   - Toda petición atendida deja la línea «petición atendida» con `route` (la plantilla de la ruta, no la URL), `method`, `status` y `duration_ms`. Las de `/assets/*` no dejan línea: están solo en el registro de acceso del balanceador.
   - Las líneas anteriores de la misma petición (`level` `WARNING` o `ERROR`, y «excepción no controlada en una petición» en un `500`) dicen qué falló y en qué `component`.
   - Los campos son solo identificadores y enumeraciones: el registro redacta todo lo demás (NFR-NUC-17), así que lo que ves se puede citar en el registro del incidente sin datos de la persona.
3. **Si la petición encoló trabajo** (un cierre, una exportación, un aviso), el evento de la bandeja de salida guarda el mismo `correlation_id`, y su entrega en `vigia-worker` abre el tramo `outbox.deliver` con ese `correlation_id` como atributo y un enlace al tramo que lo publicó (NFR-NUC-43). Mira también `/vigia/pilot/worker` en la misma ventana, filtrando por la organización (`organization_id`) de la línea del paso 2.
4. **Clasificar** con el `status` y el `code` del error:

   | `status` | Lectura habitual | Siguiente paso |
   |---|---|---|
   | `401`, `403` | Sesión vencida, segundo factor o permiso que la persona no tiene | Explicar a la persona; si se repite con muchas personas, mirar las alertas de seguridad (abajo) |
   | `404` | Recurso fuera de su alcance (`not_found`, nunca `forbidden`) o una porción de otra versión | Confirmar su alcance; si es justo después de un release, [6.2](u05-6.2-version-nueva-no-aparece.md) |
   | `409`, `422` | Conflicto de estado o datos no válidos | Explicar el mensaje; no es un fallo de la plataforma |
   | `429` | Límite de tasa de la aplicación | La persona espera el tiempo indicado |
   | `5xx` | Fallo del servidor o de una dependencia | Revisar las alarmas en `ALARM` del tablero `vigia-pilot` y seguir el runbook que nombre la alarma |

5. **Registrar** en el flujo de incidentes de U-02, si es un fallo del servidor, con el código, la ruta, el `status` y lo que se hizo. Nunca se copian líneas del registro con identificadores de la planta a un repositorio, un ticket público ni un prompt.

## Por qué las alertas viven en el servidor (NFR-APP-29)

La interfaz **no genera alertas**. Los fallos de autenticación, las denegaciones de autorización y las peticiones falsificadas los ve y los trata el servidor (NFR-NUC-28):

- la alarma `security-alert` (`vigia-security-alert` en `pilot`) salta con cualquier `security_alert_total` en 5 min. Sus tipos (`alert_type`) son `security_alert` (fallos de inicio de sesión por cuenta o por origen), `authorization_denied` (más de 20 denegaciones por actor en 10 min `[objetivo propio]`), `context_absent_attempt`, `unknown_token_reported` e `integrity_compromised`. Va al correo del dueño;
- el panel «Seguridad (NFR-NUC-28)» del tablero `vigia-pilot` grafica `auth_failures_total` y las alertas por tipo;
- `csrf_rejected` (petición falsificada) queda en la **auditoría** de la organización, sin métrica ni alarma propias.

El navegador de una persona no es un buen sensor: puede estar sin red, cerrado o manipulado, y lo que reporta no se puede verificar. El servidor ve todas las peticiones de todas las plantas con la misma vara, y las alarmas se disparan ahí. La interfaz solo aporta el **código de correlación**, para que el reporte de una persona se pueda unir a lo que el servidor ya registró (NFR-APP-41).

## Por qué la interfaz no instrumenta (NFR-APP-40)

Latencia, errores, caudal, sesiones y uso por zona se observan en el servidor (U-02 y U-04: métricas por ruta, trazas de OpenTelemetry y los paneles del tablero). La interfaz **no envía telemetría ni errores**: no hay ruta `POST /client-errors`, ni `sendBeacon`, ni bibliotecas de analítica (decisión 12 de `tech-stack-decisions.md`). Así:

- ningún dato de la pantalla de una persona sale de su navegador hacia un tercero ni hacia la plataforma fuera de las operaciones que ella hace (P3);
- no hay una vía de entrada nueva que proteger, limitar y auditar;
- el presupuesto de dependencias de la interfaz no crece con un cliente de telemetría.

Se reevalúa **después del piloto**, con los datos del servidor y lo aprendido en soporte.

## Estado en el código

- **Generación y propagación**: `CorrelationStep` en `backend/src/vigia_platform/shared/api/middleware/steps.py` (primer paso de la cadena; `uuid7` con el reloj inyectado) y `log_context(correlation_id=…)`; cuerpo de error `ApiErrorBody` en `shared/api/errors.py` (`code`, `detail_code`, `message_es`, `correlation_id`, `retry_after_seconds`).
- **Registros**: `correlation_id` es el primero de los campos de contexto de `shared/observability/logging.py` (`correlation_id`, `organization_id`, `actor_id`, `route`, `status`, `duration_ms`), tras `timestamp`, `level`, `component` y `message`. Grupos `/vigia/<despliegue>/<proceso>` de `infra/stacks/compute.py` y `infra/stacks/observability.py` (`api`, `worker`, `migrate`, `admin`, `otel`).
- **Trazas**: los tramos HTTP de `vigia-api` **no** llevan hoy `correlation_id` como atributo (NFR-NUC-43 lo pide); sí `outbox.deliver` en `vigia-worker` y el tramo de identidad del nodo. Para una petición de una persona, la búsqueda fiable es la del registro (paso 2).
- **Interfaz**: `frontend/src/components/NoticeStack/NoticeStack.tsx` y `frontend/src/app/GlobalErrorHandler.tsx` muestran y copian el código; textos en `frontend/src/texts/components_forms.json` y `app_shell.json`. Pruebas: `frontend/tests/components/notice_stack.test.tsx` (BR-APP-75, NFR-APP-41) y `frontend/tests/components/global_error_handler.test.tsx`.
- **Sin telemetría**: `frontend/tests/abuse/a10_exfiltration.test.tsx` cierra la lista de destinos de red de la interfaz (prohíbe `sendBeacon`, `WebSocket`, `EventSource`, imágenes y URL externas); `no-console` en ESLint y `frontend/tests/unit/build/no_console.test.ts` sobre `dist/`.
