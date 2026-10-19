"""Las dos rutas de la cola de revisión (TASK-309; LC-LAZ-08; SUP-U05-05 y 06).

``GET /loop/review-queue`` (``findings.read``, zona, igual que la bandeja): la página de
elementos **pendientes** visibles, por ``review_due_at`` ascendente (el más urgente primero), de
1 a 200 por página, con cursor opaco. ``Cache-Control: no-store``.

``POST /loop/review-queue/{detection_id}/resolution`` (``review_queue.resolve``, zona): ``outcome``
(``review_confirmed`` o ``review_discarded``), ``reason_category`` de la lista de esa salida y
``reason_text`` de 20 a 1000 caracteres. ``201`` con el recibo y el estado nuevo del sujeto
(``open`` o ``discarded``, T05 o T06). Rechazos de negocio como ``invalid_request`` con su
``detail_code`` (400, A-68): ``loop_reason_required``, ``loop_reason_too_short``,
``loop_blocked_vocabulary_term`` y ``loop_person_attribution_detected``;
``loop_transition_not_allowed`` viaja como ``conflict`` (409) — el elemento ya resuelto, o un
sujeto que nunca estuvo ``in_review``. Una detección inexistente, de otra organización, fuera del
alcance o bajo concesión de proveedor responde ``not_found``.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    cursor_stamp,
    exact_query,
    no_store,
    parse_instant,
)
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.adapters.postgres.review_queue_repository import (
    ReviewQueueCursor,
    ReviewQueueItemRow,
)
from vigia_platform.loop.application.review_queue import (
    MAX_REVIEW_QUEUE_PAGE,
    ReviewQueuePage,
    ReviewQueueRequestInvalid,
    ReviewResolutionRejected,
    ReviewResolutionResult,
)
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "LIST_PATH",
    "RESOLUTION_PATH",
    "ReviewQueueItemOut",
    "ReviewQueuePageOut",
    "ReviewResolutionBody",
    "ReviewResolutionOut",
    "decode_review_queue_cursor",
    "encode_review_queue_cursor",
    "review_queue_router",
]

LIST_PATH: Final = "/loop/review-queue"
RESOLUTION_PATH: Final = "/loop/review-queue/{detection_id}/resolution"
_READ: Final = PermissionKey.FINDINGS_READ.value
_RESOLVE: Final = PermissionKey.REVIEW_QUEUE_RESOLVE.value
_MAX_CURSOR_CHARS: Final = 128
_DETAIL_CODES: Final = (
    LoopDetailCode.REASON_REQUIRED.value,
    LoopDetailCode.REASON_TOO_SHORT.value,
    LoopDetailCode.BLOCKED_VOCABULARY_TERM.value,
    LoopDetailCode.PERSON_ATTRIBUTION_DETECTED.value,
    LoopDetailCode.TRANSITION_NOT_ALLOWED.value,
)

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewQueueItemOut(_Strict):
    detection_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
    standard_id: uuid.UUID
    standard_version: int
    max_confidence: float
    review_threshold: float
    publication_threshold: float
    review_reason: str
    observability_state_at_event: str
    occurred_at: str
    received_at: str
    review_due_at: str
    reminders_sent: int


class ReviewQueuePageOut(_Strict):
    items: tuple[ReviewQueueItemOut, ...]
    next_cursor: str | None
    """Pásalo como ``cursor`` para la página siguiente; ``null`` si no hay más."""


class ReviewResolutionBody(BaseModel):
    """La decisión: salida, categoría y motivo (20 a 1000 caracteres)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    outcome: StrictStr | None = None
    """``review_confirmed`` o ``review_discarded``."""
    reason_category: StrictStr | None = None
    """De la lista cerrada de la salida elegida."""
    reason_text: StrictStr | None = None
    """Motivo de 20 a 1000 caracteres; pasa los dos validadores de texto de U-04."""


