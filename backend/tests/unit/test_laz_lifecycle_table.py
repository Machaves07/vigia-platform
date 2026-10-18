"""La máquina de estados del hallazgo es exactamente la tabla de DE §4.1 y §4.2 (TASK-305).

BR-LAZ-36: una transición que no figura en §4.1 se rechaza con ``transition_not_allowed``, sin
efecto, salvo donde §1.2 y §4.2 fijan un código de negocio. Esta prueba:

- contrasta las 21 filas de ``TRANSITIONS`` con la transcripción literal de §4.1 (origen,
  destino, claves de permiso, registros y eventos);
- recorre **todos** los pares (estado de origen, comando) — los nueve estados más «—» por cada
  variante de comando que distingue la tabla — contra un **oráculo** escrito aparte desde §4.1 y
  §4.2, y comprueba el destino o el código exacto;
- comprueba cada una de las 16 filas de §4.2 (el ticket dice 15; el documento enumera 16) por su
  propia vía: código de rechazo, ausencia de comando o ausencia de clave en la matriz de U-02;
- comprueba el recálculo de T16 por ``due_at`` y que cada código es un ``detail_code`` de U-04.
"""

from __future__ import annotations

import dataclasses
import typing
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from vigia_platform.identity.authz.matrix import PermissionKey, roles_with
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.loop.domain import lifecycle
from vigia_platform.loop.domain.enums import (
    ClassificationOutcome,
    FindingLifecycleState,
    ReviewResolutionOutcome,
    UnclosableReviewOutcome,
)
from vigia_platform.loop.domain.lifecycle import (
    FORBIDDEN_TRANSITIONS,
    TRANSITIONS,
    Accepted,
    AssignAction,
    AttachLateSegment,
    Classify,
    Close,
    DeclareOverdue,
    DeclareUnclosable,
    Destination,
    LifecycleCode,
    LifecycleSnapshot,
    PolicyStatus,
    ReceiveDetection,
    ReceiveFinding,
    Rejected,
    Reopen,
    ResolveReview,
    ReviewUnclosable,
    transition,
)
from vigia_platform.shared.context import Role

S = FindingLifecycleState
K = PermissionKey
C = LifecycleCode
AT: Final = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# --- transcripción literal de §4.1 ----------------------------------------------------------

