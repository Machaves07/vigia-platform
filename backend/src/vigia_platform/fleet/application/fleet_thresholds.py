"""Umbrales del panel de flota por planta (LC-GOB-15; DE §3.10; BR-GOB-79; interfaces §3.4).

- ``thresholds`` (``GET /plants/{plant_id}/fleet-thresholds``, ``fleet.read`` sobre la planta): la
  fila de la planta o, sin fila, los valores por defecto `[estimación propia]` (100, 30 y 5 000)
  sin autor. Bajo concesión, la lectura deja su ``fleet_read`` (BR-NUC-38).
- ``put_thresholds`` (``PUT``, ``fleet.manage`` sobre la planta): los tres enteros de 1 a
  2 147 483 647 (si no, ``ThresholdInvalid``: ``invalid_request`` con ``fleet_threshold_invalid``)
  y, en una transacción, la fila (``INSERT … ON CONFLICT``: dos cambios concurrentes de una planta
  sin fila no chocan) y su entrada de auditoría ``fleet_thresholds_changed`` con los tres valores
  (fallo cerrado). Los umbrales son una proyección sin tipo de registro: esa entrada es el
  registro del cambio que pide la interfaz («los cambios quedan registrados»), como
  ``signatory_policy_changed`` (A-58).

El cambio se ve en la respuesta siguiente de ``GET /fleet/nodes`` sin recorrer nodos: los avisos se
calculan en la lectura (PAT-GOB-ESC-01, PR-GOB-27).

Planta inexistente, de otra organización o fuera del alcance: ``ResourceNotFound`` antes de mirar
el cuerpo.
"""

from __future__ import annotations

import uuid

from vigia_platform.fleet.adapters.postgres.inventory_queries import PostgresInventoryQueries
from vigia_platform.fleet.domain.fleet_thresholds import FleetThresholds, check_threshold
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository

__all__ = ["FleetThresholdsService"]


@repository
class FleetThresholdsService:
    """Leer y fijar los umbrales del panel de una planta."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        authorizer: Authorizer,
        audit: AuditWriter,
        clock: Clock,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock
        self._queries = PostgresInventoryQueries(database)

    def __repr__(self) -> str:
        return "FleetThresholdsService()"

    async def _plant(
        self, context: ScopeContext, plant_id: uuid.UUID, key: PermissionKey
    ) -> ScopeContext:
        """El contexto autorizado con ``key`` sobre la planta; inexistente o ajena, igual."""
        if not isinstance(context, ScopeContext) or type(plant_id) is not uuid.UUID:
            raise ResourceNotFound()
        if not await self._queries.plant_exists(context, plant_id):
            raise ResourceNotFound()
        return await self._authorizer.authorize(
            context, key, Resource.plant(context.organization_id, plant_id)
        )

    async def thresholds(self, context: ScopeContext, plant_id: uuid.UUID) -> FleetThresholds:
        """Los umbrales vigentes de la planta (``fleet.read``)."""
        authorized = await self._plant(context, plant_id, PermissionKey.FLEET_READ)
        async with self._database.transaction(authorized) as transaction:
            found = await self._queries.thresholds(transaction, plant_id)
            if authorized.concession_id is not None:
                # BR-NUC-38: la lectura del proveedor, auditada en la misma transacción.
                await self._audit.append(
                    authorized,
                    AuditOperation.FLEET_READ,
                    plant_id=plant_id,
                    result_count=1,
                    transaction=transaction,
                )
        return found

    async def put_thresholds(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        *,
        queue_pending_threshold: object,
        queue_age_threshold_minutes: object,
        clock_drift_threshold_ms: object,
    ) -> FleetThresholds:
        """Fija los umbrales de la planta (``fleet.manage``) y deja su auditoría."""
        authorized = await self._plant(context, plant_id, PermissionKey.FLEET_MANAGE)
        thresholds = FleetThresholds(
            plant_id=plant_id,
            queue_pending_threshold=check_threshold(queue_pending_threshold),
            queue_age_threshold_minutes=check_threshold(queue_age_threshold_minutes),
            clock_drift_threshold_ms=check_threshold(clock_drift_threshold_ms),
            updated_by=uuid.UUID(str(authorized.actor.id)),
            updated_at=self._clock.now(),
        )
        async with self._database.transaction(authorized) as transaction:
            await self._queries.save_thresholds(transaction, thresholds)
            await self._audit.append(
                authorized,
                AuditOperation.FLEET_THRESHOLDS_CHANGED,
                plant_id=plant_id,
                filters={
                    "queue_pending_threshold": thresholds.queue_pending_threshold,
                    "queue_age_threshold_minutes": thresholds.queue_age_threshold_minutes,
                    "clock_drift_threshold_ms": thresholds.clock_drift_threshold_ms,
                },
                transaction=transaction,
            )
        return thresholds
