"""Versiones de flota sobre PostgreSQL (TASK-226; gob_0018; DE §3.5, §3.11 y §3.12).

- ``plant_nodes`` / ``group_nodes``: los nodos de la publicación, **siempre** filtrados por la
  organización de la transacción y por la planta autorizada (NFR-GOB-30): un nodo de otra planta
  o de otra organización no aparece, y el servicio responde ``not_found``.
- ``lock_inventory``: ``SELECT … ORDER BY node_id FOR UPDATE`` de las filas de
  ``fleet.node_inventory`` de los nodos, el mismo candado que toma el latido
  (``inventory_projection``). Un nodo que nunca envió latido no tiene fila: no hay nada que
  bloquear y la lectura del inventario deriva su versión objetivo de la última publicación.
- ``insert_publication`` y ``project_target``: la fila ⛓ de ``fleet.target_version_publication``
  y ``NodeInventory.target_version`` de cada nodo alcanzado = la versión de su publicación más
  reciente por ``(published_at, publication_id)``, el mismo orden con el que el latido lee la
  ``target_software_version`` (``inventory_projection``). Se lee **después** de bloquear las filas:
  dos publicaciones simultáneas del mismo nodo dejan en el inventario la misma versión que verá el
  latido, sea cual sea el orden en que confirman.
- ``insert_result``: la fila ⛓ de ``fleet.update_result`` con ``ON CONFLICT DO NOTHING``: un
  segundo envío simultáneo con el mismo ``update_result_id`` espera a que el primero confirme y
  no inserta nada (la idempotencia la resuelve después el servicio, con el registro ya visible).
- ``project_result``: ``NodeInventory.last_update_result`` = el resultado con la recepción más
  reciente del nodo, leído **después** de bloquear su fila: dos resultados simultáneos del mismo
  nodo dejan el más reciente, sea cual sea el orden en que confirman.

Las tablas ⛓ solo reciben ``INSERT``; de ``node_inventory`` solo se cambian ``target_version`` y
``last_update_result`` (``updated_at`` es del latido: la cámara del último latido se cruza por
esa marca). Cada sentencia nombra la organización de la transacción (defensa en profundidad sobre
la RLS).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.fleet.domain.enums import UpdateResult
from vigia_platform.fleet.domain.fleet_versions import TargetVersionPublication, UpdateReport
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = ["AcceptedResult", "PostgresFleetVersionStore"]

_PLANT_NODES: Final = text(
    "SELECT node_id FROM identity.node_identity"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND node_id = ANY(CAST(:node_ids AS uuid[]))"
)
_GROUP_NODES: Final = text(
    "SELECT n.node_id FROM identity.node_identity AS n"
    " LEFT JOIN fleet.node_fleet_record AS f"
    " ON f.organization_id = n.organization_id AND f.node_id = n.node_id"
    " WHERE n.organization_id = :organization_id AND n.plant_id = :plant_id"
    " AND n.status <> 'revoked' AND f.decommissioned_at IS NULL"
    " ORDER BY n.node_id LIMIT :limit"
)
_LOCK_INVENTORY: Final = text(
    "SELECT node_id FROM fleet.node_inventory"
    " WHERE organization_id = :organization_id AND node_id = ANY(CAST(:node_ids AS uuid[]))"
    " ORDER BY node_id FOR UPDATE"
)
_INSERT_PUBLICATION: Final = text(
    "INSERT INTO fleet.target_version_publication (publication_id, organization_id, plant_id,"
    " target_version, node_ids, maintenance_window_from, maintenance_window_to, published_by,"
    " published_at, ledger_record_id)"
    " VALUES (:publication_id, :organization_id, :plant_id, :target_version,"
    " CAST(:node_ids AS uuid[]), :window_from, :window_to, :published_by, :published_at,"
    " :ledger_record_id)"
)
_PROJECT_TARGET: Final = text(
    "UPDATE fleet.node_inventory AS v SET target_version = ("
    " SELECT p.target_version FROM fleet.target_version_publication AS p"
    " WHERE p.organization_id = v.organization_id AND p.plant_id = v.plant_id"
    " AND v.node_id = ANY(p.node_ids)"
    " ORDER BY p.published_at DESC, p.publication_id DESC LIMIT 1)"
    " WHERE v.organization_id = :organization_id AND v.plant_id = :plant_id"
    " AND v.node_id = ANY(CAST(:node_ids AS uuid[]))"
)
_INSERT_RESULT: Final = text(
    "INSERT INTO fleet.update_result (update_result_id, organization_id, plant_id, node_id,"
    " target_version, result, reported_at, ledger_record_id)"
    " VALUES (:update_result_id, :organization_id, :plant_id, :node_id, :target_version,"
    " :result, :reported_at, :ledger_record_id)"
    " ON CONFLICT (update_result_id) DO NOTHING RETURNING update_result_id"
)
_ACCEPTED_RESULT: Final = text(
    "SELECT update_result_id, node_id, target_version, result, reported_at, ledger_record_id"
    " FROM fleet.update_result"
    " WHERE organization_id = :organization_id AND update_result_id = :update_result_id"
)
_PROJECT_RESULT: Final = text(
    "UPDATE fleet.node_inventory AS v SET last_update_result = ("
    " SELECT u.result FROM fleet.update_result AS u"
    " WHERE u.organization_id = v.organization_id AND u.plant_id = v.plant_id"
    " AND u.node_id = v.node_id ORDER BY u.reported_at DESC, u.update_result_id DESC LIMIT 1)"
    " WHERE v.organization_id = :organization_id AND v.plant_id = :plant_id"
    " AND v.node_id = :node_id"
)


@dataclass(frozen=True, slots=True)
class AcceptedResult:
    """Un resultado ya aceptado: lo que se escribió y el registro que lo respalda."""

    report: UpdateReport
    ledger_record_id: uuid.UUID


@repository
class PostgresFleetVersionStore:
    """Consultas y escrituras de la publicación de versión objetivo y del resultado."""

    def __init__(self, database: LedgerDatabase) -> None:
        self._database = database

    async def plant_nodes(
        self, transaction: Transaction, plant_id: uuid.UUID, node_ids: Sequence[uuid.UUID]
    ) -> frozenset[uuid.UUID]:
        """Los nodos de ``node_ids`` que son de la planta ``plant_id`` (y de la organización)."""
        result = await transaction.execute(
            _PLANT_NODES,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_ids": [str(node) for node in node_ids],
            },
        )
        return frozenset(uuid.UUID(str(row[0])) for row in result)

    async def group_nodes(
        self, transaction: Transaction, plant_id: uuid.UUID, limit: int
    ) -> tuple[uuid.UUID, ...]:
        """``group = plant``: los nodos no revocados ni dados de baja de la planta, por
        ``node_id`` (como mucho ``limit``: uno más que el máximo detecta el exceso)."""
        result = await transaction.execute(
            _GROUP_NODES,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "limit": limit,
            },
        )
        return tuple(uuid.UUID(str(row[0])) for row in result)

    async def lock_inventory(
        self, transaction: Transaction, node_ids: Sequence[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]:
        """Bloquea las filas de inventario de los nodos, por ``node_id``; las que existen."""
        result = await transaction.execute(
            _LOCK_INVENTORY,
            {
                "organization_id": transaction.context.organization_id,
                "node_ids": [str(node) for node in node_ids],
            },
        )
        return tuple(uuid.UUID(str(row[0])) for row in result)

    async def insert_publication(
        self, transaction: Transaction, publication: TargetVersionPublication
    ) -> None:
        """La fila de ``fleet.target_version_publication`` (⛓)."""
        await transaction.execute(
            _INSERT_PUBLICATION,
            {
                "publication_id": publication.publication_id,
                "organization_id": transaction.context.organization_id,
                "plant_id": publication.plant_id,
                "target_version": publication.target_version,
                "node_ids": [str(node) for node in publication.node_ids],
                "window_from": publication.window.starts_at,
                "window_to": publication.window.ends_at,
                "published_by": publication.published_by,
                "published_at": publication.published_at,
                "ledger_record_id": publication.ledger_record_id,
            },
        )

    async def project_target(
        self, transaction: Transaction, publication: TargetVersionPublication
    ) -> None:
        """``NodeInventory.target_version`` de cada nodo alcanzado que tiene fila (bloqueada)."""
        await transaction.execute(
            _PROJECT_TARGET,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": publication.plant_id,
                "node_ids": [str(node) for node in publication.node_ids],
            },
        )

    async def insert_result(
        self,
        transaction: Transaction,
        plant_id: uuid.UUID,
        report: UpdateReport,
        ledger_record_id: uuid.UUID,
    ) -> bool:
        """La fila de ``fleet.update_result`` (⛓); ``False`` si el identificador ya existe."""
        result = await transaction.execute(
            _INSERT_RESULT,
            {
                "update_result_id": report.update_result_id,
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_id": report.node_id,
                "target_version": report.target_version,
                "result": report.result.value,
                "reported_at": report.reported_at,
                "ledger_record_id": ledger_record_id,
            },
        )
        return result.first() is not None

    async def project_result(
        self, transaction: Transaction, plant_id: uuid.UUID, node_id: uuid.UUID
    ) -> None:
        """``NodeInventory.last_update_result`` del nodo, con su fila ya bloqueada."""
        await transaction.execute(
            _PROJECT_RESULT,
            {
                "organization_id": transaction.context.organization_id,
                "plant_id": plant_id,
                "node_id": node_id,
            },
        )

    async def accepted_result(
        self, context: ScopeContext, update_result_id: uuid.UUID
    ) -> AcceptedResult | None:
        """El resultado aceptado con ``update_result_id`` en la organización, o ``None``."""
        rows = await self._database.read(
            context,
            _ACCEPTED_RESULT,
            {"organization_id": context.organization_id, "update_result_id": update_result_id},
        )
        if not rows:
            return None
        row = rows[0]
        reported_at: datetime = row.reported_at
        return AcceptedResult(
            report=UpdateReport(
                update_result_id=uuid.UUID(str(row.update_result_id)),
                node_id=uuid.UUID(str(row.node_id)),
                target_version=str(row.target_version),
                result=UpdateResult(str(row.result)),
                reported_at=reported_at,
            ),
            ledger_record_id=uuid.UUID(str(row.ledger_record_id)),
        )
