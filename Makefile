# Objetivos del entorno local de vigia-platform (TASK-103, LC-NUC-36, PAT-NUC-MAN-07).
#
#   make up        levanta PostgreSQL 16, LocalStack y el colector y espera a que estén sanos
#   make down      los detiene y borra sus volúmenes
#   make ps        estado de los servicios
#   make test      suite completa del backend (PROFILE=ci por defecto; PROFILE=nightly)
#   make run       API (vigia-api) contra el entorno local           — llega con TASK-133
#   make worker    worker (vigia-worker) contra el entorno local     — llega con TASK-130
#   make migrate   alembic upgrade head contra PostgreSQL local
#   make admin     orden administrativa: make admin ARGS="--help"    — llega con TASK-132
#
# En Windows sin `make`, el README tiene los mismos comandos con `uv run`.
# Las pruebas de integración usan testcontainers; con `VIGIA_TEST_USE_COMPOSE=1` usan el entorno
# de `make up` (ver backend/tests/integration/conftest.py).

SHELL := /bin/sh
.DEFAULT_GOAL := help

BACKEND := backend
PROFILE ?= ci
ARGS ?=

# Configuración de run, worker, migrate y admin contra `docker-compose.yml`. Son los nombres
# estándar que ya leen las bibliotecas: libpq y asyncpg (PG*), boto3 (AWS_*) y el SDK de
# OpenTelemetry (OTEL_*). No son secretos: valores fijos del entorno local. `make test` no los
# exporta: las pruebas eligen su entorno con sus fixtures.
LOCAL_ENV = \
	PGHOST=127.0.0.1 \
	PGPORT=$${VIGIA_LOCAL_POSTGRES_PORT:-5432} \
	PGUSER=vigia \
	PGPASSWORD=vigia_local \
	PGDATABASE=vigia \
	AWS_ENDPOINT_URL=http://127.0.0.1:$${VIGIA_LOCAL_LOCALSTACK_PORT:-4566} \
	AWS_DEFAULT_REGION=us-east-1 \
	AWS_ACCESS_KEY_ID=test \
	AWS_SECRET_ACCESS_KEY=test \
	OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:$${VIGIA_LOCAL_OTLP_GRPC_PORT:-4317} \
	OTEL_SERVICE_NAME=$(1)

# Contraseñas locales de los roles vigia_app y vigia_migrate, que crea la migración nuc_0001
# (en AWS llegan de los secretos db/app y db/migrate). Valores fijos del entorno local.
MIGRATE_ENV = \
	VIGIA_DB_APP_PASSWORD=vigia_app_local_only \
	VIGIA_DB_MIGRATE_PASSWORD=vigia_migrate_local_only

# $(call require,<archivo relativo a backend>,<tarea que lo crea>)
define require
	@test -e "$(BACKEND)/$(1)" || { \
		echo "Todavía no existe $(BACKEND)/$(1): este objetivo queda operativo con $(2)."; \
		exit 2; }
endef

.PHONY: help up down ps test run worker migrate admin

help:
	@sed -n 's/^#   make /make /p' Makefile

up:
	docker compose up -d --wait

down:
	docker compose down -v

ps:
	docker compose ps

test:
	cd $(BACKEND) && uv run pytest -q --hypothesis-profile=$(PROFILE)

run:
	$(call require,src/vigia_platform/shared/api/app.py,TASK-133)
	cd $(BACKEND) && $(call LOCAL_ENV,vigia-api) uv run uvicorn \
		vigia_platform.shared.api.app:create_app --factory --host 127.0.0.1 --port 8000

worker:
	$(call require,src/vigia_platform/shared/worker/main.py,TASK-130)
	cd $(BACKEND) && $(call LOCAL_ENV,vigia-worker) uv run python -m vigia_platform.shared.worker.main

migrate:
	$(call require,alembic.ini,TASK-106)
	cd $(BACKEND) && $(call LOCAL_ENV,vigia-migrate) $(MIGRATE_ENV) uv run alembic upgrade head

admin:
	$(call require,src/vigia_platform/identity/application/admin_cli.py,TASK-132)
	cd $(BACKEND) && $(call LOCAL_ENV,vigia-admin) uv run vigia-admin $(ARGS)
