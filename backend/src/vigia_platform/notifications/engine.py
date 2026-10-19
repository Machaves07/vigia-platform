"""Consumidor ``notification_engine`` (TASK-324; LC-LAZ-31; business-logic-model.md de U-04 §7.2,
``procesar_evento``; BR-LAZ-124 a 130 y 132; PAT-LAZ-RES-04 y RES-05; A-69).

Por cada evento del catálogo (``notifications.catalog``), en la transacción que el despachador abre
con el ``ScopeContext`` del evento (BR-LAZ-140):

1. **Idempotencia** por ``event_id`` (``Notification.source_event_id``): si el evento ya dejó un
   aviso en la organización, nada (``delivery.already_delivered``); dos entregas simultáneas
   chocan en ``notification_source_key`` y la segunda reintenta (``ConcurrentDelivery``).
2. **Fila del catálogo** y urgencia (``urgency_for``: las dos urgencias condicionadas).
3. **Destinatarios** (``notifications.recipients``) **antes de escribir nada**: si la identidad no
   responde, la excepción sale, no queda ninguna entrega y el evento reintenta; nunca hay
   destinatario por defecto. Los del cliente en la organización del evento; los del proveedor en
   la proveedora.
4. **Título y cuerpo** de la plantilla (``notifications.templates``), pasados por los dos
   validadores con los preparadores de la organización que recibe el aviso.
5. **Aviso en la aplicación** de inmediato, y el correo según ``delivery.email_plan``.

**Dos organizaciones.** Los avisos de la organización del evento van en la transacción del
despachador (se confirman con ``delivered``). Los del proveedor, cuando el evento es de un cliente,
van en una transacción propia de la proveedora con el contexto del sistema, **antes**: si falla,
la entrega se revierte y reintenta; si se confirma y después falla lo del cliente, el reintento la
encuentra hecha (su propia clave de idempotencia). Los avisos del proveedor no llevan planta ni zona
(son del cliente); el enlace sí apunta al objeto del cliente, que exige sesión y alcance.

**Orden de candados**: ninguno explícito. Las únicas exclusiones son las claves
``notification_source_key``, que se reclaman en orden de ``recipient_user_id`` (dos entregas del
mismo evento no se interbloquean: la segunda espera a la primera en la primera clave).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from sqlalchemy import exc as sa_exc

from vigia_platform.ledger.application.writer import violated_constraint
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry, FreeTextPreparation
from vigia_platform.loop.domain.enums import NotificationAudience, NotificationUrgency
from vigia_platform.notifications.catalog import (
    CatalogEntry,
    catalog_entry,
    subscribed_events,
)
from vigia_platform.notifications.delivery import (
    EmailPlan,
    EmailSenders,
    already_delivered,
    email_plan,
    write_notification,
)
from vigia_platform.notifications.recipients import (
    DeepLink,
    EventScope,
    RecipientDirectory,
    RecordReader,
    client_recipients,
    event_scope,
    provider_recipients,
)
from vigia_platform.notifications.templates import (
    RenderedNotification,
    facts_for,
    render,
    validated,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry

__all__ = [
    "NOTIFICATION_ENGINE",
    "NOTIFICATION_ENGINE_CONCURRENCY",
    "ConcurrentDelivery",
    "EngineContexts",
    "EngineDatabase",
    "NotificationEngine",
    "deep_link",
    "register_notification_engine",
]

NOTIFICATION_ENGINE: Final = "notification_engine"
NOTIFICATION_ENGINE_CONCURRENCY: Final = 4
"""El mamparo del diseño (``loop.worker.bulkheads.DEFAULT_CONCURRENCY``, LC-LAZ-40)."""
SOURCE_KEY_CONSTRAINT: Final = "notification_source_key_pkey"
_UNIQUE_VIOLATION: Final = "23505"

_FINDING_SUBJECT: Final = "finding"
_LINK_FIELDS: Final[Mapping[str, tuple[str, str]]] = {
    "finding_received": ("finding", "finding_id"),
    "detection_for_review_received": ("queue_item", "detection_id"),
    "review_confirmed": ("queue_item", "detection_id"),
    "review_discarded": ("queue_item", "detection_id"),
    "review_overdue": ("queue_item", "detection_id"),
    "unclosable_review_due": ("finding", "subject_id"),
    "unclosable_reviewed": ("finding", "subject_id"),
    "export_available": ("export", "export_id"),
    "monthly_package_available": ("package", "package_id"),
    "commitments_recorded": ("package", "package_id"),
    "node_enrolled": ("node", "node_id"),
    "node_revoked": ("node", "node_id"),
    "node_decommissioned": ("node", "node_id"),
    "target_version_published": ("node", "node_id"),
    "update_result_received": ("node", "node_id"),
    "fleet_alarm_raised": ("node", "node_id"),
    "fleet_alarm_cleared": ("node", "node_id"),
}
"""El objeto al que lleva el aviso cuando la carga lo nombra (si no, la zona o la organización)."""


class ConcurrentDelivery(RuntimeError):
    """Otra entrega del mismo evento reclamó antes la clave de un destinatario.

    La transacción se revierte entera (también su correo) y el despachador reintenta: el
    reintento encuentra el evento ya entregado y termina sin efecto (PR-LAZ-40)."""

    code = "notification_concurrent_delivery"


class EngineContexts(Protocol):
    """Lo que el motor usa de ``ScopeContexts`` (U-02): el contexto del sistema de otra
    organización, como el de un evento."""

    def context_from_event(
        self, event: Any, *, unit: ActorUnit = ActorUnit.U02
    ) -> ScopeContext: ...


class EngineDatabase(Protocol):
    """La transacción propia de la proveedora (``shared.db.Database``)."""

    def transaction(self, context: ScopeContext) -> Any: ...


@dataclass(frozen=True, slots=True)
class _Organization:
    """El «evento» con el que se pide el contexto del sistema de la proveedora."""

    organization_id: uuid.UUID
    correlation_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _Draft:
    """``delivery.NotificationDraft`` de un evento en una organización."""

    entry: CatalogEntry
    source_event_id: uuid.UUID
    urgency: NotificationUrgency
    plant_id: uuid.UUID | None
    zone_id: uuid.UUID | None
    content: RenderedNotification
    deep_link: DeepLink


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def deep_link(event: OutboxEvent) -> DeepLink:
    """``deep_link_ref`` estructurado: un objeto del cliente, nunca una URL (BR-LAZ-128)."""
    payload = event.payload
    subject_kind, subject_id = payload.get("subject_kind"), _uuid(payload.get("subject_id"))
    if subject_id is not None and subject_kind is not None:
        kind = "finding" if subject_kind == _FINDING_SUBJECT else "queue_item"
        return DeepLink(kind, subject_id)
    named = _LINK_FIELDS.get(event.event_name)
    if named is not None:
        found = _uuid(payload.get(named[1]))
        if found is not None:
            return DeepLink(named[0], found)
    zone_id = _uuid(payload.get("zone_id"))
    if zone_id is not None:
        return DeepLink("zone", zone_id)
    return DeepLink("organization", event.organization_id)


@repository
class NotificationEngine:
    """El manejador de ``notification_engine`` (``ConsumerHandler``)."""

    def __init__(
        self,
        *,
        directory: RecipientDirectory,
        records: RecordReader,
        contexts: EngineContexts,
        database: EngineDatabase,
        senders: EmailSenders,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        provider_organization_id: uuid.UUID,
    ) -> None:
        self._directory = directory
        self._records = records
        self._contexts = contexts
        self._database = database
        self._senders = senders
        self._free_text = free_text
        self._clock = clock
        self._provider_organization_id = provider_organization_id

    def __repr__(self) -> str:
        return "NotificationEngine()"

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        await self.process(event, transaction)

    async def process(self, event: OutboxEvent, transaction: Transaction) -> int:
        """Procesa ``event`` y devuelve cuántos avisos creó (0 si ya estaba entregado)."""
        entry = catalog_entry(event.event_name)
        if entry is None:
            raise ValueError(f"evento no suscrito a notification_engine: {event.event_name}")
        context = transaction.context
        if context.organization_id != event.organization_id:
            raise ValueError("la transacción no es de la organización del evento")
        urgency = entry.urgency_for(event.payload)
        content = render(entry.template_key, facts_for(event.event_name, event.payload))
        link = deep_link(event)
        provider_id = self._provider_organization_id
        rule = entry.recipients
        client_side = entry.audience is not NotificationAudience.PROVIDER and bool(
            rule.client or rule.persons
        )
        provider_side = entry.audience is not NotificationAudience.CLIENT and bool(rule.provider)
        separate_provider = provider_side and event.organization_id != provider_id
        # Destinatarios primero (paso 3): sin respuesta de la identidad no se escribe nada.
        local: set[uuid.UUID] = set()
        scope = EventScope(None, None, ())
        if (client_side or provider_side) and await already_delivered(transaction, event.event_id):
            client_side = False
            provider_side = separate_provider
        if client_side:
            scope = await event_scope(event, transaction)
            local |= set(
                await client_recipients(
                    entry, event, scope, transaction, self._directory, self._records
                )
            )
        remote: tuple[uuid.UUID, ...] = ()
        provider_context = context
        if provider_side:
            if separate_provider:
                provider_context = self._contexts.context_from_event(
                    _Organization(provider_id, event.correlation_id), unit=ActorUnit.U04
                )
            found = await provider_recipients(entry, provider_context, self._directory)
            if separate_provider:
                remote = found
            else:
                local |= set(found)
        created = 0
        if remote:
            draft = _Draft(entry, event.event_id, urgency, None, None, content, link)
            created += await self._provider_side(provider_context, draft, remote)
        if local:
            draft = _Draft(
                entry, event.event_id, urgency, scope.plant_id, scope.zone_id, content, link
            )
            plan = email_plan(
                entry, urgency, self._senders, under_concession=context.concession_id is not None
            )
            created += await self._write(transaction, draft, sorted(local), plan)
        return created

    async def _provider_side(
        self, provider_context: ScopeContext, draft: _Draft, users: Sequence[uuid.UUID]
    ) -> int:
        """Los avisos de la proveedora en su propia transacción (contexto del sistema)."""
        async with self._database.transaction(provider_context) as transaction:
            if await already_delivered(transaction, draft.source_event_id):
                return 0
            plan = email_plan(draft.entry, draft.urgency, self._senders, under_concession=False)
            created: int = await self._write(transaction, draft, users, plan)
            return created

    async def _write(
        self,
        transaction: Transaction,
        draft: _Draft,
        users: Sequence[uuid.UUID],
        plan: tuple[EmailPlan, str | None],
    ) -> int:
        organization_id = transaction.context.organization_id
        await self._prepare(transaction)
        checked = _Draft(
            draft.entry,
            draft.source_event_id,
            draft.urgency,
            draft.plant_id,
            draft.zone_id,
            validated(self._free_text, draft.content, organization_id),
            draft.deep_link,
        )
        try:
            for user_id in sorted(users):
                await write_notification(transaction, checked, user_id, plan, self._clock)
        except sa_exc.IntegrityError as error:
            if violated_constraint(error, _UNIQUE_VIOLATION) == SOURCE_KEY_CONSTRAINT:
                raise ConcurrentDelivery(str(draft.source_event_id)) from error
            raise
        return len(users)

    async def _prepare(self, transaction: Transaction) -> None:
        """Los preparadores de los validadores (la lista de vocabulario de la organización que
        recibe el aviso), en su transacción, como hace el escritor (PAT-LAZ-REN-02)."""
        if not self._free_text.has_preparers:
            return

        async def rows(statement: Any, parameters: Mapping[str, Any]) -> Sequence[Any]:
            return (await transaction.execute(statement, parameters)).all()

        await self._free_text.prepare(
            FreeTextPreparation(
                organization_id=transaction.context.organization_id,
                rows=rows,
                clock=self._clock,
            )
        )


def register_notification_engine(
    registry: ConsumerRegistry, engine: NotificationEngine
) -> Consumer:
    """Registra ``notification_engine`` (U-04, sin dependencia externa: el correo lo envía el
    adaptador del ``EmailSenderPort``, que tiene su propio circuito)."""
    return registry.register(
        Consumer(
            consumer_name=NOTIFICATION_ENGINE,
            unit=ActorUnit.U04,
            subscribed_events=subscribed_events(),
            handler=engine,
        )
    )
