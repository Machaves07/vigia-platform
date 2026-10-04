"""PR-GOB-14, parte de marca (TASK-209; BR-GOB-51 a 56; LC-GOB-09).

«Un cambio en una fila afectada implica regresión ``pending``» (business-logic-model §6). La
mitad del cierre (``regression_rerun`` que devuelve a ``current``) es de TASK-216.

- **Máquina de estados sobre la base** (``RegressionMarking``, perfil ``ci`` con su semilla fija y
  la de la sesión, ``_seeds_for_profile``): una zona ``productive`` con su versión 1 de
  ``catalog_versions``; las reglas publican cambios de cada tipo (``catalog_changes``), cambian
  solo el texto de un estándar, marcan la zona unipersonal, informan otro ``model_version`` y
  cambian solo ``software_version``, siempre por los servicios reales
  (``CatalogPublicationService`` con el ``RegressionService`` real). Un modelo independiente lleva
  las filas pendientes como ``(standard_id, postura)`` o ``all``, calculadas con un oráculo propio
  (no con ``publication_rows``). Invariantes tras cada paso: la fila de la base es la del modelo
  (``pending`` con esas filas, o ``all``; ``current`` si nada marcó), un registro
  ``walk_test_regression_marked`` por marca, y ``resulting_mode`` sigue ``productive``.
- **Propiedades puras sobre ``walk_test_matrices``**: un cambio solo de texto no marca y lleva
  cada fila pendiente a la versión nueva de su estándar; la unión nunca pierde una fila; las filas
  de una marca son siempre de la matriz nueva.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

import pytest
from hypothesis import given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
    run_state_machine_as_test,
)

from tests.catalog_routes_support import CatalogRoutes, catalog_routes_world
from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from tests.properties.gob.strategies.catalog import (
    TITLES,
    CatalogScenario,
    Intent,
    catalog_changes,
    catalog_versions,
    resolve,
)
from tests.properties.gob.strategies.walk_test import (
    DECLARED_BY,
    REASON,
    T0,
    WalkTestMatrix,
    state_of,
    walk_test_matrices,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.publication import CatalogRequestInvalid
from vigia_platform.catalog.application.regression import requires_model_regression
from vigia_platform.catalog.domain.catalog_version import (
    AGGREGATION_WINDOW_MAX,
    AGGREGATION_WINDOW_MIN,
    CatalogChange,
    CatalogRuleViolated,
    CatalogState,
    NewStandard,
    NewStandardVersion,
    RetireStandard,
    SetCameras,
    SetMinimumCoverage,
    SetSignals,
    SetSingleOccupancy,
    SetThresholds,
    SetWindows,
    ZoneCatalogVersion,
    plan_publication,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField, RegressionCause
from vigia_platform.catalog.domain.matrix import POSTURES, derive_matrix
from vigia_platform.catalog.domain.regression import (
    ALL_ROWS,
    RegressionMark,
    WalkTestRegression,
    carried_forward,
    merged,
    publication_rows,
)
from vigia_platform.shared.context import ScopeContext

Key = tuple[str, str]
"""``(standard_id, postura)``: la fila sin la versión del estándar."""
Keys = frozenset[Key] | str
MODELS: Final = ("detector-v1", "detector-v2", "detector-v3")
SOFTWARE: Final = ("1.0.0", "1.1.0", "2.0.0")
STEPS: Final = 8
ZONE_WIDE: Final = (SetCameras, SetMinimumCoverage, SetSignals, SetThresholds, SetWindows)


# --- Oráculo (independiente de ``publication_rows``) -------------------------------------------


def _predicate(standard: dict[str, Any]) -> tuple[tuple[str, ...], int]:
    predicate = standard["predicate"]
    conditions = sorted(json.dumps(c, sort_keys=True) for c in predicate["all_of"])
    return tuple(conditions), int(predicate["min_duration_ms"])


def _standard_keys(standard_id: str) -> frozenset[Key]:
    return frozenset((standard_id, posture.value) for posture in POSTURES)


def expected_keys(
    change: CatalogChange, before: dict[str, Any], after: dict[str, Any]
) -> Keys | None:
    """Lo que la versión debe marcar según la tabla de TASK-209 (``None``: no marca)."""
    if isinstance(change, (*ZONE_WIDE, RetireStandard)):
        return ALL_ROWS
    if isinstance(change, SetSingleOccupancy):
        return None
    old = {s["standard_id"]: s for s in before["standards"]}
    new = {s["standard_id"]: s for s in after["standards"]}
    if isinstance(change, NewStandard):
        (added,) = set(new) - set(old)
        return _standard_keys(added)
    assert isinstance(change, NewStandardVersion)
    standard_id = str(change.standard_id)
    if _predicate(old[standard_id]) == _predicate(new[standard_id]):
        return None
    return _standard_keys(standard_id)


def _union(current: Keys | None, added: Keys) -> Keys:
    if current == ALL_ROWS or added == ALL_ROWS or current is None:
        return ALL_ROWS if ALL_ROWS in (current, added) else added
    assert isinstance(current, frozenset) and isinstance(added, frozenset)
    return current | added


def stored_keys(stored: Any, catalog: dict[str, Any]) -> Keys:
    """Las filas guardadas como ``(standard_id, postura)`` de la matriz del catálogo vigente."""
    if stored == ALL_ROWS:
        return ALL_ROWS
    matrix = {
        str(row.row_id): (str(row.standard_id), row.posture.value) for row in derive_matrix(catalog)
    }
    missing = [row for row in stored if row not in matrix]
    assert not missing, f"filas que no son de la matriz vigente: {missing}"
    return frozenset(matrix[row] for row in stored)


# --- Máquina de estados sobre la base ------------------------------------------------------------


@dataclass
class Model:
    keys: Keys | None = None
    """``None``: ``current``; si no, las filas pendientes."""
    marks: int = 0
    inventory: tuple[str, str] = (MODELS[0], SOFTWARE[0])
    """``(model_version, software_version)`` que el nodo informó por última vez."""
    model_version: str | None = None
    first_marked_at: Any = None
    versions: list[int] = field(default_factory=list)


class RegressionMarking(RuleBasedStateMachine):
    world: ClassVar[CatalogRoutes]

    def __init__(self) -> None:
        super().__init__()
        world = self.world
        self.site = world.site()
        ((plant, zone),) = self.site.zones()
        world.productive(self.site, plant, zone)
        self.zone = zone
        self.admin: ScopeContext = world.context(world.member(self.site))
        self.state: CatalogState | None = None
        self.model = Model()

    # --- Publicación ---------------------------------------------------------------------------

    def _publish(self, change: CatalogChange) -> ZoneCatalogVersion | None:
        self.world.tick()
        try:
            version: ZoneCatalogVersion = self.world.run(
                self.world.publication.publish_catalog_version(
                    self.admin, self.zone, change, REASON
                )
            )
        except (CatalogRejected, CatalogRequestInvalid):
            return None  # rechazada: nada escrito, la regresión no cambia
        self.state = CatalogState(
            catalog=version.payload,
            single_occupancy=version.single_occupancy,
            aggregation_window_minutes=version.aggregation_window_minutes,
        )
        self.model.versions.append(version.catalog_version)
        return version

    def _published(self, change: CatalogChange) -> None:
        assert self.state is not None
        before = dict(self.state.catalog)
        version = self._publish(change)
        if version is None:
            return
        added = expected_keys(change, before, dict(version.payload))
        if added is None:
            return
        if self.model.keys is None:
            self.model.first_marked_at = version.issued_at
        self.model.keys = _union(self.model.keys, added)
        self.model.marks += 1

    @initialize(scenario=catalog_versions(max_changes=0))
    def first_version(self, scenario: CatalogScenario) -> None:
        version = self._publish(scenario.first)
        assert version is not None and version.catalog_version == 1

    @rule(intent=catalog_changes())
    def publish_a_change(self, intent: Intent) -> None:
        assert self.state is not None
        self._published(resolve(intent, self.state))

    @rule(index=st.integers(0, 64), title=st.sampled_from(TITLES))
    def change_only_the_text(self, index: int, title: str) -> None:
        assert self.state is not None
        standards = self.state.catalog["standards"]
        target = standards[index % len(standards)]
        before = self.model.keys
        self._published(
            NewStandardVersion(standard_id=uuid.UUID(target["standard_id"]), title_es=title)
        )
        assert self.model.keys == before  # el texto nunca cambia el estado

    @rule(flag=st.booleans(), minutes=st.integers(AGGREGATION_WINDOW_MIN, AGGREGATION_WINDOW_MAX))
    def mark_single_occupancy(self, flag: bool, minutes: int) -> None:
        before = self.model.keys
        self._published(
            SetSingleOccupancy(single_occupancy=flag, aggregation_window_minutes=minutes)
        )
        assert self.model.keys == before

    @rule(model_version=st.sampled_from(MODELS))
    def report_a_model_version(self, model_version: str) -> None:
        previous, software = self.model.inventory
        self.model.inventory = (model_version, software)
        if not requires_model_regression(previous, model_version):
            return
        self.world.tick()
        (marked,) = self.world.run(
            self.world.regression.mark_model_version_change(
                self.world.system_context(self.site), (self.zone,), model_version
            )
        )
        if self.model.keys is None:
            self.model.first_marked_at = marked.marked_at
        self.model.keys = ALL_ROWS
        self.model.model_version = model_version
        self.model.marks += 1

    @rule(software=st.sampled_from(SOFTWARE))
    def change_only_the_software_version(self, software: str) -> None:
        model_version, _ = self.model.inventory
        self.model.inventory = (model_version, software)
        # El latido solo marca si cambió ``model_version``: aquí nunca llama al servicio.
        assert not requires_model_regression(model_version, model_version)

    # --- Invariantes ---------------------------------------------------------------------------

    @invariant()
    def the_row_is_the_model(self) -> None:
        if self.state is None:
            return
        row = self.world.regression_row(self.zone)
        if self.model.keys is None:
            assert row is None or row["state"] == "current"
        else:
            assert row is not None and row["state"] == "pending"
            stored = json.loads(row["affected_row_ids"])
            assert stored_keys(stored, dict(self.state.catalog)) == self.model.keys
            assert row["marked_at"] == self.model.first_marked_at
            if self.model.model_version is not None:
                assert row["model_version"] == self.model.model_version
        assert len(self.world.records(self.zone, "walk_test_regression_marked")) == self.model.marks

    @invariant()
    def the_zone_keeps_operating(self) -> None:
        # BR-GOB-53: la marca no toca las compuertas ni el modo resultante.
        assert self.world.resulting_mode(self.zone) == "productive"

    @invariant()
    def versions_stay_consecutive(self) -> None:
        assert self.model.versions == list(range(1, len(self.model.versions) + 1))


@pytest.fixture(scope="module")
def marking_world(postgres_endpoint: PostgresEndpoint) -> Iterator[CatalogRoutes]:
    with catalog_routes_world(postgres_endpoint, "regression_marking") as world:
        yield world


@pytest.mark.integration
def test_pr_gob_14_marking_follows_the_changes_and_never_stops_the_zone(
    marking_world: CatalogRoutes,
) -> None:
    RegressionMarking.world = marking_world
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(RegressionMarking)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS))


# --- Propiedades puras sobre la matriz -----------------------------------------------------------


def _renamed(matrix: WalkTestMatrix, index: int, title: str) -> tuple[dict[str, Any], str]:
    standards = matrix.catalog["standards"]
    target = standards[index % len(standards)]
    state = CatalogState(
        catalog=matrix.catalog, single_occupancy=False, aggregation_window_minutes=60
    )
    plan = plan_publication(
        state,
        NewStandardVersion(standard_id=uuid.UUID(target["standard_id"]), title_es=title),
        zone=matrix.zone,
        issued_at=T0,
        declared_by=DECLARED_BY,
        reason_es=REASON,
        new_standard_id=uuid.uuid4(),
    )
    return state_of(plan).catalog, str(target["standard_id"])


@given(matrix=walk_test_matrices(), index=st.integers(0, 64), title=st.sampled_from(TITLES))
def test_a_text_change_does_not_mark_and_carries_every_pending_row(
    matrix: WalkTestMatrix, index: int, title: str
) -> None:
    after, _ = _renamed(matrix, index, title)

    assert publication_rows(matrix.catalog, after, (CatalogChangedField.STANDARDS,)) is None
    pending = tuple(sorted(matrix.pending))
    carried = carried_forward(pending, matrix.catalog, after)

    assert carried != ALL_ROWS
    assert stored_keys([str(r) for r in carried], after) == stored_keys(
        [str(r) for r in pending], matrix.catalog
    )
    assert len(carried) == len(pending)


@given(matrix=walk_test_matrices(), extra=st.data())
def test_the_union_never_loses_a_row_and_keeps_the_first_instant(
    matrix: WalkTestMatrix, extra: st.DataObject
) -> None:
    rows = [row.row_id for row in matrix.rows]
    second = frozenset(extra.draw(st.lists(st.sampled_from(rows), min_size=1)))
    current = WalkTestRegression.initial(
        matrix.zone.organization_id, matrix.zone.plant_id, matrix.zone.zone_id
    )
    first_mark = RegressionMark(
        cause=RegressionCause.CATALOG_CHANGE,
        affected_row_ids=tuple(matrix.pending),
        marked_at=T0,
        catalog_version=2,
    )
    later = RegressionMark(
        cause=RegressionCause.CATALOG_CHANGE,
        affected_row_ids=tuple(second),
        marked_at=T0.replace(hour=9),
        catalog_version=3,
    )

    once = merged(current, first_mark)
    twice = merged(once, later)

    assert set(twice.affected_row_ids or ()) == set(matrix.pending) | second
    assert twice.marked_at == T0 and twice.catalog_version == 3
    absorbed = merged(
        twice,
        RegressionMark(
            cause=RegressionCause.FRAMING_RECAPTURED, affected_row_ids=ALL_ROWS, marked_at=T0
        ),
    )
    assert absorbed.affected_row_ids == ALL_ROWS
    assert merged(absorbed, later).affected_row_ids == ALL_ROWS


@given(scenario=catalog_versions(max_changes=6))
def test_the_rows_of_a_mark_are_rows_of_the_new_matrix(scenario: CatalogScenario) -> None:
    state: CatalogState | None = None
    for index, intent in enumerate([None, *scenario.intents]):
        change = scenario.first if intent is None else resolve(intent, state)  # type: ignore[arg-type]
        try:
            plan = plan_publication(
                state,
                change,
                zone=scenario.zone,
                issued_at=T0,
                declared_by=DECLARED_BY,
                reason_es=REASON,
                new_standard_id=uuid.UUID(int=index + 1, version=4),
            )
        except CatalogRuleViolated:  # los cambios rechazados no publican
            continue
        rows = publication_rows(
            None if state is None else state.catalog, plan.catalog, plan.changed_fields
        )
        if state is not None:
            expected = expected_keys(change, dict(state.catalog), plan.catalog)
            if expected is None:
                assert rows is None
            elif expected == ALL_ROWS:
                assert rows == ALL_ROWS
            else:
                assert rows is not None and rows != ALL_ROWS
                assert stored_keys([str(r) for r in rows], plan.catalog) == expected
        else:
            assert rows is None  # la versión 1 no marca
        state = state_of(plan)
