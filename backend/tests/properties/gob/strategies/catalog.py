"""Generadores de U-03 para el catálogo versionado (tech-stack-decisions §2.6: ``catalog_versions``
y ``camera_outage_subsets``).

- ``catalog_versions()`` da un ``CatalogScenario``: la zona, su primera publicación (el primer
  estándar con ``InitialZoneParameters``, sacados de un ``zone_catalog()`` del kit de U-01, que
  solo produce catálogos satisfacibles) y una secuencia de **intenciones** de cambio: estándar
  nuevo, versión nueva de un estándar, retiro, cámaras, cobertura mínima, señales, umbrales,
  ventanas y marca unipersonal. ``resolve`` convierte cada intención en un cambio concreto sobre
  el estado vigente (el estándar n-ésimo que existe, las cámaras que conservan las requeridas…).
  Algunas intenciones producen cambios que **deben** rechazarse (retirar el último estándar,
  cobertura no satisfacible, más de 32 estándares): las propiedades comprueban que esos no
  consumen número.
- ``camera_outage_subsets()`` da una cobertura satisfacible con sus cámaras y un subconjunto de
  cámaras caídas (vacío, todas, solo requeridas, solo redundantes…), a veces con identificadores
  ajenos a la zona entre las observables.
- ``predicates(family)``: predicados válidos de la plantilla de cada familia (§3.2.1).

Solo datos generados (NFR-CTR-43). Las marcas de tiempo las pone la prueba con su reloj.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from hypothesis import strategies as st
from vigia_contracts.conformance.generators import zone_catalog
from vigia_contracts.models.enumerations import CameraRoleInZone, PredicateFamily

from vigia_platform.catalog.domain.catalog_version import (
    AGGREGATION_WINDOW_MAX,
    AGGREGATION_WINDOW_MIN,
    CatalogChange,
    CatalogState,
    InitialZoneParameters,
    NewStandard,
    NewStandardVersion,
    RetireStandard,
    SetCameras,
    SetMinimumCoverage,
    SetSignals,
    SetSingleOccupancy,
    SetThresholds,
    SetWindows,
    StandardDraft,
    ZoneRef,
)
from vigia_platform.catalog.domain.coverage import MinimumCoverage
from vigia_platform.catalog.domain.zone_camera import MAX_CAMERAS, ZoneCamera

__all__ = [
    "CatalogScenario",
    "Intent",
    "OutageCase",
    "camera_outage_subsets",
    "catalog_versions",
    "predicates",
    "resolve",
]

_PRESENCE: Final = {"presence": True}
_ENERGY_ON: Final = {"signal_role": "energy", "value": "asserted"}
_ENERGY_OFF: Final = {"signal_role": "energy", "value": "deasserted"}
_GUARD_ON: Final = {"signal_role": "guard", "value": "asserted"}
_START_ON: Final = {"signal_role": "start_command", "value": "asserted"}
_TEMPLATES: Final[Mapping[str, tuple[tuple[Mapping[str, Any], ...], tuple[Any, ...], int, int]]] = {
    "coexistence": ((_PRESENCE, _ENERGY_ON), (), 0, 0),
    "guard_bypass": ((_PRESENCE, _GUARD_ON), (_ENERGY_ON,), 0, 0),
    "dwell": ((_PRESENCE, _ENERGY_ON), (), 1_000, 3_600_000),
    "startup_transition": ((_PRESENCE, _START_ON), (_ENERGY_OFF,), 0, 60_000),
}
"""Plantillas de §3.2.1, escritas aparte del dominio: obligatorias, opcionales y duración."""

TITLES: Final = (
    "Coexistencia en la celda de soldadura",
    "Resguardo cerrado con persona dentro",
    "Permanencia junto a la prensa",
    "Arranque desde reposo de la banda",
    "Estándar de zona de carga",
)
TEXTS: Final = (
    "Nadie permanece en la celda mientras la máquina está energizada.",
    "El resguardo no se cierra con una persona dentro de la zona.",
    "La permanencia junto a la prensa energizada no supera el tiempo declarado.",
    "La banda no arranca desde reposo con presencia en la zona.",
)
SIGNAL_DESCRIPTIONS: Final = ("Energía de la prensa", "Resguardo frontal", "Mando de arranque")


@st.composite
def predicates(draw: st.DrawFn, family: str) -> dict[str, Any]:
    """Un predicado válido de la plantilla de ``family``, en cualquier orden."""
    required, optional, low, high = _TEMPLATES[family]
    conditions = [dict(c) for c in required] + [dict(c) for c in optional if draw(st.booleans())]
    conditions = draw(st.permutations(conditions))
    return {"all_of": list(conditions), "min_duration_ms": draw(st.integers(low, high))}


def predicates_by_family() -> st.SearchStrategy[dict[str, dict[str, Any]]]:
    return st.fixed_dictionaries({family: predicates(family) for family in _TEMPLATES})


@st.composite
def drafts(draw: st.DrawFn) -> StandardDraft:
    family = draw(st.sampled_from(PredicateFamily))
    return StandardDraft(
        family=family,
        title_es=draw(st.sampled_from(TITLES)),
        declared_text=draw(st.sampled_from(TEXTS)),
        predicate=draw(predicates(family.value)),
    )


def _stream(camera_id: uuid.UUID) -> str:
    return f"cam-{camera_id.hex[:12]}"


@st.composite
def new_cameras(
    draw: st.DrawFn, min_size: int = 1, max_size: int = MAX_CAMERAS
) -> tuple[ZoneCamera, ...]:
    """Cámaras con ``camera_id``, ``code`` y ``stream_reference`` únicos."""
    count = draw(st.integers(min_size, max_size))
    ids = draw(st.lists(st.uuids(version=4), min_size=count, max_size=count, unique=True))
    tag = draw(st.integers(0, 9999))
    return tuple(
        ZoneCamera(
            camera_id=camera_id,
            code=f"CM-{tag}-{index}",
            role_in_zone=draw(st.sampled_from(CameraRoleInZone)),
            declared_min_fps=draw(st.sampled_from((1.0, 5.0, 12.5, 30.0, 60.0))),
            stream_reference=_stream(camera_id),
        )
        for index, camera_id in enumerate(ids)
    )


@st.composite
def signal_declarations(draw: st.DrawFn) -> tuple[dict[str, Any], ...]:
    count = draw(st.integers(0, 4))
    ids = draw(st.lists(st.uuids(version=4), min_size=count, max_size=count, unique=True))
    return tuple(
        {
            "signal_id": str(signal_id),
            "code": f"SG-{index}",
            "role": draw(st.sampled_from(("energy", "guard", "start_command", "auxiliary"))),
            "asserted_level": draw(st.sampled_from(("high", "low"))),
            "source": {"reader": "plc-1", "channel": draw(st.integers(0, 255))},
            "description_es": draw(st.sampled_from(SIGNAL_DESCRIPTIONS)),
        }
        for index, signal_id in enumerate(ids)
    )


@st.composite
def thresholds(draw: st.DrawFn) -> dict[str, float]:
    review = draw(st.floats(0.01, 0.98, allow_nan=False))
    publication = draw(st.floats(review + 0.001, 1.0, allow_nan=False))
    return {"review": review, "publication": publication}


@dataclass(frozen=True)
class Intent:
    """Un cambio por resolver contra el estado vigente."""

    kind: str
    data: Any


def _intents() -> st.SearchStrategy[Intent]:
    windows = st.tuples(
        st.none()
        | st.fixed_dictionaries(
            {"pre_seconds": st.integers(5, 30), "post_seconds": st.integers(5, 30)}
        ),
        st.none()
        | st.fixed_dictionaries(
            {
                "grouping_window_ms": st.integers(1_000, 300_000),
                "max_segment_ms": st.integers(60_000, 3_600_000),
            }
        ),
    ).filter(lambda pair: pair != (None, None))
    return st.one_of(
        drafts().map(lambda d: Intent("new_standard", d)),
        st.tuples(
            st.integers(0, 64),
            st.none() | st.sampled_from(TITLES),
            st.none() | st.sampled_from(TEXTS),
            st.none() | predicates_by_family(),
        ).map(lambda t: Intent("new_version", t)),
        st.integers(0, 64).map(lambda i: Intent("retire", i)),
        st.tuples(st.booleans(), new_cameras(1, 4)).map(lambda t: Intent("cameras", t)),
        st.tuples(st.booleans(), st.integers(0, 255), st.integers(0, 255)).map(
            lambda t: Intent("coverage", t)
        ),
        signal_declarations().map(lambda s: Intent("signals", s)),
        thresholds().map(lambda t: Intent("thresholds", t)),
        windows.map(lambda w: Intent("windows", w)),
        st.tuples(st.booleans(), st.integers(AGGREGATION_WINDOW_MIN, AGGREGATION_WINDOW_MAX)).map(
            lambda t: Intent("occupancy", t)
        ),
    )


@dataclass(frozen=True)
class CatalogScenario:
    zone: ZoneRef
    first: NewStandard
    intents: tuple[Intent, ...]


@st.composite
def catalog_versions(draw: st.DrawFn, max_changes: int = 8) -> CatalogScenario:
    """Una zona, su primera publicación y hasta ``max_changes`` intenciones de cambio."""
    seed = draw(zone_catalog())
    zone = ZoneRef(
        organization_id=uuid.UUID(seed["organization_id"]),
        plant_id=uuid.UUID(seed["plant_id"]),
        zone_id=uuid.UUID(seed["zone_id"]),
        zone_code=seed["zone_code"],
    )
    cameras = tuple(
        ZoneCamera(
            camera_id=uuid.UUID(c["camera_id"]),
            code=c["code"],
            role_in_zone=CameraRoleInZone(c["role_in_zone"]),
            declared_min_fps=c["declared_min_fps"],
            stream_reference=_stream(uuid.UUID(c["camera_id"])),
        )
        for c in seed["cameras"]
    )
    coverage = seed["minimum_coverage"]
    initial = InitialZoneParameters(
        cameras=cameras,
        required_count=coverage["required_count"],
        required_camera_ids=tuple(uuid.UUID(c) for c in coverage["required_camera_ids"]),
        signals=tuple(draw(signal_declarations())),
        thresholds=seed["thresholds"],
        clip_window=seed["clip_window"],
        episode=seed["episode"],
        single_occupancy=draw(st.booleans()),
        aggregation_window_minutes=draw(
            st.integers(AGGREGATION_WINDOW_MIN, AGGREGATION_WINDOW_MAX)
        ),
    )
    first = NewStandard(draft=draw(drafts()), initial=initial)
    intents = draw(st.lists(_intents(), max_size=max_changes))
    return CatalogScenario(zone, first, tuple(intents))


def resolve(intent: Intent, state: CatalogState) -> CatalogChange:
    """El cambio concreto de ``intent`` sobre el catálogo vigente ``state``."""
    catalog = state.catalog
    standards = catalog["standards"]
    match intent.kind:
        case "new_standard":
            draft: StandardDraft = intent.data
            return NewStandard(draft=draft)
        case "new_version":
            index, title, text, by_family = intent.data
            target = standards[index % len(standards)]
            predicate = None if by_family is None else by_family[target["family"]]
            if title is None and text is None and predicate is None:
                title = TITLES[index % len(TITLES)]
            return NewStandardVersion(
                standard_id=uuid.UUID(target["standard_id"]),
                title_es=title,
                declared_text=text,
                predicate=predicate,
            )
        case "retire":
            target = standards[intent.data % len(standards)]
            return RetireStandard(standard_id=uuid.UUID(target["standard_id"]))
        case "cameras":
            keep, drawn = intent.data
            if not keep:
                return SetCameras(cameras=drawn)
            return SetCameras(cameras=_keeping_coverage(catalog, drawn))
        case "coverage":
            satisfiable, a, b = intent.data
            return _coverage_change(catalog, satisfiable=satisfiable, a=a, b=b)
        case "signals":
            return SetSignals(signals=intent.data)
        case "thresholds":
            return SetThresholds(thresholds=intent.data)
        case "windows":
            clip_window, episode = intent.data
            return SetWindows(clip_window=clip_window, episode=episode)
        case "occupancy":
            flag, minutes = intent.data
            return SetSingleOccupancy(single_occupancy=flag, aggregation_window_minutes=minutes)
    raise AssertionError(intent.kind)


def _current_cameras(catalog: Mapping[str, Any]) -> list[ZoneCamera]:
    return [
        ZoneCamera(
            camera_id=uuid.UUID(c["camera_id"]),
            code=c["code"],
            role_in_zone=CameraRoleInZone(c["role_in_zone"]),
            declared_min_fps=c["declared_min_fps"],
            stream_reference=_stream(uuid.UUID(c["camera_id"])),
        )
        for c in catalog["cameras"]
    ]


def _keeping_coverage(
    catalog: Mapping[str, Any], drawn: tuple[ZoneCamera, ...]
) -> tuple[ZoneCamera, ...]:
    """Cámaras nuevas que conservan las requeridas y al menos ``required_count``."""
    coverage = catalog["minimum_coverage"]
    current = _current_cameras(catalog)
    required = {uuid.UUID(c) for c in coverage["required_camera_ids"]}
    kept = [c for c in current if c.camera_id in required]
    used_codes = {c.code for c in current}
    used_ids = {c.camera_id for c in current}
    extra = [c for c in drawn if c.code not in used_codes and c.camera_id not in used_ids]
    chosen = kept + extra
    for camera in current:
        if len(chosen) >= coverage["required_count"]:
            break
        if camera not in chosen:
            chosen.append(camera)
    return tuple(chosen[:MAX_CAMERAS])


def _coverage_change(
    catalog: Mapping[str, Any], *, satisfiable: bool, a: int, b: int
) -> SetMinimumCoverage:
    cameras = [uuid.UUID(c["camera_id"]) for c in catalog["cameras"]]
    required = tuple(c for index, c in enumerate(cameras) if a >> index & 1)
    low = max(1, len(required))
    if satisfiable:
        count = low + b % (len(cameras) - low + 1)
        return SetMinimumCoverage(required_count=count, required_camera_ids=required)
    if b % 2:
        return SetMinimumCoverage(required_count=len(cameras) + 1, required_camera_ids=required)
    # Una requerida que no es cámara de la zona.
    foreign = uuid.UUID(int=a * 257 + b + 1, version=4)
    return SetMinimumCoverage(
        required_count=len(required) + 1, required_camera_ids=(*required, foreign)
    )


@dataclass(frozen=True)
class OutageCase:
    coverage: MinimumCoverage
    down: frozenset[uuid.UUID]
    foreign_observable: frozenset[uuid.UUID]


@st.composite
def camera_outage_subsets(draw: st.DrawFn) -> OutageCase:
    """Una cobertura satisfacible y las cámaras caídas (con bordes: ninguna, todas…)."""
    count = draw(st.integers(1, MAX_CAMERAS))
    cameras = tuple(
        draw(st.lists(st.uuids(version=4), min_size=count, max_size=count, unique=True))
    )
    required = tuple(c for c in cameras if draw(st.booleans()))
    required_count = draw(st.integers(max(1, len(required)), count))
    shape = draw(st.sampled_from(("any", "none", "all", "required_only", "redundant_only")))
    if shape == "none":
        down: frozenset[uuid.UUID] = frozenset()
    elif shape == "all":
        down = frozenset(cameras)
    elif shape == "required_only":
        down = frozenset(c for c in required if draw(st.booleans()))
    elif shape == "redundant_only":
        down = frozenset(c for c in cameras if c not in required and draw(st.booleans()))
    else:
        down = frozenset(c for c in cameras if draw(st.booleans()))
    foreign = frozenset(draw(st.lists(st.uuids(version=4), max_size=2)))
    return OutageCase(
        coverage=MinimumCoverage(
            required_count=required_count, required_camera_ids=required, camera_ids=cameras
        ),
        down=down,
        foreign_observable=foreign - set(cameras),
    )
