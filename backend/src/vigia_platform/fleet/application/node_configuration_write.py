"""Escritura de ``fleet.node_configuration`` por consola (TASK-544; D-11; A-04, A-09; Q7).

La tabla existía y se leía (``initial_configuration`` en el alta, ``HeartbeatResponse`` en el
latido), pero nadie la escribía: ``time_sources`` quedaba en ``pool.ntp.org``, inalcanzable desde
una planta sin internet. ``vigia-admin node-configuration`` es la vía fijada por Q7, sin ampliar la
API de personas.

- **Validación**: los valores salen siempre de ``NodeConfiguration``, nunca se validan aquí: 1 a 8
  fuentes con su patrón, retención de 1 a 90, token de 600 a 3 600, intervalo de 15 a 600.
  ``mute_after_seconds`` es derivado (A-04): se escribe el de la configuración, nunca otro.
- **Cambio parcial**: lo que la orden no nombra conserva el valor guardado o, sin fila, el de D-11.
  La fila se crea primero con ``ON CONFLICT DO NOTHING`` y se toma con ``SELECT … FOR UPDATE``: dos
  cambios a la vez sobre un nodo sin fila no chocan ni se pisan.
- **Alcance**: el operador se audita en la proveedora y los datos del cliente se tocan con el
  contexto de iteración de su organización, como ``loop.projection.admin``; nunca un contexto de
  operador fabricado en el cliente. La organización y la planta salen de
  ``fleet.vigia_node_enrollment_scope``. Un nodo que no existe es ``ResourceNotFound``.
- **Auditoría**: una entrada ``node_configuration_changed`` por cambio, con actor y nodo, sin texto
  libre: los enteros y **cuántas** fuentes hay, nunca cuáles. Va en la cadena de la proveedora, en
  su propia transacción, **antes** de confirmar la fila: si la auditoría falla, el cambio no ocurre.

**Orden de candados**: la fila del nodo y después la cabeza de auditoría de la proveedora. Ninguna
operación toma esa fila teniendo la cabeza de auditoría.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from pydantic import JsonValue
from sqlalchemy import text

from vigia_platform.fleet.domain.node_configuration import NodeConfiguration
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditReceipt,
    ResourceRef,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    ContextOrigin,
    ScopeContext,
    repository,
)

__all__ = [
    "NodeConfigurationChanges",
    "NodeConfigurationResult",
    "NodeConfigurationWriter",
    "NodeScope",
    "audit_filters",
    "merged_configuration",
]

_NODE_SCOPE: Final = text(
    "SELECT node_id, organization_id, plant_id"
    " FROM fleet.vigia_node_enrollment_scope(CAST(:node_id AS uuid))"
)
_SEED_ROW: Final = text(
    "INSERT INTO fleet.node_configuration (node_id, organization_id, plant_id, time_sources,"
    " sent_records_retention_days, token_max_age_seconds, heartbeat_interval_seconds,"
    " mute_after_seconds, updated_at)"
    " VALUES (:node_id, :organization_id, :plant_id, CAST(:time_sources AS jsonb),"
    " :sent_records_retention_days, :token_max_age_seconds, :heartbeat_interval_seconds,"
    " :mute_after_seconds, :updated_at)"
    " ON CONFLICT (node_id) DO NOTHING"
)
_LOCK_ROW: Final = text(
    "SELECT time_sources, sent_records_retention_days, token_max_age_seconds,"
    " heartbeat_interval_seconds FROM fleet.node_configuration"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND node_id = :node_id"
    " FOR UPDATE"
)
_READ_ROW: Final = text(
    "SELECT time_sources, sent_records_retention_days, token_max_age_seconds,"
    " heartbeat_interval_seconds FROM fleet.node_configuration"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND node_id = :node_id"
)
_UPDATE_ROW: Final = text(
    "UPDATE fleet.node_configuration SET time_sources = CAST(:time_sources AS jsonb),"
    " sent_records_retention_days = :sent_records_retention_days,"
    " token_max_age_seconds = :token_max_age_seconds,"
    " heartbeat_interval_seconds = :heartbeat_interval_seconds,"
    " mute_after_seconds = :mute_after_seconds, updated_at = :updated_at"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id AND node_id = :node_id"
)


class _Database(Protocol):
    def transaction(self, context: ScopeContext) -> Any: ...


class _Contexts(Protocol):
    def context_for_organization(self, task: Any, organization_id: uuid.UUID) -> ScopeContext: ...


class _Audit(Protocol):
    async def append(
        self,
        context: ScopeContext,
        operation: AuditOperation,
        *,
        resource: ResourceRef | None = None,
        filters: Mapping[str, JsonValue] | None = None,
    ) -> AuditReceipt: ...


@dataclass(frozen=True)
class _NodeConfigurationTask:
    """Descriptor a demanda para el constructor de contexto existente; sin horario."""

    task_name: str = "node_configuration"
    unit: ActorUnit = ActorUnit.U03


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeConfigurationChanges:
    """Lo que la orden nombra; ``None`` conserva el valor guardado."""

    time_sources: tuple[str, ...] | None = None
    sent_records_retention_days: int | None = None
    token_max_age_seconds: int | None = None
    heartbeat_interval_seconds: int | None = None

    def named(self) -> dict[str, Any]:
        return {
            name: value
            for name, value in (
                ("time_sources", self.time_sources),
                ("sent_records_retention_days", self.sent_records_retention_days),
                ("token_max_age_seconds", self.token_max_age_seconds),
                ("heartbeat_interval_seconds", self.heartbeat_interval_seconds),
            )
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class NodeScope:
    """El nodo declarado: en qué organización y planta está su fila."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeConfigurationResult:
    """La configuración que ve el nodo. ``receipt`` es ``None`` si no se escribió nada."""

    node: NodeScope
    configuration: NodeConfiguration
    stored: bool
    receipt: AuditReceipt | None = None


