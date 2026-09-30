"""Inicio de sesión en dos pasos y sesiones contra PostgreSQL 16 real (TASK-124; S-PLA-01).

Como ``vigia_app`` (sin superusuario), con el almacén, la auditoría y la bandeja de verdad:

- **BR-NUC-23**: correo desconocido, contraseña incorrecta, cuenta invitada, desactivada u
  organización suspendida y código incorrecto responden exactamente lo mismo, y todos verifican
  una contraseña con Argon2id una vez (el tiempo no distingue los casos).
- **BR-NUC-22**: la sesión pendiente solo sirve para el segundo factor; un usuario obligado sin
  inscribir se inscribe en el inicio de sesión y la inscripción solo cuenta con un primer código
  válido (segundo factor real con cifrado de sobre). El código de recuperación también completa.
- **Recálculo** del hash con parámetros de otra versión tras un inicio correcto.
- **Criterio 3**: el identificador de sesión en claro no está en ninguna fila de ``identity``,
  ``shared`` ni ``ledger``, ni en los registros del proceso; la auditoría no lleva ni su hash.
- **Cookie** ``__Host-`` con ``Secure; HttpOnly; SameSite=Strict; Path=/``; una organización
  cambiada en la cookie no encuentra la sesión (seguridad a nivel de fila).
- **Tareas** ``expire_sessions`` (5 min) y ``throttle_window_cleanup`` (15 min) registradas y
  con su efecto; todo fin de sesión auditado con su motivo.
- **``identity.login_organization``**: solo devuelve la organización; ni ``vigia_app`` ni el
  dueño leen ``user_account`` fuera de la función, tampoco fijando ``vigia.login_lookup``.
- ``idle_expires_at = last_seen_at + 30 min`` en la base (seguimiento nº 1 de VIG-38).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import asyncpg  # type: ignore[import-untyped]
import pyotp
import pytest
from argon2 import PasswordHasher, Type

from tests.integration.conftest import PostgresEndpoint
from tests.second_factor_support import FakeKms
from tests.session_support import (
    GOOD_CODE,
    FakePasswords,
    SessionEnvironment,
    User,
    session_environment,
)
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import (
    EXPIRE_SESSIONS,
    THROTTLE_WINDOW_CLEANUP,
    cleanup_throttle_windows,
    register_session_tasks,
)
from vigia_platform.identity.auth.login import (
    INVALID_CREDENTIALS_MESSAGE,
    Authenticated,
    Rejected,
    RejectionCode,
    SecondFactorRequired,
)
from vigia_platform.identity.auth.passwords import (
    CURRENT_VERSION,
    HASH_VERSIONS,
    BreachCheck,
    BreachSource,
    PasswordService,
)
from vigia_platform.identity.auth.second_factor import SecondFactorService
from vigia_platform.identity.auth.sessions import (
    SESSION_COOKIE_NAME,
    SessionCookie,
    SessionPurpose,
    ThrottleSubject,
    clearing_cookie,
    origin_hash,
    session_id_hash,
)
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry, Schedule

pytestmark = pytest.mark.integration

INVALID = Rejected(RejectionCode.UNAUTHENTICATED, INVALID_CREDENTIALS_MESSAGE)
SCHEMAS = ("identity", "shared", "ledger")


class NeverBreached:
    async def check(self, password: str) -> BreachCheck:
        return BreachCheck(breached=False, source=BreachSource.LOCAL_LIST)


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[SessionEnvironment]:
    with session_environment(postgres_endpoint, "login_sessions") as environment:
        yield environment


@pytest.fixture(scope="module")
def pool(env: SessionEnvironment) -> Iterator[CpuPool]:
    cpu = CpuPool(env.clock, max_workers=2)
    try:
        yield cpu
    finally:
        cpu.shutdown()


def _real_second_factor(env: SessionEnvironment, pool: CpuPool) -> SecondFactorService:
    cipher = EnvelopeCipher(FakeKms(), "alias/vigia-secrets", env.clock)
    return SecondFactorService(
        PostgresSecondFactorStore(env.database, env.audit), cipher, pool, env.clock
    )


def _operations(env: SessionEnvironment, user: User) -> list[tuple[str, str, Any]]:
    rows = env.fetch(
        "SELECT operation, outcome, filters_json FROM shared.audit_entry"
        " WHERE organization_id = $1 AND resource_id = $2 ORDER BY chain_sequence",
        user.organization_id,
        user.user_id,
    )
    return [
        (
            r["operation"],
            r["outcome"],
            None if r["filters_json"] is None else json.loads(r["filters_json"]),
        )
        for r in rows
    ]


# --- BR-NUC-23: mensajes idénticos y tiempo que no distingue ------------------------------------


def test_every_failure_looks_the_same_and_verifies_one_password(env: SessionEnvironment) -> None:
    organization = env.add_organization()
    good = env.add_user(organization)
    invited = env.add_user(organization, status="invited", with_password=False)
    deactivated = env.add_user(organization, status="deactivated")
    suspended_org = env.add_organization()
    suspended = env.add_user(suspended_org)
    env.run(
        env.admin.execute(
            "UPDATE identity.organization SET status = 'suspended' WHERE organization_id = $1",
            suspended_org,
        )
    )
    passwords = FakePasswords()
    login = env.login(passwords=passwords)
    attempts = [
        ("nadie@example.test", "cualquiera"),
        ("no es un correo", "cualquiera"),
        (good.email, "incorrecta"),
        (good.email.upper(), "incorrecta"),
        (invited.email, "cualquiera"),
        (deactivated.email, deactivated.password),
        (suspended.email, suspended.password),
    ]
    for index, (email, password) in enumerate(attempts):
        before = passwords.verify_calls
        result = env.run(login.authenticate(email, password, f"198.51.100.{index}"))
        assert result == INVALID, email
        assert passwords.verify_calls == before + 1, email
        assert repr(result) == repr(INVALID)
    # El correo se normaliza: el mismo usuario entra con mayúsculas y espacios.
    result = env.run(login.authenticate(f"  {good.email.upper()} ", good.password, "198.51.100.9"))
    assert isinstance(result, Authenticated) and result.user_id == good.user_id


def test_unknown_account_failure_is_audited_in_the_provider_chain(env: SessionEnvironment) -> None:
    provider = env.seed.provider_organization_id
    before = env.fetch(
        "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'login_failed' AND resource_id IS NULL",
        provider,
    )[0]["n"]
    env.run(env.login().authenticate("fantasma@example.test", "x", "198.51.100.200"))
    after = env.fetch(
        "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'login_failed' AND resource_id IS NULL AND outcome = 'denied'",
        provider,
    )[0]["n"]
    assert after == before + 1


# --- BR-NUC-22: sesión pendiente y segundo factor -----------------------------------------------


def test_pending_session_serves_only_the_second_factor(env: SessionEnvironment) -> None:
    organization = env.add_organization()
    user = env.add_user(organization, required=True, enrolled=True)
    login, sessions = env.login(), env.sessions()
    pending = env.run(login.authenticate(user.email, user.password, "198.51.100.20"))
    assert isinstance(pending, SecondFactorRequired) and not pending.enrollment_required
    cookie = pending.cookie
    assert env.run(sessions.validate(cookie)) is None
    assert env.run(sessions.close_others(cookie)) is None
    assert env.run(sessions.list_sessions(cookie)) is None
    assert env.run(sessions.validate(cookie, SessionPurpose.SECOND_FACTOR)) is not None
    assert env.run(login.start_enrollment(cookie)) is None  # ya inscrito: no se reinscribe aquí
    assert env.run(login.verify_second_factor(cookie, "000000")) == INVALID
    done = env.run(login.verify_second_factor(cookie, GOOD_CODE))
    assert done == Authenticated(cookie, user.user_id)
    assert env.run(sessions.validate(cookie)) is not None
    assert env.run(sessions.validate(cookie, SessionPurpose.SECOND_FACTOR)) is None
    assert env.run(login.verify_second_factor(cookie, GOOD_CODE)) == INVALID
    assert [op for op, _, _ in _operations(env, user)] == ["login_failed", "login_succeeded"]
    assert _operations(env, user)[-1][2] == {"second_factor": "totp"}


def test_required_user_enrolls_at_login_and_only_a_valid_first_code_counts(
    env: SessionEnvironment, pool: CpuPool
) -> None:
    organization = env.add_organization()
    user = env.add_user(organization, required=True)
    login = env.login(second_factor=_real_second_factor(env, pool))
    first = env.run(login.authenticate(user.email, user.password, "198.51.100.30"))
    assert isinstance(first, SecondFactorRequired) and first.enrollment_required
    cookie = first.cookie
    assert env.run(login.verify_second_factor(cookie, "123456")) == Rejected(
        RejectionCode.SECOND_FACTOR_REQUIRED, "inscribe el segundo factor para continuar"
    )
    challenge = env.run(login.start_enrollment(cookie))
    assert challenge is not None and len(challenge.recovery_codes) == 10
    encoded = parse_qs(urlparse(challenge.provisioning_uri).query)["secret"][0]
    secret = base64.b32decode(encoded + "=" * (-len(encoded) % 8))
    totp = pyotp.TOTP(base64.b32encode(secret).decode())
    now = env.clock.now()
    window = {totp.at(now + timedelta(seconds=offset)) for offset in (-30, 0, 30)}
    wrong = next(code for code in ("000000", "111111", "222222", "333333") if code not in window)
    assert env.run(login.confirm_enrollment(cookie, wrong)) == INVALID
    assert (
        env.fetch(
            "SELECT second_factor_enrolled_at FROM identity.user_account WHERE user_id = $1",
            user.user_id,
        )[0]["second_factor_enrolled_at"]
        is None
    )
    done = env.run(login.confirm_enrollment(cookie, totp.at(now)))
    assert done == Authenticated(cookie, user.user_id)
    assert env.run(env.sessions().validate(cookie)) is not None
    ops = _operations(env, user)
    assert [op for op, _, _ in ops] == [
        "login_failed",
        "second_factor_enrolled",
        "login_succeeded",
    ]
    assert ops[-1][2] == {"second_factor": "enrollment"}
    # El siguiente inicio pide el código (inscrito) y acepta un código de recuperación.
    env.clock.advance(60)
    second = env.run(login.authenticate(user.email, user.password, "198.51.100.30"))
    assert isinstance(second, SecondFactorRequired) and not second.enrollment_required
    done = env.run(login.verify_second_factor(second.cookie, challenge.recovery_codes[4]))
    assert isinstance(done, Authenticated)
    assert _operations(env, user)[-1][2] == {"second_factor": "recovery_code"}


# --- Recálculo del hash --------------------------------------------------------------------------


def test_hash_with_other_parameters_is_recomputed_after_success(
    env: SessionEnvironment, pool: CpuPool
) -> None:
    organization = env.add_organization()
    old = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1, type=Type.ID)
    password = "contraseña de prueba 2026"  # noqa: S105 - contraseña sintética
    user = env.add_user(organization, password_hash=old.hash(password.encode()))
    passwords = PasswordService(NeverBreached(), pool)
    login = env.login(passwords=passwords)
    wrong = env.run(login.authenticate(user.email, "otra contraseña", "198.51.100.40"))
    assert wrong == INVALID
    unchanged = env.fetch(
        "SELECT password_hash FROM identity.password_credential WHERE user_id = $1", user.user_id
    )[0]["password_hash"]
    assert unchanged.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    result = env.run(login.authenticate(user.email, password, "198.51.100.40"))
    assert isinstance(result, Authenticated)
    row = env.fetch(
        "SELECT password_hash, algorithm_version FROM identity.password_credential"
        " WHERE user_id = $1",
        user.user_id,
    )[0]
    params = HASH_VERSIONS[CURRENT_VERSION]
    assert row["password_hash"].startswith(
        f"$argon2id$v=19$m={params.memory_kib},t={params.iterations},p={params.parallelism}$"
    )
    assert row["algorithm_version"] == f"argon2id-v{CURRENT_VERSION}"
    again = env.run(login.authenticate(user.email, password, "198.51.100.40"))
    assert isinstance(again, Authenticated)


# --- Cookie ------------------------------------------------------------------------------------


def test_cookie_attributes_and_strict_parsing(env: SessionEnvironment) -> None:
    organization = env.add_organization()
    user = env.add_user(organization)
    result = env.run(env.login().authenticate(user.email, user.password, "198.51.100.50"))
    assert isinstance(result, Authenticated)
    cookie = result.cookie
    header = cookie.header_value()
    assert header == (
        f"{SESSION_COOKIE_NAME}={organization}.{cookie.token};"
        " Secure; HttpOnly; SameSite=Strict; Path=/"
    )
    assert SESSION_COOKIE_NAME.startswith("__Host-") and "Domain" not in header
    assert "Max-Age" not in header and "Expires" not in header
    assert clearing_cookie().startswith(f"{SESSION_COOKIE_NAME}=; Max-Age=0; Secure; HttpOnly")
    assert len(base64.urlsafe_b64decode(cookie.token + "=")) == 32  # 256 bits
    assert SessionCookie.parse(cookie.value) == cookie
    assert cookie.token not in repr(cookie) and cookie.token not in repr(result)
    for bad in (
        cookie.value + "A",
        cookie.value[:-1],
        cookie.value.upper(),
        cookie.value.replace(".", ":"),
        f"{organization}.{cookie.token[:-1]}=",
        f"{str(organization).upper()}.{cookie.token}",
        " " + cookie.value,
        cookie.token,
        "",
        None,
        42,
    ):
        assert SessionCookie.parse(bad) is None, bad
    # Otra organización en la cookie: la seguridad a nivel de fila no deja ver la sesión.
    other = env.add_organization()
    forged = SessionCookie(other, cookie.token)
    assert env.run(env.sessions().validate(forged)) is None
    assert env.run(env.sessions().close(forged)) is False
    assert env.run(env.sessions().validate(cookie)) is not None


# --- Criterio 3: el identificador en claro no está en la base, los registros ni la auditoría --


def _dump(env: SessionEnvironment) -> dict[str, str]:
    rows = env.fetch(
        "SELECT table_schema || '.' || table_name AS name,"
        " (xpath('/row/dump/text()', query_to_xml(format("
        "'SELECT string_agg(t::text, E''\\n'') AS dump FROM %I.%I t',"
        " table_schema, table_name), false, true, '')))[1]::text AS dump"
        " FROM information_schema.tables"
        " WHERE table_schema = ANY($1::text[]) AND table_type = 'BASE TABLE'",
        list(SCHEMAS),
    )
    return {row["name"]: row["dump"] or "" for row in rows}


def test_plain_session_identifier_never_leaves_the_cookie(
    env: SessionEnvironment, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    organization = env.add_organization()
    user = env.add_user(organization, required=True, enrolled=True)
    plain = env.add_user(organization)
    login, sessions = env.login(), env.sessions()
    cookies: list[SessionCookie] = []
    pending = env.run(login.authenticate(user.email, user.password, "198.51.100.60"))
    assert isinstance(pending, SecondFactorRequired)
    env.run(login.verify_second_factor(pending.cookie, "000000"))
    env.run(login.verify_second_factor(pending.cookie, GOOD_CODE))
    cookies.append(pending.cookie)
    for _ in range(3):
        result = env.run(login.authenticate(plain.email, plain.password, "198.51.100.61"))
        assert isinstance(result, Authenticated)
        cookies.append(result.cookie)
    env.run(sessions.validate(cookies[1]))
    env.run(sessions.list_sessions(cookies[1]))
    assert env.run(sessions.close_others(cookies[1])) == 2
    env.run(sessions.close(cookies[1]))
    env.clock.advance(31 * 60)
    for organization_id in (organization, env.seed.provider_organization_id):
        env.run(env.store().expire(env.contexts.anonymous(organization_id), env.clock.now()))
    dump = _dump(env)
    everything = "\n".join(dump.values())
    logs = caplog.text
    for cookie in cookies:
        raw = base64.urlsafe_b64decode(cookie.token + "=")
        for form in (cookie.token, cookie.value, raw.hex(), base64.b64encode(raw).decode()):
            assert form not in everything, form
            assert form not in logs, form
        # La auditoría no lleva ni el hash: nada que enlace una entrada con la cookie.
        assert cookie.session_id_hash not in dump["shared.audit_entry"]
        assert cookie.session_id_hash not in dump["shared.outbox_event"]
        assert cookie.session_id_hash not in logs
        # La sesión sí está, por su hash: la búsqueda recorrió la tabla correcta.
        assert cookie.session_id_hash in dump["identity.session"]
    audited = {op for op, _, _ in _operations(env, plain)}
    assert {"login_succeeded", "sessions_closed_others", "session_closed"} <= audited


def test_session_hash_is_sha256_of_the_token() -> None:
    cookie = SessionCookie(uuid.uuid4(), "A" * 43)
    assert session_id_hash(cookie.token) == cookie.session_id_hash
    assert len(cookie.session_id_hash) == 64
    with pytest.raises(ValueError):
        session_id_hash("A" * 42)
    with pytest.raises(ValueError):
        SessionCookie(uuid.uuid4(), "A" * 42 + "=")


# --- Fin de sesión y tareas periódicas --------------------------------------------------------


def test_session_end_is_audited_with_its_reason(env: SessionEnvironment) -> None:
    organization = env.add_organization()
    user = env.add_user(organization)
    login, sessions = env.login(), env.sessions()
    cookies = [
        env.run(login.authenticate(user.email, user.password, "198.51.100.70")).cookie
        for _ in range(3)
    ]
    assert env.run(sessions.close_others(cookies[0])) == 2
    assert env.run(sessions.close(cookies[0])) is True
    assert env.run(sessions.close(cookies[0])) is False  # ya cerrada: nada que auditar
    ops = _operations(env, user)
    assert ops[-2:] == [
        ("sessions_closed_others", "success", {"end_reason": "closed_by_user"}),
        ("session_closed", "success", {"end_reason": "logout"}),
    ]
    rows = env.fetch(
        "SELECT status, end_reason FROM identity.session WHERE user_id = $1 ORDER BY status",
        user.user_id,
    )
    assert sorted((r["status"], r["end_reason"]) for r in rows) == [
        ("closed", "closed_by_user"),
        ("closed", "closed_by_user"),
        ("closed", "logout"),
    ]


def test_periodic_tasks_are_registered_and_do_their_work(env: SessionEnvironment) -> None:
    registry = PeriodicTaskRegistry()
    expire, cleanup = register_session_tasks(registry, audit=env.audit, clock=env.clock)
    assert (expire.task_name, expire.schedule, expire.unit.value) == (
        EXPIRE_SESSIONS,
        Schedule.every(300),
        "U-02",
    )
    assert (cleanup.task_name, cleanup.schedule) == (THROTTLE_WINDOW_CLEANUP, Schedule.every(900))
    assert [task.task_name for task in registry.tasks()] == sorted(
        [EXPIRE_SESSIONS, THROTTLE_WINDOW_CLEANUP]
    )

    organization = env.add_organization()
    user = env.add_user(organization)
    login = env.login()
    idle = env.run(login.authenticate(user.email, user.password, "198.51.100.80")).cookie
    env.clock.advance(20 * 60)
    fresh = env.run(login.authenticate(user.email, user.password, "198.51.100.80")).cookie
    env.clock.advance(10 * 60)  # la primera lleva 30 min sin actividad; la segunda, 10
    context = env.contexts.anonymous(organization)

    async def run_handler(handler: Any) -> None:
        async with env.database.transaction(context) as transaction:
            await handler(transaction)

    env.run(run_handler(expire.handler))
    status = {
        r["session_id_hash"]: (r["status"], r["end_reason"])
        for r in env.fetch(
            "SELECT session_id_hash, status, end_reason FROM identity.session WHERE user_id = $1",
            user.user_id,
        )
    }
    assert status == {
        idle.session_id_hash: ("expired", "idle_timeout"),
        fresh.session_id_hash: ("active", None),
    }
    assert _operations(env, user)[-1] == (
        "session_closed",
        "success",
        {"end_reason": "idle_timeout"},
    )
    env.clock.advance(12 * 3600)
    env.run(run_handler(expire.handler))
    assert (
        env.fetch(
            "SELECT end_reason FROM identity.session WHERE session_id_hash = $1",
            fresh.session_id_hash,
        )[0]["end_reason"]
        == "absolute_timeout"
    )

    # Limpieza de ventanas: un contador vencido vuelve a cero; uno vigente no se toca.
    for attempt in range(6):
        env.run(login.authenticate(user.email, "mala", f"198.51.100.{81 + attempt}"))
        state = env.run(
            env.store().throttle_state(context, ThrottleSubject.account(organization, user.user_id))
        )
        assert state is not None
        env.clock.advance(max(0.0, (state.next_allowed_at - env.clock.now()).total_seconds()))
    stale = ThrottleSubject.account(organization, user.user_id)
    state = env.run(env.store().throttle_state(context, stale))
    assert state is not None and state.consecutive_failures == 6

    async def cleanup_now() -> int:
        async with env.database.transaction(context) as transaction:
            return await cleanup_throttle_windows(transaction, env.clock.now())

    # Al borde: 15 minutos después del último instante permitido menos un segundo, sigue.
    env.clock.advance(15 * 60 - 1)
    assert env.run(cleanup_now()) == 0
    env.clock.advance(1)
    assert env.run(cleanup_now()) == 1
    cleaned = env.run(env.store().throttle_state(context, stale))
    assert cleaned is not None and cleaned.consecutive_failures == 0
    assert cleaned.alerted_at is None and cleaned.next_allowed_at == env.clock.now()
    assert env.run(cleanup_now()) == 0
    env.run(run_handler(cleanup.handler))


# --- Búsqueda previa a la organización -----------------------------------------------------------


def test_login_lookup_reveals_only_the_organization(env: SessionEnvironment) -> None:
    organization = env.add_organization()
    user = env.add_user(organization)

    async def as_role(role: str, *statements: str) -> list[Any]:
        connection = await env.migrated.connect(role)
        try:
            results = []
            async with connection.transaction():
                for statement in statements:
                    results.append(
                        await connection.fetch(statement, *([user.email] * statement.count("$1")))
                    )
            return results
        finally:
            await connection.close()

    looked_up = env.run(as_role("vigia_app", "SELECT identity.login_organization($1) AS o"))
    assert looked_up[0][0]["o"] == organization
    unknown = env.run(
        as_role("vigia_app", "SELECT identity.login_organization('nadie@example.test') AS o")
    )
    assert unknown[0][0]["o"] is None
    # vigia_app no lee la tabla sin contexto, tampoco fijando la variable de la función: la
    # política login_lookup es solo del dueño.
    results = env.run(
        as_role(
            "vigia_app",
            "SELECT set_config('vigia.login_lookup', 'on', true)",
            "SELECT count(*) AS n FROM identity.user_account",
        )
    )
    assert results[1][0]["n"] == 0
    # El dueño tampoco ve filas fuera de la función: tras llamarla, la variable vuelve a estar
    # vacía y la tabla sigue oculta (FORCE ROW LEVEL SECURITY).
    after = env.run(
        as_role(
            "vigia_migrate",
            "SELECT identity.login_organization($1) AS o",
            "SELECT current_setting('vigia.login_lookup', true) AS flag,"
            " (SELECT count(*) FROM identity.user_account) AS n",
        )
    )
    assert after[1][0]["flag"] == "" and after[1][0]["n"] == 0
    # La función solo existe para vigia_app (y el dueño), y devuelve un uuid, nada más.
    info = env.fetch(
        "SELECT prosecdef, proconfig, pg_get_function_result(oid) AS result,"
        " has_function_privilege('public', oid, 'EXECUTE') AS public_execute"
        " FROM pg_proc WHERE oid = 'identity.login_organization(text)'::regprocedure"
    )[0]
    assert info["prosecdef"] and info["result"] == "uuid" and not info["public_execute"]
    assert info["proconfig"] == ["search_path=pg_catalog"]


# --- Validación en cada petición: bordes que la máquina de PR-NUC-07 alcanza poco ------------


def test_absolute_timeout_ends_a_session_kept_alive(env: SessionEnvironment) -> None:
    """Con actividad cada 29 minutos la sesión vive hasta las 12 h exactas y ni un instante más."""
    organization = env.add_organization()
    user = env.add_user(organization)
    sessions = env.sessions()
    cookie = env.run(env.login().authenticate(user.email, user.password, "198.51.100.90")).cookie
    created = env.clock.now()
    while env.clock.now() + timedelta(minutes=29) < created + timedelta(hours=12):
        env.clock.advance(29 * 60)
        assert env.run(sessions.validate(cookie)) is not None
    env.clock.advance((created + timedelta(hours=12) - env.clock.now()).total_seconds() - 0.001)
    assert env.run(sessions.validate(cookie)) is not None
    env.clock.advance(0.001)
    assert env.run(sessions.validate(cookie)) is None


def test_idle_timeout_is_exclusive_at_thirty_minutes(env: SessionEnvironment) -> None:
    organization = env.add_organization()
    user = env.add_user(organization)
    sessions = env.sessions()
    cookie = env.run(env.login().authenticate(user.email, user.password, "198.51.100.91")).cookie
    env.clock.advance(30 * 60 - 0.001)
    assert env.run(sessions.validate(cookie)) is not None  # prolonga: 30 min desde ahora
    env.clock.advance(30 * 60)
    assert env.run(sessions.validate(cookie)) is None


@pytest.mark.parametrize("change", ["user", "organization"])
def test_deactivation_or_suspension_cuts_access_even_without_revoking(
    env: SessionEnvironment, change: str
) -> None:
    """La validación comprueba usuario y organización activos en cada petición (BR-NUC-25)."""
    organization = env.add_organization()
    user = env.add_user(organization)
    pending_user = env.add_user(organization, required=True, enrolled=True)
    login, sessions = env.login(), env.sessions()
    cookie = env.run(login.authenticate(user.email, user.password, "198.51.100.92")).cookie
    pending = env.run(
        login.authenticate(pending_user.email, pending_user.password, "198.51.100.92")
    ).cookie
    assert env.run(sessions.validate(cookie)) is not None
    if change == "user":
        for target in (user, pending_user):
            env.run(
                env.admin.execute(
                    "UPDATE identity.user_account SET status = 'deactivated', deactivated_at = $2"
                    " WHERE user_id = $1",
                    target.user_id,
                    env.clock.now(),
                )
            )
    else:
        env.run(
            env.admin.execute(
                "UPDATE identity.organization SET status = 'suspended' WHERE organization_id = $1",
                organization,
            )
        )
    assert env.run(sessions.validate(cookie)) is None
    assert env.run(sessions.validate(pending, SessionPurpose.SECOND_FACTOR)) is None
    assert env.run(login.verify_second_factor(pending, GOOD_CODE)) == INVALID
    status = env.fetch("SELECT status FROM identity.session WHERE user_id = $1", user.user_id)
    assert [row["status"] for row in status] == ["active"]  # sin revocar: la validación basta


def test_success_resets_the_account_counter(env: SessionEnvironment) -> None:
    """Cuatro fallos, un éxito y cuatro fallos más: ninguno retiene (el éxito reinició)."""
    organization = env.add_organization()
    user = env.add_user(organization)
    login = env.login()
    for attempt in range(4):
        env.run(login.authenticate(user.email, "mala", f"203.0.113.{attempt}"))
    assert isinstance(
        env.run(login.authenticate(user.email, user.password, "203.0.113.10")), Authenticated
    )
    for attempt in range(4):
        result = env.run(login.authenticate(user.email, "mala", f"203.0.113.{20 + attempt}"))
        assert result == INVALID
    state = env.run(
        env.store().throttle_state(
            env.contexts.anonymous(organization),
            ThrottleSubject.account(organization, user.user_id),
        )
    )
    assert state is not None and state.consecutive_failures == 4
    assert isinstance(
        env.run(login.authenticate(user.email, user.password, "203.0.113.30")), Authenticated
    )


def test_idle_expiry_is_checked_by_the_database(env: SessionEnvironment) -> None:
    organization = env.add_organization()
    user = env.add_user(organization)

    async def insert(idle: timedelta) -> str | None:
        try:
            await env.admin.execute(
                "INSERT INTO identity.session (session_id_hash, user_id, organization_id,"
                " created_at, last_seen_at, idle_expires_at, absolute_expires_at, origin_hash)"
                " VALUES ($1, $2, $3, $4, $4, $5, $4::timestamptz + interval '12 hours', $6)",
                uuid.uuid4().hex * 2,
                user.user_id,
                organization,
                env.clock.now(),
                env.clock.now() + idle,
                origin_hash("x", b"k" * 32),
            )
        except asyncpg.CheckViolationError as error:
            return str(error.constraint_name)
        return None

    assert env.run(insert(timedelta(days=10))) == "session_idle_expiry"
    assert env.run(insert(timedelta(minutes=29))) == "session_idle_expiry"
    assert env.run(insert(timedelta(minutes=30))) is None
