"""Rutas de concesión y lectura de adjuntos de cierre."""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.application.attachments import (
    AttachmentRateLimited,
    ClosureAttachmentService,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.storage import StorageUnavailable

__all__ = ["ATTACHMENTS_STATE_KEY", "attachment_router", "attachment_service"]

ATTACHMENTS_STATE_KEY: Final = "vigia_loop_attachments"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GrantBody(Strict):
    content_type: Literal["image/jpeg", "image/png", "application/pdf"]
    size_bytes: StrictInt = Field(ge=1, le=10 * 1024 * 1024)
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")


class UploadOut(Strict):
    method: Literal["PUT"]
    url: str
    required_headers: dict[str, str]
    expires_at: str


class GrantOut(Strict):
    attachment_id: uuid.UUID
    storage_key: str
    upload: UploadOut


class ReadOut(Strict):
    url: str
    content_disposition: Literal["attachment"] = "attachment"
    x_content_type_options: Literal["nosniff"] = "nosniff"


def attachment_service(request: Request) -> ClosureAttachmentService:
    service = getattr(request.app.state, ATTACHMENTS_STATE_KEY, None)
    if not isinstance(service, ClosureAttachmentService):
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


Service = Annotated[ClosureAttachmentService, Depends(attachment_service)]


def attachment_router() -> APIRouter:
    router = APIRouter(tags=["adjuntos de cierre"])

    @router.post(
        "/loop/actions/{action_id}/attachments/upload-grant",
        status_code=201,
        dependencies=[requires(PermissionKey.ACTIONS_MANAGE.value)],
    )
    async def grant(
        action_id: uuid.UUID,
        body: GrantBody,
        request: Request,
        response: Response,
        service: Service,
    ) -> GrantOut:
        response.headers["Cache-Control"] = "no-store"
        try:
            issued = await service.grant_upload(
                request_context(request),
                action_id,
                content_type=body.content_type,
                size_bytes=body.size_bytes,
                sha256=body.sha256,
                random_bytes=os.urandom,
            )
        except LookupError:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        except ValueError:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except AttachmentRateLimited as error:
            raise ApiError(
                ApiErrorCode.RATE_LIMITED, retry_after_seconds=error.retry_after_seconds
            ) from None
        except StorageUnavailable as error:
            raise ApiError(
                ApiErrorCode.STORAGE_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds
            ) from None
        return GrantOut(
            attachment_id=issued.grant.attachment_id,
            storage_key=issued.grant.storage_key,
            upload=UploadOut(
                method="PUT",
                url=issued.upload.url,
                required_headers=dict(issued.upload.headers),
                expires_at=issued.grant.expires_at.isoformat(),
            ),
        )

    @router.post(
        "/loop/attachments/{attachment_id}/read-url",
        dependencies=[requires(PermissionKey.CLOSURE_ATTACHMENTS_READ.value)],
    )
    async def read(
        attachment_id: uuid.UUID, request: Request, response: Response, service: Service
    ) -> ReadOut:
        response.headers["Cache-Control"] = "no-store"
        try:
            return ReadOut(url=await service.read_url(request_context(request), attachment_id))
        except LookupError:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        except StorageUnavailable as error:
            raise ApiError(
                ApiErrorCode.STORAGE_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds
            ) from None

    return router
