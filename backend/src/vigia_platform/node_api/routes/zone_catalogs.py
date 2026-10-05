"""``GET zones/{zone_id}/catalog`` (TASK-223; 30 por minuto por nodo, TASK-206).

La verificación previa ya exige que ``zone_id`` esté entre las zonas asignadas al nodo en el
instante de la petición (``node_zone_mismatch``, BR-GOB-88); la operación responde el sobre del
catálogo vigente **byte a byte** desde el almacenamiento (``fleet.zone_catalog_for_node``), sin
canonicalizar ni firmar.
"""

from __future__ import annotations

import uuid

from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.fleet.application.zone_catalog_for_node import ZoneCatalogForNode
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest

__all__ = ["zone_catalog_operation"]


def zone_catalog_operation(service: ZoneCatalogForNode) -> NodeOperation:
    """La ``NodeOperation`` de ``GET zones/{zone_id}/catalog`` sobre ``service``."""

    async def handle(request: NodeRequest) -> NodeReply:
        node = request.node
        if node is None:
            raise RuntimeError("catálogo sin alcance de nodo")
        try:
            zone_id = uuid.UUID(request.path.get("zone_id", ""))
        except ValueError:
            raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field="zone_id") from None
        return NodeReply(content=await service.envelope(node, zone_id))

    return NodeOperation(handle=handle)
