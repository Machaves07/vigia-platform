# VIG-408 / TASK-548 · Evidencia local

## Revisión R4/R5 sobre `3f2aca9` · 2026-11-09

Corrección en **`b2259c2`**, con commits locales nuevos, sin reescribir ni empujar.
Esta sección actualiza la evidencia original conservada debajo. Los criterios 1 y 3
aceptados no se amplían; el **criterio 2 sigue pendiente del dueño**. No se tocó el
almacén, la composición, dependencias, archivos prohibidos ni la semilla de VIG-394.

- **R4:** un retorno `None` o `halted_reason` presente termina con salida 1,
  `heartbeat_rejected`, sin incrementar el contador. Las variantes `[none]` y `[blocked]`
  de `test_missing_or_blocked_heartbeat_is_not_success` fuerzan esos estados incluso con
  un evento `success` recién emitido por el intercambio real: así no se confunde éxito
  de transporte con aceptación. Se conserva además la guarda del evento rechazado (M03).
- **R5:** `follow_redirects=False` **ya estaba explícito en `3f2aca9`** para el cliente
  de alta; se conserva y se documenta junto al POST. Se contrastó también que los clientes
  de ingesta/mTLS, subida y rotación de `SyncClient` fijan `False` en la dependencia
  instalada, sin modificarla. La prueba nueva usa dos servidores TLS reales con la misma
  CA confiable y puertos distintos (orígenes distintos), sin sustituir el transporte:
  el primero recibe el código y devuelve 307/308; el humo sale no cero, el alta queda
  `not_run` y el segundo no recibe ninguna petición. Cambiar a `True` hace fallar ambos
  casos por reenvío, aunque el segundo servidor rechace el alta y el humo también falle.
- **Runbook:** errata corregida, SCR-07 citado para la alarma
  `simulated_adapter_in_productive` y exposición de `--code` en `/proc/<pid>/cmdline`
  documentada. El humo exige argv y no tiene entrada oculta alternativa; se distingue
  del instalador de vigia-nodo, que sí pregunta sin eco. No se añadió otra interfaz.

### Comprobaciones de esta revisión

Desde `backend/`, salvo git/gitleaks y el generador. Pruebas con
`DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales` y
`VIGIA_NODO_SRC=/home/manu/vigia/nodo-ref394`, sin adaptador `port_retry`.

| Orden | Salida real |
|---|---|
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `1375 files already formatted` |
| `uv run python tools/lint_rules.py` | `lint_rules: sin violaciones en src, tests, tools, migrations` |
| `uv run mypy --follow-imports=silent tools/node_enrollment_smoke.py tests/integration/test_node_enrollment_smoke.py` | `Success: no issues found in 2 source files` |
| `uv run pytest tests/integration/test_node_enrollment_smoke.py tests/integration/test_seed_pope.py -q -m integration --basetemp=/tmp/t408b` | Tras restaurar las mutaciones: **49 passed, 1 skipped, 1 warning in 102.62s**. Antes de mutar: mismo resultado en 99.18s. |
| `uv run python tools/check_links.py --root .. ../docs/runbooks/u06-alta-nodo-integracion.md` | `check_links: 1 archivos, 23 enlaces relativos, 0 rotos` |
| Mismo comprobador incluyendo este EVIDENCE.md | `check_links: 2 archivos, 30 enlaces relativos, 0 rotos` |
| `git diff --check` | Sin salida, código 0. |
| `git diff -- <cuatro archivos de la corrección> \| gitleaks stdin --redact` | `no leaks found` antes del commit. |
| `gitleaks git --log-opts=3f2aca9..HEAD --redact` | Sobre `b2259c2`, antes del commit de esta adenda: `1 commits scanned`, `no leaks found`. Se repite al cierre para incluir la evidencia. |

El único omitido es `test_compose_runbook_with_simulated_node`, optativo: **no se
relanzó Compose ni se intervino en el bloqueo del almacén**. La advertencia es la previa
de WeasyPrint/HarfBuzz-Subset. Los servidores de redirección ejecutan `shutdown`,
recogen su hilo y cierran sus sockets; los fixtures liberaron sus contenedores.
`docker ps` al terminar solo mostró los dos `vigia-deploy-*` y `openclaw` ajenos.
La comprobación de procesos no mostró pytest, API ni mutador de esta sesión persistentes.

