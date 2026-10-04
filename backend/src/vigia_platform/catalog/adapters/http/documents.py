"""``POST /documents`` (``commissioning.run`` sobre la planta; LC-GOB-05; interfaces §3.2 y §3.7).

Concesión de subida de un documento firmado de planta: acta de alcance, captura del difuminado,
acuerdo escaneado o política de planta. El navegador calcula la SHA-256 y sube los bytes
**directamente** al depósito con la URL; el documento nunca pasa por ``vigia-api``.

Cuerpo ``{plant_id, kind, content_type, size_bytes, sha256}``. ``plant_id`` no está en la forma de
interfaces §3.2: la ruta exige la clave sobre la planta y la clave del objeto la incluye
(decisión del redactor de TASK-210, a anotar en interfaces §7). Responde ``201`` con
``{document_id, upload: {method, url, required_headers, expires_at}, document_ref}``.

- Tipo o ``kind`` fuera de la lista, tamaño de 0 o mayor que ``VIGIA_DOCUMENTS_MAX_BYTES``, suma
  mal formada o un campo de más: ``invalid_request``, sin concesión.
- Planta inexistente, de otra organización o fuera del alcance: ``not_found``.
- Almacén caído o lento: ``storage_unavailable`` con ``retry_after_seconds``; nada escrito.

La respuesta lleva ``Cache-Control: no-store``: la URL es un secreto de corta vida.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.catalog.adapters.s3.documents import DocumentKeyTaken
from vigia_platform.catalog.application.documents import DocumentService
from vigia_platform.catalog.domain.documents import (
    DOCUMENTS_MAX_BYTES,
    DocumentContentType,
    DocumentRequestInvalid,
)
from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "CATALOG_DOCUMENTS_STATE_KEY",
    "DocumentGrantBody",
    "DocumentGrantOut",
    "documents_http",
    "documents_router",
]

CATALOG_DOCUMENTS_STATE_KEY: Final = "vigia_catalog_documents"
"""Clave de ``app.state`` con el ``DocumentService`` (``units.api_state``)."""

_log = get_logger("catalog.http")


def documents_http(request: Request) -> DocumentService:
    """El servicio de la aplicación; sin él, ``internal_error`` (nunca deja pasar)."""
    service = getattr(request.app.state, CATALOG_DOCUMENTS_STATE_KEY, None)
    if not isinstance(service, DocumentService):
        _log.error("la ruta de documentos no tiene servicio instalado")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


Service = Annotated[DocumentService, Depends(documents_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentGrantBody(_Strict):
    plant_id: uuid.UUID
    kind: DocumentKind
    content_type: DocumentContentType
    size_bytes: StrictInt = Field(ge=1, le=DOCUMENTS_MAX_BYTES)
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentUploadOut(_Strict):
    method: Literal["PUT"]
    url: str
    required_headers: dict[str, str]
    expires_at: str


class DocumentRefOut(_Strict):
    document_id: uuid.UUID
    storage_key: str
    sha256: str
    content_type: DocumentContentType
    size_bytes: int


class DocumentGrantOut(_Strict):
    document_id: uuid.UUID
    upload: DocumentUploadOut
    document_ref: DocumentRefOut


def documents_router() -> APIRouter:
    router = APIRouter(tags=["documentos"])

    @router.post(
        "/documents",
        status_code=201,
        dependencies=[requires(PermissionKey.COMMISSIONING_RUN.value), Depends(exact_query())],
        summary="Concesión de subida de un documento firmado de planta (un solo PUT, 15 minutos)",
    )
    async def grant_document(
        body: DocumentGrantBody, request: Request, response: Response, service: Service
    ) -> DocumentGrantOut:
        no_store(response)
        try:
            issued = await service.issue(
                request_context(request),
                plant_id=body.plant_id,
                kind=body.kind.value,
                content_type=body.content_type.value,
                size_bytes=body.size_bytes,
                sha256=body.sha256,
            )
        except DocumentRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except DocumentKeyTaken:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        except StorageUnavailable as error:
            raise ApiError(
                ApiErrorCode.STORAGE_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds
            ) from None
        grant = issued.grant
        ref = grant.ref
        return DocumentGrantOut(
            document_id=grant.document_id,
            upload=DocumentUploadOut(
                method="PUT",
                url=issued.upload.url,
                required_headers=dict(issued.upload.headers),
                expires_at=format_timestamp(grant.expires_at),
            ),
            document_ref=DocumentRefOut(
                document_id=ref.document_id,
                storage_key=ref.storage_key,
                sha256=ref.sha256,
                content_type=ref.content_type,
                size_bytes=ref.size_bytes,
            ),
        )

    return router
