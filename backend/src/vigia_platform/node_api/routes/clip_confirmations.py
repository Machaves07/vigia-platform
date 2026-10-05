"""``POST clip-uploads/{clip_id}/confirmation``: recibo del clip de verificación (TASK-222; nº 32).

El manejador de negocio que la raíz registra en la ``NodeApiGate`` de TASK-206: la verificación
previa ya admitió la petición (30 por minuto por nodo, versión, certificado, sin cuerpo y
``clip_id`` UUID v7). ``ClipConfirmationService.confirm`` y la respuesta
``VerificationClipReceipt {clip_id, zone_id, node_id, received_at, sha256}`` con el modelo estricto
de U-01; repetir la confirmación devuelve el mismo recibo.

Traducción de los fallos de negocio: ``ClipNotOfNode`` → ``node_zone_mismatch`` (inexistente o de
otro nodo, igual); ``EvidenceClipNotConfirmable`` → ``schema_invalid`` (422) con ``field = purpose``
(A-37); ``ClipCheckFailed`` → su ``clip_missing``, ``clip_hash_mismatch``, ``clip_too_large`` o
``clip_not_anonymized`` (permanentes); ``StorageUnavailable`` → ``storage_unavailable``.
"""

from __future__ import annotations

import json
import uuid

from vigia_contracts.models import api
from vigia_contracts.models.api import ContractValidationError
from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.models.verification_clip_receipt import VerificationClipReceipt

from vigia_platform.fleet.application.clip_confirmation import (
    ClipCheckFailed,
    ClipConfirmationService,
    ClipNotOfNode,
    EvidenceClipNotConfirmable,
)
from vigia_platform.fleet.domain.verification_clip import VerificationClip
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["clip_confirmation_operation", "receipt_document"]


def receipt_document(clip: VerificationClip) -> VerificationClipReceipt:
    """El ``VerificationClipReceipt`` del contrato: los campos del ``VerificationClip``."""
    document = {
        "clip_id": str(clip.clip_id),
        "zone_id": str(clip.zone_id),
        "node_id": str(clip.node_id),
        "received_at": format_timestamp(clip.received_at),
        "sha256": clip.sha256,
    }
    try:
        return api.parse_verification_clip_receipt(json.dumps(document).encode())
    except ContractValidationError:
        raise RuntimeError("el recibo no cumple el contrato") from None


def clip_confirmation_operation(service: ClipConfirmationService) -> NodeOperation:
    """El manejador de ``POST clip-uploads/{clip_id}/confirmation`` sobre ``service``."""

    async def handle(request: NodeRequest) -> NodeReply:
        node = request.node
        raw = request.path.get("clip_id")
        if node is None or raw is None:
            raise RuntimeError("confirmación sin nodo o sin clip_id verificado")
        try:
            clip = await service.confirm(node, uuid.UUID(raw))
        except ClipNotOfNode:
            raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH) from None
        except EvidenceClipNotConfirmable:
            raise NodeRejection(RejectionCode.SCHEMA_INVALID, field="purpose") from None
        except ClipCheckFailed as error:
            raise NodeRejection(RejectionCode(error.failure.value)) from None
        return NodeReply(model=receipt_document(clip))

    return NodeOperation(handle=handle)
