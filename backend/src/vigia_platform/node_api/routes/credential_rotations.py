"""``POST credential-rotations``: rotación de la credencial del nodo (TASK-219; BR-GOB-64; nota de
NFR-GOB-33).

El manejador de negocio (``NodeOperation``) que la raíz registra en la ``NodeApiGate`` de TASK-206.
La verificación previa común ya admitió la petición con mTLS (2 por hora por nodo, versión,
identidad sin caché con la credencial presentada, cuerpo ≤ 16 KB y lector estricto
``CredentialRotationRequest`` de U-01): ``node_revoked`` y ``node_not_enrolled`` los decide ahí la
consulta de identidad.

``handle``: el ``node_id`` del cuerpo tiene que ser el del certificado (si no, ``schema_invalid``
en ``node_id``); las dos CSR con la validación del alta y el mismo ``node_id`` en el nombre común;
después ``CredentialRotationService.rotate`` y la respuesta ``NodeCredential`` **sin**
``initial_configuration``, validada con el lector estricto de U-01.

Traducción: ``CsrRejected`` → ``schema_invalid`` con su ``field``; ``RotationRefused`` →
``node_revoked`` (credencial presentada ``overlapping`` o ya sustituida, o nodo revocado) o
``node_not_enrolled``; ``CredentialUnavailable`` → ``temporarily_unavailable`` con
``retry_after_seconds``.
"""

from __future__ import annotations

from vigia_contracts.models.credential_rotation_request import CredentialRotationRequest
from vigia_contracts.models.enumerations import RejectionCode

from vigia_platform.fleet.adapters.ca.csr import CLIENT_CSR_FIELD, CsrRejected
from vigia_platform.fleet.application.credential_rotation import (
    CredentialRotationService,
    RotationRefused,
)
from vigia_platform.fleet.application.enrollment import CredentialUnavailable
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest
from vigia_platform.node_api.routes.enrollment import (
    credential_document,
    encoded,
    read_csr_pair,
    rejection_of,
)

__all__ = ["credential_rotation_operation"]


def credential_rotation_operation(service: CredentialRotationService) -> NodeOperation:
    """El manejador de ``POST credential-rotations`` sobre ``service``."""

    async def handle(request: NodeRequest) -> NodeReply:
        node = request.node
        document = request.document
        if node is None or not isinstance(document, CredentialRotationRequest):
            raise RuntimeError("rotación sin nodo o sin cuerpo verificado")
        if document.node_id != str(node.node_id):
            raise NodeRejection(RejectionCode.SCHEMA_INVALID, field="node_id")
        client, server = read_csr_pair(
            document.certificate_signing_request, document.server_certificate_signing_request
        )
        if client.node_id != node.node_id:
            raise NodeRejection(RejectionCode.SCHEMA_INVALID, field=CLIENT_CSR_FIELD)
        try:
            issued = await service.rotate(node, client, server)
        except CsrRejected as error:
            raise NodeRejection(RejectionCode.SCHEMA_INVALID, field=error.field) from None
        except RotationRefused as error:
            raise NodeRejection(RejectionCode(error.reason.value)) from None
        except CredentialUnavailable as error:
            raise rejection_of(error) from None
        return NodeReply(content=encoded(credential_document(issued), enrollment=False))

    return NodeOperation(handle=handle)
