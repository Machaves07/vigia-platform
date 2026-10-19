"""La bandeja del lazo (SUP-U05-01; LC-LAZ §6.3; LC-LAZ-05).

``GET /loop/findings`` (``findings.read``, zona) con los **únicos** filtros de BR-LAZ-02:
``zone_id`` (repetible, hasta 50), ``family``, ``tier``, ``lifecycle_state`` (repetible, uno de los
siete estados activos), ``from`` y ``to`` (periodo ``[from, to)`` sobre la marca agregada), y
``order`` (``newest_first`` por defecto u ``oldest_first``), ``cursor`` y ``limit`` (1 a 200).
Responde ``{items, zones, partitions, next_cursor}``:

- ``items``: los elementos, con sus avisos (acuerdo revocado, acta no vigente, evidencia marcada,
  tramos faltantes) y, en zona unipersonal, todas sus marcas agregadas (``time_aggregated``);
- ``zones``: **cada** zona consultada con su aviso cerrado (``coverage_unavailable``,
  ``not_observed`` o ``no_node_assigned``), su nodo y su ``communication_state``, haya o no
  elementos: la bandeja nunca dice «sin hallazgos» (P2);
- ``partitions``: las plantas cuya proyección se está reconstruyendo o difiere del expediente
  (``projection_rebuilding``, ``projection_diverged``; BR-LAZ-156).

Una zona inexistente, de otra organización o fuera del alcance responde ``not_found`` (nunca
``forbidden``); un parámetro desconocido, repetido donde no se admite o con valor fuera de su
lista, ``invalid_request``. ``Cache-Control: no-store``. Sin auditoría de consulta (BR-LAZ-09).
"""

from __future__ import annotations

import base64
import binascii
import enum
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from vigia_contracts.models.enumerations import EvidenceTier, PredicateFamily

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    cursor_stamp,
    exact_query,
    no_store,
    parse_instant,
)
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.adapters.postgres.inbox_query import (
    INBOX_STATES,
    InboxCursor,
    InboxFilters,
)
from vigia_platform.loop.application.inbox import (
    MAX_INBOX_PAGE,
    InboxItem,
    InboxPage,
    InboxRequestInvalid,
    ZoneDeclaration,
)
from vigia_platform.loop.domain.enums import (
    FindingLifecycleState,
    InboxFindingNotice,
    InboxPartitionNotice,
    InboxZoneNotice,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_ZONES",
    "PATH",
    "InboxOrder",
    "InboxPageOut",
    "decode_inbox_cursor",
    "encode_inbox_cursor",
    "findings_router",
]

PATH: Final = "/loop/findings"
MAX_ZONES: Final = 50
"""Zonas por consulta (``zone_id`` repetible)."""
MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TIMESTAMP_CHARS: Final = 40
_READ: Final = PermissionKey.FINDINGS_READ.value

Services = Annotated[LoopHttp, Depends(loop_http)]


class InboxOrder(enum.StrEnum):
    NEWEST_FIRST = "newest_first"
    OLDEST_FIRST = "oldest_first"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FindingNoticeOut(_Strict):
    code: InboxFindingNotice
    since: str | None
    """Desde cuándo el acuerdo de uso está revocado (agregada en zona unipersonal)."""
    cause: str | None
    """La causa del acta no vigente (``catalog_change`` o ``model_version_change``)."""


class InboxStandardOut(_Strict):
    standard_id: uuid.UUID
    version: int
    """La versión con la que se evaluó, para siempre (H-16)."""


class InboxItemOut(_Strict):
    finding_state_id: uuid.UUID
    subject_kind: str
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
    tier: str
    standard: InboxStandardOut
    lifecycle_state: FindingLifecycleState
    classification_outcome: str | None
    classification_count: int
    age_days: int
    observability_state_at_event: str
    occurred_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    due_at: str | None
    overdue_days: int
    time_aggregated: bool
    aggregation_window_minutes: int | None
    evidence_flagged: bool
    notices: tuple[FindingNoticeOut, ...]


class ZoneOut(_Strict):
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    notice: InboxZoneNotice
    node_id: uuid.UUID | None
    node_code: str | None
    communication_state: str | None


class PartitionOut(_Strict):
    plant_id: uuid.UUID
    notice: InboxPartitionNotice


class InboxPageOut(_Strict):
    items: tuple[InboxItemOut, ...]
    zones: tuple[ZoneOut, ...]
    partitions: tuple[PartitionOut, ...]
    next_cursor: str | None
    """Pásalo como ``cursor`` para la página siguiente; ``null`` si no hay más."""


