"""``ZoneCatalogVersion`` y la planificación pura de una versión nueva (DE §2.1; BR-GOB-01 a 12).

El catálogo es lo que cada hallazgo cita y lo que el nodo aplica. Cualquier cambio produce la
versión siguiente de la zona: ``catalog_version`` crece de uno en uno, nunca se reutiliza ni se
salta (BR-GOB-01), y una versión emitida no se edita (BR-GOB-02).

``plan_publication`` es la parte pura del servicio de publicación (sin base, sin firma, sin hora
del sistema): a partir de la versión vigente (o de ninguna) y de **un** cambio compone el
``ZoneCatalog`` canónico con el lector estricto de ``vigia-contracts`` (``version =
catalog_version``) y dice qué versiones de estándar nacen, cuáles se cierran y qué campos
cambiaron. Las variantes de cambio son las de S-PLA-04 y las rutas de parámetros (nota de
business-rules §1): estándar nuevo (el primero de la zona crea la versión 1 con
``InitialZoneParameters``), versión nueva de un estándar, retiro, cámaras, cobertura mínima,
señales, umbrales, ventanas y marca unipersonal.

``single_occupancy`` y ``aggregation_window_minutes`` (15 a 480, 60 por defecto) se versionan con
el catálogo pero **nunca** entran en el ``ZoneCatalog`` firmado (BR-GOB-10, NFR-GOB-38): el
esquema cerrado del contrato ni siquiera los admite.

Cada regla incumplida es ``CatalogRuleViolated`` con su ``CatalogViolation``; la aplicación la
traduce al ``detail_code`` del catálogo. Ninguna se comprueba después de escribir nada.
"""

from __future__ import annotations

import enum
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from pydantic import ValidationError
from vigia_contracts.models.enumerations import PredicateFamily
from vigia_contracts.models.zone_catalog import ZoneCatalog

from vigia_platform.catalog.domain.coverage import MinimumCoverage, unsatisfiable_reason
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.predicates import PredicateInvalid, validate_predicate
from vigia_platform.catalog.domain.standard import (
    MAX_STANDARDS,
    DeclaredBy,
    DeclaredStandardVersion,
    contract_timestamp,
)
from vigia_platform.catalog.domain.zone_camera import MAX_CAMERAS, ZoneCamera
from vigia_platform.shared.context import Role

__all__ = [
    "AGGREGATION_WINDOW_DEFAULT",
    "AGGREGATION_WINDOW_MAX",
    "AGGREGATION_WINDOW_MIN",
    "ALL_CHANGED_FIELDS",
    "MAX_REASON_CHARS",
    "MIN_REASON_CHARS",
    "CatalogChange",
    "CatalogRuleViolated",
    "CatalogState",
    "CatalogViolation",
    "InitialZoneParameters",
    "NewStandard",
    "NewStandardVersion",
    "PublicationPlan",
    "RetireStandard",
    "SetCameras",
    "SetMinimumCoverage",
    "SetSignals",
    "SetSingleOccupancy",
    "SetThresholds",
    "SetWindows",
    "StandardDraft",
    "ZoneCatalogVersion",
    "ZoneRef",
    "checked_changed_fields",
    "plan_publication",
]

MIN_REASON_CHARS: Final = 10
MAX_REASON_CHARS: Final = 500
"""``reason_es`` de 10 a 500 caracteres `[estimación propia]` (DE §2.1 y §2.2)."""
AGGREGATION_WINDOW_MIN: Final = 15
AGGREGATION_WINDOW_MAX: Final = 480
AGGREGATION_WINDOW_DEFAULT: Final = 60
"""``aggregation_window_minutes``: de 15 a 480, 60 por defecto `[estimación propia]` (BR-GOB-10)."""
ALL_CHANGED_FIELDS: Final = tuple(CatalogChangedField)
"""Lo que declara la versión 1 de una zona: todo el catálogo y la marca unipersonal."""
MAX_CHANGED_FIELDS: Final = 8


