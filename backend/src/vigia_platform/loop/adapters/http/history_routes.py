"""El histórico de lo cerrado (TASK-586; O-11; ``loop.application.history``).

``GET /loop/closed-findings`` (``findings.read``, zona): los hallazgos ``closed`` y ``discarded``
del alcance, aparte de la bandeja, desde la proyección y **sin auditoría de consulta**
(BR-LAZ-09). Los **únicos** filtros son los de BR-LAZ-02: ``zone_id`` (repetible, hasta 50),
``family``, ``tier``, ``lifecycle_state`` (repetible, ``closed`` o ``discarded``), ``from`` y
``to`` (periodo ``[from, to)`` sobre la marca que se muestra), más ``cursor`` y ``limit`` (1 a
200, 50 por defecto). La más reciente primero. Responde ``{items, partitions, next_cursor}``:

- ``items``: por fila, zona, familia, tier, estado, hora del hecho (agregada en zona unipersonal,
  BR-LAZ-06), el resultado y la categoría de la clasificación vigente, si tuvo acción y si tiene
  causa raíz firmada. Cada fila abre la ficha de vida
  (``GET /loop/findings/{finding_state_id}/timeline``), que sí se audita;
- ``partitions``: las plantas cuya proyección se está reconstruyendo o difiere del expediente,
  cuyas filas no se sirven (BR-LAZ-156, DR-LAZ-01).

La ruta es ``/loop/closed-findings`` y no ``/loop/findings/closed``, para no chocar con
``/loop/findings/{finding_state_id}``. Una zona inexistente, de otra organización o fuera del
alcance responde ``not_found`` (nunca ``forbidden``); un parámetro desconocido, repetido donde no
se admite o con valor fuera de su lista, ``invalid_request``. ``Cache-Control: no-store``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from vigia_contracts.models.enumerations import EvidenceTier, PredicateFamily

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store, parse_instant
from vigia_platform.loop.adapters.http.findings_routes import (
    MAX_CURSOR_CHARS,
    MAX_ZONES,
    PartitionOut,
    decode_inbox_cursor,
    encode_inbox_cursor,
)
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.adapters.postgres.history_query import CLOSED_STATES
from vigia_platform.loop.adapters.postgres.inbox_query import InboxFilters
from vigia_platform.loop.application.history import (
    DEFAULT_HISTORY_PAGE,
    MAX_HISTORY_PAGE,
    ClosedFinding,
    ClosedFindingsPage,
    HistoryRequestInvalid,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CLOSED_FINDINGS_PATH",
    "ClosedFindingOut",
    "ClosedFindingsPageOut",
    "closed_findings_router",
    "closed_page_view",
]

CLOSED_FINDINGS_PATH: Final = "/loop/closed-findings"
_READ: Final = PermissionKey.FINDINGS_READ.value
_TIMESTAMP_CHARS: Final = 40

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClosedFindingOut(_Strict):
    finding_state_id: uuid.UUID
    plant_id: uuid.UUID
    """La planta del hallazgo: su zona horaria para mostrar la hora (UI-25)."""
    zone_id: uuid.UUID
    family: str
    tier: str
    lifecycle_state: FindingLifecycleState
    """``closed`` o ``discarded``."""
    classification_outcome: str | None
    """El resultado de la clasificación vigente; ``null`` si no la hay."""
    reason_category: str | None
    """La categoría del motivo de la clasificación vigente; ``null`` si no la hay."""
    has_action: bool
    """Tuvo una acción correctiva."""
    has_signed_root_cause: bool
    occurred_at: str
    """Exacta, o el inicio de la ventana de agregación si la zona es unipersonal."""
    time_aggregated: bool
    aggregation_window_minutes: int | None


class ClosedFindingsPageOut(_Strict):
    items: tuple[ClosedFindingOut, ...]
    partitions: tuple[PartitionOut, ...]
    next_cursor: str | None
    """Pásalo como ``cursor`` para la página siguiente; ``null`` si no hay más."""


def _item_view(item: ClosedFinding) -> ClosedFindingOut:
    return ClosedFindingOut(
        finding_state_id=item.finding_state_id,
        plant_id=item.plant_id,
        zone_id=item.zone_id,
        family=item.family,
        tier=item.tier,
        lifecycle_state=item.lifecycle_state,
        classification_outcome=item.classification_outcome,
        reason_category=item.reason_category,
        has_action=item.has_action,
        has_signed_root_cause=item.has_signed_root_cause,
        occurred_at=format_timestamp(item.occurred_at),
        time_aggregated=item.time_aggregated,
        aggregation_window_minutes=item.aggregation_window_minutes,
    )


def closed_page_view(page: ClosedFindingsPage) -> ClosedFindingsPageOut:
    return ClosedFindingsPageOut(
        items=tuple(_item_view(item) for item in page.items),
        partitions=tuple(
            PartitionOut(plant_id=partition.plant_id, notice=partition.notice)
            for partition in page.partitions
        ),
        next_cursor=None if page.next_cursor is None else encode_inbox_cursor(page.next_cursor),
    )


def _states(values: list[str] | None) -> tuple[FindingLifecycleState, ...] | None:
    if not values:
        return None
    allowed = {state.value: state for state in CLOSED_STATES}
    if any(value not in allowed for value in values):
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return tuple(dict.fromkeys(allowed[value] for value in values))


def _zones(values: list[uuid.UUID] | None) -> tuple[uuid.UUID, ...] | None:
    if not values:
        return None
    if len(values) > MAX_ZONES:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return tuple(dict.fromkeys(values))


def closed_findings_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        CLOSED_FINDINGS_PATH,
        dependencies=[
            requires(_READ),
            Depends(
                exact_query(
                    "family",
                    "tier",
                    "from",
                    "to",
                    "cursor",
                    "limit",
                    repeatable=("zone_id", "lifecycle_state"),
                )
            ),
        ],
        summary="Histórico de lo cerrado: hallazgos cerrados y descartados, desde la proyección",
    )
    async def list_closed_findings(
        request: Request,
        response: Response,
        services: Services,
        zone_id: Annotated[list[uuid.UUID] | None, Query()] = None,
        family: PredicateFamily | None = None,
        tier: EvidenceTier | None = None,
        lifecycle_state: Annotated[list[str] | None, Query()] = None,
        from_: Annotated[str | None, Query(alias="from", max_length=_TIMESTAMP_CHARS)] = None,
        to: Annotated[str | None, Query(max_length=_TIMESTAMP_CHARS)] = None,
        cursor: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_HISTORY_PAGE)] = DEFAULT_HISTORY_PAGE,
    ) -> ClosedFindingsPageOut:
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
            page = await installed(services.history).page(
                request_context(request),
                filters=filters,
                after=None if cursor is None else decode_inbox_cursor(cursor),
                limit=limit,
            )
        except HistoryRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return closed_page_view(page)

    return router