### Mutaciones nuevas (ejecutadas, no supuestas)

El generador oficial ejecutó **solo G17–G19** del manifiesto, con línea base verde,
restauración byte a byte verificada y `TABLA_GUARDAS_OK 3`. Las 16 anteriores conservan
su evidencia histórica, no se presentan como repetidas en esta revisión.

| Guarda | Mutación | Prueba en rojo | Salida real |
|---|---|---|---|
| G17 / R4 | Quitar `outcome is None` | `test_missing_or_blocked_heartbeat_is_not_success[none]` | `1 failed, 1 warning in 32.39s` |
| G18 / R4 | Quitar `client.halted_reason is not None` | `test_missing_or_blocked_heartbeat_is_not_success[blocked]` | `1 failed, 1 warning in 29.61s` |
| G19 / R5 | `follow_redirects=False` → `True` | `test_enrollment_redirect_never_sends_code_to_another_origin[307/308]` | `2 failed, 1 warning in 6.10s` |

Salida generada añadida a [MUTATIONS.txt](MUTATIONS.txt); filas permanentes en
[guardas.toml](guardas.toml). Para acotar la ronda se extrajeron sus tres filas nuevas
y las exenciones a `/tmp/opencode/vig408-r1-guardas.toml`, cambiando solo la base a
`3f2aca9` y el comando a
`uv run --project backend pytest -q --tb=short -m integration --basetemp=/tmp/t408b-mut`.
Se ejecutó desde la raíz:

```bash
DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales \
VIGIA_NODO_SRC=/home/manu/vigia/nodo-ref394 \
uv run --project backend python \
  '/mnt/c/Users/manu_/Documents/MChaves/8.0_Proyecto_Vigia_OpenCode/Vigia/.agents/skills/vigia-calidad-antes-del-pr/tabla_guardas.py' \
  /tmp/opencode/vig408-r1-guardas.toml
```

```text
== cobertura frente a 3f2aca9: 2 condiciones nuevas, 2 cubiertas
TABLA_GUARDAS_OK 3
```

Se validaron además los 19 selectores del manifiesto permanente (una coincidencia por
fila) y su cobertura completa mediante la función `cobertura` del generador:
`== cobertura frente a e9660a4: 21 condiciones nuevas, 21 cubiertas`.
Esto no equivale a repetir las 16 mutaciones históricas.

## Evidencia original (conservada)

Base `e9660a4`, rama `vig-408-guia-alta-nodo`, 2026-11-09. Implementación en el commit
local **`31d1bf4`**. Solo datos sintéticos; sin Linear, GitHub, push, PR, server-casa ni
nodo de planta. Sin cambios en dependencias, migraciones, `.github/`, archivos calientes
o reglas del repositorio. Los contenedores ajenos `vigia-deploy-*` y `openclaw` no se tocaron.

## Resultado y alcance

- [Runbook](../../runbooks/u06-alta-nodo-integracion.md): 13 pasos, pantallas reales,
  comandos contrastados con vigia-nodo `815e8f1`, confianza separada de edge/nodo,
  `time_sources`, A-88/A-89, re-alta, revocación y tabla de síntomas.
- `backend/tools/node_enrollment_smoke.py`: NodeCredentials + SyncClient contra API real,
  alta, tres latidos aceptados, evento de cámara, concesión/PUT/confirmación de clip de
  verificación y GET opcional de la dirección anunciada. Estado efímero eliminado al salir,
  tiempos explícitos de 60 s, sin reintentos del 503 ni `run_forever`.
- `backend/tests/integration/test_node_enrollment_smoke.py`: API real en proceso,
  PostgreSQL 16, LocalStack tras TLS y `tools/mtls_edge.py` para app. y nodes. con mTLS;
  sondas negativas aisladas y ensayo optativo de la composición completa de VIG-393.
- `backend/tests/integration/test_seed_pope.py`: una aserción nueva exige exactamente
  `{event, message_es, writes, node_id, zone_ids}` en el evento `seeded`.
