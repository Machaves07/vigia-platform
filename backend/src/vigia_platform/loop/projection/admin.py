"""Reconstrucción administrativa a demanda (TASK-326, LC-LAZ-03; U04-H-14).

El operador autenticado se audita en la proveedora. Los datos del cliente se recorren con el
contexto de iteración de U-04 existente, nunca con un contexto de operador fabricado en el cliente.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue
from sqlalchemy import text

from vigia_platform.identity.application.admin_cli import AdminError, ExitCode
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.loop.projection.rebuild import AlreadyRebuilding, LeaseLost
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    ContextOrigin,
    ScopeContext,
    repository,
)
from vigia_platform.shared.runtime.units import UnitServices

REBUILD_LOOP_PROJECTION = "rebuild_loop_projection"


@dataclass(frozen=True)
class RebuildTask:
    """Descriptor a demanda para el constructor de contexto existente; sin horario."""

    task_name: str = REBUILD_LOOP_PROJECTION
    unit: ActorUnit = ActorUnit.U04


@repository
class ProjectionRebuildCommand:
    def __init__(self, services: UnitServices) -> None:
        self._services = services

    async def run(
        self,
        operator: ScopeContext,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        *,
        dry_run: bool,
    ) -> Mapping[str, object]:
        from vigia_platform.loop.registration import projection_rebuilder

        services = self._services
        if (
            operator.actor.kind is not ActorKind.OPERATOR
            or operator.origin is not ContextOrigin.ADMIN_COMMAND
            or operator.organization_id != services.provider_organization_id
        ):
            raise ResourceNotFound()
        context = services.contexts.context_for_organization(RebuildTask(), organization_id)
        async with services.database.transaction(context) as transaction:
            plant = (
                await transaction.execute(
                    text(
                        "SELECT plant_id FROM identity.plant"
                        " WHERE organization_id = :organization AND plant_id = :plant"
                    ),
                    {"organization": organization_id, "plant": plant_id},
                )
            ).first()
            if plant is None:
                raise ResourceNotFound()
        output: dict[str, object] = {
            "organization_id": str(organization_id),
            "plant_id": str(plant_id),
        }
        if dry_run:
            return output
        filters: dict[str, JsonValue] = {
            "organization_id": str(organization_id),
            "plant_id": str(plant_id),
        }
        # La intención se confirma antes de las tres fases: una caída no deja mantenimiento mudo.
        await services.audit.append(
            operator, AuditOperation.LOOP_PROJECTION_REBUILD, filters=filters
        )
        try:
            result = await projection_rebuilder(services).rebuild_plant(context, plant_id)
        except (AlreadyRebuilding, LeaseLost) as error:
            raise AdminError(
                "loop_projection_busy",
                "la reconstrucción tiene otro propietario: reintenta",
                ExitCode.UNAVAILABLE,
            ) from error
        await services.audit.append(
            operator,
            AuditOperation.LOOP_PROJECTION_REBUILD,
            filters={**filters, "completed": True},
        )
        return {
            **output,
            "projection_digest": result.projection_digest,
            "episodes_written": result.episodes_written,
            "findings_written": result.findings_written,
        }
