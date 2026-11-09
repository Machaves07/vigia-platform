# VIG-394 / TASK-547 · Evidencia local

Base de trabajo: `3f738f5`, rama `vig-394-semilla-pope`. Sin consultas a Linear, cambios remotos,
push, PR ni ejecución de la semilla en server-casa. No se cambian dependencias, migraciones,
manifiestos de producto, flujos ni archivos protegidos. Los únicos datos utilizados son sintéticos.

## Entrega

- `backend/tools/seed_pope.py`: herramienta HTTP con dos sesiones, plan sin red, reanudación por
  códigos de zona/nodo y título de estándar, comparación RFC 8785, acta de alcance sintética,
  montaje aprobado y uso pendiente. Sin SQL, almacenamiento de credenciales ni lectura del reloj.
- `backend/tools/pope/zone_catalog.json` y `zone_catalog.pope_foso.json`: copias byte a byte.
- `backend/tools/pope/SOURCE.md`: procedencia, hashes, uso y decisiones conservadoras de la API.
- `backend/tests/integration/test_seed_pope.py`: 30 casos; composición real en proceso,
  PostgreSQL y LocalStack mediante testcontainers. Los casos de límites del orquestador también
  usan transporte HTTP controlado para aislar cada decisión, sin sustituir los servicios de los
  cuatro criterios. Un solo reloj, `WALL`, inyectado al runtime; topes de base de 60 s y firma de
  120 s en el arnés propio. El alta simulada no escribe su código, CSR ni credenciales en archivos.
- `docs/evidence/VIG-394/guardas.toml`: mutaciones reproducibles y cobertura de condiciones.

## Criterio → prueba

Todos los nombres siguientes pertenecen a `backend/tests/integration/test_seed_pope.py`.

| Criterio | Evidencia |
|---|---|
| 1. Catálogos y hashes iguales al origen | `test_catalogs_and_simulated_enrollment_match_the_source`: carga vigente, sobre de la versión, comparación RFC 8785 independiente, firmas verificadas con las claves del alta e igualdad del sobre en `initial_configuration`. `test_copied_catalog_has_the_recorded_hash` y `test_catalog_hash_matches_vigia_nodo_when_available`, parametrizadas para ambos archivos. |
| 2. `commissioning`, nodo con ambas zonas y código sin persistir | La misma prueba comprueba montaje `approved`, uso `pending`, nodo `declared`, dos asignaciones, cuatro pares cámara-zona y tres cámaras únicas. Busca el código exacto en todos los archivos generados de su directorio y el de la plataforma y en `caplog`, sin mostrarlo al fallar. |
| 3. Segunda ejecución sin nuevos objetos | `test_second_execution_creates_nothing_and_does_not_disclose_a_code`: recuentos antes/después de zonas, pares cámara-zona, estándares, versiones, nodos, asignaciones, códigos, admisiones, actas, documentos y registros del expediente; una sola salida `unchanged` / «sin cambios», con cero operaciones HTTP de escritura. |
| 4. Cobertura insatisfacible y detención | `test_unsatisfiable_coverage_stops_before_the_next_zone_and_node`: `required_count=1` con dos cámaras requeridas; rechazo HTTP real `400 / catalog_unsatisfiable_coverage`, salida 1, sin segunda zona ni nodo, catálogo, acta o código. No se usa el lector semántico local del contrato que impediría llegar a la API. |

La prueba canónica distingue representaciones equivalentes (orden de claves y `8.0` frente a `8`)
de diferencias reales; no descarta ids de cámara/señal, campos desconocidos ni `stream_reference`.
Las asignaciones de autor y vigencia del estándar, igual que sus ids/versiones, las decide la
plataforma y se excluyen explícitamente, no mediante una regla genérica sobre nombres de campo.

No se añade ninguna garantía atómica ni de exclusión entre ejecuciones concurrentes: es una
herramienta secuencial. Las transacciones, restricciones únicas, consumo del código y bloqueo de
zona/nodo siguen siendo responsabilidad de las rutas existentes y no se modifican.

