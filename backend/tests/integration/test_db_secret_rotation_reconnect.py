"""Rotación de la contraseña de ``vigia_app`` sin reiniciar el proceso (VIG-137; runbook 6.6).

La raíz de composición abre la base con la credencial del secreto de ``VIGIA_DB_APP_SECRET``
(``shared.runtime.core``) y el pool relee el secreto cuando PostgreSQL rechaza la autenticación.
Contra PostgreSQL 16 y el Secrets Manager de LocalStack, con el mismo ``load_credentials`` y
``open_database`` que usan los constructores:

- **La transacción siguiente reconecta.** Se rota la contraseña en la base (``ALTER ROLE``) y en el
  secreto (``PutSecretValue``), y se cortan las sesiones abiertas (como tras un reinicio de la
  instancia o un fallo de red: PostgreSQL no corta las autenticadas al rotar). La transacción
  siguiente reconecta con la contraseña nueva sin reiniciar nada, el secreto se leyó una vez más y
  ``db_pool_reconnects_total`` sube.
- **Una sola relectura por ráfaga (garantía concurrente).** Con 20 conexiones cortadas y 20
  transacciones lanzadas a la vez (``asyncio.gather``) tras la rotación, las 20 terminan bien y el
  secreto se relee **una** vez. El lector de la prueba tarda 0,5 s en devolver el secreto para que
  las 20 aperturas rechazadas pidan la relectura mientras la primera sigue en curso: sin la
  exclusión de ``DatabaseCredentials.renew`` habría hasta 20 lecturas.

Solo datos generados: contraseñas aleatorias de la prueba.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from sqlalchemy import text

from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
    PostgresEndpoint,
)
from tests.writer_support import unit_context
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import Database, ProcessKind
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.runtime.core import aws_settings, load_credentials, open_database
from vigia_platform.shared.runtime.db_credentials import AwsSecretStringReader, SecretStringReader

pytestmark = pytest.mark.integration

BURST: Final = 20
READ_DELAY_SECONDS: Final = 0.5
"""Lo que tarda el lector de la prueba en devolver el secreto: abre la ventana en la que las 20
aperturas rechazadas piden la relectura. No decide nada por tiempo: la aserción cuenta lecturas."""
_PROBE: Final = text("SELECT 1")


@dataclass
class CountingReader:
    """El lector real de la raíz, contando las lecturas del secreto."""

    inner: SecretStringReader
    delay_seconds: float = 0.0
    reads: int = 0

    async def read(self, secret_id: str) -> str:
        self.reads += 1
        value = await self.inner.read(secret_id)
        await asyncio.sleep(self.delay_seconds)
        return value


@dataclass
class Stack:
    migrated: MigratedDatabase
    localstack: LocalStackEndpoint
    secret_name: str

    def runtime(self) -> RuntimeConfig:
        return RuntimeConfig.from_environ(
            {
                "VIGIA_ENVIRONMENT": "test",
                "AWS_REGION": self.localstack.region,
                "VIGIA_AWS_ENDPOINT_URL": self.localstack.url,
                "VIGIA_DB_APP_SECRET": self.secret_name,
                "VIGIA_SIGNING_SECRET_PREFIX": "vigia/rotacion/signing/",
                "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
                "PGSSLMODE": "disable",
            }
        )

    def secret_document(self, password: str) -> str:
        endpoint = self.migrated.endpoint
        return json.dumps(
            {
                "engine": "postgres",
                "host": endpoint.host,
                "port": endpoint.port,
                "dbname": self.migrated.database,
                "username": "vigia_app",
                "password": password,
            }
        )

    async def superuser(self, sql: str, *args: Any) -> list[Any]:
        connection = await self.migrated.connect()
        try:
            return list(await connection.fetch(sql, *args))
        finally:
            await connection.close()

    async def set_password(self, password: str) -> None:
        # ``ALTER ROLE`` no admite parámetros; la contraseña es un token URL-safe generado.
        assert password.replace("-", "").replace("_", "").isalnum()
        await self.superuser(f"ALTER ROLE vigia_app PASSWORD '{password}'")

    async def rotate(self) -> None:
        """Contraseña nueva en la base y en el secreto, y sesiones de ``vigia_app`` cortadas."""
        password = secrets.token_urlsafe(24)
        await self.set_password(password)
        self.localstack.aws_client("secretsmanager").put_secret_value(
            SecretId=self.secret_name, SecretString=self.secret_document(password)
        )
        await self.superuser(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE usename = 'vigia_app' AND datname = $1",
            self.migrated.database,
        )


@pytest.fixture
def stack(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Stack]:
    # El lector de la raíz usa la cadena de credenciales de boto3 (en AWS, el rol de la tarea).
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", LOCALSTACK_ACCESS_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", LOCALSTACK_SECRET_ACCESS_KEY)
    client = localstack_endpoint.aws_client("secretsmanager")
    with migrated_database(postgres_endpoint, "rotation") as migrated:
        name = f"vigia/rotacion-{uuid.uuid4().hex[:8]}/db/app"
        built = Stack(migrated, localstack_endpoint, name)
        client.create_secret(Name=name, SecretString=built.secret_document(migrated.app_password))
        try:
            yield built
        finally:
            # Los roles son del clúster: la siguiente prueba espera la contraseña de su base.
            asyncio.run(built.set_password(migrated.app_password))
            client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


def _metrics() -> tuple[PlatformMetrics, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return PlatformMetrics(provider.get_meter("pruebas")), reader


def _reconnects(reader: InMemoryMetricReader) -> int:
    data = reader.get_metrics_data()
    total = 0
    if data is None:
        return total
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == "db_pool_reconnects_total":
                    total += sum(
                        int(point.value)
                        for point in metric.data.data_points
                        if isinstance(point, NumberDataPoint)
                    )
    return total


async def _probe(database: Database) -> int:
    context = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
    async with database.transaction(context) as transaction:
        value: int = (await transaction.execute(_PROBE)).scalar_one()
        return value


async def _hold_connections(database: Database, count: int) -> None:
    """Abre ``count`` conexiones a la vez: cada transacción espera a las demás antes de cerrar."""
    barrier = asyncio.Barrier(count)

    async def hold() -> None:
        context = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
        async with database.transaction(context) as transaction:
            await transaction.execute(_PROBE)
            async with asyncio.timeout(60):
                await barrier.wait()

    await asyncio.gather(*(hold() for _ in range(count)))


def test_the_next_transaction_reconnects_with_the_rotated_password(stack: Stack) -> None:
    runtime = stack.runtime()

    async def scenario() -> tuple[int, int, int]:
        metrics, reader = _metrics()
        counting = CountingReader(AwsSecretStringReader(aws_settings(runtime)))
        credentials = await load_credentials(runtime, counting)
        database = open_database(runtime, ProcessKind.WORKER, credentials, metrics)
        try:
            assert await _probe(database) == 1
            reads_before = counting.reads
            await stack.rotate()
            assert await _probe(database) == 1  # el mismo proceso, sin reiniciar
            return reads_before, counting.reads, _reconnects(reader)
        finally:
            await database.dispose()

    reads_before, reads_after, reconnects = asyncio.run(scenario())

    assert reads_before == 1  # la lectura al construir
    assert reads_after == 2  # una relectura tras el rechazo
    assert reconnects == 1


def test_a_burst_of_rejections_rereads_the_secret_once(stack: Stack) -> None:
    runtime = stack.runtime()

    async def scenario() -> tuple[list[int], int, int]:
        metrics, reader = _metrics()
        counting = CountingReader(AwsSecretStringReader(aws_settings(runtime)))
        credentials = await load_credentials(runtime, counting)
        database = open_database(
            runtime, ProcessKind.WORKER, credentials, metrics, worker_pool_size=BURST
        )
        try:
            await _hold_connections(database, BURST)  # 20 conexiones en el pool
            await stack.rotate()  # las 20 cortadas: cada una reconecta con la credencial vieja
            counting.reads = 0
            counting.delay_seconds = READ_DELAY_SECONDS
            results = await asyncio.gather(*(_probe(database) for _ in range(BURST)))
            return list(results), counting.reads, _reconnects(reader)
        finally:
            await database.dispose()

    results, reads, reconnects = asyncio.run(scenario())

    assert results == [1] * BURST
    assert reads == 1
    assert reconnects >= 1
