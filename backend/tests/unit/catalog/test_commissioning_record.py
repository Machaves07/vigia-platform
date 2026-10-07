"""Guardas del acta, difuminado y reejecución, en funciones puras (TASK-216; LC-GOB-08 y 09).

- ``first_failing_guard``: para **cualquier** subconjunto de las siete guardas fallando a la vez,
  el error es la primera en el orden de BL §2.2.3 (Hypothesis sobre los subconjuntos).
- ``latest_by_camera``: la última prueba de oclusión no depende del orden de los UUID v7.
- ``blur_check``: solo con lo que dice ``head_object`` (objeto, SHA-256 y metadato ``1``).
- ``rerun_rows`` y ``clears_regression`` (BR-GOB-55): filas afectadas, ``all``, marcas posteriores
  y versiones que solo cambian textos.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.walk_test_support import standards
from vigia_platform.catalog.domain.commissioning_record import (
    GUARD_ORDER,
    MIN_LATENCY_REPETITIONS,
    CloseFacts,
    CloseGuard,
    blur_check,
    clears_regression,
    first_failing_guard,
    matrix_results,
    rerun_rows,
)
from vigia_platform.catalog.domain.enums import (
    OcclusionVerification,
    PassResult,
    RegressionCause,
    RegressionState,
    StepKind,
    WalkTestKind,
)
from vigia_platform.catalog.domain.occlusion import OcclusionTest, latest_by_camera
from vigia_platform.catalog.domain.regression import WalkTestRegression
from vigia_platform.catalog.domain.steps import WalkTestStep
from vigia_platform.catalog.domain.walk_test import (
    SessionRow,
    WalkTestPass,
    WalkTestSession,
    open_session,
    session_rows,
)
from vigia_platform.fleet.domain.verification_clip import ObjectFacts

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
ORG, PLANT, ZONE, NODE, USER = (uuid.uuid4() for _ in range(5))
CAMERAS = (uuid.uuid4(), uuid.uuid4())
CATALOG: dict[str, Any] = {
    "standards": standards(2),
    "cameras": [{"camera_id": str(c)} for c in CAMERAS],
}


def _pass(row: uuid.UUID, result: PassResult = PassResult.DETECTED) -> WalkTestPass:
    return WalkTestPass(uuid.uuid4(), ORG, PLANT, uuid.uuid4(), row, result, None, USER, T0)


def _test(
    camera: uuid.UUID,
    verification: OcclusionVerification,
    test_id: uuid.UUID | None = None,
    deadline: datetime = T0 + timedelta(minutes=5),
) -> OcclusionTest:
    return OcclusionTest(
        test_id=test_id or uuid.uuid4(),
        organization_id=ORG,
        plant_id=PLANT,
        session_id=uuid.uuid4(),
        camera_id=camera,
        started_at=T0 - timedelta(seconds=20),
        ended_at=T0,
        deadline=deadline,
        verification=verification,
        correlated_event_ids=None,
        declared_reason_es=None,
        recorded_by=USER,
    )


def _facts(failing: frozenset[CloseGuard]) -> CloseFacts:
    """Hechos con exactamente las guardas de ``failing`` fallando."""
    rows = session_rows(CATALOG, 3)
    passes: list[WalkTestPass] = []
    for index, row in enumerate(rows):
        short = CloseGuard.MATRIX_INCOMPLETE in failing and index == 0
        passes += [_pass(row.row_id) for _ in range(2 if short else 3)]
    if CloseGuard.FALSE_NEGATIVE_PRESENT in failing:
        passes.append(_pass(rows[1].row_id, PassResult.MISSED))
    if CloseGuard.FALSE_ALARM_RATE_ABOVE_THRESHOLD in failing:
        passes.append(_pass(rows[2].row_id, PassResult.FALSE_ALARM))
    steps = [WalkTestStep(uuid.uuid4(), ORG, PLANT, uuid.uuid4(), StepKind.FRAMING, USER, T0, T0)]
    if CloseGuard.STEPS_STILL_OPEN in failing:
        steps.append(WalkTestStep(uuid.uuid4(), ORG, PLANT, uuid.uuid4(), StepKind.OTHER, USER, T0))
    cameras = CAMERAS[:1] if CloseGuard.REDUNDANCY_NOT_VERIFIED in failing else CAMERAS
    tests = [_test(camera, OcclusionVerification.DECLARED) for camera in cameras]
    clips = 0 if CloseGuard.LATENCY_NOT_MEASURED in failing else MIN_LATENCY_REPETITIONS
    return CloseFacts(
        steps=steps,
        rows=rows,
        passes=passes,
        occlusion_tests=tests,
        cameras=CAMERAS,
        now=T0,
        verification_clips_in_window=clips,
        blur_verified=CloseGuard.BLUR_NOT_VERIFIED not in failing,
    )


@given(failing=st.frozensets(st.sampled_from(GUARD_ORDER)))
def test_the_first_failing_guard_in_order_decides(failing: frozenset[CloseGuard]) -> None:
    expected = next((guard for guard in GUARD_ORDER if guard in failing), None)
    assert first_failing_guard(_facts(failing)) == expected


def test_the_order_is_the_one_of_the_design() -> None:
    assert [guard.value for guard in GUARD_ORDER] == [
        "steps_still_open",
        "matrix_incomplete",
        "false_negative_present",
        "redundancy_not_verified",
        "false_alarm_rate_above_threshold",
        "latency_not_measured",
        "blur_not_verified",
    ]


def test_an_early_check_stops_at_its_last_guard() -> None:
    late = _facts(frozenset({CloseGuard.LATENCY_NOT_MEASURED, CloseGuard.BLUR_NOT_VERIFIED}))
    assert first_failing_guard(late, last=CloseGuard.FALSE_NEGATIVE_PRESENT) is None
    assert first_failing_guard(late) is CloseGuard.LATENCY_NOT_MEASURED


def test_ninety_nine_repetitions_do_not_close_and_one_hundred_do() -> None:
    facts = _facts(frozenset())
    passes = len(facts.passes)
    for clips, expected in ((99 - passes, CloseGuard.LATENCY_NOT_MEASURED), (100 - passes, None)):
        changed = dataclasses.replace(facts, verification_clips_in_window=clips)
        assert first_failing_guard(changed) is expected


def test_false_alarms_above_zero_pass_only_with_an_acceptance() -> None:
    facts = _facts(frozenset({CloseGuard.FALSE_ALARM_RATE_ABOVE_THRESHOLD}))
    assert first_failing_guard(facts) is CloseGuard.FALSE_ALARM_RATE_ABOVE_THRESHOLD
    assert first_failing_guard(dataclasses.replace(facts, false_alarm_accepted=True)) is None


def test_a_pending_test_before_its_deadline_blocks_and_one_failed_test_blocks() -> None:
    facts = _facts(frozenset())
    pending = [_test(CAMERAS[0], OcclusionVerification.DECLARED)]
    for blocking in (
        _test(CAMERAS[1], OcclusionVerification.PENDING, deadline=T0 + timedelta(seconds=1)),
        _test(CAMERAS[1], OcclusionVerification.FAILED),
    ):
        changed = dataclasses.replace(facts, occlusion_tests=[*pending, blocking])
        assert first_failing_guard(changed) is CloseGuard.REDUNDANCY_NOT_VERIFIED


# --- La última prueba de oclusión ----------------------------------------------------------------


def test_the_last_test_is_the_one_that_did_not_fail_whatever_its_uuid() -> None:
    camera = CAMERAS[0]
    failed = _test(
        camera, OcclusionVerification.FAILED, uuid.UUID("ffffffff-ffff-7fff-bfff-ffffffffffff")
    )
    newer = _test(
        camera, OcclusionVerification.DECLARED, uuid.UUID("00000000-0000-7000-8000-000000000001")
    )
    assert latest_by_camera([failed, newer])[camera] is newer
    assert latest_by_camera([newer, failed])[camera] is newer
    # Solo pruebas failed: cualquiera dice lo mismo; decide el id.
    older = _test(camera, OcclusionVerification.FAILED, uuid.UUID(int=1))
    assert latest_by_camera([older, failed])[camera] is failed


# --- Difuminado ----------------------------------------------------------------------------------


SHA = "ab" * 32


def _facts_of(sha: str | None = SHA, metadata: dict[str, str] | None = None) -> ObjectFacts:
    return ObjectFacts(
        size_bytes=10,
        sha256_hex=sha,
        content_type="video/mp4",
        metadata={"vigia-anonymized": "1"} if metadata is None else metadata,
    )


@pytest.mark.parametrize(
    ("facts", "cause"),
    [
        (None, "clip_missing"),
        (_facts_of(sha=None), "clip_hash_mismatch"),
        (_facts_of(sha="cd" * 32), "clip_hash_mismatch"),
        (_facts_of(metadata={}), "clip_not_anonymized"),
        (_facts_of(metadata={"vigia-anonymized": "0"}), "clip_not_anonymized"),
        (_facts_of(metadata={"vigia-anonymized": "true"}), "clip_not_anonymized"),
        (_facts_of(metadata={"vigia-anonymized": " 1"}), "clip_not_anonymized"),
        (_facts_of(), None),
    ],
)
def test_blur_is_approved_only_by_the_sha256_and_the_metadata_one(
    facts: ObjectFacts | None, cause: str | None
) -> None:
    check = blur_check(SHA, facts, T0)
    assert (check.approved, check.cause) == (cause is None, cause)
    assert check.to_json()["result"] == ("approved" if cause is None else "rejected")


# --- Reejecución ---------------------------------------------------------------------------------


def _regression(
    rows: Sequence[uuid.UUID] | str, record: uuid.UUID, *, pending: bool = True
) -> WalkTestRegression:
    return WalkTestRegression(
        organization_id=ORG,
        plant_id=PLANT,
        zone_id=ZONE,
        state=RegressionState.PENDING if pending else RegressionState.CURRENT,
        marked_at=T0 if pending else None,
        cause=RegressionCause.CATALOG_CHANGE if pending else None,
        catalog_version=2 if pending else None,
        affected_row_ids=(tuple(rows) if rows != "all" else "all") if pending else None,
        ledger_record_id=record,
    )


def _rerun(rows: tuple[SessionRow, ...], basis: uuid.UUID) -> WalkTestSession:
    return open_session(
        session_id=uuid.uuid4(),
        organization_id=ORG,
        plant_id=PLANT,
        zone_id=ZONE,
        node_id=NODE,
        catalog_version=1,
        catalog=CATALOG,
        passes_per_cell=3,
        at=T0,
        kind=WalkTestKind.REGRESSION_RERUN,
        rows=rows,
        regression_basis_record_id=basis,
    )


def test_the_rerun_opens_only_the_affected_rows_or_all_of_them() -> None:
    matrix = session_rows(CATALOG, 3)
    affected = [row.row_id for row in matrix[:4]]
    basis = uuid.uuid4()
    assert rerun_rows(_regression(affected, basis), CATALOG, 3) == matrix[:4]
    assert rerun_rows(_regression("all", basis), CATALOG, 3) == matrix
    # Una fila que ya no está en la matriz vigente: la completa, nunca se pierde una fila.
    assert rerun_rows(_regression([uuid.uuid4()], basis), CATALOG, 3) == matrix
    with pytest.raises(ValueError, match="pending"):
        rerun_rows(_regression("all", basis, pending=False), CATALOG, 3)


def test_a_rerun_clears_only_its_own_mark_when_it_covers_the_rows() -> None:
    matrix = session_rows(CATALOG, 3)
    affected = [row.row_id for row in matrix[:4]]
    basis = uuid.uuid4()
    session = _rerun(matrix[:4], basis)
    assert clears_regression(session, _regression(affected, basis), (CATALOG, CATALOG))
    # Otra marca después de abrir (otro registro): sigue pending.
    assert not clears_regression(session, _regression(affected, uuid.uuid4()), (CATALOG, CATALOG))
    # Filas que la sesión no cubre, o «all» con una sesión parcial: no.
    assert not clears_regression(
        session, _regression([row.row_id for row in matrix[3:6]], basis), (CATALOG, CATALOG)
    )
    assert not clears_regression(session, _regression("all", basis), (CATALOG, CATALOG))
    assert clears_regression(_rerun(matrix, basis), _regression("all", basis), (CATALOG, CATALOG))
    # Sin regresión pending o con una sesión inicial, nada que resolver.
    assert not clears_regression(session, None, (CATALOG, CATALOG))
    assert not clears_regression(session, _regression("all", basis, pending=False), (CATALOG,))


def test_a_text_only_standard_version_does_not_hide_the_coverage() -> None:
    """La versión que solo cambia el título cambia el ``row_id`` (incluye la versión del
    estándar) pero no lo que se midió: se compara por ``(standard_id, postura)``."""
    matrix = session_rows(CATALOG, 3)
    renamed = {
        **CATALOG,
        "standards": [{**s, "version": s["version"] + 1} for s in CATALOG["standards"]],
    }
    carried = [row.row_id for row in session_rows(renamed, 3)[:4]]
    basis = uuid.uuid4()
    assert clears_regression(
        _rerun(matrix[:4], basis), _regression(carried, basis), (CATALOG, renamed)
    )


def test_an_unverifiable_evidence_ref_is_listed_on_its_row() -> None:
    rows = session_rows(CATALOG, 3)
    clip, stray = uuid.uuid4(), uuid.uuid4()
    passes = [
        dataclasses.replace(_pass(rows[0].row_id), evidence_ref=clip),
        dataclasses.replace(_pass(rows[1].row_id), evidence_ref=stray),
        dataclasses.replace(_pass(rows[1].row_id), evidence_ref=stray),
    ]
    results = matrix_results(rows, passes, frozenset({clip}))
    assert "unverifiable_evidence_refs" not in results[0]
    assert results[1]["unverifiable_evidence_refs"] == [str(stray)]
