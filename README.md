# vigia-platform

Plataforma núcleo de Vigía (U-02 a U-05): backend FastAPI + PostgreSQL, aplicación de página única y despliegue en AWS con CDK. U-02 (plataforma núcleo) está construida: expediente, identidad, auditoría, bandeja de salida, worker, orden administrativa, seis pilas de CDK y canalización. U-03 a U-05 añaden flota, lazo de cierre y la aplicación de página única. Lo que entra en cada versión está en [CHANGELOG.md](CHANGELOG.md); los procedimientos de operación, en [docs/runbooks/](docs/runbooks/README.md).

**Sin AWS por ahora (adenda A-47)**: U-02 se cierra sin cuenta de AWS. El primer despliegue (TASK-154, VIG-98) está diferido, y los trabajos de la canalización que tocan AWS solo corren con la variable de repositorio `VIGIA_AWS_ENABLED=true`.

## Estructura

```text
vigia-platform/
  backend/                 paquete `vigia_platform` (Python 3.12, uv, hatchling)
    src/vigia_platform/
      identity/            domain, application, adapters, auth, authz
      ledger/              domain, application, adapters, chain
      shared/              api, outbox, signing, crypto, observability, clock
    migrations/            Alembic, una sola cadena, solo hacia adelante (TASK-106 a 108)
    openapi/               especificación generada de la aplicación (U-05 genera su cliente)
    tools/                 chequeos de lint propios y guiones de verificación
    tests/                 unit, properties, examples, abuse, isolation, integration, resilience, benchmarks
  infra/                   pilas de AWS CDK en Python (TASK-144 a 150)
  docs/                    formatos del paquete y del archivo de auditoría
  docs/runbooks/           procedimientos de operación 6.1 a 6.6 y contingencias (TASK-152)
  .github/workflows/       flujos de la canalización (TASK-143 y 151)
  .github/scripts/         pasos de AWS de los flujos (tareas puntuales, salidas, barrido)
  local/                   configuración de los servicios locales (colector de OpenTelemetry)
  docker-compose.yml       entorno local: PostgreSQL 16, LocalStack y colector (TASK-103)
  Makefile                 up, down, ps, test, run, worker, migrate, admin (TASK-103)
```

Cada módulo sigue puertos y adaptadores: `domain/` nunca importa FastAPI, SQLAlchemy ni boto3. Los módulos críticos `identity.auth`, `identity.authz`, `ledger.chain`, `shared.signing` y `shared.crypto` se importan sin FastAPI ni SQLAlchemy (NFR-NUC-25); `tools/check_isolated_imports.py` lo comprueba.

## Arranque local