class CatalogViolation(enum.StrEnum):
    """Reglas del catálogo que una publicación puede incumplir (sin escribir nada)."""

    PREDICATE_INVALID = "predicate_invalid"
    UNSATISFIABLE_COVERAGE = "unsatisfiable_coverage"
    ZONE_WITHOUT_CAMERAS = "zone_without_cameras"
    LAST_STANDARD_IN_ZONE = "last_standard_in_zone"
    STANDARD_NOT_FOUND = "standard_not_found"
    REQUEST_INVALID = "request_invalid"
    """Un límite del contrato o de esta entidad (más de 32 estándares, cámaras repetidas, una
    tasa fuera de 1 a 60, umbrales incoherentes…): ``invalid_request`` sin ``detail_code``."""


class CatalogRuleViolated(ValueError):
    """Una regla del catálogo no se cumple; el mensaje es genérico, sin valores recibidos."""

    def __init__(self, violation: CatalogViolation) -> None:
        super().__init__(f"regla del catálogo incumplida: {violation.value}")
        self.violation = CatalogViolation(violation)


def checked_changed_fields(
    fields: Iterable[CatalogChangedField | str],
) -> tuple[CatalogChangedField, ...]:
    """``changed_fields`` de 1 a 8 valores **distintos** de la lista cerrada (BR-GOB-03)."""
    values = tuple(CatalogChangedField(value) for value in fields)
    if not 1 <= len(values) <= MAX_CHANGED_FIELDS or len(set(values)) != len(values):
        raise ValueError("changed_fields lleva de 1 a 8 valores distintos")
    return values