ACTIVE = {S.OPEN, S.ACTION_ASSIGNED, S.OVERDUE}
DOC_41: Final = {
    "T01": ({None}, S.RECEIVED, (), (), ()),
    "T02": ({None}, S.IN_REVIEW, (), (), ()),
    "T03": (
        {S.RECEIVED},
        S.OPEN,
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("finding_confirmed",),
    ),
    "T04": (
        {S.RECEIVED},
        S.DISCARDED,
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("finding_discarded",),
    ),
    "T05": (
        {S.IN_REVIEW},
        S.OPEN,
        (K.REVIEW_QUEUE_RESOLVE,),
        ("review_resolution",),
        ("review_confirmed",),
    ),
    "T06": (
        {S.IN_REVIEW},
        S.DISCARDED,
        (K.REVIEW_QUEUE_RESOLVE,),
        ("review_resolution",),
        ("review_discarded",),
    ),
    "T07": (
        {S.OPEN},
        S.ACTION_ASSIGNED,
        (K.ACTIONS_MANAGE,),
        ("action_assigned",),
        ("action_assigned",),
    ),
    "T08": ({S.ACTION_ASSIGNED}, S.OVERDUE, (), ("action_overdue",), ("finding_overdue",)),
    "T09": (
        {S.ACTION_ASSIGNED, S.OVERDUE},
        S.CLOSED,
        (K.FINDINGS_CLOSE_AND_SIGN,),
        ("closure",),
        ("finding_closed",),
    ),
    "T10": (
        ACTIVE,
        S.UNCLOSABLE,
        (K.FINDINGS_CLOSE_AND_SIGN,),
        ("unclosable_state_declared",),
        ("finding_unclosable",),
    ),
    "T11": (
        {S.UNCLOSABLE},
        S.UNCLOSABLE,
        (K.FINDINGS_CLOSE_AND_SIGN,),
        ("unclosable_review",),
        ("unclosable_reviewed",),
    ),
    "T12": (
        {S.UNCLOSABLE},
        S.ACTION_ASSIGNED,
        (K.FINDINGS_CLOSE_AND_SIGN, K.ACTIONS_MANAGE),
        ("unclosable_review", "action_assigned"),
        ("unclosable_reviewed", "action_assigned"),
    ),
    "T13": (
        {S.CLOSED},
        S.ACTION_ASSIGNED,
        (K.ACTIONS_MANAGE,),
        ("reopening", "action_assigned"),
        ("finding_reopened", "action_assigned"),
    ),
    "T14": (
        {S.UNCLOSABLE},
        S.ACTION_ASSIGNED,
        (K.ACTIONS_MANAGE,),
        ("reopening", "action_assigned"),
        ("finding_reopened", "action_assigned"),
    ),
    "T15": (
        ACTIVE,
        S.SUSPENDED_BY_RECLASSIFICATION,
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("loop_suspended",),
    ),
    "T16": (
        {S.SUSPENDED_BY_RECLASSIFICATION},
        "anterior",
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("loop_resumed",),
    ),
    "T17": (
        {S.DISCARDED},
        S.OPEN,
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("finding_confirmed",),
    ),
    "T18": (
        {S.DISCARDED},
        S.DISCARDED,
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("finding_reclassified",),
    ),
    "T19": (
        {S.CLOSED},
        S.CLOSED,
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("finding_reclassified",),
    ),
    "T20": (
        {S.UNCLOSABLE},
        S.UNCLOSABLE,
        (K.FINDINGS_CLASSIFY,),
        ("classification",),
        ("finding_reclassified",),
    ),
    "T21": (
        set(S),
        "mismo",
        (),
        ("late_segment_attached",),
        ("late_segment_attached",),
    ),
}


def test_the_table_is_the_21_rows_of_section_4_1() -> None:
    assert sorted(TRANSITIONS) == [f"T{n:02d}" for n in range(1, 22)]
    for transition_id, (origins, destination, keys, records, events) in DOC_41.items():
        row = TRANSITIONS[transition_id]
        assert row.transition_id == transition_id
        assert row.origins == frozenset(origins), transition_id
        if destination == "anterior":
            assert row.destination is Destination.PREVIOUS
        elif destination == "mismo" or row.destination is Destination.SAME:
            assert row.destination is Destination.SAME
            assert destination == "mismo" or {destination} == set(origins), transition_id
        else:
            assert row.destination is destination, transition_id
        assert row.permission_keys == keys, transition_id
        assert row.record_types == records, transition_id
        assert row.events == events, transition_id


# --- el oráculo: cada par (origen, comando) ---------------------------------------------------

CONFIRMED, AUTHORIZED, FALSE_POSITIVE = (
    ClassificationOutcome.CONFIRMED,
    ClassificationOutcome.AUTHORIZED_OPERATION,
    ClassificationOutcome.FALSE_POSITIVE,
)


