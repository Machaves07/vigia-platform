"""Registro de ``notifications`` en la raíz de composición (TASK-302; LC-LAZ-37; A-52).

Mismo patrón que ``accreditation.registration``: ``shared.runtime.units`` declara la unidad
``notifications`` con lo que aquí se compone y cada tarea de servicio del módulo (motor,
agrupación, correo, preferencias) **añade una línea** a ``ROUTERS``, ``NOTIFICATIONS_TASKS`` y
``TASK_REGISTRARS``, ``NOTIFICATIONS_CONSUMERS`` y ``CONSUMER_REGISTRARS``, o ``API_STATE``.
El módulo no registra tipos de registro ni publica eventos (``notifications.events``). La
comprobación de arranque de U-04 es ``loop.registration.missing_u04_registrations``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter

from vigia_platform.loop.domain.enums import NOTIFICATIONS_LABEL_BINDINGS
from vigia_platform.loop.worker.bulkheads import bulkheaded_registry
from vigia_platform.notifications.detail_codes import (
    NOTIFICATIONS_DETAIL_CODE_LABEL_BINDINGS,
    NotificationsDetailCode,
)
from vigia_platform.notifications.events import register_notifications_event_types
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    EventTypeRegistry,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

if TYPE_CHECKING:
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "API_STATE",
    "CONSUMER_REGISTRARS",
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

ROUTERS: Final[tuple[Callable[[], APIRouter], ...]] = ()
"""Fábricas de los enrutadores de ``/notifications/*``; hoy ninguna ruta."""

NOTIFICATIONS_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {}
"""Tareas periódicas del módulo: nombre → (cadencia, iteración), todas con ``unit = U-04``."""
TASK_REGISTRARS: Final[tuple[TaskRegistrar, ...]] = ()

NOTIFICATIONS_CONSUMERS: Final[Mapping[str, int]] = {}
"""Consumidores del módulo: nombre → concurrencia de su mamparo (LC-LAZ-40)."""
CONSUMER_REGISTRARS: Final[tuple[ConsumerRegistrar, ...]] = ()

API_STATE: Final[tuple[StateBuilder, ...]] = ()

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
