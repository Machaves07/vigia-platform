"""PR-NUC-06: retardo progresivo exacto por cuenta y por origen (TASK-124; BR-NUC-24; LC-NUC-03).

"Para cualquier secuencia de intentos fallidos, el retardo es no decreciente hasta un éxito, vale
0 antes del quinto fallo, es al menos ``30 x 2^(n-5)`` s tras el enésimo y nunca supera 15 min;
un éxito lo reinicia" (``login_attempt_sequences``).

- **Modelo puro**: secuencias generadas de fallos, éxitos, esperas exactas hasta poder reintentar
  y huecos que cruzan la ventana de 15 minutos. Tras el enésimo fallo de la ventana el retardo es
  **exactamente** ``min(30 x 2^(n-5) s, 15 min)`` (0 hasta el cuarto), no decrece dentro de la
  ventana, un éxito lo deja en 0 y un intento retenido no cuenta. La alerta salta una vez por
  ventana, justo al llegar a 10 (cuenta) o 50 (origen). El oráculo del retardo es la fórmula con
  potencias de Python, no ``throttle_delay``.
- **Dos procesos** (criterio 2, PAT-NUC-ESC-02): la misma secuencia contra PostgreSQL con dos
  adaptadores ``shared.db`` independientes que se alternan (dos «instancias»): cada fallo lo
  anota uno u otro, y los dos leen siempre el mismo estado, igual al del modelo. Además, un
  **proceso del sistema operativo** aparte (``python -m tests.throttle_probe``) lee el mismo
  retardo que este proceso acaba de fijar.
- **Reserva antes de verificar** (PAT-NUC-ESC-03): ``reserve_attempt`` retiene o cuenta ya el
  intento; ``release_attempt`` devuelve la reserva de un intento correcto sin acortar nunca un
  retardo ajeno. Intentos simultáneos de dos procesos se serializan en la fila: los cinco
  primeros se reservan y el resto ya ve el retardo.
- **Inicio de sesión**: las cuentas inexistentes cuentan solo por origen; 10 fallos de una cuenta
  publican una ``security_alert`` con ``resource_kind = user`` y 50 de un origen otra sin recurso.
  El retardo de la cuenta retiene desde cualquier origen nuevo; los fallos del segundo paso
  cuentan en la fila de la cuenta y retienen hasta un código bueno; un inicio correcto no deja
  fallos en el origen ni (con sesión pendiente) en la cuenta. Ráfagas de 25 intentos
  simultáneos, de contraseña o de código, verifican como mucho cinco.

Semillas y ejemplos del perfil activo (``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.session_support import (
    GOOD_CODE,
    ORIGIN_KEY,
    START,
    FakePasswords,
    FakeSecondFactor,
    SessionEnvironment,
    User,
    session_environment,
)
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.auth.login import (
    Authenticated,
    LoginService,
    Rejected,
    RejectionCode,
    SecondFactorRequired,
)
from vigia_platform.identity.auth.passwords import VerifyResult
from vigia_platform.identity.auth.second_factor import TotpCredential
from vigia_platform.identity.auth.sessions import (
    ACCOUNT_ALERT_THRESHOLD,
    ORIGIN_ALERT_THRESHOLD,
    THROTTLE_MAX_DELAY,
    SessionCookie,
    ThrottleState,
    ThrottleSubject,
    ThrottleSubjectKind,
    after_failure,
    after_success,
    alert_threshold,
    origin_hash,
    release_attempt,
    reserve_attempt,
    retry_after_seconds,
    throttle_delay,
    window_expired,
)
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Database

BACKEND = Path(__file__).resolve().parents[2]


def expected_delay(failures: int) -> timedelta:
    """El oráculo de BR-NUC-24 con potencias exactas de Python."""
    if failures < 5:
        return timedelta(0)
    return timedelta(seconds=min(30 * 2 ** (failures - 5), 15 * 60))


# --- Secuencias generadas ---------------------------------------------------------------------

gaps = st.one_of(
    st.just(timedelta(0)),
    st.integers(1, 120).map(lambda s: timedelta(seconds=s)),
    st.integers(0, 3_000_000).map(lambda ms: timedelta(milliseconds=ms)),
    st.sampled_from([899, 900, 901, 1799, 1800, 1801]).map(lambda s: timedelta(seconds=s)),
)

WAIT = "wait"
"""Esperar exactamente hasta ``next_allowed_at`` y fallar (el borde del retardo)."""

attempts = st.lists(
    st.tuples(gaps, st.sampled_from(["fail", "fail", "fail", WAIT, WAIT, "success"])),
    max_size=40,
)


@given(events=attempts, kind=st.sampled_from(ThrottleSubjectKind))
def test_delay_progression_matches_br_nuc_24(
    events: list[tuple[timedelta, str]], kind: ThrottleSubjectKind
) -> None:
    threshold = alert_threshold(kind)
    now = START
    state: ThrottleState | None = None
    failures = 0
    previous = timedelta(0)
    for gap, action in events:
        now += gap
        if action == "success":
            state = after_success(now)
            failures, previous = 0, timedelta(0)
            assert retry_after_seconds(state, now) == 0
            continue
        if action == WAIT and state is not None:
            now = max(now, state.next_allowed_at)
        if retry_after_seconds(state, now) > 0:
            # Intento retenido: no se verifica ni cuenta; el estado no cambia.
            assert state is not None and now < state.next_allowed_at
            continue
        new_window = state is None or state.consecutive_failures == 0 or window_expired(state, now)
        outcome = after_failure(state, now, threshold=threshold)
        failures = 1 if new_window else failures + 1
        delay = outcome.state.next_allowed_at - now
        assert outcome.state.consecutive_failures == failures
        assert delay == expected_delay(failures)
        assert delay <= THROTTLE_MAX_DELAY
        if failures < 5:
            assert delay == timedelta(0)
        if not new_window:
            assert delay >= previous
        assert outcome.alert is (failures == threshold)
        assert (outcome.state.alerted_at is not None) is (failures >= threshold)
        previous, state = delay, outcome.state


@given(failures=st.integers(0, 10**6))
def test_throttle_delay_is_the_formula_for_any_count(failures: int) -> None:
    assert throttle_delay(failures) == expected_delay(failures)


def test_throttle_delay_bounds_and_invalid_counts() -> None:
    assert [throttle_delay(n).total_seconds() for n in range(4, 11)] == [
        0,
        30,
        60,
        120,
        240,
        480,
        900,
    ]
    assert throttle_delay(2**63) == THROTTLE_MAX_DELAY
    for bad in (-1, True, 1.0, "5"):
        with pytest.raises(ValueError):
            throttle_delay(bad)  # type: ignore[arg-type]


def test_window_resets_fifteen_minutes_after_the_last_allowed_instant() -> None:
    state = None
    now = START
    for _ in range(6):
        state = after_failure(state, now, threshold=10).state
        now = state.next_allowed_at
    assert state is not None and state.consecutive_failures == 6
    # Justo antes del final de la ventana el contador sigue; justo en el final vuelve a 1.
    edge = state.next_allowed_at + timedelta(minutes=15)
    assert after_failure(state, edge - timedelta(microseconds=1), threshold=10).state == (
        ThrottleState(7, START, edge - timedelta(microseconds=1) + timedelta(seconds=120), None)
    )
    assert after_failure(state, edge, threshold=10).state == ThrottleState(1, edge, edge, None)


@given(
    failures=st.integers(5, 40),
    skew=st.integers(1, 15 * 60 * 1000).map(lambda ms: timedelta(milliseconds=ms)),
)
def test_a_late_failure_with_an_earlier_clock_never_shortens_the_delay(
    failures: int, skew: timedelta
) -> None:
    """Dos instancias con relojes algo distintos: el fallo que se anota después con una hora
    anterior (o durante el retardo, por una carrera) nunca adelanta ``next_allowed_at``."""
    recorded = START + timedelta(hours=1)
    state = ThrottleState(failures, START, recorded + expected_delay(failures), None)
    late = after_failure(state, recorded - skew, threshold=10).state
    assert late.consecutive_failures == failures + 1
    assert late.next_allowed_at >= state.next_allowed_at
    assert late.next_allowed_at == max(
        state.next_allowed_at, recorded - skew + expected_delay(failures + 1)
    )


def test_retry_after_rounds_up_and_is_zero_at_the_boundary() -> None:
    state = ThrottleState(5, START, START + timedelta(seconds=30), None)
    assert retry_after_seconds(state, START) == 30
    assert retry_after_seconds(state, START + timedelta(milliseconds=1)) == 30
    assert retry_after_seconds(state, START + timedelta(seconds=29, milliseconds=1)) == 1
    assert retry_after_seconds(state, START + timedelta(seconds=30)) == 0
    assert retry_after_seconds(None, START) == 0


states = st.builds(
    lambda failures, offset_s, window_s: ThrottleState(
        failures,
        START - timedelta(seconds=window_s),
        START + timedelta(seconds=offset_s),
        None,
    ),
    st.integers(0, 60),
    st.integers(-3_600, 900),
    st.integers(0, 3_600),
)
ORIGIN_SUBJECT = ThrottleSubject.origin(uuid.UUID(int=1), "a" * 64)
_ACCOUNT_SUBJECT = ThrottleSubject.account(uuid.UUID(int=2), uuid.UUID(int=3))


@given(before=states, kind=st.sampled_from(ThrottleSubjectKind))
def test_reservation_is_retained_or_counted_before_verifying(
    before: ThrottleState, kind: ThrottleSubjectKind
) -> None:
    """Retenido si hay retardo (la fila no cambia); si no, contado ya como el fallo siguiente."""
    subject = ORIGIN_SUBJECT if kind is ThrottleSubjectKind.ORIGIN else _ACCOUNT_SUBJECT
    reservation = reserve_attempt(subject, before, START)
    assert reservation.before == before and reservation.subject == subject
    retry = retry_after_seconds(before, START)
    if retry > 0:
        assert not reservation.granted and reservation.retry_after_seconds == retry
        with pytest.raises(ValueError):
            release_attempt(before, reservation)
        return
    assert reservation.granted and reservation.retry_after_seconds == 0
    assert reservation.outcome == after_failure(before, START, threshold=alert_threshold(kind))


@given(before=states, alerted=st.booleans())
def test_release_without_other_attempts_restores_the_row(
    before: ThrottleState, alerted: bool
) -> None:
    """Un intento correcto no cuenta: la fila vuelve a como estaba, salvo ``alerted_at``."""
    now = max(START, before.next_allowed_at)
    reservation = reserve_attempt(ORIGIN_SUBJECT, before, now)
    assert reservation.outcome is not None
    current = reservation.outcome.state
    if alerted:
        current = ThrottleState(
            current.consecutive_failures, current.window_started_at, current.next_allowed_at, now
        )
        reservation = dataclasses.replace(
            reservation, outcome=dataclasses.replace(reservation.outcome, state=current)
        )
    released = release_attempt(current, reservation)
    assert released == dataclasses.replace(before, alerted_at=current.alerted_at)
    assert retry_after_seconds(released, now) == 0


@given(before=states, others=st.integers(1, 10), gap=st.integers(0, 600))
def test_release_after_concurrent_attempts_only_removes_its_failure(
    before: ThrottleState, others: int, gap: int
) -> None:
    """Con intentos concurrentes por medio, se resta un fallo y el retardo no se acorta."""
    now = max(START, before.next_allowed_at)
    reservation = reserve_attempt(ORIGIN_SUBJECT, before, now)
    assert reservation.outcome is not None
    current = reservation.outcome.state
    for _ in range(others):
        current = after_failure(current, now + timedelta(seconds=gap), threshold=50).state
    released = release_attempt(current, reservation)
    assert released.consecutive_failures == current.consecutive_failures - 1
    assert released.next_allowed_at == current.next_allowed_at
    assert released.alerted_at == current.alerted_at


def test_release_never_goes_below_zero() -> None:
    reservation = reserve_attempt(ORIGIN_SUBJECT, ThrottleState(0, START, START), START)
    cleaned = after_success(START + timedelta(minutes=20))  # la limpieza periódica, por medio
    assert release_attempt(cleaned, reservation).consecutive_failures == 0


def test_origin_hash_is_keyed_and_never_the_address() -> None:
    first = origin_hash("203.0.113.7", b"a" * 32)
    assert len(first) == 64 and "203.0.113.7" not in first
    assert first != origin_hash("203.0.113.7", b"b" * 32)
    assert first != hashlib.sha256(b"203.0.113.7").hexdigest()
    assert origin_hash(" 203.0.113.7 ", b"a" * 32) == first
    assert origin_hash("", b"a" * 32) == origin_hash("x" * 300, b"a" * 32)
    with pytest.raises(ValueError):
        origin_hash("203.0.113.7", b"corta")


# --- Contra PostgreSQL con dos procesos ---------------------------------------------------------


class Env:
    def __init__(self, base: SessionEnvironment, second: Database) -> None:
        self.base = base
        self.stores = (base.store(), PostgresSessionStore(second, base.audit, base.outbox))
        self.client = base.add_organization()


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[Env]:
    with session_environment(postgres_endpoint, "throttle") as base:
        # La segunda «instancia»: otro adaptador con su propio pool, como otro proceso de API.
        second = app_database(base.migrated, worker_pool_size=2)
        try:
            yield Env(base, second)
        finally:
            base.run(second.dispose())


def _subject(env: Env, kind: ThrottleSubjectKind) -> ThrottleSubject:
    if kind is ThrottleSubjectKind.ORIGIN:
        return ThrottleSubject.origin(
            env.base.seed.provider_organization_id, origin_hash(uuid.uuid4().hex, b"o" * 32)
        )
    return ThrottleSubject.account(env.client, uuid.uuid4())


@given(
    events=st.lists(
        st.tuples(gaps, st.sampled_from(["fail", "fail", WAIT, WAIT, "success"])), max_size=20
    ),
    kind=st.sampled_from(ThrottleSubjectKind),
)
@pytest.mark.integration
def test_both_processes_see_the_same_state_as_the_model(
    env: Env, events: list[tuple[timedelta, str]], kind: ThrottleSubjectKind
) -> None:
    base = env.base
    subject = _subject(env, kind)
    context = base.contexts.anonymous(subject.organization_id)
    # Un intento retenido se audita: el de una cuenta, con su usuario.
    user_id = uuid.UUID(subject.key) if kind is ThrottleSubjectKind.ACCOUNT else None
    model: ThrottleState | None = None
    now = START
    for index, (gap, action) in enumerate(events):
        store = env.stores[index % 2]
        now += gap
        seen = [base.run(s.throttle_state(context, subject)) for s in env.stores]
        assert seen[0] == seen[1] == model
        if action == "success":
            base.run(store.reset_throttle(context, subject, now))
            model = None if model is None else after_success(now)
            continue
        if action == WAIT and model is not None:
            now = max(now, model.next_allowed_at)
        reservation = base.run(store.reserve(context, subject, now, user_id=user_id))
        retry = retry_after_seconds(model, now)
        if retry > 0:
            # Retenido: no cuenta y la fila no cambia.
            assert reservation.outcome is None and reservation.retry_after_seconds == retry
            continue
        expected = after_failure(model, now, threshold=alert_threshold(kind))
        assert reservation.outcome == expected
        model = expected.state
    seen = [base.run(s.throttle_state(context, subject)) for s in env.stores]
    assert seen[0] == seen[1] == model
    assert retry_after_seconds(seen[0], now) == retry_after_seconds(seen[1], now)


@pytest.mark.integration
def test_another_os_process_reads_the_same_delay(env: Env) -> None:
    """Criterio 2 con procesos del sistema operativo de verdad: el estado vive en PostgreSQL."""
    base = env.base
    subject = _subject(env, ThrottleSubjectKind.ACCOUNT)
    context = base.contexts.anonymous(subject.organization_id)
    now = START + timedelta(days=3)
    for _ in range(7):
        outcome = base.run(env.stores[0].reserve(context, subject, now, user_id=None)).outcome
        assert outcome is not None
        now = outcome.state.next_allowed_at
    here = retry_after_seconds(outcome.state, START + timedelta(days=3))
    probe = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.throttle_probe",
            base.migrated.as_role("vigia_app").sqlalchemy_url,
            str(subject.organization_id),
            subject.kind.value,
            subject.key,
            (START + timedelta(days=3)).isoformat(),
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    reported = json.loads(probe.stdout)
    assert reported == {
        "consecutive_failures": 7,
        "next_allowed_at": outcome.state.next_allowed_at.isoformat(),
        "retry_after_seconds": here,
    }
    assert here > 0


@pytest.mark.integration
def test_concurrent_attempts_from_two_processes_are_all_gated(env: Env) -> None:
    """Ocho intentos simultáneos desde dos «instancias»: se serializan en la fila; los cinco
    primeros se reservan (1 a 5) y los tres siguientes ya ven el retardo del quinto."""
    base = env.base
    subject = _subject(env, ThrottleSubjectKind.ORIGIN)
    context = base.contexts.anonymous(subject.organization_id)
    now = START + timedelta(days=5)

    async def burst() -> list[Any]:
        return list(
            await asyncio.gather(
                *(env.stores[i % 2].reserve(context, subject, now, user_id=None) for i in range(8))
            )
        )

    reservations = base.run(burst())
    granted = [r.outcome for r in reservations if r.outcome is not None]
    assert sorted(o.state.consecutive_failures for o in granted) == [1, 2, 3, 4, 5]
    assert [r.retry_after_seconds for r in reservations if r.outcome is None] == [30] * 3
    final = base.run(env.stores[1].throttle_state(context, subject))
    assert final is not None and final.consecutive_failures == 5
    assert final.next_allowed_at == now + expected_delay(5)


# --- Inicio de sesión: quién cuenta y alertas -----------------------------------------------


def _alerts(base: SessionEnvironment, organization_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = base.fetch(
        "SELECT payload FROM shared.outbox_event"
        " WHERE organization_id = $1 AND event_name = 'security_alert' ORDER BY created_at",
        organization_id,
    )
    return [json.loads(row["payload"]) for row in rows]


def _throttle_rows(base: SessionEnvironment, key: str) -> list[Any]:
    return base.fetch(
        "SELECT organization_id, subject_kind, consecutive_failures FROM identity.auth_throttle"
        " WHERE subject_key = $1",
        key,
    )


@pytest.mark.integration
def test_unknown_accounts_count_only_by_origin(env: Env) -> None:
    base = env.base
    login = base.login()
    origin = f"198.51.100.{uuid.uuid4().int % 250}"
    hashed = origin_hash(origin, b"k" * 32)
    before = base.fetch("SELECT count(*) AS n FROM identity.auth_throttle")[0]["n"]
    for attempt in range(4):
        result = base.run(login.authenticate(f"nadie-{attempt}@example.test", "x" * 12, origin))
        assert result == Rejected(RejectionCode.UNAUTHENTICATED, "credenciales inválidas")
    after = base.fetch("SELECT count(*) AS n FROM identity.auth_throttle")[0]["n"]
    assert after == before + 1  # solo la fila del origen
    assert [
        (r["subject_kind"], r["consecutive_failures"]) for r in _throttle_rows(base, hashed)
    ] == [("origin", 4)]


@pytest.mark.integration
def test_account_alert_at_ten_and_origin_alert_at_fifty(env: Env) -> None:
    base = env.base
    organization_id = base.add_organization()
    user = base.add_user(organization_id)
    login = base.login()
    # Diez fallos de la cuenta desde orígenes distintos (el origen no llega a retener).
    for attempt in range(ACCOUNT_ALERT_THRESHOLD + 2):
        state = base.run(
            base.store().throttle_state(
                base.contexts.anonymous(organization_id),
                ThrottleSubject.account(organization_id, user.user_id),
            )
        )
        if state is not None and state.next_allowed_at > base.clock.now():
            base.clock.advance((state.next_allowed_at - base.clock.now()).total_seconds())
        result = base.run(login.authenticate(user.email, "mala", f"192.0.2.{attempt}"))
        assert isinstance(result, Rejected) and result.code is RejectionCode.UNAUTHENTICATED
    alerts = _alerts(base, organization_id)
    assert alerts == [
        {
            "alert_kind": "login_failures_account",
            "resource_kind": "user",
            "resource_id": str(user.user_id),
            "occurred_at": alerts[0]["occurred_at"],
        }
    ]
    # Cincuenta fallos de un mismo origen con cuentas inexistentes: una alerta sin recurso.
    provider = base.seed.provider_organization_id
    before = len(_alerts(base, provider))
    origin = f"203.0.113.{uuid.uuid4().int % 250}"
    subject = ThrottleSubject.origin(provider, origin_hash(origin, b"k" * 32))
    for attempt in range(ORIGIN_ALERT_THRESHOLD + 1):
        state = base.run(base.store().throttle_state(base.contexts.anonymous(provider), subject))
        if state is not None and state.next_allowed_at > base.clock.now():
            base.clock.advance((state.next_allowed_at - base.clock.now()).total_seconds())
        base.run(login.authenticate(f"x{attempt}@example.test", "mala", origin))
    new = _alerts(base, provider)[before:]
    assert [a["alert_kind"] for a in new] == ["login_failures_origin"]
    # El origen no es un recurso: ni su hash ni la dirección viajan en la carga.
    assert new[0]["resource_kind"] is None and new[0]["resource_id"] is None
    assert subject.key not in json.dumps(new[0]) and origin not in json.dumps(new[0])


@pytest.mark.integration
def test_throttled_attempt_is_not_verified_and_reports_retry_after(env: Env) -> None:
    base = env.base
    organization_id = base.add_organization()
    user = base.add_user(organization_id)
    passwords = FakePasswords()
    login = base.login(passwords=passwords)
    origin = f"192.0.2.{uuid.uuid4().int % 250}"
    for _ in range(5):
        base.run(login.authenticate(user.email, "mala", origin))
    calls = passwords.verify_calls
    # Retenido: ni con la contraseña buena se verifica ni se abre sesión.
    result = base.run(login.authenticate(user.email, user.password, origin))
    assert result == Rejected(
        RejectionCode.THROTTLED,
        "demasiados intentos; espera antes de volver a intentar",
        30,
    )
    assert passwords.verify_calls == calls
    audit = base.fetch(
        "SELECT operation FROM shared.audit_entry WHERE organization_id = $1"
        " AND resource_id = $2 ORDER BY chain_sequence",
        organization_id,
        user.user_id,
    )
    assert [a["operation"] for a in audit] == ["login_failed"] * 5 + ["login_throttled"]
    base.clock.advance(30)
    assert not isinstance(base.run(login.authenticate(user.email, user.password, origin)), Rejected)


# --- Reserva antes de verificar: cuenta, segundo paso y ráfagas ----------------------------------


class SlowPasswords(FakePasswords):
    """``FakePasswords`` que tarda como Argon2id: deja a la ráfaga solaparse."""

    async def verify(self, password: str, encoded: str) -> VerifyResult:
        result = await super().verify(password, encoded)
        await asyncio.sleep(0.3)
        return result


class CountingSecondFactor(FakeSecondFactor):
    """``FakeSecondFactor`` que cuenta (y hace lentas) las verificaciones de código."""

    def __init__(self, delay: float = 0.0) -> None:
        self.verify_calls = 0
        self.delay = delay

    async def verify_totp(
        self, context: ScopeContext, credential: TotpCredential, code: str, now: datetime
    ) -> bool:
        self.verify_calls += 1
        await asyncio.sleep(self.delay)
        return await super().verify_totp(context, credential, code, now)


def _account_state(base: SessionEnvironment, user: User) -> ThrottleState | None:
    subject = ThrottleSubject.account(user.organization_id, user.user_id)
    state: ThrottleState | None = base.run(
        base.store().throttle_state(base.contexts.anonymous(user.organization_id), subject)
    )
    return state


def _origin_state(base: SessionEnvironment, origin: str) -> ThrottleState | None:
    provider = base.seed.provider_organization_id
    subject = ThrottleSubject.origin(provider, origin_hash(origin, ORIGIN_KEY))
    state: ThrottleState | None = base.run(
        base.store().throttle_state(base.contexts.anonymous(provider), subject)
    )
    return state


def _pending(base: SessionEnvironment, login: LoginService, user: User) -> SessionCookie:
    origin = f"192.0.2.{uuid.uuid4().int % 250}"
    result = base.run(login.authenticate(user.email, user.password, origin))
    assert isinstance(result, SecondFactorRequired)
    return result.cookie


@pytest.mark.integration
def test_account_delay_holds_from_any_new_origin(env: Env) -> None:
    """(a) Cinco fallos desde cinco orígenes: el sexto, desde otro origen y con la contraseña
    buena, queda retenido por la cuenta sin verificar; el origen nuevo no se queda el fallo."""
    base = env.base
    user = base.add_user(base.add_organization())
    passwords = FakePasswords()
    login = base.login(passwords=passwords)
    prefix = f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    for attempt in range(5):
        result = base.run(login.authenticate(user.email, "mala", f"{prefix}.{attempt}"))
        assert result == Rejected(RejectionCode.UNAUTHENTICATED, "credenciales inválidas")
    calls = passwords.verify_calls
    fresh = f"{prefix}.99"
    result = base.run(login.authenticate(user.email, user.password, fresh))
    assert result == Rejected(
        RejectionCode.THROTTLED, "demasiados intentos; espera antes de volver a intentar", 30
    )
    assert passwords.verify_calls == calls
    state = _account_state(base, user)
    assert state is not None and state.consecutive_failures == 5
    # Un intento retenido no cuenta: la reserva del origen nuevo se devolvió.
    origin_state = _origin_state(base, fresh)
    assert origin_state is not None and origin_state.consecutive_failures == 0


@pytest.mark.integration
def test_second_factor_failures_count_on_the_account_and_hold_a_good_code(env: Env) -> None:
    """(b) y (c) Cinco códigos erróneos suben ``consecutive_failures`` de la cuenta; el siguiente,
    aunque sea bueno, queda retenido con ``retry_after_seconds`` y no llega a ``verify_totp``."""
    base = env.base
    user = base.add_user(base.add_organization(), required=True, enrolled=True)
    second_factor = CountingSecondFactor()
    login = base.login(second_factor=second_factor)
    cookie = _pending(base, login, user)
    before = _account_state(base, user)
    assert before is None or before.consecutive_failures == 0
    for attempt in range(1, 6):
        result = base.run(login.verify_second_factor(cookie, "000000"))
        assert result == Rejected(RejectionCode.UNAUTHENTICATED, "credenciales inválidas")
        state = _account_state(base, user)
        assert state is not None and state.consecutive_failures == attempt
    assert second_factor.verify_calls == 5
    held = base.run(login.verify_second_factor(cookie, GOOD_CODE))
    assert held == Rejected(
        RejectionCode.THROTTLED, "demasiados intentos; espera antes de volver a intentar", 30
    )
    assert second_factor.verify_calls == 5
    base.clock.advance(30)
    assert isinstance(base.run(login.verify_second_factor(cookie, GOOD_CODE)), Authenticated)
    state = _account_state(base, user)
    assert state is not None and state.consecutive_failures == 0


@pytest.mark.integration
def test_success_returns_the_origin_reservation(env: Env) -> None:
    """Entrar bien desde un origen no le suma fallos: una oficina tras una sola dirección no se
    retiene por sus propios inicios correctos, y un atacante tampoco limpia su origen así."""
    base = env.base
    organization = base.add_organization()
    plain = base.add_user(organization)
    pending = base.add_user(organization, required=True, enrolled=True)
    login = base.login()
    origin = f"198.18.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"
    for attempt in range(4):
        base.run(login.authenticate(f"nadie-{attempt}@example.test", "x", origin))
    for _ in range(3):
        result = base.run(login.authenticate(plain.email, plain.password, origin))
        assert isinstance(result, Authenticated)
        result = base.run(login.authenticate(pending.email, pending.password, origin))
        assert isinstance(result, SecondFactorRequired)
    state = _origin_state(base, origin)
    assert state is not None and state.consecutive_failures == 4
    assert retry_after_seconds(state, base.clock.now()) == 0
    # La sesión pendiente tampoco deja un fallo en la cuenta.
    account = _account_state(base, pending)
    assert account is not None and account.consecutive_failures == 0


@pytest.mark.integration
def test_concurrent_password_burst_verifies_at_most_five(env: Env) -> None:
    """Veinticinco intentos simultáneos con contraseña errónea desde 25 orígenes sobre una cuenta
    nueva: como mucho cinco llegan a Argon2id; el resto se retiene por la cuenta."""
    base = env.base
    user = base.add_user(base.add_organization())
    passwords = SlowPasswords()
    login = base.login(passwords=passwords)
    prefix = f"172.{16 + uuid.uuid4().int % 16}.{uuid.uuid4().int % 250}"

    async def burst() -> list[Any]:
        return list(
            await asyncio.gather(
                *(login.authenticate(user.email, "mala", f"{prefix}.{i}") for i in range(25))
            )
        )

    results = base.run(burst())
    assert passwords.verify_calls == 5
    codes = [r.code for r in results]
    assert codes.count(RejectionCode.UNAUTHENTICATED) == 5
    assert codes.count(RejectionCode.THROTTLED) == 20
    state = _account_state(base, user)
    assert state is not None and state.consecutive_failures == 5


@pytest.mark.integration
def test_concurrent_code_burst_verifies_at_most_five(env: Env) -> None:
    """Veinticinco códigos simultáneos sobre una sesión pendiente: como mucho cinco se verifican."""
    base = env.base
    user = base.add_user(base.add_organization(), required=True, enrolled=True)
    second_factor = CountingSecondFactor(delay=0.3)
    login = base.login(second_factor=second_factor)
    cookie = _pending(base, login, user)

    async def burst() -> list[Any]:
        return list(
            await asyncio.gather(*(login.verify_second_factor(cookie, "000000") for _ in range(25)))
        )

    results = base.run(burst())
    assert second_factor.verify_calls == 5
    codes = [r.code for r in results]
    assert codes.count(RejectionCode.UNAUTHENTICATED) == 5
    assert codes.count(RejectionCode.THROTTLED) == 20
    state = _account_state(base, user)
    assert state is not None and state.consecutive_failures == 5