## Comprobaciones ejecutadas

Las órdenes de backend se ejecutan desde `backend/`, salvo indicación contraria.

| Orden | Salida real resumida |
|---|---|
| `uv run ruff check .` | `All checks passed!` |
| `uv run ruff format --check .` | `1373 files already formatted` |
| `uv run python tools/lint_rules.py` | `lint_rules: sin violaciones en src, tests, tools, migrations` |
| `uv run mypy tools` | **Fallo previo del entorno**: `tools/check_test_split.py:59: Library stubs not installed for "yaml" [import-untyped]`; `Found 1 error in 1 file (checked 19 source files)`. |
| `uv run mypy --follow-imports=silent tools/seed_pope.py tests/integration/test_seed_pope.py` | `Success: no issues found in 2 source files`. Sigue activo `strict=true` del proyecto; se silencian solo los diagnósticos transitivos de soportes ya existentes. |
| Prueba completa de la tarea, con `VIGIA_NODO_SRC` | **Corrida final nativa, sin adaptador**: `30 passed, 1 warning in 40.56s`; advertencia previa de WeasyPrint sobre HarfBuzz-Subset. También pasó con el adaptador temporal: `30 passed, 1 warning in 66.88s`. |
| `uv run python -m tools.seed_pope --dry-run --catalog tools/pope/zone_catalog.json` | Salida 0; plan: cuatro familias, `POPE` con tres cámaras y cinco estándares, `POPE-FOSO` con cam102 y un estándar, montaje aprobado, uso pendiente. |
| `uv run python tools/check_test_split.py` | `check_test_split: los grupos reparten la colección sin huecos ni duplicados`. |
| `uv run python tools/check_test_split.py --collect` | **Verde**: 10 135 pruebas sin integración, 4 479 de integración, 2 384 propiedades sin integración y 631 de integración; unión exacta de los grupos, sin huecos ni duplicados. Solo recolección, no ejecución de esas suites. |
| `uv run python tools/check_isolated_imports.py` | `ok: true`, `failures: {}`, `leaked: []`; bloqueo efectivo de FastAPI/SQLAlchemy y todos los módulos críticos importados. |
| `uv run python tools/check_outbound_clients.py` | `[]`, salida 0. |
| `uv run python -m vigia_platform.shared.api.export_openapi --check` | `openapi/app.yaml coincide con las rutas actuales`. |
| `uv run python tools/check_links.py tools/pope/SOURCE.md` | `1 archivos, 0 enlaces relativos, 0 rotos`. |
| `uv run python tools/check_links.py --root .. tools/pope/SOURCE.md ../docs/evidence/VIG-394/EVIDENCE.md` | `2 archivos, 2 enlaces relativos, 0 rotos`. La raíz explícita evita interpretar `backend/` como raíz al verificar documentos de `docs/`. |
| `git diff --cached --check` | Sin salida, código 0 antes del commit de implementación. |
| `git diff --cached \| gitleaks stdin --redact` | `no leaks found` antes del commit. |
| `gitleaks git --log-opts=3f738f5..HEAD --redact` | `1 commits scanned`, `no leaks found` sobre el commit de implementación. |

El problema de mypy se reproduce con **solo** `uv run mypy tools/check_test_split.py`, sin importar
ningún archivo nuevo. Su SHA-256 en la base y en el árbol actual es el mismo:
`d4a1bb1e2a898104ae2e28ef3795c62838d52f95b92e6d5110bca16e9d646fe5`.
No se instala `types-PyYAML` ni se modifica `pyproject.toml`/`uv.lock`. La primera comprobación
con seguimiento transitivo de los soportes de prueba notificó además diagnósticos preexistentes
en `outbox_support.py`, `fleet_support.py`, `fleet_credentials_support.py` y
`conformance/platform_target.py`; ninguno apuntaba a los archivos nuevos.

