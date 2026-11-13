"""Versión 1 congelada de ``catalog_version_published`` (BR-NUC-52; VIG-457).

``catalog_version_published`` guarda el sobre firmado del catálogo tal como lo define el contrato
(``SignedEnvelope<ZoneCatalog>``). Hasta vigia-platform ``f64df12`` el contrato era v1.1.0 y la
versión 1 del tipo se persistió en ``ledger.record_type`` con ese esquema. Al adoptar v1.4.0 y
v1.5.0 (A-77, A-112; vigia-platform#316 y #317) el catálogo admite ``guard_conditions`` en los
predicados ``coexistence`` y ``startup_transition`` y ``no_data`` en las señales: el esquema creció
sin subir ``schema_version`` y una base anterior no arrancaba (``RegistryStartupError``).

Este módulo es una **instantánea en el código** del modelo de contenido de la versión 1: los
modelos de contrato v1.1.0 que alcanza, copiados de los generados de ``vigia-contracts`` v1.1.0
(``models/common.py``, ``enumerations.py``, ``declared_standard.py`` y ``zone_catalog.py``) con
los mismos nombres de ``$defs`` y las mismas restricciones. No importa los modelos del contrato
vigente, a propósito: si el contrato vuelve a cambiar, la versión 1 no cambia con él y
``tests/integration/test_record_type_upgrade_f64df12.py`` lo comprueba contra el esquema que
persistió ``f64df12`` (``tests/fixtures/record_types/ledger_record_type_f64df12.json``).

Única pieza que viene del contrato: ``StrictTrue`` (la guarda de ``const: true`` de v1.5.0), que no
cambia el esquema (``const: true`` igual que ``Literal[True]``) y solo estrecha lo que se valida;
el registro la admite por identidad (``registry._is_strict_true``).

Sirve para **presentar** los registros históricos v1 (``registry.get(tipo, schema_version=1)``);
los escritores escriben siempre la última versión (``CatalogVersionPublished``, versión 2). Las
reglas entre campos del contrato (unicidad, umbrales) no se repiten aquí: esos registros ya se
validaron al escribirse y el contenido del expediente nunca cambia (P4).
"""

from __future__ import annotations

import enum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictFloat, StrictInt, StrictStr, model_validator
from vigia_contracts.models._base import StrictTrue

from vigia_platform.ledger.registry import ContentModel

__all__ = ["CatalogVersionPublishedV1"]

# --- common.schema.json (v1.1.0) -----------------------------------------------------------------

type Timestamp = Annotated[
    StrictStr,
    Field(
        max_length=24,
        min_length=24,
        pattern="^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\\.[0-9]{3}Z$",
        title="Timestamp",
    ),
]
type UUID = Annotated[
    StrictStr,
    Field(
        max_length=36,
        min_length=36,
        pattern="^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        title="UUID",
    ),
]
type Code = Annotated[
    StrictStr,
    Field(max_length=32, min_length=2, pattern="^[A-Z0-9-]{2,32}$", title="Code"),
]
type TechnicalId = Annotated[
    StrictStr,
    Field(max_length=64, min_length=1, pattern="^[a-z][a-z0-9_.-]{0,63}$", title="TechnicalId"),
]
type Sha256Hex = Annotated[
    StrictStr,
    Field(max_length=64, min_length=64, pattern="^[0-9a-f]{64}$", title="Sha256Hex"),
]
type KeyId = Annotated[
    StrictStr,
    Field(max_length=64, min_length=1, pattern="^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$", title="KeyId"),
]


# --- enumerations.schema.json (v1.1.0) -----------------------------------------------------------


class PredicateFamily(enum.StrEnum):
    """Familia del predicado de un estándar declarado."""

    COEXISTENCE = "coexistence"
    GUARD_BYPASS = "guard_bypass"
    DWELL = "dwell"
    STARTUP_TRANSITION = "startup_transition"


class SignalRole(enum.StrEnum):
    """Rol de una señal declarada en el catálogo de la zona."""

    ENERGY = "energy"
    GUARD = "guard"
    START_COMMAND = "start_command"
    AUXILIARY = "auxiliary"


class SignalLevel(enum.StrEnum):
    """Nivel físico de una señal."""

    HIGH = "high"
    LOW = "low"


class CameraRoleInZone(enum.StrEnum):
    """Rol de una cámara dentro de su zona."""

    PRIMARY = "primary"
    REDUNDANT = "redundant"


# --- declared_standard.schema.json (v1.1.0) ------------------------------------------------------


