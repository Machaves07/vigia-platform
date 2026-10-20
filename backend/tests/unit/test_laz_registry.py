"""Registro de U-04 en la raíz de composición (TASK-302; LC-LAZ-37; NFR-LAZ-14; A-52).

- **Tres unidades** en ``REGISTERED_UNITS`` (``loop``, ``accreditation`` y ``notifications``),
  después de las de U-02 y U-03, que delegan en el ``registration.py`` de su paquete.
- **Manifiestos a mano**: ``U04_RECORD_TYPES`` (17) y ``U04_EVENT_TYPES`` (los 25 de §11.3 y el
  trabajo interno ``export_requested`` de TASK-317) coinciden con lo que registra el código;
  ``U04_TASKS`` con ``verify_loop_projection`` (TASK-306, diaria por organización;
  ``rebuild_loop_projection`` corre a demanda, sin cadencia, U04-H-14, y no entra aquí),
  ``review_reminders`` (TASK-309), ``unclosable_review_due`` (TASK-312), ``monthly_package``
  (TASK-342 a 344) y ``email_grouping`` y ``digest_weekly`` (TASK-348), ``U04_CONSUMERS`` con
  ``export_worker``, ``review_queue`` y ``notification_engine`` (TASK-324); en ``ROUTERS``, las
  dos rutas de adjuntos de cierre (TASK-310), las dos de la configuración del lazo (TASK-304), la
  de la clasificación (TASK-308), las dos de la cola de revisión (TASK-309), las de la acción y su
  evidencia (TASK-311), las del cierre firmado y la reapertura (TASK-343, VIG-233) y las del
  incerrable (TASK-312) de ``loop``, las dos de exportación y las tres del paquete mensual de
  ``accreditation`` (TASK-317 y TASK-342 a 344) y las cuatro de ``notifications`` (lista, marcar
  leída y preferencias; TASK-324 y TASK-348).
- **La raíz real** compone los 17 tipos y los 25 eventos de U-04 además de los de U-02 y U-03, y
  ``missing_u04_registrations`` no encuentra nada que falte.
- **Fallo cerrado**: sin un tipo, un evento, una tarea o un consumidor del manifiesto (o con la
  tarea en otra cadencia, otra unidad u otra iteración, o el tipo escrito por otra unidad),
  ``missing_u04_registrations`` nombra exactamente lo que falta y ``verify_required`` lanza
  ``RegistrationIncomplete``.

Sin base ni red: los consumidores y las tareas se componen con dependencias inertes.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Final
from unittest.mock import Mock

import pytest
from fastapi.routing import APIRoute

from tests.properties.laz.strategies.u04_records import u04_event_registry, u04_registry
from vigia_platform.accreditation import registration as accreditation_registration
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.adoption.reads import AdoptionReads
from vigia_platform.accreditation.adoption.task import ADOPTION_CHECK, ADOPTION_CHECK_SCHEDULE
from vigia_platform.accreditation.events import (
    ACCREDITATION_EVENT_TYPES,
    ACCREDITATION_INTERNAL_EVENT_TYPES,
)
from vigia_platform.accreditation.export.download import ExportDownloads
from vigia_platform.accreditation.exposure.service import ExposureService
from vigia_platform.accreditation.monthly.audit import SampleAuditService
from vigia_platform.accreditation.monthly.commitments import CommitmentService
from vigia_platform.accreditation.monthly.task import MONTHLY_PACKAGE, MONTHLY_PACKAGE_SCHEDULE
from vigia_platform.accreditation.record_types import ACCREDITATION_RECORD_TYPES
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType, RecordTypeRegistry
from vigia_platform.loop import registration
from vigia_platform.loop.adapters.http.actions_routes import actions_router
from vigia_platform.loop.adapters.http.assistant_routes import assistant_router
from vigia_platform.loop.adapters.http.attachment_routes import (
    ATTACHMENTS_STATE_KEY,
    attachment_router,
)
from vigia_platform.loop.adapters.http.classification_routes import classification_router
from vigia_platform.loop.adapters.http.closure_routes import closure_router
from vigia_platform.loop.adapters.http.configuration_routes import configuration_router
from vigia_platform.loop.adapters.http.findings_routes import findings_router
from vigia_platform.loop.adapters.http.overdue_routes import overdue_router
from vigia_platform.loop.adapters.http.review_routes import review_queue_router
from vigia_platform.loop.adapters.http.root_cause_routes import root_cause_router
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.adapters.http.unclosable_routes import unclosable_router
from vigia_platform.loop.application.attachments import ClosureAttachmentService
from vigia_platform.loop.application.unclosable import SCHEDULE as UNCLOSABLE_SCHEDULE
from vigia_platform.loop.application.unclosable import TASK_NAME as UNCLOSABLE_REVIEW_DUE
from vigia_platform.loop.events import LOOP_EVENT_TYPES, LOOP_INTERNAL_EVENT_TYPES
from vigia_platform.loop.projection.consumer import LOOP_PROJECTION_EVENTS
from vigia_platform.loop.record_types import LOOP_RECORD_TYPES
from vigia_platform.notifications import registration as notifications_registration
from vigia_platform.notifications.adapters.http.routes import notifications_router
from vigia_platform.notifications.adapters.http.services import (
    NOTIFICATIONS_STATE_KEY,
    NotificationsHttp,
)
from vigia_platform.notifications.adapters.ses.delivery_events import (
    EMAIL_DELIVERY_EVENTS,
    EMAIL_DELIVERY_EVENTS_POLL,
    POLL_SCHEDULE,
)
from vigia_platform.notifications.catalog import subscribed_events
from vigia_platform.notifications.digest import DIGEST_WEEKLY, DIGEST_WEEKLY_SCHEDULE
from vigia_platform.notifications.events import NOTIFICATIONS_EVENT_TYPES
from vigia_platform.notifications.grouping import EMAIL_GROUPING, EMAIL_GROUPING_SCHEDULE
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import (
    Consumer,
    ConsumerRegistry,
    EventType,
    OutboxCatalog,
    PayloadModel,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.runtime.config import RegistrationIncomplete
from vigia_platform.shared.runtime.units import (
    UnitServices,
    free_text_registry,
    outbox_catalog,
    record_type_registry,
    registered_units,
    verify_required,
)

U04_UNITS: Final = ("loop", "accreditation", "notifications")


def _services() -> UnitServices:
    """Servicios inertes (como ``composed_tasks`` de ``test_task_isolation``): las tareas y los
    consumidores se construyen, pero ninguno se ejecuta."""
    inert: Any = Mock()
    return UnitServices(
        clock=inert,
        metrics=get_metrics(),
        provider_organization_id=uuid.uuid4(),
        database=inert,
        contexts=inert,
        authorizer=inert,
        audit=inert,
        outbox=inert,
        writer=inert,
        free_text=free_text_registry(registered_units()),
        signing=inert,
        checkpoints=inert,
        kms=inert,
    )


def _root() -> tuple[RecordTypeRegistry, OutboxCatalog]:
    units = registered_units()
    return record_type_registry(units), outbox_catalog(units, _services())


# --- las tres unidades ----------------------------------------------------------------------


def test_the_three_u04_units_are_registered_after_u02_and_u03() -> None:
    names = [unit.name for unit in registered_units()]
    assert names[-3:] == list(U04_UNITS)
    assert names[:-3] == ["shared", "identity", "ledger", "catalog", "fleet", "node_api"]
    units = {unit.name: unit for unit in registered_units()}
    assert units["loop"].required is registration.missing_u04_registrations
    assert units["loop"].free_text is registration.loop_free_text
    accreditation, notifications = units["accreditation"], units["notifications"]
    assert accreditation.record_types is accreditation_registration.accreditation_record_types
    assert notifications.event_types is notifications_registration.notifications_event_types


def test_the_u04_units_register_only_what_their_tasks_added() -> None:
    # TASK-310: los adjuntos de cierre en loop; TASK-304, TASK-308 y TASK-315: configuración,
    # clasificación y causa raíz; TASK-311: la acción correctiva y su evidencia;
    # TASK-317: la exportación (dos rutas, export_worker con concurrencia 1 y su app.state) en
    # accreditation; TASK-324: GET /notifications y notification_engine en notifications.
    units = {unit.name: unit for unit in registered_units()}
    tasks = PeriodicTaskRegistry()
    consumers = ConsumerRegistry()
    services = replace(_services(), evidence=Mock())
    for name in U04_UNITS:
        unit = units[name]
        routes = [
            (method, route.path)
            for router in unit.routers()
            for route in router.routes
            if isinstance(route, APIRoute)
            for method in sorted(route.methods)
        ]
        state = unit.api_state(services)
        if name == "loop":
            # TASK-310: SUP-U05-09 y SUP-U05-10 y su puerto ``ClosureAttachmentPort``;
            # TASK-304: SUP-U05-38 y SUP-U05-39 y ``LoopHttp``; TASK-308: SUP-U05-03;
            # TASK-311: SUP-U05-07 y 08; TASK-315: SUP-U05-16 a 18; TASK-316: SUP-U05-19 y 20;
            # TASK-343: SUP-U05-11 y 14 (VIG-233, cierre y reapertura).
            assert routes == [
                ("POST", "/loop/actions/{action_id}/attachments/upload-grant"),
                ("POST", "/loop/attachments/{attachment_id}/read-url"),
                ("GET", "/loop/plants/{plant_id}/configuration"),
                ("PUT", "/loop/plants/{plant_id}/configuration"),
                ("POST", "/loop/findings/{finding_state_id}/classifications"),
                ("GET", "/loop/review-queue"),  # TASK-309: SUP-U05-05
                ("POST", "/loop/review-queue/{detection_id}/resolution"),  # TASK-309: SUP-U05-06
                ("POST", "/loop/findings/{finding_state_id}/actions"),
                ("POST", "/loop/actions/{action_id}/evidence"),
                ("POST", "/loop/findings/{finding_state_id}/closure"),
                ("POST", "/loop/findings/{finding_state_id}/reopening"),
                ("POST", "/loop/findings/{finding_state_id}/root-cause-analyses"),
                ("PUT", "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"),
                ("POST", "/loop/root-cause-analyses/{analysis_id}/signature"),
                ("POST", "/loop/root-cause-analyses/{analysis_id}/assistant-requests"),
                ("GET", "/loop/assistant-requests/{request_id}"),
                ("GET", "/loop/findings"),  # TASK-307: SUP-U05-01
                ("GET", "/loop/findings/{finding_state_id}"),  # TASK-342: SUP-U05-02
                ("GET", "/loop/findings/{finding_state_id}/classifications"),  # TASK-342: 04
                ("GET", "/loop/overdue"),  # TASK-313: SUP-U05-15
                # TASK-312: SUP-U05-12 y 13 (el estado incerrable y su revisión).
                ("POST", "/loop/findings/{finding_state_id}/unclosable"),
                ("POST", "/loop/unclosable/{declaration_id}/reviews"),
            ]
            assert state[LOOP_STATE_KEY].overdue is not None
            assert state[LOOP_STATE_KEY].classification is not None
            assert state[LOOP_STATE_KEY].assistant is not None
            assert state[LOOP_STATE_KEY].actions is not None
            assert state[LOOP_STATE_KEY].evidence is not None
            assert state[LOOP_STATE_KEY].root_cause is not None
            assert state[LOOP_STATE_KEY].inbox is not None
            assert state[LOOP_STATE_KEY].detail is not None
            assert state[LOOP_STATE_KEY].closure is not None
            assert state[LOOP_STATE_KEY].reopening is not None
            assert state[LOOP_STATE_KEY].review_queue is not None
            assert state[LOOP_STATE_KEY].unclosable is not None
            assert set(state) == {ATTACHMENTS_STATE_KEY, LOOP_STATE_KEY}
            assert isinstance(state[ATTACHMENTS_STATE_KEY], ClosureAttachmentService)
            assert isinstance(state[LOOP_STATE_KEY], LoopHttp)
        elif name == "accreditation":
            # TASK-317: SUP-U05-21 y 22, su app.state y el consumidor export_worker;
            # TASK-345: SUP-U05-40 y 23 (las descargas, con el depósito de evidencias);
            # TASK-342 a 344: el paquete mensual, su muestra y la descarga por planta;
            # TASK-322: SUP-U05-31, el indicador de exposición; TASK-323: SUP-U05-32, la adopción.
            assert routes == [
                ("POST", "/accreditation/exports"),
                ("GET", "/accreditation/exports/{export_id}"),
                ("GET", "/accreditation/exports/{export_id}/download"),
                ("POST", "/accreditation/exports/{export_id}/download-url"),
                ("GET", "/accreditation/monthly-packages"),
                ("GET", "/accreditation/monthly-packages/{package_id}/sample"),
                ("GET", "/accreditation/monthly-packages/{plant_id}/{period}/download"),
                # TASK-347: auditoría muestral y compromisos (SUP-U05-26 a 29).
                ("POST", "/accreditation/sample-items/{sample_item_id}/result"),
                ("POST", "/accreditation/monthly-packages/{package_id}/commitments"),
                ("PATCH", "/accreditation/commitments/{commitment_id}"),
                ("GET", "/accreditation/commitments"),
                ("GET", "/accreditation/exposure"),
                ("GET", "/accreditation/adoption"),
            ]
            assert set(state) == {ACCREDITATION_STATE_KEY}
            http = state[ACCREDITATION_STATE_KEY]
            assert isinstance(http, AccreditationHttp)
            assert isinstance(http.downloads, ExportDownloads)
            assert isinstance(http.exposure, ExposureService)
            assert isinstance(http.sample_audit, SampleAuditService)
            assert isinstance(http.commitments, CommitmentService)
            assert isinstance(http.adoption, AdoptionReads)
            bare = unit.api_state(replace(services, evidence=None))[ACCREDITATION_STATE_KEY]
            assert isinstance(bare, AccreditationHttp) and bare.downloads is None
            # Sin depósito de evidencias, las escrituras de la revisión mensual siguen instaladas.
            assert isinstance(bare.sample_audit, SampleAuditService)
            assert isinstance(bare.commitments, CommitmentService)
        else:
            # TASK-324: SUP-U05-33 en su forma mínima; TASK-348: SUP-U05-33 completa (marcar
            # leída y preferencias), su app.state y notification_engine.
            assert routes == [
                ("GET", "/notifications"),
                ("POST", "/notifications/{notification_id}/read"),
                ("PUT", "/notifications/preferences"),
                ("GET", "/notifications/preferences"),
            ]
            assert set(state) == {NOTIFICATIONS_STATE_KEY}
            assert isinstance(state[NOTIFICATIONS_STATE_KEY], NotificationsHttp)
        unit.periodic_tasks(tasks, services)
        unit.consumers(consumers, services)
    # TASK-306: verify_loop_projection, diaria por organización; rebuild_loop_projection (LC-LAZ-03)
    # corre a demanda y no entra aquí (sin cadencia que vigilar, U04-H-14).
    # TASK-342 a 344: monthly_package, mensual, por organización (accreditation);
    # TASK-309: review_reminders, horaria, por organización (loop); TASK-325:
    # email_delivery_events_poll, ``global`` cada 15 s; TASK-348: email_grouping (cada hora) y
    # digest_weekly (diaria, filtra por ``weekly_digest_weekday`` del destinatario), por
    # organización (notifications); TASK-313: evaluate_deadlines, horaria, por organización
    # (loop); TASK-323: adoption_check, diaria, por organización (accreditation); TASK-312:
    # unclosable_review_due, diaria, por organización (loop). ``tasks()`` ordena por nombre, no
    # por orden de registro: "adoption_check" < "digest_weekly" < "email_delivery_events_poll" <
    # "email_grouping" < "evaluate_deadlines" < "monthly_package" < "review_reminders" <
    # "unclosable_review_due" < "verify_loop_projection".
    assert [(t.task_name, t.unit, t.schedule, t.iteration) for t in tasks.tasks()] == [
        (ADOPTION_CHECK, ActorUnit.U04, ADOPTION_CHECK_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        (DIGEST_WEEKLY, ActorUnit.U04, DIGEST_WEEKLY_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        (EMAIL_DELIVERY_EVENTS_POLL, ActorUnit.U04, POLL_SCHEDULE, TaskIteration.GLOBAL),
        (EMAIL_GROUPING, ActorUnit.U04, EMAIL_GROUPING_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        (
            "evaluate_deadlines",
            ActorUnit.U04,
            Schedule.every(3600),
            TaskIteration.PER_ORGANIZATION,
        ),
        (MONTHLY_PACKAGE, ActorUnit.U04, MONTHLY_PACKAGE_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        ("review_reminders", ActorUnit.U04, Schedule.every(3600), TaskIteration.PER_ORGANIZATION),
        (UNCLOSABLE_REVIEW_DUE, ActorUnit.U04, UNCLOSABLE_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        (
            registration.VERIFY_LOOP_PROJECTION,
            ActorUnit.U04,
            registration.VERIFY_LOOP_PROJECTION_SCHEDULE,
            TaskIteration.PER_ORGANIZATION,
        ),
    ]
    # TASK-341: loop_projection, sobre los eventos de U-03 de §8.4 y user_deactivated de U-02;
    # TASK-309: review_queue, sobre detection_for_review_received; TASK-325:
    # email_delivery_events, sobre el pulso interno de email_delivery_events_poll.
    assert [(c.consumer_name, c.unit, c.subscribed_events) for c in consumers.consumers()] == [
        ("assistant_worker", ActorUnit.U04, ("assistant_requested",)),
        (EMAIL_DELIVERY_EVENTS, ActorUnit.U04, (EMAIL_DELIVERY_EVENTS_POLL,)),
        ("export_worker", ActorUnit.U04, ("export_requested",)),
        ("loop_projection", ActorUnit.U04, LOOP_PROJECTION_EVENTS),
        ("notification_engine", ActorUnit.U04, subscribed_events()),
        ("review_queue", ActorUnit.U04, ("detection_for_review_received",)),
    ]
    # TASK-325: email_delivery_events declara su dependencia externa (SQS), separada del
    # circuito propio de SesEmailSender (PAT-LAZ-RES-04).
    delivery_events = consumers.get(EMAIL_DELIVERY_EVENTS)
    assert delivery_events is not None and delivery_events.has_external_dependency
    # TASK-316: el asistente declara su dependencia externa (cortacircuito de U-02).
    assistant = consumers.get("assistant_worker")
    assert assistant is not None and assistant.has_external_dependency
    assert (
        attachment_router,
        configuration_router,
        classification_router,
        review_queue_router,
        actions_router,
        closure_router,
        root_cause_router,
        assistant_router,
        findings_router,
        overdue_router,
        unclosable_router,
    ) == registration.ROUTERS
    assert (notifications_router,) == notifications_registration.ROUTERS
    # email_delivery_events_poll (TASK-325), email_grouping y digest_weekly (TASK-348).
    assert len(notifications_registration.TASK_REGISTRARS) == 3
    # TASK-306, TASK-309, TASK-313 y TASK-312: verify_loop_projection, review_reminders,
    # evaluate_deadlines y unclosable_review_due.
    assert len(registration.TASK_REGISTRARS) == 4
    # notification_engine, EMAIL_SENDERS (sin Consumer propio) y email_delivery_events.
    assert len(notifications_registration.CONSUMER_REGISTRARS) == 3
    assert len(registration.CONSUMER_REGISTRARS) == 3
    # monthly_package (TASK-342 a 344) y adoption_check (TASK-323).
    assert len(accreditation_registration.TASK_REGISTRARS) == 2
    assert accreditation_registration.ACCREDITATION_TASKS == {
        MONTHLY_PACKAGE: (MONTHLY_PACKAGE_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        ADOPTION_CHECK: (ADOPTION_CHECK_SCHEDULE, TaskIteration.PER_ORGANIZATION),
    }
    assert {
        registration.VERIFY_LOOP_PROJECTION: (
            registration.VERIFY_LOOP_PROJECTION_SCHEDULE,
            TaskIteration.PER_ORGANIZATION,
        ),
        "review_reminders": (Schedule.every(3600), TaskIteration.PER_ORGANIZATION),
        "evaluate_deadlines": (Schedule.every(3600), TaskIteration.PER_ORGANIZATION),
        UNCLOSABLE_REVIEW_DUE: (UNCLOSABLE_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        MONTHLY_PACKAGE: (MONTHLY_PACKAGE_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        ADOPTION_CHECK: (ADOPTION_CHECK_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        EMAIL_DELIVERY_EVENTS_POLL: (POLL_SCHEDULE, TaskIteration.GLOBAL),
        EMAIL_GROUPING: (EMAIL_GROUPING_SCHEDULE, TaskIteration.PER_ORGANIZATION),
        DIGEST_WEEKLY: (DIGEST_WEEKLY_SCHEDULE, TaskIteration.PER_ORGANIZATION),
    } == registration.U04_TASKS
    assert registration.U04_CONSUMERS == {
        "export_worker": 1,
        "loop_projection": 4,
        "assistant_worker": 2,
        "review_queue": 2,
        "notification_engine": 4,
        EMAIL_DELIVERY_EVENTS: 2,
    }
    # TASK-303 y TASK-340: los dos validadores de texto libre, el de vocabulario primero.
    assert (
        registration.register_blocked_vocabulary,
        registration.register_person_attribution,
    ) == registration.FREE_TEXT_REGISTRARS


# --- manifiestos ----------------------------------------------------------------------------


def test_the_manifests_match_the_types_and_events_of_the_code() -> None:
    # 25 eventos de §11.3 más los trabajos internos export_requested (TASK-317),
    # assistant_requested (TASK-316) y email_delivery_events_poll (TASK-325).
    assert len(registration.U04_RECORD_TYPES) == 17 and len(registration.U04_EVENT_TYPES) == 28
    assert len(LOOP_RECORD_TYPES) == 13
    # Cuatro tipos de la acreditación; ``commitments_recorded`` también en su versión 2 (TASK-347).
    assert len({definition.record_type for definition in ACCREDITATION_RECORD_TYPES}) == 4
    assert [
        (definition.record_type, definition.schema_version)
        for definition in ACCREDITATION_RECORD_TYPES
        if definition.schema_version != 1
    ] == [("commitments_recorded", 2)]
    assert len(LOOP_EVENT_TYPES) == 21 and len(ACCREDITATION_EVENT_TYPES) == 4
    assert [event.event_name for event in ACCREDITATION_INTERNAL_EVENT_TYPES] == [
        "export_requested"
    ]
    assert [event.event_name for event in LOOP_INTERNAL_EVENT_TYPES] == ["assistant_requested"]
    # TASK-325: email_delivery_events_poll, el único pulso interno que notifications publica.
    assert [event.event_name for event in NOTIFICATIONS_EVENT_TYPES] == [EMAIL_DELIVERY_EVENTS_POLL]
    assert {
        definition.record_type for definition in (*LOOP_RECORD_TYPES, *ACCREDITATION_RECORD_TYPES)
    } == registration.U04_RECORD_TYPES
    assert {
        event.event_name
        for event in (
            *LOOP_EVENT_TYPES,
            *ACCREDITATION_EVENT_TYPES,
            *ACCREDITATION_INTERNAL_EVENT_TYPES,
            *LOOP_INTERNAL_EVENT_TYPES,
            *NOTIFICATIONS_EVENT_TYPES,
        )
    } == registration.U04_EVENT_TYPES
    assert set(u04_registry().record_types()) == registration.U04_RECORD_TYPES
    assert set(u04_event_registry().event_names()) == registration.U04_EVENT_TYPES


def test_the_real_root_lists_u04_besides_u02_and_u03() -> None:
    record_types, catalog = _root()
    by_unit: dict[ActorUnit, set[str]] = {}
    for compiled in record_types.latest():
        by_unit.setdefault(compiled.writer_unit, set()).add(compiled.record_type)
    assert by_unit[ActorUnit.U04] == registration.U04_RECORD_TYPES
    assert by_unit[ActorUnit.U02] and by_unit[ActorUnit.U03]
    events = {
        compiled.event_name
        for compiled in catalog.event_types.compiled_types()
        if compiled.definition.publisher_unit is ActorUnit.U04
    }
    assert events == registration.U04_EVENT_TYPES
    assert registration.missing_u04_registrations(record_types, catalog) == ()
    verify_required(registered_units(), record_types, catalog)


# --- fallo cerrado --------------------------------------------------------------------------


def _types_without(name: str) -> RecordTypeRegistry:
    full = record_type_registry(registered_units())
    reduced = RecordTypeRegistry()
    for compiled in full.all_versions():
        if compiled.record_type != name:
            reduced.register(compiled.definition)
    return reduced


def _catalog_without_event(name: str) -> OutboxCatalog:
    reduced = OutboxCatalog()
    for unit in registered_units():
        full = OutboxCatalog()
        unit.event_types(full.event_types)
        for compiled in full.event_types.compiled_types():
            if compiled.event_name != name:
                reduced.event_types.register(compiled.definition)
        # Los consumidores y las tareas periódicas de la raíz (export_worker, TASK-317;
        # verify_loop_projection, TASK-306; review_reminders, TASK-309; unclosable_review_due,
        # TASK-312; monthly_package, TASK-342 a 344): solo falta el evento.
        unit.consumers(reduced.consumers, _services())
        unit.periodic_tasks(reduced.periodic_tasks, _services())
    return reduced


@pytest.mark.parametrize("name", sorted(registration.U04_RECORD_TYPES))
def test_a_missing_type_is_named(name: str) -> None:
    _, catalog = _root()
    types = _types_without(name)
    assert registration.missing_u04_registrations(types, catalog) == (("record_type", name),)
    with pytest.raises(RegistrationIncomplete) as raised:
        verify_required(registered_units(), types, catalog)
    assert raised.value.missing == (("record_type", name),)


@pytest.mark.parametrize("name", sorted(registration.U04_EVENT_TYPES))
def test_a_missing_event_is_named(name: str) -> None:
    types, _ = _root()
    catalog = _catalog_without_event(name)
    assert registration.missing_u04_registrations(types, catalog) == (("event_type", name),)


class _Probe(ContentModel):
    probe: bool


class _ProbePayload(PayloadModel):
    probe: bool = True


def test_a_u04_type_registered_by_another_unit_counts_as_missing() -> None:
    _, catalog = _root()
    types = _types_without("closure")
    types.register(
        RecordType(
            record_type="closure",
            writer_unit=ActorUnit.U03,
            chain_level=ChainLevel.PLANT,
            schema_version=1,
            content_model=_Probe,
        )
    )
    assert registration.missing_u04_registrations(types, catalog) == (("record_type", "closure"),)


def test_a_u04_event_published_by_another_unit_counts_as_missing() -> None:
    types, _ = _root()
    catalog = _catalog_without_event("adoption_low")
    catalog.event_types.register(
        EventType(
            event_name="adoption_low",
            publisher_unit=ActorUnit.U02,
            payload_model=_ProbePayload,
            description_es="Sonda",
        )
    )
    missing = registration.missing_u04_registrations(types, catalog)
    assert missing == (("event_type", "adoption_low"),)


async def _never(*_: object) -> None:
    raise AssertionError("el manejador no se ejecuta")


_PROBE_TASK: Final = "u04_probe_task"
_PROBE_SCHEDULE: Final = Schedule.every(3600)


def _with_task(
    schedule: Schedule = _PROBE_SCHEDULE,
    unit: ActorUnit = ActorUnit.U04,
    iteration: TaskIteration = TaskIteration.PER_ORGANIZATION,
) -> OutboxCatalog:
    _, catalog = _root()
    catalog.periodic_tasks.register(_PROBE_TASK, schedule, _never, unit=unit, iteration=iteration)
    return catalog


@pytest.fixture
def probe_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un manifiesto con una tarea y un consumidor, como los que añadirán las tareas de U-04."""
    monkeypatch.setattr(
        registration,
        "U04_TASKS",
        {_PROBE_TASK: (_PROBE_SCHEDULE, TaskIteration.PER_ORGANIZATION)},
    )
    monkeypatch.setattr(registration, "U04_CONSUMERS", {"u04_probe_consumer": 2})


