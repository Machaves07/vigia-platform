"""Salud de ``vigia-api`` con PostgreSQL 16 y LocalStack reales (TASK-133; NFR-NUC-13, FS-NUC-05).

Criterio de aceptación: con PostgreSQL, LocalStack (el almacén) o el gestor de secretos apagados,
``/health/ready`` falla en **menos de 2 s** y ``/health/live`` responde 200.

La aplicación es la de ``create_app`` con sus adaptadores de verdad (``Database`` como
``vigia_app``, ``S3Storage``, ``SecretsManagerAdapter``, ``KmsAdapter`` y ``SigningService`` con
el material en el gestor). Entre cada adaptador y su servicio hay un ``FaultProxy`` que, en
caliente, deja pasar, rechaza (servicio caído) o congela (servicio que no responde) las
conexiones, también las que el pool ya tenía abiertas.

- Todo arriba: ``ready`` y ``live`` 200.
- Tras arrancar, base o almacén caídos o congelados: ``ready`` 503 en menos de 2 s y ``live`` 200;
  al volver la dependencia, ``ready`` vuelve a 200.
- Gestor de secretos o KMS caídos o congelados al arrancar: el proceso nunca queda listo
  (``ready`` 503 en menos de 2 s, ``live`` 200) y termina con código 3 al vencer el plazo.
- Gestor de secretos caído en operación (FS-NUC-05 b): ``ready`` sigue en 200 (las claves están
  en memoria) y el refresco periódico relee el gestor y suma ``secrets_refresh_failed``.
- Rol con la seguridad a nivel de fila sin efecto (superusuario): la base ve filas de una
  organización inexistente y el proceso no arranca.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from vigia_contracts.clock import SimulatedClock, SystemClock

from tests.fault_proxy import FaultProxy, ProxyMode, fault_proxy
from tests.hibp_service import metric_total, metrics_with_reader
from tests.identity_db import MigratedDatabase, seeded_identity
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
    PostgresEndpoint,
    versioned_bucket,
)
from tests.signing_support import (
    BOOTSTRAP_ORDER,
    PROVIDER_ORGANIZATION_ID,
    START,
    InMemoryKeyStore,
    RecordingEvents,
    provider_context,
)
from vigia_platform.shared.api.app import (
    STARTUP_FAILURE_EXIT_CODE,
    AppConfig,
    AppRuntime,
    StartupSupervisor,
    create_app,
)
from vigia_platform.shared.api.health import READINESS_BUDGET_SECONDS
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.observability.redaction import AttributePolicy
from vigia_platform.shared.secrets import (
    AwsCredentials,
    AwsSettings,
    KmsAdapter,
    SecretsManagerAdapter,
)
from vigia_platform.shared.signing import SigningService
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

WALL_CLOCK = SystemClock()
"""Reloj real: estas pruebas miden tiempo de verdad a propósito."""
SENTINEL = "health/ready-sentinel"
STARTUP_WAIT_SECONDS = 30.0
DEADLINE_SECONDS = 3.0
"""Plazo de arranque de estas pruebas (60 s en producción), para no esperar un minuto."""


@dataclass(frozen=True)
class Backends:
    database: MigratedDatabase
    localstack: LocalStackEndpoint
    bucket: str
    data_key_id: str
    signing_store: InMemoryKeyStore
    environment: str


def _aws(url: str) -> AwsSettings:
    return AwsSettings(
        region="us-east-1",
        endpoint_url=url,
        credentials=AwsCredentials(LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY),
    )


async def _bootstrap_keys(url: str, environment: str) -> InMemoryKeyStore:
    """Alta de las cinco claves de firma con el material en el Secrets Manager de LocalStack."""
    clock = SimulatedClock(START)
    store = InMemoryKeyStore()
    bootstrap = SigningService(
        provider_organization_id=PROVIDER_ORGANIZATION_ID,
        store=store,
        secrets=SecretsManagerAdapter(_aws(url), clock),
        events=RecordingEvents(),
        clock=clock,
        environment=environment,
    )
    await bootstrap.start(required=())
    for purpose in BOOTSTRAP_ORDER:
        await bootstrap.rotate(purpose, context=provider_context())
    return store


@pytest.fixture(scope="module")
def backends(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[Backends]:
    s3 = localstack_endpoint.aws_client("s3")
    kms = localstack_endpoint.aws_client("kms")
    environment = f"health-{uuid.uuid4().hex[:8]}"
    with (
        seeded_identity(postgres_endpoint, "vigia_health") as (database, _),
        versioned_bucket(s3, "vigia-health") as bucket,
    ):
        s3.put_object(Bucket=bucket, Key=SENTINEL, Body=b"centinela sintetico")
        key_id = kms.create_key(Description="vigia-secrets de prueba")["KeyMetadata"]["KeyId"]
        store = asyncio.run(_bootstrap_keys(localstack_endpoint.url, environment))
        yield Backends(database, localstack_endpoint, bucket, key_id, store, environment)


@dataclass
class Stack:
    """Los cuatro intermediarios y la aplicación sobre ellos."""

    postgres: FaultProxy
    storage: FaultProxy
    secrets: FaultProxy
    kms: FaultProxy
    backends: Backends
    exits: list[int] = dataclasses.field(default_factory=list)
    metrics: Any = None
    reader: Any = None
    signing: SigningService | None = None
    database: Database | None = None
    signing_clock: SimulatedClock = dataclasses.field(default_factory=lambda: SimulatedClock(START))

    def app(self, *, role: str | None = "vigia_app") -> Any:
        backends = self.backends
        endpoint = backends.database.as_role(role)
        proxied = PostgresEndpoint(
            "127.0.0.1", self.postgres.port, endpoint.user, endpoint.password, endpoint.database
        )
        self.database = Database.create(
            DatabaseSettings(
                url=proxied.sqlalchemy_url, process=ProcessKind.API, sslmode=SslMode.DISABLE
            )
        )
        storage_settings = dataclasses.replace(
            backends.localstack.storage_settings(backends.bucket), endpoint_url=self.storage.url
        )
        signing_clock = self.signing_clock
        self.metrics, self.reader = metrics_with_reader()
        self.signing = SigningService(
            provider_organization_id=PROVIDER_ORGANIZATION_ID,
            store=backends.signing_store,
            secrets=SecretsManagerAdapter(
                _aws(self.secrets.url), signing_clock, metrics=self.metrics
            ),
            events=RecordingEvents(),
            clock=signing_clock,
            environment=backends.environment,
            metrics=self.metrics,
        )
        config = AppConfig(
            environment="test",
            data_key_id=backends.data_key_id,
            health_sentinel_key=SENTINEL,
            startup_deadline_seconds=DEADLINE_SECONDS,
            startup_retry_seconds=0.5,
        )
        runtime = AppRuntime(
            clock=SystemClock(),
            database=self.database,
            storage=S3Storage(storage_settings, SystemClock()),
            signing=self.signing,
            kms=KmsAdapter(_aws(self.kms.url)),
            on_startup_failure=self.exits.append,
            attribute_policy=AttributePolicy(),
        )
        return create_app(config, runtime=runtime)


@pytest.fixture
def stack(backends: Backends) -> Iterator[Stack]:
    postgres = backends.database.as_role()
    localstack_port = int(backends.localstack.url.rsplit(":", 1)[1])
    localstack_host = backends.localstack.url.split("//", 1)[1].rsplit(":", 1)[0]
    with (
        fault_proxy(postgres.host, postgres.port) as pg,
        fault_proxy(localstack_host, localstack_port) as storage,
        fault_proxy(localstack_host, localstack_port) as secrets,
        fault_proxy(localstack_host, localstack_port) as kms,
    ):
        yield Stack(pg, storage, secrets, kms, backends)


def _supervisor(app: Any) -> StartupSupervisor:
    supervisor = app.state.vigia_readiness
    assert isinstance(supervisor, StartupSupervisor)
    return supervisor


def _wait_until(predicate: Any, seconds: float = STARTUP_WAIT_SECONDS) -> None:
    deadline = WALL_CLOCK.monotonic() + seconds
    while not predicate():
        assert WALL_CLOCK.monotonic() < deadline, "la condición no se cumplió a tiempo"
        time.sleep(0.05)


def _timed(client: TestClient, path: str) -> tuple[Any, float]:
    start = WALL_CLOCK.monotonic()
    response = client.get(path)
    return response, WALL_CLOCK.monotonic() - start


def _assert_not_ready_fast_and_live(client: TestClient) -> None:
    ready, elapsed = _timed(client, "/health/ready")
    assert ready.status_code == 503, ready.text
    assert ready.json()["code"] == "temporarily_unavailable"
    assert elapsed < READINESS_BUDGET_SECONDS, f"ready tardó {elapsed:.2f} s"
    live, live_elapsed = _timed(client, "/health/live")
    assert live.status_code == 200 and live.json() == {"status": "live"}
    assert live_elapsed < READINESS_BUDGET_SECONDS


def _close(client: TestClient, stack: Stack) -> None:
    if stack.database is not None:
        for proxy in (stack.postgres,):
            proxy.set_mode(ProxyMode.FORWARD)
        assert client.portal is not None
        client.portal.call(stack.database.dispose)


# --- Todo arriba -------------------------------------------------------------------------------


def test_everything_up_ready_and_live_answer_200(stack: Stack) -> None:
    app = stack.app()
    with TestClient(app) as client:
        _wait_until(lambda: _supervisor(app).started)
        ready, elapsed = _timed(client, "/health/ready")
        assert ready.status_code == 200 and ready.json() == {"status": "ready"}
        assert elapsed < READINESS_BUDGET_SECONDS
        assert client.get("/health/live").status_code == 200
        _close(client, stack)
    assert stack.exits == []


# --- Dependencias que caen tras arrancar ------------------------------------------------------


@pytest.mark.parametrize("dependency", ["postgres", "storage"])
@pytest.mark.parametrize("mode", [ProxyMode.REFUSE, ProxyMode.FREEZE])
def test_after_startup_a_down_dependency_fails_ready_within_2s_and_live_answers(
    stack: Stack, dependency: str, mode: ProxyMode
) -> None:
    app = stack.app()
    proxy: FaultProxy = getattr(stack, dependency)
    with TestClient(app) as client:
        _wait_until(lambda: _supervisor(app).started)
        assert client.get("/health/ready").status_code == 200
        proxy.set_mode(mode)
        for _ in range(3):  # sondas repetidas del balanceador: ninguna se pasa del tope
            _assert_not_ready_fast_and_live(client)
        proxy.set_mode(ProxyMode.FORWARD)
        _wait_until(lambda: client.get("/health/ready").status_code == 200, seconds=20.0)
        _close(client, stack)
    assert stack.exits == []


def test_after_startup_all_of_localstack_down_fails_ready_within_2s(stack: Stack) -> None:
    app = stack.app()
    with TestClient(app) as client:
        _wait_until(lambda: _supervisor(app).started)
        for proxy in (stack.storage, stack.secrets, stack.kms):
            proxy.set_mode(ProxyMode.FREEZE)
        _assert_not_ready_fast_and_live(client)
        for proxy in (stack.storage, stack.secrets, stack.kms):
            proxy.set_mode(ProxyMode.FORWARD)
        _close(client, stack)


# --- Gestor de secretos, KMS y base apagados al arrancar --------------------------------------


@pytest.mark.parametrize("dependency", ["secrets", "kms", "postgres"])
@pytest.mark.parametrize("mode", [ProxyMode.REFUSE, ProxyMode.FREEZE])
def test_a_dependency_down_at_startup_never_becomes_ready_and_exits(
    stack: Stack, dependency: str, mode: ProxyMode
) -> None:
    proxy: FaultProxy = getattr(stack, dependency)
    proxy.set_mode(mode)
    app = stack.app()
    with TestClient(app) as client:
        _assert_not_ready_fast_and_live(client)
        _wait_until(lambda: stack.exits != [])
        assert stack.exits == [STARTUP_FAILURE_EXIT_CODE]
        assert not _supervisor(app).started
        _assert_not_ready_fast_and_live(client)
        proxy.set_mode(ProxyMode.FORWARD)
        _close(client, stack)


# --- Gestor de secretos caído en operación (FS-NUC-05 b) --------------------------------------


def test_a_secrets_outage_in_operation_keeps_ready_and_alerts_on_refresh(stack: Stack) -> None:
    app = stack.app()
    with TestClient(app) as client:
        _wait_until(lambda: _supervisor(app).started)
        signing = stack.signing
        assert signing is not None
        stack.secrets.set_mode(ProxyMode.REFUSE)
        before = metric_total(stack.reader, MetricName.SECRETS_REFRESH_FAILED)
        # El refresco periódico, pasados 5 minutos: relee el gestor de verdad y alerta.
        stack.signing_clock.advance(301)
        assert client.portal is not None
        # Las claves siguen en memoria (``refresh`` renueva desde la base); cada secreto de una
        # clave activa se relee del gestor, falla y cuenta en la métrica que alerta.
        assert client.portal.call(signing.refresh) is True
        failed = metric_total(stack.reader, MetricName.SECRETS_REFRESH_FAILED) - before
        assert failed >= len(BOOTSTRAP_ORDER)
        ready = client.get("/health/ready")
        assert ready.status_code == 200, "las claves siguen en memoria: se sigue firmando"
        stack.secrets.set_mode(ProxyMode.FORWARD)
        _close(client, stack)


# --- La seguridad a nivel de fila sin efecto impide arrancar ----------------------------------


def test_a_role_that_bypasses_row_level_security_never_becomes_ready(stack: Stack) -> None:
    app = stack.app(role=None)  # superusuario: ve las organizaciones sembradas
    with TestClient(app) as client:
        _wait_until(lambda: stack.exits != [])
        assert stack.exits == [STARTUP_FAILURE_EXIT_CODE]
        _assert_not_ready_fast_and_live(client)
        _close(client, stack)
