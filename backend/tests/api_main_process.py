"""``vigia-api`` en un proceso propio con dobles (``tests/unit/test_api_main.py``, TASK-143).

``python -m tests.api_main_process`` sirve con ``shared.api.main.serve`` (el servidor de la orden
``vigia-api``, con su manejo de señales) una aplicación cuyas comprobaciones de arranque pasan:
base con la versión mínima del esquema y firma, KMS y almacén de ``tests.worker_support``. Escucha
en ``127.0.0.1:VIGIA_API_PORT``. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import os
import sys

from tests.worker_support import StubKms, StubSigning, StubStorage
from vigia_platform.shared.api.app import AppConfig, AppRuntime
from vigia_platform.shared.api.main import ApiServerConfig, serve
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import DatabaseHealth
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION


class _Database:
    async def health(self, *, timeout_seconds: float) -> DatabaseHealth:
        return DatabaseHealth(visible_organizations=0, schema_version=MINIMUM_SCHEMA_VERSION)


async def _build(config: AppConfig) -> AppRuntime:
    return AppRuntime(
        clock=SystemClock(),
        database=_Database(),
        storage=StubStorage(),
        signing=StubSigning(),
        kms=StubKms(),  # type: ignore[arg-type]  # el arranque solo usa la clave de datos
    )


def main() -> int:
    config = AppConfig(environment="test", data_key_id="alias/vigia-secrets")
    server = ApiServerConfig(host="127.0.0.1", port=int(os.environ["VIGIA_API_PORT"]))
    return asyncio.run(serve(config, server, _build))


if __name__ == "__main__":
    sys.exit(main())