class DeclaredBy(ContentModel):
    """Firmante responsable de un estándar: usuario de la plataforma, no persona observada."""

    user_id: UUID
    display_name: Annotated[StrictStr, Field(max_length=120, min_length=1)]
    role: TechnicalId


class PresenceCondition(ContentModel):
    """Condición de presencia en la zona. Solo admite ``true`` (P2)."""

    presence: StrictTrue


class EnergyAsserted(ContentModel):
    """Condición ``{signal_role: energy, value: asserted}``."""

    signal_role: Annotated[Literal["energy"], Field(title="condition_signal_role")]
    value: Annotated[Literal["asserted"], Field(title="condition_signal_value")]


class EnergyDeasserted(ContentModel):
    """Condición ``{signal_role: energy, value: deasserted}``."""

    signal_role: Annotated[Literal["energy"], Field(title="condition_signal_role")]
    value: Annotated[Literal["deasserted"], Field(title="condition_signal_value")]


class GuardAsserted(ContentModel):
    """Condición ``{signal_role: guard, value: asserted}``."""

    signal_role: Annotated[Literal["guard"], Field(title="condition_signal_role")]
    value: Annotated[Literal["asserted"], Field(title="condition_signal_value")]


class StartCommandAsserted(ContentModel):
    """Condición ``{signal_role: start_command, value: asserted}``."""

    signal_role: Annotated[Literal["start_command"], Field(title="condition_signal_role")]
    value: Annotated[Literal["asserted"], Field(title="condition_signal_value")]


class CoexistencePredicate(ContentModel):
    """Plantilla ``coexistence`` de v1.1.0: sin ``guard_conditions``."""

    all_of: Annotated[list[PresenceCondition | EnergyAsserted], Field(max_length=2, min_length=2)]
    min_duration_ms: Annotated[StrictInt, Field(ge=0, le=0)]


class GuardBypassPredicate(ContentModel):
    """Plantilla ``guard_bypass`` de v1.1.0."""

    all_of: Annotated[
        list[PresenceCondition | GuardAsserted | EnergyAsserted],
        Field(max_length=3, min_length=2),
    ]
    min_duration_ms: Annotated[StrictInt, Field(ge=0, le=0)]


class DwellPredicate(ContentModel):
    """Plantilla ``dwell`` de v1.1.0."""

    all_of: Annotated[list[PresenceCondition | EnergyAsserted], Field(max_length=2, min_length=2)]
    min_duration_ms: Annotated[StrictInt, Field(ge=1000, le=3600000)]


class StartupTransitionPredicate(ContentModel):
    """Plantilla ``startup_transition`` de v1.1.0: sin ``guard_conditions``."""

    all_of: Annotated[
        list[PresenceCondition | StartCommandAsserted | EnergyDeasserted],
        Field(max_length=3, min_length=2),
    ]
    min_duration_ms: Annotated[StrictInt, Field(ge=0, le=60000)]


type Predicate = Annotated[
    CoexistencePredicate | GuardBypassPredicate | DwellPredicate | StartupTransitionPredicate,
    Field(title="Predicate"),
]


class DeclaredStandard(ContentModel):
    """Estándar de seguridad tal como la planta lo declaró (v1.1.0)."""

    standard_id: UUID
    version: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    family: PredicateFamily
    title_es: Annotated[StrictStr, Field(max_length=120, min_length=1)]
    declared_text: Annotated[StrictStr, Field(max_length=4000, min_length=1)]
    declared_by: DeclaredBy
    effective_from: Timestamp
    tier_policy: Literal["tier_1_when_signal_valid"]
    predicate: Predicate


# --- zone_catalog.schema.json (v1.1.0) -----------------------------------------------------------


class CatalogCamera(ContentModel):
    """Cámara declarada en el catálogo de la zona."""

    camera_id: UUID
    code: Code
    role_in_zone: CameraRoleInZone
    declared_min_fps: Annotated[StrictFloat, Field(ge=1.0, le=60.0)]


class MinimumCoverage(ContentModel):
    """Cobertura mínima de la zona."""

    required_count: Annotated[StrictInt, Field(ge=1, le=8)]
    required_camera_ids: Annotated[list[UUID], Field(max_length=8, min_length=0)]


class SignalSource(ContentModel):
    """Lector y canal de los que el nodo toma la señal."""

    reader: Annotated[
        StrictStr, Field(max_length=32, min_length=1, pattern="^[a-z][a-z0-9_.-]{0,31}$")
    ]
    channel: Annotated[StrictInt, Field(ge=0, le=255)]


