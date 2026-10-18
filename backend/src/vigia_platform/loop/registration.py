"""Registro de U-04 en la raíz de composición (TASK-302; LC-LAZ-37; NFR-LAZ-14; A-52).

``shared.runtime.units`` declara las tres unidades de U-04 (``loop``, ``accreditation`` y
``notifications``) y las hace delegar en el ``registration.py`` de cada paquete; **ninguna tarea
posterior de U-04 edita ``units.py``**. Cada tarea de servicio **añade una línea** al
``registration.py`` de su módulo: aquí, a ``ROUTERS``, ``LOOP_TASKS`` y ``TASK_REGISTRARS``,
``LOOP_CONSUMERS`` y ``CONSUMER_REGISTRARS``, ``FREE_TEXT_REGISTRARS`` (los dos validadores de
TASK-303 y TASK-340) o ``API_STATE``.

**Comprobación de arranque con fallo cerrado** (``missing_u04_registrations``, NFR-LAZ-14,
PAT-LAZ-RES-07): la composición contrasta lo registrado por **todas** las unidades con los
manifiestos de U-04, escritos **a mano** (no derivados de los registros):

- ``U04_RECORD_TYPES``: los 17 tipos de §5.1 (13 del lazo y 4 de la acreditación);
- ``U04_EVENT_TYPES``: los 25 eventos de §11.3 (nota R14 de §7: 25, no 20; 21 del lazo y 4 de
  la acreditación; ``user_invited`` no es de U-04, U04-H-13);
- ``U04_TASKS``: las tareas periódicas con su cadencia e iteración, todas con ``unit = U-04``;
- ``U04_CONSUMERS``: los consumidores con su concurrencia, todos con ``unit = U-04``.

Si falta uno, o una tarea cambió de cadencia, unidad o iteración, ``vigia-api``,
``vigia-worker`` y ``vigia-admin`` no arrancan y la salida nombra lo que falta
(``RegistrationIncomplete``). Las enumeraciones con etiqueta las comprueba el arranque de
``vigia-api`` contra ``labels.loop.es.json`` (``PlatformUnit.labels``, NFR-LAZ-52).
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter

from vigia_platform.accreditation.registration import (
    ACCREDITATION_CONSUMERS,
    ACCREDITATION_TASKS,
)
from vigia_platform.loop.adapters.attachments.repository import PostgresAttachmentRepository
from vigia_platform.loop.adapters.http.attachment_routes import (
    ATTACHMENTS_STATE_KEY,
    attachment_router,
)
from vigia_platform.loop.application.attachments import build_attachment_service
from vigia_platform.loop.detail_codes import LOOP_DETAIL_CODE_LABEL_BINDINGS, LoopDetailCode
from vigia_platform.loop.domain.enums import LOOP_LABEL_BINDINGS
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.loop.record_types import register_loop_record_types
from vigia_platform.notifications.registration import (
    NOTIFICATIONS_CONSUMERS,
    NOTIFICATIONS_TASKS,
)
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.registries import (
    ConsumerRegistry,
    EventTypeRegistry,
    OutboxCatalog,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)

if TYPE_CHECKING:
    from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
    from vigia_platform.ledger.registry import RecordTypeRegistry
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "API_STATE",
    "CONSUMER_REGISTRARS",
    "FREE_TEXT_REGISTRARS",
    "LOOP_CONSUMERS",
    "LOOP_DETAIL_CODES",
    "LOOP_LABELS",
    "LOOP_TASKS",
    "ROUTERS",
    "TASK_REGISTRARS",
    "U04_CONSUMERS",
    "U04_EVENT_TYPES",
    "U04_RECORD_TYPES",
    "U04_TASKS",
    "loop_api_state",
    "loop_consumers",
    "loop_event_types",
    "loop_free_text",
    "loop_record_types",
    "loop_routers",
    "loop_tasks",
    "missing_u04_registrations",
]

U04_RECORD_TYPES: Final = frozenset(
    {
        # loop (13): los once reservados por U-02 que son del lazo y los seis propuestos
        "classification",
        "review_resolution",
        "action_assigned",
        "action_evidence_added",
        "closure",
        "unclosable_state_declared",
        "root_cause_analysis",
        "vocabulary_list_changed",
        "action_overdue",
        "unclosable_review",
        "reopening",
        "loop_configuration_changed",
        "late_segment_attached",
        # accreditation (4)
        "export_produced",
        "monthly_package_produced",
        "sample_audit_result",
        "commitments_recorded",
    }
)
"""Los 17 tipos de registro de U-04 (domain-entities §5.1)."""

U04_EVENT_TYPES: Final = frozenset(
    {
        # loop (21)
        "finding_confirmed",
        "finding_discarded",
        "finding_reclassified",
        "loop_suspended",
        "loop_resumed",
        "review_confirmed",
        "review_discarded",
        "review_overdue",
        "action_assigned",
        "action_evidence_added",
        "finding_closed",
        "finding_unclosable",
        "unclosable_review_due",
        "unclosable_reviewed",
        "finding_reopened",
        "finding_overdue",
        "finding_escalated",
        "root_cause_analysis_signed",
        "vocabulary_list_changed",
        "late_segment_attached",
        "loop_projection_divergence",
        # accreditation (4)
        "export_available",
        "monthly_package_available",
        "commitments_recorded",
        "adoption_low",
    }
)
"""Los 25 eventos de U-04 (domain-entities §11.3 y nota R14 de §7)."""

type TaskRegistrar = Callable[[PeriodicTaskRegistry, UnitServices], None]
type ConsumerRegistrar = Callable[[ConsumerRegistry, UnitServices], None]
type FreeTextRegistrar = Callable[[FreeTextPolicyRegistry], None]
type StateBuilder = Callable[[UnitServices], Mapping[str, object]]

ROUTERS: Final[tuple[Callable[[], APIRouter], ...]] = (attachment_router,)
"""Fábricas de los enrutadores de ``/loop/*``: hoy, los adjuntos de cierre (SUP-U05-09 y 10)."""

LOOP_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {}
"""Tareas periódicas del lazo: nombre → (cadencia, iteración), todas con ``unit = U-04``."""
TASK_REGISTRARS: Final[tuple[TaskRegistrar, ...]] = ()

LOOP_CONSUMERS: Final[Mapping[str, int]] = {}
"""Consumidores del lazo: nombre → concurrencia de su mamparo (LC-LAZ-40)."""
CONSUMER_REGISTRARS: Final[tuple[ConsumerRegistrar, ...]] = ()

FREE_TEXT_REGISTRARS: Final[tuple[FreeTextRegistrar, ...]] = ()
"""Los validadores de texto libre de U-04 (``blocked_vocabulary``, ``person_attribution``)."""


def _attachments_state(services: UnitServices) -> Mapping[str, object]:
    """``ClosureAttachmentPort`` sobre ``vigia-evidence`` (prefijo ``closure/``, LC-LAZ-10)."""
    return {
        ATTACHMENTS_STATE_KEY: build_attachment_service(
            repository=PostgresAttachmentRepository(services.database),
            storage=services.require_evidence(),
            authorizer=services.authorizer,
            audit=services.audit,
            clock=services.clock,
        )
    }


API_STATE: Final[tuple[StateBuilder, ...]] = (_attachments_state,)

U04_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {
    **LOOP_TASKS,
    **ACCREDITATION_TASKS,
    **NOTIFICATIONS_TASKS,
}
"""Todas las tareas periódicas de U-04 (hoy ninguna; TASK-326 verifica las diez)."""

U04_CONSUMERS: Final[Mapping[str, int]] = {
    **LOOP_CONSUMERS,
    **ACCREDITATION_CONSUMERS,
    **NOTIFICATIONS_CONSUMERS,
}
"""Todos los consumidores de U-04 con su concurrencia (hoy ninguno)."""

LOOP_LABELS: Final[Mapping[str, type[enum.Enum]]] = {
    **LOOP_LABEL_BINDINGS,
    **LOOP_DETAIL_CODE_LABEL_BINDINGS,
}
"""Enumeraciones con etiqueta de la unidad (``PlatformUnit.labels``)."""

LOOP_DETAIL_CODES: Final[tuple[str, ...]] = tuple(code.value for code in LoopDetailCode)


def missing_u04_registrations(
    record_types: RecordTypeRegistry, catalog: OutboxCatalog
) -> tuple[tuple[str, str], ...]:
    """Lo que falta de U-04 en los registros ya compuestos, como pares (clase, nombre).

    Un tipo o un evento de U-04 registrado por otra unidad cuenta como ausente, igual que una
    tarea con otra cadencia, otra unidad u otra iteración, o un consumidor de otra unidad.
    """
    present_types = {
        compiled.record_type
        for compiled in record_types.latest()
        if compiled.writer_unit is ActorUnit.U04
    }
    missing = [("record_type", name) for name in sorted(U04_RECORD_TYPES - present_types)]
    present_events = {
        compiled.event_name
        for compiled in catalog.event_types.compiled_types()
        if compiled.definition.publisher_unit is ActorUnit.U04
    }
    missing += [("event_type", name) for name in sorted(U04_EVENT_TYPES - present_events)]
    for name, (schedule, iteration) in sorted(U04_TASKS.items()):
        task = catalog.periodic_tasks.get(name)
        if (
            task is None
            or task.unit is not ActorUnit.U04
            or task.schedule != schedule
            or task.iteration is not iteration
        ):
            missing.append(("periodic_task", name))
    for name in sorted(U04_CONSUMERS):
        consumer = catalog.consumers.get(name)
        if consumer is None or consumer.unit is not ActorUnit.U04:
            missing.append(("consumer", name))
    return tuple(missing)


def loop_routers() -> tuple[APIRouter, ...]:
    return tuple(build() for build in ROUTERS)


def loop_record_types(registry: RecordTypeRegistry) -> None:
    register_loop_record_types(registry)


def loop_event_types(registry: EventTypeRegistry) -> None:
    register_loop_event_types(registry)


def loop_free_text(registry: FreeTextPolicyRegistry) -> None:
    for register in FREE_TEXT_REGISTRARS:
        register(registry)


def loop_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    for register in TASK_REGISTRARS:
        register(registry, services)


def loop_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    for register in CONSUMER_REGISTRARS:
        register(registry, services)


def loop_api_state(services: UnitServices) -> Mapping[str, object]:
    state: dict[str, object] = {}
    for build in API_STATE:
        for key, value in build(services).items():
            if key in state:
                raise ValueError(f"la clave de app.state «{key}» está dos veces en loop")
            state[key] = value
    return state
