"""Cobertura mínima y matriz derivada del catálogo (TASK-208; BR-GOB-97 a 99; respuesta 9).

Ejemplos en los bordes de ``coverage_state`` y ``unsatisfiable_reason`` (A-03, A-06, pendiente
nº 34) y de ``derive_matrix``: cuatro filas por estándar, ``row_id`` determinista que no depende
del orden de las condiciones ni de la versión del catálogo, y que cambia con la versión del
estándar o con sus condiciones. PR-GOB-13 sigue siendo de TASK-214: aquí basta la prueba
unitaria. Solo datos generados.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from vigia_contracts.models.enumerations import ObservabilityState

from vigia_platform.catalog.domain.coverage import (
    MinimumCoverage,
    coverage_state,
    unsatisfiable_reason,
)
from vigia_platform.catalog.domain.enums import Posture
from vigia_platform.catalog.domain.matrix import POSTURES, derive_matrix, matrix_row_id

A, B, C, D = (uuid.UUID(int=n, version=4) for n in (1, 2, 3, 4))
FOREIGN = uuid.UUID(int=99, version=4)
OBSERVABLE = ObservabilityState.OBSERVABLE
DEGRADED = ObservabilityState.DEGRADED
NOT_OBSERVABLE = ObservabilityState.NOT_OBSERVABLE


def _coverage(
    count: int, required: tuple[uuid.UUID, ...], cameras: tuple[uuid.UUID, ...] = (A, B, C)
) -> MinimumCoverage:
    return MinimumCoverage(required_count=count, required_camera_ids=required, camera_ids=cameras)


# --- BR-GOB-97: estado de la zona --------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "required", "up", "expected"),
    [
        (2, (A,), {A, B, C}, OBSERVABLE),  # ninguna caída
        (2, (A,), {A, B}, DEGRADED),  # cae una no requerida, conserva el mínimo
        (2, (A,), {A, C}, DEGRADED),
        (2, (A,), {B, C}, NOT_OBSERVABLE),  # cae la requerida aunque quedan dos
        (2, (A,), {A}, NOT_OBSERVABLE),  # baja de required_count
        (2, (A,), set(), NOT_OBSERVABLE),
        (1, (), {C}, DEGRADED),  # sin requeridas: basta una
        (1, (), set(), NOT_OBSERVABLE),
        (3, (A, B, C), {A, B, C}, OBSERVABLE),  # todas requeridas
        (3, (A, B, C), {A, B}, NOT_OBSERVABLE),
        (3, (), {A, B}, NOT_OBSERVABLE),  # required_count = todas
        (2, (A, B), {A, B}, DEGRADED),  # required_count = requeridas, cae la otra
    ],
)
def test_coverage_state_follows_br_gob_97(
    count: int, required: tuple[uuid.UUID, ...], up: set[uuid.UUID], expected: ObservabilityState
) -> None:
    assert coverage_state(_coverage(count, required), up) is expected


def test_foreign_observable_cameras_do_not_count() -> None:
    coverage = _coverage(2, (A,))
    assert coverage_state(coverage, {A, FOREIGN}) is NOT_OBSERVABLE
    assert coverage_state(coverage, {A, B, C, FOREIGN}) is OBSERVABLE


# --- BR-GOB-11 y 98: satisfacible ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "required", "cameras", "satisfiable"),
    [
        (1, (), (A,), True),
        (1, (A,), (A,), True),  # required_count = len(required) = len(cameras)
        (2, (A,), (A, B, C), True),
        (3, (A,), (A, B, C), True),  # required_count = len(cameras)
        (2, (A, B), (A, B, C), True),  # required_count = len(required)
        (4, (A,), (A, B, C), False),  # más que las cámaras
        (1, (A, B), (A, B, C), False),  # menos que las requeridas
        (2, (A, FOREIGN), (A, B, C), False),  # requerida ajena a la zona
        (2, (A, A), (A, B, C), False),  # requerida repetida
        (0, (), (A, B), False),  # el mínimo del contrato es 1
        (1, (), (), False),  # sin cámaras
    ],
)
def test_unsatisfiable_reason_edges(
    count: int,
    required: tuple[uuid.UUID, ...],
    cameras: tuple[uuid.UUID, ...],
    satisfiable: bool,
) -> None:
    reason = unsatisfiable_reason(_coverage(count, required, cameras))
    assert (reason is None) is satisfiable


def test_minimum_coverage_rejects_non_uuid_cameras() -> None:
    with pytest.raises(TypeError):
        MinimumCoverage(required_count=1, required_camera_ids=(), camera_ids=(str(A),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MinimumCoverage(required_count=True, required_camera_ids=(), camera_ids=(A,))


# --- Matriz derivada ---------------------------------------------------------------------------

PRESENCE = {"presence": True}
ENERGY = {"signal_role": "energy", "value": "asserted"}
GUARD = {"signal_role": "guard", "value": "asserted"}


def _standard(standard_id: uuid.UUID, version: int, *conditions: dict[str, Any]) -> dict[str, Any]:
    return {
        "standard_id": str(standard_id),
        "version": version,
        "family": "guard_bypass",
        "predicate": {"all_of": list(conditions), "min_duration_ms": 0},
    }


def _catalog(*standards: dict[str, Any], version: int = 1) -> dict[str, Any]:
    return {"version": version, "standards": list(standards)}


def test_each_standard_contributes_one_row_per_posture() -> None:
    rows = derive_matrix(_catalog(_standard(A, 1, PRESENCE, GUARD), _standard(B, 3, PRESENCE)))
    assert len(rows) == 8
    assert [r.posture for r in rows[:4]] == list(POSTURES)
    assert {(r.standard_id, r.standard_version) for r in rows} == {(A, 1), (B, 3)}
    assert len({r.row_id for r in rows}) == 8
    assert rows[0].conditions == (("guard", "asserted"), ("presence", "true"))


def test_the_maximum_matrix_has_128_distinct_rows() -> None:
    standards = [_standard(uuid.UUID(int=n + 1, version=4), 1, PRESENCE, ENERGY) for n in range(32)]
    rows = derive_matrix(_catalog(*standards))
    assert len(rows) == 128 == len({r.row_id for r in rows})


def test_row_ids_are_deterministic_across_catalog_versions_and_condition_order() -> None:
    first = derive_matrix(_catalog(_standard(A, 2, PRESENCE, GUARD, ENERGY), version=4))
    again = derive_matrix(_catalog(_standard(A, 2, ENERGY, PRESENCE, GUARD), version=9))
    assert [r.row_id for r in first] == [r.row_id for r in again]
    assert first[0].row_id == matrix_row_id(
        A,
        2,
        (("energy", "asserted"), ("guard", "asserted"), ("presence", "true")),
        Posture.STANDING,
    )


def test_row_ids_change_with_the_standard_version_or_its_conditions() -> None:
    base = {r.row_id for r in derive_matrix(_catalog(_standard(A, 1, PRESENCE, GUARD)))}
    bumped = {r.row_id for r in derive_matrix(_catalog(_standard(A, 2, PRESENCE, GUARD)))}
    other = {r.row_id for r in derive_matrix(_catalog(_standard(A, 1, PRESENCE, GUARD, ENERGY)))}
    another_standard = {r.row_id for r in derive_matrix(_catalog(_standard(B, 1, PRESENCE, GUARD)))}
    assert not base & bumped and not base & other and not base & another_standard


def test_rows_do_not_depend_on_the_order_of_standards() -> None:
    one = derive_matrix(_catalog(_standard(A, 1, PRESENCE), _standard(B, 1, PRESENCE)))
    two = derive_matrix(_catalog(_standard(B, 1, PRESENCE), _standard(A, 1, PRESENCE)))
    assert one == two