- [guardas.toml](guardas.toml) y [tabla generada](MUTATIONS.txt): **16/16 mutaciones en rojo**,
  restauración de bytes verificada por el generador; **19/19 condiciones nuevas cubiertas**.

**El criterio 2 no se da por cumplido de extremo a extremo.** Hay dos límites del almacén
en la composición: el documento de montaje de Pope tiene URL interna inalcanzable desde
el PC, y clip-uploads devuelve el 503 conocido. También quedan para el dueño el instalador,
las cámaras y las verificaciones visuales de navegador. No se amplió la exposición de red
ni se fingió que el humo sustituyera esos pasos.

## Criterio → prueba y evidencia

Los nombres abreviados de pruebas pertenecen a `backend/tests/integration/test_node_enrollment_smoke.py`.

| Criterio | Resultado | Prueba o evidencia real |
|---|---|---|
| 1. Alta, latidos aceptados, evento y clip confirmado; código consumido no cero | Cumplido en API en proceso | `test_real_enrollment_heartbeats_event_clip_and_used_code`: JSON exacto, inventario alcanzable, un clip persistido consultado por HTTP, revocación/reapertura autorizada y rechazo `enrollment_code_used`; búsqueda de secretos en stdout/stderr y `caplog` a DEBUG. |
| 1. No aceptar fallos como éxito | Cumplido | `test_rejected_heartbeat_is_not_success`, `test_rejected_event_is_not_success`, `test_clip_failures_are_bounded_and_never_confirmed[grant_503/confirmation/receipt]`. |
| 1. Confianza y salida segura | Cumplido | `test_untrusted_platform_certificate_does_not_consume_code`, `test_invalid_origin_never_sends_the_code`, `test_transport_failure_and_parser_do_not_echo_secrets`; mutaciones M01/M02/G11–G16. |
| 2. Runbook de principio a fin en la composición | **Parcial / bloqueado** | `test_compose_runbook_with_simulated_node`: `1 passed` como diagnóstico de los límites conocidos, no como certificación de todo el runbook; checklist siguiente. Cada paso del documento cita pantalla/código/comando real. |
| 3. Enlaces y secretos | Cumplido | `check_links`: 23 enlaces relativos, 0 rotos; gitleaks del diff y del historial local sin fugas. |

### Informe JSON contrastado contra la API en proceso

La prueba feliz exige exactamente estos campos y valores en la salida real:

```json
{"code":"ok","enrollment":"accepted","heartbeats_accepted":3,"live_view":"not_requested","observability_event":"accepted","verification_clip":"confirmed"}
```

El clip contiene solo bytes sintéticos, no vídeo reproducible. Se comprueba transporte,
hash, recibo y persistencia; **no** se acredita difuminado, rendimiento, consentimiento ni
aprobación de uso. El GET opcional se contrasta en dos sondas con servidor HTTP controlado
(200/503), la misma URL anunciada en los tres latidos y sin cabeceras de autorización/cookie;
no equivale a una sesión autenticada de SCR-16.

**Matiz del código usado:** la API enmascara los códigos como `enrollment_code_invalid`
si el nodo sigue `enrolled` (`EnrollmentCodeService.verify`, regla existente). Para exigir
el rechazo específico, la prueba revoca y abre una re-alta autorizada, presenta el código
anterior consumido y comprueba salida 1 / `enrollment_code_used`. No se cambió la API ni se
reinterpretó un rechazo inválido como usado.

## Checklist del runbook recorrido en el PC

Composición **real** `compose.integration.yml`, imagen existente
`vigia-platform:integration-vig393`, proyecto exclusivo `vigia-it-<aleatorio>`, IP
127.0.0.1, puertos y subred libres. Se reutilizan los fixtures de VIG-393; el nodo del humo
usa las credenciales y SyncClient del contrato, no el simulated_node del kit (solo StubAdmin).
No se construyó ni desplegó nada en server-casa.

