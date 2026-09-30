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
- **Concurrencia**: fallos simultáneos de dos procesos no se pierden (``INSERT ... ON CONFLICT``
  con la fila bloqueada).
- **Inicio de sesión**: las cuentas inexistentes cuentan solo por origen; 10 fallos de una cuenta
  publican una ``security_alert`` con ``resource_kind = user`` y 50 de un origen otra sin recurso.

Semillas y ejemplos del perfil activo (``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.session_support import START, FakePasswords, SessionEnvironment, session_environment
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.auth.login import Rejected, RejectionCode
from vigia_platform.identity.auth.sessions import (
    ACCOUNT_ALERT_THRESHOLD,
    ORIGIN_ALERT_THRESHOLD,
    THROTTLE_MAX_DELAY,
    ThrottleState,
    ThrottleSubject,
    ThrottleSubjectKind,
    after_failure,
    after_success,
    alert_threshold,
    origin_hash,
    retry_after_seconds,
    throttle_delay,
    window_expired,
)
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
        new_window = state is None or window_expired(state, now)
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
        if retry_after_seconds(model, now) > 0:
            continue
        outcome = base.run(store.record_failure(context, subject, now, audit=None, user_id=None))
        expected = after_failure(model, now, threshold=alert_threshold(kind))
        assert outcome == expected
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
        outcome = base.run(
            env.stores[0].record_failure(context, subject, now, audit=None, user_id=None)
        )
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
def test_concurrent_failures_from_two_processes_are_all_counted(env: Env) -> None:
    base = env.base
    subject = _subject(env, ThrottleSubjectKind.ORIGIN)
    context = base.contexts.anonymous(subject.organization_id)
    now = START + timedelta(days=5)

    async def burst() -> list[Any]:
        return list(
            await asyncio.gather(
                *(
                    env.stores[i % 2].record_failure(
                        context, subject, now, audit=None, user_id=None
                    )
                    for i in range(8)
                )
            )
        )

    outcomes = base.run(burst())
    assert sorted(o.state.consecutive_failures for o in outcomes) == list(range(1, 9))
    final = base.run(env.stores[1].throttle_state(context, subject))
    assert final is not None and final.consecutive_failures == 8
    # El retardo nunca retrocede aunque los fallos lleguen a la vez.
    assert final.next_allowed_at == now + expected_delay(8)


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
