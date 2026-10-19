"""``loop.inbox`` (LC-LAZ-05; C-PLA-22; SUP-U05-01 ``listar_bandeja``): la bandeja del lazo.

``InboxService.page(context, filters=..., after=..., limit=...)`` sirve una página **solo desde la
proyección** (BR-LAZ-09), con **cero llamadas** a los puertos de U-03 y de U-02 (NFR-LAZ-03,
PAT-LAZ-ESC-03) y **una sola sentencia** (``InboxQuery``), sin auditoría de consulta:

1. **Alcance** (BR-LAZ-01): el contexto reducido a las asignaciones con ``findings.read``
   (``narrowed``); sin ninguna, denegación auditada y ``ResourceNotFound``. Una zona pedida que no
   existe, es de otra organización o está fuera del alcance responde ``not_found`` (también
   auditado), exactamente igual: nunca ``forbidden``.
2. **Filtros** (BR-LAZ-02): los únicos que existen son zona, familia, tier, estado y periodo; no
   hay parámetro de persona, de turno, de equipo ni de cuadrilla (la metapropiedad PR-LAZ-01 lo
   comprueba sobre las rutas registradas). El estado es uno de los siete activos del índice
   parcial; el periodo, ``[from, to)`` sobre ``occurred_at_bucket``.
3. **Paginación por clave** sobre ``(occurred_at_bucket, finding_state_id)`` con página de 1 a
   200 (``MAX_INBOX_PAGE``): una fila insertada mientras se recorre nunca repite ni desplaza a
   otra (PR-LAZ-57).
4. **Lo que se declara** (P2; BR-LAZ-04, 149, 153 y 156): cada zona consultada sale con su aviso
   (``InboxZoneNotice``), su nodo asignado hoy y el ``communication_state`` que U-03 proyecta,
   **también si no tiene ningún elemento**: la bandeja nunca dice «sin hallazgos» ni presenta la
   zona como observada. Una planta con la proyección en ``rebuilding`` o ``diverged`` sale en
   ``partitions`` con su código cerrado (``InboxPartitionNotice``).
5. **Avisos de cada elemento** desde las columnas enriquecidas (BR-LAZ-10, 11, 150 y 155):
   acuerdo revocado (con su fecha), acta no vigente (con su causa), evidencia marcada y tramos
   faltantes. BR-LAZ-12: un hecho sin la compuerta de uso aprobada no se muestra.
6. **Agregación temporal** (BR-LAZ-06 y 07, PAT-LAZ-MAN-05): en una zona unipersonal **toda**
   marca del elemento es el inicio de su ventana (``loop.domain.time_aggregation``); el
   expediente y la proyección conservan la marca exacta, que nunca sale de este módulo.
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
from vigia_platform.loop.adapters.postgres.inbox_query import (
    INBOX_STATES,
    InboxCursor,
    InboxFilters,
    InboxQuery,
    SessionScope,
)
from vigia_platform.loop.domain.enums import (
    FindingLifecycleState,
    InboxFindingNotice,
    InboxPartitionNotice,
    InboxZoneNotice,
)
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.shared.context import ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "MAX_INBOX_PAGE",
    "FindingNotice",
    "InboxItem",
    "InboxPage",
    "InboxRequestInvalid",
    "InboxService",
    "PartitionDeclaration",
    "ZoneDeclaration",
    "inbox_item",
    "session_scope",
    "zone_declaration",
]

MAX_INBOX_PAGE: Final = 200
"""Página máxima de la bandeja (LC-LAZ-05)."""

_READ: Final = PermissionKey.FINDINGS_READ
_PARTITION_NOTICES: Final[Mapping[str, InboxPartitionNotice]] = {
    "rebuilding": InboxPartitionNotice.PROJECTION_REBUILDING,
    "diverged": InboxPartitionNotice.PROJECTION_DIVERGED,
}


class InboxRequestInvalid(Exception):
    """Un filtro con valor imposible: estado no activo, periodo vacío o límite fuera de rango."""


class _Database(Protocol):
    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True)
class FindingNotice:
    code: InboxFindingNotice
    since: datetime | None = None
    """Desde cuándo (acuerdo revocado), agregada si la zona es unipersonal."""
    cause: str | None = None
    """La causa que U-03 declara (acta no vigente)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InboxItem:
    """Un elemento de la bandeja: identificadores de zona, enumeraciones, contadores del lazo y
    marcas presentadas; **ningún** dato de persona (DE §2.2, BR-LAZ-02 y 03)."""

    finding_state_id: uuid.UUID
    subject_kind: str
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    family: str
    tier: str
    standard_id: uuid.UUID
    standard_version: int
    lifecycle_state: FindingLifecycleState
    classification_outcome: str | None
    classification_count: int
    age_days: int
    observability_state_at_event: str
    occurred_at: datetime
    due_at: datetime | None
    overdue_days: int
    time_aggregated: bool
    aggregation_window_minutes: int | None
    """La ventana de la agregación; ``None`` si las marcas son exactas."""
    evidence_flagged: bool
    notices: tuple[FindingNotice, ...]
    cursor: InboxCursor


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneDeclaration:
    """Lo que la bandeja declara de una zona consultada (nunca «observada», nunca «vacía»)."""

    plant_id: uuid.UUID
    zone_id: uuid.UUID
    notice: InboxZoneNotice
    node_id: uuid.UUID | None
    node_code: str | None
    communication_state: str | None