# --- Entidad -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneCatalogVersion:
    """Una versión emitida del catálogo de una zona con su sobre firmado conservado (DE §2.1)."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    catalog_version: int
    issued_at: datetime
    issued_by: uuid.UUID
    role_in_use: Role
    reason_es: str
    changed_fields: tuple[CatalogChangedField, ...]
    payload: Mapping[str, Any]
    envelope: Mapping[str, Any]
    single_occupancy: bool
    aggregation_window_minutes: int
    ledger_record_id: uuid.UUID
    superseded_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("organization_id", "plant_id", "zone_id", "issued_by", "ledger_record_id"):
            if type(getattr(self, name)) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        if type(self.catalog_version) is not int or self.catalog_version < 1:
            raise ValueError("catalog_version debe ser un entero ≥ 1")
        if self.issued_at.utcoffset() is None:
            raise ValueError("issued_at debe llevar zona horaria")
        object.__setattr__(self, "role_in_use", Role(self.role_in_use))
        object.__setattr__(self, "changed_fields", checked_changed_fields(self.changed_fields))
        if type(self.single_occupancy) is not bool:
            raise TypeError("single_occupancy debe ser bool")
        window = self.aggregation_window_minutes
        if (
            type(window) is not int
            or not AGGREGATION_WINDOW_MIN <= window <= AGGREGATION_WINDOW_MAX
        ):
            raise ValueError("aggregation_window_minutes debe estar entre 15 y 480")
        if self.superseded_at is not None and self.superseded_at < self.issued_at:
            raise ValueError("superseded_at no precede a issued_at")


# --- Cambios -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class StandardDraft:
    """Un estándar nuevo tal como lo declara la planta (la aplicación pasa sus textos por la
    política de texto libre antes de planificar)."""

    family: PredicateFamily
    title_es: str
    declared_text: str
    predicate: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", PredicateFamily(self.family))
        _check_text(self.title_es)
        _check_text(self.declared_text)


@dataclass(frozen=True, slots=True, kw_only=True)
class InitialZoneParameters:
    """Lo que la versión 1 de una zona necesita además de su primer estándar.

    El diseño no fija umbrales ni cobertura por defecto: la primera publicación los declara
    todos. Las formas de ``signals``, ``thresholds``, ``clip_window`` y ``episode`` son las del
    contrato (``SignalDeclaration``, ``Thresholds``, ``ClipWindow``, ``EpisodeParameters``).
    """

    cameras: tuple[ZoneCamera, ...]
    required_count: int
    required_camera_ids: tuple[uuid.UUID, ...]
    signals: tuple[Mapping[str, Any], ...]
    thresholds: Mapping[str, Any]
    clip_window: Mapping[str, Any]
    episode: Mapping[str, Any]
    single_occupancy: bool = False
    aggregation_window_minutes: int = AGGREGATION_WINDOW_DEFAULT


@dataclass(frozen=True, slots=True, kw_only=True)
class NewStandard:
    """Alta de un estándar; con ``initial`` si la zona aún no tiene catálogo."""

    draft: StandardDraft
    initial: InitialZoneParameters | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class NewStandardVersion:
    """Versión nueva de un estándar vigente: lo que no se pasa se conserva (BR-GOB-05)."""

    standard_id: uuid.UUID
    title_es: str | None = None
    declared_text: str | None = None
    predicate: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.title_es is None and self.declared_text is None and self.predicate is None:
            raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
        if self.title_es is not None:
            _check_text(self.title_es)
        if self.declared_text is not None:
            _check_text(self.declared_text)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetireStandard:
    """Retiro: versión nueva del catálogo sin el estándar, nunca un borrado (BR-GOB-09)."""

    standard_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class SetCameras:
    """``PUT /zones/{zone_id}/cameras``: de 1 a 8 cámaras."""

    cameras: tuple[ZoneCamera, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SetMinimumCoverage:
    """``PUT /zones/{zone_id}/minimum-coverage``."""

    required_count: int
    required_camera_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SetSignals:
    """``PUT /zones/{zone_id}/signals``: de 0 a 32 ``SignalDeclaration``."""

    signals: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class SetThresholds:
    """``PUT /zones/{zone_id}/thresholds``: ``0 < review < publication ≤ 1``."""

    thresholds: Mapping[str, Any]


@dataclass(frozen=True, slots=True, kw_only=True)
class SetWindows:
    """``PUT /zones/{zone_id}/windows``: ventana de clip, episodio o los dos."""

    clip_window: Mapping[str, Any] | None = None
    episode: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.clip_window is None and self.episode is None:
            raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)


@dataclass(frozen=True, slots=True, kw_only=True)
class SetSingleOccupancy:
    """``PUT .../single-occupancy``: la marca unipersonal y su ventana de agregación."""

    single_occupancy: bool
    aggregation_window_minutes: int = AGGREGATION_WINDOW_DEFAULT


CatalogChange = (
    NewStandard
    | NewStandardVersion
    | RetireStandard
    | SetCameras
    | SetMinimumCoverage
    | SetSignals
    | SetThresholds
    | SetWindows
    | SetSingleOccupancy
)


# --- Estado y plan -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneRef:
    """La zona del catálogo, con lo que el ``ZoneCatalog`` lleva de ella."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    zone_code: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogState:
    """Lo que la versión vigente aporta a la siguiente: su ``ZoneCatalog`` y la marca."""

    catalog: Mapping[str, Any]
    single_occupancy: bool
    aggregation_window_minutes: int
    stream_references: Mapping[uuid.UUID, str] = field(default_factory=dict)
    """``stream_reference`` de cada cámara (``zone_camera``): nunca viaja en el catálogo."""

    @property
    def catalog_version(self) -> int:
        version: int = self.catalog["version"]
        return version


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationPlan:
    """Lo que escribe la publicación, en una sola transacción."""

    catalog_version: int
    catalog: dict[str, Any]
    """El ``ZoneCatalog`` canónico (forma JSON validada por el contrato) que se firma."""
    changed_fields: tuple[CatalogChangedField, ...]
    born: tuple[DeclaredStandardVersion, ...]
    """Versiones de estándar que nacen en esta versión del catálogo."""
    closed: tuple[tuple[uuid.UUID, int], ...]
    """``(standard_id, version)`` vigentes que esta versión cierra (sustituidas o retiradas)."""
    retired: tuple[tuple[uuid.UUID, int], ...]
    """Las cerradas sin sucesora: llevan ``catalog_standard_retired``."""
    cameras: tuple[ZoneCamera, ...] | None
    """Cámaras declaradas que se proyectan en ``zone_camera`` (``None`` si no cambian)."""
    single_occupancy: bool
    aggregation_window_minutes: int
    single_occupancy_declared: bool
    """¿Escribe ``single_occupancy_declared``? (la marca se declara en esta versión)."""


