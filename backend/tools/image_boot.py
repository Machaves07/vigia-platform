"""Arranque de ``vigia-api`` de una imagen contra una base (TASK-143; NFR-NUC-14, RESILIENCY-04).

Corre **dentro** de la imagen que se prueba, montado de solo lectura (no forma parte de la
imagen)::

    docker run --read-only --tmpfs /tmp -v tools/image_boot.py:/boot/image_boot.py:ro \\
        -e VIGIA_BOOT_DATABASE_URL=postgresql+asyncpg://vigia_app:…@db:5432/vigia \\
        <imagen> python /boot/image_boot.py

Usa solo el código de la imagen: ``shared.api.main.serve`` (el mismo servidor que la orden
``vigia-api``), ``create_app`` y sus comprobaciones de arranque, con el
``MINIMUM_SCHEMA_VERSION`` de esa versión. Así ``tools/run_boot_check.py`` prueba que la imagen
anterior (N-1) arranca contra el esquema que dejan las migraciones nuevas (N):

- **Base de verdad** como ``vigia_app``: versión mínima del esquema y seguridad a nivel de fila
  en vigor (la comprobación ``database`` de PAT-NUC-RES-02).
- **Registros de verdad**: el catálogo de la bandeja de U-02 (tipos de evento y consumidores) se
  sincroniza contra sus tablas, como en el arranque de producción.
- **Firma, KMS y almacén**: dobles que pasan sus comprobaciones. No dependen del esquema y su
  arranque real lo prueba ``tests/resilience`` contra LocalStack.

Variables: ``VIGIA_BOOT_DATABASE_URL`` (obligatoria), ``VIGIA_BOOT_DEADLINE_SECONDS`` (60 por
defecto) y las de ``ApiServerConfig`` (``VIGIA_API_PORT``…). Sale como ``vigia-api``: 0 tras una
parada ordenada y ``STARTUP_FAILURE_EXIT_CODE`` si no llega a ``ready``. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from vigia_platform.identity.authz.context import ScopeContexts
from vigia_platform.shared.api.app import (
    STARTUP_FAILURE_EXIT_CODE,
    AppConfig,
    AppRuntime,
)
from vigia_platform.shared.api.main import ApiServerConfig, serve
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.alerts_consumer import register_alerts_consumer
from vigia_platform.shared.observability.logging import configure_logging, get_logger
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.secrets import DataKey
from vigia_platform.shared.signing.keys import SigningPurpose
from vigia_platform.shared.storage import ObjectHead

DATABASE_VARIABLE: Final = "VIGIA_BOOT_DATABASE_URL"
DEADLINE_VARIABLE: Final = "VIGIA_BOOT_DEADLINE_SECONDS"

_log = get_logger("tools.image_boot")


class _Signing:
    """Firma con clave activa para cada propósito: no depende del esquema."""

    ready = True

    async def start(self) -> None:
        return None

    def has_active_key(self, purpose: SigningPurpose) -> bool:
        return True

    async def run_refresh(self, stop: asyncio.Event) -> None:
        await stop.wait()


class _Kms:
    async def generate_data_key(self, key_id: str, *, context: Mapping[str, str]) -> DataKey:
        return DataKey(plaintext=b"k" * 32, wrapped=b"w" * 32, key_id=key_id)

    async def decrypt(self, wrapped: bytes, *, key_id: str, context: Mapping[str, str]) -> bytes:
        return b"k" * 32

    async def sign(self, key_id: str, message: bytes) -> bytes:
        raise RuntimeError("el arranque de prueba no firma con KMS")

    async def get_public_key(self, key_id: str) -> bytes:
        raise RuntimeError("el arranque de prueba no lee claves de KMS")


class _Storage:
    async def head_object(self, key: str) -> ObjectHead:
        return ObjectHead(
            key=key,
            size_bytes=0,
            checksum_sha256=None,
            checksum_type=None,
            content_type=None,
            metadata={},
            version_id=None,
        )


class _NoContextStore:
    """El contexto de auditoría del proveedor no lee la base."""

    async def session_row(self, *_: Any) -> None:
        raise RuntimeError("el arranque no construye contextos de sesión")

    async def operator_row(self, *_: Any) -> None:
        raise RuntimeError("el arranque no construye contextos de operador")


def _builder(url: str) -> Callable[[AppConfig], Awaitable[AppRuntime]]:
    async def build(config: AppConfig) -> AppRuntime:
        clock = SystemClock()
        database = Database.create(
            DatabaseSettings(url=url, process=ProcessKind.API, sslmode=SslMode.DISABLE)
        )
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        register_alerts_consumer(catalog.consumers)
        contexts = ScopeContexts(
            store=_NoContextStore(),
            clock=clock,
            provider_organization_id=uuid.uuid4(),
            system_actor_id=uuid.uuid4(),
        )

        async def synchronize_outbox() -> None:
            async with database.transaction(contexts.provider_audit_context()) as transaction:
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)

        return AppRuntime(
            clock=clock,
            database=database,
            storage=_Storage(),
            signing=_Signing(),
            kms=_Kms(),
            registries=(synchronize_outbox,),
        )

    return build


def main() -> int:
    configure_logging()
    try:
        url = os.environ[DATABASE_VARIABLE]
        deadline = float(os.environ.get(DEADLINE_VARIABLE, "60"))
        config = AppConfig(
            environment="test",
            data_key_id="alias/vigia-boot-check",
            startup_deadline_seconds=deadline,
            startup_retry_seconds=2.0,
        )
        server = ApiServerConfig.from_environ(os.environ)
    except Exception:
        _log.exception("configuración del arranque de prueba no válida")
        return STARTUP_FAILURE_EXIT_CODE
    return asyncio.run(serve(config, server, _builder(url)))


if __name__ == "__main__":
    sys.exit(main())
