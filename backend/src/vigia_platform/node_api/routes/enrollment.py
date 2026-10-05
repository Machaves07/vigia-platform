"""``POST enrollment``: alta del nodo (TASK-219; LC-GOB-11, LC-GOB-19; NFR-GOB-33).

El manejador de negocio (``NodeOperation``) que la raíz registra en la ``NodeApiGate`` de TASK-206.
La verificación previa común ya admitió la petición **sin certificado** (las cabeceras
``X-Amzn-Mtls-*`` no se leen en esta ruta: el alta llega por ``app.<dominio>``): límites por
origen (5 cada 15 min y 20 al día), versión, cuerpo ≤ 16 KB y lector estricto
``NodeEnrollmentRequest`` de U-01. ``handle`` sigue el orden de TASK-219:

1. **CSR** de cliente y de servidor (``fleet.adapters.ca.csr.read_csr``): cualquier otra forma es
   ``schema_invalid`` con ``field`` ``certificate_signing_request`` o
   ``server_certificate_signing_request`` (422, A-37);
2. **``node_id``** del nombre común (el de las dos CSR, igual) y el límite por ``node_id`` (5 cada
   15 min, ``admit_enrollment_node``);
3. **contexto de la organización del nodo declarado** (``NodeIdentity.enrollment``, A-51): un
   ``node_id`` que no es de ningún nodo declarado no tiene organización; deja solo métrica y
   registro estructurado del intento y responde ``schema_invalid`` en el nombre común;
4. código, huella, firma y transacción (``EnrollmentService.enroll``) y la respuesta
   ``NodeEnrollmentResponse`` validada con el modelo estricto de U-01.

Traducción (el resto lo hace ``node_api.rejections``): ``CsrRejected`` → ``schema_invalid`` con su
``field``; ``EnrollmentRejected`` → su ``enrollment_code_*``; ``CredentialUnavailable`` →
``temporarily_unavailable`` con ``retry_after_seconds``. El PEM, el código y la huella nunca salen
en un registro, una métrica, un tramo ni un evento (NFR-GOB-25).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from vigia_contracts.models import api
from vigia_contracts.models.api import ContractValidationError
from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.models.node_enrollment import NodeEnrollmentRequest

from vigia_platform.fleet.adapters.ca.csr import (
    CLIENT_CSR_FIELD,
    SERVER_CSR_FIELD,
    CsrRejected,
    NodeCsr,
    read_csr,
)
from vigia_platform.fleet.application.enrollment import (
    CredentialUnavailable,
    EnrollmentPresentation,
    EnrollmentRejected,
    EnrollmentService,
    IssuedCredential,
)
from vigia_platform.fleet.domain.node_subject import serial_hex
from vigia_platform.node_api.identity import NodeIdentity
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest
from vigia_platform.shared.node_ca import certificate_pem
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "credential_document",
    "encoded",
    "enrollment_operation",
    "read_csr_pair",
    "rejection_of",
]


def read_csr_pair(client_pem: str, server_pem: str) -> tuple[NodeCsr, NodeCsr]:
    """Las dos CSR validadas, con el mismo ``node_id`` en el nombre común."""
    try:
        client = read_csr(client_pem, field=CLIENT_CSR_FIELD)
        server = read_csr(server_pem, field=SERVER_CSR_FIELD)
    except CsrRejected as error:
        raise NodeRejection(RejectionCode.SCHEMA_INVALID, field=error.field) from None
    if server.node_id != client.node_id:
        raise NodeRejection(RejectionCode.SCHEMA_INVALID, field=SERVER_CSR_FIELD)
    return client, server


def rejection_of(error: CredentialUnavailable) -> NodeRejection:
    """``temporarily_unavailable`` con el reintento del motivo (1 a 60 s)."""
    return NodeRejection(
        RejectionCode.TEMPORARILY_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds
    )


def credential_document(issued: IssuedCredential) -> dict[str, Any]:
    """``NodeCredential`` del contrato (con ``initial_configuration`` solo en el alta)."""
    credential = issued.credential
    certificates = issued.certificates
    document: dict[str, Any] = {
        "node_id": str(credential.node_id),
        "organization_id": str(credential.organization_id),
        "plant_id": str(credential.plant_id),
        "certificate": certificate_pem(certificates.client).decode("ascii"),
        "server_certificate_pem": certificate_pem(certificates.server).decode("ascii"),
        "ca_chain": certificates.ca_chain,
        "expires_at": format_timestamp(credential.expires_at),
        "platform_public_keys": [dict(key) for key in issued.platform_public_keys],
    }
    if issued.initial_configuration is not None:
        document["initial_configuration"] = dict(issued.initial_configuration)
    if serial_hex(certificates.client.serial_number) != credential.certificate_serial:
        raise RuntimeError("el certificado no es el de la credencial guardada")
    return document


def encoded(document: Mapping[str, Any], *, enrollment: bool) -> bytes:
    """El cuerpo JSON validado con el lector estricto de U-01 (los sobres, tal como se guardaron).

    Un documento que el contrato no admite es un defecto de la plataforma: transitorio, sin el
    detalle (nunca sale una respuesta que el nodo rechazaría).
    """
    body = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    try:
        if enrollment:
            api.parse_node_enrollment_response(body)
        else:
            api.parse_credential_rotation_response(body)
    except ContractValidationError:
        raise RuntimeError("la credencial no cumple el contrato") from None
    return body


def enrollment_operation(
    service: EnrollmentService, identity: NodeIdentity, limits: NodeRateLimits
) -> NodeOperation:
    """El manejador de ``POST enrollment`` sobre ``service``."""

    async def handle(request: NodeRequest) -> NodeReply:
        document = request.document
        if not isinstance(document, NodeEnrollmentRequest):
            raise RuntimeError("alta sin cuerpo verificado")
        client, server = read_csr_pair(
            document.certificate_signing_request, document.server_certificate_signing_request
        )
        limits.admit_enrollment_node(client.node_id)
        enrollment = await identity.enrollment(client.node_id, request.correlation_id)
        presentation = EnrollmentPresentation(
            code=document.enrollment_code,
            hardware_fingerprint=document.hardware_fingerprint,
            software_version=document.software_version,
            contract_version=document.contract_version,
            client=client,
            server=server,
            correlation_id=request.correlation_id,
            source_address=request.source_address,
        )
        try:
            if enrollment is None:
                # Sin nodo declarado no hay organización: métrica y registro estructurado solo.
                await service.unknown_node(presentation)
                raise NodeRejection(RejectionCode.SCHEMA_INVALID, field=CLIENT_CSR_FIELD)
            issued = await service.enroll(enrollment, presentation)
        except CsrRejected as error:
            raise NodeRejection(RejectionCode.SCHEMA_INVALID, field=error.field) from None
        except EnrollmentRejected as error:
            raise NodeRejection(RejectionCode(error.result.value)) from None
        except CredentialUnavailable as error:
            raise rejection_of(error) from None
        return NodeReply(content=encoded(credential_document(issued), enrollment=True))

    return NodeOperation(handle=handle)
