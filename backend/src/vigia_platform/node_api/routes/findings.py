"""``POST findings`` (TASK-221; LC-GOB-12; cuerpo ≤ 256 KB y 240 por minuto por nodo, TASK-206).

El hallazgo pasa los nueve pasos de BR-GOB-84 y se escribe como ``finding_received`` con su evento
homónimo en la misma transacción; ningún hallazgo entra en una zona sin compuerta de uso aprobada
en el instante del hecho (BR-GOB-92, G-4). La operación es la común de ``routes._ingest``.
"""

from __future__ import annotations

from vigia_platform.fleet.application.ingest import IngestService
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.node_api.router import NodeOperation
from vigia_platform.node_api.routes._ingest import ingest_operation

__all__ = ["finding_operation"]


def finding_operation(service: IngestService) -> NodeOperation:
    """La ``NodeOperation`` de ``POST findings`` sobre ``service``."""
    return ingest_operation(IngestKind.FINDING, service)
