# vigia-platform

Plataforma núcleo de Vigía (U-02 a U-05): backend FastAPI + PostgreSQL, aplicación de página única y despliegue en AWS con CDK. Este repositorio nace con TASK-101 (VIG-18): el backend en Python 3.12, sus herramientas de calidad y el árbol de módulos vacío. La base de datos, el entorno local con Docker, la infraestructura y el frontend llegan en tareas posteriores.

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

`uv sync --frozen` instala Python 3.12 si hace falta (`.python-version`) y resuelve `vigia-contracts` al commit fijado en `uv.lock`. Las pruebas de integración (`-m integration`) necesitan Docker en ejecución; todavía no hay ninguna.

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

Todavía no existen: las define TASK-103 (entorno local) y las tareas de `vigia-admin`. Ningún secreto, clave ni `.env` entra al repositorio.

## Documentación de referencia

- Diseño aprobado (fuente del *qué*): `aidlc-docs/construction/plataforma-nucleo/` del espacio de trabajo de Vigía, fuera de este repositorio.
- Decisiones posteriores del dueño: `docs/decisions/adenda-al-diseno.md` del plan; mandan sobre el diseño donde difieran.
- Reglas para agentes: [AGENTS.md](AGENTS.md).