No se ejecutó la suite completa del backend, ni ningún despliegue o aprobación de uso.

## Particularidad del ejecutor Docker Desktop

No hay marcador `integracion` ni xdist en este entorno. Se usa el marcador existente `integration`,
sin `-n 0`. Algunas corridas ordinarias fallaron **antes de las pruebas** por
`Port mapping ... 8080/4566 is not available` (consulta de metadatos inmediatamente tras crear
Ryuk/LocalStack). Una corrida ordinaria llegó a ejecutar los criterios y descubrió el rechazo
local del lector semántico; ese problema de la herramienta quedó corregido. El modo `bridge_ip`
de testcontainers no es alcanzable desde este WSL y tampoco se adopta.

Para verificar el trabajo sin cambiar ninguna dependencia se empleó este adaptador **temporal
fuera del repositorio**, `/tmp/opencode/vig394/port_retry.py`:

```python
import time
from testcontainers.core.docker_client import DockerClient

original_port = DockerClient.port

def port(self, container_id, number):
    deadline = time.monotonic() + 60
    while True:
        try:
            return original_port(self, container_id, number)
        except ConnectionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)

DockerClient.port = port
```

Solo reintenta la consulta de mapeo de puertos del ejecutor durante un máximo de 60 s. No
sustituye la API, Docker, PostgreSQL, LocalStack, firma, validación ni ninguna aserción. No es
parte del código de producto ni una dependencia. Orden real de las pruebas:

```sh
DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales \
PYTHONPATH=/tmp/opencode/vig394:. \
VIGIA_NODO_SRC=/home/manu/vigia/nodo-ref394 \
uv run pytest -p port_retry tests/integration/test_seed_pope.py \
  -q -m integration --basetemp=/tmp/t394
```

En un ejecutor sin esa carrera, omitir `-p port_retry` y la extensión temporal de `PYTHONPATH`.
La **última corrida**, después de restaurar las 32 mutaciones, ya pasó sin ese adaptador:

```sh
DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales \
VIGIA_NODO_SRC=/home/manu/vigia/nodo-ref394 \
uv run pytest tests/integration/test_seed_pope.py -q -m integration \
  -p no:randomly --basetemp=/tmp/t394
# 30 passed, 1 warning in 40.56s
```

## Decisiones conservadoras y límites pendientes

1. La instrucción explícita de la torre permite sembrar v1.1.0 pese al orden original de A-77:
   no se actualiza ni reinterpreta el fixture. La actualización a v1.2.0 sigue pendiente del catálogo
   y de que la plataforma acepte antes esa versión.
2. Los códigos reales son `POPE` y `POPE-FOSO`; `pope_foso` es el nombre lógico del segundo fixture.
3. La ruta de consola del catálogo entrega la carga; su sobre se obtiene por el detalle de versión.
   No se amplía la API para ajustarla a la redacción abreviada del criterio.
4. Los estándares no tienen `code` en la API actual: se usa su título declarado como clave estable.
5. No hay consulta HTTP de códigos de alta vigentes: un nodo existente nunca recibe reemisión
   automática, también si el primer intento se interrumpió antes de mostrar el código. Para
   recuperar uno perdido o vencido, el operador usa explícitamente el flujo normal de SCR-07.
6. El acta y PNG solo acreditan un montaje **sintético de integración**, no la instalación de planta;
   el texto del acta lo dice. El walk-test y aprobación de uso están fuera de alcance.
7. «Sin cambios» se refiere a objetos de la semilla y al expediente: las lecturas normales sí pueden
   actualizar la sesión y anexar auditoría de proveedor; no se desactiva ese rastro.

## Mutaciones y cierre

Se ejecutó el generador de tablas obligatorio del workspace, con este manifiesto y la base
`3f738f5`. El resultado íntegro, **generado por el script**, está en [MUTATIONS.txt](MUTATIONS.txt);
el manifiesto reproducible está en [guardas.toml](guardas.toml).

