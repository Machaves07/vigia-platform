"""``vigia-api`` real con la ruta de prueba interna de ``node_api`` (TASK-206; NFR-GOB-15).

``python -m tests.node_api_process`` sirve con uvicorn ``create_app`` (cadena fija y arranque
supervisado) con la salud de la plataforma y la unidad de prueba de ``tests/node_api_support.py``,
y una ``NodeApiGate`` real con ``PostgresNodeContextStore`` sobre PostgreSQL como ``vigia_app``.
Firma, KMS y almacén son los dobles de ``tests.worker_support``, que pasan sus comprobaciones.
El reloj es el del sistema: los certificados y las credenciales que siembra la prueba usan
márgenes de días.

Variables: ``VIGIA_TEST_DATABASE_URL``, ``VIGIA_TEST_PROVIDER_ORGANIZATION`` y
``VIGIA_TEST_PORT``. Solo datos generados.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Mapping
from typing import Any

import uvicorn

from tests.authz_support import SYSTEM_ACTOR_ID
from tests.node_api_support import Probe, node_gate, node_unit
from tests.worker_support import StubKms, StubSigning, StubStorage
from vigia_platform.identity.adapters.authz_store import PostgresContextStore
from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.node_api.identity import PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.shared.api.app import AppConfig, AppRuntime, UnitRegistration, create_app
from vigia_platform.shared.api.declarations import NODE_GATE_STATE_KEY
from vigia_platform.shared.api.health import health_router
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.logging import configure_logging
from vigia_platform.shared.ratelimit import RateLimiter

SENTINEL = "health/ready-sentinel"


def build(environ: Mapping[str, str]) -> Any:
    clock = SystemClock()
    provider = uuid.UUID(environ["VIGIA_TEST_PROVIDER_ORGANIZATION"])
    database = Database.create(
        DatabaseSettings(
            url=environ["VIGIA_TEST_DATABASE_URL"],
            process=ProcessKind.API,
            sslmode=SslMode.DISABLE,  # el contenedor local no tiene TLS
        )
    )
    contexts = ScopeContexts(
        store=PostgresContextStore(database),
        clock=clock,
        provider_organization_id=provider,
        system_actor_id=SYSTEM_ACTOR_ID,
    )
    gate = node_gate(
        contexts=contexts,
        store=PostgresNodeContextStore(database),
        clock=clock,
        probe=Probe(),
        limits=NodeRateLimits(RateLimiter(clock)),
    )
    config = AppConfig(
        environment="test",
        data_key_id="alias/vigia-secrets",
        health_sentinel_key=SENTINEL,
        startup_retry_seconds=0.5,
    )
    runtime = AppRuntime(
        clock=clock,
        database=database,
        storage=StubStorage(),
        signing=StubSigning(),
        kms=StubKms(),  # type: ignore[arg-type]  # el arranque solo usa la clave de datos
        state={NODE_GATE_STATE_KEY: gate},
    )
    units = (UnitRegistration("salud", routers=(health_router(),)), node_unit())
    return create_app(config, runtime=runtime, units=units)


def run(environ: Mapping[str, str]) -> None:
    configure_logging()
    uvicorn.run(
        build(environ),
        host="127.0.0.1",
        port=int(environ["VIGIA_TEST_PORT"]),
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    run(os.environ)
    sys.exit(0)
