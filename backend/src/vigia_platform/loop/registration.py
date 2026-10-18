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
  la acreditación; ``user_invited`` no es de U-04, U04-H-13) y el trabajo interno
  ``export_requested`` del consumidor ``export_worker`` (TASK-317);
- ``U04_TASKS``: las tareas periódicas con su cadencia e iteración, todas con ``unit = U-04``;
- ``U04_CONSUMERS``: los consumidores con su concurrencia, todos con ``unit = U-04``;
- ``U04_FREE_TEXT_VALIDATORS``: los validadores de texto libre (BLM §9: sin ellos U-04 **no
  arranca**). Los mira ``loop_tasks`` sobre la política ya sellada de ``UnitServices``, porque
  ``required`` solo recibe los tipos y el catálogo (``units.py`` no se edita).

Si falta uno, o una tarea cambió de cadencia, unidad o iteración, ``vigia-api``,
``vigia-worker`` y ``vigia-admin`` no arrancan y la salida nombra lo que falta
(``RegistrationIncomplete``). Las enumeraciones con etiqueta las comprueba el arranque de
``vigia-api`` contra ``labels.loop.es.json`` (``PlatformUnit.labels``, NFR-LAZ-52).

**Transversales** (TASK-349), que las tareas de servicio usan sin construirlos por su cuenta:

- ``loop_consumers`` (y los de ``accreditation`` y ``notifications``) entregan a sus registradores
  ``bulkheaded_registry``: cada consumidor de U-04 sale con su mamparo (LC-LAZ-40), que el
  despachador comprueba antes del bloqueo de la partición;
- ``loop_http_client_factory(services)``: la fábrica única de clientes de salida con los dos
  destinos de la configuración (LC-LAZ-39);
- ``loop_metrics()``: los instrumentos de ``loop.observability.metrics`` (LC-LAZ-38).
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter

from vigia_platform.accreditation.registration import (
    ACCREDITATION_CONSUMERS,
    ACCREDITATION_TASKS,
)
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.loop.adapters.attachments.repository import PostgresAttachmentRepository
from vigia_platform.loop.adapters.http.attachment_routes import (
    ATTACHMENTS_STATE_KEY,
    attachment_router,
)
from vigia_platform.loop.adapters.http.classification_routes import classification_router
from vigia_platform.loop.adapters.http.configuration_routes import configuration_router
from vigia_platform.loop.adapters.http.services import loop_http_state
from vigia_platform.loop.adapters.postgres.vocabulary import PostgresVocabularyStore
from vigia_platform.loop.application.attachments import build_attachment_service
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LOOP_DETAIL_CODE_LABEL_BINDINGS, LoopDetailCode
from vigia_platform.loop.domain.enums import LOOP_LABEL_BINDINGS
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.loop.http.client_factory import HttpClientFactory
from vigia_platform.loop.observability.metrics import LoopMetrics, get_loop_metrics
from vigia_platform.loop.projection.consumer import (
    LOOP_PROJECTION,
    LOOP_PROJECTION_CONCURRENCY,
    LoopProjectionConsumer,
    register_loop_projection,
)
from vigia_platform.loop.projection.enrichment import ProjectionEnrichment
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.loop.record_types import register_loop_record_types
from vigia_platform.loop.validators.cache import VocabularyCache
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY,
    BlockedVocabularyValidator,
    initial_vocabulary,
)
from vigia_platform.loop.worker.bulkheads import bulkheaded_registry
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
from vigia_platform.shared.runtime.config import RegistrationIncomplete
from vigia_platform.shared.storage import PRESIGN_GET_MAX_TTL, ObjectHead, PresignedRequest

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
    "U04_FREE_TEXT_VALIDATORS",
    "U04_RECORD_TYPES",
    "U04_TASKS",
    "loop_api_state",
    "loop_consumers",
    "loop_event_types",
    "loop_free_text",
    "loop_http_client_factory",
    "loop_metrics",
    "loop_record_types",
    "loop_routers",
    "loop_tasks",
    "missing_free_text_validators",
    "missing_u04_registrations",
    "register_blocked_vocabulary",
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
        # interno (TASK-317): el trabajo del consumidor export_worker, fuera de §11.3
        "export_requested",
    }
)
"""Los 25 eventos de U-04 (domain-entities §11.3 y nota R14 de §7) y el trabajo interno
``export_requested`` (TASK-317)."""

type TaskRegistrar = Callable[[PeriodicTaskRegistry, UnitServices], None]
type ConsumerRegistrar = Callable[[ConsumerRegistry, UnitServices], None]
type FreeTextRegistrar = Callable[[FreeTextPolicyRegistry], None]
type StateBuilder = Callable[[UnitServices], Mapping[str, object]]

ROUTERS: Final[tuple[Callable[[], APIRouter], ...]] = (
    attachment_router,  # SUP-U05-09 y 10 (TASK-310)
    configuration_router,  # SUP-U05-38 y 39 (TASK-304)
    classification_router,  # SUP-U05-03 (TASK-308)
)
"""Fábricas de los enrutadores de ``/loop/*``."""

LOOP_TASKS: Final[Mapping[str, tuple[Schedule, TaskIteration]]] = {}
"""Tareas periódicas del lazo: nombre → (cadencia, iteración), todas con ``unit = U-04``."""
TASK_REGISTRARS: Final[tuple[TaskRegistrar, ...]] = ()


