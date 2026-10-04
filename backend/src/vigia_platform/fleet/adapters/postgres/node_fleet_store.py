"""``fleet.node_fleet_record`` y las credenciales del nodo sobre PostgreSQL (TASK-218; gob_0018).

Toda operación recibe un ``ScopeContext`` o una ``Transaction`` abierta con él: la seguridad a nivel
de fila limita a la organización del contexto y, bajo concesión, al alcance concedido. Cada
sentencia nombra además la organización del contexto (defensa en profundidad) y, donde la
operación es de una planta, la **planta** (el nodo que se reemplaza tiene que ser de la planta de
la ruta: ``node_in_plant``).

La identidad del nodo (``identity.node_identity``) y sus asignaciones de zona solo se **leen**
aquí, como en ``node_api.identity``: todo cambio de identidad va por ``IdentityCommandPort``.
``node_fleet_record`` es 🔒 (proyección): solo cambian las columnas de ``gob_0018``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.fleet.domain.node_fleet_record import (
    CredentialState,
    FleetNode,
    NodeFleetRecord,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["NodeAssignment", "PostgresNodeFleetStore"]

_PLANT: Final = text(
    "SELECT plant_id FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
# Las cuatro lecturas del nodo repiten la lista de columnas literal: ``text()`` no admite
# concatenar (VIG001, ``tools/lint_rules.py``).
_NODE: Final = text(
    "SELECT n.node_id, n.organization_id, n.plant_id, n.code, n.status,"
    " n.live_view_local_url AS identity_live_view_local_url, f.replaces_node_id,"
    " f.hardware_fingerprint, f.declared_at, f.declared_by, f.enrolled_at, f.revoked_at,"
    " f.revocation_reason_es, f.decommissioned_at, f.live_view_local_url"
    " FROM fleet.node_fleet_record AS f JOIN identity.node_identity AS n ON n.node_id = f.node_id"
    " WHERE f.organization_id = :organization_id AND f.node_id = :node_id"
)
_LOCK_NODE: Final = text(
    "SELECT n.node_id, n.organization_id, n.plant_id, n.code, n.status,"
    " n.live_view_local_url AS identity_live_view_local_url, f.replaces_node_id,"
    " f.hardware_fingerprint, f.declared_at, f.declared_by, f.enrolled_at, f.revoked_at,"
    " f.revocation_reason_es, f.decommissioned_at, f.live_view_local_url"
    " FROM fleet.node_fleet_record AS f JOIN identity.node_identity AS n ON n.node_id = f.node_id"
    " WHERE f.organization_id = :organization_id AND f.node_id = :node_id FOR UPDATE OF f"
)
_LOCK_NODE_IN_PLANT: Final = text(
    "SELECT n.node_id, n.organization_id, n.plant_id, n.code, n.status,"
    " n.live_view_local_url AS identity_live_view_local_url, f.replaces_node_id,"
    " f.hardware_fingerprint, f.declared_at, f.declared_by, f.enrolled_at, f.revoked_at,"
    " f.revocation_reason_es, f.decommissioned_at, f.live_view_local_url"
    " FROM fleet.node_fleet_record AS f JOIN identity.node_identity AS n ON n.node_id = f.node_id"
    " WHERE f.organization_id = :organization_id AND f.plant_id = :plant_id"
    " AND f.node_id = :node_id FOR UPDATE OF f"
)
_NODE_IN_TRANSACTION: Final = text(
    "SELECT n.node_id, n.organization_id, n.plant_id, n.code, n.status,"
    " n.live_view_local_url AS identity_live_view_local_url, f.replaces_node_id,"
    " f.hardware_fingerprint, f.declared_at, f.declared_by, f.enrolled_at, f.revoked_at,"
    " f.revocation_reason_es, f.decommissioned_at, f.live_view_local_url"
    " FROM fleet.node_fleet_record AS f JOIN identity.node_identity AS n ON n.node_id = f.node_id"
    " WHERE f.organization_id = :organization_id AND f.node_id = :node_id FOR SHARE OF f"
)
_INSERT: Final = text(
    "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id, replaces_node_id,"
    " declared_at, declared_by) VALUES (:node_id, :organization_id, :plant_id,"
    " :replaces_node_id, :declared_at, :declared_by)"
)
_MARK_REVOKED: Final = text(
    "UPDATE fleet.node_fleet_record SET revoked_at = :revoked_at,"
    " revocation_reason_es = :reason_es"
    " WHERE organization_id = :organization_id AND node_id = :node_id AND revoked_at IS NULL"
    " RETURNING node_id"
)
_CLEAR_REVOCATION: Final = text(
    "UPDATE fleet.node_fleet_record SET revoked_at = NULL, revocation_reason_es = NULL"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " AND decommissioned_at IS NULL RETURNING node_id"
)
_MARK_DECOMMISSIONED: Final = text(
    "UPDATE fleet.node_fleet_record SET decommissioned_at = :decommissioned_at"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " AND revoked_at IS NOT NULL AND decommissioned_at IS NULL RETURNING node_id"
)
_CREDENTIALS: Final = text(
    "SELECT credential_id, status, expires_at FROM fleet.node_credential"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " ORDER BY issued_at, credential_id"
)
_REVOKE_CREDENTIALS: Final = text(
    "UPDATE fleet.node_credential SET status = 'revoked', revoked_at = :revoked_at"
    " WHERE organization_id = :organization_id AND node_id = :node_id"
    " AND status IN ('active', 'overlapping') RETURNING credential_id"
)
_CURRENT_ZONES: Final = text(
    "SELECT zone_id FROM identity.zone_node_assignment"
    " WHERE organization_id = :organization_id AND node_id = :node_id AND unassigned_at IS NULL"
    " ORDER BY assigned_at, assignment_id"
)
_ASSIGNMENT: Final = text(
    "SELECT assignment_id, zone_id, node_id, assigned_at, unassigned_at"
    " FROM identity.zone_node_assignment"
    " WHERE organization_id = :organization_id AND assignment_id = :assignment_id"
)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _node(row: Row[Any]) -> FleetNode:
    record = NodeFleetRecord(
        node_id=_uuid(row.node_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        replaces_node_id=_optional_uuid(row.replaces_node_id),
        hardware_fingerprint=row.hardware_fingerprint,
        declared_at=row.declared_at,
        declared_by=_uuid(row.declared_by),
        enrolled_at=row.enrolled_at,
        revoked_at=row.revoked_at,
        revocation_reason_es=row.revocation_reason_es,
        decommissioned_at=row.decommissioned_at,
        live_view_local_url=row.live_view_local_url,
    )
    return FleetNode(
        node_id=record.node_id,
        organization_id=record.organization_id,
        plant_id=record.plant_id,
        code=str(row.code),
        status=str(row.status),
        live_view_local_url=row.identity_live_view_local_url,
        record=record,
    )


class NodeAssignment:
    """Una asignación nodo-zona tal como la dejó U-02 (para la respuesta de la ruta)."""

    __slots__ = ("assigned_at", "assignment_id", "node_id", "unassigned_at", "zone_id")

    def __init__(self, row: Row[Any]) -> None:
        self.assignment_id = _uuid(row.assignment_id)
        self.zone_id = _uuid(row.zone_id)
        self.node_id = _uuid(row.node_id)
        self.assigned_at: datetime = row.assigned_at
        self.unassigned_at: datetime | None = row.unassigned_at


def _key(context: ScopeContext, node_id: uuid.UUID) -> dict[str, Any]:
    return {"organization_id": context.organization_id, "node_id": node_id}


@repository
class PostgresNodeFleetStore:
    """Lecturas y escrituras de ``NodeFleetRecord`` y de las credenciales de un nodo."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def plant_exists(self, context: ScopeContext, plant_id: uuid.UUID) -> bool:
        """¿Existe la planta en la organización del contexto (y la deja ver la RLS)?"""
        rows = await self._database.read(
            context, _PLANT, {"organization_id": context.organization_id, "plant_id": plant_id}
        )
        return bool(rows)

    async def node(self, context: ScopeContext, node_id: uuid.UUID) -> FleetNode | None:
        """El nodo con su ficha de flota, o ``None`` (inexistente, sin ficha o fuera de alcance)."""
        rows = await self._database.read(context, _NODE, _key(context, node_id))
        return _node(rows[0]) if rows else None

    async def read(self, transaction: Transaction, node_id: uuid.UUID) -> FleetNode | None:
        """El nodo leído en la transacción, sin bloquear su ficha."""
        result = await transaction.execute(_NODE, _key(transaction.context, node_id))
        row = result.first()
        return None if row is None else _node(row)

    async def lock(self, transaction: Transaction, node_id: uuid.UUID) -> FleetNode | None:
        """El nodo, bloqueando su ficha hasta el final de la transacción (``FOR UPDATE``)."""
        result = await transaction.execute(_LOCK_NODE, _key(transaction.context, node_id))
        row = result.first()
        return None if row is None else _node(row)

    async def share(self, transaction: Transaction, node_id: uuid.UUID) -> FleetNode | None:
        """El nodo leído en la transacción, sin dejar que otra lo dé de baja a la vez."""
        result = await transaction.execute(_NODE_IN_TRANSACTION, _key(transaction.context, node_id))
        row = result.first()
        return None if row is None else _node(row)

    async def node_in_plant(
        self, transaction: Transaction, node_id: uuid.UUID, plant_id: uuid.UUID
    ) -> FleetNode | None:
        """El nodo de **esa** planta, bloqueado (el que se reemplaza, BR-GOB-68)."""
        result = await transaction.execute(
            _LOCK_NODE_IN_PLANT, {**_key(transaction.context, node_id), "plant_id": plant_id}
        )
        row = result.first()
        return None if row is None else _node(row)

    async def insert(self, transaction: Transaction, record: NodeFleetRecord) -> None:
        if record.organization_id != transaction.context.organization_id:
            raise ValueError("la ficha es de otra organización que la transacción")
        await transaction.execute(
            _INSERT,
            {
                "node_id": record.node_id,
                "organization_id": record.organization_id,
                "plant_id": record.plant_id,
                "replaces_node_id": record.replaces_node_id,
                "declared_at": record.declared_at,
                "declared_by": record.declared_by,
            },
        )

    async def mark_revoked(
        self, transaction: Transaction, node_id: uuid.UUID, revoked_at: datetime, reason_es: str
    ) -> bool:
        """Fija ``revoked_at`` y el motivo si aún no estaba revocada; ``True`` si cambió."""
        result = await transaction.execute(
            _MARK_REVOKED,
            {
                **_key(transaction.context, node_id),
                "revoked_at": revoked_at,
                "reason_es": reason_es,
            },
        )
        return result.first() is not None

    async def clear_revocation(self, transaction: Transaction, node_id: uuid.UUID) -> None:
        """Re-alta desde ``revoked``: la ficha deja de estar revocada (la historia queda en el
        expediente, ``node_revoked``)."""
        await transaction.execute(_CLEAR_REVOCATION, _key(transaction.context, node_id))

    async def mark_decommissioned(
        self, transaction: Transaction, node_id: uuid.UUID, decommissioned_at: datetime
    ) -> bool:
        """Fija ``decommissioned_at`` sobre una ficha revocada y sin baja; ``True`` si cambió."""
        result = await transaction.execute(
            _MARK_DECOMMISSIONED,
            {**_key(transaction.context, node_id), "decommissioned_at": decommissioned_at},
        )
        return result.first() is not None

    async def credentials(
        self, transaction: Transaction, node_id: uuid.UUID
    ) -> tuple[CredentialState, ...]:
        result = await transaction.execute(_CREDENTIALS, _key(transaction.context, node_id))
        return tuple(
            CredentialState(_uuid(row.credential_id), CredentialStatus(row.status), row.expires_at)
            for row in result.all()
        )

    async def revoke_credentials(
        self, transaction: Transaction, node_id: uuid.UUID, revoked_at: datetime
    ) -> tuple[uuid.UUID, ...]:
        """Toda credencial ``active``/``overlapping`` del nodo pasa a ``revoked``."""
        result = await transaction.execute(
            _REVOKE_CREDENTIALS, {**_key(transaction.context, node_id), "revoked_at": revoked_at}
        )
        return tuple(sorted(_uuid(row.credential_id) for row in result.all()))

    async def current_zones(
        self, transaction: Transaction, node_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        """Las zonas asignadas ahora al nodo, en orden de asignación."""
        result = await transaction.execute(_CURRENT_ZONES, _key(transaction.context, node_id))
        return tuple(_uuid(row.zone_id) for row in result.all())

    async def assignment(
        self, transaction: Transaction, assignment_id: uuid.UUID
    ) -> NodeAssignment | None:
        result = await transaction.execute(
            _ASSIGNMENT,
            {
                "organization_id": transaction.context.organization_id,
                "assignment_id": assignment_id,
            },
        )
        row = result.first()
        return None if row is None else NodeAssignment(row)
