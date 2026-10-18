"""Registro de ``accreditation`` en la raíz de composición (TASK-302; LC-LAZ-37; A-52).

``shared.runtime.units`` declara la unidad ``accreditation`` con lo que aquí se compone; ninguna
tarea posterior de U-04 vuelve a editar ``units.py``. Cada tarea de servicio del módulo **añade
una línea** a los manifiestos y a las tuplas de este archivo:

- ``ROUTERS``: fábricas de los enrutadores del módulo (``/accreditation/*``, LC-LAZ-41);
- ``ACCREDITATION_TASKS`` (nombre → cadencia e iteración, escrito **a mano**) y
  ``TASK_REGISTRARS`` (la función ``register_*`` que la registra con su manejador);
- ``ACCREDITATION_CONSUMERS`` (nombre → concurrencia del mamparo, LC-LAZ-40) y
  ``CONSUMER_REGISTRARS``;
- ``API_STATE``: los servicios que las rutas del módulo dejan en ``app.state``.

Los tipos de registro y los eventos ya están completos (``accreditation.record_types`` y
``accreditation.events``). La comprobación de arranque de las tres unidades de U-04 es
``loop.registration.missing_u04_registrations``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter

from vigia_platform.accreditation.detail_codes import (
    ACCREDITATION_DETAIL_CODE_LABEL_BINDINGS,
    AccreditationDetailCode,
)
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.record_types import register_accreditation_record_types
from vigia_platform.loop.domain.enums import ACCREDITATION_LABEL_BINDINGS
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    EventTypeRegistry,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

if TYPE_CHECKING:
    from vigia_platform.ledger.registry import RecordTypeRegistry
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "ACCREDITATION_CONSUMERS",
    "ACCREDITATION_DETAIL_CODES",
    "ACCREDITATION_LABELS",
    "ACCREDITATION_TASKS",
    "API_STATE",
    "CONSUMER_REGISTRARS",
    "ROUTERS",
    "TASK_REGISTRARS",
    "accreditation_api_state",
    "accreditation_consumers",
    "accreditation_event_types",
    "accreditation_record_types",
    "accreditation_routers",
    "accreditation_tasks",
]

type TaskRegistrar = Callable[[PeriodicTaskRegistry, UnitServices], None]
type ConsumerRegistrar = Callable[[ConsumerRegistry, UnitServices], None]
type StateBuilder = Callable[[UnitServices], Mapping[str, object]]

ROUTERS: Final[tuple[Callable[[], APIRouter], ...]] = ()
"""Fábricas de los enrutadores de ``/accreditation/*``; hoy ninguna ruta."""

ACCREDITATION_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {}
"""Tareas periódicas del módulo: nombre → (cadencia, iteración), todas con ``unit = U-04``."""
TASK_REGISTRARS: Final[tuple[TaskRegistrar, ...]] = ()

ACCREDITATION_CONSUMERS: Final[Mapping[str, int]] = {}
"""Consumidores del módulo: nombre → concurrencia de su mamparo (LC-LAZ-40)."""
CONSUMER_REGISTRARS: Final[tuple[ConsumerRegistrar, ...]] = ()

API_STATE: Final[tuple[StateBuilder, ...]] = ()

ACCREDITATION_LABELS: Final[Mapping[str, type[enum.Enum]]] = {
    **ACCREDITATION_LABEL_BINDINGS,
    **ACCREDITATION_DETAIL_CODE_LABEL_BINDINGS,
}
"""Enumeraciones con etiqueta de la unidad (``PlatformUnit.labels``)."""

ACCREDITATION_DETAIL_CODES: Final[tuple[str, ...]] = tuple(
    code.value for code in AccreditationDetailCode
)


def accreditation_routers() -> tuple[APIRouter, ...]:
    return tuple(build() for build in ROUTERS)


def accreditation_record_types(registry: RecordTypeRegistry) -> None:
    register_accreditation_record_types(registry)


def accreditation_event_types(registry: EventTypeRegistry) -> None:
    register_accreditation_event_types(registry)


def accreditation_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    for register in TASK_REGISTRARS:
        register(registry, services)


def accreditation_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    for register in CONSUMER_REGISTRARS:
        register(registry, services)


def accreditation_api_state(services: UnitServices) -> Mapping[str, object]:
    state: dict[str, object] = {}
    for build in API_STATE:
        for key, value in build(services).items():
            if key in state:
                raise ValueError(f"la clave de app.state «{key}» está dos veces en accreditation")
            state[key] = value
    return state
