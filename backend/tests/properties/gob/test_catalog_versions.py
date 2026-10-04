"""Propiedades del catálogo versionado (C-PLA-07; TASK-208): PR-GOB-05, 06, 09 y 18.

- **PR-GOB-05** (monotonía): para toda secuencia generada de cambios (``catalog_versions``),
  ``catalog_version`` crece de uno en uno desde 1, sin huecos; un cambio rechazado no consume
  número; publicar después nunca altera lo ya emitido (ni el catálogo ni el hash de su sobre). La
  parte con la base (filas, registros y sobres guardados) está en
  ``tests/integration/test_catalog_publication.py``.
- **PR-GOB-06** (parte de catálogo): ``standard_valid_at`` devuelve la versión cuyo intervalo
  ``[effective_from, effective_until)`` contiene el instante, y ninguna fuera de él, en la historia
  que deja la secuencia (con publicaciones en el mismo milisegundo).
- **PR-GOB-09** (oráculo): ``coverage_state`` coincide con la lectura literal de BR-GOB-97 para todo
  subconjunto de cámaras caídas (``camera_outage_subsets``); todo catálogo insatisfacible (la
  mutación del kit de U-01 y las coberturas generadas) se rechaza con ``unsatisfiable_coverage``.
- **PR-GOB-18** (ida y vuelta): firmar con el ``SigningService`` real y verificar con el
  verificador del paquete de U-01 (``expected_purpose = catalog``) devuelve el mismo ``payload``,
  sobre catálogos de ``zone_catalogs`` del kit y los que compone ``plan_publication``, también tras
  pasar por JSON (como el ``jsonb`` donde se guarda).

Además NFR-GOB-38: ningún catálogo compuesto lleva ``single_occupancy`` ni
``aggregation_window_minutes``. Perfil ``ci`` con semilla registrada (``tests/conftest.py``).
Solo datos generados.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.canonical import canonical_sha256, canonicalize
from vigia_contracts.conformance.generators import (
    mutate_unsatisfiable_coverage,
    zone_catalog,
    zone_catalogs,
)
from vigia_contracts.models.enumerations import ObservabilityState
from vigia_contracts.signing import KeySet, verify

from tests.properties.gob.strategies.catalog import (
    CatalogScenario,
    OutageCase,
    camera_outage_subsets,
    catalog_versions,
    resolve,
)
from tests.signing_support import SigningWorld, bootstrapped_world
from vigia_platform.catalog.domain.catalog_version import (
    CatalogRuleViolated,
    CatalogState,
    CatalogViolation,
    PublicationPlan,
    SetMinimumCoverage,
    ZoneRef,
    plan_publication,
)
from vigia_platform.catalog.domain.coverage import (
    MinimumCoverage,
    coverage_state,
    unsatisfiable_reason,
)
from vigia_platform.catalog.domain.standard import (
    DeclaredBy,
    DeclaredStandardVersion,
    standard_valid_at,
)
from vigia_platform.shared.signing import NODE_PURPOSES, SigningPurpose

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
DECLARED_BY = DeclaredBy(uuid.UUID(int=7, version=4), "Coordinación SST sintética", "administrator")
REASON = "Cambio sintético del catálogo de la zona"
STEPS = (timedelta(0), timedelta(milliseconds=1), timedelta(seconds=1), timedelta(hours=1))
"""Avance del reloj entre publicaciones: 0 deja dos versiones en el mismo milisegundo."""
PLATFORM_ONLY = ("single_occupancy", "aggregation_window_minutes")


@dataclass
class Run:
    """Lo que dejó una secuencia: versiones emitidas (con su instante) y rechazos."""

    published: list[tuple[datetime, PublicationPlan]] = field(default_factory=list)
    rejected: list[CatalogViolation] = field(default_factory=list)
    history: dict[tuple[uuid.UUID, int], DeclaredStandardVersion] = field(default_factory=dict)


def _state(plan: PublicationPlan) -> CatalogState:
    return CatalogState(
        catalog=plan.catalog,
        single_occupancy=plan.single_occupancy,
        aggregation_window_minutes=plan.aggregation_window_minutes,
    )


def _run(scenario: CatalogScenario, steps: list[timedelta]) -> Run:
    run = Run()
    state: CatalogState | None = None
    moment = T0
    changes = [None, *scenario.intents]
    for index, intent in enumerate(changes):
        change = scenario.first if intent is None else resolve(intent, state)  # type: ignore[arg-type]
        try:
            plan = plan_publication(
                state,
                change,
                zone=scenario.zone,
                issued_at=moment,
                declared_by=DECLARED_BY,
                reason_es=REASON,
                new_standard_id=uuid.UUID(int=index + 1, version=4),
            )
        except CatalogRuleViolated as violation:
            run.rejected.append(violation.violation)
            continue
        run.published.append((moment, plan))
        for standard_id, version in plan.closed:
            run.history[(standard_id, version)] = dataclasses.replace(
                run.history[(standard_id, version)],
                retired_in_catalog_version=plan.catalog_version,
                effective_until=moment,
            )
        for standard in plan.born:
            run.history[(standard.standard_id, standard.version)] = standard
        state = _state(plan)
        moment += steps[index % len(steps)]
    return run


def _keys(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(item)


# --- PR-GOB-05 ---------------------------------------------------------------------------------


@given(scenario=catalog_versions(), steps=st.lists(st.sampled_from(STEPS), min_size=1, max_size=4))
def test_pr_gob_05_catalog_version_grows_by_one_and_never_rewrites_what_was_issued(
    scenario: CatalogScenario, steps: list[timedelta]
) -> None:
    state: CatalogState | None = None
    snapshots: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    moment = T0
    expected = 1
    for index, intent in enumerate([None, *scenario.intents]):
        change = scenario.first if intent is None else resolve(intent, state)  # type: ignore[arg-type]
        try:
            plan = plan_publication(
                state,
                change,
                zone=scenario.zone,
                issued_at=moment,
                declared_by=DECLARED_BY,
                reason_es=REASON,
                new_standard_id=uuid.UUID(int=index + 1, version=4),
            )
        except CatalogRuleViolated:
            # Rechazado: no consume número y la vigente no cambia.
            assert index > 0, "la primera publicación de la secuencia es válida"
            continue
        assert plan.catalog_version == expected == plan.catalog["version"]
        assert 1 <= len(plan.changed_fields) <= 8
        assert len(set(plan.changed_fields)) == len(plan.changed_fields)
        assert not set(PLATFORM_ONLY) & set(_keys(plan.catalog)), "NFR-GOB-38"
        # Ninguna publicación posterior altera lo ya emitido: el objeto devuelto entonces (no una
        # copia) sigue igual a su copia y con el mismo hash canónico de la carga.
        for issued, frozen, digest in snapshots:
            assert issued == frozen and canonical_sha256(issued) == digest
        snapshots.append(
            (plan.catalog, copy.deepcopy(plan.catalog), canonical_sha256(plan.catalog))
        )
        expected += 1
        state = _state(plan)
        moment += steps[index % len(steps)]
    assert expected - 1 == len(snapshots) >= 1
    assert [s[0]["version"] for s in snapshots] == list(range(1, len(snapshots) + 1))


@given(scenario=catalog_versions())
def test_pr_gob_05_each_standard_has_one_current_version_and_versions_grow_by_one(
    scenario: CatalogScenario,
) -> None:
    run = _run(scenario, [timedelta(seconds=1)])
    for _, plan in run.published:
        ids = [s["standard_id"] for s in plan.catalog["standards"]]
        assert len(ids) == len(set(ids)) and 1 <= len(ids) <= 32
    by_standard: dict[uuid.UUID, list[int]] = {}
    for standard_id, version in run.history:
        by_standard.setdefault(standard_id, []).append(version)
    for versions in by_standard.values():
        assert sorted(versions) == list(range(1, len(versions) + 1))
    current = [v for v in run.history.values() if v.current]
    final = run.published[-1][1].catalog["standards"]
    assert {(str(v.standard_id), v.version) for v in current} == {
        (s["standard_id"], s["version"]) for s in final
    }


# --- PR-GOB-06 (parte de catálogo) --------------------------------------------------------------


@given(
    scenario=catalog_versions(),
    steps=st.lists(st.sampled_from(STEPS), min_size=1, max_size=4),
    data=st.data(),
)
def test_pr_gob_06_standard_valid_at_returns_the_version_whose_interval_contains_t(
    scenario: CatalogScenario, steps: list[timedelta], data: st.DataObject
) -> None:
    run = _run(scenario, steps)
    history = list(run.history.values())
    last = run.published[-1][0]
    edges = [v.effective_from for v in history] + [
        v.effective_until for v in history if v.effective_until is not None
    ]
    instants = st.sampled_from(edges) | st.datetimes(
        min_value=(T0 - timedelta(hours=1)).replace(tzinfo=None),
        max_value=(last + timedelta(hours=1)).replace(tzinfo=None),
        timezones=st.just(UTC),
    )
    for _ in range(4):
        moment = data.draw(instants, label="t")
        nudge = data.draw(st.sampled_from((timedelta(0), timedelta(microseconds=-1))))
        moment += nudge
        for standard_id in {v.standard_id for v in history}:
            found = standard_valid_at(history, standard_id, moment)
            inside = [
                v
                for v in history
                if v.standard_id == standard_id
                and v.effective_from <= moment
                and (v.effective_until is None or moment < v.effective_until)
            ]
            assert len(inside) <= 1
            assert found == (inside[0] if inside else None)
            for other in history:
                if other.standard_id == standard_id and other is not found:
                    assert not other.valid_at(moment)


# --- PR-GOB-09 -----------------------------------------------------------------------------------


def _oracle(case: OutageCase) -> ObservabilityState:
    """Lectura literal de BR-GOB-97, escrita aparte de ``coverage_state``."""
    coverage = case.coverage
    up = [c for c in coverage.camera_ids if c not in case.down]
    lost_required = any(c in case.down for c in coverage.required_camera_ids)
    if lost_required or len(up) < coverage.required_count:
        return ObservabilityState.NOT_OBSERVABLE
    if case.down:
        return ObservabilityState.DEGRADED
    return ObservabilityState.OBSERVABLE


@given(case=camera_outage_subsets())
def test_pr_gob_09_coverage_state_matches_br_gob_97_for_every_outage(case: OutageCase) -> None:
    observable = (set(case.coverage.camera_ids) - case.down) | case.foreign_observable
    assert unsatisfiable_reason(case.coverage) is None
    assert coverage_state(case.coverage, observable) is _oracle(case)


@given(catalog=zone_catalog(), data=st.data())
def test_pr_gob_09_every_unsatisfiable_kit_catalog_is_rejected(
    catalog: dict[str, Any], data: st.DataObject
) -> None:
    catalog = {**catalog, "version": 1}
    assert unsatisfiable_reason(MinimumCoverage.of_catalog(catalog)) is None
    mutated = data.draw(mutate_unsatisfiable_coverage(catalog), label="mutated")
    assert unsatisfiable_reason(MinimumCoverage.of_catalog(mutated)) is not None
    coverage = mutated["minimum_coverage"]
    zone = ZoneRef(
        organization_id=uuid.UUID(catalog["organization_id"]),
        plant_id=uuid.UUID(catalog["plant_id"]),
        zone_id=uuid.UUID(catalog["zone_id"]),
        zone_code=catalog["zone_code"],
    )
    change = SetMinimumCoverage(
        required_count=coverage["required_count"],
        required_camera_ids=tuple(uuid.UUID(c) for c in coverage["required_camera_ids"]),
    )
    with pytest.raises(CatalogRuleViolated) as raised:
        plan_publication(
            CatalogState(catalog=catalog, single_occupancy=False, aggregation_window_minutes=60),
            change,
            zone=zone,
            issued_at=T0,
            declared_by=DECLARED_BY,
            reason_es=REASON,
            new_standard_id=uuid.uuid4(),
        )
    assert raised.value.violation is CatalogViolation.UNSATISFIABLE_COVERAGE


@given(scenario=catalog_versions())
def test_pr_gob_09_generated_unsatisfiable_coverages_are_rejected_and_satisfiable_ones_pass(
    scenario: CatalogScenario,
) -> None:
    run = _run(scenario, [timedelta(seconds=1)])
    for _, plan in run.published:
        assert unsatisfiable_reason(MinimumCoverage.of_catalog(plan.catalog)) is None
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
        except CatalogRuleViolated as violation:
            if intent is not None and intent.kind == "coverage":
                assert intent.data[0] is False  # solo las insatisfacibles se rechazan
                assert violation.violation is CatalogViolation.UNSATISFIABLE_COVERAGE
            continue
        if intent is not None and intent.kind == "coverage":
            assert intent.data[0] is True
        state = _state(plan)


# --- PR-GOB-18 -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world() -> Iterator[SigningWorld]:
    yield asyncio.run(bootstrapped_world())


def _keyset(world: SigningWorld) -> KeySet:
    keyset = KeySet(world.clock)
    keyset.pin_initial(
        [k.to_contract() for p in NODE_PURPOSES for k in world.service.public_keys(p)]
    )
    return keyset


def _round_trip(world: SigningWorld, catalog: dict[str, Any]) -> None:
    envelope = world.service.sign(SigningPurpose.CATALOG, catalog)
    document = envelope.to_json_value()  # type: ignore[union-attr]
    keyset = _keyset(world)
    payload = verify(document, keyset, expected_purpose="catalog", clock=world.clock)
    assert payload == catalog
    assert canonicalize(payload) == canonicalize(catalog)
    assert document["payload_canonical_sha256"] == canonical_sha256(catalog)
    # Como queda en ``jsonb``: el sobre leído de vuelta sigue verificando con la misma carga.
    stored = json.loads(json.dumps(document, ensure_ascii=False))
    assert verify(stored, keyset, expected_purpose="catalog", clock=world.clock) == catalog


@given(catalogs=zone_catalogs())
def test_pr_gob_18_kit_catalogs_round_trip_through_sign_and_the_u01_verifier(
    world: SigningWorld, catalogs: list[dict[str, Any]]
) -> None:
    for catalog in catalogs:
        _round_trip(world, catalog)


@given(scenario=catalog_versions(max_changes=4))
def test_pr_gob_18_composed_catalogs_round_trip_through_sign_and_the_u01_verifier(
    world: SigningWorld, scenario: CatalogScenario
) -> None:
    run = _run(scenario, [timedelta(seconds=1)])
    for _, plan in run.published:
        _round_trip(world, plan.catalog)
