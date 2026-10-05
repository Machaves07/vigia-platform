"""Propiedades del comisionamiento (C-PLA-10; TASK-214): PR-GOB-13 y PR-GOB-10.

- **PR-GOB-13** (``walk_test_matrices``): la matriz de la sesión tiene exactamente una fila por
  (combinación de condiciones del predicado, postura) de cada estándar vigente, ni una más ni una
  menos, con ``required_passes = passes_per_cell``; el ``row_id`` es estable para el mismo catálogo
  (otra derivación, otro orden de los estándares, ida y vuelta por la forma JSON de la sesión).
- **PR-GOB-10, invariante** (``walk_test_step_sequences``, con correcciones generadas):
  ``total_hours`` es exactamente la suma de las duraciones efectivas de los pasos cerrados
  (calculadas aquí de forma independiente) y ``steps_summary`` suma lo mismo agrupado por
  ``step_kind``; cambiar los responsables no cambia ninguna de las dos (H-53). Las marcas del
  servidor sobreviven a toda corrección (BR-GOB-45).

La parte estructural de PR-GOB-10 está en ``tests/unit/catalog/test_no_hours_by_responsible.py``.
Semillas: las del perfil activo (``tests/conftest.py``, ``_seeds_for_profile``). Solo datos
generados (NFR-CTR-43).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.gob.strategies.walk_test import (
    RESPONSIBLES,
    StepSequence,
    WalkTestMatrix,
    walk_test_matrices,
    walk_test_step_sequences,
)
from vigia_platform.catalog.domain.enums import Posture, StepKind
from vigia_platform.catalog.domain.matrix import matrix_row_id
from vigia_platform.catalog.domain.predicates import canonical_conditions
from vigia_platform.catalog.domain.steps import (
    CorrectionRequest,
    StepRequestInvalid,
    close_step,
    steps_summary,
    total_hours,
)
from vigia_platform.catalog.domain.walk_test import (
    MIN_PASSES_PER_CELL,
    SessionRow,
    WalkTestRuleViolated,
    WalkTestViolation,
    session_rows,
)

PASSES = st.integers(MIN_PASSES_PER_CELL, 50)
_MS = timedelta(milliseconds=1)


# --- PR-GOB-13 ---------------------------------------------------------------------------------


@given(matrix=walk_test_matrices(), passes=PASSES)
def test_pr_gob_13_one_row_per_condition_combination_and_posture(
    matrix: WalkTestMatrix, passes: int
) -> None:
    rows = session_rows(matrix.catalog, passes)
    expected = {
        (
            uuid.UUID(standard["standard_id"]),
            int(standard["version"]),
            canonical_conditions(standard["predicate"]),
            posture,
        )
        for standard in matrix.catalog["standards"]
        for posture in Posture
    }
    derived = [(r.standard_id, r.standard_version, r.conditions, r.posture) for r in rows]
    # Ni una más ni una menos: mismo conjunto y sin repetidas.
    assert len(derived) == len(expected) == 4 * len(matrix.catalog["standards"])
    assert set(derived) == expected
    assert len({row.row_id for row in rows}) == len(rows)
    assert {row.required_passes for row in rows} == {passes}


@given(matrix=walk_test_matrices(), passes=PASSES, order=st.randoms(use_true_random=False))
def test_pr_gob_13_row_id_is_stable_for_the_same_catalog(
    matrix: WalkTestMatrix, passes: int, order: object
) -> None:
    rows = session_rows(matrix.catalog, passes)
    shuffled = copy.deepcopy(matrix.catalog)
    order.shuffle(shuffled["standards"])  # type: ignore[attr-defined]
    again = session_rows(shuffled, passes)
    assert {row.row_id: row for row in again} == {row.row_id: row for row in rows}
    for row in rows:
        assert row.row_id == matrix_row_id(
            row.standard_id, row.standard_version, row.conditions, row.posture
        )
        # La sesión guarda la matriz en JSON: la ida y vuelta conserva fila e identificador.
        stored = json.loads(json.dumps(row.to_json()))
        assert SessionRow.from_json(stored) == row
    # Otro número de pases no cambia las filas, solo lo que exige cada una.
    other = session_rows(matrix.catalog, passes + 1)
    assert [r.row_id for r in other] == [r.row_id for r in rows]


@given(passes=st.integers(-(2**63), MIN_PASSES_PER_CELL - 1))
def test_fewer_than_three_passes_is_passes_below_minimum(passes: int) -> None:
    with pytest.raises(WalkTestRuleViolated) as raised:
        session_rows({"standards": []}, passes)
    assert raised.value.violation is WalkTestViolation.PASSES_BELOW_MINIMUM


# --- PR-GOB-10 (invariante) ----------------------------------------------------------------------


def _effective_ms(sequence: StepSequence) -> list[tuple[StepKind, int]]:
    """Duraciones efectivas calculadas sin ``WalkTestStep.effective_window``."""
    found: list[tuple[StepKind, int]] = []
    for step in sequence.steps:
        if step.ended_at is None:
            continue
        start, end = step.started_at, step.ended_at
        if step.correction is not None:
            if step.correction.started_at is not None:
                start = step.correction.started_at
            if step.correction.ended_at is not None:
                end = step.correction.ended_at
        found.append((step.step_kind, (end - start) // _MS))
    return found


@given(sequence=walk_test_step_sequences())
def test_pr_gob_10_total_hours_is_the_exact_sum_of_closed_steps(sequence: StepSequence) -> None:
    effective = _effective_ms(sequence)
    assert total_hours(sequence.steps) == sum(duration for _, duration in effective)
    summary = steps_summary(sequence.steps)
    assert sum(hours.duration_ms for hours in summary) == total_hours(sequence.steps)
    by_kind: dict[StepKind, int] = {}
    for kind, duration in effective:
        by_kind[kind] = by_kind.get(kind, 0) + duration
    assert {hours.step_kind: hours.duration_ms for hours in summary} == by_kind
    assert [hours.step_kind for hours in summary] == [k for k in StepKind if k in by_kind]
    # Cada tipo una vez y ningún campo de persona en el resumen (BR-GOB-46).
    assert all(
        {f.name for f in dataclasses.fields(hours)} == {"step_kind", "duration_ms"}
        for hours in summary
    )


@given(sequence=walk_test_step_sequences(), shift=st.integers(1, len(RESPONSIBLES) - 1))
def test_pr_gob_10_hours_do_not_depend_on_who_was_responsible(
    sequence: StepSequence, shift: int
) -> None:
    rotated = tuple(
        dataclasses.replace(
            step,
            responsible_user_id=RESPONSIBLES[
                (RESPONSIBLES.index(step.responsible_user_id) + shift) % len(RESPONSIBLES)
            ],
        )
        for step in sequence.steps
    )
    assert total_hours(rotated) == total_hours(sequence.steps)
    assert steps_summary(rotated) == steps_summary(sequence.steps)


@given(sequence=walk_test_step_sequences())
def test_pr_gob_10_server_marks_survive_every_correction(sequence: StepSequence) -> None:
    for opened, step, at in zip(sequence.opened, sequence.steps, sequence.closed_at, strict=True):
        assert step.started_at == opened.started_at
        if at is None:
            assert step.ended_at is None and step.correction is None
            continue
        assert step.ended_at == max(at, opened.started_at)
        if step.correction is not None:
            assert step.correction.corrected_at == step.ended_at
            assert step.correction.reason_es
        content = step.record_content()
        assert content["responsible_user_id"] == str(step.responsible_user_id)
        assert "ended_at" in content


@given(
    sequence=walk_test_step_sequences(max_steps=1),
    back=st.integers(1, 10_000),
)
def test_pr_gob_10_a_negative_corrected_duration_is_rejected(
    sequence: StepSequence, back: int
) -> None:
    for opened, at in zip(sequence.opened, sequence.closed_at, strict=True):
        if at is None:
            continue
        correction = CorrectionRequest(
            "Corrección sintética inválida", started_at=at, ended_at=at - _MS * back
        )
        with pytest.raises(StepRequestInvalid):
            close_step(opened, at=at, corrected_by=RESPONSIBLES[0], correction=correction)
