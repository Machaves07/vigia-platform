"""Planificación pura de una versión del catálogo, gramática del predicado y vigencia (TASK-208).

Bordes de BR-GOB-01 a 12: ``changed_fields`` por variante, retiro del último estándar, zona sin
cámaras, de 1 a 32 estándares, de 1 a 8 cámaras con tasa de 1 a 60, ventana de agregación de 15
a 480, ``reason_es`` de 10 a 500, ``stream_reference`` y la marca unipersonal fuera del
``ZoneCatalog`` (NFR-GOB-38). La gramática cerrada del predicado rechaza ``presence: false``,
negación, disyunción, rol ``auxiliary``, la plantilla de otra familia y entradas hostiles, siempre
con ``PredicateInvalid``. ``standard_valid_at`` en los bordes del intervalo semiabierto. Solo
datos generados.
"""

from __future__ import annotations

import dataclasses
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.catalog.domain import predicates
from vigia_platform.catalog.domain.catalog_version import (
    ALL_CHANGED_FIELDS,
    CatalogRuleViolated,
    CatalogState,
    CatalogViolation,
    InitialZoneParameters,
    NewStandard,
    NewStandardVersion,
    PublicationPlan,
    RetireStandard,
    SetCameras,
    SetMinimumCoverage,
    SetSignals,
    SetSingleOccupancy,
    SetThresholds,
    SetWindows,
    StandardDraft,
    ZoneRef,
    checked_changed_fields,
    plan_publication,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.predicates import (
    PredicateInvalid,
    canonical_conditions,
    validate_predicate,
)
from vigia_platform.catalog.domain.standard import (
    DeclaredBy,
    DeclaredStandardVersion,
    standard_valid_at,
)
from vigia_platform.catalog.domain.zone_camera import ZoneCamera

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
ZONE = ZoneRef(
    organization_id=uuid.UUID(int=1, version=4),
    plant_id=uuid.UUID(int=2, version=4),
    zone_id=uuid.UUID(int=3, version=4),
    zone_code="ZN-01",
)
WHO = DeclaredBy(uuid.UUID(int=4, version=4), "Coordinación SST sintética", "administrator")
REASON = "Motivo sintético del cambio"
PRESENCE = {"presence": True}
ENERGY_ON = {"signal_role": "energy", "value": "asserted"}
ENERGY_OFF = {"signal_role": "energy", "value": "deasserted"}
GUARD_ON = {"signal_role": "guard", "value": "asserted"}
START_ON = {"signal_role": "start_command", "value": "asserted"}
COEXISTENCE = {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 0}


def _camera(n: int, **changes: Any) -> ZoneCamera:
    camera_id = uuid.UUID(int=100 + n, version=4)
    fields: dict[str, Any] = {
        "camera_id": camera_id,
        "code": f"CM-{n}",
        "role_in_zone": "primary" if n == 0 else "redundant",
        "declared_min_fps": 5.0,
        "stream_reference": f"cam-{n}",
    }
    fields.update(changes)
    return ZoneCamera(**fields)


def _initial(
    cameras: tuple[ZoneCamera, ...] | None = None, **changes: Any
) -> InitialZoneParameters:
    cameras = cameras if cameras is not None else (_camera(0), _camera(1))
    fields: dict[str, Any] = {
        "cameras": cameras,
        "required_count": 1,
        "required_camera_ids": (cameras[0].camera_id,) if cameras else (),
        "signals": (),
        "thresholds": {"review": 0.4, "publication": 0.7},
        "clip_window": {"pre_seconds": 10, "post_seconds": 10},
        "episode": {"grouping_window_ms": 3000, "max_segment_ms": 900000},
    }
    fields.update(changes)
    return InitialZoneParameters(**fields)


def _draft(family: str = "coexistence", predicate: Any = None) -> StandardDraft:
    return StandardDraft(
        family=PredicateFamily(family),
        title_es="Coexistencia en la celda",
        declared_text="Nadie permanece en la celda energizada.",
        predicate=COEXISTENCE if predicate is None else predicate,
    )


def _plan(
    previous: CatalogState | None,
    change: Any,
    *,
    reason: str = REASON,
    standard: int = 1,
    at: datetime = T0,
) -> PublicationPlan:
    return plan_publication(
        previous,
        change,
        zone=ZONE,
        issued_at=at,
        declared_by=WHO,
        reason_es=reason,
        new_standard_id=uuid.UUID(int=500 + standard, version=4),
    )


def _state(plan: PublicationPlan) -> CatalogState:
    return CatalogState(
        catalog=plan.catalog,
        single_occupancy=plan.single_occupancy,
        aggregation_window_minutes=plan.aggregation_window_minutes,
    )


def _first(**changes: Any) -> PublicationPlan:
    return _plan(None, NewStandard(draft=_draft(), initial=_initial(**changes)))


def _violation(previous: CatalogState | None, change: Any, **kwargs: Any) -> CatalogViolation:
    with pytest.raises(CatalogRuleViolated) as raised:
        _plan(previous, change, **kwargs)
    return raised.value.violation


# --- Versión 1 y numeración --------------------------------------------------------------------


def test_the_first_standard_creates_version_1_with_every_changed_field() -> None:
    plan = _first(single_occupancy=True, aggregation_window_minutes=90)

    assert plan.catalog_version == 1 == plan.catalog["version"]
    assert plan.changed_fields == ALL_CHANGED_FIELDS
    assert plan.single_occupancy is True and plan.aggregation_window_minutes == 90
    assert plan.single_occupancy_declared
    (born,) = plan.born
    assert (born.version, born.catalog_version, born.effective_from) == (1, 1, T0)
    assert plan.catalog["issued_at"] == "2026-10-01T08:00:00.000Z"
    assert plan.catalog["commissioning_watermark"] is True


def test_nfr_gob_38_neither_the_flag_nor_the_window_nor_stream_references_enter_the_catalog() -> (
    None
):
    plan = _first(single_occupancy=True)
    text = repr(plan.catalog)
    for forbidden in (
        "single_occupancy",
        "aggregation_window_minutes",
        "stream_reference",
        "cam-0",
    ):
        assert forbidden not in text


def test_each_change_adds_exactly_one_and_reports_its_field() -> None:
    first = _first()
    cameras = (_camera(0), _camera(1), _camera(2))
    changes: list[tuple[Any, tuple[CatalogChangedField, ...]]] = [
        (NewStandard(draft=_draft()), (CatalogChangedField.STANDARDS,)),
        (SetCameras(cameras=cameras), (CatalogChangedField.CAMERAS,)),
        (
            SetMinimumCoverage(required_count=2, required_camera_ids=(cameras[0].camera_id,)),
            (CatalogChangedField.MINIMUM_COVERAGE,),
        ),
        (SetSignals(signals=()), (CatalogChangedField.SIGNALS,)),
        (
            SetThresholds(thresholds={"review": 0.3, "publication": 1.0}),
            (CatalogChangedField.THRESHOLDS,),
        ),
        (
            SetWindows(clip_window={"pre_seconds": 5, "post_seconds": 30}),
            (CatalogChangedField.CLIP_WINDOW,),
        ),
        (
            SetWindows(
                clip_window={"pre_seconds": 6, "post_seconds": 6},
                episode={"grouping_window_ms": 1000, "max_segment_ms": 60000},
            ),
            (CatalogChangedField.CLIP_WINDOW, CatalogChangedField.EPISODE),
        ),
        (SetSingleOccupancy(single_occupancy=True), (CatalogChangedField.SINGLE_OCCUPANCY,)),
    ]
    state = _state(first)
    for index, (change, fields) in enumerate(changes, start=2):
        plan = _plan(state, change, standard=index)
        assert plan.catalog_version == index == plan.catalog["version"]
        assert plan.changed_fields == fields
        state = _state(plan)
    assert state.single_occupancy is True and state.aggregation_window_minutes == 60


def test_a_zone_without_catalog_and_without_initial_parameters_has_no_cameras() -> None:
    assert _violation(None, NewStandard(draft=_draft())) is CatalogViolation.ZONE_WITHOUT_CAMERAS
    assert _violation(None, SetThresholds(thresholds={"review": 0.1, "publication": 0.2})) is (
        CatalogViolation.ZONE_WITHOUT_CAMERAS
    )
    assert _violation(None, NewStandard(draft=_draft(), initial=_initial(cameras=()))) is (
        CatalogViolation.ZONE_WITHOUT_CAMERAS
    )


def test_initial_parameters_are_only_for_the_first_version() -> None:
    state = _state(_first())
    change = NewStandard(draft=_draft(), initial=_initial())
    assert _violation(state, change) is CatalogViolation.REQUEST_INVALID


# --- Estándares: versión nueva, retiro, límites ------------------------------------------------


def test_a_new_standard_version_keeps_family_and_closes_the_previous_one() -> None:
    first = _first()
    (born,) = first.born
    change = NewStandardVersion(
        standard_id=born.standard_id, declared_text="Texto declarado nuevo."
    )

    plan = _plan(_state(first), change)

    (successor,) = plan.born
    assert (successor.standard_id, successor.version, successor.family) == (
        born.standard_id,
        2,
        PredicateFamily.COEXISTENCE,
    )
    assert successor.title_es == born.title_es  # lo no pasado se conserva
    assert plan.closed == ((born.standard_id, 1),) and plan.retired == ()
    (standard,) = plan.catalog["standards"]
    assert (standard["version"], standard["declared_text"]) == (2, "Texto declarado nuevo.")


def test_a_new_version_with_another_family_template_is_predicate_invalid() -> None:
    first = _first()
    (born,) = first.born
    dwell = {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 5000}
    change = NewStandardVersion(standard_id=born.standard_id, predicate=dwell)
    assert _violation(_state(first), change) is CatalogViolation.PREDICATE_INVALID


def test_a_new_version_needs_some_change() -> None:
    with pytest.raises(CatalogRuleViolated):
        NewStandardVersion(standard_id=uuid.uuid4())


def test_retiring_closes_without_successor_and_the_last_one_cannot_go() -> None:
    first = _first()
    second = _plan(_state(first), NewStandard(draft=_draft()), standard=2)
    (born,) = first.born

    plan = _plan(_state(second), RetireStandard(standard_id=born.standard_id))

    assert plan.closed == plan.retired == ((born.standard_id, 1),) and plan.born == ()
    assert [s["standard_id"] for s in plan.catalog["standards"]] == [
        str(uuid.UUID(int=502, version=4))
    ]
    (last,) = plan.catalog["standards"]
    retire_last = RetireStandard(standard_id=uuid.UUID(last["standard_id"]))
    assert _violation(_state(plan), retire_last) is CatalogViolation.LAST_STANDARD_IN_ZONE


def test_unknown_standards_are_not_found() -> None:
    state = _state(_first())
    for change in (
        RetireStandard(standard_id=uuid.uuid4()),
        NewStandardVersion(standard_id=uuid.uuid4(), title_es="Otro título"),
    ):
        assert _violation(state, change) is CatalogViolation.STANDARD_NOT_FOUND


def test_from_one_to_thirty_two_standards() -> None:
    state = _state(_first())
    for index in range(2, 33):
        state = _state(_plan(state, NewStandard(draft=_draft()), standard=index))
    assert len(state.catalog["standards"]) == 32
    assert _violation(state, NewStandard(draft=_draft()), standard=33) is (
        CatalogViolation.REQUEST_INVALID
    )


@pytest.mark.parametrize(("length", "valid"), [(9, False), (10, True), (500, True), (501, False)])
def test_reason_from_10_to_500_characters(length: int, valid: bool) -> None:
    change = NewStandard(draft=_draft(), initial=_initial())
    if valid:
        assert _plan(None, change, reason="x" * length).catalog_version == 1
    else:
        assert _violation(None, change, reason="x" * length) is CatalogViolation.REQUEST_INVALID


# --- Cámaras y cobertura -----------------------------------------------------------------------


def test_from_one_to_eight_cameras_unique_and_required_ones_kept() -> None:
    state = _state(_first())
    eight = tuple(_camera(n) for n in range(8))
    assert _plan(state, SetCameras(cameras=eight)).catalog_version == 2
    nine = tuple(_camera(n) for n in range(9))
    assert _violation(state, SetCameras(cameras=nine)) is CatalogViolation.REQUEST_INVALID
    assert _violation(state, SetCameras(cameras=())) is CatalogViolation.ZONE_WITHOUT_CAMERAS
    twin = (_camera(0), _camera(1, code="CM-0"))
    assert _violation(state, SetCameras(cameras=twin)) is CatalogViolation.REQUEST_INVALID
    shared_stream = (_camera(0), _camera(1, stream_reference="cam-0"))
    assert _violation(state, SetCameras(cameras=shared_stream)) is CatalogViolation.REQUEST_INVALID
    # Sin la cámara requerida (la 0) la cobertura deja de ser satisfacible.
    without_required = (_camera(1), _camera(2))
    assert _violation(state, SetCameras(cameras=without_required)) is (
        CatalogViolation.UNSATISFIABLE_COVERAGE
    )


@pytest.mark.parametrize("fps", [0.99, 60.01, 0.0, math.nan, math.inf, True, "5"])
def test_declared_min_fps_outside_one_to_sixty_is_rejected(fps: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        _camera(0, declared_min_fps=fps)


@pytest.mark.parametrize("fps", [1, 1.0, 60.0])
def test_declared_min_fps_edges_are_accepted(fps: float) -> None:
    assert _camera(0, declared_min_fps=fps).declared_min_fps == float(fps)


@pytest.mark.parametrize(
    ("count", "required", "violation"),
    [
        (2, (0,), None),
        (3, (0,), CatalogViolation.UNSATISFIABLE_COVERAGE),  # más que cámaras
        (1, (0, 1), CatalogViolation.UNSATISFIABLE_COVERAGE),  # menos que requeridas
        (2, (0, 7), CatalogViolation.UNSATISFIABLE_COVERAGE),  # requerida ajena
        (0, (), CatalogViolation.UNSATISFIABLE_COVERAGE),
    ],
)
def test_minimum_coverage_must_be_satisfiable(
    count: int, required: tuple[int, ...], violation: CatalogViolation | None
) -> None:
    state = _state(_first())
    ids = tuple(uuid.UUID(int=100 + n, version=4) for n in required)
    change = SetMinimumCoverage(required_count=count, required_camera_ids=ids)
    if violation is None:
        assert _plan(state, change).catalog["minimum_coverage"]["required_count"] == count
    else:
        assert _violation(state, change) is violation


# --- Parámetros y marca unipersonal ------------------------------------------------------------


@pytest.mark.parametrize(
    "thresholds",
    [
        {"review": 0.5, "publication": 0.5},
        {"review": 0.0, "publication": 0.5},
        {"review": 0.5, "publication": 1.01},
        {"review": 0.5},
        {"review": "0.4", "publication": 0.9},
        {"review": 0.4, "publication": 0.9, "extra": 1},
    ],
)
def test_thresholds_outside_the_contract_are_invalid(thresholds: dict[str, Any]) -> None:
    change = SetThresholds(thresholds=thresholds)
    assert _violation(_state(_first()), change) is CatalogViolation.REQUEST_INVALID


def test_windows_need_at_least_one_part() -> None:
    with pytest.raises(CatalogRuleViolated):
        SetWindows()


@pytest.mark.parametrize(("minutes", "valid"), [(14, False), (15, True), (480, True), (481, False)])
def test_aggregation_window_from_15_to_480(minutes: int, valid: bool) -> None:
    state = _state(_first())
    change = SetSingleOccupancy(single_occupancy=True, aggregation_window_minutes=minutes)
    if valid:
        plan = _plan(state, change)
        assert plan.aggregation_window_minutes == minutes and plan.single_occupancy_declared
        assert plan.catalog == {**state.catalog, "version": 2}  # el catálogo no cambia
    else:
        assert _violation(state, change) is CatalogViolation.REQUEST_INVALID


def test_signals_with_auxiliary_role_are_declarable_but_never_in_a_predicate() -> None:
    state = _state(_first())
    auxiliary = {
        "signal_id": str(uuid.UUID(int=900, version=4)),
        "code": "SG-1",
        "role": "auxiliary",
        "asserted_level": "high",
        "source": {"reader": "plc-1", "channel": 3},
        "description_es": "Señal auxiliar",
    }
    assert _plan(state, SetSignals(signals=(auxiliary,))).catalog["signals"] == [auxiliary]
    with pytest.raises(PredicateInvalid):
        validate_predicate(
            "coexistence",
            {
                "all_of": [PRESENCE, {"signal_role": "auxiliary", "value": "asserted"}],
                "min_duration_ms": 0,
            },
        )


def test_changed_fields_from_one_to_eight_distinct_values() -> None:
    assert checked_changed_fields(["standards"]) == (CatalogChangedField.STANDARDS,)
    assert len(checked_changed_fields(ALL_CHANGED_FIELDS)) == 8
    for invalid in ([], ["standards", "standards"], ["productivity"]):
        with pytest.raises(ValueError):
            checked_changed_fields(invalid)


# --- Gramática cerrada del predicado -----------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "predicate"),
    [
        ("coexistence", COEXISTENCE),
        ("coexistence", {"all_of": [ENERGY_ON, PRESENCE], "min_duration_ms": 0}),
        ("guard_bypass", {"all_of": [PRESENCE, GUARD_ON], "min_duration_ms": 0}),
        ("guard_bypass", {"all_of": [GUARD_ON, ENERGY_ON, PRESENCE], "min_duration_ms": 0}),
        ("dwell", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 1000}),
        ("dwell", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 3_600_000}),
        ("startup_transition", {"all_of": [PRESENCE, START_ON], "min_duration_ms": 60_000}),
        ("startup_transition", {"all_of": [ENERGY_OFF, START_ON, PRESENCE], "min_duration_ms": 0}),
    ],
)
def test_each_family_template_is_accepted(family: str, predicate: dict[str, Any]) -> None:
    assert validate_predicate(family, predicate) == predicate