Orden desde la raíz del repositorio (ruta externa del generador abreviada como `<plan>`):

```sh
DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales \
PYTHONPATH=/tmp/opencode/vig394:/home/manu/vigia/plat-vig394/backend \
VIGIA_NODO_SRC=/home/manu/vigia/nodo-ref394 \
uv run --project backend python \
  '<plan>/.agents/skills/vigia-calidad-antes-del-pr/tabla_guardas.py' \
  docs/evidence/VIG-394/guardas.toml
```

Resultado real:

```text
== cobertura frente a 3f738f5: 45 condiciones nuevas, 45 cubiertas
TABLA_GUARDAS_OK 32
```

El generador ejecutó las líneas base, mutó cada guarda, obtuvo salida 1 de pytest y restauró
el archivo original verificando su SHA-256 en cada caso. Las seis mutaciones solicitadas:

| Nombre | Cambio introducido | Prueba en rojo | Salida real |
|---|---|---|---|
| M01 | Omitir cam102 de `pope_foso` | `test_catalogs_and_simulated_enrollment_match_the_source` | `1 failed, 1 warning in 42.05s` |
| M02 | Incluir `stream_reference` en el catálogo que se intenta publicar | La misma prueba | `1 failed, 1 warning in 40.04s` |
| M03 | No buscar la zona por `code`, intentar crearla de nuevo | `test_second_execution_creates_nothing_and_does_not_disclose_a_code` | `1 failed, 1 warning in 40.48s` |
| M04 | Escribir el código en `enrollment-code.txt` | `test_catalogs_and_simulated_enrollment_match_the_source` | `1 failed, 1 warning in 41.22s` |
| M05 | Capturar el rechazo y seguir con la siguiente zona | `test_unsatisfiable_coverage_stops_before_the_next_zone_and_node` | `1 failed, 1 warning in 33.18s` |
| M06 | Comparar serializaciones JSON en vez de RFC 8785 | `test_comparison_is_rfc8785_not_serialization_and_preserves_content` | `1 failed, 1 warning in 6.20s` |

M03 intenta duplicar la zona; la unicidad preexistente de la API impide el duplicado con 409.
La prueba exige una segunda ejecución satisfactoria y los recuentos intactos, no tolerar ese
conflicto como «sin cambios». M02 es rechazado por la validación estricta del catálogo: no se
debilita la plataforma para conseguir publicar una carga inválida.

Las otras 26 mutaciones verifican guardas de alcance y asignación, admisiones/paginación,
estándares y parámetros, compuertas decididas, ausencia de sesiones en las subidas, rechazos
sin divulgar cuerpos, sesiones obligatorias, `--dry-run` y no reemisión de códigos. No quedó
ninguna mutación superviviente ni ningún error de colección contabilizado como detección.

### Estado final verificado

- Implementación y pruebas en el commit local `cc50b57`.
- Tras las mutaciones, `git diff --exit-code HEAD --` sobre la herramienta, la prueba y
  `catalog/domain/zone_camera.py` devolvió 0: también quedó restaurado ese archivo existente.
- Corrida nativa posterior: **30 pruebas en verde**; lint, formato y tipos aislados, verdes.
- `--dry-run` volvió a terminar en 0 con el plan esperado, sin sesiones ni conectividad.
- La búsqueda de `enrollment-code.txt` en `/tmp/t394-mut` quedó vacía. El proceso de pruebas,
  los hilos de API y sus contenedores quedaron liberados; `ps` no muestra procesos propios de
  pytest, el comprobador de reparto, el mutador ni el balanceador de pruebas.
- `docker ps` solo muestra servicios ajenos a esta tarea; no se detienen ni modifican.
- Sin cambios en archivos protegidos, dependencias o configuración remota. El único fallo
  pendiente de las comprobaciones solicitadas es el stub de YAML preexistente descrito arriba.
