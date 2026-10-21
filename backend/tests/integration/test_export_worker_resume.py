"""El consumidor ``export_worker`` contra PostgreSQL 16 real (TASK-317; LC-LAZ-21; PAT-LAZ-RES-01;
NFR-LAZ-12; BR-LAZ-91 y 101; U04-H-08; FS-LAZ-05 sin señal real).

Como ``vigia_app``, con el compositor sintético determinista y dobles de los puertos
(``tests/export_support.py``). Las decisiones de renovar y de publicar el progreso salen del
``Clock`` simulado; el acompañante solo da una vuelta cuando la prueba la suelta (``StepSleep``):
ninguna prueba espera pared para decidir.

- **Retoma tras la muerte del proceso** (dos pools, como dos procesos): mientras A tiene el
  arrendamiento, B no la toma (``ExportBusy``) y **no existe paquete**; a los 60 s justos aún no,
  un milisegundo después sí: ``attempt_count`` 2, se **aborta la subida por partes de A** por su
  ``multipart_upload_id``, y ``generated_at`` y ``checkpoint_record_ids`` son los del primer
  intento (``write_checkpoints_now`` del doble, **una** llamada por solicitud): el paquete es el
  mismo que sin interrupción. A ya no puede renovar ni escribir (valla): su intento acaba en
  ``LeaseLost`` y no toca la solicitud de B.
- **Progreso**: al terminar cada artefacto y, sin terminar ninguno, al cumplirse 30 s del reloj
  inyectado (29,999 s no); renovación a los 20 s (19,999 s no).
- **Fallos**: uno no declarado libera el arrendamiento y deja ``in_progress``; la retoma compone
  desde cero. **Alarma una sola vez** al llegar al tercer intento tras dos fallos seguidos (y no al
  cuarto), o en el fallo declarado del segundo; un fallo declarado deja ``failed`` con su código y
  sin paquete (BR-LAZ-101), y la reentrega no hace nada.
- **De punta a punta**: ``POST`` → despachador (``export_worker``) → ``ready`` con el paquete;
  ``export_state`` nunca es ``composing``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.export_support import (
    CountingCheckpoints,
    ExportStack,
    RecordingAlarm,
    RecordingUploads,
    StepSleep,
    SyntheticComposer,
    Undeclared,
    export_body,
    export_stack,
    synthetic_package,
)
from tests.integration.conftest import PostgresEndpoint
from tests.laz_metrics_support import MetricCapture, metric_capture, unscoped_rows  # noqa: F401
from vigia_platform.accreditation.export.lease import ExportLease, LeaseLost
from vigia_platform.accreditation.export.worker import EXPORT_WORKER, ExportBusy
from vigia_platform.loop.domain.enums import ExportState, PackageForm
from vigia_platform.loop.observability.worker import _EXPORT, LoopWorkerTelemetry
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.outbox.dispatcher import Outcome
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry, Schedule

pytestmark = pytest.mark.integration

COVERAGE = "accreditation_coverage_unavailable"
STATES = {state.value for state in ExportState}


def _telemetry(stack: ExportStack) -> LoopWorkerTelemetry:
    async def idle(transaction: Any) -> None:
        pass

    task = PeriodicTaskRegistry().register(
        "evaluate_deadlines", Schedule.every(3600), idle, unit=ActorUnit.U04
    )
    return LoopWorkerTelemetry(
        database=stack.database, contexts=stack.authz.contexts, clock=stack.clock, task=task
    )


def test_export_producers_emit_states_duration_and_size(
    stack: ExportStack,
    capture: MetricCapture,
) -> None:
    organization, export_id = _requested(stack)

    class MeasuredComposer(SyntheticComposer):
        async def compose(self, job: Any, sink: Any) -> Any:
            stack.clock.advance(0.125)
            return await super().compose(job, sink)

    worker = stack.export_worker(composer=MeasuredComposer())
    stack.run(worker.run(stack.event_context(organization), export_id))
    stack.run(worker.run(stack.event_context(organization), export_id))
    points = capture.points("export_requests_total")
    assert {p.attributes["export_state"]: p.value for p in points} == {
        "requested": 1,
        "in_progress": 1,
        "ready": 1,
    }
    assert all(p.attributes["organization_id"] == str(organization) for p in points)
    (duration,) = capture.points("export_duration_ms")
    assert (duration.attributes["export_state"], duration.count, duration.sum) == ("ready", 1, 125)
    (size,) = capture.points("export_package_size_bytes")
    assert (size.count, size.sum) == (1, stack.package_row(export_id)["size_bytes"])


@pytest.mark.parametrize("terminal", [False, True], ids=["reintentable", "terminal"])
def test_export_monitor_checks_sixty_minutes_and_two_failures(
    stack: ExportStack,
    capture: MetricCapture,
    terminal: bool,
) -> None:
    organization, export_id = _requested(stack)
    telemetry = _telemetry(stack)
    requested_at = stack.export_row(export_id)["requested_at"]

    def flag() -> int:
        stack.run(telemetry.report_once())
        (point,) = [
            p
            for p in capture.points("export_stuck_or_failed")
            if p.attributes["organization_id"] == str(organization)
        ]
        return int(point.value)

    stack.clock.set(requested_at + timedelta(hours=1))
    assert flag() == 0
    stack.tick(0.001)
    assert flag() == 1
    worker = stack.export_worker()
    stack.run(worker.run(stack.event_context(organization), export_id))
    assert flag() == 0

    other_org, other_id = _requested(stack)
    other_context = stack.event_context(other_org)
    composer = (
        SyntheticComposer(fail_attempts={1}, declared={2: COVERAGE})
        if terminal
        else SyntheticComposer(fail_attempts={1, 2})
    )
    broken = stack.export_worker(composer=composer)
    with pytest.raises(Undeclared):
        stack.run(broken.run(other_context, other_id))
    stack.run(telemetry.report_once())
    assert (
        next(
            p.value
            for p in capture.points("export_stuck_or_failed")
            if p.attributes["organization_id"] == str(other_org)
        )
        == 0
    )
    if terminal:
        stack.run(broken.run(other_context, other_id))
    else:
        with pytest.raises(Undeclared):
            stack.run(broken.run(other_context, other_id))
    stack.run(telemetry.report_once())
    assert (
        next(
            p.value
            for p in capture.points("export_stuck_or_failed")
            if p.attributes["organization_id"] == str(other_org)
        )
        == 1
    )
    assert flag() == 0  # La otra organización no contamina la ya recuperada.
    # El predicado se prueba también sin RLS para no esconder su retirada detrás de la política.
    (row,) = stack.run(
        unscoped_rows(
            stack.authz.sessions.admin,
            _EXPORT,
            organization_id=organization,
            now=stack.now(),
            cutoff=stack.now() - timedelta(hours=1),
        )
    )
    assert row["unhealthy"] is False
    if not terminal:
        leases = stack.leases()
        acquisition = stack.run(leases.acquire(other_context, other_id, "prueba", stack.now()))
        assert acquisition.lease is not None and acquisition.lease.attempt_count == 3
        stack.run(telemetry.report_once())
        assert (
            next(
                p.value
                for p in capture.points("export_stuck_or_failed")
                if p.attributes["organization_id"] == str(other_org)
            )
            == 1
        )
        stack.run(leases.release(other_context, acquisition.lease))
        stack.run(stack.export_worker().run(other_context, other_id))
        stack.run(telemetry.report_once())
        assert (
            next(
                p.value
                for p in capture.points("export_stuck_or_failed")
                if p.attributes["organization_id"] == str(other_org)
            )
            == 0
        )


def test_declared_failure_is_observed_without_a_package(
    stack: ExportStack,
    capture: MetricCapture,
) -> None:
    organization, export_id = _requested(stack)
    worker = stack.export_worker(composer=SyntheticComposer(declared={1: COVERAGE}))
    stack.run(worker.run(stack.event_context(organization), export_id))
    assert {
        p.attributes["export_state"]: p.value for p in capture.points("export_requests_total")
    } == {"requested": 1, "in_progress": 1, "failed": 1}
    assert capture.points("export_package_size_bytes") == []
    (duration,) = capture.points("export_duration_ms")
    assert (duration.attributes["export_state"], duration.count) == ("failed", 1)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExportStack]:
    with export_stack(postgres_endpoint, "export_worker") as built:
        yield built


def _requested(stack: ExportStack) -> tuple[uuid.UUID, uuid.UUID]:
    """Una exportación admitida por HTTP: ``(organización, export_id)``."""
    site = stack.site(plants=1, zones=2)
    plant = next(iter(site.plants))
    person = stack.member(site)
    start = stack.now() - timedelta(days=40)
    response = stack.request_export(
        person, export_body(plant, list(site.plants[plant]), start=start)
    )
    assert response.status_code == 202, response.text
    return site.organization_id, uuid.UUID(response.json()["export_id"])


def _state(stack: ExportStack, export_id: uuid.UUID) -> Any:
    return _checked(stack.export_row(export_id))


async def _astate(stack: ExportStack, export_id: uuid.UUID) -> Any:
    """``_state`` dentro de un escenario que ya corre en el bucle de la base."""
    return _checked(await stack.export_row_now(export_id))


def _checked(row: Any) -> Any:
    assert row["state"] in STATES and row["state"] != "composing"
    return row


# --- retoma tras la muerte del proceso ------------------------------------------------------------


def test_another_process_takes_over_after_expiry_and_resumes_from_scratch(
    stack: ExportStack,
) -> None:
    organization, export_id = _requested(stack)
    checkpoints, uploads = CountingCheckpoints(), RecordingUploads()
    first = SyntheticComposer(pause_at=(1, 1))
    second = SyntheticComposer()
    a = stack.export_worker(
        composer=first, checkpoints=checkpoints, uploads=uploads, database=stack.new_database()
    )
    b = stack.export_worker(
        composer=second, checkpoints=checkpoints, uploads=uploads, database=stack.new_database()
    )
    started = stack.now()

    async def scenario() -> None:
        dying = asyncio.create_task(a.run(stack.event_context(organization), export_id))
        await asyncio.wait_for(first.paused.wait(), 60)
        row = await _astate(stack, export_id)
        assert (row["state"], row["attempt_count"], row["lease_owner"]) == (
            "in_progress",
            1,
            a.owner,
        )
        assert row["multipart_upload_id"] == f"upload-{export_id}-1"
        assert row["lease_until"] == started + timedelta(seconds=60)
        # Mientras A tiene el arrendamiento, nadie más la compone y no hay paquete.
        with pytest.raises(ExportBusy):
            await b.run(stack.event_context(organization), export_id)
        stack.clock.set(started + timedelta(seconds=60))
        with pytest.raises(ExportBusy):
            await b.run(stack.event_context(organization), export_id)
        assert await stack.package_row_now(export_id) is None
        assert (await _astate(stack, export_id))["attempt_count"] == 1
        # A muere sin liberar nada: el arrendamiento vence y B la retoma desde el principio.
        stack.clock.set(started + timedelta(seconds=60, milliseconds=1))
        await b.run(stack.event_context(organization), export_id)
        # A ya no puede renovar, ni publicar su progreso al seguir.
        stale = _lease(export_id, a.owner, organization)
        assert (
            await stack.leases().renew(stack.event_context(organization), stale, stack.now())
            is None
        )
        first.resume.set()
        with pytest.raises(LeaseLost):
            await asyncio.wait_for(dying, 60)

    stack.run(scenario())
    row = _state(stack, export_id)
    assert (row["state"], row["attempt_count"], row["lease_owner"]) == ("ready", 2, None)
    assert row["multipart_upload_id"] is None and row["progress_percent"] == 100
    assert uploads.aborted == [f"upload-{export_id}-1"]
    assert len(checkpoints.calls) == 1
    (job_a,), (job_b,) = first.jobs, second.jobs
    assert (job_a.attempt, job_b.attempt) == (1, 2)
    assert job_b.generated_at == job_a.generated_at == row["generated_at"]
    assert job_b.checkpoint_record_ids == job_a.checkpoint_record_ids
    assert list(job_a.checkpoint_record_ids) == list(row["checkpoint_record_ids"])
    # El paquete de la retoma es el de una ejecución sin interrupción.
    package = stack.package_row(export_id)
    assert package["archive_sha256"] == synthetic_package(job_a).archive_sha256
    assert package["document_sha256"] == synthetic_package(job_a).document_sha256
    assert list(package["checkpoint_record_ids"]) == list(job_a.checkpoint_record_ids)


def _lease(export_id: uuid.UUID, owner: str, organization: uuid.UUID) -> ExportLease:
    """El arrendamiento que un proceso cree tener: la valla solo mira solicitud y dueño."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    return ExportLease(
        export_id=export_id,
        owner=owner,
        until=moment,
        attempt_count=1,
        previous_upload_id=None,
        checkpoint_record_ids=None,
        generated_at=None,
        organization_id=organization,
        plant_id=organization,
        zone_ids=(),
        period_from=moment,
        period_to=moment,
        package_form=PackageForm.WITHOUT_CLIPS,
        correlation_id=export_id,
    )


