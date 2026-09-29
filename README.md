# vigia-platform

Plataforma núcleo de Vigía (U-02 a U-05): backend FastAPI + PostgreSQL, aplicación de página única y despliegue en AWS con CDK. Este repositorio nace con TASK-101 (VIG-18): el backend en Python 3.12, sus herramientas de calidad y el árbol de módulos vacío. TASK-103 (VIG-21) añade el entorno local con Docker; la base de datos, la infraestructura y el frontend llegan en tareas posteriores.

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
    tests/                 unit, properties, examples, abuse, isolation, integration, benchmarks
  infra/                   pilas de AWS CDK en Python (TASK-144)
  docs/runbooks/           restauración, cola muerta, rotación, archivado
  .github/workflows/       flujos de la canalización (TASK-143 y 151)
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

`run`, `worker`, `migrate` y `admin` quedan operativos cuando llegan sus tareas (TASK-133, TASK-130, TASK-106 y TASK-132). Hasta entonces, `make` avisa de qué falta y termina con error.

### Variables del entorno local

`make run`, `worker`, `migrate` y `admin` apuntan al entorno de compose con los nombres estándar que ya leen las bibliotecas: libpq y asyncpg (`PG*`), boto3 (`AWS_*`) y el SDK de OpenTelemetry (`OTEL_*`). En PowerShell, antes de los comandos de la tabla:

```powershell
$env:PGHOST = "127.0.0.1"; $env:PGPORT = "5432"; $env:PGUSER = "vigia"; $env:PGPASSWORD = "vigia_local"; $env:PGDATABASE = "vigia"
$env:AWS_ENDPOINT_URL = "http://127.0.0.1:4566"; $env:AWS_DEFAULT_REGION = "us-east-1"
$env:AWS_ACCESS_KEY_ID = "test"; $env:AWS_SECRET_ACCESS_KEY = "test"
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://127.0.0.1:4317"
$env:OTEL_SERVICE_NAME = "vigia-api"      # vigia-worker, vigia-migrate o vigia-admin según el proceso
```

`make test` no exporta estas variables. Corre `pytest` en otra consola, sin ellas: `AWS_ENDPOINT_URL` redirigiría a LocalStack las pruebas unitarias con moto.

### Pruebas de integración

`backend/tests/integration/conftest.py` ofrece dos fixtures de sesión, `postgres_endpoint` y `localstack_endpoint`, que cualquier prueba de integración reutiliza:

- **Por defecto**, cada corrida de pytest levanta sus propios contenedores con testcontainers, con las mismas imágenes y digests que `docker-compose.yml`, y los borra al terminar.
- **Con `VIGIA_TEST_USE_COMPOSE=1`**, las pruebas usan el entorno ya levantado con `make up` o `docker compose up -d`. Es más rápido al iterar.

```text
cd backend && uv run pytest -q --hypothesis-profile=ci -m integration tests/integration/test_localstack_checksum.py
cd backend && VIGIA_TEST_USE_COMPOSE=1 uv run pytest -q -m integration tests/integration     # bash
cd backend; $env:VIGIA_TEST_USE_COMPOSE = "1"; uv run pytest -q -m integration tests/integration   # PowerShell
```

Sin Docker, estas pruebas fallan con un mensaje; nunca se omiten en silencio.

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
| `VIG001` | `text()` con f-string, `%`, `.format()` o concatenación (NFR-NUC-19) | `tools/lint_rules.py` |
| `VIG002` | httpx sin `timeout=` (PAT-NUC-RES-03) | `tools/lint_rules.py` |
| `VIG003` | boto3 sin `config=` o `botocore.config.Config` sin tiempos de espera (PAT-NUC-RES-03) | `tools/lint_rules.py` |

`tools/lint_rules.py` corre también dentro de la suite (`tests/unit/test_lint_rules.py`), así que `pytest` falla si el árbol tiene una violación.

## Variables de configuración y orden administrativa

Las del entorno local están en «Variables del entorno local». El modelo de configuración de la aplicación llega con TASK-133 y la orden `vigia-admin` con TASK-132. Ningún secreto, clave ni `.env` entra al repositorio.

## Documentación de referencia

- Diseño aprobado (fuente del *qué*): `aidlc-docs/construction/plataforma-nucleo/` del espacio de trabajo de Vigía, fuera de este repositorio.
- Decisiones posteriores del dueño: `docs/decisions/adenda-al-diseno.md` del plan; mandan sobre el diseño donde difieran.
- Reglas para agentes: [AGENTS.md](AGENTS.md).