| Paso | Estado real | Recorrido / límite |
|---|---|---|
| 1. Arranque y autoridad | ✓ PC | `bootstrap.sh --first-install`, después `bootstrap.sh`; salidas iguales, CA conservada, dos API, worker y mtls-edge levantados. |
| 2. Semilla Pope | **Bloqueado en Compose** | Se ejecutó VIG-394 por HTTP. El PUT del documento a `localstack` terminó en `ConnectError`; salida 1 antes de emitir código. En API en proceso sí termina. No se sustituyó su acta ni sus compuertas por SQL. |
| 3. Catálogo y compuertas | Parcial | Consultas HTTP reales de catálogo y compuertas de la zona diagnóstica de VIG-393, modo commissioning. No se acredita Pope completa ni inspección visual de SCR-04/05. |
| 4. Nodo e identidad | ✓ diagnóstico | `seed.py issue-code` existente de VIG-393 declara nodos. Se reserva su primera zona (productive) y se usa la segunda (commissioning), sin duplicar la semilla. UUID entregados en memoria al humo. |
| 5. Código y huellas | Parcial | Código de un solo uso generado y consumido. TLS y certificados verificados por el cliente; comparación humana de huellas y pantalla SCR-07 no ejecutadas. |
| 6. Confianza y tiempo | Parcial | Raíz pública edge copiada y validada por TLS. `vigia-admin node-configuration show → set --time-source ntp.local → show` ejecutados. Fuente sintética, no ensayo NTP/chrony ni instalación de raíces en navegadores. |
| 7. Alta | ✓ sintético | POST por app. real; NodeCredentials valida y guarda efímeramente las dos credenciales. El instalador de vigia-nodo no se ejecutó. |
| 8. Latidos y cobertura | Parcial | Tres latidos por nodes. con mTLS y evento aceptado; GET de inventario muestra reachable. SCR-03 visual y medición real de cámaras no ejecutadas. |
| 9. Admitir cámaras / líneas base | No ejecutado | No hay cámaras ni nodo real; opciones contrastadas en el código del clon `815e8f1`. |
| 10. Clip y walk-test | **Bloqueado en Compose** | Humo sale 3 / `clip_storage_unpublished` tras alta, tres latidos y evento. En API en proceso, PUT, confirmación y consulta del clip pasan. Sin walk-test ni aprobación de uso. |
| 11. Vista SCR-16 | No ejecutado en Compose | No se levanta servidor de vídeo ni navegador. GET/anuncio opcionales cubiertos por sondas controladas; no se probaron token, canje, stream ni auditoría visual. |
| 12. Re-alta y retiro | ✓ lado plataforma | Revocación, código nuevo, rechazo del anterior usado, re-alta del mismo node_id y nueva salida 3 por clips; revocación final y decommission HTTP 200. `enroll --replace` del equipo real queda sin ejecutar. |
| 13. Cierre | ✓ PC | Fixture `finally`: down de perfiles app/tools, borrado de volúmenes y huérfanos solo de su proyecto. `docker volume ls --filter name=vigia-it-` vacío y `docker ps` solo con servicios ajenos. |

La prueba no oculta el bloqueo de la semilla: solo admite continuar el **diagnóstico de
transporte** si el último intercambio fue exactamente PUT al anfitrión interno localstack
con ConnectError. Otro fallo hace caer el ensayo. El JUnit temporal dejó estas propiedades
(se omite el path con UUID por irrelevante): `seed_pope_exit=1`,
`seed_pope_last_exchange=('PUT', 'localstack', '<ruta de documento sintético>', 'ConnectError')`.
No se guarda ese XML ni credenciales en el repositorio.

La composición deja pendiente la invitación inicial de la operadora; para probar la CLI,
el arnés activa **solo esa cuenta sintética de su base efímera**, sin leer credenciales.
No se propone ese atajo para un destino real. No hay navegación web simulada como evidencia.

## Comprobaciones ejecutadas

Desde `backend/`, salvo las órdenes de git/gitleaks y el generador de mutaciones.

