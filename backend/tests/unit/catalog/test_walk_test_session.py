"""Reglas puras de la sesión de walk-test y sus pasos (TASK-214; BR-GOB-35 a 38, 44 a 47, 50).

Bordes de cada regla: ``passes_per_cell`` en 2, 3, 1000 y 1001; ``expire_if_inactive`` a 7 días
menos un milisegundo y a 7 días exactos (reloj simulado, ``>=``); reapertura solo desde
``incomplete``; corrección sin marcas, con marca después del cierre, con duración negativa o de
más de un año; matriz máxima del contrato (32 estándares) dentro del tope de filas del acta.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from vigia_platform.catalog.domain.enums import (
    PassResult,
    Posture,
    StepKind,
    WalkTestKind,
    WalkTestStatus,
)
from vigia_platform.catalog.domain.steps import (
    MAX_STEP_DURATION_MS,
    CorrectionRequest,
    StepAlreadyClosed,
    StepRequestInvalid,
    WalkTestStep,
    close_step,
    steps_summary,
    total_hours,
)
from vigia_platform.catalog.domain.walk_test import (
    INACTIVITY_LIMIT,
    MAX_PASSES_PER_CELL,
    WalkTestPass,
    WalkTestRuleViolated,
    WalkTestSession,
    WalkTestViolation,
    check_operable,
    check_passes_per_cell,
    expire_if_inactive,
    open_session,
    pass_counts,
    predicate_condition,
    reopen,
    session_rows,
)
from vigia_platform.catalog.record_types import MAX_MATRIX_ROWS

T0: Final = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
MS: Final = timedelta(milliseconds=1)
USER: Final = uuid.UUID(int=7, version=4)
REASON: Final = "Se olvidó cerrar el paso al terminar"


def _standard(index: int, conditions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "standard_id": str(uuid.UUID(int=index + 1, version=4)),
        "version": 1,
        "predicate": {
            "all_of": conditions
            or [{"presence": True}, {"signal_role": "energy", "value": "asserted"}]
        },
    }


def _session(**changes: Any) -> WalkTestSession:
    session = open_session(
        session_id=uuid.UUID(int=1, version=4),
        organization_id=uuid.UUID(int=2, version=4),
        plant_id=uuid.UUID(int=3, version=4),
        zone_id=uuid.UUID(int=4, version=4),
        node_id=uuid.UUID(int=5, version=4),
        catalog_version=1,
        catalog={"standards": [_standard(0)]},
        passes_per_cell=3,
        at=T0,
    )
    return dataclasses.replace(session, **changes)


def _step(**changes: Any) -> WalkTestStep:
    step = WalkTestStep(
        step_id=uuid.UUID(int=10, version=4),
        organization_id=uuid.UUID(int=2, version=4),
        plant_id=uuid.UUID(int=3, version=4),
        session_id=uuid.UUID(int=1, version=4),
        step_kind=StepKind.FRAMING,
        responsible_user_id=USER,
        started_at=T0,
    )
    return dataclasses.replace(step, **changes)


# --- Pases por celda y matriz ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "violation"),
    [
        (2, WalkTestViolation.PASSES_BELOW_MINIMUM),
        (0, WalkTestViolation.PASSES_BELOW_MINIMUM),
        (-1, WalkTestViolation.PASSES_BELOW_MINIMUM),
        (MAX_PASSES_PER_CELL + 1, WalkTestViolation.REQUEST_INVALID),
        (2**63, WalkTestViolation.REQUEST_INVALID),
        (True, WalkTestViolation.REQUEST_INVALID),
        (3.0, WalkTestViolation.REQUEST_INVALID),
        ("3", WalkTestViolation.REQUEST_INVALID),
    ],
)
def test_passes_per_cell_out_of_bounds(value: object, violation: WalkTestViolation) -> None:
    with pytest.raises(WalkTestRuleViolated) as raised:
        check_passes_per_cell(value)
    assert raised.value.violation is violation


@pytest.mark.parametrize("value", [3, 4, MAX_PASSES_PER_CELL])
def test_passes_per_cell_within_bounds(value: int) -> None:
    assert check_passes_per_cell(value) == value


def test_the_largest_matrix_of_the_contract_fits_in_the_record() -> None:
    """32 estándares (máximo del ``ZoneCatalog``) por 4 posturas: 128 filas ≤ 1 024 (nota de la
    sesión de control sobre el tope de ``walk_test_result``)."""
    rows = session_rows({"standards": [_standard(index) for index in range(32)]}, 3)
    assert len(rows) == 32 * len(Posture) == 128 <= MAX_MATRIX_ROWS


def test_matrix_rows_carry_the_conditions_in_the_predicate_grammar() -> None:
    (row, *_) = session_rows(
        {"standards": [_standard(0, [{"signal_role": "guard", "value": "open"}])]}, 3
    )
    assert row.to_json()["predicate_conditions"] == [{"signal_role": "guard", "value": "open"}]
    assert predicate_condition(("presence", "true")) == {"presence": True}


# --- Inactividad y reapertura ---------------------------------------------------------------


def test_expire_if_inactive_at_seven_days_exactly_and_not_before() -> None:
    session = _session()
    almost = expire_if_inactive(session, T0 + INACTIVITY_LIMIT - MS)
    assert almost is session and almost.status is WalkTestStatus.IN_PROGRESS
    expired = expire_if_inactive(session, T0 + INACTIVITY_LIMIT)
    assert expired.status is WalkTestStatus.INCOMPLETE
    # No se borra nada: solo cambia el estado.
    assert dataclasses.replace(expired, status=session.status) == session
    assert timedelta(days=7) == INACTIVITY_LIMIT


def test_activity_restarts_the_seven_days() -> None:
    session = _session(last_activity_at=T0 + timedelta(days=6))
    assert expire_if_inactive(session, T0 + timedelta(days=12)).is_open
    assert not expire_if_inactive(session, T0 + timedelta(days=13)).is_open


@pytest.mark.parametrize("status", [WalkTestStatus.INCOMPLETE, WalkTestStatus.CLOSED])
def test_expire_never_touches_a_session_that_is_not_open(status: WalkTestStatus) -> None:
    session = _session(status=status, closed_at=T0 if status is WalkTestStatus.CLOSED else None)
    assert expire_if_inactive(session, T0 + timedelta(days=365)) is session


def test_a_reopened_session_also_expires() -> None:
    session = _session(status=WalkTestStatus.REOPENED)
    assert expire_if_inactive(session, T0 + INACTIVITY_LIMIT).status is WalkTestStatus.INCOMPLETE


@pytest.mark.parametrize(
    ("status", "violation"),
    [
        (WalkTestStatus.INCOMPLETE, WalkTestViolation.INCOMPLETE),
        (WalkTestStatus.CLOSED, WalkTestViolation.CLOSED),
    ],
)
def test_operations_on_incomplete_or_closed(
    status: WalkTestStatus, violation: WalkTestViolation
) -> None:
    with pytest.raises(WalkTestRuleViolated) as raised:
        check_operable(_session(status=status))
    assert raised.value.violation is violation


def test_reopen_only_from_incomplete_and_keeps_everything() -> None:
    incomplete = _session(status=WalkTestStatus.INCOMPLETE)
    at = T0 + timedelta(days=9)
    reopened = reopen(incomplete, at=at, by=USER, reason_es=REASON)
    assert reopened.status is WalkTestStatus.REOPENED
    assert (reopened.reopened_at, reopened.reopened_by, reopened.reopen_reason_es) == (
        at,
        USER,
        REASON,
    )
    assert reopened.last_activity_at == at
    assert reopened.matrix_rows == incomplete.matrix_rows
    assert reopened.kind is WalkTestKind.INITIAL
    for status, violation in (
        (WalkTestStatus.IN_PROGRESS, WalkTestViolation.NOT_INCOMPLETE),
        (WalkTestStatus.REOPENED, WalkTestViolation.NOT_INCOMPLETE),
        (WalkTestStatus.CLOSED, WalkTestViolation.CLOSED),
    ):
        with pytest.raises(WalkTestRuleViolated) as raised:
            reopen(_session(status=status), at=at, by=USER, reason_es=REASON)
        assert raised.value.violation is violation


# --- Pasos -------------------------------------------------------------------------------------


def test_close_uses_the_server_clock_once() -> None:
    closed = close_step(_step(), at=T0 + timedelta(minutes=5), corrected_by=USER)
    assert closed.ended_at == T0 + timedelta(minutes=5) and closed.correction is None
    assert closed.effective_duration_ms == 5 * 60 * 1000
    with pytest.raises(StepAlreadyClosed):
        close_step(closed, at=T0 + timedelta(minutes=6), corrected_by=USER)


def test_a_server_clock_behind_the_start_never_gives_a_negative_step() -> None:
    closed = close_step(_step(), at=T0 - timedelta(seconds=1), corrected_by=USER)
    assert closed.ended_at == T0 and closed.effective_duration_ms == 0


def test_a_correction_is_appended_and_the_server_marks_stay() -> None:
    at = T0 + timedelta(minutes=30)
    earlier = T0 - timedelta(minutes=10)
    closed = close_step(
        _step(), at=at, corrected_by=USER, correction=CorrectionRequest(REASON, started_at=earlier)
    )
    assert (closed.started_at, closed.ended_at) == (T0, at)
    assert closed.correction is not None
    assert (closed.correction.started_at, closed.correction.ended_at) == (earlier, None)
    assert (closed.correction.corrected_by, closed.correction.corrected_at) == (USER, at)
    assert closed.effective_duration_ms == 40 * 60 * 1000
    content = closed.record_content()
    assert content["started_at"] == "2026-10-01T08:00:00.000Z"
    assert content["correction"]["started_at"] == "2026-10-01T07:50:00.000Z"
    assert "ended_at" not in content["correction"]


@pytest.mark.parametrize(
    "correction",
    [
        CorrectionRequest(REASON),
        CorrectionRequest(REASON, ended_at=T0 - MS),
        CorrectionRequest(REASON, started_at=T0 + timedelta(minutes=5), ended_at=T0),
        CorrectionRequest(REASON, ended_at=T0 + timedelta(minutes=5) + MS),
        CorrectionRequest(REASON, started_at=T0 + timedelta(minutes=5) + MS),
        CorrectionRequest(REASON, started_at=T0 - timedelta(milliseconds=MAX_STEP_DURATION_MS)),
        CorrectionRequest(REASON, started_at=datetime(2026, 10, 1, 7, 0)),  # noqa: DTZ001
    ],
    ids=[
        "sin-marcas",
        "fin-antes-del-inicio",
        "inicio-despues-del-fin",
        "fin-despues-del-cierre",
        "inicio-despues-del-cierre",
        "mas-de-un-ano",
        "sin-zona-horaria",
    ],
)
def test_incoherent_corrections_are_rejected(correction: CorrectionRequest) -> None:
    with pytest.raises(StepRequestInvalid):
        close_step(_step(), at=T0 + timedelta(minutes=5), corrected_by=USER, correction=correction)


def test_a_correction_of_exactly_one_year_is_accepted() -> None:
    at = T0 + timedelta(minutes=5)
    start = at - timedelta(milliseconds=MAX_STEP_DURATION_MS)
    closed = close_step(
        _step(), at=at, corrected_by=USER, correction=CorrectionRequest(REASON, started_at=start)
    )
    assert closed.effective_duration_ms == MAX_STEP_DURATION_MS


def test_hours_count_only_closed_steps_and_group_by_kind() -> None:
    open_step = _step(step_id=uuid.UUID(int=11, version=4))
    first = close_step(_step(), at=T0 + timedelta(minutes=10), corrected_by=USER)
    second = close_step(
        _step(step_id=uuid.UUID(int=12, version=4), responsible_user_id=uuid.uuid4()),
        at=T0 + timedelta(minutes=20),
        corrected_by=USER,
    )
    other = close_step(
        _step(step_id=uuid.UUID(int=13, version=4), step_kind=StepKind.PHYSICAL_SETUP),
        at=T0 + timedelta(minutes=1),
        corrected_by=USER,
    )
    steps = (open_step, first, second, other)
    assert total_hours(steps) == 31 * 60 * 1000
    assert [(h.step_kind, h.duration_ms) for h in steps_summary(steps)] == [
        (StepKind.PHYSICAL_SETUP, 60 * 1000),
        (StepKind.FRAMING, 30 * 60 * 1000),
    ]
    assert total_hours(()) == 0 and steps_summary(()) == ()


def test_pass_counts_cover_every_row_and_ignore_foreign_rows() -> None:
    session = _session()
    rows = session.matrix_rows
    passes = [
        WalkTestPass(
            pass_id=uuid.uuid4(),
            organization_id=session.organization_id,
            plant_id=session.plant_id,
            session_id=session.session_id,
            row_id=row_id,
            result=result,
            evidence_ref=None,
            recorded_by=USER,
            recorded_at=T0,
        )
        for row_id, result in (
            (rows[0].row_id, PassResult.DETECTED),
            (rows[0].row_id, PassResult.MISSED),
            (rows[1].row_id, PassResult.FALSE_ALARM),
            (uuid.uuid4(), PassResult.DETECTED),
        )
    ]
    counts = pass_counts(rows, passes)
    assert list(counts) == [row.row_id for row in rows]
    assert (counts[rows[0].row_id].detected, counts[rows[0].row_id].missed) == (1, 1)
    assert counts[rows[1].row_id].false_alarm == 1
    assert all(counts[row.row_id] == type(counts[row.row_id])() for row in rows[2:])