# --- progreso y renovación con el reloj inyectado -----------------------------------------------


def test_progress_after_each_artifact_and_every_30_s_and_renewal_every_20_s(
    stack: ExportStack,
) -> None:
    organization, export_id = _requested(stack)
    composer = SyntheticComposer(pause_at=(1, 1))
    sleeper = StepSleep()
    worker = stack.export_worker(composer=composer, sleep=sleeper)
    started = stack.now()

    async def scenario() -> None:
        running = asyncio.create_task(worker.run(stack.event_context(organization), export_id))
        await asyncio.wait_for(composer.paused.wait(), 60)
        row = await _astate(stack, export_id)
        # Al terminar el primer artefacto: publicado.
        assert (row["progress_percent"], row["current_artifact"]) == (33, "document")
        assert row["progress_updated_at"] == started
        lease_until = row["lease_until"]
        for seconds, renewed, published in (
            (19.999, False, False),
            (20.0, True, False),
            (29.999, False, False),
            (30.0, False, True),
            (40.0, True, False),
        ):
            stack.clock.set(started + timedelta(seconds=seconds))
            await sleeper.step()
            row = await _astate(stack, export_id)
            moment = started + timedelta(seconds=seconds)
            if renewed:
                assert row["lease_until"] == moment + timedelta(seconds=60), seconds
                lease_until = row["lease_until"]
            else:
                assert row["lease_until"] == lease_until, seconds
            if published:
                assert row["progress_updated_at"] == moment, seconds
                # Sin terminar ninguno, se publica el artefacto en curso con el mismo porcentaje.
                assert (row["progress_percent"], row["current_artifact"]) == (33, "findings_json")
            elif seconds < 30:
                assert row["progress_updated_at"] == started, seconds
        composer.resume.set()
        await asyncio.wait_for(running, 60)

    stack.run(scenario())
    row = _state(stack, export_id)
    assert (row["state"], row["progress_percent"], row["current_artifact"]) == ("ready", 100, None)