| Orden | Salida real resumida |
|---|---|
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `1375 files already formatted` |
| `uv run python tools/lint_rules.py` | `lint_rules: sin violaciones en src, tests, tools, migrations` |
| `uv run mypy --follow-imports=silent tools/node_enrollment_smoke.py tests/integration/test_node_enrollment_smoke.py` | `Success: no issues found in 2 source files` |
| `uv run pytest tests/integration/test_node_enrollment_smoke.py tests/integration/test_seed_pope.py -q -m integration --basetemp=/tmp/t408 --tb=short` | **Corrida final tras restaurar mutaciones: 45 passed, 1 skipped, 1 warning in 97.81s**, nativa. Antes: mismo resultado en 106.50s. El omitido es solo el Compose optativo, ejecutado aparte. |
| Compose optativo, orden siguiente | **1 passed, 1 warning in 205.57s**; diagnóstico parcial descrito, no recorrido humano completo. |
| `uv run python tools/check_links.py --root .. ../docs/runbooks/u06-alta-nodo-integracion.md` | `1 archivos, 23 enlaces relativos, 0 rotos` |
| Mismo comprobador incluyendo este EVIDENCE.md | `2 archivos, 28 enlaces relativos, 0 rotos` |
| `uv run python tools/check_test_split.py --collect` | 10 135 sin integración, 4 495 de integración, 2 384 propiedades sin integración y 631 de integración; reparto sin huecos ni duplicados. Solo colección. |
| `uv run python tools/check_isolated_imports.py` | `ok: true`, `failures: {}`, `leaked: []`, barrera efectiva. |
| `uv run python -m tools.node_enrollment_smoke --help` | Salida 0, ayuda y opciones en español. |
| `git diff --cached --check` | Sin salida, código 0. |
| `git diff --cached \| gitleaks stdin --redact` | `no leaks found` antes del commit. |
| `gitleaks git --log-opts=main..HEAD --redact` | `1 commits scanned`, `no leaks found` sobre `31d1bf4`; se repite al cerrar la evidencia. |

Las pruebas llevan `DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales` y
`VIGIA_NODO_SRC=/home/manu/vigia/nodo-ref394`. No se ejecutó `mypy tools` entero (stub de
yaml previo), ninguna suite completa, ni `pre_pr.sh` que integra ramas: la torre controla
la integración y el PR; esta entrega solo hace commits locales.

Orden del Compose, sin las variables de las pruebas de API en proceso:

```bash
DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales \
VIGIA_PLATFORM_IMAGE=vigia-platform:integration-vig393 VIGIA_408_COMPOSE=1 \
uv run pytest tests/integration/test_node_enrollment_smoke.py::test_compose_runbook_with_simulated_node \
  -q -m integration --basetemp=/tmp/t408-compose --tb=short \
  --junitxml=/tmp/t408-compose-results.xml -o junit_family=xunit1
```

Hubo carreras de Docker Desktop (`Port mapping ... 8080 is not available`) antes de
las pruebas: se reintentaron. La primera repetición posterior a las mutaciones dio
29 passed y 16 errores de fixture por el mismo mapeo; el reintento nativo terminó con
45 passed en 97.81s. Una primera corrida con API por caso excedió el tope externo
de 220 s; se comparte ahora infraestructura, renovando solo la API y sus cuotas reales.
La última colección completa también necesitó superar ese tope externo; se repitió sin
él y terminó en verde. Ningún timeout se contabilizó como prueba aprobada ni mutación
detectada. Advertencia previa en todas las corridas: WeasyPrint/HarfBuzz-Subset.

## Mutaciones ejecutadas y restauradas

El generador oficial del workspace ejecutó las líneas base, cada mutación y su restauración
con verificación SHA-256. [Salida íntegra generada](MUTATIONS.txt); [manifiesto reproducible](guardas.toml).

```text
== cobertura frente a e9660a4: 19 condiciones nuevas, 19 cubiertas
TABLA_GUARDAS_OK 16
```

| Nombre | Fallo introducido | Prueba en rojo | Salida real |
|---|---|---|---|
| M01 | Código de alta en el informe | `test_real_enrollment_heartbeats_event_clip_and_used_code` | `1 failed, 1 warning in 36.74s` |
| M02 | Perder `enrollment_code_used` | La misma | `1 failed, 1 warning in 38.62s` |
| M03 | Aceptar latido rechazado | `test_rejected_heartbeat_is_not_success` | `1 failed, 1 warning in 34.88s` |
| M04 | Saltar subida/confirmación | Prueba real feliz, lista persistida de clips | `1 failed, 1 warning in 38.37s` |
| M05 | Reintentar indefinidamente 503 | `test_clip_failures_are_bounded_and_never_confirmed[grant_503]` | `1 failed, 1 warning in 37.39s` |
| M06 | Clave extra en `seeded` | `test_seed_pope.py::test_catalogs_and_simulated_enrollment_match_the_source` | `1 failed, 1 warning in 36.13s` |