def _check_text(value: object) -> None:
    # Solo el tipo: la longitud y el contenido los decide la política de texto libre en la
    # aplicación (``free_text_rejected``) y, al componer, el lector del contrato.
    if not isinstance(value, str):
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)


def _check_window(minutes: object) -> int:
    if type(minutes) is not int or not AGGREGATION_WINDOW_MIN <= minutes <= AGGREGATION_WINDOW_MAX:
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
    return minutes


def _check_cameras(cameras: Sequence[ZoneCamera]) -> tuple[ZoneCamera, ...]:
    cameras = tuple(cameras)
    if not cameras:
        raise CatalogRuleViolated(CatalogViolation.ZONE_WITHOUT_CAMERAS)
    if len(cameras) > MAX_CAMERAS or any(not isinstance(c, ZoneCamera) for c in cameras):
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
    for key in ("camera_id", "code", "stream_reference"):
        values = [getattr(camera, key) for camera in cameras]
        if len(set(values)) != len(values):
            raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
    return cameras


def _coverage(required_count: object, required_camera_ids: Iterable[object]) -> dict[str, Any]:
    required = tuple(required_camera_ids)
    if type(required_count) is not int or any(type(c) is not uuid.UUID for c in required):
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
    return {"required_count": required_count, "required_camera_ids": [str(c) for c in required]}


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
    return dict(value)


def _standard_version(
    zone: ZoneRef,
    *,
    standard_id: uuid.UUID,
    version: int,
    family: PredicateFamily,
    title_es: str,
    declared_text: str,
    predicate: Mapping[str, Any],
    catalog_version: int,
    issued_at: datetime,
    declared_by: DeclaredBy,
    reason_es: str,
) -> DeclaredStandardVersion:
    try:
        checked = validate_predicate(family, predicate)
    except PredicateInvalid:
        raise CatalogRuleViolated(CatalogViolation.PREDICATE_INVALID) from None
    return DeclaredStandardVersion(
        organization_id=zone.organization_id,
        plant_id=zone.plant_id,
        zone_id=zone.zone_id,
        standard_id=standard_id,
        version=version,
        family=family,
        title_es=title_es,
        declared_text=declared_text,
        declared_by=declared_by,
        effective_from=issued_at,
        predicate=checked,
        catalog_version=catalog_version,
        reason_es=reason_es,
    )


