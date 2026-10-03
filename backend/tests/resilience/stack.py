"""El expediente sobre un PostgreSQL propio del escenario (LC-NUC-34).

``ledger_stack(prefix)``: contenedor de PostgreSQL 16 del arnés (``harness.dedicated_postgres``),
base migrada con ``alembic upgrade head``, el entorno del escritor de ``tests/writer_support``
(tipos de prueba sincronizados, lugares dados de alta, bandeja, almacén en memoria) y, aparte,
bases de ``shared.db`` **con los ajustes de producción** de ``vigia-api`` o ``vigia-worker``
(tiempos de espera de NFR-NUC-36 y pools de la adenda A-21) como ``vigia_app``, nunca
superusuario. Los escenarios pausan, reinician o detienen ``LedgerStack.container``.

Solo datos generados.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.resilience.harness import Container, dedicated_postgres
from tests.writer_support import WriterEnvironment, writer_environment
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.registry import RecordType
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.metrics import PlatformMetrics

__all__ = ["LedgerStack", "ledger_stack"]


@dataclass
class LedgerStack:
    container: Container
    endpoint: PostgresEndpoint
    migrated: MigratedDatabase
    env: WriterEnvironment
    databases: list[Database] = field(default_factory=list)

    def run(self, awaitable: Any) -> Any:
        return self.env.loop.run(awaitable)

    def database(self, process: ProcessKind = ProcessKind.API, **changes: Any) -> Database:
        """Un ``shared.db`` nuevo como ``vigia_app`` con los ajustes de producción del proceso."""
        values: dict[str, Any] = {
            "url": self.migrated.as_role("vigia_app").sqlalchemy_url,
            "process": process,
            "sslmode": SslMode.DISABLE,  # el contenedor local no tiene TLS
        }
        values.update(changes)
        database = Database.create(DatabaseSettings(**values))
        self.databases.append(database)
        return database

    def writer(
        self,
        database: Any,
        *,
        storage: Any = None,
        metrics: PlatformMetrics | None = None,
        clock: Clock | None = None,
    ) -> EscritorExpediente:
        """Un escritor con el registro y la bandeja del entorno sobre ``database``.

        Por defecto con el reloj simulado del entorno; ``clock`` (p. ej. el real) cuando el
        escenario mide duraciones del escritor, como ``chain_lock_wait_ms``.
        """
        env = self.env
        assert env.outbox is not None
        chosen = clock if clock is not None else env.clock
        return EscritorExpediente(
            database=database,
            registry=env.registry,
            free_text=FreeTextPolicyRegistry(),
            evidence=EvidenceVerifier(storage if storage is not None else env.storage, chosen),
            outbox=env.outbox,
            clock=chosen,
            metrics=metrics,
        )


@contextlib.contextmanager
def ledger_stack(
    prefix: str,
    *,
    command: Sequence[str] | None = None,
    extra_types: Sequence[RecordType] = (),
    pool_size: int = 6,
) -> Iterator[LedgerStack]:
    with (
        dedicated_postgres(command=command) as (endpoint, container),
        migrated_database(endpoint, prefix) as migrated,
        writer_environment(migrated, extra_types=extra_types, pool_size=pool_size) as env,
    ):
        stack = LedgerStack(container, endpoint, migrated, env)
        try:
            yield stack
        finally:
            status = container.status()
            if status == "paused":
                container.unpause()
            elif status != "running":
                container.start()
                container.wait_ready()
            for database in stack.databases:
                env.loop.run(database.dispose())
