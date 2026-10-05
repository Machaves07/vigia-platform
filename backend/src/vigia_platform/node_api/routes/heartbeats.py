"""``POST heartbeats`` (TASK-223; LC-GOB-14; cuerpo ≤ 64 KB y 4 por minuto por nodo, TASK-206).

Fuera de la transacción, en la verificación previa común: versión, certificado y alcance, tamaño y
esquema con el modelo estricto ``Heartbeat`` de U-01. Antes del esquema (``before_schema``), la
parte del alcance que depende del cuerpo: organización, planta y nodo distintos de los del
certificado responden ``node_zone_mismatch`` (el paso 2 gana al 4, PR-GOB-02). Las zonas que el
nodo informa y ya no tiene asignadas **no** rechazan (BR-GOB-70): se ignoran en la proyección.

La operación delega en ``fleet.heartbeat`` y devuelve la respuesta ya serializada, con los sobres
de compuertas tal como se guardaron; un ``heartbeat_id`` repetido es ``accepted_duplicate`` en la
métrica de la ruta, con la misma respuesta compuesta del estado vigente (BR-GOB-71).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.models.heartbeat import Heartbeat

from vigia_platform.fleet.application.heartbeat import HeartbeatService
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest

__all__ = ["heartbeat_operation"]

_SCOPE_FIELDS = ("organization_id", "plant_id", "node_id")


def _body_scope(node: NodeScope | None, document: object) -> None:
    """Organización, planta y nodo del cuerpo (aún sin validar) contra el certificado.

    Un valor que no es un UUID lo rechaza después el esquema; uno legible y distinto del
    certificado es ``node_zone_mismatch`` antes de mirar el esquema.
    """
    if node is None or not isinstance(document, Mapping):
        return
    expected = {
        "organization_id": node.organization_id,
        "plant_id": node.plant_id,
        "node_id": node.node_id,
    }
    for name in _SCOPE_FIELDS:
        value = document.get(name)
        if not isinstance(value, str):
            continue
        try:
            presented = uuid.UUID(value)
        except ValueError:
            continue
        if presented != expected[name]:
            raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field=name)


def heartbeat_operation(service: HeartbeatService) -> NodeOperation:
    """La ``NodeOperation`` de ``POST heartbeats`` sobre ``service``."""

    async def handle(request: NodeRequest) -> NodeReply:
        node, document = request.node, request.document
        if node is None or not isinstance(document, Heartbeat):
            raise RuntimeError("latido sin alcance de nodo o sin cuerpo validado")
        reply = await service.accept(node, document, request.compatibility_result)
        return NodeReply(content=reply.content, duplicate=reply.duplicate)

    return NodeOperation(handle=handle, before_schema=_body_scope)