def plan_publication(
    previous: CatalogState | None,
    change: CatalogChange,
    *,
    zone: ZoneRef,
    issued_at: datetime,
    declared_by: DeclaredBy,
    reason_es: str,
    new_standard_id: uuid.UUID,
) -> PublicationPlan:
    """La versión siguiente del catálogo de ``zone`` tras ``change``; sin efectos.

    ``catalog_version`` es la vigente más uno, o 1 si la zona no tiene catálogo (BR-GOB-01).
    ``new_standard_id`` solo se usa si ``change`` da de alta un estándar. Una regla incumplida
    lanza ``CatalogRuleViolated`` antes de devolver nada.
    """
    if not isinstance(reason_es, str) or not MIN_REASON_CHARS <= len(reason_es) <= MAX_REASON_CHARS:
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
    if previous is not None and previous.catalog["zone_id"] != str(zone.zone_id):
        raise ValueError("la versión vigente es de otra zona")
    version = 1 if previous is None else previous.catalog_version + 1
    if previous is None and not (isinstance(change, NewStandard) and change.initial is not None):
        # Sin catálogo no hay cámaras declaradas: la versión 1 la crea el primer estándar con los
        # parámetros iniciales de la zona.
        raise CatalogRuleViolated(CatalogViolation.ZONE_WITHOUT_CAMERAS)
    base: dict[str, Any] = {} if previous is None else dict(previous.catalog)
    standards: list[dict[str, Any]] = [dict(s) for s in base.get("standards", ())]
    single_occupancy = False if previous is None else previous.single_occupancy
    window = AGGREGATION_WINDOW_DEFAULT if previous is None else previous.aggregation_window_minutes
    born: list[DeclaredStandardVersion] = []
    closed: list[tuple[uuid.UUID, int]] = []
    retired: list[tuple[uuid.UUID, int]] = []
    cameras: tuple[ZoneCamera, ...] | None = None
    declared_occupancy = False
    changed: tuple[CatalogChangedField, ...]

    def current(standard_id: uuid.UUID) -> int:
        for index, standard in enumerate(standards):
            if standard["standard_id"] == str(standard_id):
                return index
        raise CatalogRuleViolated(CatalogViolation.STANDARD_NOT_FOUND)

    match change:
        case NewStandard(draft=draft, initial=initial):
            if initial is not None:
                if previous is not None:
                    raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
                cameras = _check_cameras(initial.cameras)
                base.update(
                    cameras=[c.catalog_camera() for c in cameras],
                    minimum_coverage=_coverage(initial.required_count, initial.required_camera_ids),
                    signals=[_mapping(s) for s in initial.signals],
                    thresholds=_mapping(initial.thresholds),
                    clip_window=_mapping(initial.clip_window),
                    episode=_mapping(initial.episode),
                )
                single_occupancy = initial.single_occupancy
                window = initial.aggregation_window_minutes
                declared_occupancy = True
                changed = ALL_CHANGED_FIELDS
            else:
                changed = (CatalogChangedField.STANDARDS,)
            standard = _standard_version(
                zone,
                standard_id=new_standard_id,
                version=1,
                family=draft.family,
                title_es=draft.title_es,
                declared_text=draft.declared_text,
                predicate=draft.predicate,
                catalog_version=version,
                issued_at=issued_at,
                declared_by=declared_by,
                reason_es=reason_es,
            )
            if any(s["standard_id"] == str(new_standard_id) for s in standards):
                raise ValueError("new_standard_id ya existe en la zona")
            born.append(standard)
            standards.append(standard.contract())
        case NewStandardVersion(standard_id=standard_id):
            index = current(standard_id)
            old = standards[index]
            standard = _standard_version(
                zone,
                standard_id=standard_id,
                version=int(old["version"]) + 1,
                family=PredicateFamily(old["family"]),
                title_es=change.title_es if change.title_es is not None else old["title_es"],
                declared_text=(
                    change.declared_text
                    if change.declared_text is not None
                    else old["declared_text"]
                ),
                predicate=change.predicate if change.predicate is not None else old["predicate"],
                catalog_version=version,
                issued_at=issued_at,
                declared_by=declared_by,
                reason_es=reason_es,
            )
            born.append(standard)
            closed.append((standard_id, int(old["version"])))
            standards[index] = standard.contract()
            changed = (CatalogChangedField.STANDARDS,)
        case RetireStandard(standard_id=standard_id):
            index = current(standard_id)
            if len(standards) == 1:
                raise CatalogRuleViolated(CatalogViolation.LAST_STANDARD_IN_ZONE)
            old = standards.pop(index)
            closed.append((standard_id, int(old["version"])))
            retired.append((standard_id, int(old["version"])))
            changed = (CatalogChangedField.STANDARDS,)
        case SetCameras(cameras=declared):
            cameras = _check_cameras(declared)
            base["cameras"] = [c.catalog_camera() for c in cameras]
            changed = (CatalogChangedField.CAMERAS,)
        case SetMinimumCoverage(required_count=count, required_camera_ids=required):
            base["minimum_coverage"] = _coverage(count, required)
            changed = (CatalogChangedField.MINIMUM_COVERAGE,)
        case SetSignals(signals=signals):
            base["signals"] = [_mapping(s) for s in signals]
            changed = (CatalogChangedField.SIGNALS,)
        case SetThresholds(thresholds=thresholds):
            base["thresholds"] = _mapping(thresholds)
            changed = (CatalogChangedField.THRESHOLDS,)
        case SetWindows(clip_window=clip_window, episode=episode):
            fields: list[CatalogChangedField] = []
            if clip_window is not None:
                base["clip_window"] = _mapping(clip_window)
                fields.append(CatalogChangedField.CLIP_WINDOW)
            if episode is not None:
                base["episode"] = _mapping(episode)
                fields.append(CatalogChangedField.EPISODE)
            changed = tuple(fields)
        case SetSingleOccupancy(single_occupancy=flag, aggregation_window_minutes=minutes):
            if type(flag) is not bool:
                raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
            single_occupancy = flag
            window = minutes
            declared_occupancy = True
            changed = (CatalogChangedField.SINGLE_OCCUPANCY,)
        case _:
            raise TypeError("cambio de catálogo desconocido")

    window = _check_window(window)
    if len(standards) > MAX_STANDARDS:
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID)
    catalog = _compose(base, standards, zone=zone, version=version, issued_at=issued_at)
    return PublicationPlan(
        catalog_version=version,
        catalog=catalog,
        changed_fields=checked_changed_fields(changed),
        born=tuple(born),
        closed=tuple(closed),
        retired=tuple(retired),
        cameras=cameras,
        single_occupancy=single_occupancy,
        aggregation_window_minutes=window,
        single_occupancy_declared=declared_occupancy,
    )


