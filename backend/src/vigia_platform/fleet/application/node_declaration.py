"""Declaración, asignación y reemplazo de nodos (TASK-218; BR-GOB-57, 68, 70, 73; BLM §2.3).

U-02 sigue siendo la identidad autoritativa: todo cambio de identidad va por
``IdentityCommandPort`` **en la transacción de U-03** (argumento ``transaction``), así que cada
operación de aquí es atómica:

- ``declare`` (``POST /plants/{plant_id}/nodes``, ``commissioning.run`` sobre la planta):
  ``declare_node`` + ``assign_node_to_zone`` por cada zona + ``NodeFleetRecord`` +
  ``node_communication_state_changed`` con ``state = unknown`` (BR-GOB-73: el estado de
  comunicación nace al declarar). Los rechazos de U-02 viajan con nombre de la flota:
  ``zone_already_served``, ``zone_in_other_plant`` y ``code_in_use``. Si una zona falla, no queda
  nada escrito.
- **Reemplazo** (``replaces_node_id``, BR-GOB-68, respuesta 15): el viejo tiene que ser un nodo con
  ficha de flota de **la misma planta** y sin baja (si no, ``replaced_node_not_found``). En la
  misma transacción el viejo queda ``revoked`` (con sus credenciales revocadas, su código de alta
  ``superseded`` y la marca de la lista), se retira con fecha cada asignación suya y queda con
  ``decommissioned_at``; después se declara el nuevo y se le asignan las zonas del viejo (y las de
  ``zone_ids`` que no tuviera). El instante de la retirada es el de la asignación nueva:
  ``assignment_at`` explica el hueco.

Candados en el orden único de ``fleet.application.common``, todos al empezar: código del nodo →
ficha del viejo → filas de zona por ``zone_id``. Asignar y retirar zona toman la ficha del nodo
(``FOR SHARE``) y después la fila de la zona.
- ``assign_zone`` (``POST /nodes/{node_id}/zones``) y ``unassign_zone``
  (``POST /nodes/{node_id}/zones/{zone_id}/unassignment``, con ``reason_es``): sin rotación ni
  configuración nueva (BR-GOB-70); retirar marca ``unassigned_at``, nunca borra. Un nodo dado de
  baja no recibe zonas (``fleet_node_not_declared``); sí se le pueden retirar.

Un recurso de la ruta fuera de alcance responde ``not_found`` (``ResourceNotFound``), nunca
``forbidden``. Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from vigia_platform.fleet.application.common import (
    FleetDependencies,
    FleetRejected,
    authorized_node,
    checked_reason,
    from_identity,
    write,
)
from vigia_platform.fleet.application.node_revocation import decommission_in, revoke_in
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.node_fleet_record import FleetNode, NodeFleetRecord
from vigia_platform.fleet.record_types import MAX_NODE_ZONES
from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "COMMUNICATION_RECORD_TYPE",
    "REPLACEMENT_REASON",
    "DeclaredNode",
    "NodeDeclarationService",
    "ZoneAssignment",
]

COMMUNICATION_RECORD_TYPE: Final = "node_communication_state_changed"
REPLACEMENT_REASON: Final = "Reemplazo de equipo por un nodo nuevo (BR-GOB-68)"
"""Motivo de la revocación y la baja del nodo reemplazado: el cuerpo de la declaración no lleva
uno y ``reason_es`` es obligatorio en ambos registros."""


@dataclass(frozen=True, slots=True)
class ZoneAssignment:
    assignment_id: uuid.UUID
    node_id: uuid.UUID
    zone_id: uuid.UUID
    assigned_at: datetime
    unassigned_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeclaredNode:
    """El nodo ``declared`` con sus asignaciones (respuesta 201 de la declaración)."""

    node_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    status: str
    declared_at: datetime
    replaces_node_id: uuid.UUID | None
    assignments: tuple[ZoneAssignment, ...]


def _zones(values: Sequence[uuid.UUID]) -> tuple[uuid.UUID, ...]:
    if not isinstance(values, Sequence) or any(type(value) is not uuid.UUID for value in values):
        raise TypeError("zone_ids debe ser una secuencia de uuid.UUID")
    zones = tuple(dict.fromkeys(values))
    if len(zones) != len(values) or len(zones) > MAX_NODE_ZONES:
        raise ValueError(f"zone_ids admite hasta {MAX_NODE_ZONES} zonas sin repetir")
    return zones


@repository
class NodeDeclarationService:
    """Declarar (con o sin reemplazo), añadir y retirar zonas de un nodo."""

    def __init__(self, deps: FleetDependencies) -> None:
        self._deps = deps

    def __repr__(self) -> str:
        return "NodeDeclarationService()"

    async def _assign(
        self,
        authorized: ScopeContext,
        transaction: Transaction,
        node_id: uuid.UUID,
        zone_id: uuid.UUID,
    ) -> ZoneAssignment:
        try:
            assignment_id = await self._deps.identity.assign_node_to_zone(
                authorized, node_id, zone_id, transaction=transaction
            )
        except IdentityRejected as error:
            raise from_identity(error) from None
        found = await self._deps.nodes.assignment(transaction, assignment_id)
        if found is None:  # recién escrita en esta transacción: siempre está
            raise ResourceNotFound()
        return ZoneAssignment(
            found.assignment_id, found.node_id, found.zone_id, found.assigned_at, None
        )

    async def declare(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        *,
        code: str,
        zone_ids: Sequence[uuid.UUID],
        replaces_node_id: uuid.UUID | None = None,
    ) -> DeclaredNode:
        """Declara el nodo con sus zonas (y, si procede, reemplaza al viejo) en una transacción."""
        deps = self._deps
        zones = _zones(zone_ids)
        if replaces_node_id is not None and type(replaces_node_id) is not uuid.UUID:
            raise TypeError("replaces_node_id debe ser uuid.UUID")
        if type(plant_id) is not uuid.UUID or not await deps.nodes.plant_exists(context, plant_id):
            raise ResourceNotFound()
        authorized = await deps.authorizer.authorize(
            context,
            PermissionKey.COMMISSIONING_RUN,
            Resource.plant(context.organization_id, plant_id),
        )
        writer = with_unit(authorized, ActorUnit.U03)
        now = deps.clock.now()
        async with deps.database.transaction(writer) as transaction:
            # Los candados, antes de escribir nada y en el orden de ``common``: código del nodo →
            # ficha del viejo → filas de zona (las pedidas y las que el viejo deja).
            if isinstance(code, str):
                await deps.nodes.lock_node_code(transaction, code)
            old: FleetNode | None = None
            inherited: tuple[uuid.UUID, ...] = ()
            if replaces_node_id is not None:
                old = await deps.nodes.node_in_plant(transaction, replaces_node_id, plant_id)
                if old is None or old.record.decommissioned:
                    raise FleetRejected(FleetDetailCode.REPLACED_NODE_NOT_FOUND)
                inherited = await deps.nodes.current_zones(transaction, old.node_id)
            await deps.nodes.lock_zones(transaction, (*zones, *inherited))
            if old is not None:
                # El viejo se revoca y se retira antes de declarar el nuevo: sus credenciales y
                # sus códigos se cambian antes de tomar la cadena de la planta.
                await self._retire(authorized, transaction, old, inherited, now)
            try:
                node = await deps.identity.declare_node(
                    authorized, plant_id, code, transaction=transaction
                )
            except IdentityRejected as error:
                raise from_identity(error) from None
            record = NodeFleetRecord(
                node_id=node.node_id,
                organization_id=authorized.organization_id,
                plant_id=plant_id,
                replaces_node_id=replaces_node_id,
                hardware_fingerprint=None,
                declared_at=now,
                declared_by=authorized.actor.id,
            )
            await deps.nodes.insert(transaction, record)
            await write(
                deps,
                writer,
                transaction,
                COMMUNICATION_RECORD_TYPE,
                {"node_id": str(node.node_id), "state": "unknown", "since": format_timestamp(now)},
                plant_id=plant_id,
                occurred_at=now,
            )
            await deps.audit.append(
                writer,
                AuditOperation.NODE_DECLARED,
                plant_id=plant_id,
                resource=ResourceRef("node", node.node_id),
                transaction=transaction,
            )
            assignments = [
                await self._assign(authorized, transaction, node.node_id, zone_id)
                for zone_id in (*inherited, *(zone for zone in zones if zone not in inherited))
            ]
        return DeclaredNode(
            node_id=node.node_id,
            plant_id=plant_id,
            code=node.code,
            status=node.status,
            declared_at=now,
            replaces_node_id=replaces_node_id,
            assignments=tuple(assignments),
        )

    async def _retire(
        self,
        authorized: ScopeContext,
        transaction: Transaction,
        old: FleetNode,
        zones: tuple[uuid.UUID, ...],
        now: datetime,
    ) -> None:
        """Revoca el nodo reemplazado, le retira ``zones`` y lo da de baja (todo ya bloqueado).

        La revocación va primero: cambia la identidad, las credenciales y los códigos del viejo
        (nivel 4 del orden) antes de escribir el primer registro (nivel 5).
        """
        deps = self._deps
        await revoke_in(
            deps, authorized, transaction, old, REPLACEMENT_REASON, now, served_zones=zones
        )
        for zone_id in zones:
            try:
                await deps.identity.unassign_node(
                    authorized,
                    zone_id,
                    node_id=old.node_id,
                    reason_es=REPLACEMENT_REASON,
                    transaction=transaction,
                )
            except IdentityRejected as error:
                raise from_identity(error) from None
        revoked = await deps.nodes.lock(transaction, old.node_id)
        if revoked is None:
            raise ResourceNotFound()
        await decommission_in(deps, authorized, transaction, revoked, REPLACEMENT_REASON, now)

    async def assign_zone(
        self, context: ScopeContext, node_id: uuid.UUID, zone_id: uuid.UUID
    ) -> ZoneAssignment:
        """Añade ``zone_id`` (de la planta del nodo) a un nodo sin baja (BR-GOB-70)."""
        deps = self._deps
        if type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        authorized, _ = await authorized_node(
            deps, context, node_id, PermissionKey.COMMISSIONING_RUN
        )
        writer = with_unit(authorized, ActorUnit.U03)
        async with deps.database.transaction(writer) as transaction:
            node = await deps.nodes.share(transaction, node_id)
            if node is None:
                raise ResourceNotFound()
            if node.record.decommissioned:
                raise FleetRejected(FleetDetailCode.NODE_NOT_DECLARED)
            await deps.nodes.lock_zones(transaction, (zone_id,))
            return await self._assign(authorized, transaction, node_id, zone_id)

    async def unassign_zone(
        self, context: ScopeContext, node_id: uuid.UUID, zone_id: uuid.UUID, reason_es: object
    ) -> ZoneAssignment:
        """Retira ``zone_id`` del nodo con fecha y motivo; nunca borra (BR-GOB-70)."""
        deps = self._deps
        reason = checked_reason(deps.free_text, reason_es, "node_zone_unassigned")
        if type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        authorized, _ = await authorized_node(
            deps, context, node_id, PermissionKey.COMMISSIONING_RUN
        )
        writer = with_unit(authorized, ActorUnit.U03)
        async with deps.database.transaction(writer) as transaction:
            # Ficha del nodo y después la fila de zona, como al asignar y al reemplazar.
            if await deps.nodes.share(transaction, node_id) is None:
                raise ResourceNotFound()
            await deps.nodes.lock_zones(transaction, (zone_id,))
            try:
                assignment_id = await deps.identity.unassign_node(
                    authorized,
                    zone_id,
                    node_id=node_id,
                    reason_es=reason,
                    transaction=transaction,
                )
            except IdentityRejected as error:
                raise from_identity(error) from None
            found = await deps.nodes.assignment(transaction, assignment_id)
            if found is None:
                raise ResourceNotFound()
            return ZoneAssignment(
                found.assignment_id,
                found.node_id,
                found.zone_id,
                found.assigned_at,
                found.unassigned_at,
            )
