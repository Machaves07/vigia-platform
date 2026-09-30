"""El secreto TOTP nunca está en claro, y sin KMS no hay inscripción (TASK-123; LC-NUC-02, 27;
PAT-NUC-SEG-04; NFR-NUC-26; FS-NUC-05 b).

Contra PostgreSQL 16 real, como ``vigia_app`` (sin superusuario), y KMS de LocalStack:

- **Volcado** (criterio 2): tras inscribir, verificar, usar un código de recuperación y
  restablecer, se recorren como superusuario **todas las filas de todas las tablas** de
  ``identity``, ``shared`` y ``ledger`` (``fila::text``, con ``bytea`` en hexadecimal) y los
  registros del proceso: ni el secreto (bytes, hexadecimal, base32 ni base64) ni los códigos de
  recuperación en claro aparecen. La fila de ``totp_credential`` solo lleva texto cifrado y clave
  envuelta, y el texto cifrado no se descifra con el dato asociado de otro usuario.
- **KMS inaccesible** (criterio 3): con KMS en un puerto cerrado (conexión rechazada), ``enroll``
  lanza ``SecretsUnavailable`` (``temporarily_unavailable``) y en la base no queda credencial,
  código, marca de inscripción ni entrada de auditoría. Un proceso que ya tenía la clave de datos
  en memoria sigue verificando pasados los 5 minutos de la caché.
- **PR-NUC-09 en la base**: el mismo código no se acepta dos veces, tampoco en dos
  verificaciones concurrentes; un código de recuperación se acepta exactamente una vez.
- **BR-NUC-29**: el restablecimiento desactiva la credencial, cierra las sesiones activas con
  ``second_factor_reset``, borra la marca de inscripción y lo audita en la misma transacción; un
  administrador de otra organización recibe ``not_found``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import socket
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest

from tests.factories import make_context
from tests.identity_db import IdentitySeed, MigratedDatabase, _insert_user, seeded_identity
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
    PostgresEndpoint,
)
from tests.ledger_database import DatabaseLoop
from tests.outbox_support import app_database
from tests.second_factor_support import SwitchableKms
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.auth.second_factor import (
    AlreadyEnrolled,
    EnrollmentChallenge,
    SecondFactorNotFound,
    SecondFactorService,
    SecondFactorUser,
    TotpCredential,
    credential_aad,
    totp_step,
)
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ScopeContext
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import DecryptionFailed, EnvelopeCipher
from vigia_platform.shared.db import Database
from vigia_platform.shared.secrets import (
    AwsCredentials,
    AwsSettings,
    KmsAdapter,
    SecretsUnavailable,
)

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
SCHEMAS = ("identity", "shared", "ledger")


def _settings(url: str) -> AwsSettings:
    return AwsSettings(
        region="us-east-1",
        endpoint_url=url,
        credentials=AwsCredentials(LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY),
    )


def _closed_port_url() -> str:
    """Un puerto sin nadie escuchando: la conexión se rechaza."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


