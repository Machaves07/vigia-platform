"""``POST clip-uploads``: concesión de subida de un clip (TASK-222; LC-GOB-13; NFR-GOB-33).

El manejador de negocio (``NodeOperation``) que la raíz registra en la ``NodeApiGate`` de TASK-206:
la verificación previa común ya admitió la petición (tasa de 480 por minuto con ráfaga de 120 por
nodo, versión, certificado, cuerpo ≤ 16 KB y lector estricto ``ClipUploadRequest`` de U-01).

- ``before_schema`` (entre los pasos 3 y 4 de BR-GOB-84): la zona del cuerpo, si es un UUID
  canónico, tiene que estar entre las asignadas al nodo: si no, ``node_zone_mismatch`` **antes**
  que cualquier ``schema_invalid`` del resto del cuerpo (el alcance gana al esquema);
- ``handle``: ``ClipGrantService.issue`` y la respuesta ``ClipUploadGrant`` validada con el modelo
  estricto de U-01.

Traducción de los fallos de negocio (el resto lo traduce ``node_api.rejections``):
``ZoneNotAssigned`` → ``node_zone_mismatch``; ``ClipGrantConflict`` → ``schema_invalid`` (422) con
``field = clip_id`` (la operación no declara ``409``: A-37); ``ClipGrantRequestInvalid`` →
``schema_invalid`` con su ``field``; ``StorageUnavailable`` → ``storage_unavailable`` (503,
transitorio). La URL prefirmada solo viaja en el cuerpo de la respuesta: nunca en registros,
métricas, tramos ni eventos (NFR-GOB-25).
"""

from __future__ import annotations

import json
import uuid

from vigia_contracts.models import api
from vigia_contracts.models.api import ContractValidationError
from vigia_contracts.models.clip_upload_grant import ClipUploadGrant as ClipUploadGrantDocument
from vigia_contracts.models.clip_upload_request import ClipUploadRequest
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.fleet.application.clip_grants import (
    ClipGrantConflict,
    ClipGrantService,
    IssuedClipGrant,
    ZoneNotAssigned,
)
from vigia_platform.fleet.domain.clip_upload_grant import ClipGrantRequestInvalid
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["clip_upload_operation", "grant_document", "zone_before_schema"]


def zone_before_schema(node: NodeScope | None, body: object) -> None:
    """``node_zone_mismatch`` si el cuerpo nombra una zona que no es del nodo (paso 2 > 4)."""
    if node is None or not isinstance(body, dict):
        return
    raw = body.get("zone_id")
    if not isinstance(raw, str):
        return
    try:
        zone_id = uuid.UUID(raw)
    except ValueError:
        return
    if str(zone_id) != raw:
        return  # No canónico: lo rechaza el lector estricto (schema_invalid).
    if not node.covers_zone(zone_id):
        raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field="zone_id")


def grant_document(issued: IssuedClipGrant) -> ClipUploadGrantDocument:
    """La respuesta ``ClipUploadGrant`` del contrato, con el modelo estricto de U-01."""
    grant = issued.grant
    document = {
        "clip_id": str(grant.clip_id),
        "purpose": grant.purpose.value,
        "upload_url": issued.upload.url,
        "method": "PUT",
        "expires_at": format_timestamp(grant.expires_at),
        "max_size_bytes": grant.max_size_bytes,
        "required_headers": dict(issued.upload.headers),
        "storage_key": grant.storage_key,
    }
    try:
        return api.parse_clip_upload_grant(json.dumps(document).encode())
    except ContractValidationError:
        # Defecto de la plataforma (p. ej. un almacén sin https): nunca sale una concesión que
        # el contrato no admite. Sale transitorio, sin el detalle.
        raise RuntimeError("la concesión no cumple el contrato") from None


def clip_upload_operation(service: ClipGrantService) -> NodeOperation:
    """El manejador de ``POST clip-uploads`` sobre ``service``."""

    async def handle(request: NodeRequest) -> NodeReply:
        node = request.node
        document = request.document
        if node is None or not isinstance(document, ClipUploadRequest):
            raise RuntimeError("concesión de clip sin nodo o sin cuerpo verificado")
        try:
            issued = await service.issue(node, document)
        except ZoneNotAssigned:
            raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field="zone_id") from None
        except ClipGrantConflict:
            raise NodeRejection(RejectionCode.SCHEMA_INVALID, field="clip_id") from None
        except ClipGrantRequestInvalid as error:
            raise NodeRejection(RejectionCode.SCHEMA_INVALID, field=error.field) from None
        return NodeReply(model=grant_document(issued))

    return NodeOperation(handle=handle, before_schema=zone_before_schema)
