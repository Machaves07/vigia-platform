"""FS-NUC-05 · Gestor de secretos y KMS inaccesibles (a) al arrancar y (b) en operación
(PAT-NUC-RES-02, PAT-NUC-RES-03; NFR-NUC-37; LC-NUC-25, 27, 28).

**Inyección**: los **puntos concretos** de LocalStack que usa la plataforma para Secrets Manager
y para KMS se bloquean, cada uno con su ``FaultProxy`` (el almacén no se toca): **(a)** antes
del arranque de un proceso ``vigia-api`` real (``tests/resilience/api_process.py``, uvicorn,
``SigningService`` con el material en el gestor y ``KmsAdapter``); **(b)** tras 5 minutos de
operación (reloj simulado: la caché de 5 minutos del gestor y la de la clave de datos vencen).
El modo del bloqueo (rechazo o servicio congelado) sale de la semilla.

**Resultado esperado**:

- (a) el proceso **nunca queda ``ready``** (``/health/ready`` 503, ``/health/live`` 200) y
  **termina con código distinto de cero** (``STARTUP_FAILURE_EXIT_CODE``) al vencer el plazo de
  arranque;
- (b) la **firma** sigue con las claves en memoria; ``secrets_refresh_failed`` **alerta** (gestor y
  KMS); la **rotación** responde ``temporarily_unavailable`` sin cambiar nada; la **verificación del
  segundo factor** sigue con la clave de datos en memoria y una **inscripción nueva** responde
  ``temporarily_unavailable`` sin guardar nada; nunca se omite el factor. Al volver el gestor, la
  rotación funciona.

Solo datos generados.
"""

from __future__ import annotations

import base64
import secrets
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest

from tests.fault_proxy import FaultProxy, ProxyMode, fault_proxy
from tests.hibp_service import metric_total, metrics_with_reader
from tests.identity_db import IdentitySeed, MigratedDatabase, _insert_user, seeded_identity
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
    PostgresEndpoint,
)
from tests.ledger_database import DatabaseLoop
from tests.resilience.harness import WALL, free_port, scenario, wait_until
from tests.resilience.processes import process_environment, process_group
from tests.signing_support import (
    BOOTSTRAP_ORDER,
    PROVIDER_ORGANIZATION_ID,
    InMemoryKeyStore,
    RecordingEvents,
    provider_context,
)
from tests.writer_support import unit_context
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.auth.second_factor import SecondFactorService, SecondFactorUser
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE
from vigia_platform.shared.api.errors import ApiErrorCode, translate
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.db import Database, DatabaseSettings, ProcessKind, SslMode
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.secrets import (
    AwsCredentials,
    AwsSettings,
    KmsAdapter,
    SecretsManagerAdapter,
    SecretsUnavailable,
)
from vigia_platform.shared.signing import SigningPurpose, SigningService

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

START: Final = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
STARTUP_DEADLINE_SECONDS: Final = 6.0
"""Plazo de arranque del proceso de prueba (60 s en producción), para no esperar un minuto."""
EXIT_MARGIN_SECONDS: Final = 20.0
"""Holgura sobre el plazo: el último intento de cada comprobación puede agotar su tope (10 s)."""
OPERATION_SECONDS: Final = 5 * 60 + 1
"""«Tras 5 minutos de operación»: vencen la caché del gestor y la de la clave de datos."""


def _aws(url: str) -> AwsSettings:
    return AwsSettings(
        region="us-east-1",
        endpoint_url=url,
        credentials=AwsCredentials(LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY),
    )


def _split(url: str) -> tuple[str, int]:
    host, port = url.split("//", 1)[1].rsplit(":", 1)
    return host, int(port)


@pytest.fixture(scope="module")
def identity(
    postgres_endpoint: PostgresEndpoint,
) -> Iterator[tuple[MigratedDatabase, IdentitySeed]]:
    with seeded_identity(postgres_endpoint, "fs_nuc_05") as seeded:
        yield seeded


# --- (a) al arrancar ------------------------------------------------------------------------------