class _NoEvidenceReads:
    """``vigia-worker`` sin depósito de evidencias: ``loop_projection`` solo usa
    ``resultados_marca``, que lee la base; una lectura de clip aquí es un defecto."""

    async def head_object(self, key: str) -> ObjectHead | None:
        raise RuntimeError("loop_projection no lee clips")

    async def presign_get(
        self, key: str, ttl: timedelta = PRESIGN_GET_MAX_TTL, *, version_id: str | None = None
    ) -> PresignedRequest:
        raise RuntimeError("loop_projection no lee clips")


def _register_loop_projection(registry: ConsumerRegistry, services: UnitServices) -> None:
    """``loop_projection`` (TASK-341) con los puertos de U-03 y de U-02 en proceso."""
    ports = catalog_query_ports(services.database)
    reader = LectorExpediente(
        database=services.database,
        audit=services.audit,
        clock=services.clock,
        metrics=services.metrics,
    )
    enrichment = ProjectionEnrichment(
        catalog=ports.catalog,
        gates=ports.gates,
        coverage=CoverageService(
            database=services.database,
            audit=services.audit,
            clock=services.clock,
            metrics=services.metrics,
        ),
        markers=EvidenceService(
            database=services.database,
            audit=services.audit,
            storage=services.evidence if services.evidence is not None else _NoEvidenceReads(),
        ),
        settings=PostgresLoopConfigurationRepository(services.database),
    )
    register_loop_projection(
        registry,
        LoopProjectionConsumer(
            store=LoopProjectionStore(),
            reader=reader,
            enrichment=enrichment,
            writer=services.writer,
            clock=services.clock,
        ),
    )


LOOP_CONSUMERS: Final[Mapping[str, int]] = {
    LOOP_PROJECTION: LOOP_PROJECTION_CONCURRENCY,  # TASK-341
}
"""Consumidores del lazo: nombre → concurrencia de su mamparo (LC-LAZ-40)."""
CONSUMER_REGISTRARS: Final[tuple[ConsumerRegistrar, ...]] = (
    _register_loop_projection,  # loop_projection (TASK-341)
)


def register_blocked_vocabulary(registry: FreeTextPolicyRegistry) -> None:
    """``blocked_vocabulary`` (TASK-303) con su caché por proceso y su preparador.

    El preparador comprueba la versión de la lista al abrir cada escritura (PAT-LAZ-REN-02); el
    validador solo lee la caché. La lista inicial se compila aquí, al arrancar: un error en la tabla
    de flexiones o en un patrón detiene el arranque, no la primera escritura.
    """
    initial_vocabulary()
    cache = VocabularyCache(PostgresVocabularyStore())
    registry.register(BLOCKED_VOCABULARY, BlockedVocabularyValidator(cache.compiled_for))
    registry.register_preparer(BLOCKED_VOCABULARY, cache.refresh)


FREE_TEXT_REGISTRARS: Final[tuple[FreeTextRegistrar, ...]] = (register_blocked_vocabulary,)
"""Los validadores de texto libre de U-04 (``blocked_vocabulary``; ``person_attribution`` llega
con TASK-340)."""

U04_FREE_TEXT_VALIDATORS: Final = frozenset({BLOCKED_VOCABULARY})
"""Los validadores que U-04 exige en la política sellada (BLM §9; NFR-LAZ-14)."""


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


API_STATE: Final[tuple[StateBuilder, ...]] = (
    _attachments_state,  # ClosureAttachmentPort (TASK-310)
    loop_http_state,  # LoopHttp (TASK-304)
)

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
"""Todos los consumidores de U-04 con su concurrencia."""

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


def missing_free_text_validators(services: UnitServices) -> tuple[tuple[str, str], ...]:
    """Los validadores de ``U04_FREE_TEXT_VALIDATORS`` que la política sellada no tiene."""
    present = set(services.free_text.validator_names)
    return tuple(
        ("free_text_validator", name) for name in sorted(U04_FREE_TEXT_VALIDATORS - present)
    )


def loop_tasks(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
    # Arranque cerrado (BLM §9): sin los validadores de U-04 no arranca ningún proceso. Esta es la
    # primera llamada de la composición que ve la política ya sellada.
    missing = missing_free_text_validators(services)
    if missing:
        raise RegistrationIncomplete(missing)
    for register in TASK_REGISTRARS:
        register(registry, services)


def loop_consumers(registry: ConsumerRegistry, services: UnitServices) -> None:
    # Cada consumidor sale con su mamparo (LC-LAZ-40); el registrador no lo conoce.
    bulkheaded = bulkheaded_registry(registry, services)
    for register in CONSUMER_REGISTRARS:
        register(bulkheaded, services)


def loop_http_client_factory(services: UnitServices) -> HttpClientFactory:
    """La fábrica única de clientes de salida de U-04 (LC-LAZ-39), con los destinos de
    ``services.config`` (los del diseño si no hay configuración)."""
    return HttpClientFactory.from_config(services.config)


def loop_metrics() -> LoopMetrics:
    """Los instrumentos de U-04 sobre el medidor de la plataforma (LC-LAZ-38)."""
    return get_loop_metrics()


def loop_api_state(services: UnitServices) -> Mapping[str, object]:
    state: dict[str, object] = {}
    for build in API_STATE:
        for key, value in build(services).items():
            if key in state:
                raise ValueError(f"la clave de app.state «{key}» está dos veces en loop")
            state[key] = value
    return state
