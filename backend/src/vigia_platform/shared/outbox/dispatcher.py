"""Despachador de la bandeja de salida (LC-NUC-23, parte 2; BR-NUC-76 a 80; PAT-NUC-ESC-05).

``Dispatcher.run(consumer, stop)`` entrega cada evento a cada consumidor **al menos una vez**, en
el orden de su partición, con el contexto de la organización del evento. ``vigia-worker``
(TASK-130) arranca un bucle por consumidor registrado; ``dispatch_once`` es una ronda, la unidad
que usan las pruebas.

Una ronda:

1. Lee el cortacircuito del consumidor (``shared.consumer``) y decide con ``ConsumerBreaker``:
   cerrado, entrega; abierto, pausa **sin consumir intentos** en ninguna partición; cumplido el
   intervalo, reclama la sonda (una actualización condicionada: solo un proceso gana) y entrega
   **una** cabeza, la más antigua (BR-NUC-79, PAT-NUC-RES-04).
2. Pide a ``shared.vigia_outbox_due_heads`` la cabeza vencida de cada partición: solo
   identificadores, porque con ``FORCE ROW LEVEL SECURITY`` el proceso no ve entregas sin una
   organización fijada (nuc_0010).
3. Por cada cabeza, una transacción con ``context_from_event`` (la organización emisora y su
   ``correlation_id``, BR-NUC-80):

   - ``pg_try_advisory_xact_lock(hashtextextended(consumidor|partición, 0))``: si otro proceso
     tiene la partición, se salta; dos procesos nunca entregan la misma partición a la vez;
   - toma la entrega más antigua de la partición por ``(created_at, ledger_sequence,
     publish_seq)`` y la bloquea con ``FOR UPDATE SKIP LOCKED``. Si la bloqueada no es la cabeza
     (o no es la que se descubrió) no se toma otra: saltar a la siguiente rompería el orden
     (BR-NUC-77). Si la cabeza aún no vence (reintento pendiente), la partición espera;
   - invoca al manejador dentro de un ``SAVEPOINT`` y marca el resultado en **la misma**
     transacción: éxito → ``delivered``; ``ExternalDependencyDown`` de un consumidor con
     dependencia externa → la entrega no cambia y el circuito se abre; cualquier otra excepción
     (también ``ContextAbsent``, BR-NUC-80) → se deshace lo del manejador, ``attempts + 1`` y
     ``RetryPolicy``: reintento con retroceso o, al octavo, cola muerta, ``dead_letter_created``
     en la bandeja y alarma (``dead_letter_created_total``); la partición continúa.

Si el proceso muere entre el efecto del manejador y la confirmación, nada queda marcado y la
entrega vuelve con el mismo ``event_id``: el manejador debe ser idempotente por ``event_id`` en
sus efectos fuera de la transacción (BR-NUC-76, PAT-NUC-RES-05). Una caída de la base no es un
intento del manejador: la transacción no confirma, la entrega queda como estaba y el bucle espera
con retroceso propio (PAT-NUC-RES-03).

**Orden dentro de la partición.** ``(created_at, ledger_sequence, publish_seq)``. Un evento sin
``ledger_sequence`` (identidad, alertas) puede compartir partición con eventos del expediente; si
la secuencia fuera la primera clave, con ``NULLS LAST`` se entregaría después de eventos
publicados más tarde. Quien escribe en el expediente publica después de tomar la cabeza de la
cadena, así que ``created_at`` sigue el orden de la secuencia; ``ledger_sequence`` desempata
dentro del mismo milisegundo y ``publish_seq`` (asignado por la base al insertar) hace el orden
total.

**Trazas.** Cada entrega abre el tramo ``outbox.deliver`` con un enlace al tramo que publicó el
evento (``trace_id``/``span_id`` guardados al publicar, PAT-NUC-MAN-01) y el ``correlation_id``
del evento como atributo.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Link, SpanContext, TraceFlags
from sqlalchemy import text

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.db import Transaction, TransientDatabaseError
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import Operation, PlatformMetrics, get_metrics
from vigia_platform.shared.observability.tracing import TRACER_NAME, span_name
from vigia_platform.shared.outbox.breaker import (
    BreakerAction,
    BreakerState,
    CircuitState,
    ConsumerBreaker,
    ExternalDependencyDown,
)
from vigia_platform.shared.outbox.publish import NewEvent, OutboxEvent, OutboxPort
from vigia_platform.shared.outbox.registries import Consumer, OutboxCatalog
from vigia_platform.shared.outbox.retry import DeadLetter, RetryPolicy
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "DEAD_LETTER_CREATED",
    "DELIVERY_SPAN",
    "DispatchReport",
    "Dispatcher",
    "DueHead",
    "EventContexts",
    "Outcome",
    "error_code",
]

_log = get_logger("shared.outbox")

DEAD_LETTER_CREATED: Final = "dead_letter_created"
DELIVERY_SPAN: Final = span_name("outbox.deliver")

DEFAULT_BATCH_SIZE: Final = 32
"""Cabezas por ronda ``[objetivo propio]``."""
DEFAULT_IDLE_SECONDS: Final = 1.0
"""Espera del bucle cuando una ronda no entregó nada ``[objetivo propio]``: la entrega en 5 s
(NFR-NUC-01) cabe con holgura."""
MAX_BACKOFF_SECONDS: Final = 30.0
"""Tope de la espera del bucle tras fallos seguidos de la base ``[objetivo propio]``."""

_SNAKE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CAMEL_BOUNDARY: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NOT_SNAKE: Final = re.compile(r"[^a-z0-9_]+")
_FALLBACK_ERROR_CODE: Final = "handler_error"

_DUE_HEADS: Final = text(
    "SELECT organization_id, partition_key, event_id, correlation_id"
    " FROM shared.vigia_outbox_due_heads(:consumer, :due_at, :max_heads)"
)
_READ_BREAKER: Final = text(
    "SELECT circuit_state, circuit_opened_at, probe_interval_seconds"
    " FROM shared.consumer WHERE consumer_name = :consumer"
)
_WRITE_BREAKER: Final = text(
    "UPDATE shared.consumer SET circuit_state = :new_state, circuit_opened_at = :new_changed_at"
    " WHERE consumer_name = :consumer AND circuit_state = :old_state"
    " AND circuit_opened_at IS NOT DISTINCT FROM :old_changed_at"
    " RETURNING consumer_name"
)
_PARTITION_LOCK: Final = text(
    "SELECT pg_try_advisory_xact_lock(hashtextextended(:consumer || '|' || :partition, 0))"
)
_LOCK_HEAD: Final = text(
    "WITH head AS ("
    " SELECT d.event_id FROM shared.outbox_delivery AS d"
    " JOIN shared.outbox_event AS e ON e.event_id = d.event_id"
    " WHERE d.consumer_name = :consumer AND e.partition_key = :partition"
    " AND d.status IN ('pending', 'retrying')"
    " ORDER BY e.created_at, e.ledger_sequence, e.publish_seq LIMIT 1)"
    " SELECT d.event_id, d.attempts, d.next_attempt_at,"
    " e.organization_id, e.plant_id, e.event_name, e.partition_key, e.ledger_sequence,"
    " e.payload, e.correlation_id, e.created_at, e.trace_id, e.span_id"
    " FROM shared.outbox_delivery AS d"
    " JOIN head ON head.event_id = d.event_id"
    " JOIN shared.outbox_event AS e ON e.event_id = d.event_id"
    " WHERE d.consumer_name = :consumer AND d.status IN ('pending', 'retrying')"
    " FOR UPDATE OF d SKIP LOCKED"
)
_MARK_DELIVERED: Final = text(
    "UPDATE shared.outbox_delivery SET status = 'delivered', delivered_at = :at"
    " WHERE event_id = :event_id AND consumer_name = :consumer"
)
_MARK_RETRYING: Final = text(
    "UPDATE shared.outbox_delivery SET status = 'retrying', attempts = :attempts,"
    " next_attempt_at = :next_attempt_at, last_error_code = :error_code"
    " WHERE event_id = :event_id AND consumer_name = :consumer"
)
_MARK_DEAD_LETTER: Final = text(
    "UPDATE shared.outbox_delivery SET status = 'dead_letter', attempts = :attempts,"
    " last_error_code = :error_code"
    " WHERE event_id = :event_id AND consumer_name = :consumer"
)
_INSERT_DEAD_LETTER: Final = text(
    "INSERT INTO shared.dead_letter (event_id, consumer_name, organization_id, failed_at,"
    " attempts, last_error_code)"
    " VALUES (:event_id, :consumer, :organization_id, :failed_at, :attempts, :error_code)"
)


class EventContexts(Protocol):
    """Los constructores de contexto que usa el despachador (``identity.authz.ScopeContexts``)."""

    def context_from_event(self, event: Any, *, unit: ActorUnit = ActorUnit.U02) -> ScopeContext:
        """La organización emisora del evento, con su ``correlation_id``."""
        ...

    def provider_audit_context(self) -> ScopeContext:
        """Contexto del sistema en la proveedora: lecturas globales (consumidores, cabezas)."""
        ...


class TransactionSource(Protocol):
    """``shared.db.Database``: el único camino a la base."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