def _unclosable(**changes: object) -> DeclareUnclosable:
    base = DeclareUnclosable(
        fields_complete=True,
        policy=PolicyStatus.LOADED,
        review_date_in_range=True,
        signer_has_license=True,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


COMMANDS: Final[dict[str, lifecycle.Command]] = {
    "receive_finding": ReceiveFinding(),
    "receive_detection": ReceiveDetection(),
    "late_segment": AttachLateSegment(),
    "classify_confirmed_reinforced": Classify(CONFIRMED, True, AT),
    "classify_confirmed_plain": Classify(CONFIRMED, False, AT),
    "classify_authorized_reinforced": Classify(AUTHORIZED, True, AT),
    "classify_authorized_plain": Classify(AUTHORIZED, False, AT),
    "classify_false_positive_reinforced": Classify(FALSE_POSITIVE, True, AT),
    "classify_false_positive_plain": Classify(FALSE_POSITIVE, False, AT),
    "review_confirmed": ResolveReview(ReviewResolutionOutcome.REVIEW_CONFIRMED),
    "review_discarded": ResolveReview(ReviewResolutionOutcome.REVIEW_DISCARDED),
    "assign": AssignAction(),
    "overdue": DeclareOverdue(),
    "close_ok": Close(evidence_entry_count=1, signer_has_license=True),
    "close_no_evidence": Close(evidence_entry_count=0, signer_has_license=True),
    "close_no_license": Close(evidence_entry_count=2, signer_has_license=False),
    "unclosable_ok": _unclosable(),
    "unclosable_incomplete": _unclosable(fields_complete=False),
    "unclosable_not_signed": _unclosable(policy=PolicyStatus.NOT_SIGNED),
    "unclosable_revoked": _unclosable(policy=PolicyStatus.REVOKED),
    "unclosable_unavailable": _unclosable(policy=PolicyStatus.UNAVAILABLE),
    "unclosable_bad_date": _unclosable(review_date_in_range=False),
    "unclosable_no_license": _unclosable(signer_has_license=False),
    "review_renewed": ReviewUnclosable(UnclosableReviewOutcome.RENEWED),
    "review_converted": ReviewUnclosable(UnclosableReviewOutcome.CONVERTED_TO_ACTION),
    "reopen_ok": Reopen(reason_given=True),
    "reopen_no_reason": Reopen(reason_given=False),
}

DISCARD_REINFORCED = ("classify_authorized_reinforced", "classify_false_positive_reinforced")
DISCARD_PLAIN = ("classify_authorized_plain", "classify_false_positive_plain")
ALL_CLASSIFY_REINFORCED = ("classify_confirmed_reinforced", *DISCARD_REINFORCED)
ALL_CLASSIFY_PLAIN = ("classify_confirmed_plain", *DISCARD_PLAIN)
CLOSE_VARIANTS = ("close_ok", "close_no_evidence", "close_no_license")

ACCEPTED: dict[tuple[S | None, str], tuple[str, S]] = {
    (None, "receive_finding"): ("T01", S.RECEIVED),
    (None, "receive_detection"): ("T02", S.IN_REVIEW),
    (S.RECEIVED, "classify_confirmed_reinforced"): ("T03", S.OPEN),
    (S.RECEIVED, "classify_confirmed_plain"): ("T03", S.OPEN),
    **{
        (S.RECEIVED, label): ("T04", S.DISCARDED) for label in (*DISCARD_REINFORCED, *DISCARD_PLAIN)
    },
    (S.IN_REVIEW, "review_confirmed"): ("T05", S.OPEN),
    (S.IN_REVIEW, "review_discarded"): ("T06", S.DISCARDED),
    (S.OPEN, "assign"): ("T07", S.ACTION_ASSIGNED),
    (S.ACTION_ASSIGNED, "overdue"): ("T08", S.OVERDUE),
    (S.ACTION_ASSIGNED, "close_ok"): ("T09", S.CLOSED),
    (S.OVERDUE, "close_ok"): ("T09", S.CLOSED),
    **{(state, "unclosable_ok"): ("T10", S.UNCLOSABLE) for state in ACTIVE},
    (S.UNCLOSABLE, "review_renewed"): ("T11", S.UNCLOSABLE),
    (S.UNCLOSABLE, "review_converted"): ("T12", S.ACTION_ASSIGNED),
    (S.CLOSED, "reopen_ok"): ("T13", S.ACTION_ASSIGNED),
    (S.UNCLOSABLE, "reopen_ok"): ("T14", S.ACTION_ASSIGNED),
    **{
        (state, label): ("T15", S.SUSPENDED_BY_RECLASSIFICATION)
        for state in ACTIVE
        for label in DISCARD_REINFORCED
    },
    # T16 con el anterior ``open`` (el recálculo por ``due_at`` va en su propia prueba).
    (S.SUSPENDED_BY_RECLASSIFICATION, "classify_confirmed_reinforced"): ("T16", S.OPEN),
    (S.DISCARDED, "classify_confirmed_reinforced"): ("T17", S.OPEN),
    **{(S.DISCARDED, label): ("T18", S.DISCARDED) for label in DISCARD_REINFORCED},
    **{(S.CLOSED, label): ("T19", S.CLOSED) for label in ALL_CLASSIFY_REINFORCED},
    **{(S.UNCLOSABLE, label): ("T20", S.UNCLOSABLE) for label in ALL_CLASSIFY_REINFORCED},
    **{(state, "late_segment"): ("T21", state) for state in S},
}
"""§4.1 expandida por variante de comando."""

CODED: dict[tuple[S | None, str], C] = {
    # Reclasificar exige motivo reforzado (§2.3): donde la reforzada se aceptaría.
    **{(state, label): C.REASON_REQUIRED for state in ACTIVE for label in DISCARD_PLAIN},
    (S.SUSPENDED_BY_RECLASSIFICATION, "classify_confirmed_plain"): C.REASON_REQUIRED,
    **{
        (state, label): C.REASON_REQUIRED
        for state in (S.CLOSED, S.UNCLOSABLE)
        for label in ALL_CLASSIFY_PLAIN
    },
    (S.DISCARDED, "classify_confirmed_plain"): C.REASON_REQUIRED,
    **{(S.DISCARDED, label): C.REASON_REQUIRED for label in DISCARD_PLAIN},
    # F02 y F03: sin acción asignada no hay cierre.
    **{
        (state, label): C.CLOSURE_REQUIRES_ASSIGNED_ACTION
        for state in (S.RECEIVED, S.IN_REVIEW, S.OPEN, S.DISCARDED)
        for label in CLOSE_VARIANTS
    },
    # F04: con acción, sin evidencia o sin licencia.
    **{(state, "close_no_evidence"): C.CLOSURE_EVIDENCE_MISSING for state in ACTIVE - {S.OPEN}},
    **{(state, "close_no_license"): C.SIGNER_LICENSE_MISSING for state in ACTIVE - {S.OPEN}},
    # T10 y F09, F10: cada exigencia de la declaración con su código.
    **{
        (state, label): code
        for state in ACTIVE
        for label, code in (
            ("unclosable_incomplete", C.UNCLOSABLE_FIELDS_INCOMPLETE),
            ("unclosable_not_signed", C.UNCLOSABLE_POLICY_NOT_SIGNED),
            ("unclosable_revoked", C.UNCLOSABLE_POLICY_REVOKED),
            ("unclosable_unavailable", C.UNCLOSABLE_POLICY_UNAVAILABLE),
            ("unclosable_bad_date", C.UNCLOSABLE_REVIEW_DATE_OUT_OF_RANGE),
            ("unclosable_no_license", C.SIGNER_LICENSE_MISSING),
        )
    },
    (S.CLOSED, "reopen_no_reason"): C.REOPENING_REASON_REQUIRED,
    (S.UNCLOSABLE, "reopen_no_reason"): C.REOPENING_REASON_REQUIRED,
}
"""Los rechazos con código de negocio (§1.2 y §4.2); todo lo demás es ``transition_not_allowed``."""

ORIGINS: Final[tuple[S | None, ...]] = (None, *S)
SUSPENDED_FROM_OPEN: Final = LifecycleSnapshot(S.SUSPENDED_BY_RECLASSIFICATION, S.OPEN)


def _snapshot(state: S | None) -> LifecycleSnapshot:
    if state is S.SUSPENDED_BY_RECLASSIFICATION:
        return SUSPENDED_FROM_OPEN
    return LifecycleSnapshot(state)


def test_oracle_has_no_overlap_and_covers_all_21_rows() -> None:
    assert not set(ACCEPTED) & set(CODED)
    assert {transition_id for transition_id, _ in ACCEPTED.values()} == set(TRANSITIONS)


@pytest.mark.parametrize("label", sorted(COMMANDS))
@pytest.mark.parametrize("origin", ORIGINS, ids=lambda state: "none" if state is None else state)
def test_every_origin_and_command_pair_matches_the_oracle(origin: S | None, label: str) -> None:
    snapshot = _snapshot(origin)
    result = transition(snapshot, COMMANDS[label])
    if (origin, label) in ACCEPTED:
        expected_id, expected_state = ACCEPTED[(origin, label)]
        assert isinstance(result, Accepted), result
        assert result.transition.transition_id == expected_id
        assert result.state is expected_state
        if expected_id == "T15":
            assert result.previous_state is origin
        elif expected_state is S.SUSPENDED_BY_RECLASSIFICATION:
            # T21 sobre un suspendido conserva el estado que T16 reanudará.
            assert result.previous_state is S.OPEN
        else:
            assert result.previous_state is None
    else:
        expected_code = CODED.get((origin, label), C.TRANSITION_NOT_ALLOWED)
        assert result == Rejected(expected_code)


def test_open_to_closed_does_not_exist() -> None:
    """F03, el caso que la mutación de la tabla debe hacer caer."""
    result = transition(LifecycleSnapshot(S.OPEN), Close(1, True))
    assert result == Rejected(C.CLOSURE_REQUIRES_ASSIGNED_ACTION)


def test_late_segment_on_a_suspended_keeps_its_previous_state() -> None:
    snapshot = LifecycleSnapshot(S.SUSPENDED_BY_RECLASSIFICATION, S.OVERDUE, AT)
    result = transition(snapshot, AttachLateSegment())
    assert isinstance(result, Accepted)
    assert (result.state, result.previous_state) == (S.SUSPENDED_BY_RECLASSIFICATION, S.OVERDUE)


# --- T16: el estado anterior, con el vencimiento recalculado ----------------------------------


@pytest.mark.parametrize(
    ("previous", "due_offset", "expected"),
    [
        (S.OPEN, None, S.OPEN),
        (S.ACTION_ASSIGNED, timedelta(days=1), S.ACTION_ASSIGNED),
        (S.ACTION_ASSIGNED, timedelta(0), S.OVERDUE),
        (S.ACTION_ASSIGNED, -timedelta(milliseconds=1), S.OVERDUE),
        (S.OVERDUE, -timedelta(days=3), S.OVERDUE),
        (S.OVERDUE, timedelta(days=3), S.ACTION_ASSIGNED),
    ],
)
def test_t16_resumes_the_previous_state_with_overdue_recomputed(
    previous: S, due_offset: timedelta | None, expected: S
) -> None:
    due_at = None if due_offset is None else AT + due_offset
    snapshot = LifecycleSnapshot(S.SUSPENDED_BY_RECLASSIFICATION, previous, due_at)
    result = transition(snapshot, Classify(CONFIRMED, True, AT))
    assert isinstance(result, Accepted)
    assert result.transition.transition_id == "T16"
    assert (result.state, result.previous_state) == (expected, None)


def test_a_suspended_without_previous_state_is_an_inconsistency() -> None:
    with pytest.raises(ValueError, match="anterior"):
        transition(
            LifecycleSnapshot(S.SUSPENDED_BY_RECLASSIFICATION), Classify(CONFIRMED, True, AT)
        )


# --- §4.2, fila por fila ----------------------------------------------------------------------

FORBIDDEN_WORDS: Final = ("delete", "archive", "hide", "expire", "age", "edit", "withdraw", "reev")


def _command_types() -> tuple[type, ...]:
    return typing.get_args(lifecycle.Command.__value__)


def test_section_4_2_has_its_16_rows() -> None:
    assert [row.forbidden_id for row in FORBIDDEN_TRANSITIONS] == [
        f"F{n:02d}" for n in range(1, 17)
    ]


def test_f01_f11_no_command_deletes_archives_hides_or_edits() -> None:
    names = [command.__name__.lower() for command in _command_types()]
    assert len(names) == 11
    assert not [name for name in names if any(word in name for word in FORBIDDEN_WORDS)]


@pytest.mark.parametrize("origin", [S.IN_REVIEW, S.RECEIVED])
def test_f05_f06_nothing_is_discarded_without_a_human_decision(origin: S) -> None:
    rows = [
        row
        for row in TRANSITIONS.values()
        if origin in row.origins and row.destination is S.DISCARDED
    ]
    assert rows
    assert all(row.permission_keys for row in rows)


@pytest.mark.parametrize("origin", [S.OVERDUE, S.UNCLOSABLE])
def test_f07_f08_the_system_never_moves_an_overdue_or_unclosable(origin: S) -> None:
    system_rows = [
        row for row in TRANSITIONS.values() if origin in row.origins and not row.permission_keys
    ]
    assert {row.transition_id for row in system_rows} == {"T21"}
    assert all(row.destination is Destination.SAME for row in system_rows)
    assert not [
        row for row in TRANSITIONS.values() if origin in row.origins and row.destination is S.OPEN
    ]


@pytest.mark.parametrize("origin", ORIGINS, ids=lambda state: "none" if state is None else state)
@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("unclosable_not_signed", C.UNCLOSABLE_POLICY_NOT_SIGNED),
        ("unclosable_revoked", C.UNCLOSABLE_POLICY_REVOKED),
        ("unclosable_unavailable", C.UNCLOSABLE_POLICY_UNAVAILABLE),
    ],
)
def test_f09_f10_no_state_reaches_unclosable_without_a_loaded_policy(
    origin: S | None, label: str, code: C
) -> None:
    result = transition(_snapshot(origin), COMMANDS[label])
    assert isinstance(result, Rejected)
    if origin in ACTIVE:
        assert result.code is code


