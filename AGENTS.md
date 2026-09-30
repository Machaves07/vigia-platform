# AGENTS.md — vigia-platform

Contexto persistente para agentes que trabajan en este repositorio (U-02 a U-05 del proyecto Vigía). Este archivo es el punto de entrada; las reglas completas del proyecto están en el `AGENTS.md` del workspace.

## Lectura obligatoria antes de tocar código

1. `AGENTS.md` del workspace (reglas del proyecto, principios P1–P10, invariantes, convenciones, comandos estándar):
   (el orquestador lo copia como `../CLAUDE.md` junto al workspace; su ruta original está en el prompt de la tarea)
2. El issue de Linear asignado y su archivo local `docs/tasks/NNN-*.md` (Scope, Deliverables, Acceptance Criteria, Test Plan, Context). Si difieren, manda el archivo local.
3. `docs/decisions/adenda-al-diseno.md` del plan: manda sobre `aidlc-docs` donde difieran.
4. Solo los archivos que la sección *Context* del issue cita.

## Traducción de rutas (las tareas se escribieron para Windows)

| Ruta en la tarea | Úsala así |
|---|---|
| `<carpeta del plan>\vigia-platform` (ruta de Windows que citan las tareas) | la raíz de este repositorio; **aquí** se escribe el código |
| `...\vigia-platform\backend` | `backend/` |
| `<carpeta del plan>\<resto>` | la misma ruta vista desde WSL (`/mnt/c/...`, con `/`) — **solo lectura**; la traducción exacta está en el prompt de la tarea |
| `cd "C:\...\vigia-platform\backend" && <cmd>` | ejecuta `<cmd>` en `backend/` |

Nunca escribas fuera del workspace del issue. Las carpetas `registros/` del workspace contienen **datos personales**: no copiar a este repositorio, a la canalización ni a un prompt. Las pruebas usan solo datos sintéticos.

## Stack y comandos

Python 3.12 única versión (`requires-python = ">=3.12,<3.13"`) con `uv` (usa `uv run`, nunca `pip` global), FastAPI asíncrono, Pydantic 2 estricto, SQLAlchemy 2 asíncrono con asyncpg, Alembic, boto3, `ruff`, `mypy --strict` con el complemento de Pydantic, pytest + pytest-asyncio + Hypothesis (perfiles `ci` y `nightly`). Los comandos estándar los establece TASK-101 (VIG-18) y todos corren desde `backend/`:

```text
cd backend && uv sync --frozen
cd backend && uv run ruff check . && uv run ruff format --check .
cd backend && uv run python tools/lint_rules.py                  # VIG001–VIG003, donde ruff no alcanza
cd backend && uv run mypy --strict src
cd backend && uv run pytest -q --hypothesis-profile=ci          # nocturno: --hypothesis-profile=nightly
cd backend && uv run python -m vigia_platform.shared.api.export_openapi --check   # desde TASK-133
cd infra   && uv sync --frozen && uv run pytest -q && npx aws-cdk synth --quiet   # desde TASK-144
```

Las pruebas de integración (`-m integration`, testcontainers con PostgreSQL 16 y LocalStack) necesitan Docker en ejecución. Las pruebas `nightly` solo corren con `--hypothesis-profile=nightly`.

## Estructura

- `backend/src/vigia_platform/`: módulos `identity`, `ledger` y `shared` con puertos y adaptadores (`tech-stack-decisions.md` §7). `domain/` nunca importa FastAPI, SQLAlchemy ni boto3.
- Módulos críticos aislados (NFR-NUC-25): `identity.auth`, `identity.authz`, `ledger.chain`, `shared.signing` y `shared.crypto` no importan FastAPI ni SQLAlchemy; `tools/check_isolated_imports.py` lo verifica en cada corrida de pytest.
- `shared.clock` es el único paquete que lee la hora del sistema; el resto recibe un `Clock`.
- Dependencia de `vigia-contracts` por `git+ssh` con `#subdirectory=generated/python` (ADR-004), fijada por hash de commit en `uv.lock` durante el desarrollo; TASK-153 adopta la etiqueta `v1.0.0`. Una dependencia nueva se declara en `backend/pyproject.toml`, se bloquea con `uv lock` y se documenta en el PR con versión y licencia (MIT, BSD, Apache-2.0, ISC, PSF o MPL-2.0).
- `backend/tests/conftest.py` define los perfiles de Hypothesis y las semillas; las propiedades no fijan `max_examples` ni semillas propias.

## Reglas de lint bloqueantes

- ruff: `pickle`, `marshal`, `yaml.load` inseguro y `eval` (S301, S302, S307, S506, TID251); `datetime` sin zona (DTZ); hora del sistema fuera de `shared.clock` (TID251).
- `tools/lint_rules.py`: `VIG001` `text()` con f-string o formato (NFR-NUC-19); `VIG002` httpx sin `timeout=`; `VIG003` boto3 sin `config=` con tiempos de espera (PAT-NUC-RES-03). Corre en `tests/unit/test_lint_rules.py`.

## Invariantes que no se negocian

- Toda transacción de datos se abre con un `ScopeContext` (seguridad a nivel de fila forzada por `SET LOCAL`); el único camino de escritura del expediente es `EscritorExpediente`, y el disparador de la base calcula los hashes; un recurso fuera de alcance responde `not_found`, nunca `forbidden`; toda ruta declara su clave de permiso o no arranca; las migraciones solo van hacia adelante.
- P4: el registro no se borra. P3: nada identifica a una persona observada.
- Ningún secreto, clave privada ni código de alta en el árbol; el escaneo de secretos es bloqueante.

## PR

Título con `VIG-<n>`, rama la que da Linear, PR contra `main` con la tabla criterio → prueba y la salida real de los comandos del Test Plan en la sección *Evidence*. Sin merge propio. Lo que el review pida fuera de alcance → cítalo en la tarea a la que pertenece, no amplíes la tuya.

## Review guidelines

- Primero: errores de corrección, regresiones, riesgos de seguridad y pruebas faltantes; sin comentarios de estilo que ya aplican ruff/formatter.
- Un cambio de comportamiento sin evidencia de prueba está incompleto.
- Violación de un principio P1–P10 o de un invariante de la plataforma = bloqueante.
