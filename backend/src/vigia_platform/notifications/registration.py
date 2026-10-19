"""Registro de ``notifications`` en la raíz de composición (TASK-302; LC-LAZ-37; A-52).

Mismo patrón que ``accreditation.registration``: ``shared.runtime.units`` declara la unidad
``notifications`` con lo que aquí se compone y cada tarea de servicio del módulo (motor,
agrupación, correo, preferencias) **añade una línea** a ``ROUTERS``, ``NOTIFICATIONS_TASKS`` y
``TASK_REGISTRARS``, ``NOTIFICATIONS_CONSUMERS`` y ``CONSUMER_REGISTRARS``, o ``API_STATE``.
El módulo no registra tipos de registro ni publica eventos (``notifications.events``). La
comprobación de arranque de U-04 es ``loop.registration.missing_u04_registrations``.

``EMAIL_SENDERS`` es la ranura del ``EmailSenderPort`` (``EmailSenderRegistry`` de U-02) que lee
el motor: sin puerto registrado, todo correo inmediato queda ``degraded_app_only``. El adaptador
SES de TASK-325 se registra en ella desde aquí.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from fastapi import APIRouter

from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.application.invitations import EmailSenderRegistry
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.loop.domain.enums import NOTIFICATIONS_LABEL_BINDINGS
from vigia_platform.loop.http.client_factory import DEFAULT_REGION, HttpClientFactory
from vigia_platform.loop.worker.bulkheads import bulkheaded_registry
from vigia_platform.notifications.adapters.http.routes import notifications_router
from vigia_platform.notifications.adapters.http.services import (
    NOTIFICATIONS_STATE_KEY,
    NotificationsHttp,
)
from vigia_platform.notifications.adapters.ses.delivery_events import (
    EMAIL_DELIVERY_EVENTS,
    EMAIL_DELIVERY_EVENTS_POLL,
    POLL_SCHEDULE,
    RECEIVE_WAIT_SECONDS,
    EmailDeliveryEventsConsumer,
    EmailDeliveryEventsPollTrigger,
    register_email_delivery_events,
    register_email_delivery_events_poll,
)
from vigia_platform.notifications.adapters.ses.fake import FakeEmailSender
from vigia_platform.notifications.adapters.ses.sender import SesEmailSender
from vigia_platform.notifications.detail_codes import (
    NOTIFICATIONS_DETAIL_CODE_LABEL_BINDINGS,
    NotificationsDetailCode,
)
from vigia_platform.notifications.engine import (
    NOTIFICATION_ENGINE,
    NOTIFICATION_ENGINE_CONCURRENCY,
    NotificationEngine,
    register_notification_engine,
)
from vigia_platform.notifications.events import register_notifications_event_types
from vigia_platform.notifications.reader import NotificationReader
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    EventTypeRegistry,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

if TYPE_CHECKING:
    from vigia_platform.shared.runtime.units import UnitServices

_PILOT_ENVIRONMENT: Final = "pilot"
_SQS_CONNECT_TIMEOUT_SECONDS: Final = 5
_SQS_READ_TIMEOUT_SECONDS: Final = RECEIVE_WAIT_SECONDS + 10

__all__ = [
    "API_STATE",
    "CONSUMER_REGISTRARS",
    "EMAIL_SENDERS",
    "NOTIFICATIONS_CONSUMERS",
    "NOTIFICATIONS_DETAIL_CODES",
    "NOTIFICATIONS_LABELS",
    "NOTIFICATIONS_TASKS",
    "ROUTERS",
    "TASK_REGISTRARS",
    "notifications_api_state",
    "notifications_consumers",
    "notifications_event_types",
    "notifications_routers",
    "notifications_tasks",
]

type TaskRegistrar = Callable[[PeriodicTaskRegistry, UnitServices], None]
type ConsumerRegistrar = Callable[[ConsumerRegistry, UnitServices], None]
type StateBuilder = Callable[[UnitServices], Mapping[str, object]]

ROUTERS: Final[tuple[Callable[[], APIRouter], ...]] = (
    notifications_router,  # SUP-U05-33, forma mínima (TASK-324)
)
"""Fábricas de los enrutadores de ``/notifications/*``."""

EMAIL_SENDERS: Final = EmailSenderRegistry()
"""La ranura del ``EmailSenderPort`` del proceso (a lo sumo uno; TASK-325 registra SES)."""


def _register_notification_engine(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``notification_engine`` (TASK-324) con ``IdentityQueryPort`` de U-02 (la consulta acotada
    bajo concesión con el contexto del sistema, A-69) y ``LectorExpediente``."""
    directory = HierarchyService(
        IdentityDependencies(
            database=services.database,
            writer=services.writer,
            audit=services.audit,
            outbox=services.outbox,
            authorizer=services.authorizer,
            free_text=services.free_text,
            clock=services.clock,
            provider_organization_id=services.provider_organization_id,
        ),
        lookups=services.contexts,
    )
    register_notification_engine(
        registry,
        NotificationEngine(
            directory=directory,
            records=LectorExpediente(
                database=services.database,
                audit=services.audit,
                clock=services.clock,
                metrics=services.metrics,
            ),
            contexts=services.contexts,
            database=services.database,
            senders=EMAIL_SENDERS,
            free_text=services.free_text,
            clock=services.clock,
            provider_organization_id=services.provider_organization_id,
        ),
    )


def _register_email_sender(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``SesEmailSender`` solo en ``pilot`` con el dominio verificado configurado; en cualquier
    otro entorno (o sin configurarlo), ``FakeEmailSender`` (TASK-325; LC-LAZ-33). Idempotente:
    ``EMAIL_SENDERS`` es la ranura de todo el proceso (``api.py`` y ``admin.py`` la leen también),
    así que una segunda composición en el mismo proceso no vuelve a registrar."""
    del registry
    if EMAIL_SENDERS.sender is not None:
        return
    config = services.config
    if (
        config is not None
        and config.environment == _PILOT_ENVIRONMENT
        and config.email_sender_domain is not None
    ):
        EMAIL_SENDERS.register(
            SesEmailSender(
                database=services.database,
                http_clients=HttpClientFactory.from_config(config),
                region_name=config.aws_region,
                sender_email=f"no-responder@mail.{config.email_sender_domain}",
                configuration_set_name=f"vigia-email-{config.environment}",
                clock=services.clock,
            )
        )
        return
    EMAIL_SENDERS.register(FakeEmailSender(clock=services.clock))


def _email_events_sqs_client(services: UnitServices) -> tuple[Any, str | None]:
    config = services.config
    region_name = config.aws_region if config is not None else DEFAULT_REGION
    endpoint_url = config.aws_endpoint_url if config is not None else None
    queue_url = config.email_events_queue_url if config is not None else None
    client = boto3.client(
        "sqs",
        region_name=region_name,
        endpoint_url=endpoint_url,
        config=Config(
            connect_timeout=_SQS_CONNECT_TIMEOUT_SECONDS,
            read_timeout=_SQS_READ_TIMEOUT_SECONDS,
            retries={"max_attempts": 1},
        ),
    )
    return client, queue_url


def _register_email_delivery_events(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``email_delivery_events`` (TASK-325, LC-LAZ-34): sin ``VIGIA_EMAIL_EVENTS_QUEUE_URL`` (fuera
    de ``pilot``, o mientras U-04 esté en pausa), queda registrado pero no sondea nada."""
    sqs, queue_url = _email_events_sqs_client(services)
    register_email_delivery_events(
        registry,
        EmailDeliveryEventsConsumer(
            sqs=sqs,
            queue_url=queue_url,
            database=services.database,
            provider_context=services.contexts.provider_audit_context(),
            contexts=services.contexts,
            clock=services.clock,
        ),
    )


def _register_email_delivery_events_poll(
    registry: PeriodicTaskRegistry, services: UnitServices
) -> None:
    """La tarea ``global`` que dispara el pulso de ``email_delivery_events`` (TASK-325)."""
    register_email_delivery_events_poll(registry, EmailDeliveryEventsPollTrigger(services.outbox))


NOTIFICATIONS_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {
    EMAIL_DELIVERY_EVENTS_POLL: (POLL_SCHEDULE, TaskIteration.GLOBAL),  # TASK-325, D-7
}
"""Tareas periódicas del módulo: nombre → (cadencia, iteración), todas con ``unit = U-04``."""
TASK_REGISTRARS: Final[tuple[TaskRegistrar, ...]] = (
    _register_email_delivery_events_poll,  # TASK-325
)

NOTIFICATIONS_CONSUMERS: Final[Mapping[str, int]] = {
    NOTIFICATION_ENGINE: NOTIFICATION_ENGINE_CONCURRENCY,  # TASK-324
    EMAIL_DELIVERY_EVENTS: 2,  # TASK-325 (LC-LAZ-40)
}
"""Consumidores del módulo: nombre → concurrencia de su mamparo (LC-LAZ-40)."""
CONSUMER_REGISTRARS: Final[tuple[ConsumerRegistrar, ...]] = (
    _register_notification_engine,  # notification_engine (TASK-324)
    _register_email_sender,  # EMAIL_SENDERS (TASK-325); no registra ningún Consumer
    _register_email_delivery_events,  # email_delivery_events (TASK-325)
)


def _notifications_state(services: UnitServices) -> Mapping[str, object]:
    return {
        NOTIFICATIONS_STATE_KEY: NotificationsHttp(
            reader=NotificationReader(
                database=services.database,
                authorizer=services.authorizer,
                provider_organization_id=services.provider_organization_id,
            )
        )
    }


API_STATE: Final[tuple[StateBuilder, ...]] = (
    _notifications_state,  # NotificationsHttp (TASK-324)
)

NOTIFICATIONS_LABELS: Final[Mapping[str, type[enum.Enum]]] = {
    **NOTIFICATIONS_LABEL_BINDINGS,
    **NOTIFICATIONS_DETAIL_CODE_LABEL_BINDINGS,
}
"""Enumeraciones con etiqueta de la unidad (``PlatformUnit.labels``)."""

NOTIFICATIONS_DETAIL_CODES: Final[tuple[str, ...]] = tuple(
    code.value for code in NotificationsDetailCode
)


def notifications_routers() -> tuple[APIRouter, ...]:
    return tuple(build() for build in ROUTERS)


def notifications_event_types(registry: EventTypeRegistry) -> None:
    register_notifications_event_types(registry)


def notifications_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    for register in TASK_REGISTRARS:
        register(registry, services)


def notifications_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    # Cada consumidor sale con su mamparo (LC-LAZ-40); el registrador no lo conoce.
    bulkheaded = bulkheaded_registry(registry, services)
    for register in CONSUMER_REGISTRARS:
        register(bulkheaded, services)


def notifications_api_state(services: UnitServices) -> Mapping[str, object]:
    state: dict[str, object] = {}
    for build in API_STATE:
        for key, value in build(services).items():
            if key in state:
                raise ValueError(f"la clave de app.state «{key}» está dos veces en notifications")
            state[key] = value
    return state
