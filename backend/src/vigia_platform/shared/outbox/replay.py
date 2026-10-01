"""Reproceso de la cola muerta (LC-NUC-23; BR-NUC-82; ``business-logic-model.md`` §8).

``DeadLetterReplay.replay(context, event_id, consumer)``:

1. ``authorize(context, platform.dead_letter.replay, …)``: solo ``platform_operator`` de la
   organización proveedora, en una orden administrativa (``context_from_operator``). Si no
   concede, se audita ``authorization_denied`` y sale ``ResourceNotFound`` (BR-NUC-09).
2. En **una** transacción con el contexto del operador: ``shared.vigia_outbox_replay`` devuelve
   la entrega de ``(event_id, consumer)`` de ``dead_letter`` a ``pending`` con ``attempts = 0`` y
   vencida ya (la función solo actúa con un actor operador), y se audita ``dead_letter_replayed``
   en la cadena de la proveedora con el evento como recurso y el consumidor y la organización
   del evento como filtros. Si no hay una entrega en cola muerta con ese par, no cambia nada y
   sale ``ResourceNotFound``.

El manejador recibe el **mismo** ``event_id`` (los consumidores son idempotentes, BR-NUC-76).
La cola muerta no se borra ni se reescribe (solo anexar): la fila queda como constancia, y si la
entrega vuelve a agotar sus intentos se anexa otra con su ``failed_at``.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy import text

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import REGISTRY_NAME

__all__ = ["DeadLetterReplay", "ReplayReceipt"]

_REPLAY: Final = text(
    "SELECT shared.vigia_outbox_replay(:event_id, :consumer, :replay_at) AS organization_id"
)
_RESOURCE_KIND: Final = "outbox_event"


class TransactionSource(Protocol):
    """``shared.db.Database``."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True)
class ReplayReceipt:
    """La entrega que volvió a ``pending``."""

    event_id: uuid.UUID
    consumer_name: str
    organization_id: uuid.UUID


@repository
class DeadLetterReplay:
    """``OutboxPort.replay`` (``business-logic-model.md`` §10.1)."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        authorizer: Authorizer,
        audit: AuditWriter,
        clock: Clock,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._clock = clock

    async def replay(
        self, context: ScopeContext, event_id: uuid.UUID, consumer_name: str
    ) -> ReplayReceipt:
        """Reentrega ``event_id`` a ``consumer_name`` desde la cola muerta (solo el operador)."""
        if type(event_id) is not uuid.UUID:
            raise TypeError("event_id debe ser uuid.UUID")
        if not isinstance(consumer_name, str) or not REGISTRY_NAME.fullmatch(consumer_name):
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context,
            PermissionKey.PLATFORM_DEAD_LETTER_REPLAY,
            Resource(self._audit.provider_organization_id, _RESOURCE_KIND, event_id),
        )
        async with self._database.transaction(authorized) as transaction:
            organization = (
                await transaction.execute(
                    _REPLAY,
                    {
                        "event_id": event_id,
                        "consumer": consumer_name,
                        "replay_at": self._clock.now(),
                    },
                )
            ).scalar_one()
            if organization is None:
                raise ResourceNotFound()
            organization_id = uuid.UUID(str(organization))
            await self._audit.append(
                authorized,
                AuditOperation.DEAD_LETTER_REPLAYED,
                resource=ResourceRef(_RESOURCE_KIND, event_id),
                filters={
                    "consumer_name": consumer_name,
                    "organization_id": str(organization_id),
                },
                transaction=transaction,
            )
        return ReplayReceipt(event_id, consumer_name, organization_id)