@pytest.mark.usefixtures("probe_manifests")
def test_a_declared_task_and_consumer_pass_when_registered_as_declared() -> None:
    types, _ = _root()
    catalog = _with_task()
    catalog.consumers.register(
        Consumer(
            consumer_name="u04_probe_consumer",
            unit=ActorUnit.U04,
            subscribed_events=("finding_closed",),
            handler=_never,
        )
    )
    assert registration.missing_u04_registrations(types, catalog) == ()


@pytest.mark.usefixtures("probe_manifests")
@pytest.mark.parametrize(
    "catalog_of",
    [
        lambda: _root()[1],
        lambda: _with_task(schedule=Schedule.every(60)),
        lambda: _with_task(unit=ActorUnit.U03),
        lambda: _with_task(iteration=TaskIteration.GLOBAL),
    ],
    ids=["ausente", "otra-cadencia", "otra-unidad", "otra-iteracion"],
)
def test_a_task_missing_or_not_as_declared_is_named(catalog_of: Any) -> None:
    types, _ = _root()
    catalog = catalog_of()
    missing = registration.missing_u04_registrations(types, catalog)
    assert ("periodic_task", _PROBE_TASK) in missing
    assert ("consumer", "u04_probe_consumer") in missing
    assert len(missing) == 2


@pytest.mark.usefixtures("probe_manifests")
def test_a_consumer_of_another_unit_is_named() -> None:
    types, _ = _root()
    catalog = _with_task()
    catalog.consumers.register(
        Consumer(
            consumer_name="u04_probe_consumer",
            unit=ActorUnit.U02,
            subscribed_events=("finding_closed",),
            handler=_never,
        )
    )
    assert registration.missing_u04_registrations(types, catalog) == (
        ("consumer", "u04_probe_consumer"),
    )


def test_the_unit_registrars_are_called_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lo que una tarea añada a ``TASK_REGISTRARS`` se registra sin tocar ``units.py``."""
    calls: list[str] = []

    def register(registry: PeriodicTaskRegistry, services: UnitServices) -> None:
        calls.append("task")
        registry.register(_PROBE_TASK, _PROBE_SCHEDULE, _never, unit=ActorUnit.U04)

    monkeypatch.setattr(registration, "TASK_REGISTRARS", (register,))
    unit = next(unit for unit in registered_units() if unit.name == "loop")
    registry = PeriodicTaskRegistry()
    unit.periodic_tasks(registry, _services())
    assert calls == ["task"] and [task.task_name for task in registry.tasks()] == [_PROBE_TASK]
