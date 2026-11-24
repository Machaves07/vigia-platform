"""Versión 1 congelada de ``finding_received`` y ``detection_for_review_received`` (TASK-593).

El contenido de los dos tipos es el modelo del contrato con ``receipt`` (``Finding`` y
``DetectionForReview``). Hasta v1.5.0 su versión 1 se persistió en ``ledger.record_type`` con ese
esquema. v1.6.0 (A-128 y A-129) lo amplía por adición: ``process_context`` opcional y el clip de
cada cámara deja de ser obligatorio (``clips`` de 0 a 2). Sin subir ``schema_version``, una base
anterior no arrancaría (``RegistryStartupError``, como en VIG-457).

Este módulo es una **instantánea en el código** de los dos modelos de v1.5.0: los mismos campos,
``$defs`` y restricciones, sin ``process_context`` y con ``clips`` de 1 a 2. Las piezas que v1.6.0
no cambió (clip de video, clasificación automática, lecturas de señal, tramo, reloj, recibo…) se
toman del contrato vigente; ``tests/unit/test_record_types_frozen_v1_5.py`` compara el esquema con
el que persistía la plataforma con v1.5.0 (``tests/fixtures/record_types/``), así que un cambio
futuro de esas piezas no pasa inadvertido.

Sirve para **presentar** los registros históricos v1; los escritores escriben siempre la última
versión (el modelo vigente, versión 2). Las reglas entre campos del contrato no se repiten aquí:
esos registros ya se validaron al escribirse y el contenido del expediente nunca cambia (P4).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StrictBool, StrictFloat
from vigia_contracts.models.common import (
    UUID,
    EpisodeSegment,
    NodeTime,
    SemVer,
    StandardReference,
    TechnicalId,
    UUIDv7,
)
from vigia_contracts.models.enumerations import EvidenceTier, PredicateFamily
from vigia_contracts.models.finding import (
    AutomaticClassification,
    Confidence,
    EmittingObservabilityState,
    SegmentDurationMs,
    SignalReadings,
    VideoClip,
)
from vigia_contracts.models.receipt import Receipt

from vigia_platform.ledger.registry import ContentModel

__all__ = ["DetectionForReviewV1", "FindingV1"]


class CameraContribution(ContentModel):
    """Aporte de una cámara (v1.5.0): un clip ``full`` o dos (``start`` y ``end``)."""

    camera_id: UUID
    originating: StrictBool
    clips: Annotated[list[VideoClip], Field(max_length=2, min_length=1)]


type CameraContributions = Annotated[
    list[CameraContribution], Field(max_length=8, min_length=1, title="CameraContributions")
]


class FindingV1(ContentModel):
    """``finding_received`` en su versión 1: el ``Finding`` de v1.5.0."""

    finding_id: UUIDv7
    contract_version: SemVer
    organization_id: UUID
    plant_id: UUID
    zone_id: UUID
    node_id: UUID
    episode: EpisodeSegment
    family: PredicateFamily
    secondary_families: Annotated[
        list[PredicateFamily] | None, Field(max_length=3, min_length=0)
    ] = None
    standard: StandardReference
    tier: EvidenceTier
    node_time: NodeTime
    signals: SignalReadings
    observability_state: EmittingObservabilityState
    cameras: CameraContributions
    max_confidence: Confidence
    episode_duration_ms: SegmentDurationMs
    condition_duration_ms: SegmentDurationMs
    automatic_classification: AutomaticClassification
    model_version: TechnicalId
    software_version: SemVer
    receipt: Receipt


class DetectionForReviewV1(ContentModel):
    """``detection_for_review_received`` en su versión 1: el ``DetectionForReview`` de v1.5.0."""

    detection_id: UUIDv7
    contract_version: SemVer
    organization_id: UUID
    plant_id: UUID
    zone_id: UUID
    node_id: UUID
    episode: EpisodeSegment
    family: PredicateFamily
    standard: StandardReference
    node_time: NodeTime
    signals: SignalReadings
    observability_state: EmittingObservabilityState
    cameras: CameraContributions
    max_confidence: Confidence
    review_threshold: Annotated[StrictFloat, Field(ge=0.0, gt=0.0, le=1.0, lt=1.0)]
    publication_threshold: Annotated[StrictFloat, Field(ge=0.0, gt=0.0, le=1.0)]
    episode_duration_ms: SegmentDurationMs
    condition_duration_ms: SegmentDurationMs
    model_version: TechnicalId
    software_version: SemVer
    receipt: Receipt