# --- fallos y alarma ----------------------------------------------------------------------------


def test_the_alarm_fires_once_after_two_failures_in_a_row(stack: ExportStack) -> None:
    organization, export_id = _requested(stack)
    composer = SyntheticComposer(fail_attempts={1, 2, 3})
    checkpoints, uploads, alarm = CountingCheckpoints(), RecordingUploads(), RecordingAlarm()
    worker = stack.export_worker(
        composer=composer, checkpoints=checkpoints, uploads=uploads, alarm=alarm
    )
    raised_after: list[int] = []
    for attempt in (1, 2, 3):
        with pytest.raises(Undeclared):
            stack.run(worker.run(stack.event_context(organization), export_id))
        row = _state(stack, export_id)
        # Fallo no declarado: libera, sigue in_progress, sin paquete; se retoma desde cero.
        assert (row["state"], row["attempt_count"], row["lease_owner"]) == (
            "in_progress",
            attempt,
            None,
        )
        assert stack.package_row(export_id) is None
        raised_after.append(len(alarm.raised))
        stack.tick(1)
    stack.run(worker.run(stack.event_context(organization), export_id))
    assert raised_after == [0, 0, 1]
    (alarm_raised,) = alarm.raised
    assert alarm_raised["export_id"] == export_id and alarm_raised["attempt_count"] == 3
    row = _state(stack, export_id)
    assert (row["state"], row["attempt_count"]) == ("ready", 4)
    assert uploads.aborted == [f"upload-{export_id}-{n}" for n in (1, 2, 3)]
    assert len(checkpoints.calls) == 1
    assert len({job.generated_at for job in composer.jobs}) == 1


