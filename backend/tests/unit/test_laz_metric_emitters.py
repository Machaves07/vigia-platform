"""Productores de métricas U-04 con InMemoryMetricReader (NFR-LAZ-41, VIG-270)."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from tests.factories import make_context
from tests.laz_metrics_support import MetricCapture, metric_capture  # noqa: F401
from vigia_platform.ledger.free_text import FreeTextCandidate, FreeTextField, FreeTextRejected
from vigia_platform.loop.observability.worker import LoopWorkerTelemetry
from vigia_platform.loop.registration import U04_CONSUMERS, U04_TASKS
from vigia_platform.loop.validators.attribution import PersonAttributionValidator
from vigia_platform.loop.validators.vocabulary import BlockedVocabularyValidator, initial_vocabulary
from vigia_platform.notifications.adapters.ses.delivery_events import EmailDeliveryEventsConsumer
from vigia_platform.notifications.adapters.ses.sender import SesEmailSender
from vigia_platform.notifications.delivery import EmailPlan, write_notification
from vigia_platform.notifications.grouping import Candidate, DigestGrouperBase
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.dispatcher import Dispatcher, Outcome
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry, Schedule, TaskIteration
from vigia_platform.shared.worker.leases import LeaseSettings
from vigia_platform.shared.worker.scheduler import PeriodicScheduler, TaskRunReport, _Holder


@pytest.mark.parametrize(
    "validator, rejected, name, verdict",
    [
        (
            BlockedVocabularyValidator(lambda _: initial_vocabulary()),
            "zona despejada",
            "blocked_vocabulary",
            "rejected_blocked_vocabulary",
        ),
        (
            PersonAttributionValidator(),
            "el operario omitió el procedimiento",
            "person_attribution",
            "rejected_person_attribution",
        ),
    ],
)
def test_text_validation_observes_both_verdicts_without_text(
    capture: MetricCapture,
    validator: Any,
    rejected: str,
    name: str,
    verdict: str,
) -> None:
    field = FreeTextField("root_cause_analysis", "/statement", 1, 1000)
    validator(FreeTextCandidate("resguardo sin anclaje", "resguardo sin anclaje"), field)
    with pytest.raises(FreeTextRejected):
        validator(FreeTextCandidate(rejected, rejected), field)
    points = capture.points("loop_text_validations_total")
    assert [(dict(p.attributes), p.value) for p in points] == [
        ({"text_validator": name, "verdict": "accepted"}, 1),
        ({"text_validator": name, "verdict": verdict}, 1),
    ]


def test_exempt_vocabulary_values_do_not_count_as_validations(capture: MetricCapture) -> None:
    field = FreeTextField("vocabulary_list_changed", "/added[*]/value", 1, 1000)
    candidate = FreeTextCandidate("zona segura", "zona segura")
    BlockedVocabularyValidator(lambda _: initial_vocabulary())(candidate, field)
    PersonAttributionValidator()(candidate, field)
    assert capture.points("loop_text_validations_total") == []


class SnapshotDatabase:
    """Filas de las tres consultas, y marca de confirmación de cada lectura."""

    def __init__(self, context: Any, clock: SimulatedClock) -> None:
        self.context = context
        self.clock = clock
        self.empty = False
        self.fail = False
        self.plant = uuid.UUID(int=41)

    @asynccontextmanager
    async def transaction(self, context: Any) -> Any:
        async def execute(statement: Any, parameters: Any = None) -> Any:
            sql = str(statement)
            result = Mock()
            if "vigia_active_organizations" in sql:
                result.all.return_value = [
                    SimpleNamespace(organization_id=self.context.organization_id)
                ]
            elif "identity.plant" in sql:
                assert parameters["organization_id"] == context.organization_id
                assert parameters["now"] == self.clock.now()
                result.all.return_value = [
                    SimpleNamespace(
                        plant_id=self.plant,
                        unclassified=0 if self.empty else 2,
                        overdue=0 if self.empty else 1,
                        oldest=None if self.empty else self.clock.now() - timedelta(seconds=42),
                    )
                ]
            elif "shared.outbox_delivery" in sql:
                result.all.return_value = (
                    []
                    if self.empty
                    else [
                        SimpleNamespace(
                            consumer_name="review_queue",
                            pending=3,
                            oldest=self.clock.now() - timedelta(seconds=18),
                        )
                    ]
                )
            else:
                if self.fail:
                    raise RuntimeError("lectura interrumpida")
                result.scalar_one.return_value = not self.empty
            return result

        yield SimpleNamespace(execute=execute)


@pytest.mark.asyncio
async def test_worker_snapshot_emits_totals_and_resets_empty_series(capture: MetricCapture) -> None:
    context = make_context()
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    database = SnapshotDatabase(context, clock)
    registry = PeriodicTaskRegistry()
    task = registry.register(
        "evaluate_deadlines", Schedule.every(3600), AsyncMock(), unit=ActorUnit.U04
    )
    telemetry = LoopWorkerTelemetry(
        database=database,
        contexts=Mock(
            provider_audit_context=Mock(return_value=context),
            context_for_organization=Mock(return_value=context),
        ),
        clock=clock,
        task=task,
    )
    await telemetry.report_once()
    attrs = {"organization_id": str(context.organization_id), "plant_id": str(database.plant)}
    for name, value in (
        ("loop_unclassified_findings", 2),
        ("loop_unclassified_oldest_age_seconds", 42),
        ("loop_overdue_findings", 1),
    ):
        (point,) = capture.points(name)
        assert (dict(point.attributes), point.value) == (attrs, value)
    (point,) = capture.points("export_stuck_or_failed")
    assert (dict(point.attributes), point.value) == (
        {"organization_id": str(context.organization_id)},
        1,
    )
    for name, value in (
        ("loop_consumer_pending", 3),
        ("loop_consumer_oldest_pending_age_seconds", 18),
    ):
        points = capture.points(name)
        assert len(points) == len(U04_CONSUMERS)
        assert {p.attributes["consumer"]: p.value for p in points} == {
            consumer: value if consumer == "review_queue" else 0 for consumer in U04_CONSUMERS
        }
    database.empty = True
    await telemetry.report_once()
    for name in (
        "loop_unclassified_findings",
        "loop_unclassified_oldest_age_seconds",
        "loop_overdue_findings",
        "export_stuck_or_failed",
        "loop_consumer_pending",
        "loop_consumer_oldest_pending_age_seconds",
    ):
        assert all(p.value == 0 for p in capture.points(name))


@pytest.mark.asyncio
async def test_degraded_delivery_and_digest_states_reach_the_reader(capture: MetricCapture) -> None:
    context = make_context()
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    tx = SimpleNamespace(context=context, execute=AsyncMock())
    draft = Mock(plant_id=None, zone_id=None, source_event_id=uuid.UUID(int=1))
    draft.entry.event_name = "finding_received"
    draft.urgency.value = "immediate"
    draft.content.title_es = "Aviso de zona"
    draft.content.body_es = "Revise la condición"
    draft.deep_link.as_json.return_value = {"kind": "loop"}
    await write_notification(
        tx, draft, uuid.UUID(int=2), (EmailPlan.DEGRADED, "email_circuit_open"), clock
    )
    grouper = DigestGrouperBase(senders=Mock(), clock=clock)
    for failure_code in ("email_circuit_open", None):
        await grouper._send(
            tx,
            digest_batch_id=uuid.UUID(int=3),
            recipient_user_id=uuid.UUID(int=2),
            subject_prefix="Resumen",
            candidates=[
                Candidate(uuid.UUID(int=value), clock.now(), None, "Aviso", "Condición")
                for value in (4, 5)
            ],
            failure_code=failure_code,
        )
    points = capture.points("email_messages_total")
    assert {p.attributes["email_state"]: p.value for p in points} == {
        "degraded_app_only": 3,
        "digested": 1,
    }
    assert all(set(p.attributes) == {"email_state", "organization_id"} for p in points)


@pytest.mark.asyncio
async def test_failed_snapshot_does_not_publish_a_partial_global_total(
    capture: MetricCapture,
) -> None:
    context = make_context()
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    database = SnapshotDatabase(context, clock)
    database.fail = True
    task = PeriodicTaskRegistry().register(
        "evaluate_deadlines", Schedule.every(3600), AsyncMock(), unit=ActorUnit.U04
    )
    telemetry = LoopWorkerTelemetry(
        database=database,
        clock=clock,
        task=task,
        contexts=Mock(
            provider_audit_context=Mock(return_value=context),
            context_for_organization=Mock(return_value=context),
        ),
    )
    with pytest.raises(RuntimeError, match="lectura interrumpida"):
        await telemetry.report_once()
    assert capture.points("loop_consumer_pending") == []
    assert capture.points("export_stuck_or_failed") == []


@pytest.mark.asyncio
async def test_dispatcher_emits_dead_letter_after_commit_only_for_u04(
    capture: MetricCapture,
) -> None:
    context = make_context()
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    committed = False

    @asynccontextmanager
    async def transaction(context: Any) -> Any:
        nonlocal committed
        yield Mock()
        committed = True

    database = Mock(transaction=transaction)
    contexts = Mock(context_from_event=Mock(return_value=context))
    task = Mock()
    telemetry = LoopWorkerTelemetry(database=database, contexts=contexts, clock=clock, task=task)
    catalog = Mock(
        consumers=Mock(consumers=Mock(return_value=())),
        event_types=Mock(event_names=Mock(return_value=())),
    )
    dispatcher = Dispatcher(
        database=database,
        catalog=catalog,
        outbox=Mock(),
        contexts=contexts,
        clock=clock,
        telemetry=telemetry,
    )
    event = SimpleNamespace(
        organization_id=context.organization_id,
        event_name="finding_received",
        event_id=uuid.UUID(int=1),
    )
    dispatcher._process_in = AsyncMock(return_value=(Outcome.DEAD_LETTERED, (event, 8)))
    for name in (*U04_CONSUMERS, "alerts"):
        consumer = SimpleNamespace(consumer_name=name, unit=ActorUnit.U04)
        await dispatcher._process_admitted(consumer, event, None)
        assert committed
    assert {
        p.attributes["consumer"]: p.value for p in capture.points("loop_dead_letter_entries_total")
    } == dict.fromkeys(U04_CONSUMERS, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True], ids=["exito", "fallo"])
async def test_scheduler_observes_each_u04_task_with_catalog_attributes(
    capture: MetricCapture,
    failed: bool,
) -> None:
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    context = make_context()

    @asynccontextmanager
    async def transaction(context: Any) -> Any:
        yield SimpleNamespace(
            execute=AsyncMock(
                return_value=Mock(
                    all=Mock(
                        return_value=[
                            SimpleNamespace(
                                task_name=task, last_success_at=clock.now() - timedelta(seconds=17)
                            )
                            for task in (*U04_TASKS, "not_u04")
                        ]
                    )
                )
            )
        )

    database = Mock(transaction=transaction)
    contexts = Mock(
        context_for_organization=Mock(return_value=context),
        provider_audit_context=Mock(return_value=context),
    )
    registry = PeriodicTaskRegistry()
    tasks = [
        registry.register(name, Schedule.every(3600), AsyncMock(), unit=ActorUnit.U04)
        for name in (*U04_TASKS, "not_u04")
    ]
    telemetry = LoopWorkerTelemetry(
        database=database, contexts=contexts, clock=clock, task=tasks[0]
    )
    leases = Mock(
        settings=LeaseSettings(),
        holds=AsyncMock(return_value=True),
        advance=AsyncMock(return_value=Mock()),
    )
    scheduler = PeriodicScheduler(
        database=database,
        registry=registry,
        leases=leases,
        contexts=contexts,
        clock=clock,
        owner="prueba",
        telemetry=telemetry,
    )

    async def invoke(task: Any, transaction: Any) -> Exception | None:
        clock.advance(0.125)
        return ValueError() if failed else None

    scheduler._invoke = invoke
    for task in tasks:
        await scheduler._run_for(task, _Holder(Mock()), context.organization_id)
    await scheduler._report_last_success()
    result = "failed" if failed else "succeeded"
    points = capture.points("loop_periodic_task_duration_ms")
    assert {p.attributes["task"] for p in points} == set(U04_TASKS)
    assert all(
        dict(p.attributes) == {"task": p.attributes["task"], "result": result}
        and p.count == 1
        and p.sum == 125
        for p in points
    )
    ages = capture.points("loop_periodic_task_last_success_age_seconds")
    assert {p.attributes["task"] for p in ages} == set(U04_TASKS)
    assert all(p.value == 17 and set(p.attributes) == {"task"} for p in ages)


@pytest.mark.asyncio
async def test_ses_sent_bounced_and_complained_observations(capture: MetricCapture) -> None:
    context = make_context()
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))

    @asynccontextmanager
    async def transaction(context: Any) -> Any:
        yield SimpleNamespace(execute=AsyncMock())

    database = Mock(transaction=transaction)
    sender = SesEmailSender(
        database=database,
        http_clients=Mock(),
        region_name="us-east-1",
        sender_email="no-responder@mail.example.test",
        configuration_set_name="vigia-email-test",
        clock=clock,
    )
    await sender._mark_sent(
        context, SimpleNamespace(message_id=uuid.UUID(int=1), queued_at=clock.now()), "id-sintetico"
    )
    consumer = EmailDeliveryEventsConsumer(
        sqs=Mock(),
        queue_url=None,
        database=database,
        provider_context=context,
        contexts=Mock(context_from_event=Mock(return_value=context)),
        clock=clock,
    )
    consumer._apply_complaint = AsyncMock()
    consumer._apply_bounce = AsyncMock()
    found = SimpleNamespace(organization_id=context.organization_id)
    await consumer._apply(found, SimpleNamespace(is_complaint=False))
    await consumer._apply(found, SimpleNamespace(is_complaint=True))
    assert {
        p.attributes["email_state"]: p.value for p in capture.points("email_messages_total")
    } == {"sent": 1, "bounced": 1, "complained": 1}


@pytest.mark.asyncio
async def test_global_email_poll_task_emits_its_duration(capture: MetricCapture) -> None:
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))

    async def handler(run: Any) -> None:
        clock.advance(0.25)

    registry = PeriodicTaskRegistry()
    task = registry.register(
        "email_delivery_events_poll",
        Schedule.every(60),
        handler,
        unit=ActorUnit.U04,
        iteration=TaskIteration.GLOBAL,
    )
    telemetry = LoopWorkerTelemetry(database=Mock(), contexts=Mock(), clock=clock, task=task)
    scheduler = PeriodicScheduler(
        database=Mock(),
        registry=registry,
        leases=Mock(settings=LeaseSettings()),
        contexts=Mock(),
        clock=clock,
        owner="prueba",
        telemetry=telemetry,
    )
    holder = _Holder(SimpleNamespace(until=clock.now() + timedelta(seconds=60)))
    report = TaskRunReport(task.task_name, clock.now(), None)
    await scheduler._run_global(task, holder, report, None)
    (point,) = capture.points("loop_periodic_task_duration_ms")
    assert (dict(point.attributes), point.count, point.sum) == (
        {"task": "email_delivery_events_poll", "result": "succeeded"},
        1,
        250,
    )