def test_fs_nuc_05a_secrets_and_kms_blocked_before_startup(
    identity: tuple[MigratedDatabase, IdentitySeed],
    localstack_endpoint: LocalStackEndpoint,
    tmp_path: Path,
) -> None:
    migrated, seed = identity
    with scenario(
        "FS-NUC-05a",
        title="Gestor de secretos y KMS inaccesibles al arrancar",
        injection="puntos de LocalStack (Secrets Manager y KMS) bloqueados antes del arranque",
        expected="el proceso nunca queda ready y termina con código distinto de cero",
    ) as run:
        modes = [ProxyMode.REFUSE, ProxyMode.FREEZE]
        run.random.shuffle(modes)
        secrets_mode, kms_mode = modes
        kms_key = localstack_endpoint.aws_client("kms").create_key(
            Description="vigia-secrets de prueba (FS-NUC-05)"
        )["KeyMetadata"]["KeyId"]
        host, port = _split(localstack_endpoint.url)
        with (
            fault_proxy(host, port) as secrets_proxy,
            fault_proxy(host, port) as kms_proxy,
            process_group(tmp_path) as group,
        ):
            secrets_proxy.set_mode(secrets_mode)
            kms_proxy.set_mode(kms_mode)
            api_port = free_port()
            environ = process_environment(
                VIGIA_TEST_DATABASE_URL=migrated.as_role("vigia_app").sqlalchemy_url,
                VIGIA_TEST_PROVIDER_ORGANIZATION=seed.provider_organization_id,
                VIGIA_TEST_PORT=api_port,
                VIGIA_TEST_STARTUP_DEADLINE=STARTUP_DEADLINE_SECONDS,
                VIGIA_TEST_SIGNING="localstack",
                VIGIA_TEST_SIGNING_ENVIRONMENT=f"fs05-{uuid.uuid4().hex[:8]}",
                VIGIA_TEST_BOOTSTRAP_SECRETS_URL=localstack_endpoint.url,
                VIGIA_TEST_SECRETS_URL=secrets_proxy.url,
                VIGIA_TEST_KMS_URL=kms_proxy.url,
                VIGIA_SECRETS_KEY_ARN=kms_key,
            )
            spawned = group.start("api", "tests.resilience.api_process", environ)
            base = f"http://127.0.0.1:{api_port}"

            def live() -> bool:
                try:
                    return httpx.get(f"{base}/health/live", timeout=2).status_code == 200
                except httpx.HTTPError:
                    return False

            wait_until(live, timeout=90, message="el proceso no atendió /health/live")
            serving_since = WALL.monotonic()
            probes: list[dict[str, Any]] = []
            while spawned.process.poll() is None:
                try:
                    ready = httpx.get(f"{base}/health/ready", timeout=3).status_code
                    alive = httpx.get(f"{base}/health/live", timeout=3).status_code
                except httpx.HTTPError:
                    break  # el proceso terminó entre la consulta y la respuesta
                probes.append({"ready": ready, "live": alive})
                assert WALL.monotonic() - serving_since < (
                    STARTUP_DEADLINE_SECONDS + EXIT_MARGIN_SECONDS
                ), "el proceso siguió vivo después del plazo de arranque"
                time.sleep(0.5)
            code = spawned.process.wait(30)
            exited_after = WALL.monotonic() - serving_since
            output = spawned.tail(60)
        run.observe(
            secrets_mode=secrets_mode.value,
            kms_mode=kms_mode.value,
            probes=len(probes),
            ready_statuses=sorted({probe["ready"] for probe in probes}),
            live_statuses=sorted({probe["live"] for probe in probes}),
            exit_code=code,
            exited_after_seconds=round(exited_after, 2),
        )
        assert probes, "se consultó la salud mientras el proceso arrancaba"
        assert {probe["ready"] for probe in probes} == {503}, "nunca quedó ready"
        assert {probe["live"] for probe in probes} == {200}
        assert code == STARTUP_FAILURE_EXIT_CODE, output  # 3: distinto de cero
        assert "signing_keys" in output and "data_key" in output, output


# --- (b) en operación ---------------------------------------------------------------------------