class Outcome(enum.StrEnum):
    """Qué pasó con una cabeza en una ronda."""

    DELIVERED = "delivered"
    RETRIED = "retried"
    DEAD_LETTERED = "dead_lettered"
    DEPENDENCY_DOWN = "dependency_down"
    PAUSED = "paused"
    SKIPPED = "skipped"


_COUNTED_OUTCOMES: Final = frozenset(
    {Outcome.DELIVERED, Outcome.RETRIED, Outcome.DEAD_LETTERED, Outcome.DEPENDENCY_DOWN}
)
"""Intentos de entrega que cuenta ``outbox_deliveries_total``: los que invocaron al manejador."""


@dataclass(frozen=True, slots=True)
class DueHead:
    """Una cabeza vencida tal como la devuelve ``vigia_outbox_due_heads``."""

    organization_id: uuid.UUID
    partition_key: str
    event_id: uuid.UUID
    correlation_id: uuid.UUID


@dataclass(slots=True)
class DispatchReport:
    """Resultado de una ronda: cuántas cabezas acabaron en cada ``Outcome``."""

    outcomes: dict[Outcome, int] = field(default_factory=dict)
    probed: bool = False

    def add(self, outcome: Outcome) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def count(self, outcome: Outcome) -> int:
        return self.outcomes.get(outcome, 0)

    @property
    def progressed(self) -> bool:
        """Alguna entrega cambió de estado (entregada, reintento o cola muerta)."""
        return any(
            self.count(outcome) > 0
            for outcome in (Outcome.DELIVERED, Outcome.RETRIED, Outcome.DEAD_LETTERED)
        )


