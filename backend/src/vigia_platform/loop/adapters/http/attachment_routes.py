"""Rutas de los adjuntos de cierre: SUP-U05-09 y SUP-U05-10 (LC-LAZ-10).

- ``POST /loop/actions/{action_id}/attachments/upload-grant`` (``actions.manage``, zona): 201
  con la URL de un solo ``PUT`` y las cabeceras que el navegador envía **tal cual**; 400
  ``invalid_request`` (esquema, o ``detail_code`` ``loop_attachment_type_not_allowed`` /
  ``loop_attachment_too_large``), 404, 429 y 503 ``storage_unavailable``.
- ``POST /loop/attachments/{attachment_id}/read-url`` (``closure_attachments.read``, zona): 200
  con la URL de descarga de 15 minutos; 404, 409 (el objeto no es el verificado) y 503.

Las respuestas llevan ``Cache-Control: no-store``: la URL es un secreto de corta vida.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.adapters.attachments.read import AttachmentUnreadable
from vigia_platform.loop.adapters.attachments.rules import (
    AttachmentRejected,
    AttachmentSchemaInvalid,
)
from vigia_platform.loop.application.attachments import (
    AttachmentRateLimited,
    ClosureAttachmentService,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "ATTACHMENTS_STATE_KEY",
    "attachment_router",
    "attachment_service",
    "rejection_error",
    "unavailable_error",
]

ATTACHMENTS_STATE_KEY: Final = "vigia_loop_attachments"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UploadGrantRequest(_Strict):
    """El tipo y el tamaño se comprueban después, con su ``detail_code`` (SUP-U05-09)."""

    content_type: StrictStr = Field(min_length=1, max_length=255)
    size_bytes: StrictInt = Field(ge=1)
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class UploadRequest(_Strict):
    method: Literal["PUT"]
    url: str
    required_headers: dict[str, str]
    expires_at: str


class UploadGrantResponse(_Strict):
    attachment_id: uuid.UUID
    storage_key: str
    upload: UploadRequest


class ReadUrlResponse(_Strict):
    attachment_id: uuid.UUID
    url: str
    expires_at: str
    content_disposition: Literal["attachment"] = "attachment"


def attachment_service(request: Request) -> ClosureAttachmentService:
    service = getattr(request.app.state, ATTACHMENTS_STATE_KEY, None)
    if not isinstance(service, ClosureAttachmentService):
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


Service = Annotated[ClosureAttachmentService, Depends(attachment_service)]


def rejection_error(error: AttachmentRejected) -> ApiError:
    code, _ = HTTP_STATUS_BY_DETAIL_CODE[error.detail]
    return ApiError(code, detail_code=error.detail.value)


def unavailable_error(error: StorageUnavailable) -> ApiError:
    return ApiError(ApiErrorCode.STORAGE_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds)


def attachment_router() -> APIRouter:
    router = APIRouter(tags=["adjuntos de cierre"])

    @router.post(
        "/loop/actions/{action_id}/attachments/upload-grant",
        status_code=201,
        dependencies=[
            requires(
                PermissionKey.ACTIONS_MANAGE.value,
                detail_codes=(
                    LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED.value,
                    LoopDetailCode.ATTACHMENT_TOO_LARGE.value,
                ),
            )
        ],
        summary="Concesión de subida de un adjunto de cierre: un solo PUT de 15 minutos",
    )
    async def upload_grant(
        action_id: uuid.UUID,
        body: UploadGrantRequest,
        request: Request,
        response: Response,
        service: Service,
    ) -> UploadGrantResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            issued = await service.grant_upload(
                request_context(request),
                action_id,
                content_type=body.content_type,
                size_bytes=body.size_bytes,
                sha256=body.sha256,
            )
        except AttachmentRejected as error:
            raise rejection_error(error) from None
        except AttachmentSchemaInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except AttachmentRateLimited as error:
            raise ApiError(
                ApiErrorCode.RATE_LIMITED, retry_after_seconds=error.retry_after_seconds
            ) from None
        except StorageUnavailable as error:
            raise unavailable_error(error) from None
        return UploadGrantResponse(
            attachment_id=issued.grant.attachment_id,
            storage_key=issued.grant.storage_key,
            upload=UploadRequest(
                method="PUT",
                url=issued.upload.url,
                required_headers=dict(issued.upload.headers),
                expires_at=format_timestamp(issued.upload.expires_at),
            ),
        )

    @router.post(
        "/loop/attachments/{attachment_id}/read-url",
        dependencies=[requires(PermissionKey.CLOSURE_ATTACHMENTS_READ.value)],
        summary="URL de descarga de un adjunto de cierre, vigente 15 minutos (concesión auditada)",
    )
    async def read_url(
        attachment_id: uuid.UUID, request: Request, response: Response, service: Service
    ) -> ReadUrlResponse:
        response.headers["Cache-Control"] = "no-store"
        try:
            grant = await service.read_url(request_context(request), attachment_id)
        except AttachmentUnreadable:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        except StorageUnavailable as error:
            raise unavailable_error(error) from None
        return ReadUrlResponse(
            attachment_id=grant.attachment_id,
            url=grant.url,
            expires_at=format_timestamp(grant.expires_at),
        )

    return router