@dataclass
class Environment:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    seed: IdentitySeed
    database: Database
    audit: AuditWriter
    clock: SimulatedClock
    pool: CpuPool
    key_id: str
    localstack_url: str

    def run(self, awaitable: Any) -> Any:
        return self.loop.run(awaitable)

    def service(self, kms: Any | None = None) -> tuple[SecondFactorService, EnvelopeCipher]:
        """Un «proceso» con su propia caché de claves de datos."""
        kms = kms if kms is not None else KmsAdapter(_settings(self.localstack_url))
        cipher = EnvelopeCipher(kms, self.key_id, self.clock)
        store = PostgresSecondFactorStore(self.database, self.audit)
        return SecondFactorService(store, cipher, self.pool, self.clock), cipher

    def context(self, organization_id: uuid.UUID | None = None) -> ScopeContext:
        return make_context(
            kind=ActorKind.USER, organization_id=organization_id or self.seed.a.organization_id
        )

    def new_user(self, sessions: int = 0) -> SecondFactorUser:
        """Usuario nuevo del cliente A (como superusuario) con ``sessions`` sesiones activas."""

        async def insert() -> uuid.UUID:
            connection = await self.migrated.connect()
            try:
                async with connection.transaction():
                    user_id: uuid.UUID = await _insert_user(
                        connection, self.seed.a.organization_id, "segundo-factor"
                    )
                    for _ in range(sessions):
                        await connection.execute(
                            "INSERT INTO identity.session (session_id_hash, user_id,"
                            " organization_id, created_at, last_seen_at, idle_expires_at,"
                            " absolute_expires_at, origin_hash)"
                            " VALUES ($4, $1, $2, $3::timestamptz, $3::timestamptz,"
                            " $3::timestamptz + interval '30 minutes',"
                            " $3::timestamptz + interval '12 hours', $5)",
                            user_id,
                            self.seed.a.organization_id,
                            START,
                            secrets.token_hex(32),
                            secrets.token_hex(32),
                        )
                return user_id
            finally:
                await connection.close()

        user_id = self.run(insert())
        return SecondFactorUser(user_id, self.seed.a.organization_id, "persona@example.test")

    def query(self, sql: str, *args: Any) -> list[Any]:
        async def fetch() -> list[Any]:
            connection = await self.migrated.connect()
            try:
                return list(await connection.fetch(sql, *args))
            finally:
                await connection.close()

        return self.run(fetch())  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def env(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[Environment]:
    key_id = localstack_endpoint.aws_client("kms").create_key(
        Description="vigia-secrets de prueba (TASK-123)"
    )["KeyMetadata"]["KeyId"]
    with seeded_identity(postgres_endpoint, "totp_secret") as (migrated, seed):
        loop = DatabaseLoop()
        database = app_database(migrated, worker_pool_size=4)
        clock = SimulatedClock(START)
        audit = AuditWriter(
            database=database, clock=clock, provider_organization_id=seed.provider_organization_id
        )
        pool = CpuPool(clock, max_workers=2)
        try:
            yield Environment(
                loop, migrated, seed, database, audit, clock, pool, key_id, localstack_endpoint.url
            )
        finally:
            pool.shutdown()
            loop.run(database.dispose())
            loop.close()


def _secret(challenge: EnrollmentChallenge) -> bytes:
    encoded = parse_qs(urlparse(challenge.provisioning_uri).query)["secret"][0]
    return base64.b32decode(encoded + "=" * (-len(encoded) % 8))


def _code(secret: bytes, now: datetime) -> str:
    return pyotp.TOTP(base64.b32encode(secret).decode()).at(now)


def _confirm(
    env: Environment,
    service: SecondFactorService,
    context: ScopeContext,
    challenge: EnrollmentChallenge,
) -> TotpCredential:
    """Confirma la inscripción con un primer código y avanza un paso; la credencial guardada."""
    now = env.clock.now()
    code = _code(_secret(challenge), now)
    assert env.run(service.confirm_enrollment(context, challenge.credential, code, now)) is True
    env.clock.advance(30)
    stored = env.run(
        PostgresSecondFactorStore(env.database, env.audit).get_credential(
            context, challenge.credential.user_id
        )
    )
    assert stored is not None and stored.usable
    return stored


def _database_dump(env: Environment) -> str:
    """Todas las filas de todas las tablas de los tres esquemas, como texto (superusuario).

    La consulta de cada tabla la arma el servidor con ``format('%I')``; ``query_to_xml`` la
    ejecuta y ``xpath`` saca el texto (``fila::text``: ``bytea`` en hexadecimal).
    """
    tables = env.query(
        "SELECT table_name, (xpath('/row/dump/text()', query_to_xml(format("
        "'SELECT string_agg(t::text, E''\\n'') AS dump FROM %I.%I t',"
        " table_schema, table_name), false, true, '')))[1]::text AS dump"
        " FROM information_schema.tables"
        " WHERE table_schema = ANY($1::text[]) AND table_type = 'BASE TABLE'"
        " ORDER BY table_schema, table_name",
        list(SCHEMAS),
    )
    assert {row["table_name"] for row in tables} >= {"totp_credential", "recovery_code"}
    return "\n".join(row["dump"] or "" for row in tables)


def _forbidden_forms(secret: bytes, codes: tuple[str, ...]) -> list[str]:
    b32 = base64.b32encode(secret).decode().rstrip("=")
    forms = [secret.hex(), b32, b32.lower(), base64.b64encode(secret).decode()]
    for code in codes:
        forms += [code, code.replace("-", ""), code.replace("-", "").lower()]
    return forms


# --- Criterio 2: el secreto no aparece en claro ------------------------------------------------


def test_secret_is_never_in_plain_in_any_column_or_log(
    env: Environment, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    service, cipher = env.service()
    context = env.context()
    user = env.new_user(sessions=1)
    challenge = env.run(service.enroll(context, user))
    secret = _secret(challenge)
    credential = _confirm(env, service, context, challenge)
    now = env.clock.now()
    assert env.run(service.verify_totp(context, credential, _code(secret, now), now))
    assert env.run(service.consume_recovery_code(context, credential, challenge.recovery_codes[0]))
    assert env.run(service.reset(context, user.user_id)) == 1

    row = env.query(
        "SELECT secret_encrypted, data_key_wrapped FROM identity.totp_credential"
        " WHERE user_id = $1",
        user.user_id,
    )[0]
    assert secret not in row["secret_encrypted"] and secret not in row["data_key_wrapped"]
    # El texto cifrado va atado a su credencial: con el dato asociado de otra no se descifra.
    with pytest.raises(DecryptionFailed):
        env.run(
            cipher.decrypt(
                row["secret_encrypted"],
                row["data_key_wrapped"],
                credential_aad(user.organization_id, uuid.uuid4()),
            )
        )
    assert (
        env.run(
            cipher.decrypt(
                row["secret_encrypted"],
                row["data_key_wrapped"],
                credential_aad(user.organization_id, user.user_id),
            )
        )
        == secret
    )

    dump = _database_dump(env).lower()
    logs = caplog.text.lower()
    for form in _forbidden_forms(secret, challenge.recovery_codes):
        assert form.lower() not in dump
        assert form.lower() not in logs
    # El volcado sí contiene la fila cifrada: la búsqueda recorrió la tabla correcta.
    assert row["secret_encrypted"].hex() in dump
    assert str(user.user_id) in dump


# --- Criterio 3: sin KMS no hay inscripción ----------------------------------------------------


def test_enroll_with_kms_unreachable_is_temporarily_unavailable_and_saves_nothing(
    env: Environment,
) -> None:
    service, _ = env.service(KmsAdapter(_settings(_closed_port_url())))
    user = env.new_user()
    with pytest.raises(SecretsUnavailable) as raised:
        env.run(service.enroll(env.context(), user))
    assert raised.value.code == "temporarily_unavailable"
    assert (
        env.query("SELECT 1 FROM identity.totp_credential WHERE user_id = $1", user.user_id) == []
    )
    assert env.query("SELECT 1 FROM identity.recovery_code WHERE user_id = $1", user.user_id) == []
    assert (
        env.query(
            "SELECT second_factor_enrolled_at FROM identity.user_account WHERE user_id = $1",
            user.user_id,
        )[0]["second_factor_enrolled_at"]
        is None
    )
    assert env.query("SELECT 1 FROM shared.audit_entry WHERE resource_id = $1", user.user_id) == []


def test_verification_continues_without_kms_with_the_data_key_in_memory(env: Environment) -> None:
    enrolling, _ = env.service()
    context = env.context()
    user = env.new_user()
    challenge = env.run(enrolling.enroll(context, user))
    secret = _secret(challenge)
    confirmed = _confirm(env, enrolling, context, challenge)
    # Otro proceso de la API: descifra una vez con KMS y guarda la clave de datos.
    kms = SwitchableKms(KmsAdapter(_settings(env.localstack_url)))
    api, _ = env.service(kms)
    store = PostgresSecondFactorStore(env.database, env.audit)
    now = env.clock.now()
    assert env.run(api.verify_totp(context, confirmed, _code(secret, now), now))
    kms.inner = KmsAdapter(_settings(_closed_port_url()))
    env.clock.advance(10 * 60)
    now = env.clock.now()
    credential = env.run(store.get_credential(context, user.user_id))
    assert env.run(api.verify_totp(context, credential, _code(secret, now), now)) is True
    newcomer = env.new_user()
    with pytest.raises(SecretsUnavailable):
        env.run(api.enroll(context, newcomer))
    assert (
        env.query("SELECT 1 FROM identity.totp_credential WHERE user_id = $1", newcomer.user_id)
        == []
    )
    # Un proceso sin la clave en memoria falla cerrado: nunca acepta sin verificar.
    cold, _ = env.service(KmsAdapter(_settings(_closed_port_url())))
    env.clock.advance(30)
    now = env.clock.now()
    with pytest.raises(SecretsUnavailable):
        env.run(cold.verify_totp(context, credential, _code(secret, now), now))


# --- PR-NUC-09 contra la base ------------------------------------------------------------------


def test_same_code_is_accepted_once_even_concurrently(env: Environment) -> None:
    service, _ = env.service()
    context = env.context()
    user = env.new_user()
    challenge = env.run(service.enroll(context, user))
    secret = _secret(challenge)
    credential = _confirm(env, service, context, challenge)
    env.clock.advance(60)
    now = env.clock.now()
    code = _code(secret, now)

    async def twice() -> list[bool]:
        return list(
            await asyncio.gather(
                service.verify_totp(context, credential, code, now),
                service.verify_totp(context, credential, code, now),
            )
        )

    assert sorted(env.run(twice())) == [False, True]
    assert env.run(service.verify_totp(context, credential, code, now)) is False
    stored = env.query(
        "SELECT last_accepted_step FROM identity.totp_credential WHERE user_id = $1", user.user_id
    )[0]["last_accepted_step"]
    assert stored == totp_step(now)
    recovery = challenge.recovery_codes[5]

    async def consume_twice() -> list[bool]:
        return list(
            await asyncio.gather(
                service.consume_recovery_code(context, credential, recovery),
                service.consume_recovery_code(context, credential, recovery),
            )
        )

    assert sorted(env.run(consume_twice())) == [False, True]
    assert env.run(service.consume_recovery_code(context, credential, recovery)) is False
    # La base es la que decide: marcar dos veces el mismo código a la vez cambia una sola fila.
    store = PostgresSecondFactorStore(env.database, env.audit)
    unused = env.run(store.unused_recovery_codes(context, user.user_id))
    assert len(unused) == 9
    target = unused[0].recovery_code_id

    async def mark_twice() -> list[bool]:
        at = env.clock.now()
        return list(
            await asyncio.gather(
                store.mark_recovery_code_used(context, target, at),
                store.mark_recovery_code_used(context, target, at),
            )
        )

    assert sorted(env.run(mark_twice())) == [False, True]
    used = env.query(
        "SELECT count(*) FILTER (WHERE used_at IS NOT NULL) AS used, count(*) AS total"
        " FROM identity.recovery_code WHERE user_id = $1",
        user.user_id,
    )[0]
    assert (used["used"], used["total"]) == (2, 10)


# --- BR-NUC-29 ---------------------------------------------------------------------------------


def test_reset_closes_sessions_audits_and_forces_reenrollment(env: Environment) -> None:
    service, _ = env.service()
    context = env.context()
    user = env.new_user(sessions=2)
    first = env.run(service.enroll(context, user))
    stale = _confirm(env, service, context, first)
    with pytest.raises(AlreadyEnrolled):
        env.run(service.enroll(context, user))
    # La base tampoco deja sustituir una credencial activa (carrera entre dos inscripciones).
    store = PostgresSecondFactorStore(env.database, env.audit)
    intruder = replace(first.credential, secret_encrypted=b"\x01" + b"x" * 40)
    with pytest.raises(AlreadyEnrolled):
        env.run(store.save_enrollment(context, intruder, ()))
    kept = env.run(store.get_credential(context, user.user_id))
    assert kept is not None and kept.secret_encrypted == first.credential.secret_encrypted
    other_admin = env.context(env.seed.b.organization_id)
    with pytest.raises(SecondFactorNotFound):
        env.run(service.reset(other_admin, user.user_id))
    assert env.run(service.reset(context, user.user_id)) == 2

    sessions = env.query(
        "SELECT status, end_reason, ended_at FROM identity.session WHERE user_id = $1",
        user.user_id,
    )
    assert [(s["status"], s["end_reason"]) for s in sessions] == [
        ("revoked", "second_factor_reset")
    ] * 2
    account = env.query(
        "SELECT second_factor_enrolled_at FROM identity.user_account WHERE user_id = $1",
        user.user_id,
    )[0]
    assert account["second_factor_enrolled_at"] is None
    audit = env.query(
        "SELECT operation, result_count FROM shared.audit_entry"
        " WHERE resource_kind = 'user' AND resource_id = $1 ORDER BY chain_sequence",
        user.user_id,
    )
    assert [(a["operation"], a["result_count"]) for a in audit] == [
        ("second_factor_enrolled", None),
        ("session_closed", 2),
        ("second_factor_reset", 2),
    ]
    disabled = env.run(
        PostgresSecondFactorStore(env.database, env.audit).get_credential(context, user.user_id)
    )
    assert disabled is not None and not disabled.active
    now = env.clock.now()
    assert env.run(service.verify_totp(context, disabled, _code(_secret(first), now), now)) is False
    # Seguimiento nº 2 de VIG-66: la credencial leída antes del reset sigue «activa» en memoria;
    # lo que la rechaza es la condición de credencial activa del SQL.
    assert stale.usable
    assert env.run(service.verify_totp(context, stale, _code(_secret(first), now), now)) is False
    assert env.run(service.consume_recovery_code(context, stale, first.recovery_codes[1])) is False

    env.clock.advance(1)
    second = env.run(service.enroll(context, user))
    assert _secret(second) != _secret(first)
    # La base, por sí sola, no acepta nada de una inscripción sin confirmar (la guarda del
    # servicio aparte): ni un paso, ni sus códigos de recuperación.
    assert env.run(store.advance_step(context, user.user_id, totp_step(env.clock.now()))) is False
    assert env.run(store.unused_recovery_codes(context, user.user_id)) == ()
    pending_codes = env.query(
        "SELECT recovery_code_id FROM identity.recovery_code WHERE user_id = $1"
        " AND generated_at = $2",
        user.user_id,
        second.credential.enrolled_at,
    )
    assert len(pending_codes) == 10
    assert (
        env.run(
            store.mark_recovery_code_used(
                context,
                uuid.UUID(bytes=pending_codes[0]["recovery_code_id"].bytes),
                env.clock.now(),
            )
        )
        is False
    )
    # Sin confirmar, la inscripción nueva no verifica ni consume nada.
    assert (
        env.run(service.consume_recovery_code(context, second.credential, second.recovery_codes[0]))
        is False
    )
    now = env.clock.now()
    unconfirmed_code = _code(_secret(second), now)
    assert env.run(service.verify_totp(context, second.credential, unconfirmed_code, now)) is False
    current = _confirm(env, service, context, second)
    assert (
        env.run(service.consume_recovery_code(context, current, first.recovery_codes[0])) is False
    )
    assert env.run(service.consume_recovery_code(context, current, second.recovery_codes[0]))
    now = env.clock.now()
    assert env.run(service.verify_totp(context, current, _code(_secret(second), now), now))