class ReviewResolutionOut(_Strict):
    detection_id: uuid.UUID
    resolution_id: uuid.UUID
    record_id: uuid.UUID
    outcome: str
    reason_category: str
    lifecycle_state: str
    resolved_at: str


_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")


def encode_review_queue_cursor(cursor: ReviewQueueCursor) -> str:
    """Cursor opaco: la marca del plazo con microsegundos y el ``detection_id``."""
    raw = f"{cursor_stamp(cursor.review_due_at)}|{cursor.detection_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def decode_review_queue_cursor(value: str) -> ReviewQueueCursor:
    """El cursor de ``encode_review_queue_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, separator, identifier = raw.partition("|")
        if not separator:
            raise ValueError("cursor sin separador")
        review_due_at = parse_instant(moment)
        detection_id = uuid.UUID(identifier)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    return ReviewQueueCursor(review_due_at=review_due_at, detection_id=detection_id)


def _item_view(row: ReviewQueueItemRow) -> ReviewQueueItemOut:
    return ReviewQueueItemOut(
        detection_id=row.detection_id,
        plant_id=row.plant_id,
        zone_id=row.zone_id,
        family=row.family,
        standard_id=row.standard_id,
        standard_version=row.standard_version,
        max_confidence=row.max_confidence,
        review_threshold=row.review_threshold,
        publication_threshold=row.publication_threshold,
        review_reason=row.review_reason,
        observability_state_at_event=row.observability_state_at_event,
        occurred_at=format_timestamp(row.occurred_at_exact),
        received_at=format_timestamp(row.received_at),
        review_due_at=format_timestamp(row.review_due_at),
        reminders_sent=row.reminders_sent,
    )


def _page_view(page: ReviewQueuePage) -> ReviewQueuePageOut:
    return ReviewQueuePageOut(
        items=tuple(_item_view(item) for item in page.items),
        next_cursor=(
            None if page.next_cursor is None else encode_review_queue_cursor(page.next_cursor)
        ),
    )


def _resolution_view(result: ReviewResolutionResult) -> ReviewResolutionOut:
    return ReviewResolutionOut(
        detection_id=result.detection_id,
        resolution_id=result.resolution_id,
        record_id=result.record_id,
        outcome=result.outcome.value,
        reason_category=result.reason_category,
        lifecycle_state=result.lifecycle_state.value,
        resolved_at=format_timestamp(result.resolved_at),
    )


def review_queue_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        LIST_PATH,
        dependencies=[
            requires(_READ),
            Depends(exact_query("cursor", "limit")),
        ],
        summary="Cola de revisión pendiente, por plazo ascendente, por páginas de hasta 200",
    )
    async def list_review_queue(
        request: Request,
        response: Response,
        services: Services,
        cursor: Annotated[str | None, Query(max_length=_MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_REVIEW_QUEUE_PAGE)] = MAX_REVIEW_QUEUE_PAGE,
    ) -> ReviewQueuePageOut:
        no_store(response)
        try:
            page = await installed(services.review_queue).page(
                request_context(request),
                after=None if cursor is None else decode_review_queue_cursor(cursor),
                limit=limit,
            )
        except ReviewQueueRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return _page_view(page)

    @router.post(
        RESOLUTION_PATH,
        status_code=201,
        dependencies=[requires(_RESOLVE, detail_codes=_DETAIL_CODES), Depends(exact_query())],
        summary="Resuelve un elemento de la cola de revisión con motivo validado (registro nuevo)",
    )
    async def resolve(
        detection_id: uuid.UUID,
        body: ReviewResolutionBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ReviewResolutionOut:
        no_store(response)
        try:
            result = await installed(services.review_queue).resolve(
                request_context(request),
                detection_id,
                outcome=body.outcome,
                reason_category=body.reason_category,
                reason_text=body.reason_text,
            )
        except ReviewResolutionRejected as rejected:
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[rejected.detail_code]
            raise ApiError(code, detail_code=rejected.detail_code.value) from None
        except ReviewQueueRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return _resolution_view(result)

    return router
