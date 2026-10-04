"""Revocación capa 1 y baja del nodo (TASK-218; BR-GOB-66, 67; D-7, D-8, D-14; LC-GOB-10 y 11).

**Revocación** (``POST /nodes/{node_id}/revocation``, ``fleet.manage`` sobre la planta), en **una**
transacción corta (PAT-GOB-RES-02):

1. ``IdentityCommandPort.update_node(status = revoked)``: la primera capa, que ``node_api``
   comprueba en **cada** petición del nodo (``node_revoked``) sin esperar a nada más;
2. toda ``NodeCredential`` ``active``/``overlapping`` del nodo pasa a ``revoked`` (``revoked_at``);
3. ``NodeFleetRecord.revoked_at`` y ``revocation_reason_es``;
4. el registro ``node_revoked`` ``{node_id, reason_es, revoked_at, revoked_by}`` en la cadena de la
   planta con el evento ``node_revoked`` ``{node_id, plant_id, zone_ids[], replaces_node_id?}``;
5. la **marca única** de la lista de revocación (``fleet.revocation_list_state``, D-7):
   ``regenerate_revocation_list`` (TASK-220) publica la lista después; la respuesta no espera al
   almacén de confianza. La marca por organización existe solo como métrica
   (``node_revocations_total``).

Revocar un nodo ya revocado no escribe nada otra vez (el registro no se reescribe, P4): responde
la revocación que ya consta.

**Baja** (``POST /nodes/{node_id}/decommission``, ``fleet.manage``): ``decommissioned_at`` sobre un
nodo revocado, registro ``node_decommissioned`` ``{node_id, reason_es, decommissioned_at}``
(``source_key = node_id``) y evento ``node_decommissioned``; nada se borra (D-14, P4). Sobre un nodo
no revocado, ``fleet_node_not_revoked``; sobre uno ya dado de baja, responde la baja que consta.

``reason_es`` pasa la política de texto libre y solo llega al expediente: nunca a un registro
estructurado, una métrica, una traza ni un evento (NFR-GOB-25). El reemplazo (``node_declaration``)
y la re-alta (``enrollment_codes``) reutilizan estos pasos dentro de su propia transacción.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.fleet.application.common import (
    FleetDependencies,
    FleetRejected,
    authorized_node,
    checked_reason,
    write,
)
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.node_fleet_record import FleetNode
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "DECOMMISSIONED_RECORD_TYPE",
    "REVOKED_RECORD_TYPE",
    "DecommissionOutcome",
    "NodeRevocationService",
    "RevocationOutcome",
    "decommission_in",
    "lifecycle_payload",
    "revoke_credentials_in",
    "revoke_in",
]

REVOKED_RECORD_TYPE: Final = "node_revoked"
DECOMMISSIONED_RECORD_TYPE: Final = "node_decommissioned"


@dataclass(frozen=True, slots=True)
class RevocationOutcome:
    node_id: uuid.UUID
    revoked_at: datetime
    revoked_credentials: tuple[uuid.UUID, ...] = ()
    dirty_generation: int | None = None
    """La generación sucia que dejó esta revocación; ``None`` si el nodo ya estaba revocado."""


@dataclass(frozen=True, slots=True)
class DecommissionOutcome:
    node_id: uuid.UUID
    revoked_at: datetime
    decommissioned_at: datetime
    changed: bool


def lifecycle_payload(node: FleetNode, zone_ids: Sequence[uuid.UUID]) -> dict[str, Any]:
    """Carga de ``node_revoked`` y ``node_decommissioned`` (``NodeLifecycle``)."""
    payload: dict[str, Any] = {
        "node_id": str(node.node_id),
        "plant_id": str(node.plant_id),
        "zone_ids": [str(zone) for zone in zone_ids],
    }
    if node.record.replaces_node_id is not None:
        payload["replaces_node_id"] = str(node.record.replaces_node_id)
    return payload


async def revoke_credentials_in(
    deps: FleetDependencies, transaction: Transaction, node_id: uuid.UUID, now: datetime
) -> tuple[tuple[uuid.UUID, ...], int | None]:
    """Revoca las credenciales vivas del nodo y, si alguna cambió, deja la marca de la lista."""
    revoked = await deps.nodes.revoke_credentials(transaction, node_id, now)
    generation = await deps.marks.mark_dirty(transaction, now) if revoked else None
    return revoked, generation


async def revoke_in(
    deps: FleetDependencies,
    authorized: ScopeContext,
    transaction: Transaction,
    node: FleetNode,
    reason_es: str,
    now: datetime,
    *,
    served_zones: Sequence[uuid.UUID] | None = None,
) -> RevocationOutcome:
    """Los cinco pasos de la revocación en ``transaction`` (``node`` ya bloqueado).

    ``served_zones``: las zonas del evento cuando el llamador ya se las retiró (reemplazo); por
    omisión, las que tiene asignadas ahora.
    """
    if node.record.revoked_at is not None:
        return RevocationOutcome(node.node_id, node.record.revoked_at)
    writer = with_unit(authorized, ActorUnit.U03)
    await deps.identity.update_node(
        authorized, node.node_id, "revoked", node.live_view_local_url, transaction=transaction
    )
    credentials = await deps.nodes.revoke_credentials(transaction, node.node_id, now)
    if not await deps.nodes.mark_revoked(transaction, node.node_id, now, reason_es):
        raise ResourceNotFound()  # la ficha bloqueada no cambió: nunca se da por revocado
    zones = (
        tuple(served_zones)
        if served_zones is not None
        else await deps.nodes.current_zones(transaction, node.node_id)
    )
    await write(
        deps,
        writer,
        transaction,
        REVOKED_RECORD_TYPE,
        {
            "node_id": str(node.node_id),
            "reason_es": reason_es,
            "revoked_at": format_timestamp(now),
            "revoked_by": str(authorized.actor.id),
        },
        plant_id=node.plant_id,
        occurred_at=now,
        events=(NewEvent(event_name="node_revoked", payload=lifecycle_payload(node, zones)),),
    )
    generation = await deps.marks.mark_dirty(transaction, now)
    await deps.audit.append(
        writer,
        AuditOperation.NODE_REVOKED,
        plant_id=node.plant_id,
        resource=ResourceRef("node", node.node_id),
        transaction=transaction,
    )
    return RevocationOutcome(node.node_id, now, credentials, generation)


async def decommission_in(
    deps: FleetDependencies,
    authorized: ScopeContext,
    transaction: Transaction,
    node: FleetNode,
    reason_es: str,
    now: datetime,
) -> DecommissionOutcome:
    """La baja de ``node`` (bloqueado y ya revocado en esta transacción o antes)."""
    revoked_at = node.record.revoked_at
    if node.record.decommissioned_at is not None and revoked_at is not None:
        return DecommissionOutcome(node.node_id, revoked_at, node.record.decommissioned_at, False)
    if revoked_at is None:
        raise FleetRejected(FleetDetailCode.NODE_NOT_REVOKED)
    writer = with_unit(authorized, ActorUnit.U03)
    if not await deps.nodes.mark_decommissioned(transaction, node.node_id, now):
        raise FleetRejected(FleetDetailCode.NODE_NOT_REVOKED)
    zones = await deps.nodes.current_zones(transaction, node.node_id)
    await write(
        deps,
        writer,
        transaction,
        DECOMMISSIONED_RECORD_TYPE,
        {
            "node_id": str(node.node_id),
            "reason_es": reason_es,
            "decommissioned_at": format_timestamp(now),
        },
        plant_id=node.plant_id,
        occurred_at=now,
        events=(
            NewEvent(event_name="node_decommissioned", payload=lifecycle_payload(node, zones)),
        ),
    )
    await deps.audit.append(
        writer,
        AuditOperation.NODE_DECOMMISSIONED,
        plant_id=node.plant_id,
        resource=ResourceRef("node", node.node_id),
        transaction=transaction,
    )
    return DecommissionOutcome(node.node_id, revoked_at, now, True)


@repository
class NodeRevocationService:
    """``POST /nodes/{node_id}/revocation`` y ``POST /nodes/{node_id}/decommission``."""

    def __init__(self, deps: FleetDependencies) -> None:
        self._deps = deps

    def __repr__(self) -> str:
        return "NodeRevocationService()"

    async def revoke(
        self, context: ScopeContext, node_id: uuid.UUID, reason_es: object
    ) -> RevocationOutcome:
        """Revoca el nodo en una transacción corta (BR-GOB-66)."""
        deps = self._deps
        reason = checked_reason(deps.free_text, reason_es, REVOKED_RECORD_TYPE)
        authorized, _ = await authorized_node(deps, context, node_id, PermissionKey.FLEET_MANAGE)
        writer = with_unit(authorized, ActorUnit.U03)
        now = deps.clock.now()
        async with deps.database.transaction(writer) as transaction:
            node = await deps.nodes.lock(transaction, node_id)
            if node is None:
                raise ResourceNotFound()
            outcome = await revoke_in(deps, authorized, transaction, node, reason, now)
            if outcome.dirty_generation is None:
                # Ya constaba revocado: la petición queda auditada, sin escribir nada más.
                await deps.audit.append(
                    writer,
                    AuditOperation.NODE_REVOKED,
                    plant_id=node.plant_id,
                    resource=ResourceRef("node", node.node_id),
                    transaction=transaction,
                )
        if outcome.dirty_generation is not None:
            deps.platform_metrics().node_revocations_total.add(
                1, {"organization_id": str(authorized.organization_id)}
            )
        return outcome

    async def decommission(
        self, context: ScopeContext, node_id: uuid.UUID, reason_es: object
    ) -> DecommissionOutcome:
        """Da de baja un nodo revocado (BR-GOB-67); ``fleet_node_not_revoked`` si no lo está."""
        deps = self._deps
        reason = checked_reason(deps.free_text, reason_es, DECOMMISSIONED_RECORD_TYPE)
        authorized, _ = await authorized_node(deps, context, node_id, PermissionKey.FLEET_MANAGE)
        writer = with_unit(authorized, ActorUnit.U03)
        now = deps.clock.now()
        async with deps.database.transaction(writer) as transaction:
            node = await deps.nodes.lock(transaction, node_id)
            if node is None:
                raise ResourceNotFound()
            outcome = await decommission_in(deps, authorized, transaction, node, reason, now)
            if not outcome.changed:
                # La baja ya constaba: la petición queda auditada, sin escribir nada más.
                await deps.audit.append(
                    writer,
                    AuditOperation.NODE_DECOMMISSIONED,
                    plant_id=node.plant_id,
                    resource=ResourceRef("node", node.node_id),
                    transaction=transaction,
                )
        return outcome