@dataclass(frozen=True, slots=True)
class PartitionDeclaration:
    plant_id: uuid.UUID
    notice: InboxPartitionNotice


@dataclass(frozen=True, slots=True)
class InboxPage:
    items: tuple[InboxItem, ...]
    zones: tuple[ZoneDeclaration, ...]
    partitions: tuple[PartitionDeclaration, ...]
    next_cursor: InboxCursor | None


def session_scope(context: ScopeContext) -> SessionScope:
    """El alcance (ya reducido a ``findings.read``) como listas para la sentencia."""
    organization_wide = False
    plants: list[uuid.UUID] = []
    zones: list[uuid.UUID] = []
    for scope in context.allowed_scopes:
        if scope.scope_level is ScopeLevel.ORGANIZATION:
            organization_wide = organization_wide or scope.scope_id == context.organization_id
        elif scope.scope_level is ScopeLevel.PLANT:
            plants.append(scope.scope_id)
        else:
            zones.append(scope.scope_id)
    return SessionScope(
        organization_wide=organization_wide,
        plant_ids=tuple(sorted(set(plants))),
        zone_ids=tuple(sorted(set(zones))),
    )


def _instant(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.utcoffset() is None:
        raise ValueError("marca sin zona horaria en la proyección")
    return moment


def _optional_instant(value: Any) -> datetime | None:
    return None if value is None else _instant(value)


def inbox_item(row: Mapping[str, Any]) -> InboxItem:
    """El elemento de una fila de la proyección, con **todas** sus marcas presentadas."""
    single = bool(row["single_occupancy_zone"])
    window = int(row["aggregation_window_minutes"])

    def shown(moment: datetime) -> datetime:
        return presented(moment, single_occupancy=single, window_minutes=window)

    notices: list[FindingNotice] = []
    if row["zone_agreement_revoked"]:
        revoked_at = _optional_instant(row["zone_agreement_revoked_at"])
        notices.append(
            FindingNotice(
                InboxFindingNotice.AGREEMENT_REVOKED,
                since=None if revoked_at is None else shown(revoked_at),
            )
        )
    if row["walk_test_regression_pending"]:
        notices.append(
            FindingNotice(
                InboxFindingNotice.WALK_TEST_REGRESSION_PENDING,
                cause=row["walk_test_regression_cause"],
            )
        )
    if row["evidence_flagged"]:
        notices.append(FindingNotice(InboxFindingNotice.EVIDENCE_FLAGGED))
    if row["missing_segments_declared"]:
        notices.append(FindingNotice(InboxFindingNotice.MISSING_SEGMENTS_DECLARED))
    due_at = _optional_instant(row["due_at"])
    finding_state_id = uuid.UUID(str(row["finding_state_id"]))
    return InboxItem(
        finding_state_id=finding_state_id,
        subject_kind=str(row["subject_kind"]),
        plant_id=uuid.UUID(str(row["plant_id"])),
        zone_id=uuid.UUID(str(row["zone_id"])),
        family=str(row["family"]),
        tier=str(row["tier"]),
        standard_id=uuid.UUID(str(row["standard_id"])),
        standard_version=int(row["standard_version"]),
        lifecycle_state=FindingLifecycleState(row["lifecycle_state"]),
        classification_outcome=row["current_classification_outcome"],
        classification_count=int(row["classification_count"]),
        age_days=int(row["age_days"]),
        observability_state_at_event=str(row["observability_state_at_event"]),
        occurred_at=shown(_instant(row["occurred_at_exact"])),
        due_at=None if due_at is None else shown(due_at),
        overdue_days=int(row["overdue_days"]),
        time_aggregated=single,
        aggregation_window_minutes=window if single else None,
        evidence_flagged=bool(row["evidence_flagged"]),
        notices=tuple(notices),
        cursor=InboxCursor(_instant(row["occurred_at_bucket"]), finding_state_id),
    )


def zone_declaration(row: Mapping[str, Any]) -> ZoneDeclaration:
    """El aviso de una zona: sin nodo, nodo que no comunica o cobertura no disponible aquí."""
    node = row["node_id"]
    state = row["communication_state"]
    if node is None:
        notice = InboxZoneNotice.NO_NODE_ASSIGNED
    elif state == "reachable":
        notice = InboxZoneNotice.COVERAGE_UNAVAILABLE
    else:
        notice = InboxZoneNotice.NOT_OBSERVED
    return ZoneDeclaration(
        plant_id=uuid.UUID(str(row["plant_id"])),
        zone_id=uuid.UUID(str(row["zone_id"])),
        notice=notice,
        node_id=None if node is None else uuid.UUID(str(node)),
        node_code=row["node_code"],
        communication_state=None if node is None else str(state),
    )


def _validated(filters: InboxFilters, limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_INBOX_PAGE:
        raise InboxRequestInvalid("limit")
    if filters.lifecycle_states is not None and (
        not filters.lifecycle_states
        or any(state not in INBOX_STATES for state in filters.lifecycle_states)
    ):
        raise InboxRequestInvalid("lifecycle_state")
    if filters.zone_ids is not None and not filters.zone_ids:
        raise InboxRequestInvalid("zone_id")
    if (
        filters.period_from is not None
        and filters.period_to is not None
        and filters.period_from >= filters.period_to
    ):
        raise InboxRequestInvalid("period")


@repository
class InboxService:
    """``listar_bandeja`` (SUP-U05-01)."""

    def __init__(
        self, *, database: _Database, authorizer: Authorizer, query: InboxQuery | None = None
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._query = query if query is not None else InboxQuery()

    async def page(
        self,
        context: ScopeContext,
        *,
        filters: InboxFilters | None = None,
        after: InboxCursor | None = None,
        limit: int = MAX_INBOX_PAGE,
        newest_first: bool = True,
    ) -> InboxPage:
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
                newest_first=newest_first,
            )
        zones = tuple(zone_declaration(row) for row in rows.zones)
        if filters.zone_ids is not None:
            visible = {zone.zone_id for zone in zones}
            for zone_id in filters.zone_ids:
                if zone_id not in visible:
                    # Inexistente, de otra organización o fuera del alcance: lo mismo.
                    await self._authorizer.deny(
                        reduced, _READ, Resource(organization_id, "zone", zone_id)
                    )
        items = tuple(inbox_item(row) for row in rows.items[:limit])
        more = len(rows.items) > limit
        return InboxPage(
            items=items,
            zones=zones,
            partitions=tuple(
                PartitionDeclaration(
                    uuid.UUID(str(row["plant_id"])), _PARTITION_NOTICES[str(row["state"])]
                )
                for row in rows.partitions
            ),
            next_cursor=items[-1].cursor if more and items else None,
        )