class Thresholds(ContentModel):
    """Umbrales de confianza: ``0 < review < publication ≤ 1``."""

    review: Annotated[StrictFloat, Field(ge=0.0, gt=0.0, le=1.0, lt=1.0)]
    publication: Annotated[StrictFloat, Field(ge=0.0, gt=0.0, le=1.0)]


class ClipWindow(ContentModel):
    """Segundos de video antes y después del tramo que cubren los clips."""

    pre_seconds: Annotated[StrictInt, Field(ge=5, le=30)]
    post_seconds: Annotated[StrictInt, Field(ge=5, le=30)]


class EpisodeParameters(ContentModel):
    """Agrupación de observaciones en episodios y tope de duración de cada tramo."""

    grouping_window_ms: Annotated[StrictInt, Field(ge=1000, le=300000)]
    max_segment_ms: Annotated[StrictInt, Field(ge=60000, le=3600000)]


class SignalDeclaration(ContentModel):
    """Señal declarada de la zona (v1.1.0: sin ``no_data``)."""

    signal_id: UUID
    code: Code
    role: SignalRole
    asserted_level: SignalLevel
    source: SignalSource
    description_es: Annotated[StrictStr, Field(max_length=120, min_length=1)]


class ZoneCatalog(ContentModel):
    """Catálogo de una zona (v1.1.0)."""

    zone_id: UUID
    zone_code: Code
    plant_id: UUID
    organization_id: UUID
    version: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    issued_at: Timestamp
    cameras: Annotated[list[CatalogCamera], Field(max_length=8, min_length=1)]
    minimum_coverage: MinimumCoverage
    signals: Annotated[list[SignalDeclaration], Field(max_length=32, min_length=0)]
    standards: Annotated[list[DeclaredStandard], Field(max_length=32, min_length=1)]
    thresholds: Thresholds
    clip_window: ClipWindow
    episode: EpisodeParameters
    commissioning_watermark: StrictTrue


type Payload = ZoneCatalog


class SignedEnvelope(ContentModel):
    """Sobre firmado ``SignedEnvelope<ZoneCatalog>`` (v1.1.0)."""

    payload: Payload
    payload_canonical_sha256: Sha256Hex
    signature: Annotated[
        StrictStr, Field(max_length=88, min_length=88, pattern="^[A-Za-z0-9+/]{86}==$")
    ]
    key_id: KeyId
    signed_at: Timestamp


# --- el tipo de registro, versión 1 --------------------------------------------------------------


class CatalogChangedField(enum.StrEnum):
    """Qué cambió de una versión del catálogo a la siguiente (los ocho valores de la versión 1)."""

    STANDARDS = "standards"
    CAMERAS = "cameras"
    MINIMUM_COVERAGE = "minimum_coverage"
    SIGNALS = "signals"
    THRESHOLDS = "thresholds"
    CLIP_WINDOW = "clip_window"
    EPISODE = "episode"
    SINGLE_OCCUPANCY = "single_occupancy"


_UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class CatalogVersionPublishedV1(ContentModel):
    """``catalog_version_published`` en su versión 1: el sobre del contrato v1.1.0."""

    source_key: Annotated[
        StrictStr,
        Field(min_length=38, max_length=47, pattern=f"^{_UUID_PATTERN}:[1-9][0-9]{{0,9}}$"),
    ]
    zone_id: UUID
    catalog_version: Annotated[StrictInt, Field(ge=1, le=2**31 - 1)]
    changed_fields: Annotated[tuple[CatalogChangedField, ...], Field(min_length=1, max_length=8)]
    reason_es: Annotated[StrictStr, Field(min_length=10, max_length=500)]
    envelope: SignedEnvelope
    """El sobre firmado: la clase ``SignedEnvelope`` (no el alias ``SignedZoneCatalog``), como el
    modelo de ``f64df12``, que la importaba con ese nombre (``$ref: #/$defs/SignedEnvelope``)."""

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.source_key != f"{self.zone_id}:{self.catalog_version}":
            raise ValueError("source_key debe ser <zone_id>:<catalog_version>")
        if len(set(self.changed_fields)) != len(self.changed_fields):
            raise ValueError("changed_fields no admite valores repetidos")
        payload = self.envelope.payload
        if payload.zone_id != self.zone_id or payload.version != self.catalog_version:
            raise ValueError("el sobre firmado es de otra zona o de otra versión del catálogo")
        return self
