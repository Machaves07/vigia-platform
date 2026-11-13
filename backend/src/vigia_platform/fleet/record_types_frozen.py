"""Versión 1 congelada de ``ingest_rejected`` (BR-NUC-52; VIG-457).

``ingest_rejected`` nombra el ``record_kind`` del contrato. Hasta vigia-platform ``f64df12`` el
contrato era v1.1.0, con cinco tipos de presentación, y la versión 1 del tipo se persistió con esa
lista cerrada. v1.5.0 (A-112, vigia-platform#317) añadió ``episode_photo``: el esquema creció sin
subir ``schema_version`` y una base anterior no arrancaba (``RegistryStartupError``).

Este módulo es una **instantánea en el código** del modelo de contenido de la versión 1, con la
lista de v1.1.0 copiada de ``models/enumerations.py`` de ``vigia-contracts`` v1.1.0 y los mismos
nombres de ``$defs``. No importa el contrato vigente: si ``RecordKind`` vuelve a crecer, la
versión 1 no cambia con él (lo comprueba ``tests/integration/test_record_type_upgrade_f64df12.py``
contra el esquema que persistió ``f64df12``).

Sirve para **presentar** los registros históricos v1; los escritores escriben siempre la última
versión (``IngestRejected``, versión 2).
"""

from __future__ import annotations

import enum
from typing import Annotated, Literal

from pydantic import Field, StrictStr

from vigia_platform.ledger.registry import ContentModel

__all__ = ["IngestRejectedV1"]

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


class RecordKind(enum.StrEnum):
    """Tipo de registro que el nodo presenta (los cinco de v1.1.0)."""

    FINDING = "finding"
    DETECTION_FOR_REVIEW = "detection_for_review"
    OBSERVABILITY_EVENT = "observability_event"
    HEARTBEAT = "heartbeat"
    UPDATE_RESULT = "update_result"


class IngestRejectedV1(ContentModel):
    """``ingest_rejected`` en su versión 1: identificadores y código, nunca el contenido."""

    node_id: UUID
    zone_id: UUID | None = None
    record_kind: RecordKind
    code: Literal["zone_gate_not_approved", "node_zone_mismatch"]
    correlation_id: UUID
    received_at: Timestamp