@pytest.mark.parametrize("role", [Role.LINE_MANAGER, Role.ADMINISTRATOR, Role.PROVIDER_INSTALLER])
def test_f12_f13_f14_no_transition_key_is_granted_to_line_manager_admin_or_provider(
    role: Role,
) -> None:
    keys = {key for row in TRANSITIONS.values() for key in row.permission_keys}
    assert keys
    assert not [key for key in keys if role in roles_with(key)]


def test_f15_f16_no_command_carries_a_standard_version_or_a_person() -> None:
    fields = {field.name for command in _command_types() for field in dataclasses.fields(command)}
    assert not [name for name in fields if "standard" in name or "version" in name]
    assert not [name for name in fields if "person" in name or "user" in name or "name" in name]
    assert dataclasses.fields(AssignAction) == ()


def test_forbidden_rows_with_a_code_are_rejected_with_it() -> None:
    by_id = {row.forbidden_id: row for row in FORBIDDEN_TRANSITIONS}
    probes = {
        "F02": (LifecycleSnapshot(S.RECEIVED), COMMANDS["close_ok"]),
        "F03": (LifecycleSnapshot(S.OPEN), COMMANDS["close_ok"]),
        "F04": (LifecycleSnapshot(S.ACTION_ASSIGNED), COMMANDS["close_no_evidence"]),
        "F09": (LifecycleSnapshot(S.OPEN), COMMANDS["unclosable_not_signed"]),
        "F10": (LifecycleSnapshot(S.OVERDUE), COMMANDS["unclosable_unavailable"]),
    }
    assert {row.forbidden_id for row in FORBIDDEN_TRANSITIONS if row.code} == set(probes)
    for forbidden_id, (snapshot, command) in probes.items():
        assert transition(snapshot, command) == Rejected(by_id[forbidden_id].code)  # type: ignore[arg-type]


def test_every_lifecycle_code_is_a_loop_detail_code() -> None:
    published = {code.value for code in LoopDetailCode}
    assert {f"loop_{code.value}" for code in LifecycleCode} <= published