Las otras diez detectan recibo alterado, evento rechazado, GET omitido/rechazado, URL
prefirmadas en registros, origen HTTP, excepción sin redactar, diagnóstico del almacén
perdido, raíz TLS ajena y errores de argumentos que repiten secretos. Para M05 la sonda
interrumpe el **segundo intercambio**: falla por número de intentos, no por un timeout
rápido ni por esperar un bucle infinito.

Orden real del generador, desde la raíz (en primer plano):

```bash
DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales \
PYTHONPATH=/tmp/opencode/vig394:/home/manu/vigia/plat-vig408/backend \
VIGIA_NODO_SRC=/home/manu/vigia/nodo-ref394 \
uv run --project backend python \
  '/mnt/c/Users/manu_/Documents/MChaves/8.0_Proyecto_Vigia_OpenCode/Vigia/.agents/skills/vigia-calidad-antes-del-pr/tabla_guardas.py' \
  docs/evidence/VIG-408/guardas.toml
```

`port_retry` es el adaptador temporal **ya existente** de VIG-394, fuera del repositorio:
reintenta solo la consulta de metadatos `DockerClient.port` durante un máximo de 60 s,
con pausas de 0,2 s. No sustituye rutas, firma, base, almacén ni aserciones. Se carga con
`-p port_retry` en el manifiesto para evitar que esa carrera invalide las mutaciones;
en un ejecutor sin esa carrera, omitir la opción y la extensión temporal de PYTHONPATH.
Las comprobaciones conjuntas principales pasaron nativamente, sin ese adaptador.

Después de las mutaciones, `git diff --exit-code` contra el índice sobre herramienta,
pruebas y `tools/seed_pope.py` devolvió 0. Se mantuvo idéntico el archivo de la semilla.
No se añade ni modifica ninguna garantía atómica: el consumo del código, las transacciones
y los bloqueos siguen siendo de los servicios existentes; no se reclama concurrencia nueva.

La comprobación final de procesos hijos de esta sesión solo mostró las órdenes puntuales
de verificación (uv, docker y ps), ningún pytest, API, balanceador o mutador persistente.
Los volúmenes `vigia-it-*` no existen y `docker ps` solo lista los servicios ajenos indicados.

## Calidad antes del PR: revisión local

1. Base local entregada por la torre; no fetch, merge ni PR. Pruebas afectadas ejecutadas.
2. Comparaciones por JSON/códigos cerrados. Las búsquedas de subcadenas son exclusivamente
   sondas de fuga de secretos, sin imprimir el valor al fallar.
3. Sin nuevas rutas, eventos, consumidores, suscripciones, tablas ni manifiestos de producto.
4. No hay reintentos nuevos ni reanudación automática; el código de alta es de un solo uso.
5. Sin propiedades ni reglas de dominio modificadas; no corresponde nightly completo.
6. Parametrizaciones estables y colección repartida comprobada.
7. Único cambio compartido: endurecimiento pedido de las claves exactas de `seeded`.
8. Generador oficial: 16 guardas en rojo y 19 condiciones cubiertas, sin supervivientes.
9. Búsquedas acotadas al workspace, sus dependencias y los documentos/clon indicados.

## Dudas abiertas / entrega al dueño

- **VIG-407 / TASK-562:** cómo publicar el almacén por HTTPS alcanzable y con su ACL, o
  cómo acotar la prueba real. El bloqueo afecta tanto al documento de montaje como a clips.
- Completar el recorrido Pope, las pantallas en navegador, confianza en equipos de planta,
  cámaras, líneas base, NTP y sesión de vista cuando el dueño autorice la prueba real.
- La torre hace push, PR e integración. No se marca el criterio 2 como cumplido mientras
  esos tramos permanezcan bloqueados o sin ejecutar.
