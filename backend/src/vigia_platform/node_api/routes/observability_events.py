"""``POST observability-events`` (TASK-221; LC-GOB-12; cuerpo ≤ 64 KB, límite de TASK-206).

El evento describe al observador, no a la zona observada: se acepta en **cualquier** modo de la zona
(BR-GOB-92), sin catálogo ni compuerta, pero con la zona asignada en el instante, la idempotencia,
la evidencia y la antigüedad máxima. Se escribe como ``observability_event_received`` con su evento;
un cierre cuya apertura no está aceptada se acepta y queda marcado como cierre huérfano
(``fleet.observability_orphan_close``, BL §2.4).
"""

from __future__ import annotations

from vigia_platform.fleet.application.ingest import IngestService
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.node_api.router import NodeOperation
from vigia_platform.node_api.routes._ingest import ingest_operation

__all__ = ["observability_event_operation"]


def observability_event_operation(service: IngestService) -> NodeOperation:
    """La ``NodeOperation`` de ``POST observability-events`` sobre ``service``."""
    return ingest_operation(IngestKind.OBSERVABILITY_EVENT, service)