@dataclass
class Operation:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    seed: IdentitySeed
    database: Database
    audit: AuditWriter
    clock: SimulatedClock
    pool: CpuPool

    def run(self, awaitable: Any) -> Any:
        return self.loop.run(awaitable)

    def new_user(self) -> SecondFactorUser:
        async def insert() -> uuid.UUID:
            connection = await self.migrated.connect()
            try:
                user: uuid.UUID = await _insert_user(
                    connection, self.seed.a.organization_id, "fs-nuc-05"
                )
                return user
            finally:
                await connection.close()

        user_id = self.run(insert())
        return SecondFactorUser(user_id, self.seed.a.organization_id, "persona@example.test")

    def rows(self, sql: str, *args: Any) -> list[Any]:
        async def fetch() -> list[Any]:
            connection = await self.migrated.connect()
            try:
                return list(await connection.fetch(sql, *args))
            finally:
                await connection.close()

        rows: list[Any] = self.run(fetch())
        return rows


@pytest.fixture
def operation(identity: tuple[MigratedDatabase, IdentitySeed]) -> Iterator[Operation]:
    migrated, seed = identity
    loop = DatabaseLoop()
    database = Database.create(
        DatabaseSettings(
            url=migrated.as_role("vigia_app").sqlalchemy_url,
            process=ProcessKind.API,
            sslmode=SslMode.DISABLE,
        )
    )
    clock = SimulatedClock(START)
    audit = AuditWriter(
        database=database, clock=clock, provider_organization_id=seed.provider_organization_id
    )
    pool = CpuPool(clock, max_workers=2)
    try:
        yield Operation(loop, migrated, seed, database, audit, clock, pool)
    finally:
        pool.shutdown()
        loop.run(database.dispose())
        loop.close()


def _secret(provisioning_uri: str) -> bytes:
    encoded = parse_qs(urlparse(provisioning_uri).query)["secret"][0]
    return base64.b32decode(encoded + "=" * (-len(encoded) % 8))


def _code(secret: bytes, now: datetime) -> str:
    return pyotp.TOTP(base64.b32encode(secret).decode()).at(now)


