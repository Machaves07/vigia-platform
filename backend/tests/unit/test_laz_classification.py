"""Piezas puras de ``loop.classification`` (TASK-308; BLM §1; DE §1, §4.1 y §11.3; BR-LAZ-06).

- ``EFFECT_BY_TRANSITION`` cubre exactamente las filas de §4.1 que ejecuta ``findings.classify``;
- las categorías de cada salida son las del esquema del registro (``loop.record_types``);
- los eventos de cada transición son los de su fila de §4.1 y su carga valida contra el modelo
  registrado del evento (solo identificadores y valores de listas cerradas);
- el motivo en sus bordes (ausente, en blanco, 19, 20, 1000 y 1001 caracteres, categoría de otra
  lista) y la traducción de los rechazos del escritor (los dos validadores de U-04, la política
  base, la cadena ocupada, el duplicado);
- la agregación temporal de presentación: inicio de la ventana, bordes de 15 y 480 minutos.

Solo datos generados.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import LedgerRejection, LedgerRejectionCode, Receipt
from vigia_platform.loop import record_types
from vigia_platform.loop.application import classification as module
from vigia_platform.loop.application.classification import (
    EFFECT_BY_TRANSITION,
    REASONS_BY_OUTCOME,
    ClassificationRejected,
    ClassificationRequestInvalid,
    ClassificationService,
    ClassificationUnavailable,
)
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain.enums import ClassificationOutcome, FindingLifecycleState
from vigia_platform.loop.domain.lifecycle import (
    TRANSITIONS,
    Accepted,
    Classify,
    LifecycleSnapshot,
    transition,
)
from vigia_platform.loop.domain.time_aggregation import bucket_start, presented
from vigia_platform.loop.events import LOOP_EVENT_TYPES

S = FindingLifecycleState
Out = ClassificationOutcome
NOW: Final = datetime(2026, 10, 18, 10, 37, 21, 123000, tzinfo=UTC)
V7: Final = uuid.UUID("01890a5d-ac96-774b-bcce-b302099a8057")


# --- tablas --------------------------------------------------------------------------------------


def test_the_effect_table_covers_exactly_the_rows_of_findings_classify() -> None:
    rows = {
        row.transition_id
        for row in TRANSITIONS.values()
        if PermissionKey.FINDINGS_CLASSIFY in row.permission_keys
    }
    assert set(EFFECT_BY_TRANSITION) == rows == {
        "T03", "T04", "T15", "T16", "T17", "T18", "T19", "T20",
    }  # fmt: skip
    for row_id in rows:
        assert TRANSITIONS[row_id].record_types == ("classification",)


def test_the_reason_lists_are_those_of_the_record_schema() -> None:
    assert dict(REASONS_BY_OUTCOME) == {
        outcome: frozenset(str(value) for value in values)
        for outcome, values in record_types._REASONS_BY_OUTCOME.items()
    }
    for categories in REASONS_BY_OUTCOME.values():
        assert all(type(category) is str for category in categories)


_CASES: Final = {
    "T03": (LifecycleSnapshot(S.RECEIVED), Out.CONFIRMED, None),
    "T04": (LifecycleSnapshot(S.RECEIVED), Out.FALSE_POSITIVE, None),
    "T15": (LifecycleSnapshot(S.OPEN), Out.AUTHORIZED_OPERATION, Out.CONFIRMED),
    "T16": (
        LifecycleSnapshot(S.SUSPENDED_BY_RECLASSIFICATION, S.OPEN),
        Out.CONFIRMED,
        Out.FALSE_POSITIVE,
    ),
    "T17": (LifecycleSnapshot(S.DISCARDED), Out.CONFIRMED, Out.FALSE_POSITIVE),
    "T18": (LifecycleSnapshot(S.DISCARDED), Out.AUTHORIZED_OPERATION, Out.FALSE_POSITIVE),
    "T19": (LifecycleSnapshot(S.CLOSED), Out.FALSE_POSITIVE, Out.CONFIRMED),
    "T20": (LifecycleSnapshot(S.UNCLOSABLE), Out.FALSE_POSITIVE, Out.CONFIRMED),
}


def _subject(snapshot: LifecycleSnapshot, current: Out | None) -> Any:
    return module._Subject(
        finding_state_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        zone_id=uuid.uuid4(),
        subject_kind="finding",
        subject_id=uuid.uuid4(),
        anchor_record_id=uuid.uuid4(),
        family="coexistence",
        standard_id=uuid.uuid4(),
        standard_version=1,
        state=snapshot.state or S.RECEIVED,
        previous_state=snapshot.previous_state,
        current_outcome=current,
        classification_count=0 if current is None else 1,
        due_at=None,
        observability_state_at_event="observable",
        single_occupancy_zone=True,
        aggregation_window_minutes=60,
    )


@pytest.mark.parametrize("transition_id", sorted(_CASES))
def test_each_transition_publishes_the_events_of_its_row_with_a_valid_payload(
    transition_id: str,
) -> None:
    snapshot, outcome, current = _CASES[transition_id]
    accepted = transition(snapshot, Classify(outcome, True, NOW))
    assert isinstance(accepted, Accepted)
    assert accepted.transition.transition_id == transition_id
    events = ClassificationService._events(
        accepted, _subject(snapshot, current), outcome, V7, uuid.uuid4()
    )
    assert tuple(e.event_name for e in events) == TRANSITIONS[transition_id].events
    models = {event_type.event_name: event_type.payload_model for event_type in LOOP_EVENT_TYPES}
    for event in events:
        assert isinstance(event.payload, dict)
        models[event.event_name].model_validate_json(json.dumps(event.payload))
        # Solo identificadores y valores de listas cerradas: nunca texto libre.
        assert set(event.payload) <= {
            "zone_id", "subject_kind", "subject_id", "classification_id", "record_id",
            "outcome", "family", "previous_outcome",
        }  # fmt: skip


# --- motivo --------------------------------------------------------------------------------------


def _text(length: int) -> str:
    base = "Coexistencia observada en el pasillo de carga con la carretilla en marcha "
    return (base * (length // len(base) + 1))[: length - 1] + "x"


@pytest.mark.parametrize(
    ("category", "text", "code"),
    [
        (None, _text(30), LoopDetailCode.REASON_REQUIRED),
        ("real_coexistence", None, LoopDetailCode.REASON_REQUIRED),
        ("real_coexistence", "", LoopDetailCode.REASON_REQUIRED),
        ("real_coexistence", " \t\n\u3000 ", LoopDetailCode.REASON_REQUIRED),
        ("  ", _text(30), LoopDetailCode.REASON_REQUIRED),
        ("real_coexistence", _text(19), LoopDetailCode.REASON_TOO_SHORT),
        ("real_coexistence", "   " + _text(19) + "   ", LoopDetailCode.REASON_TOO_SHORT),
    ],
)
def test_reason_rejections(category: object, text: object, code: LoopDetailCode) -> None:
    with pytest.raises(ClassificationRejected) as raised:
        module._reason(Out.CONFIRMED, category, text)
    assert raised.value.detail_code is code


@pytest.mark.parametrize(
    ("outcome", "category", "text"),
    [
        (Out.CONFIRMED, "reflection_or_glare", _text(30)),
        (Out.FALSE_POSITIVE, "real_coexistence", _text(30)),
        (Out.CONFIRMED, "Real_Coexistence", _text(30)),
        (Out.CONFIRMED, 7, _text(30)),
        (Out.CONFIRMED, "real_coexistence", 12345678901234567890),
        (Out.CONFIRMED, "real_coexistence", _text(1001)),
        (Out.CONFIRMED, "real_coexistence", "Motivo con \u0007 control en la zona de carga"),
    ],
)
def test_reason_shape_errors_are_invalid_requests(
    outcome: Out, category: object, text: object
) -> None:
    with pytest.raises(ClassificationRequestInvalid):
        module._reason(outcome, category, text)


@pytest.mark.parametrize("length", [20, 1000])
def test_reason_borders_are_accepted(length: int) -> None:
    category, text = module._reason(Out.FALSE_POSITIVE, "no_person_present", _text(length))
    assert (category, len(text)) == ("no_person_present", length)


@pytest.mark.parametrize("value", [None, 1, "pending", "CONFIRMED", ""])
def test_unknown_outcomes_are_invalid_requests(value: object) -> None:
    with pytest.raises(ClassificationRequestInvalid):
        module._outcome(value)


# --- rechazos del escritor ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detail_code", "code"),
    [
        ("loop_blocked_vocabulary_term", LoopDetailCode.BLOCKED_VOCABULARY_TERM),
        ("loop_person_attribution_detected", LoopDetailCode.PERSON_ATTRIBUTION_DETECTED),
    ],
)
def test_the_two_validators_keep_their_detail_code(detail_code: str, code: LoopDetailCode) -> None:
    rejection = LedgerRejection.of(
        LedgerRejectionCode.FREE_TEXT_REJECTED, "/reason_text", detail_code=detail_code
    )
    with pytest.raises(ClassificationRejected) as raised:
        module._written(rejection)
    assert raised.value.detail_code is code


@pytest.mark.parametrize("detail_code", [None, "catalog_contact_data", "loop_reason_required"])
def test_other_free_text_rejections_are_invalid_requests(detail_code: str | None) -> None:
    rejection = LedgerRejection.of(
        LedgerRejectionCode.FREE_TEXT_REJECTED, "/reason_text", detail_code=detail_code
    )
    with pytest.raises(ClassificationRequestInvalid):
        module._written(rejection)


def test_a_busy_chain_is_transient_and_anything_else_is_internal() -> None:
    with pytest.raises(ClassificationUnavailable):
        module._written(LedgerRejection.of(LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT))
    for code in (LedgerRejectionCode.CONTENT_INVALID, LedgerRejectionCode.IDEMPOTENCY_CONFLICT):
        with pytest.raises(RuntimeError):
            module._written(LedgerRejection.of(code))
    duplicate = Receipt(uuid.uuid4(), NOW, AcceptanceStatus.ACCEPTED_DUPLICATE)
    with pytest.raises(RuntimeError):
        module._written(duplicate)
    accepted = Receipt(uuid.uuid4(), NOW, AcceptanceStatus.ACCEPTED)
    assert module._written(accepted) is accepted


# --- agregación temporal --------------------------------------------------------------------------


def test_bucket_borders() -> None:
    assert bucket_start(NOW, 60) == datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
    assert bucket_start(NOW, 15) == datetime(2026, 10, 18, 10, 30, tzinfo=UTC)
    assert bucket_start(NOW, 480) == datetime(2026, 10, 18, 8, 0, tzinfo=UTC)
    for window in (14, 481, 0, -60):
        with pytest.raises(ValueError):
            bucket_start(NOW, window)
    with pytest.raises(ValueError):
        bucket_start(NOW.replace(tzinfo=None), 60)
    start = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
    assert bucket_start(start, 60) == start  # el inicio de la ventana es su propio inicio
    bogota = timezone(timedelta(hours=-5))
    assert bucket_start(NOW.astimezone(bogota), 60) == bucket_start(NOW, 60)


def test_presented_is_exact_outside_single_occupancy() -> None:
    assert presented(NOW, single_occupancy=False, window_minutes=60) == NOW
    assert presented(NOW, single_occupancy=True, window_minutes=60) == bucket_start(NOW, 60)


@given(
    moment=st.datetimes(
        # Hypothesis pide límites sin zona y la añade con ``timezones``.
        min_value=datetime(2000, 1, 1),  # noqa: DTZ001
        max_value=datetime(2100, 1, 1),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
    window=st.integers(15, 480),
)
def test_the_bucket_contains_the_moment_and_starts_its_window(
    moment: datetime, window: int
) -> None:
    start = bucket_start(moment, window)
    assert start <= moment < start + timedelta(minutes=window)
    assert bucket_start(start, window) == start
