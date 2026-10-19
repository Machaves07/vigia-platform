"""SUP-U05-05 y 06: la cola de revisión (TASK-309; LC-LAZ-08; BR-LAZ-29 a 35; PR-LAZ-08 y 09).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``review_queue_stack``):

- **Consumidor** ``review_queue`` de ``detection_for_review_received``: lee el contenido completo
  por ``LectorExpediente.get`` (el evento solo trae ``platform_record_id``, nunca el contenido:
  BR-NUC-75, la única variante que el contrato admite), crea el elemento ``pending`` con su
  ``review_due_at`` congelado y aplica la misma proyección general (``in_review``, T02).
- **Idempotencia** (PR-LAZ-09): dos entregas del mismo evento, o dos trabajadores a la vez sobre la
  misma detección, crean un solo elemento (``(organization_id, source_event_id)`` y
  ``detection_id``).
- **SUP-U05-05**: la página de elementos pendientes, por ``review_due_at`` ascendente, con cursor.
- **SUP-U05-06**: la resolución (T05 confirma, abre el lazo; T06 descarta), con motivo validado,
  registro, ``Label``, historial y evento, todo en una transacción; dos resoluciones simultáneas
  del mismo elemento dejan una y un ``transition_not_allowed``.
- **Alcance**: la cola nunca muestra ni toca la de otra organización; resolver una detección de
  otra organización responde ``not_found``.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_review_queue_support import (
    REASON,
    ReviewQueueStack,
    Who,
    review_queue_stack,
)
from vigia_platform.loop.adapters.postgres.review_queue_repository import ReviewQueueRepository
from vigia_platform.loop.application.review_queue import (
    ReviewQueueReminder,
    ReviewResolutionRejected,
    ReviewResolutionResult,
)
from vigia_platform.loop.calendar import business_deadline
from vigia_platform.loop.config.parameters import LoopSettings
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.notifications.catalog import catalog_entry
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration

LOCK_WAIT_SECONDS: Final = 60.0


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ReviewQueueStack]:
    with review_queue_stack(postgres_endpoint, "loop_review_queue") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: ReviewQueueStack) -> Iterator[None]:
    yield
    stack.world.stack.run(stack.world.stack.release())


@pytest.fixture(scope="module")
def coordinator(stack: ReviewQueueStack) -> Who:
    return stack.member(stack.world.a)


def _error(response: Any) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: Any) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


# --- el consumidor -------------------------------------------------------------------------------


def test_the_consumer_creates_the_item_and_projects_in_review(stack: ReviewQueueStack) -> None:
    detection = stack.in_review()
    row = stack.queue_row(detection)
    assert row is not None
    assert row["state"] == "pending"
    assert row["detection_id"] == detection.subject_id
    assert row["anchor_record_id"] == detection.record_id
    document = detection.document
    assert row["family"] == document["family"]
    assert row["review_reason"] in ("low_confidence", "ambiguous", "no_bounding_box", "other")
    assert row["max_confidence"] == pytest.approx(document["max_confidence"])
    assert row["review_due_at"] > row["received_at"]
    assert row["reminders_sent"] == 0 and row["resolved_at"] is None
    projected = stack.world.finding_state(detection)
    assert projected is not None
    assert projected["lifecycle_state"] == "in_review"
    assert projected["subject_kind"] == "detection"
    assert projected["last_record_id"] == detection.record_id


def test_the_deadline_is_business_days_from_the_configuration(stack: ReviewQueueStack) -> None:
    """Sin configuración propia todavía, la planta usa los valores por defecto
    (``review_deadline_business_days`` 5, lunes a viernes sin festivos): BR-LAZ-145, el mismo
    plazo que ``settings_in`` devuelve sin fila."""
    organization = stack.world.organization()
    detection = stack.in_review(organization)
    row = stack.queue_row(detection)
    assert row is not None
    (timezone_row,) = stack.fetch(
        "SELECT timezone FROM identity.plant WHERE organization_id = $1 AND plant_id = $2",
        organization.organization_id,
        organization.plant_id,
    )
    config_rows = stack.fetch(
        "SELECT 1 FROM loop.plant_loop_configuration WHERE organization_id = $1 AND plant_id = $2",
        organization.organization_id,
        organization.plant_id,
    )
    assert config_rows == []
    settings = LoopSettings.defaults()
    expected = business_deadline(
        row["received_at"],
        settings["review_deadline_business_days"],
        settings.business_calendar,
        str(timezone_row["timezone"]),
    )
    assert row["review_due_at"] == expected


def test_a_duplicate_delivery_of_the_same_event_creates_only_one_item(
    stack: ReviewQueueStack,
) -> None:
    detection = stack.world.ingest_detection(stack.world.a)
    event = stack.world.review_queue_event(detection)
    first = stack.world.deliver_review_queue(stack.consumer, event)
    second = stack.world.deliver_review_queue(stack.consumer, event)
    assert first is True
    assert second is False
    rows = stack.fetch(
        "SELECT count(*) AS n FROM loop.review_queue_item"
        " WHERE organization_id = $1 AND detection_id = $2",
        detection.organization.organization_id,
        detection.subject_id,
    )
    assert int(rows[0]["n"]) == 1


def test_two_workers_delivering_the_same_detection_at_once_create_one_item(
    stack: ReviewQueueStack,
) -> None:
    """Mutación (tabla de garantías): quitar ``ON CONFLICT (detection_id) DO NOTHING`` del
    ``INSERT`` hace que la segunda entrega concurrente choque con una violación de unicidad en
    vez de devolver ``False`` en silencio; con el candado restaurado, exactamente un elemento."""
    detection = stack.world.ingest_detection(stack.world.a)
    event = stack.world.review_queue_event(detection)
    database = stack.world.stack.database()
    context = stack.world.event_context(detection.organization)

    async def deliver() -> bool:
        async with database.transaction(context) as transaction:
            return await stack.consumer.consume(event, transaction)

    async def race() -> list[Any]:
        return list(await asyncio.gather(deliver(), deliver(), return_exceptions=True))

    try:
        results = stack.run(race())
    finally:
        stack.run(database.dispose())
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], failures
    assert sorted(results) == [False, True]
    rows = stack.fetch(
        "SELECT count(*) AS n FROM loop.review_queue_item"
        " WHERE organization_id = $1 AND detection_id = $2",
        detection.organization.organization_id,
        detection.subject_id,
    )
    assert int(rows[0]["n"]) == 1


# --- review_reminders -----------------------------------------------------------------------------


def _reminder(stack: ReviewQueueStack) -> ReviewQueueReminder:
    return ReviewQueueReminder(
        repository=stack.repository,
        configuration=PostgresLoopConfigurationRepository(stack.database),
        outbox=stack.world.stack.outbox,
        clock=stack.world.stack.authz.sessions.clock,
    )


def _make_overdue(stack: ReviewQueueStack, detection: IngestedSubject) -> None:
    """Mueve ``review_due_at`` justo después de ``received_at`` (la restricción
    ``review_queue_item_due_after_received`` exige que siga siendo posterior) y avanza el reloj
    simulado para que quede antes de ahora, sin depender del orden de otras pruebas del módulo."""
    stack.world.stack.execute(
        "UPDATE loop.review_queue_item SET review_due_at = received_at + interval '1 second'"
        " WHERE organization_id = $1 AND detection_id = $2",
        detection.organization.organization_id,
        detection.subject_id,
    )
    stack.tick(5)


async def _remind(stack: ReviewQueueStack, reminder: ReviewQueueReminder, organization: Any) -> Any:
    context = stack.world.event_context(organization)
    async with stack.database.transaction(context) as transaction:
        return await reminder.remind(transaction)


def test_one_reminder_per_window_even_if_the_task_runs_twice(stack: ReviewQueueStack) -> None:
    """U04-H-07: la actualización condicional de ``send_reminders`` es la garantía (tabla de
    garantías del PR). Mutación: quitar la condición de la ventana
    (``reminder_sent_at IS NULL OR reminder_sent_at <= :cutoff``) del ``UPDATE`` hace que la
    segunda ejecución, sin que pase la ventana, vuelva a avisar; con la condición restaurada, un
    solo aviso por ventana (verificado y revertido antes de este commit, evidencia en el PR)."""
    organization = stack.world.organization()
    detection = stack.in_review(organization)
    _make_overdue(stack, detection)
    reminder = _reminder(stack)

    first = stack.run(_remind(stack, reminder, organization))
    assert len(first) == 1 and first[0].detection_id == detection.subject_id
    assert first[0].reminders_sent == 1
    row = stack.queue_row(detection)
    assert row is not None
    assert (row["reminders_sent"], row["state"]) == (1, "pending")

    # La tarea corre otra vez enseguida (arrendamiento vencido artificialmente, U04-H-07): nada.
    second = stack.run(_remind(stack, reminder, organization))
    assert second == ()
    row_again = stack.queue_row(detection)
    assert row_again is not None
    assert (row_again["reminders_sent"], row_again["state"]) == (1, "pending")

    # Pasa la ventana completa (reminder_interval_hours = 24 por defecto): un segundo aviso.
    stack.tick(24 * 3600 + 1)
    third = stack.run(_remind(stack, reminder, organization))
    assert len(third) == 1 and third[0].reminders_sent == 2
    row_third = stack.queue_row(detection)
    assert row_third is not None
    assert (row_third["reminders_sent"], row_third["state"]) == (2, "pending")


def test_the_first_reminder_is_immediate_and_repeats_are_normal(stack: ReviewQueueStack) -> None:
    """Criterio de aceptación de TASK-309: con ``reminder_interval_hours = 24`` por defecto y la
    tarea corriendo cada hora, un elemento vencido recibe un recordatorio al día; el primero
    conserva el nivel ``immediate`` del catálogo de notificaciones y las repeticiones bajan a
    ``normal`` (``phase`` en la carga, ``notifications.catalog`` lo usa como ``immediate_when``).
    """
    organization = stack.world.organization()
    detection = stack.in_review(organization)
    _make_overdue(stack, detection)
    reminder = _reminder(stack)

    sequence = []
    for _ in range(3):
        sent = stack.run(_remind(stack, reminder, organization))
        assert len(sent) == 1 and sent[0].detection_id == detection.subject_id
        sequence.append((sent[0].reminders_sent, sent[0].phase.value))
        stack.tick(24 * 3600 + 1)
    assert sequence == [(1, "first"), (2, "repeat"), (3, "repeat")]

    published = stack.overdue_events(organization, detection.subject_id)
    assert len(published) == 3
    entry = catalog_entry("review_overdue")
    assert entry is not None
    urgencies = [entry.urgency_for(json.loads(row["payload"])).value for row in published]
    assert urgencies == ["immediate", "normal", "normal"]


def test_two_concurrent_reminder_runs_send_only_one(stack: ReviewQueueStack) -> None:
    """Prueba concurrente (tabla de garantías): dos ejecuciones de ``remind`` a la vez sobre el
    mismo elemento vencido dejan un solo aviso; el ``UPDATE ... WHERE ...`` condicional serializa
    a la segunda, que no encuentra fila que cumpla la condición."""
    organization = stack.world.organization()
    detection = stack.in_review(organization)
    _make_overdue(stack, detection)
    database = stack.world.stack.database()
    context = stack.world.event_context(organization)
    reminder = ReviewQueueReminder(
        repository=ReviewQueueRepository(database),
        configuration=PostgresLoopConfigurationRepository(database),
        outbox=stack.world.stack.outbox,
        clock=stack.world.stack.authz.sessions.clock,
    )

    async def remind_once() -> Any:
        async with database.transaction(context) as transaction:
            return await reminder.remind(transaction)

    async def race() -> list[Any]:
        return list(await asyncio.gather(remind_once(), remind_once(), return_exceptions=True))

    try:
        results = stack.run(race())
    finally:
        stack.run(database.dispose())
    failures = [r for r in results if isinstance(r, BaseException)]
    assert failures == [], failures
    sent_counts = sorted(len(r) for r in results)
    assert sent_counts == [0, 1]
    row = stack.queue_row(detection)
    assert row is not None
    assert (row["reminders_sent"], row["state"]) == (1, "pending")


# --- SUP-U05-05: listar --------------------------------------------------------------------------


def test_the_list_orders_by_due_date_and_paginates(stack: ReviewQueueStack) -> None:
    organization = stack.world.organization()
    detections = []
    for _ in range(3):
        stack.tick(3600 * 6)
        detections.append(stack.in_review(organization))
    # La sesión se abre después de avanzar el reloj: una sesión abierta antes expiraría por
    # inactividad con las 18 horas que suman los tres avances.
    who = stack.member(organization)
    response = stack.list_queue(who, limit=2)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None
    first_ids = [item["detection_id"] for item in body["items"]]
    assert first_ids == [str(d.subject_id) for d in detections[:2]]
    second = stack.list_queue(who, cursor=body["next_cursor"], limit=2)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert [item["detection_id"] for item in second_body["items"]] == [
        str(detections[2].subject_id)
    ]
    assert second_body["next_cursor"] is None


def test_the_list_never_shows_another_organizations_items(stack: ReviewQueueStack) -> None:
    foreign = stack.in_review(stack.world.b)
    who = stack.member(stack.world.a)
    response = stack.list_queue(who, limit=200)
    assert response.status_code == 200, response.text
    ids = {item["detection_id"] for item in response.json()["items"]}
    assert str(foreign.subject_id) not in ids


def test_a_zone_scoped_coordinator_does_not_see_a_sibling_zone_of_the_same_organization(
    stack: ReviewQueueStack,
) -> None:
    """``_PENDING_PAGE`` filtra por alcance (``organization_wide`` o ``plant_id``/``zone_id`` del
    rol), no solo por organización: la guarda de alcance exige una prueba que falle si se quita
    (AGENTS.md, «guardas de alcance»). Mutación verificada en rojo y revertida (tabla del PR):
    cambiar la cláusula a ``CAST(TRUE AS boolean) OR ...`` hace que ``sibling`` aparezca en la
    lista del coordinador de zona; con la cláusula restaurada, no aparece."""
    organization = stack.world.organization()
    own = stack.in_review(organization)
    sibling = stack.in_review(organization)
    site = organization.site
    stack.world.stack.execute(
        "UPDATE loop.review_queue_item SET zone_id = $1"
        " WHERE organization_id = $2 AND detection_id = $3",
        site.other_zone_id,
        organization.organization_id,
        sibling.subject_id,
    )
    zone_coordinator = stack.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id
    )
    response = stack.list_queue(zone_coordinator, limit=200)
    assert response.status_code == 200, response.text
    ids = {item["detection_id"] for item in response.json()["items"]}
    assert ids == {str(own.subject_id)}
    assert str(sibling.subject_id) not in ids


def test_a_resolved_item_leaves_the_list(stack: ReviewQueueStack) -> None:
    organization = stack.world.organization()
    who = stack.member(organization)
    detection = stack.in_review(organization)
    before = {item["detection_id"] for item in stack.list_queue(who, limit=200).json()["items"]}
    assert str(detection.subject_id) in before
    assert stack.resolve(who, detection).status_code == 201
    after = {item["detection_id"] for item in stack.list_queue(who, limit=200).json()["items"]}
    assert str(detection.subject_id) not in after


# --- SUP-U05-06: resolver -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "category", "state", "event"),
    [
        ("review_confirmed", "real_coexistence", "open", "review_confirmed"),
        ("review_discarded", "object_mistaken_for_person", "discarded", "review_discarded"),
    ],
)
def test_each_outcome_writes_record_label_event_history_and_projection(
    stack: ReviewQueueStack, coordinator: Who, outcome: str, category: str, state: str, event: str
) -> None:
    detection = stack.in_review()
    response = stack.resolve(coordinator, detection, outcome, category)
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["outcome"], body["reason_category"], body["lifecycle_state"]) == (
        outcome,
        category,
        state,
    )
    record_id = uuid.UUID(body["record_id"])
    (resolution,) = stack.resolutions(detection)
    assert resolution["record_id"] == record_id
    assert resolution["reason_text"] == REASON
    organization_labels = stack.labels(detection.organization)
    (label,) = [x for x in organization_labels if x["source_record_id"] == record_id]
    assert (label["outcome"], label["reason_category"]) == (outcome, category)
    assert stack.events(detection.organization, record_id) == [event]
    row = stack.queue_row(detection)
    assert row is not None and row["state"] == "resolved" and row["resolved_at"] is not None
    projected = stack.world.finding_state(detection)
    assert projected is not None and projected["lifecycle_state"] == state


def _untouched(stack: ReviewQueueStack, subject: IngestedSubject) -> tuple[Any, Any]:
    return stack.counts(subject.organization), stack.queue_row(subject)


_REQUIRED: Final = (400, "invalid_request", "loop_reason_required")
_TOO_SHORT: Final = (400, "invalid_request", "loop_reason_too_short")
_INVALID: Final = (400, "invalid_request", None)
_NOT_ALLOWED: Final = (409, "conflict", "loop_transition_not_allowed")


def test_reason_borders_and_shape(stack: ReviewQueueStack, coordinator: Who) -> None:
    detection = stack.in_review()
    before = _untouched(stack, detection)
    confirmed = {"outcome": "review_confirmed", "reason_category": "real_coexistence"}
    rejected: list[tuple[dict[str, Any], tuple[int, str, str | None]]] = [
        ({"outcome": "review_confirmed", "reason_text": REASON}, _REQUIRED),
        (dict(confirmed), _REQUIRED),
        ({**confirmed, "reason_text": None}, _REQUIRED),
        ({**confirmed, "reason_text": "   "}, _REQUIRED),
        ({**confirmed, "reason_text": "x" * 19}, _TOO_SHORT),
        ({**confirmed, "reason_text": "x" * 1001}, _INVALID),
        (
            {**confirmed, "reason_category": "object_mistaken_for_person", "reason_text": REASON},
            _INVALID,
        ),
        ({**confirmed, "outcome": "pending", "reason_text": REASON}, _INVALID),
        ({"reason_category": "real_coexistence", "reason_text": REASON}, _INVALID),
    ]
    for body, expected in rejected:
        response = stack.send(coordinator, detection.subject_id, body)
        assert _error(response) == expected, (body, response.text)
    assert _untouched(stack, detection) == before
    accepted = stack.resolve(coordinator, detection, reason_text="x" * 20)
    assert accepted.status_code == 201, accepted.text


def test_transition_not_allowed_writes_nothing(stack: ReviewQueueStack, coordinator: Who) -> None:
    detection = stack.in_review()
    assert stack.resolve(coordinator, detection).status_code == 201
    before = _untouched(stack, detection)
    again = stack.resolve(coordinator, detection, "review_discarded", "no_person_present")
    assert _error(again) == _NOT_ALLOWED, again.text
    assert _untouched(stack, detection) == before
    finding = stack.world.ingest_finding(stack.world.a)
    assert stack.world.project(finding).transition_id == "T01"
    response = stack.send(coordinator, finding.subject_id, {"outcome": "review_confirmed"})
    assert _error(response) == (404, "not_found", None)


# --- concurrencia ---------------------------------------------------------------------------------


async def _waiting_advisory_locks(stack: ReviewQueueStack) -> int:
    rows = await stack.world.stack.authz.sessions.admin.fetch(
        "SELECT count(*) AS n FROM pg_locks WHERE NOT granted AND locktype = 'advisory'"
        " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    )
    return int(rows[0]["n"])


def test_two_simultaneous_resolutions_leave_exactly_one_winner(
    stack: ReviewQueueStack, coordinator: Who
) -> None:
    """Tabla de garantías: la exclusión de la detección (``lock_finding``) deja una sola
    resolución; la mutación (quitar la llamada a ``lock_finding`` en
    ``ReviewQueueService.resolve``) se corrió directamente contra esta prueba —no contra
    ``classification``, que usa el mismo mecanismo pero es una prueba distinta— y dio
    ``assert (1 == 1 and 0 == 1)`` en rojo (evidencia en el PR); restaurada después."""
    detection = stack.in_review()
    context = stack.context(coordinator)
    service = stack.service

    async def both() -> list[Any]:
        calls = [
            service.resolve(
                context,
                detection.subject_id,
                outcome=outcome,
                reason_category=category,
                reason_text=REASON,
            )
            for outcome, category in (
                ("review_confirmed", "real_coexistence"),
                ("review_discarded", "no_person_present"),
            )
        ]
        return list(await asyncio.gather(*calls, return_exceptions=True))

    results = stack.run(both())
    succeeded = [r for r in results if isinstance(r, ReviewResolutionResult)]
    rejected = [r for r in results if isinstance(r, ReviewResolutionRejected)]
    assert len(succeeded) == 1 and len(rejected) == 1
    assert rejected[0].detail_code is LoopDetailCode.TRANSITION_NOT_ALLOWED
    assert len(stack.resolutions(detection)) == 1
    row = stack.queue_row(detection)
    assert row is not None and row["state"] == "resolved"


# --- alcance --------------------------------------------------------------------------------------


def test_out_of_scope_answers_like_missing_and_writes_nothing(
    stack: ReviewQueueStack, coordinator: Who
) -> None:
    world = stack.world
    foreign = stack.in_review(world.b)
    before = _untouched(stack, foreign)
    body = {
        "outcome": "review_confirmed",
        "reason_category": "real_coexistence",
        "reason_text": REASON,
    }
    known = stack.resolve(coordinator, foreign)
    missing = stack.send(coordinator, uuid.uuid4(), body)
    assert _error(known)[:2] == (404, "not_found")
    assert _comparable(known) == _comparable(missing)
    assert _untouched(stack, foreign) == before
    detection = stack.in_review()
    before_own = _untouched(stack, detection)
    site = world.a.site
    for who in (
        stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.other_zone_id),
        stack.member(world.a, Role.PLANT_MANAGER),
        stack.member(world.a, Role.ADMINISTRATOR),
        stack.member(world.a, Role.LINE_MANAGER),
    ):
        response = stack.resolve(who, detection)
        assert _error(response)[:2] == (404, "not_found"), response.text
    assert _untouched(stack, detection) == before_own
    zone_coordinator = stack.member(world.a, Role.COORDINATOR_SST, ScopeLevel.ZONE, site.zone_id)
    assert stack.resolve(zone_coordinator, detection).status_code == 201