def _deep(levels: int) -> dict[str, Any]:
    node: dict[str, Any] = {"presence": True}
    for _ in range(levels):
        node = {"all_of": [node]}
    return node


@pytest.mark.parametrize(
    ("family", "predicate"),
    [
        ("coexistence", {"all_of": [{"presence": False}, ENERGY_ON], "min_duration_ms": 0}),
        ("coexistence", {"all_of": [PRESENCE, {"not": ENERGY_ON}], "min_duration_ms": 0}),
        ("coexistence", {"any_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 0}),
        ("coexistence", {"all_of": [PRESENCE, {"one_of": [ENERGY_ON]}], "min_duration_ms": 0}),
        (
            "coexistence",
            {
                "all_of": [PRESENCE, {"signal_role": "auxiliary", "value": "asserted"}],
                "min_duration_ms": 0,
            },
        ),
        ("coexistence", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 1}),
        ("coexistence", {"all_of": [PRESENCE, ENERGY_ON, ENERGY_ON], "min_duration_ms": 0}),
        ("coexistence", {"all_of": [PRESENCE], "min_duration_ms": 0}),
        ("coexistence", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 0, "count": 2}),
        ("dwell", COEXISTENCE),  # plantilla de otra familia
        ("guard_bypass", COEXISTENCE),
        ("dwell", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 3_600_001}),
        ("productivity", COEXISTENCE),
        ("coexistence", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": math.nan}),
        ("coexistence", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 10**40}),
        ("coexistence", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": "0"}),
        ("coexistence", {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": True}),
        ("coexistence", _deep(2000)),
        ("coexistence", {"all_of": ["x" * 10_000], "min_duration_ms": 0}),
        ("coexistence", {"all_of": [object()], "min_duration_ms": 0}),
        ("coexistence", [PRESENCE, ENERGY_ON]),
        ("coexistence", None),
    ],
)
def test_everything_outside_the_closed_grammar_is_predicate_invalid(
    family: str, predicate: Any
) -> None:
    with pytest.raises(PredicateInvalid):
        validate_predicate(family, predicate)


@pytest.mark.parametrize(
    "predicate",
    [
        {"all_of": [{"presence": False}, ENERGY_ON], "min_duration_ms": 0},
        {"all_of": [PRESENCE, {"not": ENERGY_ON}], "min_duration_ms": 0},
        {"any_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 0},
        {"all_of": [PRESENCE, {"signal_role": "auxiliary", "value": "asserted"}]},
    ],
)
def test_the_explicit_guard_does_not_depend_on_the_contract_schema(
    monkeypatch: pytest.MonkeyPatch, predicate: dict[str, Any]
) -> None:
    # Si una versión futura del esquema dejara pasar ausencia, negación, disyunción o el rol
    # auxiliar, la guarda explícita (business-rules §11) los sigue rechazando.
    monkeypatch.setattr(
        predicates.DeclaredStandard, "model_validate_json", classmethod(lambda cls, data: None)
    )
    assert validate_predicate("coexistence", COEXISTENCE) == COEXISTENCE
    with pytest.raises(PredicateInvalid):
        validate_predicate("coexistence", predicate)


def test_canonical_conditions_ignore_order() -> None:
    one = canonical_conditions({"all_of": [PRESENCE, GUARD_ON, ENERGY_ON]})
    two = canonical_conditions({"all_of": [ENERGY_ON, PRESENCE, GUARD_ON]})
    assert one == two == (("energy", "asserted"), ("guard", "asserted"), ("presence", "true"))


def test_a_draft_with_an_invalid_predicate_is_rejected_before_any_version() -> None:
    draft = _draft(predicate={"all_of": [{"presence": False}, ENERGY_ON], "min_duration_ms": 0})
    change = NewStandard(draft=draft, initial=_initial())
    assert _violation(None, change) is CatalogViolation.PREDICATE_INVALID


# --- Vigencia de un estándar (PR-GOB-06, ejemplos) ---------------------------------------------


def _version(version: int, start: datetime, end: datetime | None) -> DeclaredStandardVersion:
    return DeclaredStandardVersion(
        organization_id=ZONE.organization_id,
        plant_id=ZONE.plant_id,
        zone_id=ZONE.zone_id,
        standard_id=uuid.UUID(int=77, version=4),
        version=version,
        family=PredicateFamily.COEXISTENCE,
        title_es="Título",
        declared_text="Texto",
        declared_by=WHO,
        effective_from=start,
        predicate=COEXISTENCE,
        catalog_version=version,
        reason_es=REASON,
        retired_in_catalog_version=None if end is None else version + 1,
        effective_until=end,
    )


def test_standard_valid_at_uses_half_open_intervals() -> None:
    t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    history = [_version(1, T0, t1), _version(2, t1, t2), _version(3, t2, None)]
    standard_id = history[0].standard_id
    tick = timedelta(microseconds=1)

    assert standard_valid_at(history, standard_id, T0 - tick) is None
    assert standard_valid_at(history, standard_id, T0) is history[0]
    assert standard_valid_at(history, standard_id, t1 - tick) is history[0]
    assert standard_valid_at(history, standard_id, t1) is history[1]
    assert standard_valid_at(history, standard_id, t2) is history[2]
    assert standard_valid_at(history, standard_id, t2 + timedelta(days=900)) is history[2]
    assert standard_valid_at(history, uuid.uuid4(), t1) is None


def test_a_retired_standard_is_valid_nowhere_after_its_end() -> None:
    t1 = T0 + timedelta(minutes=5)
    history = [_version(1, T0, t1)]
    assert standard_valid_at(history, history[0].standard_id, t1) is None
    # Sustituida en el mismo milisegundo en que nació: no rige en ningún instante.
    empty = [_version(1, T0, T0), _version(2, T0, None)]
    assert standard_valid_at(empty, empty[0].standard_id, T0) is empty[1]


def test_overlapping_histories_and_naive_instants_fail() -> None:
    history = [_version(1, T0, None), _version(2, T0, None)]
    with pytest.raises(ValueError):
        standard_valid_at(history, history[0].standard_id, T0)
    with pytest.raises(ValueError):
        standard_valid_at(history[:1], history[0].standard_id, T0.replace(tzinfo=None))


def test_retirement_and_end_go_together() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(_version(1, T0, None), retired_in_catalog_version=2)
    with pytest.raises(ValueError):
        dataclasses.replace(_version(1, T0, None), effective_until=T0)
    with pytest.raises(ValueError):
        _version(1, T0, T0 - timedelta(seconds=1))