def test_a_declared_failure_fails_without_package_and_alarms_on_the_second_attempt(
    stack: ExportStack,
) -> None:
    organization, export_id = _requested(stack)
    composer = SyntheticComposer(fail_attempts={1}, declared={2: COVERAGE})
    alarm = RecordingAlarm()
    worker = stack.export_worker(composer=composer, alarm=alarm)
    with pytest.raises(Undeclared):
        stack.run(worker.run(stack.event_context(organization), export_id))
    stack.run(worker.run(stack.event_context(organization), export_id))
    row = _state(stack, export_id)
    assert (row["state"], row["failure_code"], row["attempt_count"]) == ("failed", COVERAGE, 2)
    assert row["lease_owner"] is None and row["completed_at"] is not None
    assert stack.package_row(export_id) is None
    assert len(alarm.raised) == 1
    # La reentrega del evento no hace nada: la solicitud ya terminó.
    stack.run(worker.run(stack.event_context(organization), export_id))
    assert len(alarm.raised) == 1 and len(composer.jobs) == 2
    assert _state(stack, export_id)["attempt_count"] == 2


def test_a_declared_failure_on_the_first_attempt_does_not_alarm(stack: ExportStack) -> None:
    organization, export_id = _requested(stack)
    alarm = RecordingAlarm()
    worker = stack.export_worker(composer=SyntheticComposer(declared={1: COVERAGE}), alarm=alarm)
    stack.run(worker.run(stack.event_context(organization), export_id))
    row = _state(stack, export_id)
    assert (row["state"], row["failure_code"]) == ("failed", COVERAGE)
    assert alarm.raised == []


