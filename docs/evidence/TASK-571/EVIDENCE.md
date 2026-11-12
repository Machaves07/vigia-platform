# TASK-571 · Evidencia local: adopción de `vigia-contracts` v1.4.0 en el backend

Base: `main` en `f64df12`. Rama `torre/contratos-v1-4-0`. Datos solo sintéticos. Sin cambios en
`.github/workflows`, `CODEOWNERS`, `AGENTS.md`, `.agents/`, migraciones, rutas ni dependencias
nuevas. No se tocó Linear, el nodo de planta ni otros repositorios.

## Qué cambia

| Archivo | Cambio |
|---|---|
| `backend/pyproject.toml`, `backend/uv.lock` | `vigia-contracts` de `v1.1.0` (`f4b8853`) a `v1.4.0` (`60d3136d5c88437cd6edb0d81913ae515393812e`), paquete y extra `conformance`. El bloqueo solo cambia esas tres líneas. |
| `backend/tests/unit/test_contract_pin.py` | Etiqueta, commit y versión de la v1.4.0 (la prueba exige cambiarlos a la vez). |
| `backend/src/vigia_platform/node_api/versioning.py` | Ventana de la plataforma `PLATFORM_CONTRACT_WINDOW` hasta `OLDEST_ACCEPTED_MINOR = 0` (4 con la 1.4.0). Ver «Desvíos». |
| `backend/tests/unit/fleet/test_fleet_versions.py` | Bordes de BR-GOB-101 con la ventana mínima explícita; política por defecto de la 1.0 a la 1.4. |
| `backend/tests/integration/test_fleet_target_versions.py` | La versión objetivo «más nueva que la plataforma» deja de ser el literal `1.2.0` (ya en ventana con la 1.4.0) y se deriva de `CONTRACT_VERSION` (`1.5.0` y `1.5.0-rc.1`): misma intención, `rejected_newer`. |
| `backend/openapi/app.yaml`, `frontend/src/client/schema.d.ts`, `frontend/tests/msw/openapi.responses.json` | Regenerados: el `app.yaml` incluye los modelos del contrato (`guard_conditions`, `GuardSignalCondition`, `ConditionSignalValue`, `no_data` y el texto de la regla (6)). Solo campos opcionales y descripciones; ninguna ruta. |
| `backend/tests/integration/test_contract_v1_4_catalog.py` | Prueba nueva de los criterios 2 y 3 (cuatro casos). |
| `CHANGELOG.md`, `backend/tools/pope/SOURCE.md` | Versión adoptada y nota de que la plataforma ya acepta la gramática v1.2.0. |

## Criterio → evidencia

| Criterio | Evidencia |
|---|---|
| 1. `uv sync --frozen && uv run pytest -q` en verde con v1.4.0, con `test_node_route_declarations.py`, `test_registered_tasks.py` y aislamiento | Suite completa en el CI del PR (8 grupos: 4 sin integración y 4 de integración). En local: las pruebas tocadas, los catálogos transversales de la skill de calidad y la de declaraciones de rutas, tareas y aislamiento (salidas abajo). |
| 2. Catálogo con `guard_conditions` (señal `guard` concreta) y señal `no_data` publicado y descargado por el nodo simulado v1.4.0 sin `schema_invalid`; uno con `guard_conditions` sobre una señal que no es `guard` rechazado con el `field` de la regla (6) | `test_a_v1_2_catalog_with_guard_conditions_and_no_data_reaches_the_v1_4_node`: planta Pope sembrada con `seed_pope`, alta real del nodo del kit (`NodeCredentials`, `SyncClient`) por mTLS, `PUT /zones/{id}/signals` con P1 `no_data` y versión del estándar del foso con `guard_conditions = [{JUMBO-IN-POSITION, asserted}]`; el latido provoca `GET zones/{id}/catalog`, el evento `fetch_catalog` es `success`, la caché del nodo es igual al catálogo publicado y el lector estricto lo acepta. `test_guard_conditions_citing_a_non_guard_signal_is_refused_by_rule_6[energy|unknown]`: la consola responde `400 invalid_request` sin versión nueva, y el mismo documento con el lector estricto da `schema_invalid` con `field = standards[i].predicate.guard_conditions[0].signal_id`; con la señal `guard` el documento es válido. Mutación `m_catalogo_sin_regla6`: en rojo. |
| 3. Un nodo simulado v1.1.0 sigue conectándose (latido y catálogo) | `test_a_v1_1_0_node_still_enrolls_beats_and_fetches_the_catalog`: el nodo del kit declara `1.1.0` en el alta, el latido y cada petición (`VersionNegotiator(version="1.1.0")`); la plataforma responde `1.4.0`, acepta sin aviso de retiro (`contract_notice.result = accepted`, sin `retires_at`), el nodo descarga la versión nueva del catálogo, que llega sin campos nuevos (forma v1.1.0), y el inventario lo da `reachable`. Mutación `m_ventana_minima`: en rojo. |

