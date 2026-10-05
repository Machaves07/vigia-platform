"""La ``NodeOperation`` común de las tres rutas de la ingesta (TASK-221, LC-GOB-12).

- ``check_body``: la parte del paso 2 que depende del cuerpo (``IngestService.check_body_scope``:
  organización, planta, nodo y zona asignada en el instante del hecho), antes del esquema;
- ``handle``: ``IngestService.accept`` con la presentación validada, la ``Idempotency-Key``, la
  versión de la cabecera y el instante de recepción; responde el ``Receipt`` (``accepted`` o
  ``accepted_duplicate``, BR-CTR-27);
- ``on_rejection`` (y todo fallo de ``handle``): el fallo se traduce con ``rejections.translate``,
  la traducción **única** de ``node_api``, y si es permanente ``IngestService.record_rejection``
  deja su auditoría y, con ``zone_gate_not_approved`` o ``node_zone_mismatch``, el registro
  ``ingest_rejected`` (BR-GOB-96). Un fallo al dejar ese rastro sale en su lugar (transitorio: el
  nodo reintenta).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from vigia_platform.fleet.application.ingest import IngestRejected, IngestService
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.node_api.rejections import TRANSIENT_CODES, translate
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest

__all__ = ["ingest_operation"]


def _zone_of(request: NodeRequest | None, error: BaseException) -> uuid.UUID | None:
    if isinstance(error, IngestRejected) and error.zone_id is not None:
        return error.zone_id
    document = None if request is None else request.document
    zone = getattr(document, "zone_id", None)
    if isinstance(zone, str):
        try:
            return uuid.UUID(zone)
        except ValueError:
            return None
    return None


def ingest_operation(kind: IngestKind, service: IngestService) -> NodeOperation:
    """La ``NodeOperation`` de la ruta de ``kind`` sobre ``service``."""
    kind = IngestKind(kind)

    async def record(
        node: NodeScope,
        error: BaseException,
        received_at: datetime,
        request: NodeRequest | None = None,
    ) -> None:
        rejection = translate(error)
        if rejection is None or rejection.code in TRANSIENT_CODES:
            return
        await service.record_rejection(
            kind,
            node,
            rejection.code,
            received_at=received_at,
            zone_id=_zone_of(request, error),
        )

    async def check_body(node: NodeScope, document: object, received_at: datetime) -> None:
        await service.check_body_scope(kind, node, document, received_at)

    async def on_rejection(node: NodeScope, error: BaseException, received_at: datetime) -> None:
        await record(node, error, received_at)

    async def handle(request: NodeRequest) -> NodeReply:
        node, document = request.node, request.document
        if node is None or document is None:
            raise RuntimeError("presentación de la ingesta sin alcance de nodo o sin cuerpo")
        try:
            reply = await service.accept(
                kind,
                node,
                document,
                received_at=request.received_at,
                idempotency_key=request.idempotency_key,
                contract_version=request.contract_version,
            )
        except Exception as error:
            await record(node, error, request.received_at, request)
            raise
        return NodeReply(model=reply.receipt, duplicate=reply.duplicate)

    return NodeOperation(handle=handle, check_body=check_body, on_rejection=on_rejection)