def test_one_export_in_progress_per_organization(stack: ExportStack) -> None:
    site = stack.site(plants=1, zones=1)
    plant = next(iter(site.plants))
    person = stack.member(site)
    body = export_body(plant, list(site.plants[plant]), start=stack.now() - timedelta(days=5))
    first = uuid.UUID(stack.request_export(person, body).json()["export_id"])
    second = uuid.UUID(stack.request_export(person, body).json()["export_id"])
    composer = SyntheticComposer(pause_at=(1, 0))
    holder = stack.export_worker(composer=composer, database=stack.new_database())
    other = stack.export_worker()
    foreign_organization, foreign = _requested(stack)

    async def scenario() -> None:
        running = asyncio.create_task(holder.run(stack.event_context(site.organization_id), first))
        await asyncio.wait_for(composer.paused.wait(), 60)
        with pytest.raises(ExportBusy):
            await other.run(stack.event_context(site.organization_id), second)
        # Otra organización no espera.
        await other.run(stack.event_context(foreign_organization), foreign)
        composer.resume.set()
        await asyncio.wait_for(running, 60)
        await other.run(stack.event_context(site.organization_id), second)

    stack.run(scenario())
    assert _state(stack, first)["state"] == _state(stack, second)["state"] == "ready"
    assert _state(stack, foreign)["state"] == "ready"


# --- de punta a punta ---------------------------------------------------------------------------


def test_from_request_to_ready_through_the_dispatcher(stack: ExportStack) -> None:
    organization, export_id = _requested(stack)
    composer = SyntheticComposer()
    stack.worker = stack.export_worker(composer=composer)
    dispatcher = stack.dispatcher()
    report = stack.run(dispatcher.dispatch_once(EXPORT_WORKER))
    assert report.count(Outcome.DELIVERED) >= 1, stack.fetch(
        "SELECT status, last_error_code FROM shared.outbox_delivery WHERE organization_id = $1",
        organization,
    )
    row = _state(stack, export_id)
    assert (row["state"], row["attempt_count"]) == ("ready", 1)
    (job,) = [job for job in composer.jobs if job.export_id == export_id]
    assert job.organization_id == organization
    assert stack.package_row(export_id)["archive_sha256"] == synthetic_package(job).archive_sha256
    # La reentrega (o una segunda ronda) no compone otra vez.
    stack.run(dispatcher.dispatch_once(EXPORT_WORKER))
    assert len([job for job in composer.jobs if job.export_id == export_id]) == 1
