"""``POST detection-reviews`` (TASK-221; LC-GOB-12; cuerpo ≤ 256 KB, límite compartido de TASK-206).

La detección para revisión llega por su **operación propia** con las mismas verificaciones que el
hallazgo (D-3, BR-GOB-95): se escribe como ``detection_for_review_received`` y publica su evento en
la misma transacción. Nunca se descarta, nunca se convierte en hallazgo y nunca espera a que exista
un consumidor (la bandeja la entrega cuando U-04 se registre).
"""

from __future__ import annotations

from vigia_platform.fleet.application.ingest import IngestService
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.node_api.router import NodeOperation
from vigia_platform.node_api.routes._ingest import ingest_operation

__all__ = ["detection_review_operation"]


def detection_review_operation(service: IngestService) -> NodeOperation:
    """La ``NodeOperation`` de ``POST detection-reviews`` sobre ``service``."""
    return ingest_operation(IngestKind.DETECTION_FOR_REVIEW, service)