def _compose(
    base: Mapping[str, Any],
    standards: Sequence[Mapping[str, Any]],
    *,
    zone: ZoneRef,
    version: int,
    issued_at: datetime,
) -> dict[str, Any]:
    """El ``ZoneCatalog`` canónico, validado por el lector estricto del contrato."""
    try:
        document: dict[str, Any] = {
            "zone_id": str(zone.zone_id),
            "zone_code": zone.zone_code,
            "plant_id": str(zone.plant_id),
            "organization_id": str(zone.organization_id),
            "version": version,
            "issued_at": contract_timestamp(issued_at),
            "cameras": list(base["cameras"]),
            "minimum_coverage": dict(base["minimum_coverage"]),
            "signals": list(base["signals"]),
            "standards": [dict(s) for s in standards],
            "thresholds": dict(base["thresholds"]),
            "clip_window": dict(base["clip_window"]),
            "episode": dict(base["episode"]),
            "commissioning_watermark": True,
        }
        coverage = MinimumCoverage(
            required_count=document["minimum_coverage"]["required_count"],
            required_camera_ids=tuple(
                uuid.UUID(c) for c in document["minimum_coverage"]["required_camera_ids"]
            ),
            camera_ids=tuple(uuid.UUID(c["camera_id"]) for c in document["cameras"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID) from None
    if not document["cameras"]:
        raise CatalogRuleViolated(CatalogViolation.ZONE_WITHOUT_CAMERAS)
    if unsatisfiable_reason(coverage) is not None:
        raise CatalogRuleViolated(CatalogViolation.UNSATISFIABLE_COVERAGE)
    try:
        model = ZoneCatalog.model_validate_json(
            json.dumps(document, ensure_ascii=False, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError, RecursionError, OverflowError):
        raise CatalogRuleViolated(CatalogViolation.REQUEST_INVALID) from None
    return model.to_json_value()
