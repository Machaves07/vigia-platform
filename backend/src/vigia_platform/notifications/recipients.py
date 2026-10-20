"""Destinatarios de una notificación (TASK-324; LC-LAZ-31; BR-LAZ-124, 127, 132 y 137; A-58 y A-69).

**Regla** (BR-LAZ-124): un usuario recibe el aviso solo si tiene uno de los roles de la fila con un
alcance que **contiene** la zona o planta del evento
(``IdentityQueryPort.users_by_role_and_scope``),
y siempre de la organización que corresponde: los roles del cliente en la organización del evento,
los del proveedor en la proveedora (toda ella). **Nunca un destinatario por defecto**: si la
identidad no responde, la excepción sale, la transacción del despachador se revierte y el evento
reintenta (PAT-LAZ-RES-04).

**Alcance del evento** (``event_scope``), en este orden: las zonas de ``zone_ids`` (la
exportación: hay que cubrirlas **todas**), la zona de ``zone_id``, la planta de los eventos de nodo
(``plant_id`` de la carga), la planta de la cadena (``OutboxEvent.plant_id``) y, sin nada de eso, la
organización. Las concesiones van por su propio alcance (``scope_level``/``scope_id`` de
``concession_granted``; las de ``concession_revoked`` y ``concession_expired`` se leen de
``identity.provider_concession``).

**Personas** (``PersonRef``): el responsable de la acción sale de ``assignee_user_id`` o, en la
reapertura, de la acción nueva (``loop.corrective_action``); el solicitante, de ``requested_by``
del registro ``export_produced`` del evento (``LectorExpediente.get``). También ellas tienen que
tener **algún** rol del cliente con alcance sobre el evento.

**Bajo concesión** (``context.concession_id``, A-69): la resolución usa
``IdentityQueryPort.recipients_under_concession``, la consulta acotada como la de firmantes de
A-58: comprueba el alcance con el contexto de la concesión y lee usuario y rol con el del sistema,
**sin ampliar la RLS** de A-46.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.identity.application.hierarchy import Recipient, ScopedRecipient
from vigia_platform.ledger.application.reader import LedgerRecordView
from vigia_platform.notifications.catalog import CLIENT_ROLES, CatalogEntry, PersonRef
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import OutboxEvent

__all__ = [
    "DeepLink",
    "EventScope",
    "RecipientDirectory",
    "RecordReader",
    "client_recipients",
    "event_scope",
    "provider_recipients",
]

NODE_EVENTS: Final = frozenset({"node_enrolled", "node_revoked", "node_decommissioned"})
CONCESSION_EVENTS: Final = frozenset(
    {"concession_granted", "concession_revoked", "concession_expired"}
)
EXPORT_PRODUCED: Final = "export_produced"

_ZONE_PLANT: Final = text(
    "SELECT plant_id FROM identity.zone WHERE organization_id = :organization_id"
    " AND zone_id = :zone_id"
)
_CONCESSION_SCOPE: Final = text(
    "SELECT scope_level, scope_id FROM identity.provider_concession"
    " WHERE organization_id = :organization_id AND concession_id = :concession_id"
)
_ACTION_ASSIGNEE: Final = text(
    "SELECT assignee_user_id FROM loop.corrective_action"
    " WHERE organization_id = :organization_id AND action_id = :action_id"
)


class RecipientDirectory(Protocol):
    """Lo que el motor usa de ``IdentityQueryPort`` (``identity.application.hierarchy``)."""

    async def users_by_role_and_scope(
        self,
        context: ScopeContext,
        roles: Iterable[Role],
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
    ) -> tuple[Recipient, ...]: ...

    async def recipients_under_concession(
        self,
        context: ScopeContext,
        roles: Iterable[Role],
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
    ) -> tuple[ScopedRecipient, ...]: ...


class RecordReader(Protocol):
    """Lo que el motor usa de ``LectorExpediente`` (el registro del evento)."""

    async def get(
        self,
        context: ScopeContext,
        record_id: uuid.UUID,
        *,
        record_types: tuple[str, ...] | None = None,
    ) -> LedgerRecordView | None: ...


@dataclass(frozen=True, slots=True)
class DeepLink:
    """``deep_link_ref``: a qué lleva el aviso dentro de la aplicación (exige sesión y alcance)."""

    kind: str
    id: uuid.UUID

    def as_json(self) -> dict[str, str]:
        return {"kind": self.kind, "id": str(self.id)}


@dataclass(frozen=True, slots=True)
class EventScope:
    """Dónde ocurrió el evento en la organización que lo publicó."""

    plant_id: uuid.UUID | None
    zone_id: uuid.UUID | None
    """La zona del aviso: solo si el evento es de **una** zona."""
    targets: tuple[tuple[ScopeLevel, uuid.UUID], ...]
    """Los alcances que el destinatario tiene que contener, todos."""


def _uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _uuids(value: object) -> tuple[uuid.UUID, ...] | None:
    if not isinstance(value, list | tuple):
        return None
    parsed = tuple(_uuid(item) for item in value)
    if not parsed or any(item is None for item in parsed):
        return None
    return tuple(item for item in parsed if item is not None)


async def _first(transaction: Transaction, statement: Any, parameters: Mapping[str, Any]) -> Any:
    return (await transaction.execute(statement, parameters)).first()


async def event_scope(event: OutboxEvent, transaction: Transaction) -> EventScope:
    """El alcance del evento (ver el docstring del módulo); lee en la transacción del evento."""
    payload = event.payload
    organization_id = event.organization_id
    plant_id = event.plant_id
    name = event.event_name
    if name in CONCESSION_EVENTS:
        level, scope_id = await _concession_scope(event, transaction)
        if level is ScopeLevel.ORGANIZATION:
            return EventScope(None, None, ((ScopeLevel.ORGANIZATION, organization_id),))
        if level is ScopeLevel.PLANT:
            return EventScope(scope_id, None, ((ScopeLevel.PLANT, scope_id),))
        zone_plant = await _zone_plant(transaction, organization_id, scope_id)
        return EventScope(zone_plant, scope_id, ((ScopeLevel.ZONE, scope_id),))
    if name in NODE_EVENTS:
        plant_id = _uuid(payload.get("plant_id")) or plant_id
        if plant_id is not None:
            return EventScope(plant_id, None, ((ScopeLevel.PLANT, plant_id),))
    zones = _uuids(payload.get("zone_ids")) if name not in NODE_EVENTS else None
    if zones is not None:
        if plant_id is None:
            plant_id = await _zone_plant(transaction, organization_id, zones[0])
        unique = tuple(dict.fromkeys(zones))
        return EventScope(
            plant_id,
            unique[0] if len(unique) == 1 else None,
            tuple((ScopeLevel.ZONE, zone) for zone in unique),
        )
    zone_id = _uuid(payload.get("zone_id"))
    if zone_id is not None:
        if plant_id is None:
            plant_id = await _zone_plant(transaction, organization_id, zone_id)
        return EventScope(plant_id, zone_id, ((ScopeLevel.ZONE, zone_id),))
    if plant_id is not None:
        return EventScope(plant_id, None, ((ScopeLevel.PLANT, plant_id),))
    return EventScope(None, None, ((ScopeLevel.ORGANIZATION, organization_id),))


async def _zone_plant(
    transaction: Transaction, organization_id: uuid.UUID, zone_id: uuid.UUID
) -> uuid.UUID:
    row = await _first(
        transaction, _ZONE_PLANT, {"organization_id": organization_id, "zone_id": zone_id}
    )
    if row is None:
        raise LookupError(f"la zona {zone_id} del evento no existe en su organización")
    return uuid.UUID(str(row.plant_id))


async def _concession_scope(
    event: OutboxEvent, transaction: Transaction
) -> tuple[ScopeLevel, uuid.UUID]:
    payload = event.payload
    level, scope_id = payload.get("scope_level"), _uuid(payload.get("scope_id"))
    if not isinstance(level, str) or scope_id is None:
        concession_id = _uuid(payload.get("concession_id"))
        row = (
            None
            if concession_id is None
            else await _first(
                transaction,
                _CONCESSION_SCOPE,
                {"organization_id": event.organization_id, "concession_id": concession_id},
            )
        )
        if row is None:
            raise LookupError("la concesión del evento no existe en su organización")
        level, scope_id = str(row.scope_level), uuid.UUID(str(row.scope_id))
    return ScopeLevel(level), scope_id


# --- Resolución ---------------------------------------------------------------------------------


async def _holders(
    directory: RecipientDirectory,
    context: ScopeContext,
    roles: Sequence[Role],
    targets: Sequence[tuple[ScopeLevel, uuid.UUID]],
) -> set[uuid.UUID]:
    """Los usuarios con alguno de ``roles`` sobre **todos** ``targets``."""
    if not roles:
        return set()
    held: set[uuid.UUID] | None = None
    for level, scope_id in targets:
        if context.concession_id is not None:
            found: Iterable[
                Recipient | ScopedRecipient
            ] = await directory.recipients_under_concession(context, roles, level, scope_id)
        else:
            found = await directory.users_by_role_and_scope(context, roles, level, scope_id)
        users = {recipient.user_id for recipient in found}
        held = users if held is None else held & users
    return held or set()


async def _person_candidates(
    entry: CatalogEntry,
    event: OutboxEvent,
    transaction: Transaction,
    records: RecordReader,
) -> set[uuid.UUID]:
    payload = event.payload
    found: set[uuid.UUID] = set()
    for person in entry.recipients.persons:
        if person is PersonRef.ASSIGNEE:
            assignee = _uuid(payload.get("assignee_user_id"))
            action_id = _uuid(payload.get("new_action_id"))
            if assignee is None and action_id is not None:
                row = await _first(
                    transaction,
                    _ACTION_ASSIGNEE,
                    {"organization_id": event.organization_id, "action_id": action_id},
                )
                assignee = None if row is None else _uuid(str(row.assignee_user_id))
            if assignee is not None:
                found.add(assignee)
        elif person is PersonRef.REQUESTER:
            record_id = _uuid(payload.get("record_id"))
            if record_id is None:
                continue
            view = await records.get(
                transaction.context, record_id, record_types=(EXPORT_PRODUCED,)
            )
            if view is None:
                raise LookupError(f"el registro {record_id} del evento no se puede leer")
            requested_by = view.document().get("requested_by")
            requester = (
                _uuid(requested_by.get("user_id")) if isinstance(requested_by, Mapping) else None
            )
            if requester is not None:
                found.add(requester)
    return found


async def client_recipients(
    entry: CatalogEntry,
    event: OutboxEvent,
    scope: EventScope,
    transaction: Transaction,
    directory: RecipientDirectory,
    records: RecordReader,
) -> tuple[uuid.UUID, ...]:
    """Los usuarios de la organización del evento que reciben el aviso, en orden de
    ``user_id`` (el orden en que se reclaman sus claves de idempotencia)."""
    context = transaction.context
    rule = entry.recipients
    users = await _holders(directory, context, rule.client, scope.targets)
    if not users and rule.client_fallback:
        users = await _holders(directory, context, rule.client_fallback, scope.targets)
    candidates = await _person_candidates(entry, event, transaction, records)
    if candidates:
        users |= candidates & await _holders(directory, context, CLIENT_ROLES, scope.targets)
    return tuple(sorted(users))


async def provider_recipients(
    entry: CatalogEntry,
    provider_context: ScopeContext,
    directory: RecipientDirectory,
) -> tuple[uuid.UUID, ...]:
    """Los usuarios de la organización proveedora que reciben el aviso (toda la proveedora)."""
    users = await _holders(
        directory,
        provider_context,
        entry.recipients.provider,
        ((ScopeLevel.ORGANIZATION, provider_context.organization_id),),
    )
    return tuple(sorted(users))
