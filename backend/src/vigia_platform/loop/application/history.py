"""El histórico de lo cerrado (TASK-586; O-11; LC-LAZ-05; BR-LAZ-01, 02, 06, 09 y 12).

``ClosedFindingsService.page(context, filters=..., after=..., limit=...)`` lista los hallazgos
``closed`` y ``discarded``, aparte de la bandeja (que es la cola de trabajo de los siete estados
activos), **solo desde la proyección** y con **una sola sentencia** (``ClosedFindingsQuery``):

1. **Alcance** (BR-LAZ-01), igual que la bandeja: el contexto reducido a las asignaciones con
   ``findings.read``; sin ninguna, denegación auditada y ``ResourceNotFound``. Una zona pedida que
   no existe, es de otra organización o está fuera del alcance responde ``not_found`` (también
   auditado como denegación), exactamente igual: nunca ``forbidden``.
2. **Filtros** (BR-LAZ-02): zona, familia, tier, estado (solo ``closed`` o ``discarded``) y
   periodo ``[from, to)`` sobre la marca que se muestra. Ningún parámetro de persona.
3. **Paginación por clave** sobre ``(occurred_at_bucket, finding_state_id)``, la más reciente
   primero, de 1 a 200 (``MAX_HISTORY_PAGE``) y 50 por defecto (BR-APP-53).
4. **Sin auditoría de consulta** (BR-LAZ-09): listar no escribe nada en la auditoría; la ficha de
   vida que abre cada fila sí (``loop.application.timeline``).
5. **Agregación temporal** (BR-LAZ-06): en una zona unipersonal la hora del hecho es el inicio de
   su ventana; la marca exacta nunca sale de este módulo.

Por fila, solo campos de zona: zona, familia, tier, estado, hora del hecho, resultado y categoría
de la clasificación vigente, si tuvo acción y si tiene causa raíz firmada. Ningún dato de persona.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_platform.identity.authz.authorize import Authorizer, Resource, narrowed
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.adapters.postgres.history_query import CLOSED_STATES, ClosedFindingsQuery
from vigia_platform.loop.adapters.postgres.inbox_query import InboxCursor, InboxFilters
from vigia_platform.loop.application.inbox import PartitionDeclaration, session_scope
from vigia_platform.loop.domain.enums import FindingLifecycleState, InboxPartitionNotice
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "DEFAULT_HISTORY_PAGE",
    "MAX_HISTORY_PAGE",
    "ClosedFinding",
    "ClosedFindingsPage",
    "ClosedFindingsService",
    "HistoryRequestInvalid",
    "closed_finding",
]

MAX_HISTORY_PAGE: Final = 200
"""Página máxima del histórico (BR-APP-53)."""
DEFAULT_HISTORY_PAGE: Final = 50
"""Página por defecto del histórico (BR-APP-53)."""

_READ: Final = PermissionKey.FINDINGS_READ
_PARTITION_NOTICES: Final[Mapping[str, InboxPartitionNotice]] = {
    "rebuilding": InboxPartitionNotice.PROJECTION_REBUILDING,
    "diverged": InboxPartitionNotice.PROJECTION_DIVERGED,
}


class HistoryRequestInvalid(Exception):
    """Un filtro con valor imposible: estado fuera del histórico, periodo vacío o límite."""


class _Database(Protocol):
    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ClosedFinding:
    """Una fila del histórico: identificadores de zona, enumeraciones y marcas; **ningún** dato de
    persona (BR-LAZ-02 y 03)."""

    finding_state_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
    tier: str
    lifecycle_state: FindingLifecycleState
    classification_outcome: str | None
    """El resultado de la clasificación vigente (para etiquetar su categoría)."""
    reason_category: str | None
    """La categoría del motivo de la clasificación vigente."""
    has_action: bool
    has_signed_root_cause: bool
    occurred_at: datetime
    time_aggregated: bool
    aggregation_window_minutes: int | None
    cursor: InboxCursor


@dataclass(frozen=True, slots=True)
class ClosedFindingsPage:
    items: tuple[ClosedFinding, ...]
    partitions: tuple[PartitionDeclaration, ...]
    next_cursor: InboxCursor | None


def _instant(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.utcoffset() is None:
        raise ValueError("marca sin zona horaria en la proyección")
    return moment


def closed_finding(row: Mapping[str, Any]) -> ClosedFinding:
    """La fila del histórico, con la hora del hecho presentada (agregada si es unipersonal)."""
    single = bool(row["single_occupancy_zone"])
    window = int(row["aggregation_window_minutes"])
    finding_state_id = uuid.UUID(str(row["finding_state_id"]))
    return ClosedFinding(
        finding_state_id=finding_state_id,
        plant_id=uuid.UUID(str(row["plant_id"])),
        zone_id=uuid.UUID(str(row["zone_id"])),
        family=str(row["family"]),
        tier=str(row["tier"]),
        lifecycle_state=FindingLifecycleState(row["lifecycle_state"]),
        classification_outcome=row["current_classification_outcome"],
        reason_category=row["current_reason_category"],
        has_action=bool(row["has_action"]),
        has_signed_root_cause=bool(row["has_signed_root_cause"]),
        occurred_at=presented(
            _instant(row["occurred_at_exact"]), single_occupancy=single, window_minutes=window
        ),
        time_aggregated=single,
        aggregation_window_minutes=window if single else None,
        cursor=InboxCursor(_instant(row["occurred_at_bucket"]), finding_state_id),
    )


def _validated(filters: InboxFilters, limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_PAGE:
        raise HistoryRequestInvalid("limit")
    if filters.lifecycle_states is not None and (
        not filters.lifecycle_states
        or any(state not in CLOSED_STATES for state in filters.lifecycle_states)
    ):
        raise HistoryRequestInvalid("lifecycle_state")
    if filters.zone_ids is not None and not filters.zone_ids:
        raise HistoryRequestInvalid("zone_id")
    if (
        filters.period_from is not None
        and filters.period_to is not None
        and filters.period_from >= filters.period_to
    ):
        raise HistoryRequestInvalid("period")


@repository
class ClosedFindingsService:
    """El histórico de lo cerrado (O-11, TASK-586)."""

    def __init__(
        self,
        *,
        database: _Database,
        authorizer: Authorizer,
        query: ClosedFindingsQuery | None = None,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._query = query if query is not None else ClosedFindingsQuery()

    async def page(
        self,
        context: ScopeContext,
        *,
        filters: InboxFilters | None = None,
        after: InboxCursor | None = None,
        limit: int = DEFAULT_HISTORY_PAGE,
    ) -> ClosedFindingsPage:
        filters = filters if filters is not None else InboxFilters()
        _validated(filters, limit)
        organization_id = context.organization_id
        reduced = narrowed(
            context, _READ, provider_organization_id=self._authorizer.provider_organization_id
        )
        if reduced is None:
            await self._authorizer.deny(context, _READ, Resource.organization(organization_id))
        async with self._database.transaction(reduced) as transaction:
            rows = await self._query.page(
                transaction,
                organization_id=organization_id,
                scope=session_scope(reduced),
                filters=filters,
                after=after,
                limit=limit + 1,
            )
        if filters.zone_ids is not None:
            visible = set(rows.zone_ids)
            for zone_id in filters.zone_ids:
                if zone_id not in visible:
                    # Inexistente, de otra organización o fuera del alcance: lo mismo.
                    await self._authorizer.deny(
                        reduced, _READ, Resource(organization_id, "zone", zone_id)
                    )
        items = tuple(closed_finding(row) for row in rows.items[:limit])
        more = len(rows.items) > limit
        return ClosedFindingsPage(
            items=items,
            partitions=tuple(
                PartitionDeclaration(
                    uuid.UUID(str(row["plant_id"])), _PARTITION_NOTICES[str(row["state"])]
                )
                for row in rows.partitions
            ),
            next_cursor=items[-1].cursor if more and items else None,
        )
