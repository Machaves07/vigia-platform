"""Alarmas de flota de SCR-07 (TASK-225; interfaces §3.4 y «Versión 1.5»; LC-GOB-16).

``GET /fleet/alarms`` (``fleet.read``, organización o planta; ``after``, ``limit`` ≤ 100,
``plant_id``, ``node_id``, ``alarm_kind``, ``status``) → ``{alarms: FleetAlarm[], next_after}``:
las alarmas **por transición**, activas y recientes, la más reciente primero, con ``raised_at`` y
``cleared_at`` (``null`` mientras sigue activa). U-03 solo publica: los destinatarios los resuelve
U-04 con los eventos ``fleet_alarm_raised`` y ``fleet_alarm_cleared``.

Una planta o un nodo inexistentes, de otra organización o fuera del alcance responden
``not_found``, nunca ``forbidden``; un parámetro desconocido o repetido, ``invalid_request``.
``Cache-Control: no-store``.
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

from vigia_platform.fleet.adapters.http.services import FleetHttp, fleet_http, installed
from vigia_platform.fleet.adapters.postgres.fleet_alarm_store import AlarmCursor, AlarmFilters
from vigia_platform.fleet.application.fleet_alarms import MAX_ALARMS_PAGE
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_alarm import AlarmStatus, FleetAlarm
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["alarms_router", "decode_alarm_cursor", "encode_alarm_cursor"]

MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_READ: Final = PermissionKey.FLEET_READ.value

Services = Annotated[FleetHttp, Depends(fleet_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FleetAlarmView(_Strict):
    """``FleetAlarm`` (DE §3.9): solo identificadores, la clase y marcas."""

    alarm_id: uuid.UUID
    alarm_kind: FleetAlarmKind
    plant_id: uuid.UUID
    node_id: uuid.UUID
    zone_id: uuid.UUID | None
    raised_at: str
    cleared_at: str | None
    """``null`` mientras la alarma sigue activa."""


class FleetAlarmPage(_Strict):
    alarms: tuple[FleetAlarmView, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


def alarm_view(alarm: FleetAlarm) -> FleetAlarmView:
    return FleetAlarmView(
        alarm_id=alarm.alarm_id,
        alarm_kind=alarm.alarm_kind,
        plant_id=alarm.plant_id,
        node_id=alarm.node_id,
        zone_id=alarm.zone_id,
        raised_at=format_timestamp(alarm.raised_at),
        cleared_at=None if alarm.cleared_at is None else format_timestamp(alarm.cleared_at),
    )


def encode_alarm_cursor(cursor: AlarmCursor) -> str:
    """Cursor opaco: la marca con microsegundos y el ``alarm_id`` de la última alarma devuelta."""
    raw = f"{cursor.raised_at.astimezone(UTC).isoformat()}|{cursor.alarm_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def decode_alarm_cursor(value: str) -> AlarmCursor:
    """El cursor de ``encode_alarm_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, _, identifier = raw.partition("|")
        raised_at = datetime.fromisoformat(moment)
        alarm_id = uuid.UUID(identifier)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    if raised_at.utcoffset() is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return AlarmCursor(raised_at, alarm_id)


def alarms_router() -> APIRouter:
    router = APIRouter(tags=["flota"])

    @router.get(
        "/fleet/alarms",
        dependencies=[
            requires(_READ),
            Depends(exact_query("after", "limit", "plant_id", "node_id", "alarm_kind", "status")),
        ],
        summary="Alarmas de flota por transición, activas y recientes, por páginas de hasta 100",
    )
    async def list_fleet_alarms(
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_ALARMS_PAGE)] = MAX_ALARMS_PAGE,
        plant_id: uuid.UUID | None = None,
        node_id: uuid.UUID | None = None,
        alarm_kind: FleetAlarmKind | None = None,
        status: AlarmStatus | None = None,
    ) -> FleetAlarmPage:
        response.headers["Cache-Control"] = "no-store"
        cursor = None if after is None else decode_alarm_cursor(after)
        page = await installed(services.alarms).page(
            request_context(request),
            filters=AlarmFilters(
                plant_id=plant_id, node_id=node_id, alarm_kind=alarm_kind, status=status
            ),
            after=cursor,
            limit=limit,
        )
        return FleetAlarmPage(
            alarms=tuple(alarm_view(alarm) for alarm in page.items),
            next_after=None if page.next_after is None else encode_alarm_cursor(page.next_after),
        )

    return router
