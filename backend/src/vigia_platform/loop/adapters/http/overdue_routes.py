"""El panel de vencidos (SUP-U05-15; LC-LAZ §6; LC-LAZ-11; BR-LAZ-47 y 48; H-27).

``GET /loop/overdue`` (``executive_summary.read``, planta) con ``plant_id`` (obligatorio),
``zone_id`` (repetible, hasta 50, zonas de esa planta), ``from`` y ``to`` (periodo ``[from, to)``
sobre ``due_at``), ``min_overdue_days`` (0 a 3 660), ``cursor`` y ``limit`` (1 a 200). Responde
``{plant_id, highlight_threshold_days, items, zones, partition_notice, next_cursor}``:

- ``items``: las acciones vigentes con el plazo pasado, en orden de ``(due_at,
  finding_state_id)``, con sus días de vencimiento y ``highlighted`` si llegan a
  ``highlight_threshold_days`` (``manager_highlight_overdue_days`` de la planta, 30 por defecto,
  PRD §10); ``declared`` dice si la tarea ``evaluate_deadlines`` ya lo declaró;
- ``zones``: por zona, cuántas hay y cuántas destacadas (todas las páginas);
- ``partition_notice``: la proyección de la planta en reconstrucción o divergente (BR-LAZ-156).

Una planta o una zona inexistente, de otra organización o fuera del alcance responde
``not_found`` (nunca ``forbidden``); un parámetro desconocido, repetido donde no se admite o con
valor fuera de rango, ``invalid_request``. ``Cache-Control: no-store``. Sin auditoría de consulta.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    cursor_stamp,
    exact_query,
    no_store,
    parse_instant,
)
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.application.deadlines import (
    MAX_OVERDUE_DAYS,
    MAX_OVERDUE_PAGE,
    MAX_OVERDUE_ZONES,
    OverdueCursor,
    OverdueFilters,
    OverdueItem,
    OverduePage,
    OverdueRequestInvalid,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState, InboxPartitionNotice
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "PATH",
    "OverduePageOut",
    "decode_overdue_cursor",
    "encode_overdue_cursor",
    "overdue_router",
]

PATH: Final = "/loop/overdue"
MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TIMESTAMP_CHARS: Final = 40
_READ: Final = PermissionKey.EXECUTIVE_SUMMARY_READ.value

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OverdueItemOut(_Strict):
    finding_state_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    family: str
    tier: str
    lifecycle_state: FindingLifecycleState
    declared: bool
    """``true`` si ``evaluate_deadlines`` ya declaró el vencimiento (``overdue``)."""
    action_id: uuid.UUID
    action_type: str | None
    assignee_user_id: uuid.UUID | None
    """El responsable de la acción: un usuario de la plataforma, nunca una persona observada."""
    assignee_inactive: bool
    due_at: str
    """El plazo congelado al asignar (fin exclusivo de su último día hábil)."""
    overdue_days: int
    highlighted: bool


class OverdueZoneOut(_Strict):
    zone_id: uuid.UUID
    overdue_count: int
    highlighted_count: int


class OverduePageOut(_Strict):
    plant_id: uuid.UUID
    highlight_threshold_days: int
    items: tuple[OverdueItemOut, ...]
    zones: tuple[OverdueZoneOut, ...]
    partition_notice: InboxPartitionNotice | None
    next_cursor: str | None
    """Pásalo como ``cursor`` para la página siguiente; ``null`` si no hay más."""


def encode_overdue_cursor(cursor: OverdueCursor) -> str:
    """Cursor opaco: ``due_at`` con microsegundos y el ``finding_state_id``."""
    raw = f"{cursor_stamp(cursor.due_at)}|{cursor.finding_state_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def decode_overdue_cursor(value: str) -> OverdueCursor:
    """El cursor de ``encode_overdue_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, separator, identifier = raw.partition("|")
        if not separator:
            raise ValueError("cursor sin separador")
        due_at = parse_instant(moment)
        finding_state_id = uuid.UUID(identifier)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    return OverdueCursor(due_at, finding_state_id)


def _stamp(moment: datetime) -> str:
    return format_timestamp(moment.astimezone(UTC))


def item_view(item: OverdueItem) -> OverdueItemOut:
    return OverdueItemOut(
        finding_state_id=item.finding_state_id,
        plant_id=item.plant_id,
        zone_id=item.zone_id,
        subject_kind=item.subject_kind,
        family=item.family,
        tier=item.tier,
        lifecycle_state=item.lifecycle_state,
        declared=item.declared,
        action_id=item.action_id,
        action_type=item.action_type,
        assignee_user_id=item.assignee_user_id,
        assignee_inactive=item.assignee_inactive,
        due_at=_stamp(item.due_at),
        overdue_days=item.overdue_days,
        highlighted=item.highlighted,
    )


def page_view(page: OverduePage) -> OverduePageOut:
    return OverduePageOut(
        plant_id=page.plant_id,
        highlight_threshold_days=page.highlight_threshold_days,
        items=tuple(item_view(item) for item in page.items),
        zones=tuple(
            OverdueZoneOut(
                zone_id=zone.zone_id,
                overdue_count=zone.overdue_count,
                highlighted_count=zone.highlighted_count,
            )
            for zone in page.zones
        ),
        partition_notice=page.partition_notice,
        next_cursor=None if page.next_cursor is None else encode_overdue_cursor(page.next_cursor),
    )


def _zones(values: list[uuid.UUID] | None) -> tuple[uuid.UUID, ...] | None:
    if not values:
        return None
    if len(values) > MAX_OVERDUE_ZONES:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return tuple(dict.fromkeys(values))


def overdue_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        PATH,
        dependencies=[
            requires(_READ),
            Depends(
                exact_query(
                    "plant_id",
                    "from",
                    "to",
                    "min_overdue_days",
                    "cursor",
                    "limit",
                    repeatable=("zone_id",),
                )
            ),
        ],
        summary="Vencidos de la planta por zona, con los de más días destacados",
    )
    async def list_overdue(
        request: Request,
        response: Response,
        services: Services,
        plant_id: uuid.UUID,
        zone_id: Annotated[list[uuid.UUID] | None, Query()] = None,
        from_: Annotated[str | None, Query(alias="from", max_length=_TIMESTAMP_CHARS)] = None,
        to: Annotated[str | None, Query(max_length=_TIMESTAMP_CHARS)] = None,
        min_overdue_days: Annotated[int, Query(ge=0, le=MAX_OVERDUE_DAYS)] = 0,
        cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_OVERDUE_PAGE)] = MAX_OVERDUE_PAGE,
    ) -> OverduePageOut:
        no_store(response)
        filters = OverdueFilters(
            zone_ids=_zones(zone_id),
            due_from=None if from_ is None else parse_instant(from_),
            due_to=None if to is None else parse_instant(to),
            min_overdue_days=min_overdue_days,
        )
        try:
            page = await installed(services.overdue).page(
                request_context(request),
                plant_id,
                filters=filters,
                after=None if cursor is None else decode_overdue_cursor(cursor),
                limit=limit,
            )
        except OverdueRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return page_view(page)

    return router