## Mutaciones con nombre

Manifiesto: [guardas.toml](guardas.toml), en el formato de `tabla_guardas.py`. Salida real en
[MUTATIONS.txt](MUTATIONS.txt). El aislamiento del worktree de esta sesión no permite usar git
fuera de él, así que no se pudo correr el generador canónico (llama a `git`). Se usó un
ejecutor equivalente fuera del repositorio, sobre una copia del árbol con su propio entorno: línea
base en verde, mutación del texto exacto, prueba con salida 1 y restauración verificada por
SHA-256. Resultado: `MUTACIONES_OK 6`.

| Mutación | Archivo:línea | Prueba en rojo | Salida |
|---|---|---|---|
| `m_catalogo_sin_regla6` | `.venv/…/vigia_contracts/models/_validators.py:808` (la validación del catálogo que usa la plataforma, sin la regla (6)) | `test_guard_conditions_citing_a_non_guard_signal_is_refused_by_rule_6` | `1 failed, 1 warning in 34.55s` |
| `m_ventana_minima` | `src/vigia_platform/node_api/versioning.py:91` | `test_a_v1_1_0_node_still_enrolls_beats_and_fetches_the_catalog` | `1 failed, 1 warning in 33.64s` |
| `m_ventana_minima` (unidad) | `src/vigia_platform/node_api/versioning.py:91` | `test_br_gob_101_the_default_platform_policy_keeps_every_minor_it_accepted` | `1 failed in 1.60s` |
| `m_publicar_sin_guard_conditions` | `src/vigia_platform/catalog/domain/predicates.py:137` | `test_a_v1_2_catalog_with_guard_conditions_and_no_data_reaches_the_v1_4_node` | `1 failed, 1 warning in 42.91s` |
| `m_senales_sin_no_data` | `src/vigia_platform/catalog/domain/catalog_version.py:545` | `test_a_v1_2_catalog_with_guard_conditions_and_no_data_reaches_the_v1_4_node` | `1 failed, 1 warning in 37.68s` |
| `m_contrato_v1_1` | `pyproject.toml:22` | `test_pyproject_pins_the_release_tag` | `1 failed in 0.11s` |

No hay condiciones nuevas en código que no es de pruebas: `PLATFORM_CONTRACT_WINDOW` es una
constante, y la cubre `m_ventana_minima`. No se añade ninguna garantía atómica, así que no hace
falta una prueba concurrente nueva.

## Desvíos de la especificación

1. **Ventana de versiones.** Con la ventana mínima del contrato (dos menores), una plataforma en
   `1.4.x` acepta solo de la `1.2` a la `1.4` (POLITICA-COMPATIBILIDAD §2) y rechazaría con
   `contract_version_retired` a los nodos `1.0.x` y `1.1.x`, que hoy acepta. Eso incumple el
   criterio 3 y BR-CTR-21 (90 días de aviso), y sería un cambio de comportamiento. La política
   admite ampliar la ventana (BR-CTR-19, NFR-CTR-40), así que la plataforma la lleva hasta la
   menor más antigua que ya aceptaba. Retirar la `1.0` o la `1.1` queda como decisión explícita:
   aviso de retiro y cambio de `OLDEST_ACCEPTED_MINOR`.
2. **`app.yaml` y frontend regenerados.** El `app.yaml` incluye los modelos del contrato, y
   `export_openapi --check` y `generate:check` exigen regenerar en el mismo PR. El frontend sigue
   con `@vigia/contracts` v1.1.0: solo cambian los tipos generados y la copia de respuestas de MSW.
3. **`field` de la regla (6).** La consola responde con el `ApiError` cerrado, que no tiene
   `field`: el rechazo es `400 invalid_request` y la prueba comprueba la regla con el lector
   estricto del contrato sobre el mismo documento.
4. **Suite completa.** La corre el CI del PR. En el PC, con poca memoria libre, se corrieron las
   pruebas tocadas, los catálogos transversales y la prueba nueva.
