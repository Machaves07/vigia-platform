"""PR-LAZ-48 (TASK-326): las diez tareas periódicas de U-04 son idempotentes con huecos de reloj
y tras una caída de 48 h (NFR-LAZ-13 y 14; PAT-LAZ-RES-05; LC-LAZ-11, 26, 27, 29 y 32).

Cada propiedad genera una **secuencia de avances del reloj con huecos de días** (``Timeline``) y
la recorre con tres organizaciones equivalentes sobre el mismo mundo y el **mismo reloj
inyectado** (retro 15: nada se compara con la hora de la base):

- ``once``: la tarea corre una vez en cada paso (la ejecución continua);
- ``twice``: corre dos veces seguidas en cada paso (un arrendamiento vencido, una reentrega);
- ``outage``: deja de correr 48 h desde un paso generado y vuelve después.

Las tres terminan con **el mismo estado final** (registros, colas, marcas, proyecciones y
notificaciones), con la clave de idempotencia de PAT-LAZ-RES-05 de cada tarea:

=========================== =========================================== ==========================
Tarea                       Clave                                       Qué se compara
=========================== =========================================== ==========================
``evaluate_deadlines``      ``UUID v5 (action_id, due_at, level)``      ``action_overdue`` y sus
                                                                        eventos, cada uno con la
                                                                        marca de su propio día
``review_reminders``        ventana de ``reminder_interval_hours``      ver la decisión de abajo
``unclosable_review_due``   ``(declaration_id, review_due_at)``         una marca y un evento por
                                                                        declaración
``monthly_package``         ``(zone_id, periodo)`` sellado              periodos sellados, un
                                                                        ``monthly_package_produced``
                                                                        y un aviso por periodo
``metrics_materialize``     ``(zone_id, periodo)`` definitivo           mismos valores; el
                                                                        definitivo no se reescribe
``adoption_check``          ``(zone_id, inicio de la racha)``           una alerta y un aviso por
                                                                        racha
``email_grouping``          ventana del lote (``email_grouping_hours``) cada aviso en un solo
                                                                        lote; un envío por ventana
``digest_weekly``           ventana del lote (semana)                   ídem, por semana
``verify_loop_projection``  ``(organization_id, plant_id, día)``        una divergencia, una
                                                                        reconstrucción y la misma
                                                                        proyección final
``email_delivery_events``   identificador de mensaje del proveedor      mismas marcas de rebote,
_poll                                                                   queja y supresión
=========================== =========================================== ==========================

**Recordatorios de revisión (decisión del dueño, VIG-241, 2026-10-20; manda U04-H-07).** Tras la
caída sale **un** recordatorio real y los siguientes respetan ``reminder_interval_hours`` desde
ese envío: no se recuperan de golpe los omitidos ni se anotan envíos que no ocurrieron. La
propiedad compara el estado final (el elemento sigue en la cola) pero **no** el número de
envíos; para los envíos comprueba que ningún par de la misma revisión queda a menos del
intervalo, que ``reminders_sent`` cuenta envíos reales y que la vuelta envía exactamente uno.

**Lo que la caída cambia por diseño** (y la propiedad no iguala): la agrupación de correo emite
**un solo envío** con lo acumulado, así que la caída puede dejar menos lotes que la continua (nunca
una ráfaga); ``adoption_check`` publica la racha una vez con los días transcurridos hasta la
vuelta; ``verify_loop_projection`` no inventa muestras de los días que no corrió; y las marcas de
tiempo de supresión llevan la hora en que se procesó el aviso del proveedor.

``rebuild_loop_projection`` no es periódica (U04-H-14): su exclusión la prueba PR-LAZ-51.
``email_delivery_events_poll`` solo publica el pulso; lo que se repite es la ronda del
consumidor ``email_delivery_events``, que es lo que se ejecuta aquí.

Propiedades con base: marca ``integration`` (reparto de las pruebas de integración). Solo datos
generados.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import uuid
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.adoption_support import AdoptionStack, adoption_stack
from tests.exposure_support import ExposureStack, exposure_stack
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.integration.test_loop_projection_rebuild import _diverge, _rebuilder, _verifier
from tests.integration.test_monthly_package_task import World as MonthlyWorld
from tests.laz_support import IngestedSubject, LoopWorld, loop_world
from tests.loop_deadlines_support import DeadlinesStack, deadlines_stack
from tests.loop_review_queue_support import ReviewQueueStack, review_queue_stack
from tests.loop_unclosable_support import UnclosableStack, unclosable_stack
from tests.notification_support import NotificationWorld, Senders, StubSender, notification_world
from tests.properties.laz.test_pr_laz_48_deadlines_catch_up import (
    _expected,
    _local_hour,
    _normalized,
)
from tests.properties.laz.test_pr_laz_58_bounces_complaints import (
    FakeSqsClient,
    _bounce_body,
    _complaint_body,
    _email_row,
    _message_id_of,
    _preference_row,
    _run_once,
    _setup,
)
from tests.signing_support import bootstrapped_world
from vigia_platform.accreditation.metrics.service import ProgramMetricsService
from vigia_platform.accreditation.metrics.task import MetricsMaterialize
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.loop.application.review_queue import ReviewQueueReminder
from vigia_platform.loop.config.parameters import INTEGER_PARAMETERS
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.notifications.digest import WEEKLY_MANAGER_KIND, DigestWeeklyTask
from vigia_platform.notifications.grouping import (
    EMAIL_GROUPED_KIND,
    DigestGrouperBase,
    EmailGroupingTask,
)
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.outbox.registries import Schedule
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

ONCE: Final = "once"
TWICE: Final = "twice"
OUTAGE: Final = "outage"
MODES: Final = (ONCE, TWICE, OUTAGE)
HOUR: Final = timedelta(hours=1)
DAY: Final = timedelta(days=1)
OUTAGE_SPAN: Final = timedelta(hours=48)
EXAMPLES: Final = 3
"""Cada ejemplo crea tres organizaciones y corre la tarea decenas de veces contra la base real;
el perfil ``nightly`` no sube este número."""
HEAVY_EXAMPLES: Final = 2
"""``monthly_package`` compone, firma y sube un paquete por periodo y organización."""
SLOW: Final = [HealthCheck.too_slow, HealthCheck.function_scoped_fixture]


class Harness[T]:
    """El mundo de una propiedad, sin volcar la infraestructura ni las credenciales sintéticas en
    el contraejemplo de Hypothesis (que imprime campo a campo cualquier ``dataclass``)."""

    def __init__(self, built: T) -> None:
        self.built = built

    def __repr__(self) -> str:
        return f"Harness({type(self.built).__name__}, sintético)"


# --- la secuencia de reloj y el recorrido común ---------------------------------------------------


@dataclass(frozen=True)
class Timeline:
    steps: tuple[int, ...]
    """Horas entre pasadas; un paso de 24 o más es un hueco de días."""
    down_at: int
    """El paso en que la organización ``outage`` deja de correr 48 h."""


def timelines(hours: Sequence[int], *, min_size: int = 3, max_size: int = 7) -> Any:
    return st.lists(st.sampled_from(hours), min_size=min_size, max_size=max_size).flatmap(
        lambda steps: st.builds(Timeline, st.just(tuple(steps)), st.integers(0, len(steps) - 1))
    )


@dataclass
class Trace:
    """El recorrido: cuándo cayó ``outage``, cuántas pasadas se saltó y cuándo volvió."""

    down_from: datetime | None = None
    skipped: int = 0
    recovered_at: datetime | None = None
    runs: dict[str, list[datetime]] = field(default_factory=lambda: {m: [] for m in MODES})


def drive(
    timeline: Timeline,
    *,
    now: Callable[[], datetime],
    advance: Callable[[timedelta], None],
    run: Callable[[str], None],
    at_step: Callable[[int], None] | None = None,
) -> Trace:
    """Recorre ``timeline``: en cada paso, ``once`` corre una vez, ``twice`` dos veces seguidas y
    ``outage`` solo si no está dentro de su caída. Al final, si hace falta, avanza hasta cumplir
    las 48 h de la caída y corren las tres (la vuelta)."""
    trace = Trace()

    def everyone(down: bool) -> None:
        for mode in (ONCE, TWICE, TWICE):
            trace.runs[mode].append(now())
            run(mode)
        if down:
            trace.skipped += 1
            return
        if trace.skipped and trace.recovered_at is None:
            trace.recovered_at = now()
        trace.runs[OUTAGE].append(now())
        run(OUTAGE)

    for index, hours in enumerate(timeline.steps):
        advance(hours * HOUR)
        if at_step is not None:
            at_step(index)
        if index == timeline.down_at:
            trace.down_from = now()
        down = trace.down_from is not None and now() - trace.down_from < OUTAGE_SPAN
        everyone(down)
    assert trace.down_from is not None
    advance(max(trace.down_from + OUTAGE_SPAN - now(), HOUR))
    if at_step is not None:
        at_step(len(timeline.steps))
    everyone(False)
    assert trace.skipped > 0 and trace.recovered_at is not None
    return trace


def _outbox_names(fetch: Callable[..., list[Any]], organization_id: uuid.UUID) -> Counter[str]:
    return Counter(
        str(row["event_name"])
        for row in fetch(
            "SELECT event_name FROM shared.outbox_event WHERE organization_id = $1",
            organization_id,
        )
    )


# --- 1. evaluate_deadlines ------------------------------------------------------------------------


@pytest.fixture
def deadlines(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness[DeadlinesStack]]:
    with deadlines_stack(postgres_endpoint, "pr_laz_48_ten_deadlines") as built:
        yield Harness(built)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((1, 5, 24, 49)),
    deadline_days=st.lists(st.integers(1, 3), min_size=1, max_size=2),
    highlight_days=st.integers(1, 2),
)
def test_pr_laz_48_evaluate_deadlines(
    deadlines: Harness[DeadlinesStack],
    timeline: Timeline,
    deadline_days: list[int],
    highlight_days: int,
) -> None:
    stack = deadlines.built
    world = stack.actions.world
    organizations = {mode: world.organization() for mode in MODES}
    for organization in organizations.values():
        stack.set_highlight_days(organization, highlight_days)
    if _local_hour(stack, organizations[ONCE]) >= 21:
        stack.tick(5 * 3600)  # las acciones de los tres en el mismo día local
    people = {
        mode: (stack.actions.member(o), stack.actions.user(o)) for mode, o in organizations.items()
    }
    actions: dict[str, list[uuid.UUID]] = {mode: [] for mode in MODES}
    due_dates: list[datetime] = []
    for days in deadline_days:
        dues = set()
        for mode, organization in organizations.items():
            _, action_id, due_at = stack.with_action(organization, *people[mode], days)
            actions[mode].append(action_id)
            dues.add(due_at)
        (due_at,) = dues
        due_dates.append(due_at)

    stack.tick_to(min(due_dates) - HOUR)
    trace = drive(
        timeline,
        now=stack.now,
        advance=lambda delta: stack.tick(delta.total_seconds()),
        run=lambda mode: stack.evaluate(organizations[mode]),
    )
    stack.tick_to(max(due_dates) + highlight_days * DAY + HOUR)
    for mode in MODES:
        stack.evaluate(organizations[mode])

    expected = _expected(due_dates, highlight_days)
    for mode, organization in organizations.items():
        assert _normalized(stack, organization, actions[mode]) == expected, (mode, trace)
    names = {
        mode: {
            name: count
            for name, count in _outbox_names(stack.fetch, o.organization_id).items()
            if name in ("finding_overdue", "finding_escalated")
        }
        for mode, o in organizations.items()
    }
    assert names[ONCE] == names[TWICE] == names[OUTAGE], names
    world.stack.run(world.stack.release())


# --- 2. review_reminders --------------------------------------------------------------------------


@pytest.fixture
def reviews(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness[ReviewQueueStack]]:
    with review_queue_stack(postgres_endpoint, "pr_laz_48_ten_reminders") as built:
        yield Harness(built)


REMINDER_INTERVAL: Final = timedelta(hours=INTEGER_PARAMETERS["reminder_interval_hours"].default)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(timeline=timelines((1, 6, 23, 24, 25, 49)))
def test_pr_laz_48_review_reminders_follow_the_interval_rule(
    reviews: Harness[ReviewQueueStack], timeline: Timeline
) -> None:
    """La decisión del dueño (U04-H-07): mismo estado final; para los envíos, ningún par a menos
    de ``reminder_interval_hours``, ``reminders_sent`` igual a los envíos reales, ``once`` y
    ``twice`` idénticos, y la vuelta de la caída envía exactamente uno (el elemento ya estaba
    vencido y su último envío, si lo hubo, es anterior a la caída)."""
    stack = reviews.built
    clock = stack.world.stack.authz.sessions.clock
    organizations = {mode: stack.world.organization() for mode in MODES}
    subjects = {mode: stack.in_review(o) for mode, o in organizations.items()}
    rows = {mode: stack.queue_row(s) for mode, s in subjects.items()}
    due = max(row["review_due_at"] for row in rows.values() if row is not None)
    stack.tick(max((due - clock.now()).total_seconds(), 0.0))
    reminder = ReviewQueueReminder(
        repository=stack.repository,
        configuration=PostgresLoopConfigurationRepository(stack.database),
        outbox=stack.world.stack.outbox,
        clock=clock,
    )
    sends: dict[str, list[datetime]] = {mode: [] for mode in MODES}

    def run(mode: str) -> None:
        async def execute() -> Any:
            context = stack.world.event_context(organizations[mode])
            async with stack.database.transaction(context) as transaction:
                return await reminder.remind(transaction)

        sends[mode].extend(clock.now() for _ in stack.run(execute()))

    trace = drive(
        timeline,
        now=clock.now,
        advance=lambda delta: stack.tick(delta.total_seconds()),
        run=run,
    )

    assert sends[ONCE] == sends[TWICE], (sends, trace)
    for mode, subject in subjects.items():
        row = stack.queue_row(subject)
        assert row is not None and row["state"] == "pending", mode  # sigue en la cola
        projected = stack.world.finding_state(subject)
        assert projected is not None and projected["lifecycle_state"] == "in_review", mode
        events = stack.overdue_events(organizations[mode], subject.subject_id)
        assert row["reminders_sent"] == len(sends[mode]) == len(events), mode
        assert [json.loads(e["payload"])["reminders_sent"] for e in events] == list(
            range(1, len(events) + 1)
        )
        gaps = [later - earlier for earlier, later in itertools.pairwise(sends[mode])]
        assert all(gap >= REMINDER_INTERVAL for gap in gaps), (mode, sends[mode])
    assert trace.down_from is not None and trace.recovered_at is not None
    assert not [t for t in sends[OUTAGE] if trace.down_from <= t < trace.recovered_at]
    assert sends[OUTAGE].count(trace.recovered_at) == 1, (sends[OUTAGE], trace)


# --- 3. unclosable_review_due ---------------------------------------------------------------------


@pytest.fixture
def unclosable(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness[UnclosableStack]]:
    with unclosable_stack(postgres_endpoint, "pr_laz_48_ten_unclosable") as built:
        yield Harness(built)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((1, 6, 24, 49)),
    review_hours=st.lists(st.integers(1, 96), min_size=1, max_size=2),
)
def test_pr_laz_48_unclosable_review_due(
    unclosable: Harness[UnclosableStack], timeline: Timeline, review_hours: list[int]
) -> None:
    stack = unclosable.built
    organizations = {mode: stack.world.organization() for mode in MODES}
    declared: dict[str, list[tuple[IngestedSubject, Any]]] = {mode: [] for mode in MODES}
    people = {}
    for mode, organization in organizations.items():
        stack.signed_policy(organization, version=len(stack.policy_rows(organization)) + 1)
        people[mode] = (stack.member(organization), stack.user(organization))
    subjects = {mode: [stack.opened(organizations[mode]) for _ in review_hours] for mode in MODES}
    origin = stack.now() + HOUR
    review_dates = [format_timestamp(origin + hours * HOUR) for hours in review_hours]
    for mode in MODES:
        coordinator, responsible = people[mode]
        for subject, review_due_at in zip(subjects[mode], review_dates, strict=True):
            body = stack.declaration_body(responsible, review_due_at=review_due_at)
            response = stack.declare(coordinator, subject, body)
            assert response.status_code == 201, response.text
            declared[mode].append((subject, uuid.UUID(response.json()["declaration_id"])))

    trace = drive(
        timeline,
        now=stack.now,
        advance=lambda delta: stack.tick(delta.total_seconds()),
        run=lambda mode: stack.run(stack.mark_due_async(organizations[mode])),
    )
    last = max(origin + hours * HOUR for hours in review_hours)
    if stack.now() <= last:
        stack.tick((last - stack.now()).total_seconds() + 60)
        for mode in MODES:
            stack.run(stack.mark_due_async(organizations[mode]))

    outcome = {}
    for mode, organization in organizations.items():
        events = stack.due_events(organization)
        by_declaration = Counter(e["payload"]["declaration_id"] for e in events)
        marks = []
        for index, (subject, declaration_id) in enumerate(declared[mode]):
            (row,) = stack.declaration_rows(subject)
            projected = stack.world.finding_state(subject)
            assert projected is not None
            # Marca ``pending_review`` una sola vez y **no** cambia el estado del ciclo de vida.
            assert projected["lifecycle_state"] == "unclosable", mode
            marks.append(
                (
                    index,
                    row["state"],
                    projected["unclosable_pending_review"],
                    by_declaration[str(declaration_id)],
                )
            )
        outcome[mode] = marks
    expected = [(index, "pending_review", True, 1) for index in range(len(review_hours))]
    assert outcome[ONCE] == outcome[TWICE] == outcome[OUTAGE] == expected, (outcome, trace)


# --- 4. monthly_package ---------------------------------------------------------------------------

_MONTHLY_BASELINE: Final = datetime(2026, 1, 1, tzinfo=UTC)
"""Como en ``test_monthly_package_task``: el certificado del nodo sintético cubre 2026."""


@pytest.fixture
def monthly(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[Harness[MonthlyWorld]]:
    with loop_world(postgres_endpoint, "pr_laz_48_ten_monthly") as loop:
        signing = loop.stack.run(bootstrapped_world())
        sessions = loop.stack.authz.sessions
        store = SqlCheckpointStore(
            database=loop.stack.primary.database,
            writer=loop.stack.primary.writer,
            audit=sessions.audit,
            outbox=loop.stack.outbox,
        )
        checkpoints = CheckpointService(
            store=store,
            signer=signing.service,
            clock=signing.clock,
            writer_context=loop.stack.authz.contexts.checkpoint_writer,
        )
        with versioned_bucket(localstack_endpoint.aws_client("s3"), "vigia-pr48") as bucket:
            yield Harness(
                MonthlyWorld(
                    loop=loop,
                    checkpoints=checkpoints,
                    config=PostgresLoopConfigurationRepository(loop.stack.primary.database),
                    repository=MonthlyPackageRepository(database=loop.stack.primary.database),
                    evidence=S3Storage(
                        localstack_endpoint.storage_settings(bucket), sessions.clock
                    ),
                    signer=signing.service,
                )
            )


@settings(max_examples=HEAVY_EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(timeline=timelines((24, 48, 120, 216), min_size=3, max_size=6))
def test_pr_laz_48_monthly_package(monthly: Harness[MonthlyWorld], timeline: Timeline) -> None:
    world = monthly.built
    world.clock.set(_MONTHLY_BASELINE)
    organizations = {mode: world.loop.organization() for mode in MODES}
    for organization in organizations.values():
        world.configure(organization)
        world.grant_coordinator(organization)
    world.clock.set(datetime(2026, 5, 10, tzinfo=UTC))
    for organization in organizations.values():
        world.classify(world.loop.ingest_finding(organization), outcome="confirmed")
    world.clock.set(datetime(2026, 5, 31, 6, tzinfo=UTC))  # tres pasos de 24 h cruzan junio local
    task = world.task(max_catchup_months=1)  # abril y mayo: el atraso desde enero no aporta

    def run(mode: str) -> None:
        async def execute() -> None:
            context = world.loop.event_context(organizations[mode])
            async with world.database.transaction(context) as transaction:
                await task(transaction)

        world.loop.stack.run(execute())

    trace = drive(
        timeline,
        now=world.clock.now,
        advance=lambda delta: world.clock.advance(delta.total_seconds()),
        run=run,
    )

    outcome = {}
    for mode, organization in organizations.items():
        sealed = [
            (row["period"].strftime("%Y-%m"), row["state"])
            for row in world.sealed_rows(organization.zone_id)
        ]
        produced = world.produced_periods(organization.zone_id)
        available = Counter(
            json.loads(row["payload"])["period"]
            for row in world.loop.stack.fetch(
                "SELECT payload::text AS payload FROM shared.outbox_event"
                " WHERE organization_id = $1 AND event_name = 'monthly_package_available'"
                " AND payload ->> 'zone_id' = $2",
                organization.organization_id,
                str(organization.zone_id),
            )
        )
        assert all(state == "sealed" for _, state in sealed), (mode, sealed)
        # Un paquete sellado no se vuelve a sellar: un registro y un aviso por periodo, en orden.
        assert produced == sorted(set(produced)) == [period for period, _ in sealed], mode
        assert set(available.values()) <= {1} and sorted(available) == produced, mode
        outcome[mode] = produced
    assert "2026-05" in outcome[ONCE]
    assert outcome[ONCE] == outcome[TWICE] == outcome[OUTAGE], (outcome, trace)


# --- 5. metrics_materialize -----------------------------------------------------------------------


@contextlib.contextmanager
def _ledger_follows_the_clock(built: ExposureStack) -> Iterator[Callable[[], None]]:
    """Como ``test_metrics_routes``: solo en esta base desechable, el disparador de la cadena y
    las recepciones leen una copia del ``Clock`` inyectado (un solo reloj); devuelve con qué
    sincronizarla."""
    (row,) = built.fetch("SELECT pg_get_functiondef('ledger.vigia_chain_link()'::regprocedure)")
    original = row[0]
    assert "clock_timestamp()" in original
    built.execute("CREATE TABLE ledger.metrics_test_clock (at timestamptz NOT NULL)")
    built.execute("INSERT INTO ledger.metrics_test_clock VALUES ($1)", built.now())
    built.execute(
        "CREATE FUNCTION ledger.metrics_test_now() RETURNS timestamptz LANGUAGE sql"
        " STABLE SECURITY DEFINER SET search_path=pg_catalog"
        " AS 'SELECT at FROM ledger.metrics_test_clock'"
    )
    built.execute(original.replace("clock_timestamp()", "ledger.metrics_test_now()"))
    built.execute(
        "ALTER TABLE ledger.ledger_record ALTER COLUMN received_at"
        " SET DEFAULT date_trunc('milliseconds', ledger.metrics_test_now())"
    )

    def sync() -> None:
        built.execute("UPDATE ledger.metrics_test_clock SET at=$1", built.now())

    try:
        yield sync
    finally:
        built.execute(original)
        built.execute(
            "ALTER TABLE ledger.ledger_record ALTER COLUMN received_at"
            " SET DEFAULT date_trunc('milliseconds', clock_timestamp())"
        )
        built.execute("DROP FUNCTION ledger.metrics_test_now()")
        built.execute("DROP TABLE ledger.metrics_test_clock")


class MetricsHarness:
    """Como ``Harness``, con la sincronización del reloj de la base."""

    def __init__(self, stack: ExposureStack, sync: Callable[[], None]) -> None:
        self.stack = stack
        self.sync = sync

    def __repr__(self) -> str:
        return "MetricsHarness(sintético)"


@pytest.fixture
def metrics(
    postgres_endpoint: PostgresEndpoint, monkeypatch: pytest.MonkeyPatch
) -> Iterator[MetricsHarness]:
    with (
        exposure_stack(postgres_endpoint, "pr_laz_48_ten_metrics") as built,
        _ledger_follows_the_clock(built) as sync,
    ):
        ingest = built.world.stack
        original_tick = type(ingest).tick

        def tick(self: Any, seconds: float = 1.0) -> None:
            original_tick(self, seconds)
            sync()

        monkeypatch.setattr(type(ingest), "tick", tick)
        yield MetricsHarness(built, sync)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((24, 72, 168, 288), min_size=3, max_size=6),
    finding_steps=st.sets(st.integers(0, 3), max_size=2),
)
def test_pr_laz_48_metrics_materialize(
    metrics: MetricsHarness, timeline: Timeline, finding_steps: set[int]
) -> None:
    """Los mismos valores definitivos en los tres, y una materialización ya escrita no se repite:
    el definitivo de cada mes es el de la **primera** pasada tras su cierre."""
    stack = metrics.stack
    clock = stack.world.stack.authz.sessions.clock
    clock.set(stack.bounds(stack.period())[1] - 3 * DAY)
    metrics.sync()
    organizations = {mode: stack.world.organization() for mode in MODES}
    start, _ = stack.bounds(stack.period())
    stack.execute(
        "UPDATE identity.zone SET created_at=$1 WHERE zone_id=ANY($2::uuid[])",
        start,
        [o.zone_id for o in organizations.values()],
    )
    task = MetricsMaterialize(
        service=ProgramMetricsService(
            database=stack.database,
            authorizer=stack.world.stack.authz.authorizer,
            exposure=stack.service(),
            clock=clock,
        ),
        clock=clock,
    )

    def at_step(index: int) -> None:
        if index in finding_steps:
            started = clock.now() - timedelta(minutes=10)
            for organization in organizations.values():
                stack.finding(organization, started=started, duration=timedelta(seconds=1))

    def run(mode: str) -> None:
        async def execute() -> None:
            context = stack.world.event_context(organizations[mode])
            async with stack.database.transaction(context) as transaction:
                await task.run(transaction)

        stack.run(execute())

    def advance(delta: timedelta) -> None:
        stack.advance(delta.total_seconds())
        metrics.sync()

    trace = drive(timeline, now=clock.now, advance=advance, run=run, at_step=at_step)

    outcome = {}
    for mode, organization in organizations.items():
        rows = stack.fetch(
            "SELECT period, values::text AS values, materialized_at, recomputed_at"
            " FROM accreditation.program_metric_snapshot WHERE zone_id = $1 ORDER BY period",
            organization.zone_id,
        )
        assert rows, mode
        for row in rows:
            assert row["recomputed_at"] is None, (mode, row["period"])  # mes cerrado: definitivo
            closed_at = stack.bounds(row["period"].strftime("%Y-%m"))[1]
            first_after_close = min(t for t in trace.runs[mode] if t >= closed_at)
            assert row["materialized_at"] == first_after_close.replace(
                microsecond=first_after_close.microsecond // 1000 * 1000
            ), (mode, row["period"], trace)
        outcome[mode] = [(str(row["period"]), json.loads(row["values"])) for row in rows]
    assert outcome[ONCE] == outcome[TWICE] == outcome[OUTAGE], trace


# --- 6. adoption_check ----------------------------------------------------------------------------


@pytest.fixture
def adoption(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness[AdoptionStack]]:
    with adoption_stack(postgres_endpoint, "pr_laz_48_ten_adoption") as built:
        yield Harness(built)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((6, 24, 25, 49, 73)),
    threshold=st.integers(1, 3),
    active_steps=st.sets(st.integers(0, 6), max_size=2),
)
def test_pr_laz_48_adoption_check(
    adoption: Harness[AdoptionStack], timeline: Timeline, threshold: int, active_steps: set[int]
) -> None:
    """Una alerta y un aviso por racha, nunca dos, en los tres; ``once`` y ``twice`` idénticos.

    La caída no inventa alertas: publica cada racha que **sigue abierta** al volver una sola vez,
    con los días transcurridos hasta entonces (nunca menos que la continua). Una racha que empezó
    y terminó (por actividad) dentro de la caída no se alerta a posteriori: ``adoption_check`` mira
    el silencio vigente (PAT-LAZ-RES-05 solo pide no emitir dos veces por la misma racha)."""
    stack = adoption.built
    organizations = {mode: stack.organization() for mode in MODES}
    for organization in organizations.values():
        stack.set_silence_days(organization, threshold)
    stack.next_morning(organizations[ONCE], hour=10)
    zones = {mode: stack.site_zone(o) for mode, o in organizations.items()}
    for zone in zones.values():
        stack.activate(zone)
    timezone = ZoneInfo(stack.timezone(organizations[ONCE]))
    activity: list[datetime] = []

    def at_step(index: int) -> None:
        if index in active_steps:
            activity.append(stack.now())
            for zone in zones.values():
                stack.record(zone, "classification")

    trace = drive(
        timeline,
        now=stack.now,
        advance=lambda delta: stack.tick(delta.total_seconds()),
        run=lambda mode: stack.run_check(organizations[mode]),
        at_step=at_step,
    )

    streaks = {}
    for mode, organization in organizations.items():
        marks = stack.alert_rows(organization)
        events = stack.adoption_events(organization)
        assert len(events) == len(marks), (mode, events, marks)
        assert len({m["streak_start"] for m in marks}) == len(marks), mode
        streaks[mode] = [(m["streak_start"], m["threshold_days"], m["silent_days"]) for m in marks]
    assert streaks[ONCE] == streaks[TWICE], trace
    assert {s[0] for s in streaks[OUTAGE]} <= {s[0] for s in streaks[ONCE]}, (streaks, trace)
    assert trace.recovered_at is not None
    recovered_at = trace.recovered_at
    continuous = {start: silent for start, _, silent in streaks[ONCE]}
    for start, _, silent in streaks[OUTAGE]:
        assert silent >= continuous[start], (streaks, trace)
    for start, _, _ in streaks[ONCE]:
        open_at_recovery = not [
            t for t in activity if t <= recovered_at and t.astimezone(timezone).date() >= start
        ]
        if open_at_recovery:
            assert start in {s[0] for s in streaks[OUTAGE]}, (start, streaks, trace)
    world = stack.actions.world.stack
    world.run(world.release())


# --- 7 y 8. email_grouping y digest_weekly --------------------------------------------------------


@pytest.fixture
def notifications(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness[NotificationWorld]]:
    with notification_world(postgres_endpoint, "pr_laz_48_ten_grouping") as built:
        yield Harness(built)


def _grouping_property(
    world: NotificationWorld,
    timeline: Timeline,
    counts: list[int],
    *,
    task: DigestGrouperBase,
    urgency: str,
    kind: str,
    window: Schedule,
    drain: timedelta,
) -> None:
    """Cada aviso termina en exactamente un lote enviado en los tres; ``once`` y ``twice`` dejan
    los mismos lotes; nunca dos envíos del mismo destinatario dentro de una ventana (``window``);
    y la vuelta de la caída agrupa lo acumulado en un solo envío."""
    # Cada ejemplo empieza en el borde de una ventana: que dos pasos caigan en la misma ventana
    # depende solo de la secuencia generada, no de dónde dejó el reloj el ejemplo anterior.
    now = world.clock.now()
    world.clock.advance((window.next_after(now) - now).total_seconds())
    seeded: dict[str, list[uuid.UUID]] = {mode: [] for mode in MODES}
    places = {}
    for mode in MODES:
        organization_id, plant_id, zone_id = world.new_organization()
        recipient = world.add_member(
            organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id
        ).user_id
        places[mode] = (organization_id, plant_id, zone_id, recipient)

    def at_step(index: int) -> None:
        for mode, (organization_id, plant_id, zone_id, recipient) in places.items():
            for _ in range(counts[index % len(counts)]):
                seeded[mode].append(
                    world.seed_delivery(
                        organization_id, recipient, urgency=urgency, plant_id=plant_id,
                        zone_id=zone_id,
                    )
                )  # fmt: skip

    def run(mode: str) -> None:
        organization_id = places[mode][0]
        context = world.event_context(world.event(organization_id, "finding_received", {}))

        async def execute() -> None:
            async with world.database.transaction(context) as transaction:
                await task.run(transaction)

        world.run(execute())

    trace = drive(
        timeline,
        now=world.clock.now,
        advance=lambda delta: world.clock.advance(int(delta.total_seconds())),
        run=run,
        at_step=at_step,
    )
    world.clock.advance(int(drain.total_seconds()))
    for mode in MODES:
        run(mode)

    batches = {}
    for mode, (organization_id, *_rest) in places.items():
        rows = world.fetch(
            "SELECT window_to, status, notification_ids FROM notifications.digest_batch"
            " WHERE organization_id = $1 AND kind = $2 ORDER BY window_to",
            organization_id,
            kind,
        )
        assert {row["status"] for row in rows} <= {"sent"}, (mode, rows)
        index = {notification_id: n for n, notification_id in enumerate(seeded[mode])}
        sent = [
            (row["window_to"], sorted(index[i] for i in row["notification_ids"])) for row in rows
        ]
        grouped = [n for _, members in sent for n in members]
        assert sorted(grouped) == list(range(len(seeded[mode]))), (mode, sent)  # uno y solo uno
        emails = world.fetch(
            "SELECT count(*) AS n FROM notifications.email_message"
            " WHERE organization_id = $1 AND kind = 'digest'",
            organization_id,
        )
        assert emails[0]["n"] == len(sent), mode
        for (earlier, _), (later, _) in itertools.pairwise(sent):
            assert window.next_after(earlier) <= later, (mode, earlier, later)
        batches[mode] = sent
    assert batches[ONCE] == batches[TWICE], trace
    assert len([b for b in batches[OUTAGE] if b[0] == trace.recovered_at]) <= 1
    assert len(batches[OUTAGE]) <= len(batches[ONCE]), (batches, trace)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((1, 2, 5, 25, 49)),
    counts=st.lists(st.integers(1, 2), min_size=1, max_size=3),
)
def test_pr_laz_48_email_grouping(
    notifications: Harness[NotificationWorld], timeline: Timeline, counts: list[int]
) -> None:
    world = notifications.built
    hours = INTEGER_PARAMETERS["email_grouping_hours"].default
    _grouping_property(
        world,
        timeline,
        counts,
        task=EmailGroupingTask(
            configuration=PostgresLoopConfigurationRepository(world.database),
            senders=Senders(StubSender()),  # type: ignore[arg-type]
            clock=world.clock,
        ),
        urgency="normal",
        kind=EMAIL_GROUPED_KIND,
        window=Schedule.every(hours * 3600),
        drain=timedelta(hours=hours + 1),
    )


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((24, 48, 72, 192)),
    counts=st.lists(st.integers(1, 2), min_size=1, max_size=3),
)
def test_pr_laz_48_digest_weekly(
    notifications: Harness[NotificationWorld], timeline: Timeline, counts: list[int]
) -> None:
    world = notifications.built
    _grouping_property(
        world,
        timeline,
        counts,
        task=DigestWeeklyTask(senders=Senders(StubSender()), clock=world.clock),  # type: ignore[arg-type]
        urgency="digest",
        kind=WEEKLY_MANAGER_KIND,
        window=Schedule.weekly(weekday=0),  # lunes, la preferencia por defecto
        drain=timedelta(days=8),
    )


# --- 9. verify_loop_projection --------------------------------------------------------------------


@pytest.fixture
def projection(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness[LoopWorld]]:
    with loop_world(postgres_endpoint, "pr_laz_48_ten_projection") as built:
        yield Harness(built)


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((1, 6, 24, 49)),
    findings=st.integers(1, 3),
    diverge_at=st.integers(0, 6),
)
def test_pr_laz_48_verify_loop_projection(
    projection: Harness[LoopWorld], timeline: Timeline, findings: int, diverge_at: int
) -> None:
    """Una divergencia (la misma en los tres, en un paso generado) se declara **una** vez y la
    reconstrucción deja la misma proyección que antes; ``once`` y ``twice`` registran las mismas
    muestras diarias y la caída no inventa las de los días que no corrió."""
    world = projection.built
    consumer, ports = world.projection_consumer()
    verifier = _verifier(world, _rebuilder(world, ports))
    organizations = {mode: world.organization() for mode in MODES}
    subjects: dict[str, list[IngestedSubject]] = {}
    for mode, organization in organizations.items():
        subjects[mode] = [world.ingest_finding(organization) for _ in range(findings)]
        for subject in subjects[mode]:
            assert world.deliver(consumer, world.outbox_event(subject)) == 1

    def digests(mode: str) -> list[tuple[Any, ...]]:
        out = []
        for subject in subjects[mode]:
            row = world.finding_state(subject)
            assert row is not None
            out.append((row["projection_digest"], row["evidence_flagged"]))
        return out

    before = {mode: digests(mode) for mode in MODES}
    diverge_step = diverge_at % len(timeline.steps)

    def at_step(index: int) -> None:
        if index == diverge_step:
            for mode in MODES:
                _diverge(world, subjects[mode][0])

    trace = drive(
        timeline,
        now=world.stack.now,
        advance=lambda delta: world.stack.tick(delta.total_seconds()),
        run=lambda mode: world.stack.run(
            verifier.verify_organization(world.context(organizations[mode]))
        ),
        at_step=at_step,
    )

    days = {}
    for mode, organization in organizations.items():
        assert digests(mode) == before[mode], (mode, trace)
        checkpoint = world.checkpoint(organization)
        assert checkpoint is not None and checkpoint["state"] == "healthy", mode
        events = world.stack.fetch(
            "SELECT payload::text AS payload FROM shared.outbox_event"
            " WHERE organization_id = $1 AND event_name = 'loop_projection_divergence'",
            organization.organization_id,
        )
        assert [json.loads(e["payload"])["divergence_count"] for e in events] == [1], mode
        days[mode] = {
            row["sample_day"]
            for row in world.stack.fetch(
                "SELECT sample_day FROM loop.loop_projection_sample_run WHERE organization_id = $1",
                organization.organization_id,
            )
        }
    assert days[ONCE] == days[TWICE] == {t.astimezone(UTC).date() for t in trace.runs[ONCE]}
    assert days[OUTAGE] == {t.astimezone(UTC).date() for t in trace.runs[OUTAGE]}


# --- 10. email_delivery_events_poll ---------------------------------------------------------------


@pytest.fixture
def deliveries(postgres_endpoint: PostgresEndpoint) -> Iterator[Harness[NotificationWorld]]:
    with notification_world(postgres_endpoint, "pr_laz_48_ten_deliveries") as built:
        yield Harness(built)


_PROVIDER_KINDS: Final = ("Permanent", "Transient", "complaint")


@settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=SLOW)
@given(
    timeline=timelines((1, 6, 24, 49)),
    messages=st.lists(
        st.tuples(st.integers(0, 6), st.integers(0, 1), st.sampled_from(_PROVIDER_KINDS)),
        min_size=1,
        max_size=5,
    ),
)
def test_pr_laz_48_email_delivery_events(
    deliveries: Harness[NotificationWorld], timeline: Timeline, messages: list[tuple[int, int, str]]
) -> None:
    """``twice`` recibe cada aviso del proveedor dos veces y además lo vuelve a recibir en el paso
    siguiente (SQS entrega al menos una vez): deja exactamente lo mismo que ``once``, marcas de
    tiempo incluidas. La caída procesa lo acumulado al volver: mismas marcas de rebote, queja y
    supresión (sus horas son las de la vuelta)."""
    world = deliveries.built
    emails = {mode: [_setup(world) for _ in range(2)] for mode in MODES}
    queues = {mode: FakeSqsClient() for mode in MODES}
    previous: list[str] = []

    def bodies(mode: str, index: int) -> list[str]:
        out = []
        for step, email, kind in messages:
            if step % len(timeline.steps) != index:
                continue
            provider_message_id = emails[mode][email][2]
            out.append(
                _complaint_body(provider_message_id)
                if kind == "complaint"
                else _bounce_body(provider_message_id, kind)
            )
        return out

    def at_step(index: int) -> None:
        nonlocal previous
        for mode in MODES:
            for body in bodies(mode, index):
                queues[mode].enqueue(body)
                if mode == TWICE:
                    queues[mode].enqueue(body)
        for body in previous:
            queues[TWICE].enqueue(body)  # la reentrega tardía
        previous = bodies(TWICE, index)

    trace = drive(
        timeline,
        now=world.clock.now,
        advance=lambda delta: world.clock.advance(int(delta.total_seconds())),
        run=lambda mode: _run_once(world, queues[mode]),
        at_step=at_step,
    )

    state = {}
    for mode in MODES:
        assert queues[mode].queue == [], mode
        rows = []
        for organization_id, user_id, provider_message_id in emails[mode]:
            email = _email_row(world, _message_id_of(world, provider_message_id))
            preference = _preference_row(world, organization_id, user_id)
            rows.append(
                (
                    email["bounce_kind"],
                    email["status"],
                    email["complaint_at"],
                    preference["email_suppressed_hard_bounce"],
                    preference["email_suppressed_complaint"],
                    preference["email_suppressed_at"],
                )
            )
        state[mode] = rows
    assert state[ONCE] == state[TWICE], (state, trace)

    def flags(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [(r[0], r[1], r[2] is not None, r[3], r[4], r[5] is not None) for r in rows]

    assert flags(state[OUTAGE]) == flags(state[ONCE]), (state, trace)
