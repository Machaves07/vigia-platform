"""``GET /labels`` (``labels.read``; BR-NUC-67, 68; §10.2).

Consulta de etiquetas por periodo de ``labeled_at`` (``from`` inclusivo, ``to`` exclusivo,
obligatorios), zona, familia y categoría de motivo, de la más reciente a la más antigua, paginada
por clave como el expediente. ``LabelPort.consultar`` ya cuenta solo las asignaciones con
``labels.read`` y audita cada consulta (``label_read``) en la misma transacción; sin ninguna,
``not_found`` auditado como ``denied``.

Cada etiqueta lleva solo identificadores, códigos de listas cerradas y marcas: referencias de
evidencia (nunca clave, URL ni bytes) y, del firmante, su identificador y su rol. No existe
exportación de etiquetas ni de imágenes (BR-NUC-68): esta es la única operación.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    LedgerHttp,
    cursor_stamp,
    exact_query,
    ledger_http,
    no_store,
    parse_instant,
    provider_access,
    request_context,
)
from vigia_platform.ledger.application.labels import (
    MAX_PAGE_SIZE,
    LabelCursor,
    LabelPageRequest,
    LabelPeriod,
    LabelQueryInvalid,
    LabelReadDenied,
    LabelView,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["LabelOut", "LabelPageOut", "labels_router"]

Services = Annotated[LedgerHttp, Depends(ledger_http)]
_SNAKE: Final = r"^[a-z][a-z0-9_]{0,63}$"
_QUERY: Final = (
    "from",
    "to",
    "zone_id",
    "family",
    "reason_category",
    "page_size",
    "after_labeled_at",
    "after_label_id",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabelOut(_Strict):
    label_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    source_record_id: uuid.UUID
    subject_record_id: uuid.UUID
    family: str
    outcome: str
    reason_category: str
    evidence_ids: tuple[uuid.UUID, ...]
    labeled_at: str
    labeled_by_user_id: uuid.UUID | None
    labeled_by_role: str | None


class LabelCursorOut(_Strict):
    labeled_at: str
    label_id: uuid.UUID


class LabelPageOut(_Strict):
    items: tuple[LabelOut, ...]
    next_cursor: LabelCursorOut | None


def _label(view: LabelView) -> LabelOut:
    return LabelOut(
        label_id=view.label_id,
        plant_id=view.plant_id,
        zone_id=view.zone_id,
        source_record_id=view.source_record_id,
        subject_record_id=view.subject_record_id,
        family=view.family,
        outcome=view.outcome,
        reason_category=view.reason_category,
        evidence_ids=view.evidence_ids,
        labeled_at=format_timestamp(view.labeled_at),
        labeled_by_user_id=view.labeled_by_user_id,
        labeled_by_role=None if view.labeled_by_role is None else view.labeled_by_role.value,
    )


def labels_router() -> APIRouter:
    router = APIRouter(tags=["etiquetas"])

    @router.get(
        "/labels",
        dependencies=[requires(PermissionKey.LABELS_READ.value), Depends(exact_query(*_QUERY))],
        summary="Etiquetas por periodo, zona, familia y categoría de motivo (consulta auditada)",
    )
    async def list_labels(
        request: Request,
        response: Response,
        services: Services,
        from_: Annotated[str, Query(alias="from")],
        to: str,
        zone_id: uuid.UUID | None = None,
        family: Annotated[str | None, Query(pattern=_SNAKE)] = None,
        reason_category: Annotated[str | None, Query(pattern=_SNAKE)] = None,
        page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
        after_labeled_at: str | None = None,
        after_label_id: uuid.UUID | None = None,
    ) -> LabelPageOut:
        no_store(response)
        context = request_context(request)
        if (after_labeled_at is None) != (after_label_id is None):
            raise ApiError(ApiErrorCode.INVALID_REQUEST)
        after = (
            None
            if after_labeled_at is None or after_label_id is None
            else LabelCursor(parse_instant(after_labeled_at), after_label_id)
        )
        period = LabelPeriod(parse_instant(from_), parse_instant(to))
        try:
            page = await services.labels.consultar(
                context,
                period,
                zone_id=zone_id,
                family=family,
                reason_category=reason_category,
                page=LabelPageRequest(size=page_size, after=after),
            )
        except LabelReadDenied:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        except LabelQueryInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        await provider_access(request, services, context, "read")
        cursor = page.next_cursor
        return LabelPageOut(
            items=tuple(_label(view) for view in page.items),
            next_cursor=None
            if cursor is None
            else LabelCursorOut(
                labeled_at=cursor_stamp(cursor.labeled_at), label_id=cursor.label_id
            ),
        )

    return router