def merged_configuration(
    current: Mapping[str, Any], changes: NodeConfigurationChanges
) -> NodeConfiguration:
    """La configuración resultante; ``NodeConfigurationInvalid`` si el dominio no la admite."""
    return NodeConfiguration(**{**current, **changes.named()})


def audit_filters(node: NodeScope, configuration: NodeConfiguration) -> dict[str, JsonValue]:
    """Los ``filters`` de la auditoría: identificadores y enteros, nunca las fuentes de tiempo."""
    return {
        "organization_id": str(node.organization_id),
        "plant_id": str(node.plant_id),
        "time_sources_count": len(configuration.time_sources),
        "sent_records_retention_days": configuration.sent_records_retention_days,
        "token_max_age_seconds": configuration.token_max_age_seconds,
        "heartbeat_interval_seconds": configuration.heartbeat_interval_seconds,
        "mute_after_seconds": configuration.mute_after_seconds,
    }


def _values(configuration: NodeConfiguration) -> dict[str, Any]:
    return {
        "time_sources": json.dumps(list(configuration.time_sources)),
        "sent_records_retention_days": configuration.sent_records_retention_days,
        "token_max_age_seconds": configuration.token_max_age_seconds,
        "heartbeat_interval_seconds": configuration.heartbeat_interval_seconds,
        "mute_after_seconds": configuration.mute_after_seconds,
    }


def _stored(row: Any) -> dict[str, Any]:
    """Los cuatro campos de la fila, sin validar: un valor antiguo no impide corregirlo."""
    sources = row.time_sources
    decoded = json.loads(sources) if isinstance(sources, str | bytes) else sources
    return {
        "time_sources": tuple(decoded) if isinstance(decoded, list) else (),
        "sent_records_retention_days": row.sent_records_retention_days,
        "token_max_age_seconds": row.token_max_age_seconds,
        "heartbeat_interval_seconds": row.heartbeat_interval_seconds,
    }


def _defaults() -> dict[str, Any]:
    defaults = NodeConfiguration()
    return {
        "time_sources": defaults.time_sources,
        "sent_records_retention_days": defaults.sent_records_retention_days,
        "token_max_age_seconds": defaults.token_max_age_seconds,
        "heartbeat_interval_seconds": defaults.heartbeat_interval_seconds,
    }


