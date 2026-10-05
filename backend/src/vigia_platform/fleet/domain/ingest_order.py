"""Orden fijo de la ingesta y sus verificaciones de contenido (BR-GOB-83 a 96; BL §2.4; LC-GOB-12).

**Orden** (BR-GOB-84 = BR-CTR-32; el primer fallo decide el código, el de menor índice):
(1) versión, (2) certificado y alcance, (3) tamaño, (4) esquema, (5) idempotencia, (6) clips,
(7) catálogo y estándar, (8) compuerta de uso, (9) escritura. ``IngestStep`` los numera y
``STEP_CODES`` dice qué códigos del contrato puede dar cada uno.

**Por tipo** (``IngestKind``): hallazgo, detección para revisión (su operación propia, D-3) y
evento de observabilidad. Los tres pasan los pasos 1 a 6 y la antigüedad; catálogo y compuerta,
solo hallazgos y detecciones (BR-GOB-92: los eventos describen al observador).

**Verificaciones puras** (las consultas solo traen las filas candidatas; aquí se decide):

- ``assigned_at``: la zona estaba asignada al nodo en el instante (``[assigned_at,
  unassigned_at)``, BR-GOB-88);
- ``catalog_violation``: el ``{standard_id, version}`` citado existe en alguna versión del
  catálogo vigente en la ventana (BR-CTR-08) y, con ella, las señales son todas las declaradas,
  cada una una vez y con su rol (BR-CTR-04) y los umbrales son coherentes (BR-CTR-10): publicación
  en un hallazgo por umbral, y los dos umbrales iguales a los del catálogo en una detección. Si
  ninguna versión que lo cita los cumple, el campo de la **más nueva** (como la plataforma
  simulada de U-01); si ninguna lo cita, ``standard``;
- ``usage_approved_during``: la compuerta de uso estuvo ``approved`` en algún instante de la
  ventana (``GateStateHistory`` ``[effective_from, effective_until)``);
- ``same_submission``: el registro aceptado, **sin** su ``receipt``, es exactamente la presentación
  (bytes canónicos RFC 8785): el paso 5 con el recibo dentro del registro (decisión declarada en
  TASK-221: el contenido de los tres tipos es el modelo del contrato con ``receipt``).

Dominio puro: sin base ni reloj.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from vigia_contracts.canonical import canonicalize
from vigia_contracts.models.enumerations import RecordKind, RejectionCode

from vigia_platform.fleet.domain.clock_tolerance import Window

__all__ = [
    "RECEIPT_FIELD",
    "STEP_CODES",
    "AssignmentSpan",
    "CatalogUnreadable",
    "CatalogVersionView",
    "GateSpan",
    "IngestKind",
    "IngestStep",
    "assigned_at",
    "catalog_violation",
    "cited_clip_ids",
    "same_submission",
    "usage_approved_during",
]

RECEIPT_FIELD: Final = "receipt"


class IngestStep(enum.IntEnum):
    """Los nueve pasos de BR-GOB-84, en su orden."""

    VERSION = 1
    CERTIFICATE = 2
    SIZE = 3
    SCHEMA = 4
    IDEMPOTENCY = 5
    CLIPS = 6
    CATALOG = 7
    GATE = 8
    WRITE = 9


STEP_CODES: Final[Mapping[IngestStep, frozenset[RejectionCode]]] = MappingProxyType(
    {
        IngestStep.VERSION: frozenset(
            {
                RejectionCode.CONTRACT_VERSION_UNSUPPORTED,
                RejectionCode.CONTRACT_VERSION_RETIRED,
                RejectionCode.SCHEMA_INVALID,
            }
        ),
        IngestStep.CERTIFICATE: frozenset(
            {
                RejectionCode.NODE_NOT_ENROLLED,
                RejectionCode.NODE_REVOKED,
                RejectionCode.NODE_ZONE_MISMATCH,
            }
        ),
        IngestStep.SIZE: frozenset({RejectionCode.PAYLOAD_TOO_LARGE}),
        IngestStep.SCHEMA: frozenset({RejectionCode.SCHEMA_INVALID}),
        IngestStep.IDEMPOTENCY: frozenset({RejectionCode.IDEMPOTENCY_CONFLICT}),
        IngestStep.CLIPS: frozenset(
            {
                RejectionCode.CLIP_MISSING,
                RejectionCode.CLIP_HASH_MISMATCH,
                RejectionCode.CLIP_NOT_ANONYMIZED,
            }
        ),
        IngestStep.CATALOG: frozenset(
            {RejectionCode.SCHEMA_INVALID, RejectionCode.TIMESTAMP_OUT_OF_WINDOW}
        ),
        IngestStep.GATE: frozenset({RejectionCode.ZONE_GATE_NOT_APPROVED}),
        IngestStep.WRITE: frozenset(
            {RejectionCode.TEMPORARILY_UNAVAILABLE, RejectionCode.STORAGE_UNAVAILABLE}
        ),
    }
)
"""Los códigos del contrato que puede dar cada paso (la antigüedad va con el catálogo, como en la
plataforma simulada de U-01; ``schema_invalid`` del paso 1 es una cabecera mal formada)."""


class IngestKind(enum.Enum):
    """Los tres tipos de la ingesta: ``(record_kind, record_type, id_field, evento, catálogo y
    compuerta)``."""

    FINDING = (RecordKind.FINDING, "finding_received", "finding_id", True)
    DETECTION_FOR_REVIEW = (
        RecordKind.DETECTION_FOR_REVIEW,
        "detection_for_review_received",
        "detection_id",
        True,
    )
    OBSERVABILITY_EVENT = (
        RecordKind.OBSERVABILITY_EVENT,
        "observability_event_received",
        "event_id",
        False,
    )

    @property
    def record_kind(self) -> RecordKind:
        kind: RecordKind = self.value[0]
        return kind

    @property
    def record_type(self) -> str:
        """El tipo de registro del expediente; su evento de la bandeja se llama igual."""
        return str(self.value[1])

    @property
    def event_name(self) -> str:
        return self.record_type

    @property
    def id_field(self) -> str:
        """El identificador del nodo: clave de idempotencia y ``Idempotency-Key`` (BR-CTR-26)."""
        return str(self.value[2])

    @property
    def gated(self) -> bool:
        """¿Pasa catálogo y compuerta? (BR-GOB-92: los eventos se aceptan en todo modo)."""
        return bool(self.value[3])


# --- Paso 2: zona asignada en el instante --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentSpan:
    """Una asignación de ``identity.zone_node_assignment``: ``[assigned_at, unassigned_at)``."""

    zone_id: uuid.UUID
    assigned_at: datetime
    unassigned_at: datetime | None


def assigned_at(spans: Iterable[AssignmentSpan], zone_id: uuid.UUID, at: datetime) -> bool:
    """¿Estaba ``zone_id`` asignada al nodo en ``at``? (BR-GOB-88)."""
    return any(
        span.zone_id == zone_id
        and span.assigned_at <= at
        and (span.unassigned_at is None or at < span.unassigned_at)
        for span in spans
    )


# --- Paso 7: catálogo y estándar -----------------------------------------------------------------


class CatalogUnreadable(Exception):
    """Una versión guardada del catálogo no tiene la forma de ``ZoneCatalog``: error interno."""


@dataclass(frozen=True, slots=True)
class CatalogVersionView:
    """Lo que el paso 7 necesita de una versión emitida del catálogo de la zona."""

    version: int
    effective_from: datetime
    effective_until: datetime | None
    standards: frozenset[tuple[uuid.UUID, int]]
    signals: Mapping[uuid.UUID, str]
    """``signal_id`` → ``role`` de las señales declaradas."""
    review: float
    publication: float

    @classmethod
    def from_payload(
        cls,
        version: int,
        effective_from: datetime,
        effective_until: datetime | None,
        payload: Mapping[str, Any],
    ) -> CatalogVersionView:
        """La vista de un ``ZoneCatalog`` guardado (``CatalogUnreadable`` si no lo es)."""
        try:
            standards = frozenset(
                (uuid.UUID(str(item["standard_id"])), int(item["version"]))
                for item in payload["standards"]
            )
            signals = MappingProxyType(
                {
                    uuid.UUID(str(item["signal_id"])): str(item["role"])
                    for item in payload["signals"]
                }
            )
            thresholds = payload["thresholds"]
            review, publication = float(thresholds["review"]), float(thresholds["publication"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CatalogUnreadable("versión del catálogo ilegible") from None
        return cls(
            version, effective_from, effective_until, standards, signals, review, publication
        )

    def in_force_during(self, window: Window) -> bool:
        return window.overlaps(self.effective_from, self.effective_until)

    def cites(self, standard_id: uuid.UUID, version: int) -> bool:
        return (standard_id, version) in self.standards


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _mismatch(
    catalog: CatalogVersionView, document: Mapping[str, Any], kind: IngestKind
) -> str | None:
    """El primer campo del registro que no concuerda con ``catalog`` (BR-CTR-04 y 10), o nada."""
    seen: set[uuid.UUID] = set()
    for index, reading in enumerate(document["signals"]):
        signal_id = _uuid(reading["signal_id"])
        if signal_id is None or signal_id not in catalog.signals or signal_id in seen:
            return "signals"
        if reading["role"] != catalog.signals[signal_id]:
            return f"signals[{index}].role"
        seen.add(signal_id)
    if seen != set(catalog.signals):
        return "signals"
    if kind is IngestKind.FINDING:
        trigger = document["automatic_classification"]["trigger"]
        if trigger == "publication_threshold" and document["max_confidence"] < catalog.publication:
            return "max_confidence"
    elif kind is IngestKind.DETECTION_FOR_REVIEW:
        if document["review_threshold"] != catalog.review:
            return "review_threshold"
        if document["publication_threshold"] != catalog.publication:
            return "publication_threshold"
    return None


def catalog_violation(
    versions: Sequence[CatalogVersionView],
    document: Mapping[str, Any],
    kind: IngestKind,
    window: Window,
) -> str | None:
    """``field`` del ``schema_invalid`` del paso 7, o ``None`` si alguna versión vigente en
    ``window`` cita el estándar y concuerda con el registro."""
    if not kind.gated:
        return None
    standard = document["standard"]
    standard_id, version = _uuid(standard["standard_id"]), standard["version"]
    in_force = sorted(
        (item for item in versions if item.in_force_during(window)),
        key=lambda item: item.version,
        reverse=True,
    )
    citing = [
        item
        for item in in_force
        if standard_id is not None and type(version) is int and item.cites(standard_id, version)
    ]
    if not citing:
        return "standard"
    fields = [_mismatch(item, document, kind) for item in citing]
    if None in fields:
        return None
    return fields[0]


# --- Paso 8: compuerta de uso --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateSpan:
    """Un intervalo de la compuerta de uso: ``[effective_from, effective_until)``."""

    approved: bool
    effective_from: datetime
    effective_until: datetime | None


def usage_approved_during(spans: Iterable[GateSpan], window: Window) -> bool:
    """¿Estuvo el uso ``approved`` en algún instante de ``window``? (BR-GOB-92)."""
    return any(
        span.approved and window.overlaps(span.effective_from, span.effective_until)
        for span in spans
    )


# --- Paso 5: idempotencia con el recibo dentro ---------------------------------------------------


def same_submission(stored: Mapping[str, Any], submission: Mapping[str, Any]) -> bool:
    """¿Es el registro aceptado ``stored``, sin su recibo, la presentación ``submission``?"""
    without = {key: value for key, value in stored.items() if key != RECEIPT_FIELD}
    return canonicalize(without) == canonicalize(dict(submission))


def cited_clip_ids(document: Mapping[str, Any]) -> tuple[uuid.UUID, ...]:
    """Los ``clip_id`` que cita un registro (clips de las cámaras y evidencia del evento)."""
    found: list[uuid.UUID] = []
    for camera in document.get("cameras", ()):
        found += [uuid.UUID(str(clip["clip_id"])) for clip in camera["clips"]]
    found += [uuid.UUID(str(clip["clip_id"])) for clip in document.get("evidence", ())]
    return tuple(sorted(set(found), key=str))