Requisitos: [uv](https://docs.astral.sh/uv/) y acceso de lectura a `Machaves07/vigia-contracts` (credenciales SSH de GitHub en el PC; clave de despliegue de solo lectura en la canalización).

```text
cd backend && uv sync --frozen
cd backend && uv run ruff check . && uv run ruff format --check .
cd backend && uv run python tools/lint_rules.py
cd backend && uv run mypy --strict src
cd backend && uv run pytest -q --hypothesis-profile=ci          # nocturno: --hypothesis-profile=nightly
```

`uv sync --frozen` instala Python 3.12 si hace falta (`.python-version`) y resuelve `vigia-contracts` al commit fijado en `uv.lock`. Las pruebas de integración (`-m integration`) necesitan Docker en ejecución: ver la sección siguiente.

## Entorno local con Docker

`docker-compose.yml` (TASK-103, PAT-NUC-MAN-07) levanta los mismos servicios que usan las pruebas de integración. Requisito: Docker Desktop, o Docker Engine con Compose v2, en ejecución.

| Servicio | Imagen, fijada por digest | Puertos en `127.0.0.1` |
|---|---|---|
| `postgres` | `postgres:16` (PostgreSQL 16.15), base `vigia`, usuario `vigia` | 5432 |
| `localstack` | `localstack/localstack:4.14.0`, edición comunitaria: S3 con sumas de verificación, KMS y Secrets Manager | 4566 |
| `otel-collector` | binario de `otel/opentelemetry-collector-contrib:0.161.0` (`local/otel-collector/`) | 4317 (OTLP gRPC), 4318 (OTLP HTTP) |

Sin MinIO (AGPL). Solo datos generados (NFR-CTR-43). Usuarios y contraseñas son valores fijos del entorno local, no secretos. Si un puerto está ocupado, cámbialo con `VIGIA_LOCAL_POSTGRES_PORT`, `VIGIA_LOCAL_LOCALSTACK_PORT`, `VIGIA_LOCAL_OTLP_GRPC_PORT` o `VIGIA_LOCAL_OTLP_HTTP_PORT`. El colector escribe un resumen de lo que recibe en `docker compose logs otel-collector`.

| `make` (Linux, macOS, WSL, Git Bash) | Windows sin `make`, desde la raíz del repositorio |
|---|---|
| `make up` | `docker compose up -d --wait` (o `docker compose up -d` y luego `docker compose ps` hasta ver los tres `healthy`) |
| `make ps` | `docker compose ps` |
| `make down` | `docker compose down -v` (borra también la base) |
| `make test` | `cd backend; uv run pytest -q --hypothesis-profile=ci` |
| `make run` | `cd backend; uv run uvicorn vigia_platform.shared.api.app:create_app --factory --host 127.0.0.1 --port 8000` |
| `make worker` | `cd backend; uv run python -m vigia_platform.shared.worker.main` |
| `make migrate` | `cd backend; uv run alembic upgrade head` |
| `make admin ARGS="--help"` | `cd backend; uv run vigia-admin --help` |

`migrate` funciona contra el entorno local. Los módulos de `run`, `worker` y `admin` ya existen (TASK-133, TASK-130 y TASK-132), pero los tres procesos piden sus dependencias a un constructor, la **raíz de composición**, que nombran `VIGIA_API_RUNTIME`, `VIGIA_WORKER_RUNTIME` y `VIGIA_ADMIN_RUNTIME` (`vigia_platform.<módulo>:<función>`, asíncrono). Ningún módulo de `src/` implementa todavía ese constructor: solo existen los de las pruebas (`backend/tests/api_support.py`, `backend/tests/worker_process.py`, `backend/tests/admin_support.py`). Sin la variable, el proceso termina sin arrancar con un mensaje que lo dice. `make admin ARGS="--help"` sí funciona: la ayuda no necesita dependencias.

### Variables del entorno local

`make run`, `worker`, `migrate` y `admin` apuntan al entorno de compose con los nombres estándar que ya leen las bibliotecas: libpq y asyncpg (`PG*`), boto3 (`AWS_*`) y el SDK de OpenTelemetry (`OTEL_*`). En PowerShell, antes de los comandos de la tabla:

```powershell
$env:PGHOST = "127.0.0.1"; $env:PGPORT = "5432"; $env:PGUSER = "vigia"; $env:PGPASSWORD = "vigia_local"; $env:PGDATABASE = "vigia"
$env:AWS_ENDPOINT_URL = "http://127.0.0.1:4566"; $env:AWS_DEFAULT_REGION = "us-east-1"
$env:AWS_ACCESS_KEY_ID = "test"; $env:AWS_SECRET_ACCESS_KEY = "test"
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4317"
$env:OTEL_SERVICE_NAME = "vigia-api"      # vigia-worker, vigia-migrate o vigia-admin según el proceso
```

`make migrate` añade las contraseñas locales de los roles que crea la primera migración. En PowerShell, antes de `uv run alembic upgrade head`: `$env:VIGIA_DB_APP_PASSWORD = "vigia_app_local_only"; $env:VIGIA_DB_MIGRATE_PASSWORD = "vigia_migrate_local_only"`.

`make test` no exporta estas variables. Corre `pytest` en otra consola, sin ellas: `AWS_ENDPOINT_URL` redirigiría a LocalStack las pruebas unitarias con moto.

### Migraciones

Una sola cadena de Alembic para U-02, U-03 y U-04, solo hacia adelante (`backend/alembic.ini`, `backend/migrations/`):

- Revisión `<unidad>_<NNNN>`: `nuc_` (U-02), `gob_` (U-03), `laz_` (U-04). `NNNN` es la posición en la cadena, y es lo que devuelve `shared.vigia_schema_version()`. Cada unidad añade eslabones al final: `cd backend && uv run alembic revision --rev-id nuc_0002 -m "identity tables"`.
- `downgrade` lanza `NotImplementedError`. Prohibidos `DROP TABLE`, `TRUNCATE` y `DELETE` sobre tablas de solo anexar, registradas en `backend/migrations/append_only.py`. Nombres de tabla siempre con esquema.
- `cd backend && uv run python tools/lint_migrations.py` lo comprueba (reglas `MIG001` a `MIG005`). También lo corre pytest (`tests/unit/test_lint_migrations.py`).
- En AWS, la tarea `vigia-migrate` solo recibe nombres o ARN de secretos (`VIGIA_DB_MIGRATE_SECRET` y, en el primer despliegue, `VIGIA_DB_MASTER_SECRET_ARN` y `VIGIA_DB_APP_SECRET`). `vigia_platform.shared.migration_credentials` lee los valores de Secrets Manager. El modo TLS lo fijan `PGSSLMODE` y `PGSSLROOTCERT`.
- Cada imagen declara la versión mínima del esquema que necesita (`vigia_platform.shared.schema_version.MINIMUM_SCHEMA_VERSION`) y no arranca sobre uno más viejo.

### Pruebas de integración

`backend/tests/conftest.py` declara dos fixtures de sesión, `postgres_endpoint` y `localstack_endpoint` (con los generadores de `backend/tests/integration/conftest.py`), que cualquier prueba con contenedores reutiliza:

- **Por defecto**, cada corrida de pytest levanta sus propios contenedores con testcontainers, con las mismas imágenes y digests que `docker-compose.yml`, y los borra al terminar.
- **Con `VIGIA_TEST_USE_COMPOSE=1`**, las pruebas usan el entorno ya levantado con `make up` o `docker compose up -d`. Es más rápido al iterar.

```text
cd backend && uv run pytest -q --hypothesis-profile=ci -m integration tests/integration/test_localstack_checksum.py
cd backend && VIGIA_TEST_USE_COMPOSE=1 uv run pytest -q -m integration tests/integration     # bash
cd backend; $env:VIGIA_TEST_USE_COMPOSE = "1"; uv run pytest -q -m integration tests/integration   # PowerShell
```

Sin Docker, estas pruebas fallan con un mensaje; nunca se omiten en silencio.

### Resiliencia (FS-NUC-01 a 10 y dos procesos)

`backend/tests/resilience/` es el arnés de inyección de fallos (LC-NUC-34, PAT-NUC-RES-06):

- **FS-NUC-01 a 10** (`test_fs_nuc_*.py`, marcadas `integration` y `nightly`): cada escenario levanta sus propios contenedores de PostgreSQL 16 y LocalStack (las imágenes de `docker-compose.yml`, con puerto fijo) y los pausa, reinicia o detiene; bloquea puntos concretos con un intermediario TCP, o mata procesos de prueba de `vigia-api` y `vigia-worker`. Los contenedores de la sesión no se tocan. FS-NUC-10 es el ensayo de restauración sobre datos generados: copia física y WAL archivado restaurados en contenedores nuevos, verificación completa de todas las cadenas y tiempo frente al RTO de 4 h.
- **Dos procesos** (`test_two_processes.py`, solo `integration`, también en `ci`): dos `vigia-api` y dos `vigia-worker` reales tras un balanceador local (NFR-NUC-06).
- Cada escenario imprime su semilla (la de `--hypothesis-seed`; se repite con ella) y deja un informe JSON en `VIGIA_RESILIENCE_REPORT_DIR` (por defecto `backend/reports/resilience/`, fuera del repositorio), que `nightly.yml` conserva 90 días.

```text
cd backend && uv run pytest -q --hypothesis-profile=nightly tests/resilience/
cd backend && uv run pytest -q --hypothesis-profile=ci -m integration tests/resilience/test_two_processes.py
```

En WSL con Docker Desktop, si testcontainers falla con «Exec format error», exporta antes `DOCKER_CONFIG` hacia una configuración de Docker sin `credsStore`.

## Dependencia del contrato

`vigia-contracts` se consume por `git+ssh` con `#subdirectory=generated/python` (ADR-004). Durante el desarrollo se fija por **hash de commit** de `main`; TASK-153 (VIG-96) lo cambia a la etiqueta `v1.0.0`:

```toml
"vigia-contracts @ git+ssh://git@github.com/Machaves07/vigia-contracts.git@0365139aa69b7abe788b60a2224a108fa4e97c37#subdirectory=generated/python"
```

## Reglas de lint bloqueantes

| Regla | Qué prohíbe | Dónde |
|---|---|---|
| `S301`, `S302`, `S307`, `S506`, `TID251` | `pickle`, `marshal`, `yaml.load` inseguro y `eval` (NFR-NUC-27) | ruff, `backend/pyproject.toml` |
| `DTZ*` | `datetime` sin zona horaria (BR-NUC-47) | ruff |
| `TID251` | hora del sistema fuera de `vigia_platform.shared.clock` (PAT-NUC-RES-07) | ruff, con excepción solo para `shared/clock/` |
| `G001` a `G004` | mensaje de registro construido con `format`, `%`, `+` o f-string (NFR-NUC-17) | ruff |
| `TID251` en `src/` | registro con la biblioteca estándar (`logging.getLogger`, `logging.info`…, `logging.root`): en `src/` solo `get_logger` (NFR-NUC-17) | ruff, `backend/src/ruff.toml` |
| `VIG001` | `text()` con f-string, `%`, `.format()` o concatenación (NFR-NUC-19) | `tools/lint_rules.py` |
| `VIG002` | httpx sin `timeout=` (PAT-NUC-RES-03) | `tools/lint_rules.py` |
| `VIG003` | boto3 sin `config=` o `botocore.config.Config` sin tiempos de espera (PAT-NUC-RES-03) | `tools/lint_rules.py` |
| `VIG004` | mensaje de `get_logger` o nombre de tramo o evento que no es un literal ni una constante `Final` (NFR-NUC-17, PR-NUC-54) | `tools/lint_rules.py` |

## Observabilidad

`vigia_platform.shared.observability` (TASK-104) da el registro JSON con redacción (`get_logger`, `log_context`, `configure_logging`), las métricas con nombre fijo (`get_metrics()`, catálogo en `metrics.CATALOG` y condiciones de alarma de NFR-NUC-38 en `metrics.ALARM_CONDITIONS`) y OpenTelemetry con exportación OTLP de cola acotada hacia `localhost:4317` (`tracing.configure_telemetry`, `tracing.enable_auto_instrumentation`). El mensaje de un registro y el nombre de un tramo o de un evento son siempre constantes (VIG004): un mensaje construido en ejecución sale como `[redactado]` y un nombre de tramo sin registrar (`tracing.span_name`, `tracing.event_name`) sale como `other`. Los datos van como campos o atributos con nombre y solo salen identificadores y enumeraciones. Con el colector caído, la aplicación no espera: lo descartado se cuenta en `otel_dropped_total`.

`tools/lint_rules.py` corre también dentro de la suite (`tests/unit/test_lint_rules.py`), así que `pytest` falla si el árbol tiene una violación.

## Variables de configuración

Cada proceso lee su configuración del entorno **una vez**, al arrancar, con un modelo estricto de Pydantic (`extra="forbid"`). Si falta una variable obligatoria o no es válida, el proceso no arranca. Ningún secreto, clave ni `.env` entra al repositorio. En AWS, las tareas solo reciben **nombres o ARN** de secretos y claves (`infra/stacks/compute.py`), nunca sus valores. Las del entorno local están en «Variables del entorno local».

| Variable | Procesos | Obligatoria | Contenido |
|---|---|---|---|
| `VIGIA_ENVIRONMENT` | api, worker, admin, migrate | Sí | `local`, `test`, `staging-<n>` o `pilot` |
| `VIGIA_SECRETS_KEY_ARN` | api, worker | Sí | Clave KMS `vigia-secrets` del cifrado de sobre |
| `VIGIA_API_RUNTIME`, `VIGIA_WORKER_RUNTIME`, `VIGIA_ADMIN_RUNTIME` | api, worker, admin | Sí | Constructor de dependencias `vigia_platform.<módulo>:<función>` (raíz de composición) |
| `VIGIA_HEALTH_SENTINEL_KEY` | api, worker | No (`health/ready-sentinel`) | Objeto centinela del depósito de evidencias que consulta `/health/ready` |
| `VIGIA_PUBLIC_ORIGIN` | api, admin | No | `https://app.<dominio>`: origen exigido por la barrera anti-falsificación y base del enlace de invitación. Sin él, toda petición que cambia estado con `Origin` se rechaza |
| `VIGIA_CSP_STORE_ORIGINS` | api | No | Orígenes del almacén para `media-src` y `connect-src`, separados por espacios |
| `VIGIA_STATIC_DIR`, `VIGIA_VERIFIER_PATH` | api | No | Construcción de la aplicación de página única y `tools/vigia_verify.py`, cuyo SHA-256 publica `/.well-known/vigia-verifier` |
| `VIGIA_API_PORT`, `VIGIA_FORWARDED_ALLOW_IPS` | api | No (8000; ninguna) | Puerto y redes del balanceador de las que se aceptan cabeceras `X-Forwarded-*` |
| `VIGIA_WORKER_HEALTH_PORT` | worker | No (8001) | Puerto de la sonda de salud del worker |
| `VIGIA_PROVIDER_ORGANIZATION_ID` | admin | Todas las órdenes salvo `bootstrap` | La organización proveedora que imprimió `bootstrap` |
| `VIGIA_BOOTSTRAP_INVITATION_SECRET` | admin | No (`vigia/<entorno>/bootstrap/invitation`) | Secreto de un solo uso donde queda el enlace de invitación |
| `VIGIA_NODE_CA_KEY_ARN`, `VIGIA_EDGE_BUCKET`, `VIGIA_ROOT_CERTIFICATE_KEY` | admin | Con `first_deploy=true` o `ca_rotation=true` | Clave de `vigia-node-ca`, depósito de borde y `ca/root.pem` |
| `VIGIA_ARCHIVE_BUCKET` | admin | Para `restore-audit-partition` | Depósito `vigia-archive` |
| `VIGIA_DB_MIGRATE_SECRET`; `VIGIA_DB_MASTER_SECRET_ARN` y `VIGIA_DB_APP_SECRET` | migrate | En AWS; los dos últimos solo en el primer despliegue | Secretos de los roles de la base. En local, `PG*` y las contraseñas `VIGIA_DB_APP_PASSWORD` y `VIGIA_DB_MIGRATE_PASSWORD` |
| `VIGIA_LOG_LEVEL` | todos | No | Nivel del registro JSON |
| `PGSSLMODE`, `PGSSLROOTCERT` | migrate y conexiones a la base | En AWS | Modo TLS de la conexión con RDS |

La definición de tareas de `infra/stacks/compute.py` fija además variables que todavía no lee ningún módulo de `src/`: `VIGIA_DB_APP_SECRET` de api y worker, `VIGIA_EVIDENCE_BUCKET`, `VIGIA_SIGNING_SECRET_PREFIX`, `VIGIA_METRICS_NAMESPACE`, `VIGIA_SERVICE` y los ajustes de la API (`VIGIA_UVICORN_WORKERS`, `VIGIA_BULKHEAD_*`, `VIGIA_DB_POOL_*`…). Las leerá la raíz de composición.

## Orden administrativa `vigia-admin`

`vigia-admin` (LC-NUC-07, TASK-132) hace las operaciones sin interfaz del operador del proveedor. Llama a los mismos servicios de aplicación que la API. La salida es **una línea JSON** con identificadores, nunca enlaces ni contraseñas. `--dry-run` valida y muestra lo que haría sin escribir nada. `uv run vigia-admin <orden> --help` da la ayuda de cada orden.

| Orden | Para qué | Runbook |
|---|---|---|
| `bootstrap [--resume] [--publish-root]` | Organización proveedora, primer `platform_operator` por invitación, claves Ed25519 por propósito y raíz de `vigia-node-ca` | Primer despliegue (TASK-154) |
| `create-organization --operator …` | Organización cliente, su primera planta y su primer administrador | Primer despliegue |
| `rotate-key <propósito> --operator …` | Rotación de la clave de firma de `catalog`, `gate`, `live_view_token`, `key_set` o `checkpoint` | [6.4](docs/runbooks/6.4-key-rotation.md) |
| `rotate-node-ca --new-key-id … --operator …` | Paquete de dos raíces de `vigia-node-ca`, solo con `ca_rotation=true` | [6.4](docs/runbooks/6.4-key-rotation.md) |
| `replay-dead-letter <evento> <consumidor> --operator …` | Reentrega de una entrega de la cola muerta | [6.3](docs/runbooks/6.3-dead-letter-replay.md) |
| `create-partitions --until AAAA-MM --operator …` | Particiones mensuales por adelantado | [Contingencia de subparticionado](docs/runbooks/subpartitioning-contingency.md) |
| `restore-audit-partition <objeto> --sha256 … --output …` | Descarga, verifica y extrae un archivo de auditoría (solo lectura) | [6.5](docs/runbooks/6.5-audit-archive-and-restore.md) |
| `record-restore-drill --result ok\|failed --operator …` | Registra el ensayo de restauración y reinicia su alarma | [6.1](docs/runbooks/6.1-quarterly-restore-drill.md) |

Códigos de salida: `0` hecho, `1` configuración o error inesperado, `2` uso incorrecto, `3` sin confirmación, `4` rechazo de la operación, `5` dependencia no disponible (reintentable). En local: `make admin ARGS="…"`. En un despliegue, como tarea puntual de ECS: `python .github/scripts/staging.py run-task --environment pilot --task admin -- vigia-admin …` (detalle en [runbooks](docs/runbooks/README.md#cómo-lanzar-vigia-admin-y-vigia-migrate-en-un-despliegue)).

## Flujo de release

Los flujos están en `.github/workflows/` (TASK-143 y TASK-151). Los trabajos que tocan AWS (federación con `vigia-deploy`, ECR, `cdk deploy`) solo corren con la variable de repositorio `VIGIA_AWS_ENABLED=true` (adenda A-47). Sin ella, `release.yml` ensaya hasta donde no hace falta AWS y lo dice en el resumen.

1. **`ci.yml`**, en cada PR listo: escaneo de secretos; backend sin integración (ruff, `lint_rules`, `mypy --strict`, pytest); backend con integración; infra (pruebas y `cdk synth`). La imagen `arm64` se construye y se escanea sin publicarse.
2. **`nightly.yml`**, cada noche sobre `main`: propiedades con el perfil `nightly`, bancos, volumetría, resiliencia FS-NUC-01 a 10 con el ensayo de restauración y conformidad.
3. **`release.yml`**, a mano (`workflow_dispatch`) sobre un commit con el nocturno verde. Entradas: `version` (`X.Y.Z`), `dry-run` (verdadero por omisión) y `soak`.
   1. Construye y publica la imagen por digest y calcula el `cdk diff`.
   2. Levanta `staging-<n>` efímero: despliega, migra, ejecuta `vigia-admin bootstrap` y las comprobaciones de despliegue (`backend/tools/deploy_checks.py`).
   3. Destruye `staging-<n>` siempre.
   4. Pide la **aprobación del dueño** en el entorno de GitHub `pilot`, despliega, migra, verifica salud, humo y 15 minutos de alarmas, y crea la etiqueta `vX.Y.Z`.
   5. Cada etiqueta lleva su sección en [CHANGELOG.md](CHANGELOG.md).
4. **`rollback.yml`**, a mano: devuelve `vigia-api` y `vigia-worker` al digest anterior (entradas `digest` y `verifier-sha256`) y repite la verificación posterior. El esquema ya migrado se queda: las migraciones son solo hacia adelante.
5. **`trust-store.yml`**, a mano: respaldo para añadir una versión de `ca/crl.pem` al almacén de confianza `vigia-node-trust` (entrada `crl-version`).
6. **`staging-sweeper.yml`**, cada noche: destruye los `staging-<n>` huérfanos de más de 6 horas.

## Operación

Los procedimientos de conmutación y recuperación (RESILIENCY-13, NFR-NUC-12) están en [docs/runbooks/](docs/runbooks/README.md):

- [6.1 restauración de prueba trimestral](docs/runbooks/6.1-quarterly-restore-drill.md);
- [6.2 conmutación y vuelta](docs/runbooks/6.2-failover-and-failback.md);
- [6.3 cola muerta](docs/runbooks/6.3-dead-letter-replay.md);
- [6.4 rotación de claves](docs/runbooks/6.4-key-rotation.md);
- [6.5 archivado y restauración de auditoría](docs/runbooks/6.5-audit-archive-and-restore.md);
- [6.6 contraseña de la base](docs/runbooks/6.6-database-password-rotation.md);
- y las contingencias.

Los enlaces relativos de este archivo y de `docs/` se comprueban con:

```text
uv run --project backend python backend/tools/check_links.py README.md docs/
```

## Documentación de referencia

- Diseño aprobado (fuente del *qué*): `aidlc-docs/construction/plataforma-nucleo/` del espacio de trabajo de Vigía, fuera de este repositorio.
- Decisiones posteriores del dueño: `docs/decisions/adenda-al-diseno.md` del plan; mandan sobre el diseño donde difieran.
- Formatos de este repositorio: [paquete `vigia-package`](docs/package-format.md) y [archivo de auditoría](docs/audit-archive-format.md).
- Registro de cambios: [CHANGELOG.md](CHANGELOG.md).
- Reglas para agentes: [AGENTS.md](AGENTS.md).
