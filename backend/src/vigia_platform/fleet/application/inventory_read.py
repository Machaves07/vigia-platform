"""``fleet.inventory`` y ``fleet.ports``: lectura del inventario de flota (LC-GOB-15, LC-GOB-23b).

- ``page`` (``GET /fleet/nodes``, ``fleet.read`` de organización, planta o zona): el contexto se
  reduce a las asignaciones con ``fleet.read`` (``narrowed``; sin ninguna, denegación auditada y
  ``not_found``) y la página sale de **una** sentencia en una transacción ``READ ONLY``: hasta 100
  nodos por cursor (``code``), filtrables por planta, estado de comunicación y aviso, con los
  avisos calculados en la misma consulta.
- ``detail`` (``GET /fleet/nodes/{node_id}``, ``fleet.read`` sobre la planta del nodo): el mismo
  ``NodeInventory`` más su historia de latidos (90 días, cursor, solo ``payload_summary``).
- ``FleetQueryPort`` (``nodes_by_zone``, ``node``, ``assignment_at``, ``assignment_history``):
  para U-04, en proceso, una sentencia ``READ ONLY`` por operación, con el alcance del contexto
  (``allowed_scopes``, como ``CoveragePort`` de U-02); el rango de ``assignment_history`` tiene
  tope de 366 días y superarlo es ``FleetRangeTooLong``, nunca un resultado truncado.

Inexistente, de otra organización o fuera del alcance: ``ResourceNotFound`` (``not_found``, nunca
``forbidden``). Bajo concesión del proveedor, la lectura de la ruta deja su entrada ``fleet_read``
en la misma transacción (BR-NUC-38, fallo cerrado), así que esa lectura no es ``READ ONLY``.

Ninguna métrica nueva: la lectura no emite series por nodo ni por zona (NFR-GOB-13).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.fleet.adapters.postgres.assignment_queries import PostgresAssignmentQueries
from vigia_platform.fleet.adapters.postgres.inventory_queries import (
    HeartbeatCursor,
    HeartbeatEntry,
    InventoryFilters,
    PostgresInventoryQueries,
    inventory_parameters,
)
from vigia_platform.fleet.ports import (
    MAX_ASSIGNMENT_RANGE,
    FleetQueryInvalid,
    FleetRangeTooLong,
    NodeInventory,
    ZoneAssignmentPeriod,
    ZoneNode,
)
from vigia_platform.identity.authz.authorize import (
    Authorizer,
    Resource,
    ResourceNotFound,
    narrowed,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ContextAbsent, ScopeContext, repository

__all__ = [
    "MAX_HISTORY_PAGE",
    "MAX_NODES_PAGE",
    "FleetInventory",
    "InventoryPage",
    "NodeDetail",
]

MAX_NODES_PAGE: Final = 100
"""Nodos por página de ``GET /fleet/nodes`` (interfaces §3.4: «cursor de hasta 100 nodos»)."""
MAX_HISTORY_PAGE: Final = 100
"""Latidos por página de la historia del detalle `[objetivo propio]`."""


@dataclass(frozen=True, slots=True)
class InventoryPage:
    """Una página del inventario y el ``code`` del último nodo si hay más."""

    items: tuple[NodeInventory, ...]
    next_after: str | None


@dataclass(frozen=True, slots=True)
class NodeDetail:
    """El nodo con una página de su historia de latidos."""

    node: NodeInventory
    history: tuple[HeartbeatEntry, ...]
    next_history: HeartbeatCursor | None


def _context(value: object) -> ScopeContext:
    if not isinstance(value, ScopeContext):
        raise ContextAbsent()
    return value


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise FleetQueryInvalid(f"{name} debe ser una marca con zona horaria")
    return value


@repository
class FleetInventory:
    """El inventario de la consola (SCR-07) y ``FleetQueryPort`` sobre PostgreSQL."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        authorizer: Authorizer,
        audit: AuditWriter,
        clock: Clock,
        provider_organization_id: uuid.UUID,
    ) -> None:
        if type(provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock
        self._provider = provider_organization_id
        self._queries = PostgresInventoryQueries(database)
        self._assignments = PostgresAssignmentQueries(database)

    def __repr__(self) -> str:
        return "FleetInventory()"

    # --- Rutas de la consola ----------------------------------------------------------------

    async def page(
        self,
        context: ScopeContext,
        *,
        filters: InventoryFilters | None = None,
        after: str | None = None,
        limit: int = MAX_NODES_PAGE,
    ) -> InventoryPage:
        """Una página de ``GET /fleet/nodes`` con el alcance de ``fleet.read`` del contexto."""
        context = _context(context)
        if type(limit) is not int or not 1 <= limit <= MAX_NODES_PAGE:
            raise ValueError(f"limit debe estar entre 1 y {MAX_NODES_PAGE}")
        reduced = narrowed(
            context, PermissionKey.FLEET_READ, provider_organization_id=self._provider
        )
        if reduced is None:
            # Ninguna asignación concede fleet.read: denegación auditada, igual que inexistente.
            await self._authorizer.deny(
                context, PermissionKey.FLEET_READ, Resource.organization(context.organization_id)
            )
        filters = filters if filters is not None else InventoryFilters()
        parameters = inventory_parameters(
            reduced, self._clock.now(), filters=filters, after=after, limit=limit + 1
        )
        rows = await self._read(reduced, parameters, filters.plant_id, None)
        items = rows[:limit]
        return InventoryPage(items, items[-1].code if len(rows) > limit and items else None)

    async def detail(
        self,
        context: ScopeContext,
        node_id: uuid.UUID,
        *,
        history_after: HeartbeatCursor | None = None,
        history_limit: int = MAX_HISTORY_PAGE,
    ) -> NodeDetail:
        """``GET /fleet/nodes/{node_id}``: ``fleet.read`` sobre la planta del nodo."""
        context = _context(context)
        if type(history_limit) is not int or not 1 <= history_limit <= MAX_HISTORY_PAGE:
            raise ValueError(f"history_limit debe estar entre 1 y {MAX_HISTORY_PAGE}")
        if type(node_id) is not uuid.UUID:
            raise ResourceNotFound()
        plant_id = await self._queries.node_plant(context, node_id)
        if plant_id is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context, PermissionKey.FLEET_READ, Resource.plant(context.organization_id, plant_id)
        )
        now = self._clock.now()
        parameters = inventory_parameters(authorized, now, node_id=node_id, limit=1)
        async with self._database.transaction(authorized) as transaction:
            found = await self._queries.inventory(transaction, parameters)
            history: Sequence[HeartbeatEntry] = ()
            if found:
                history = await self._queries.heartbeat_history(
                    transaction,
                    plant_id=plant_id,
                    node_id=node_id,
                    now=now,
                    after=history_after,
                    limit=history_limit + 1,
                )
                await self._audited(transaction, authorized, plant_id, node_id, 1)
        if not found:
            raise ResourceNotFound()
        page = tuple(history[:history_limit])
        last = page[-1] if len(history) > history_limit and page else None
        cursor = None if last is None else HeartbeatCursor(last.received_at, last.heartbeat_id)
        return NodeDetail(found[0], page, cursor)

    async def _read(
        self,
        context: ScopeContext,
        parameters: dict[str, Any],
        plant_id: uuid.UUID | None,
        node_id: uuid.UUID | None,
    ) -> tuple[NodeInventory, ...]:
        """Sin concesión, una lectura ``READ ONLY``; bajo concesión, con su ``fleet_read``."""
        if context.concession_id is None:
            return await self._queries.read_inventory(context, parameters)
        async with self._database.transaction(context) as transaction:
            rows = await self._queries.inventory(transaction, parameters)
            await self._audited(transaction, context, plant_id, node_id, len(rows))
        return rows

    async def _audited(
        self,
        transaction: Any,
        context: ScopeContext,
        plant_id: uuid.UUID | None,
        node_id: uuid.UUID | None,
        count: int,
    ) -> None:
        if context.concession_id is None:
            return
        # BR-NUC-38: la lectura del proveedor, auditada en la misma transacción (fallo cerrado).
        await self._audit.append(
            context,
            AuditOperation.FLEET_READ,
            plant_id=plant_id,
            resource=None if node_id is None else ResourceRef("node", node_id),
            result_count=count,
            transaction=transaction,
        )

    # --- FleetQueryPort -----------------------------------------------------------------------

    async def nodes_by_zone(self, context: ScopeContext, zone_id: uuid.UUID) -> Sequence[ZoneNode]:
        """Los nodos que atienden la zona ahora (una sentencia)."""
        context = _context(context)
        if type(zone_id) is not uuid.UUID:
            raise FleetQueryInvalid("zone_id debe ser uuid.UUID")
        nodes = await self._assignments.nodes_by_zone(context, zone_id)
        if nodes is None:
            raise ResourceNotFound()
        return nodes

    async def node(self, context: ScopeContext, node_id: uuid.UUID) -> NodeInventory:
        """El ``NodeInventory`` completo del nodo (una sentencia, la misma de la consola)."""
        context = _context(context)
        if type(node_id) is not uuid.UUID:
            raise FleetQueryInvalid("node_id debe ser uuid.UUID")
        parameters = inventory_parameters(context, self._clock.now(), node_id=node_id, limit=1)
        found = await self._queries.read_inventory(context, parameters)
        if not found:
            raise ResourceNotFound()
        return found[0]

    async def assignment_at(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> ZoneAssignmentPeriod:
        """La asignación de la zona vigente en ``at``, o el hueco sin nodo (una sentencia)."""
        context = _context(context)
        if type(zone_id) is not uuid.UUID:
            raise FleetQueryInvalid("zone_id debe ser uuid.UUID")
        found = await self._assignments.assignment_at(context, zone_id, _instant(at, "at"))
        if found is None:
            raise ResourceNotFound()
        return found

    async def assignment_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> Sequence[ZoneAssignmentPeriod]:
        """Las asignaciones que se solapan con ``[start, end]`` (≤ 366 días; una sentencia)."""
        context = _context(context)
        if type(zone_id) is not uuid.UUID:
            raise FleetQueryInvalid("zone_id debe ser uuid.UUID")
        start = _instant(start, "from")
        end = _instant(end, "to")
        if end < start:
            raise FleetQueryInvalid("from no puede ser posterior a to")
        if end - start > MAX_ASSIGNMENT_RANGE:
            # PAT-GOB-REN-04: el tope se rechaza en el borde, nunca se trunca.
            raise FleetRangeTooLong()
        found = await self._assignments.assignment_history(context, zone_id, start, end)
        if found is None:
            raise ResourceNotFound()
        return found