def test_fs_nuc_05b_secrets_and_kms_blocked_in_operation(
    operation: Operation, localstack_endpoint: LocalStackEndpoint
) -> None:
    op = operation
    with scenario(
        "FS-NUC-05b",
        title="Gestor de secretos y KMS inaccesibles en operación",
        injection="puntos de LocalStack (Secrets Manager y KMS) bloqueados tras 5 min de operación",
        expected=(
            "firma y verificación del segundo factor siguen con lo que hay en memoria,"
            " secrets_refresh_failed alerta, rotación e inscripciones nuevas responden"
            " temporarily_unavailable"
        ),
    ) as run:
        mode = run.random.choice([ProxyMode.REFUSE, ProxyMode.FREEZE])
        kms_key = localstack_endpoint.aws_client("kms").create_key(
            Description="vigia-secrets de prueba (FS-NUC-05 b)"
        )["KeyMetadata"]["KeyId"]
        host, port = _split(localstack_endpoint.url)
        metrics, reader = metrics_with_reader()
        environment = f"fs05b-{uuid.uuid4().hex[:8]}"
        with fault_proxy(host, port) as secrets_proxy, fault_proxy(host, port) as kms_proxy:
            signing = _signing(op, localstack_endpoint.url, secrets_proxy, environment, metrics)
            cipher = EnvelopeCipher(
                KmsAdapter(_aws(kms_proxy.url)), kms_key, op.clock, metrics=metrics
            )
            factor = SecondFactorService(
                PostgresSecondFactorStore(op.database, op.audit), cipher, op.pool, op.clock
            )
            context = unit_context(op.seed.a.organization_id, ActorUnit.U02, kind=ActorKind.USER)
            # Operación normal: inscripción, confirmación y una verificación (clave en memoria).
            user = op.new_user()
            challenge = op.run(factor.enroll(context, user))
            secret = _secret(challenge.provisioning_uri)
            now = op.clock.now()
            assert op.run(
                factor.confirm_enrollment(context, challenge.credential, _code(secret, now), now)
            )
            op.clock.advance(30)
            store = PostgresSecondFactorStore(op.database, op.audit)
            credential = op.run(store.get_credential(context, user.user_id))
            now = op.clock.now()
            assert op.run(factor.verify_totp(context, credential, _code(secret, now), now))

            # Tras 5 minutos de operación, el gestor y KMS dejan de responder.
            secrets_proxy.set_mode(mode)
            kms_proxy.set_mode(mode)
            op.clock.advance(OPERATION_SECONDS)
            before = metric_total(reader, MetricName.SECRETS_REFRESH_FAILED)
            refreshed = op.run(signing.refresh())
            after_refresh = metric_total(reader, MetricName.SECRETS_REFRESH_FAILED)
            signed = signing.sign(SigningPurpose.CHECKPOINT, {"n": secrets.randbelow(1000)})
            keys_before = dict(signing_store(signing).keys)
            with pytest.raises(SecretsUnavailable) as rotation:
                op.run(signing.rotate(SigningPurpose.GATE, context=provider_context()))
            keys_unchanged = signing_store(signing).keys == keys_before
            credential = op.run(store.get_credential(context, user.user_id))
            now = op.clock.now()
            verified = op.run(factor.verify_totp(context, credential, _code(secret, now), now))
            wrong = op.run(factor.verify_totp(context, credential, "000000", now))
            after_verify = metric_total(reader, MetricName.SECRETS_REFRESH_FAILED)
            newcomer = op.new_user()
            with pytest.raises(SecretsUnavailable) as enrollment:
                op.run(factor.enroll(context, newcomer))
            saved = op.rows(
                "SELECT 1 FROM identity.totp_credential WHERE user_id = $1", newcomer.user_id
            )

            # El gestor vuelve: la rotación funciona.
            secrets_proxy.set_mode(ProxyMode.FORWARD)
            kms_proxy.set_mode(ProxyMode.FORWARD)
            op.run(signing.rotate(SigningPurpose.GATE, context=provider_context()))

        run.observe(
            mode=mode.value,
            refreshed=refreshed,
            secrets_refresh_failed={
                "after_signing_refresh": after_refresh - before,
                "after_totp_verification": after_verify - after_refresh,
            },
            signed=bool(signed),
            rotation=translate(rotation.value).code.value,
            keys_unchanged=keys_unchanged,
            totp_verified_from_memory=verified,
            totp_wrong_code_rejected=not wrong,
            enrollment=translate(enrollment.value).code.value,
            enrollment_saved=len(saved),
        )
        assert refreshed is True, "las claves siguen en memoria"
        assert after_refresh - before >= len(BOOTSTRAP_ORDER), "secrets_refresh_failed alerta"
        assert signed
        assert translate(rotation.value).code is ApiErrorCode.TEMPORARILY_UNAVAILABLE
        assert keys_unchanged
        assert verified is True and wrong is False, "el factor se verifica, nunca se omite"
        assert after_verify - after_refresh >= 1, "KMS caído también alerta"
        assert translate(enrollment.value).code is ApiErrorCode.TEMPORARILY_UNAVAILABLE
        assert saved == []


def signing_store(service: SigningService) -> InMemoryKeyStore:
    store = service._store
    assert isinstance(store, InMemoryKeyStore)
    return store


def _signing(
    op: Operation, direct_url: str, proxy: FaultProxy, environment: str, metrics: Any
) -> SigningService:
    """Claves dadas de alta por LocalStack directo; el servicio en operación, por ``proxy``."""
    store = InMemoryKeyStore()
    bootstrap = SigningService(
        provider_organization_id=PROVIDER_ORGANIZATION_ID,
        store=store,
        secrets=SecretsManagerAdapter(_aws(direct_url), op.clock),
        events=RecordingEvents(),
        clock=op.clock,
        environment=environment,
    )
    op.run(bootstrap.start(required=()))
    for purpose in BOOTSTRAP_ORDER:
        op.run(bootstrap.rotate(purpose, context=provider_context()))
    service = SigningService(
        provider_organization_id=PROVIDER_ORGANIZATION_ID,
        store=store,
        secrets=SecretsManagerAdapter(_aws(proxy.url), op.clock, metrics=metrics),
        events=RecordingEvents(),
        clock=op.clock,
        environment=environment,
        metrics=metrics,
    )
    op.run(service.start())
    return service
