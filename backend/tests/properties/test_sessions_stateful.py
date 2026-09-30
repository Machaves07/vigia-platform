"""PR-NUC-07: máquina de sesiones frente al modelo simplificado (TASK-124; PBT-06; BR-NUC-22 a 27).

"Tras cualquier secuencia de ``login``, ``request(t)``, ``logout``, ``close_others``,
``password_change``, ``second_factor_reset``, ``deactivate``, ``suspend_org`` con instantes
generados, el conjunto de sesiones utilizables coincide con el del modelo después de **cada**
comando" (``session_commands``).

Contra PostgreSQL 16 real, como ``vigia_app``, con el ``LoginService``, el ``SessionService`` y el
almacén de verdad; solo la verificación de la contraseña y del código son dobles deterministas
(sus módulos tienen sus propias propiedades). Cada ejemplo crea dos organizaciones con tres
usuarios: uno sin segundo factor y otro con él inscrito y obligatorio en la organización A, y uno
sin segundo factor en la B.

Cada comando avanza el reloj un instante generado (0, segundos, los bordes de 30 minutos y de
12 horas, o hasta 14 horas) y se compara su resultado con el del modelo. El modelo es
independiente del código: sesiones con creación, última actividad, segundo factor, estado y
motivo; utilizable si está activa, ``now < last_seen + 30 min``, ``now < created + 12 h``, con
el segundo factor verificado y usuario y organización activos. El retardo de fallos del modelo
usa las funciones puras que prueba PR-NUC-06.

**Invariante tras cada comando**: para cada usuario, la lista de sesiones de la base (estado,
motivo, última actividad y si es utilizable ahora) es exactamente la del modelo.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import text

from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from tests.session_support import (
    GOOD_CODE,
    SessionEnvironment,
    User,
    session_environment,
)
from vigia_platform.identity.auth.login import (
    Authenticated,
    Rejected,
    RejectionCode,
    SecondFactorRequired,
)
from vigia_platform.identity.auth.sessions import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    SessionCookie,
    SessionSummary,
    ThrottleState,
    after_failure,
    after_success,
    retry_after_seconds,
)

pytestmark = pytest.mark.integration

ORIGIN = "192.0.2.10"
PLAIN, TWO_FACTOR, OTHER = "a_plain", "a_2fa", "b_plain"
STEPS_PER_EXAMPLE = 15
"""Cada paso son varias transacciones reales más la comprobación de todas las listas."""

instants = st.one_of(
    st.just(0),
    st.integers(1, 600),
    st.sampled_from([1799, 1800, 1801, 43199, 43200, 43201]),
    st.integers(0, 14 * 3600),
).map(lambda seconds: timedelta(seconds=seconds))


@dataclass
class ModelSession:
    user: str
    created: datetime
    last_seen: datetime
    verified: bool
    status: str = "active"
    reason: str | None = None


@dataclass
class ModelUser:
    record: User
    organization: str
    required: bool
    enrolled: bool
    active: bool = True
    throttle: ThrottleState | None = None


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[SessionEnvironment]:
    with session_environment(postgres_endpoint, "sessions_stateful") as env:
        SessionMachine.environment = env
        yield env


class SessionMachine(RuleBasedStateMachine):
    environment: SessionEnvironment
    sessions = Bundle("sessions")

    def __init__(self) -> None:
        super().__init__()
        self.env = self.environment
        self.login = self.env.login()
        self.service = self.env.sessions()
        self.store = self.env.store()
        self.model: dict[str, ModelSession] = {}
        self.cookies: dict[str, SessionCookie] = {}
        self.users: dict[str, ModelUser] = {}
        self.organizations: dict[str, uuid.UUID] = {}
        self.organization_active: dict[str, bool] = {}
        self.origin_throttle: ThrottleState | None = None
        self.origin = f"{ORIGIN}-{uuid.uuid4().hex}"
        self.now = self.env.clock.now()

    # --- Tiempo y modelo ------------------------------------------------------------------

    def advance(self, delta: timedelta) -> datetime:
        self.env.clock.advance(delta.total_seconds())
        self.now = self.env.clock.now()
        return self.now

    def user_ok(self, name: str) -> bool:
        user = self.users[name]
        return user.active and self.organization_active[user.organization]

    def usable(self, key: str, *, verified: bool = True) -> bool:
        session = self.model[key]
        return (
            session.status == "active"
            and self.now < session.last_seen + IDLE_TIMEOUT
            and self.now < session.created + ABSOLUTE_TIMEOUT
            and session.verified is verified
            and self.user_ok(session.user)
        )

    def end(self, key: str, status: str, reason: str) -> None:
        self.model[key].status = status
        self.model[key].reason = reason

    def fail_account(self, name: str) -> None:
        user = self.users[name]
        user.throttle = after_failure(user.throttle, self.now, threshold=10).state

    def succeed_account(self, name: str) -> None:
        user = self.users[name]
        if user.throttle is not None:
            user.throttle = after_success(self.now)

    # --- Preparación ----------------------------------------------------------------------

    @initialize()
    def setup(self) -> None:
        env = self.env
        for label in ("a", "b"):
            self.organizations[label] = env.add_organization()
            self.organization_active[label] = True
        self.users[PLAIN] = ModelUser(env.add_user(self.organizations["a"]), "a", False, False)
        self.users[TWO_FACTOR] = ModelUser(
            env.add_user(self.organizations["a"], required=True, enrolled=True), "a", True, True
        )
        self.users[OTHER] = ModelUser(env.add_user(self.organizations["b"]), "b", False, False)

    # --- Comandos -------------------------------------------------------------------------

    @rule(
        target=sessions,
        name=st.sampled_from([PLAIN, TWO_FACTOR, OTHER]),
        correct=st.booleans(),
        delta=instants,
    )
    def login_(self, name: str, correct: bool, delta: timedelta) -> str:
        self.advance(delta)
        user = self.users[name]
        password = user.record.password if correct else "incorrecta"
        result = self.env.run(self.login.authenticate(user.record.email, password, self.origin))
        retry = max(
            retry_after_seconds(self.origin_throttle, self.now),
            retry_after_seconds(user.throttle, self.now),
        )
        if retry > 0:
            assert isinstance(result, Rejected) and result.code is RejectionCode.THROTTLED
            assert result.retry_after_seconds == retry
            return "none"
        if not correct or not self.user_ok(name):
            assert isinstance(result, Rejected) and result.code is RejectionCode.UNAUTHENTICATED
            self.origin_throttle = after_failure(self.origin_throttle, self.now, threshold=50).state
            self.fail_account(name)
            return "none"
        pending = user.required or user.enrolled
        if pending:
            assert isinstance(result, SecondFactorRequired)
            assert result.enrollment_required is (not user.enrolled)
        else:
            assert isinstance(result, Authenticated)
            self.succeed_account(name)
        key = result.cookie.session_id_hash
        assert key not in self.model
        self.model[key] = ModelSession(name, self.now, self.now, verified=not pending)
        self.cookies[key] = result.cookie
        return key

    @rule(key=sessions, delta=instants)
    def request(self, key: str, delta: timedelta) -> None:
        self.advance(delta)
        if key == "none":
            return
        expected = self.usable(key)
        found = self.env.run(self.service.validate(self.cookies[key]))
        assert (found is not None) is expected
        if expected:
            assert (
                found is not None
                and found.user_id == self.users[self.model[key].user].record.user_id
            )
            self.model[key].last_seen = self.now

    @rule(key=sessions, correct=st.booleans(), enrollment=st.booleans(), delta=instants)
    def second_factor(self, key: str, correct: bool, enrollment: bool, delta: timedelta) -> None:
        self.advance(delta)
        if key == "none":
            return
        cookie = self.cookies[key]
        code = GOOD_CODE if correct else "000000"
        method = self.login.confirm_enrollment if enrollment else self.login.verify_second_factor
        result = self.env.run(method(cookie, code))
        session = self.model[key]
        if not self.usable(key, verified=False):
            assert isinstance(result, Rejected) and result.code is RejectionCode.UNAUTHENTICATED
            return
        session.last_seen = self.now  # la validación de la sesión pendiente la prolonga
        user = self.users[session.user]
        if user.enrolled is enrollment:
            code_expected = (
                RejectionCode.UNAUTHENTICATED
                if enrollment
                else RejectionCode.SECOND_FACTOR_REQUIRED
            )
            assert isinstance(result, Rejected) and result.code is code_expected
            return
        retry = retry_after_seconds(user.throttle, self.now)
        if retry > 0:
            assert isinstance(result, Rejected) and result.code is RejectionCode.THROTTLED
            assert result.retry_after_seconds == retry
            return
        if not correct:
            assert isinstance(result, Rejected) and result.code is RejectionCode.UNAUTHENTICATED
            self.fail_account(session.user)
            return
        assert isinstance(result, Authenticated)
        session.verified = True
        self.succeed_account(session.user)

    @rule(key=sessions, delta=instants)
    def logout(self, key: str, delta: timedelta) -> None:
        self.advance(delta)
        if key == "none":
            return
        closed = self.env.run(self.service.close(self.cookies[key]))
        was_active = self.model[key].status == "active"
        assert closed is was_active
        if was_active:
            self.end(key, "closed", "logout")

    @rule(key=sessions, delta=instants)
    def close_others(self, key: str, delta: timedelta) -> None:
        self.advance(delta)
        if key == "none":
            return
        count = self.env.run(self.service.close_others(self.cookies[key]))
        if not self.usable(key):
            assert count is None
            return
        self.model[key].last_seen = self.now
        others = [
            other
            for other, s in self.model.items()
            if other != key and s.user == self.model[key].user and s.status == "active"
        ]
        assert count == len(others)
        for other in others:
            self.end(other, "closed", "closed_by_user")

    @rule(key=sessions, delta=instants)
    def password_change(self, key: str, delta: timedelta) -> None:
        """La ruta del cambio (TASK-135) valida la sesión y revoca las demás del usuario."""
        self.advance(delta)
        if key == "none":
            return
        found = self.env.run(self.service.validate(self.cookies[key]))
        assert (found is not None) is self.usable(key)
        if found is None:
            return
        self.model[key].last_seen = self.now
        context = self.env.contexts.anonymous(found.organization_id)
        count = self.env.run(
            self.service.on_password_changed(context, found.user_id, found.session_id_hash)
        )
        others = [
            other
            for other, s in self.model.items()
            if other != key and s.user == self.model[key].user and s.status == "active"
        ]
        assert count == len(others)
        for other in others:
            self.end(other, "revoked", "password_changed")

    @rule(name=st.sampled_from([PLAIN, TWO_FACTOR, OTHER]), delta=instants)
    def second_factor_reset(self, name: str, delta: timedelta) -> None:
        self.advance(delta)
        user = self.users[name]
        context = self.env.contexts.anonymous(user.record.organization_id)
        closed = self.env.run(
            self.env.second_factor_store().reset(context, user.record.user_id, self.now)
        )
        affected = [k for k, s in self.model.items() if s.user == name and s.status == "active"]
        assert closed == len(affected)
        for key in affected:
            self.end(key, "revoked", "second_factor_reset")
        user.enrolled = False

    @rule(name=st.sampled_from([PLAIN, TWO_FACTOR, OTHER]), revoke=st.booleans(), delta=instants)
    def deactivate(self, name: str, revoke: bool, delta: timedelta) -> None:
        """La desactivación (TASK-126) revoca; sin revocar, la validación ya la rechaza."""
        self.advance(delta)
        user = self.users[name]
        context = self.env.contexts.anonymous(user.record.organization_id)

        async def apply() -> int:
            async with self.env.database.transaction(context) as transaction:
                await transaction.execute(
                    text(
                        "UPDATE identity.user_account SET status = 'deactivated',"
                        " deactivated_at = :now WHERE user_id = :user_id"
                    ),
                    {"now": self.now, "user_id": user.record.user_id},
                )
            if not revoke:
                return 0
            return await self.service.on_user_deactivated(context, user.record.user_id)

        closed = self.env.run(apply())
        user.active = False
        if revoke:
            affected = [k for k, s in self.model.items() if s.user == name and s.status == "active"]
            assert closed == len(affected)
            for key in affected:
                self.end(key, "revoked", "user_deactivated")

    @rule(organization=st.sampled_from(["a", "b"]), revoke=st.booleans(), delta=instants)
    def suspend_org(self, organization: str, revoke: bool, delta: timedelta) -> None:
        self.advance(delta)
        context = self.env.contexts.anonymous(self.organizations[organization])

        async def apply() -> int:
            async with self.env.database.transaction(context) as transaction:
                await transaction.execute(
                    text("UPDATE identity.organization SET status = 'suspended'")
                )
            if not revoke:
                return 0
            return await self.service.on_organization_suspended(context)

        closed = self.env.run(apply())
        self.organization_active[organization] = False
        if revoke:
            affected = [
                k
                for k, s in self.model.items()
                if self.users[s.user].organization == organization and s.status == "active"
            ]
            assert closed == len(affected)
            for key in affected:
                self.end(key, "revoked", "organization_suspended")

    @rule(delta=instants)
    def expire_sweep(self, delta: timedelta) -> None:
        """La tarea ``expire_sessions`` en cada organización."""
        self.advance(delta)
        total = 0
        for organization_id in self.organizations.values():
            context = self.env.contexts.anonymous(organization_id)
            total += self.env.run(self.store.expire(context, self.now))
        expected = 0
        for session in self.model.values():
            if session.status != "active":
                continue
            if self.now >= session.created + ABSOLUTE_TIMEOUT:
                session.status, session.reason = "expired", "absolute_timeout"
            elif self.now >= session.last_seen + IDLE_TIMEOUT:
                session.status, session.reason = "expired", "idle_timeout"
            else:
                continue
            expected += 1
        assert total == expected

    # --- Invariante -----------------------------------------------------------------------

    @invariant()
    def database_matches_model(self) -> None:
        if not self.users:
            return
        names = list(self.users)

        async def list_all() -> list[Sequence[SessionSummary]]:
            # Las tres listas a la vez, cada una en su transacción y su conexión (menos espera).
            return list(
                await asyncio.gather(
                    *(
                        self.store.list_sessions(
                            self.env.contexts.anonymous(self.users[name].record.organization_id),
                            self.users[name].record.user_id,
                            self.now,
                        )
                        for name in names
                    )
                )
            )

        listings = self.env.run(list_all())
        for name, listed in zip(names, listings, strict=True):
            actual = {
                s.session_id_hash: (
                    s.status.value,
                    None if s.end_reason is None else s.end_reason.value,
                    s.last_seen_at,
                    s.second_factor_verified,
                    s.usable,
                )
                for s in listed
            }
            expected = {
                key: (s.status, s.reason, s.last_seen, s.verified, self.usable(key))
                for key, s in self.model.items()
                if s.user == name
            }
            assert actual == expected


def test_session_machine_matches_the_model(environment: SessionEnvironment) -> None:
    """PR-NUC-07 con cada semilla del perfil activo (``tests/conftest.py``)."""
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(SessionMachine)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS_PER_EXAMPLE))
