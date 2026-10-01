"""Despachador de la bandeja contra PostgreSQL 16 real, como ``vigia_app`` (TASK-129).

Ejemplos de las reglas BR-NUC-76 a 82 y de los escenarios que la tarea cita:

- entrega con el contexto de la organización del evento y marca en la misma transacción; un
  defecto deshace el efecto del manejador en la base (``SAVEPOINT``) y cuenta el intento;
- ocho fallos → cola muerta, ``dead_letter_created`` y ``dead_letter_created_total``; la
  partición continúa con el evento siguiente;
- caída entre el efecto y la confirmación: reentrega con el mismo ``event_id`` y un solo efecto;
- **FS-NUC-07 a nivel de módulo**: ``ExternalDependencyDown`` durante 3 minutos abre el circuito
  en el primer fallo, pausa todas las particiones sin consumir intentos, sondea cada 60 s, y al
  volver la dependencia la sonda cierra y todo se drena sin una sola entrada en la cola muerta;
- reproceso por el operador con el mismo ``event_id`` y ``dead_letter_replayed``; cualquier otro
  contexto, ``not_found``;
- PAT-NUC-SEG-08: cada alerta produce su métrica con tipo y organización;
- enlace de trazas entre el tramo que publica y el de la entrega (PAT-NUC-MAN-01);
- las funciones ``SECURITY DEFINER`` solo devuelven identificadores y el reproceso solo actúa
  con un actor operador.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.dispatch_support import (
    EXTERNAL,
    PLAIN,
    Behavior,
    DispatchEnvironment,
    SimulatedCrash,
    dispatch_environment,
    metric_points,
    operator_context,
    replay_service,
)
from tests.factories import make_context
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.shared.context import ActorKind
from vigia_platform.shared.observability.alerts_consumer import ALERTS_CONSUMER
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.outbox.dispatcher import DELIVERY_SPAN, Outcome
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.outbox.retry import MAX_ATTEMPTS

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def env(postgres_endpoint: PostgresEndpoint) -> Iterator[DispatchEnvironment]:
    with dispatch_environment(postgres_endpoint, "outbox_dispatch") as environment:
        yield environment


@pytest.fixture(autouse=True)
def _quiet(env: DispatchEnvironment) -> None:
    env.run(env.quiesce())


def _drain(env: DispatchEnvironment, consumer: str, rounds: int = 50) -> None:
    dispatcher = env.dispatcher()
    for _ in range(rounds):
        report = env.run(dispatcher.dispatch_once(consumer))
        if not report.progressed and report.count(Outcome.SKIPPED) == 0:
            return
    raise AssertionError("la bandeja no se drenó")


# --- entrega, contexto y SAVEPOINT -----------------------------------------------------------


def test_delivers_with_the_event_organization_context_and_marks_in_the_same_transaction(
    env: DispatchEnvironment,
) -> None:
    organization = uuid.uuid4()
    (event,) = env.run(env.publish(organization, [uuid.uuid4()]))
    _drain(env, PLAIN)
    (invocation,) = env.handlers[PLAIN].invocations
    assert invocation.context_organization_id == event.organization_id == organization
    assert invocation.context_correlation_id == event.correlation_id
    rows = {(row.consumer, row.status) for row in env.run(env.deliveries())}
    assert (PLAIN, "delivered") in rows and (EXTERNAL, "pending") in rows
    assert env.run(env.echoes())[(PLAIN, event.event_id)] == 1


def test_events_of_one_partition_in_the_same_millisecond_are_delivered_in_publication_order(
    env: DispatchEnvironment,
) -> None:
    """Seguimiento 1 de VIG-47: mismo milisegundo, sin ``ledger_sequence``; desempata
    ``publish_seq`` (dentro de una transacción y entre transacciones)."""
    organization = uuid.uuid4()
    first_batch = env.run(env.publish(organization, [None, None, None]))
    second_batch = env.run(env.publish(organization, [None, None]))
    published = first_batch + second_batch
    assert len({event.created_at for event in published}) == 1
    assert {event.ledger_sequence for event in published} == {None}
    sequences = [row.publish_seq for row in env.run(env.deliveries(PLAIN))]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(published)
    _drain(env, PLAIN)
    invoked = [invocation.event_id for invocation in env.handlers[PLAIN].invocations]
    assert invoked == [event.event_id for event in published]


def test_a_defect_rolls_back_the_handler_effect_and_counts_one_attempt(
    env: DispatchEnvironment,
) -> None:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    env.handlers[PLAIN].plan(event.event_id, [Behavior.DEFECT])
    report = env.run(env.dispatcher().dispatch_once(PLAIN))
    assert report.count(Outcome.RETRIED) == 1
    (row,) = env.run(env.deliveries(PLAIN))
    assert (row.status, row.attempts) == ("retrying", 1)
    assert env.run(env.echoes())[(PLAIN, event.event_id)] == 0  # el eco se deshizo
    (code,) = env.run(
        env.fetch(
            "SELECT last_error_code, next_attempt_at FROM shared.outbox_delivery"
            " WHERE event_id = $1 AND consumer_name = $2",
            event.event_id,
            PLAIN,
        )
    )
    assert code["last_error_code"] == "probe_defect"
    # Retraso del primer reintento: 1 s con variación central (jitter 0,5).
    assert code["next_attempt_at"] == env.clock.now() + timedelta(seconds=1)
    # No vence aún: la partición espera; vencido, se entrega.
    assert env.run(env.dispatcher().dispatch_once(PLAIN)).count(Outcome.DELIVERED) == 0
    env.clock.advance(1)
    assert env.run(env.dispatcher().dispatch_once(PLAIN)).count(Outcome.DELIVERED) == 1
    assert env.run(env.echoes())[(PLAIN, event.event_id)] == 1


def test_eight_failures_go_to_the_dead_letter_queue_and_the_partition_continues(
    env: DispatchEnvironment,
) -> None:
    organization, plant = uuid.uuid4(), uuid.uuid4()
    first, second = env.run(env.publish(organization, [plant, plant]))
    env.handlers[PLAIN].plan(first.event_id, [Behavior.DEFECT] * MAX_ATTEMPTS)
    before = sum(v for _, v in metric_points(env.reader, MetricName.DEAD_LETTER_CREATED_TOTAL))
    dispatcher = env.dispatcher()
    for _ in range(MAX_ATTEMPTS):
        env.run(dispatcher.dispatch_once(PLAIN))
        # el segundo nunca se adelanta al primero
        assert second.event_id not in {i.event_id for i in env.handlers[PLAIN].invocations}
        env.clock.advance(601)
    (letter,) = env.run(env.dead_letters())
    assert (letter["event_id"], letter["consumer_name"], letter["attempts"]) == (
        first.event_id,
        PLAIN,
        MAX_ATTEMPTS,
    )
    assert letter["organization_id"] == organization
    created = env.run(env.events_named("dead_letter_created"))
    assert [row["organization_id"] for row in created] == [organization]
    assert '"attempts": 8' in created[0]["payload"]
    after = sum(v for _, v in metric_points(env.reader, MetricName.DEAD_LETTER_CREATED_TOTAL))
    assert after == before + 1
    _drain(env, PLAIN)
    statuses = {row.event_id: row.status for row in env.run(env.deliveries(PLAIN))}
    assert statuses == {first.event_id: "dead_letter", second.event_id: "delivered"}


def test_a_crash_between_effect_and_ack_redelivers_with_one_effect(
    env: DispatchEnvironment,
) -> None:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    env.handlers[PLAIN].plan(event.event_id, [Behavior.CRASH_AFTER_EFFECT])
    with pytest.raises(SimulatedCrash):
        env.run(env.dispatcher().dispatch_once(PLAIN))
    (row,) = env.run(env.deliveries(PLAIN))
    assert (row.status, row.attempts) == ("pending", 0)
    _drain(env, PLAIN)
    handler = env.handlers[PLAIN]
    assert [i.event_id for i in handler.invocations] == [event.event_id] * 2
    assert handler.raw_effects[event.event_id] == 2  # el manejador lo vio dos veces
    assert handler.effects[event.event_id] == 1  # su efecto, una sola (idempotente)
    assert env.run(env.echoes())[(PLAIN, event.event_id)] == 1  # el de la base, también


# --- FS-NUC-07 a nivel de módulo ---------------------------------------------------------------


def test_fs_nuc_07_dependency_down_for_three_minutes_pauses_without_dead_letters(
    env: DispatchEnvironment,
) -> None:
    first_org, second_org = uuid.uuid4(), uuid.uuid4()
    plants = [uuid.uuid4(), uuid.uuid4()]
    events = env.run(env.publish(first_org, [plants[0], plants[1], None]))
    events += env.run(env.publish(second_org, [None, None]))
    handler = env.handlers[EXTERNAL]
    handler.mode = Behavior.DEPENDENCY_DOWN
    dispatcher = env.dispatcher()
    down_since = env.clock.now()

    report = env.run(dispatcher.dispatch_once(EXTERNAL))
    assert report.count(Outcome.DEPENDENCY_DOWN) == 1
    state, opened_at = env.run(env.circuit(EXTERNAL))
    assert (state, opened_at) == ("open", down_since)  # abierto en el primer fallo

    # Tres minutos de caída, una ronda cada 5 s.
    while env.clock.now() < down_since + timedelta(minutes=3):
        env.clock.advance(5)
        env.run(dispatcher.dispatch_once(EXTERNAL))
    # Una invocación al abrir y una sonda por minuto (36 rondas de 5 s), nunca más.
    assert len(handler.invocations) == 1 + 3
    rows = env.run(env.deliveries(EXTERNAL))
    assert all((row.status, row.attempts) == ("pending", 0) for row in rows)
    assert env.run(env.dead_letters()) == []

    # La dependencia vuelve: la siguiente sonda cierra el circuito y todo se drena.
    handler.mode = Behavior.OK
    env.clock.advance(60)
    report = env.run(dispatcher.dispatch_once(EXTERNAL))
    assert report.probed and report.count(Outcome.DELIVERED) == 1
    assert env.run(env.circuit(EXTERNAL)) == ("closed", None)
    _drain(env, EXTERNAL)
    rows = env.run(env.deliveries(EXTERNAL))
    assert {row.event_id for row in rows} == {event.event_id for event in events}
    assert {row.status for row in rows} == {"delivered"}
    assert env.run(env.dead_letters()) == []


def test_without_a_declared_external_dependency_the_exception_counts_as_an_attempt(
    env: DispatchEnvironment,
) -> None:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    env.handlers[PLAIN].plan(event.event_id, [Behavior.DEPENDENCY_DOWN])
    report = env.run(env.dispatcher().dispatch_once(PLAIN))
    assert report.count(Outcome.RETRIED) == 1
    assert env.run(env.circuit(PLAIN)) == ("closed", None)


def test_a_probe_that_fails_with_another_exception_closes_and_counts(
    env: DispatchEnvironment,
) -> None:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    handler = env.handlers[EXTERNAL]
    handler.plan(event.event_id, [Behavior.DEPENDENCY_DOWN, Behavior.DEFECT])
    dispatcher = env.dispatcher()
    env.run(dispatcher.dispatch_once(EXTERNAL))
    assert env.run(env.circuit(EXTERNAL))[0] == "open"
    env.clock.advance(60)
    report = env.run(dispatcher.dispatch_once(EXTERNAL))
    assert report.probed and report.count(Outcome.RETRIED) == 1
    assert env.run(env.circuit(EXTERNAL)) == ("closed", None)
    (row,) = env.run(env.deliveries(EXTERNAL))
    assert (row.status, row.attempts) == ("retrying", 1)


def test_only_one_of_two_dispatchers_wins_the_probe(env: DispatchEnvironment) -> None:
    env.run(env.publish(uuid.uuid4(), [None, uuid.uuid4()]))
    handler = env.handlers[EXTERNAL]
    handler.mode = Behavior.DEPENDENCY_DOWN
    first, second = env.dispatcher(), env.dispatcher(env.new_database())
    env.run(first.dispatch_once(EXTERNAL))
    env.clock.advance(60)
    # Los dos leyeron el mismo circuito abierto y reclaman la sonda a la vez: gana uno.
    stale = env.run(first._read_breaker(EXTERNAL))
    now = env.clock.now()

    async def both_claim() -> list[Any]:
        return list(
            await asyncio.gather(
                first._claim_probe(EXTERNAL, stale, now),
                second._claim_probe(EXTERNAL, stale, now),
            )
        )

    claims = env.run(both_claim())
    assert sorted(claim is not None for claim in claims) == [False, True]
    assert env.run(env.circuit(EXTERNAL)) == ("half_open", now)
    # La sonda ya está en curso: ninguna ronda entrega hasta el siguiente intervalo.
    reports = [env.run(d.dispatch_once(EXTERNAL)) for d in (first, second)]
    assert not any(report.probed for report in reports)
    assert len(handler.invocations) == 1


# --- reproceso -----------------------------------------------------------------------------------


_operator = operator_context
_replay = replay_service


def _dead_letter(env: DispatchEnvironment) -> Any:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    env.handlers[PLAIN].plan(event.event_id, [Behavior.DEFECT] * MAX_ATTEMPTS)
    dispatcher = env.dispatcher()
    for _ in range(MAX_ATTEMPTS):
        env.run(dispatcher.dispatch_once(PLAIN))
        env.clock.advance(601)
    assert env.run(env.deliveries(PLAIN))[0].status == "dead_letter"
    return event


def test_the_operator_replays_with_the_same_event_id_and_audits_it(
    env: DispatchEnvironment,
) -> None:
    event = _dead_letter(env)
    replay, _ = _replay(env)
    operator = _operator(env)
    receipt = env.run(replay.replay(operator, event.event_id, PLAIN))
    assert (receipt.event_id, receipt.organization_id) == (event.event_id, event.organization_id)
    (row,) = env.run(env.deliveries(PLAIN))
    assert (row.status, row.attempts) == ("pending", 0)
    _drain(env, PLAIN)
    assert env.handlers[PLAIN].invocations[-1].event_id == event.event_id
    assert env.run(env.deliveries(PLAIN))[0].status == "delivered"
    (audit,) = env.run(
        env.fetch(
            "SELECT organization_id, actor_kind, resource_kind, resource_id,"
            " filters_json::text AS filters_json, correlation_id FROM shared.audit_entry"
            " WHERE operation = 'dead_letter_replayed' AND resource_id = $1",
            event.event_id,
        )
    )
    assert audit["organization_id"] == env.provider_organization_id
    assert (audit["actor_kind"], audit["resource_kind"], audit["resource_id"]) == (
        "operator",
        "outbox_event",
        event.event_id,
    )
    assert audit["correlation_id"] == operator.correlation_id
    assert str(event.organization_id) in audit["filters_json"]
    # La cola muerta no se reescribe: la fila sigue, una sola.
    assert len(env.run(env.dead_letters())) == 1
    # Ya no está en la cola muerta: un segundo reproceso no encuentra nada.
    with pytest.raises(ResourceNotFound):
        env.run(replay.replay(operator, event.event_id, PLAIN))


@pytest.mark.parametrize("kind", [ActorKind.USER, ActorKind.SYSTEM])
def test_only_a_platform_operator_can_replay(env: DispatchEnvironment, kind: ActorKind) -> None:
    event = _dead_letter(env)
    replay, denials = _replay(env)
    context = make_context(kind=kind, organization_id=env.provider_organization_id)
    with pytest.raises(ResourceNotFound):
        env.run(replay.replay(context, event.event_id, PLAIN))
    assert len(denials.denied) == 1
    assert env.run(env.deliveries(PLAIN))[0].status == "dead_letter"


def test_replay_of_a_delivery_that_is_not_dead_lettered_is_not_found(
    env: DispatchEnvironment,
) -> None:
    (event,) = env.run(env.publish(uuid.uuid4(), [None]))
    replay, _ = _replay(env)
    for event_id, consumer in ((event.event_id, PLAIN), (uuid.uuid4(), PLAIN)):
        with pytest.raises(ResourceNotFound):
            env.run(replay.replay(_operator(env), event_id, consumer))
    with pytest.raises(ResourceNotFound):
        env.run(replay.replay(_operator(env), event.event_id, "Dispatch-Plain"))
    assert env.run(env.deliveries(PLAIN))[0].status == "pending"


# --- funciones SECURITY DEFINER ------------------------------------------------------------------


def test_definer_functions_expose_only_identifiers_and_replay_needs_an_operator(
    env: DispatchEnvironment,
) -> None:
    event = _dead_letter(env)

    async def as_app(sql: str, *args: Any, actor_kind: str = "system") -> list[Any]:
        connection = await env.migrated.connect("vigia_app")
        try:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('vigia.organization_id', $1, true),"
                    " set_config('vigia.actor_kind', $2, true),"
                    " set_config('vigia.outbox_dispatch', 'on', true)",
                    str(uuid.uuid4()),
                    actor_kind,
                )
                return list(await connection.fetch(sql, *args))
        finally:
            await connection.close()

    # Con otra organización fijada, vigia_app no ve la entrega ni activando él mismo la
    # variable de las políticas outbox_dispatch (son solo de vigia_migrate).
    for table in ("outbox_delivery", "outbox_event", "dead_letter"):
        direct = env.run(as_app(f"SELECT count(*) AS n FROM shared.{table}"))  # noqa: S608
        assert direct[0]["n"] == 0, table
    columns = env.run(
        env.fetch(
            "SELECT pg_get_function_result(oid) AS result, prosecdef, proconfig"
            " FROM pg_proc WHERE proname = 'vigia_outbox_due_heads'"
        )
    )
    assert columns[0]["prosecdef"] and "search_path=pg_catalog" in columns[0]["proconfig"]
    assert "payload" not in columns[0]["result"]
    # Reproceso sin actor operador: no cambia nada.
    assert (
        env.run(
            as_app("SELECT shared.vigia_outbox_replay($1, $2, now()) AS o", event.event_id, PLAIN)
        )[0]["o"]
        is None
    )
    assert env.run(env.deliveries(PLAIN))[0].status == "dead_letter"
    assert (
        env.run(
            as_app(
                "SELECT shared.vigia_outbox_replay($1, $2, now()) AS o",
                event.event_id,
                PLAIN,
                actor_kind="operator",
            )
        )[0]["o"]
        == event.organization_id
    )


# --- PAT-NUC-SEG-08 ----------------------------------------------------------------------------

_ALERTS = [
    ("security_alert", {"alert_kind": "context_absent_attempt"}, "context_absent_attempt"),
    ("security_alert", {"alert_kind": "unknown_token_reported"}, "unknown_token_reported"),
    ("security_alert", {"alert_kind": "authorization_denied_repeated"}, "authorization_denied"),
    ("security_alert", {"alert_kind": "login_failures_account"}, "security_alert"),
    (
        "integrity_compromised",
        {"chain_kind": "ledger", "first_failed_sequence": 7, "verification_mode": "full"},
        "integrity_compromised",
    ),
]


@pytest.mark.parametrize(("event_name", "payload", "alert_type"), _ALERTS)
def test_each_security_alert_produces_its_metric_with_type_and_organization(
    env: DispatchEnvironment, event_name: str, payload: dict[str, Any], alert_type: str
) -> None:
    organization = uuid.uuid4()
    stamp = "occurred_at" if event_name == "security_alert" else "detected_at"
    context = make_context(kind=ActorKind.SYSTEM, organization_id=organization)

    async def publish() -> None:
        async with env.database.transaction(context) as transaction:
            await env.outbox.publish(
                transaction,
                NewEvent(
                    event_name=event_name,
                    payload={**payload, stamp: "2026-09-30T08:00:00.000Z"},
                ),
            )

    env.run(publish())
    _drain(env, ALERTS_CONSUMER)
    points = metric_points(env.reader, MetricName.SECURITY_ALERT_TOTAL)
    assert (
        {"alert_type": alert_type, "organization_id": str(organization)},
        1.0,
    ) in points


# --- enlace de trazas -------------------------------------------------------------------------


def test_the_delivery_span_links_to_the_publishing_span(env: DispatchEnvironment) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("pruebas")
    organization = uuid.uuid4()

    async def publish_in_span() -> Any:
        with tracer.start_as_current_span("request") as span:
            events = await env.publish(organization, [None])
            return span.get_span_context(), events[0]

    request, event = env.run(publish_in_span())
    assert event.trace_id == f"{request.trace_id:032x}"
    env.run(env.dispatcher(tracer=tracer).dispatch_once(PLAIN))
    (delivery,) = [span for span in exporter.get_finished_spans() if span.name == DELIVERY_SPAN]
    (link,) = delivery.links
    assert (link.context.trace_id, link.context.span_id) == (request.trace_id, request.span_id)
    assert delivery.attributes is not None
    assert delivery.attributes["correlation_id"] == str(event.correlation_id)
    assert delivery.attributes["consumer"] == PLAIN
