"""``POST update-results`` (TASK-226; LC-GOB-17; cuerpo ≤ 64 KB y 10 por hora por nodo, TASK-206).

Fuera de la transacción, en la verificación previa común: versión, certificado, tamaño y esquema
con el modelo estricto ``UpdateResult`` de U-01. Antes del esquema (``before_schema``), la parte
del alcance que depende del cuerpo: organización, planta y nodo distintos de los del certificado
responden ``node_zone_mismatch`` (el paso 2 gana al 4, PR-GOB-02).

La operación delega en ``fleet.versions`` (``UpdateResultService``) y responde el ``Receipt``
(``accepted`` o ``accepted_duplicate`` con el recibo original, BR-CTR-27). Los rechazos los
traduce ``rejections.translate``, la traducción única de ``node_api``.
"""

from __future__ import annotations

from vigia_contracts.models.update_result import UpdateResult

from vigia_platform.fleet.application.update_results import UpdateResultService
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest

__all__ = ["update_result_operation"]


def update_result_operation(service: UpdateResultService) -> NodeOperation:
    """La ``NodeOperation`` de ``POST update-results`` sobre ``service``."""

    async def handle(request: NodeRequest) -> NodeReply:
        node, document = request.node, request.document
        if node is None or not isinstance(document, UpdateResult):
            raise RuntimeError("resultado de actualización sin alcance de nodo o sin cuerpo")
        reply = await service.accept(
            node,
            document,
            received_at=request.received_at,
            idempotency_key=request.idempotency_key,
            contract_version=request.contract_version,
        )
        return NodeReply(model=reply.receipt, duplicate=reply.duplicate)

    return NodeOperation(handle=handle, before_schema=service.check_body_scope)