def encode_inbox_cursor(cursor: InboxCursor) -> str:
    """Cursor opaco: la marca del cubo con microsegundos y el ``finding_state_id``."""
    raw = f"{cursor_stamp(cursor.occurred_at_bucket)}|{cursor.finding_state_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def decode_inbox_cursor(value: str) -> InboxCursor:
    """El cursor de ``encode_inbox_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, separator, identifier = raw.partition("|")
        if not separator:
            raise ValueError("cursor sin separador")
        bucket = parse_instant(moment)
        finding_state_id = uuid.UUID(identifier)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    return InboxCursor(bucket, finding_state_id)


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else format_timestamp(moment.astimezone(UTC))


def item_view(item: InboxItem) -> InboxItemOut:
    return InboxItemOut(
        finding_state_id=item.finding_state_id,
        subject_kind=item.subject_kind,
        plant_id=item.plant_id,
        zone_id=item.zone_id,
        family=item.family,
        tier=item.tier,
        standard=InboxStandardOut(standard_id=item.standard_id, version=item.standard_version),
        lifecycle_state=item.lifecycle_state,
        classification_outcome=item.classification_outcome,
        classification_count=item.classification_count,
        age_days=item.age_days,
        observability_state_at_event=item.observability_state_at_event,
        occurred_at=format_timestamp(item.occurred_at),
        due_at=_stamp(item.due_at),
        overdue_days=item.overdue_days,
        time_aggregated=item.time_aggregated,
        aggregation_window_minutes=item.aggregation_window_minutes,
        evidence_flagged=item.evidence_flagged,
        notices=tuple(
            FindingNoticeOut(code=notice.code, since=_stamp(notice.since), cause=notice.cause)
            for notice in item.notices
        ),
    )


def zone_view(zone: ZoneDeclaration) -> ZoneOut:
    return ZoneOut(
        plant_id=zone.plant_id,
        zone_id=zone.zone_id,
        notice=zone.notice,
        node_id=zone.node_id,
        node_code=zone.node_code,
        communication_state=zone.communication_state,
    )


def page_view(page: InboxPage) -> InboxPageOut:
    return InboxPageOut(
        items=tuple(item_view(item) for item in page.items),
        zones=tuple(zone_view(zone) for zone in page.zones),
        partitions=tuple(
            PartitionOut(plant_id=partition.plant_id, notice=partition.notice)
            for partition in page.partitions
        ),
        next_cursor=None if page.next_cursor is None else encode_inbox_cursor(page.next_cursor),
    )


def _states(values: list[str] | None) -> tuple[FindingLifecycleState, ...] | None:
    if not values:
        return None
    active = {state.value: state for state in INBOX_STATES}
    if any(value not in active for value in values):
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return tuple(dict.fromkeys(active[value] for value in values))


def _zones(values: list[uuid.UUID] | None) -> tuple[uuid.UUID, ...] | None:
    if not values:
        return None
    if len(values) > MAX_ZONES:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return tuple(dict.fromkeys(values))


def findings_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        PATH,
        dependencies=[
            requires(_READ),
            Depends(
                exact_query(
                    "family",
                    "tier",
                    "from",
                    "to",
                    "order",
                    "cursor",
                    "limit",
                    repeatable=("zone_id", "lifecycle_state"),
                )
            ),
        ],
        summary="Bandeja del lazo por zona, desde la proyección, por páginas de hasta 200",
    )
    async def list_findings(
        request: Request,
        response: Response,
        services: Services,
        zone_id: Annotated[list[uuid.UUID] | None, Query()] = None,
        family: PredicateFamily | None = None,
        tier: EvidenceTier | None = None,
        lifecycle_state: Annotated[list[str] | None, Query()] = None,
        from_: Annotated[str | None, Query(alias="from", max_length=_TIMESTAMP_CHARS)] = None,
        to: Annotated[str | None, Query(max_length=_TIMESTAMP_CHARS)] = None,
        order: InboxOrder = InboxOrder.NEWEST_FIRST,
        cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_INBOX_PAGE)] = MAX_INBOX_PAGE,
    ) -> InboxPageOut:
        no_store(response)
        filters = InboxFilters(
            zone_ids=_zones(zone_id),
            family=None if family is None else family.value,
            tier=None if tier is None else tier.value,
            lifecycle_states=_states(lifecycle_state),
            period_from=None if from_ is None else parse_instant(from_),
            period_to=None if to is None else parse_instant(to),
        )
        try:
            page = await installed(services.inbox).page(
                request_context(request),
                filters=filters,
                after=None if cursor is None else decode_inbox_cursor(cursor),
                limit=limit,
                newest_first=order is InboxOrder.NEWEST_FIRST,
            )
        except InboxRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return page_view(page)

    return router