@repository
class NodeConfigurationWriter:
    """``node-configuration show|set`` sobre ``fleet.node_configuration`` (ver el módulo)."""

    def __init__(
        self,
        *,
        database: _Database,
        contexts: _Contexts,
        audit: _Audit,
        clock: Clock,
        provider_organization_id: uuid.UUID,
    ) -> None:
        self._database = database
        self._contexts = contexts
        self._audit = audit
        self._clock = clock
        self._provider = provider_organization_id

    def _require_operator(self, actor: ScopeContext) -> None:
        if (
            actor.actor.kind is not ActorKind.OPERATOR
            or actor.origin is not ContextOrigin.ADMIN_COMMAND
            or actor.organization_id != self._provider
        ):
            raise ResourceNotFound()

    async def _scope(self, node_id: uuid.UUID) -> NodeScope:
        """La búsqueda de ``gob_0019`` solo responde a un actor ``system``, en la proveedora."""
        lookup = self._contexts.context_for_organization(_NodeConfigurationTask(), self._provider)
        async with self._database.transaction(lookup) as transaction:
            row = (await transaction.execute(_NODE_SCOPE, {"node_id": str(node_id)})).first()
        if row is None:
            raise ResourceNotFound()
        return NodeScope(
            uuid.UUID(str(row.node_id)),
            uuid.UUID(str(row.organization_id)),
            uuid.UUID(str(row.plant_id)),
        )

    async def show(self, node_id: uuid.UUID, actor: ScopeContext) -> NodeConfigurationResult:
        """La configuración que recibe el nodo: su fila o, sin fila, los valores de D-11."""
        self._require_operator(actor)
        node = await self._scope(node_id)
        context = self._contexts.context_for_organization(
            _NodeConfigurationTask(), node.organization_id
        )
        async with self._database.transaction(context) as transaction:
            row = (await transaction.execute(_READ_ROW, _keys(node))).first()
        current = _defaults() if row is None else _stored(row)
        return NodeConfigurationResult(
            node=node,
            configuration=merged_configuration(current, NodeConfigurationChanges()),
            stored=row is not None,
        )

    async def set_node_configuration(
        self,
        node_id: uuid.UUID,
        changes: NodeConfigurationChanges,
        actor: ScopeContext,
        *,
        dry_run: bool = False,
    ) -> NodeConfigurationResult:
        """Valida con el dominio y, salvo ``dry_run``, escribe la fila y su única auditoría."""
        self._require_operator(actor)
        node = await self._scope(node_id)
        context = self._contexts.context_for_organization(
            _NodeConfigurationTask(), node.organization_id
        )
        keys = _keys(node)
        async with self._database.transaction(context) as transaction:
            if not dry_run:
                # Antes del candado, para que dos primeras escrituras se pongan en fila.
                await transaction.execute(
                    _SEED_ROW,
                    {
                        **keys,
                        **_values(NodeConfiguration(**_defaults())),
                        "updated_at": self._clock.now(),
                    },
                )
            row = (await transaction.execute(_LOCK_ROW, keys)).first()
            current = _defaults() if row is None else _stored(row)
            configuration = merged_configuration(current, changes)
            if dry_run:
                return NodeConfigurationResult(
                    node=node, configuration=configuration, stored=row is not None
                )
            await transaction.execute(
                _UPDATE_ROW,
                {**keys, **_values(configuration), "updated_at": self._clock.now()},
            )
            # Antes de confirmar la fila: sin su entrada de auditoría, el cambio no ocurre.
            receipt = await self._audit.append(
                actor,
                AuditOperation.NODE_CONFIGURATION_CHANGED,
                resource=ResourceRef("node", node.node_id),
                filters=audit_filters(node, configuration),
            )
        return NodeConfigurationResult(
            node=node, configuration=configuration, stored=True, receipt=receipt
        )


def _keys(node: NodeScope) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "organization_id": node.organization_id,
        "plant_id": node.plant_id,
    }