def error_code(error: BaseException) -> str:
    """``last_error_code`` de una excepción: su ``code`` si es ``snake_case``, o el nombre de su
    clase en ``snake_case`` (``ContextAbsent`` → ``context_absent``). Nunca el mensaje."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and _SNAKE.fullmatch(code):
        return code
    name = _CAMEL_BOUNDARY.sub("_", type(error).__name__).lower()
    name = _NOT_SNAKE.sub("_", name).strip("_")[:64]
    return name if _SNAKE.fullmatch(name) else _FALLBACK_ERROR_CODE


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el contexto exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _system_jitter() -> float:
    """Fracción aleatoria en ``[0, 1)`` para la variación del retraso."""
    return int.from_bytes(os.urandom(7), "big") / float(1 << 56)


def _link_to_publisher(trace_id: str | None, span_id: str | None) -> list[Link]:
    if trace_id is None or span_id is None:
        return []
    context = SpanContext(
        trace_id=int(trace_id, 16),
        span_id=int(span_id, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return [Link(context)] if context.is_valid else []


@dataclass(frozen=True, slots=True)
class _LockedHead:
    event: OutboxEvent
    attempts: int
    next_attempt_at: datetime


class Dispatcher:
    """El despachador de ``OutboxCatalog``: un ``run`` por consumidor registrado.

    No es un repositorio: no recibe contexto de nadie, lo construye por evento con
    ``context_from_event`` (y con el de la proveedora para las lecturas globales).
    """

    def __init__(
        self,
        *,
        database: TransactionSource,
        catalog: OutboxCatalog,
        outbox: OutboxPort,
        contexts: EventContexts,
        clock: Clock,
        jitter: Callable[[], float] = _system_jitter,
        metrics: PlatformMetrics | None = None,
        tracer: otel_trace.Tracer | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size debe ir de 1 a 1000")
        self._database = database
        self._catalog = catalog
        self._outbox = outbox
        self._contexts = contexts
        self._clock = clock
        self._jitter = jitter
        self._metrics = metrics if metrics is not None else get_metrics()
        self._tracer = tracer if tracer is not None else otel_trace.get_tracer(TRACER_NAME)
        self._batch_size = batch_size
        self._idle_seconds = idle_seconds
        # Las métricas solo admiten valores registrados (NFR-NUC-41). Un nombre que la política
        # no admite (p. ej. de 20 caracteres o más, que trata como token) sale como «other».
        for key, values in (
            ("consumer", [consumer.consumer_name for consumer in catalog.consumers.consumers()]),
            ("event_type", list(catalog.event_types.event_names())),
            ("result", [outcome.value for outcome in _COUNTED_OUTCOMES]),
        ):
            for value in values:
                try:
                    redaction.DEFAULT_POLICY.register(key, [value])
                except ValueError:
                    _log.warning("nombre sin dimensión de métrica; se registra como «other»")

    # --- Bucle ------------------------------------------------------------------------------

    async def run(self, consumer_name: str, stop: asyncio.Event) -> None:
        """Rondas hasta que ``stop`` se fije; termina la entrega en curso antes de salir."""
        consumer = self._consumer(consumer_name)
        failures = 0
        while not stop.is_set():
            try:
                report = await self.dispatch_once(consumer.consumer_name)
            except TransientDatabaseError:
                failures += 1
                _log.warning("despacho en pausa: la base no responde", consumer=consumer_name)
                await self._wait(stop, min(self._idle_seconds * 2**failures, MAX_BACKOFF_SECONDS))
                continue
            except Exception:
                failures += 1
                _log.exception("fallo inesperado del despacho", consumer=consumer_name)
                await self._wait(stop, min(self._idle_seconds * 2**failures, MAX_BACKOFF_SECONDS))
                continue
            failures = 0
            if not report.progressed:
                await self._wait(stop, self._idle_seconds)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    def _consumer(self, consumer_name: str) -> Consumer:
        if not self._catalog.sealed:
            raise RuntimeError("el catálogo de la bandeja no está sellado")
        consumer = self._catalog.consumers.get(consumer_name)
        if consumer is None:
            raise LookupError(f"consumidor no registrado: {consumer_name!r}")
        return consumer

    # --- Ronda ------------------------------------------------------------------------------

    async def dispatch_once(self, consumer_name: str) -> DispatchReport:
        """Una ronda sobre las cabezas vencidas de ``consumer_name``."""
        consumer = self._consumer(consumer_name)
        report = DispatchReport()
        now = self._clock.now()
        breaker = await self._read_breaker(consumer.consumer_name)
        action = ConsumerBreaker.tick(breaker, now)
        if action is BreakerAction.PAUSE:
            report.add(Outcome.PAUSED)
            return report
        probe: BreakerState | None = None
        if action is BreakerAction.PROBE:
            probe = await self._claim_probe(consumer.consumer_name, breaker, now)
            if probe is None:
                report.add(Outcome.PAUSED)
                return report
            report.probed = True
        heads = await self._due_heads(consumer.consumer_name, now, 1 if probe else None)
        for head in heads:
            outcome = await self._process(consumer, head, probe)
            report.add(outcome)
            if outcome in (Outcome.DEPENDENCY_DOWN, Outcome.PAUSED):
                break
        return report

    async def _read_breaker(self, consumer_name: str) -> BreakerState:
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            return await self._breaker_in(tx, consumer_name)

    @staticmethod
    async def _breaker_in(transaction: Transaction, consumer_name: str) -> BreakerState:
        row = (await transaction.execute(_READ_BREAKER, {"consumer": consumer_name})).one()
        return BreakerState(
            CircuitState(row.circuit_state),
            row.circuit_opened_at,
            timedelta(seconds=int(row.probe_interval_seconds)),
        )

    async def _claim_probe(
        self, consumer_name: str, current: BreakerState, now: datetime
    ) -> BreakerState | None:
        """Reclama la sonda; ``None`` si otro proceso la reclamó antes."""
        claimed = ConsumerBreaker.on_probe(current, now)
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            won = await self._write_breaker(tx, consumer_name, current, claimed)
        return claimed if won else None

    @staticmethod
    async def _write_breaker(
        transaction: Transaction, consumer_name: str, old: BreakerState, new: BreakerState
    ) -> bool:
        if (old.state, old.changed_at) == (new.state, new.changed_at):
            return True
        result = await transaction.execute(
            _WRITE_BREAKER,
            {
                "consumer": consumer_name,
                "new_state": new.state.value,
                "new_changed_at": new.changed_at,
                "old_state": old.state.value,
                "old_changed_at": old.changed_at,
            },
        )
        return result.first() is not None

    async def _due_heads(
        self, consumer_name: str, now: datetime, limit: int | None
    ) -> list[DueHead]:
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            rows = (
                await tx.execute(
                    _DUE_HEADS,
                    {
                        "consumer": consumer_name,
                        "due_at": now,
                        "max_heads": limit if limit is not None else self._batch_size,
                    },
                )
            ).all()
        return [
            DueHead(
                organization_id=_uuid(row.organization_id),
                partition_key=row.partition_key,
                event_id=_uuid(row.event_id),
                correlation_id=_uuid(row.correlation_id),
            )
            for row in rows
        ]

    # --- Una cabeza -------------------------------------------------------------------------

    async def _process(
        self, consumer: Consumer, head: DueHead, probe: BreakerState | None
    ) -> Outcome:
        context = self._contexts.context_from_event(head, unit=consumer.unit)
        settled: tuple[OutboxEvent, int] | None = None
        async with self._database.transaction(context) as tx:
            outcome, settled = await self._process_in(tx, consumer, head, probe)
        # Después de confirmar: la alarma solo cuenta lo que quedó en la cola muerta, y la
        # latencia de la entrega va de la publicación a la confirmación de ``delivered``
        # (NFR-NUC-01: p95 ≤ 5 s desde la confirmación; las dos marcas son del ``Clock``).
        if outcome is Outcome.DELIVERED and settled is not None:
            waited = self._clock.now() - settled[0].created_at
            self._metrics.record_operation(Operation.OUTBOX_DELIVERY, waited.total_seconds())
        elif outcome is Outcome.DEAD_LETTERED and settled is not None:
            event, attempts = settled
            self._metrics.dead_letter_created_total.add(
                1, {"consumer": consumer.consumer_name, "event_type": event.event_name}
            )
            _log.error(
                "entrega enviada a la cola muerta",
                consumer=consumer.consumer_name,
                event_id=str(event.event_id),
                organization_id=str(event.organization_id),
                attempts=attempts,
            )
        elif outcome is Outcome.RETRIED:
            self._metrics.outbox_retries_total.add(1, {"consumer": consumer.consumer_name})
        if outcome in _COUNTED_OUTCOMES:
            self._metrics.outbox_deliveries_total.add(
                1, {"consumer": consumer.consumer_name, "result": outcome.value}
            )
        return outcome

    async def _process_in(
        self,
        tx: Transaction,
        consumer: Consumer,
        head: DueHead,
        probe: BreakerState | None,
    ) -> tuple[Outcome, tuple[OutboxEvent, int] | None]:
        name = consumer.consumer_name
        locked = (
            await tx.execute(_PARTITION_LOCK, {"consumer": name, "partition": head.partition_key})
        ).scalar_one()
        if not locked:
            return Outcome.SKIPPED, None
        breaker = await self._breaker_in(tx, name)
        if probe is None and breaker.state is not CircuitState.CLOSED:
            return Outcome.PAUSED, None
        if probe is not None and (breaker.state, breaker.changed_at) != (
            probe.state,
            probe.changed_at,
        ):
            return Outcome.PAUSED, None
        locked_head = await self._lock_head(tx, name, head)
        now = self._clock.now()
        if locked_head is None or locked_head.next_attempt_at > now:
            return Outcome.SKIPPED, None
        event = locked_head.event
        failure: Exception | None = None
        with self._tracer.start_as_current_span(
            DELIVERY_SPAN,
            kind=otel_trace.SpanKind.CONSUMER,
            links=_link_to_publisher(event.trace_id, event.span_id),
            attributes={
                "consumer": name,
                "event_type": event.event_name,
                "event_id": str(event.event_id),
                "correlation_id": str(event.correlation_id),
                "organization_id": str(event.organization_id),
            },
        ) as span:
            try:
                async with tx.savepoint():
                    await consumer.handler(event, tx)
            except Exception as error:
                if tx.failed:
                    # Conexión rota o paso abandonado: no hay transacción que marcar.
                    raise
                failure = error
                span.record_exception(error)
        at = self._clock.now()
        if failure is None:
            await tx.execute(
                _MARK_DELIVERED, {"at": at, "event_id": event.event_id, "consumer": name}
            )
            if probe is not None:
                await self._write_breaker(
                    tx, name, probe, ConsumerBreaker.on_success(probe, probe=True)
                )
                self._metrics.outbox_circuit_open.set(0, {"consumer": name})
            return Outcome.DELIVERED, (event, locked_head.attempts + 1)
        if isinstance(failure, ExternalDependencyDown) and consumer.has_external_dependency:
            opened = ConsumerBreaker.on_dependency_failure(breaker, at, probe=probe is not None)
            await self._write_breaker(tx, name, breaker, opened)
            self._metrics.outbox_circuit_open.set(1, {"consumer": name})
            _log.warning(
                "circuito del consumidor abierto: dependencia externa caída", consumer=name
            )
            return Outcome.DEPENDENCY_DOWN, None
        if probe is not None:
            await self._write_breaker(
                tx, name, probe, ConsumerBreaker.on_handler_defect(probe, probe=True)
            )
            self._metrics.outbox_circuit_open.set(0, {"consumer": name})
        return await self._record_failure(tx, name, locked_head, failure, at)

    async def _lock_head(
        self, tx: Transaction, consumer_name: str, head: DueHead
    ) -> _LockedHead | None:
        row = (
            await tx.execute(
                _LOCK_HEAD, {"consumer": consumer_name, "partition": head.partition_key}
            )
        ).one_or_none()
        if row is None or _uuid(row.event_id) != head.event_id:
            return None
        payload: Mapping[str, Any] = (
            json.loads(row.payload) if isinstance(row.payload, str) else row.payload
        )
        event = OutboxEvent(
            event_id=_uuid(row.event_id),
            organization_id=_uuid(row.organization_id),
            plant_id=None if row.plant_id is None else _uuid(row.plant_id),
            event_name=row.event_name,
            partition_key=row.partition_key,
            ledger_sequence=row.ledger_sequence,
            payload=payload,
            correlation_id=_uuid(row.correlation_id),
            created_at=row.created_at,
            trace_id=row.trace_id,
            span_id=row.span_id,
        )
        return _LockedHead(event, int(row.attempts), row.next_attempt_at)

    async def _record_failure(
        self,
        tx: Transaction,
        consumer_name: str,
        head: _LockedHead,
        failure: Exception,
        at: datetime,
    ) -> tuple[Outcome, tuple[OutboxEvent, int] | None]:
        event = head.event
        code = error_code(failure)
        decision = RetryPolicy.after_failure(head.attempts + 1, self._jitter())
        keys = {"event_id": event.event_id, "consumer": consumer_name}
        if not isinstance(decision, DeadLetter):
            await tx.execute(
                _MARK_RETRYING,
                {
                    **keys,
                    "attempts": decision.attempts,
                    "next_attempt_at": at + decision.delay,
                    "error_code": code,
                },
            )
            return Outcome.RETRIED, None
        await tx.execute(
            _MARK_DEAD_LETTER, {**keys, "attempts": decision.attempts, "error_code": code}
        )
        await tx.execute(
            _INSERT_DEAD_LETTER,
            {
                **keys,
                "organization_id": event.organization_id,
                "failed_at": at,
                "attempts": decision.attempts,
                "error_code": code,
            },
        )
        await self._outbox.publish(
            tx,
            NewEvent(
                event_name=DEAD_LETTER_CREATED,
                payload={
                    "event_id": str(event.event_id),
                    "consumer_name": consumer_name,
                    "attempts": decision.attempts,
                    "last_error_code": code,
                    "failed_at": format_timestamp(at),
                },
            ),
        )
        return Outcome.DEAD_LETTERED, (event, decision.attempts)
