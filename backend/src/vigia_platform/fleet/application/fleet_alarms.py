"""``fleet.alarms``: alarmas de flota por transición y ``GET /fleet/alarms`` (TASK-225; LC-GOB-16).

BR-GOB-49, 65, 76, 78 a 81 y 94; NFR-GOB-08, 36, 45 a 47; PAT-GOB-RES-04; BL §2.6 y §3.6.

**Levantar y bajar** (``raise_in`` y ``clear_in``, que usan también ``detect_mute_nodes`` y
``alert_expiring_certificates``): en la transacción que decide, cada transición publica su evento
en la bandeja (``fleet_alarm_raised`` / ``fleet_alarm_cleared``) y escribe la fila con el
``event_id`` publicado; si la transacción se deshace, ni la fila ni el evento existen. El cierre
bloquea antes las filas que siguen abiertas: una alarma que otro ciclo ya cerró no se cierra ni se
publica dos veces. La apertura confía en la ranura de ``open_fleet_alarm``: si otra transacción la
ocupó, ``AlarmAlreadyOpen`` deshace el ciclo entero (sin efecto).

**``evaluate_fleet_alarms``** (60 s, por organización; ``register_evaluate_fleet_alarms``; el
registro en la raíz es de TASK-227). Por lotes de ``batch_size`` nodos y con ``now`` del
``Clock``:

1. los hechos de cada nodo en una sentencia y sus condiciones con el evaluador de referencia
   ``fleet_warnings.evaluate`` (las mismas de los avisos de ``GET /fleet/nodes``);
2. las filas de histéresis de esos nodos, **bloqueadas**; una fila evaluada hace menos de
   ``OVERLAP_WINDOW`` (medio ciclo) es de un ciclo solapado y no se vuelve a contar; las que faltan
   se insertan y solo cuentan si las insertó este ciclo (NFR-GOB-08, 47);
3. las alarmas abiertas, leídas **después** de esos candados;
4. por nodo: ``queue_over_threshold``, ``clock_drift`` y ``camera_below_min_fps`` con dos
   evaluaciones consecutivas; ``orphan_clips_growing`` con más de 50 huérfanos o 24 h sostenidas;
   ``version_retiring`` (solo con ``retires_at``, BR-GOB-80) y ``simulated_adapter_in_productive``
   en la primera; ``node_mute`` baja cuando el nodo vuelve a ``reachable`` y
   ``certificate_expiring`` cuando la credencial vigente ya no vence en 15 días (rotación);
5. un nodo **revocado o dado de baja** no se evalúa: sus alarmas abiertas se cierran una vez
   (decisión del redactor de TASK-225, BR-GOB-76);
6. cada ``simulated_adapter_in_productive`` levantada deja además su alerta
   ``fleet_security_alert`` en la auditoría de U-02, en la misma transacción (NFR-GOB-36, G-11).

La evaluación no se hace en la ruta del latido (nota de TASK-225, «reparto entre tareas»): el
latido no hace trabajo de más y la histéresis cuenta ciclos de 60 s.

**Orden de los candados** de la evaluación (encaja con el de ``detect_mute_nodes``, que nunca
toma una fila de histéresis): (1) las filas de ``fleet_alarm_evaluation`` de los nodos, por
``(node_id, alarm_kind)``; (2) las filas de ``fleet_alarm`` que cierra, por ``alarm_id``, y con
ellas su ranura (disparador); (3) las ranuras de las que abre, por ``(node_id, alarm_kind)``; (4)
la cadena de auditoría de la organización.

**``GET /fleet/alarms``** (``FleetAlarms.page``; ``fleet.read`` de organización o planta): las
alarmas activas y recientes (24 meses en línea, NFR-GOB-39), la más reciente primero, por cursor
``(raised_at, alarm_id)`` y filtrables por planta, nodo, clase y estado. Planta o nodo inexistente,
de otra organización o fuera del alcance responden ``not_found``. Bajo concesión, la lectura deja
su ``fleet_read`` en la misma transacción (BR-NUC-38).

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from vigia_platform.fleet.adapters.postgres.fleet_alarm_store import (
    AlarmCursor,
    AlarmFilters,
    EvaluationFacts,
    PostgresFleetAlarmStore,
)
from vigia_platform.fleet.adapters.postgres.inventory_queries import PostgresInventoryQueries
from vigia_platform.fleet.domain.alarm_hysteresis import (
    STATEFUL_KINDS,
    Evaluation,
    Transition,
    observe,
    transition,
)
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_alarm import (
    CLEARED_EVENT,
    RAISED_EVENT,
    STATELESS_EVALUATED_KINDS,
    FleetAlarm,
    NewAlarm,
    cleared_payload,
    raised_payload,
)
from vigia_platform.fleet.domain.fleet_warnings import (
    ORPHAN_CLIPS_MAX,
    ORPHAN_CLIPS_WINDOW,
    evaluate,
)
from vigia_platform.identity.authz.authorize import (
    Authorizer,
    Resource,
    ResourceNotFound,
    narrowed,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ContextAbsent, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7_at
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = [
    "BATCH_SIZE",
    "EVALUATE_FLEET_ALARMS",
    "EVALUATE_FLEET_ALARMS_SCHEDULE",
    "MAX_ALARMS_PAGE",
    "OVERLAP_WINDOW",
    "AlarmDependencies",
    "AlarmPage",
    "AlarmReport",
    "FleetAlarmEvaluator",
    "FleetAlarms",
    "clear_in",
    "raise_in",
    "register_evaluate_fleet_alarms",
]

EVALUATE_FLEET_ALARMS: Final = "evaluate_fleet_alarms"
EVALUATE_FLEET_ALARMS_SCHEDULE: Final = Schedule.every(60)
"""Cada 60 s (nota de cadencias de BL §2.6; NFR-GOB-12)."""
OVERLAP_WINDOW: Final = timedelta(seconds=30)
"""Medio ciclo `[objetivo propio]`: una fila de histéresis evaluada hace menos es de un ciclo
solapado y no cuenta otra evaluación (NFR-GOB-08: «el segundo se descarta sin efecto»)."""
BATCH_SIZE: Final = 500
"""Nodos por lote `[objetivo propio]`: un número acotado de sentencias por organización."""
MAX_ALARMS_PAGE: Final = 100
"""Alarmas por página de ``GET /fleet/alarms`` `[objetivo propio]`, como el inventario."""
SECURITY_ALERT_KIND: Final = FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE


@dataclass(frozen=True, slots=True, kw_only=True)
class AlarmDependencies:
    """Lo que reciben las tres tareas de alarmas (lo construye la raíz de composición)."""

    clock: Clock
    outbox: OutboxPort
    store: PostgresFleetAlarmStore = field(default_factory=PostgresFleetAlarmStore)
    random_bytes: Callable[[int], bytes] = os.urandom


@dataclass(slots=True)
class AlarmReport:
    """Lo que hizo una ejecución en una organización: las alarmas abiertas y cerradas."""

    raised: list[FleetAlarm] = field(default_factory=list)
    cleared: list[FleetAlarm] = field(default_factory=list)
    transitions: list[uuid.UUID] = field(default_factory=list)
    """Nodos con su transición a ``mute`` escrita (``detect_mute_nodes``)."""


def now_of(clock: Clock) -> datetime:
    """``now`` al milisegundo: las marcas de las cargas y de las filas coinciden."""
    return to_millisecond(clock.now())


async def raise_in(
    deps: AlarmDependencies,
    transaction: Transaction,
    alarms: Sequence[NewAlarm],
    now: datetime,
) -> tuple[FleetAlarm, ...]:
    """Abre ``alarms`` en ``transaction``: evento y fila de cada una (las cerradas, a lo sumo una
    por clase y nodo). Ranura ocupada → ``AlarmAlreadyOpen``."""
    built: list[FleetAlarm] = []
    organization_id = transaction.context.organization_id
    for new in sorted(alarms, key=lambda alarm: (str(alarm.node_id), alarm.alarm_kind.value)):
        alarm_id = uuid7_at(now, deps.random_bytes(10))
        publication = await deps.outbox.publish(
            transaction,
            NewEvent(
                event_name=RAISED_EVENT,
                payload=raised_payload(
                    alarm_id=alarm_id,
                    kind=new.alarm_kind,
                    node_id=new.node_id,
                    zone_id=new.zone_id,
                    since=new.since,
                ),
                plant_id=new.plant_id,
            ),
        )
        built.append(
            FleetAlarm(
                alarm_id=alarm_id,
                organization_id=organization_id,
                plant_id=new.plant_id,
                alarm_kind=new.alarm_kind,
                node_id=new.node_id,
                zone_id=new.zone_id,
                raised_at=now,
                cleared_at=None,
                raised_event_id=publication.event.event_id,
                cleared_event_id=None,
            )
        )
    await deps.store.raise_alarms(transaction, built)
    return tuple(built)


async def clear_in(
    deps: AlarmDependencies,
    transaction: Transaction,
    alarms: Sequence[tuple[FleetAlarm, datetime]],
    now: datetime,
) -> tuple[FleetAlarm, ...]:
    """Cierra las alarmas ``(alarma, since)`` que sigan abiertas tras bloquearlas: una sola vez."""
    since_of = {alarm.alarm_id: since for alarm, since in alarms}
    locked = await deps.store.lock_open(transaction, [alarm for alarm, _ in alarms])
    cleared: list[tuple[FleetAlarm, uuid.UUID]] = []
    for alarm in locked:
        publication = await deps.outbox.publish(
            transaction,
            NewEvent(
                event_name=CLEARED_EVENT,
                payload=cleared_payload(alarm, since=since_of[alarm.alarm_id], cleared_at=now),
                plant_id=alarm.plant_id,
            ),
        )
        cleared.append((alarm, publication.event.event_id))
    await deps.store.clear(transaction, cleared, now)
    return tuple(
        FleetAlarm(
            alarm_id=alarm.alarm_id,
            organization_id=alarm.organization_id,
            plant_id=alarm.plant_id,
            alarm_kind=alarm.alarm_kind,
            node_id=alarm.node_id,
            zone_id=alarm.zone_id,
            raised_at=alarm.raised_at,
            cleared_at=now,
            raised_event_id=alarm.raised_event_id,
            cleared_event_id=event_id,
        )
        for alarm, event_id in cleared
    )


@dataclass(slots=True)
class _Decisions:
    raise_: list[NewAlarm] = field(default_factory=list)
    clear: list[tuple[FleetAlarm, datetime]] = field(default_factory=list)


@repository
class FleetAlarmEvaluator:
    """El manejador de ``evaluate_fleet_alarms`` para la transacción de una organización."""

    def __init__(
        self, deps: AlarmDependencies, *, audit: AuditWriter, batch_size: int = BATCH_SIZE
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._deps = deps
        self._audit = audit
        self._batch_size = batch_size

    def __repr__(self) -> str:
        return "FleetAlarmEvaluator()"

    async def evaluate(self, transaction: Transaction) -> AlarmReport:
        """Una pasada sobre todos los nodos de la organización de ``transaction``."""
        deps = self._deps
        now = now_of(deps.clock)
        report = AlarmReport()
        after: uuid.UUID | None = None
        while True:
            facts = await deps.store.evaluation_facts(
                transaction,
                now=now,
                clips_since=now - ORPHAN_CLIPS_WINDOW,
                after=after,
                limit=self._batch_size,
            )
            if facts:
                await self._batch(transaction, facts, now, report)
            if len(facts) < self._batch_size:
                return report
            after = facts[-1].node_id

    async def _batch(
        self,
        transaction: Transaction,
        facts: Sequence[EvaluationFacts],
        now: datetime,
        report: AlarmReport,
    ) -> None:
        deps = self._deps
        active = [node for node in facts if not node.retired]
        # (1) Las filas de histéresis, bloqueadas antes de leer las alarmas abiertas.
        stored = await deps.store.lock_evaluations(transaction, [node.node_id for node in active])
        open_alarms = await deps.store.open_alarms(transaction, [node.node_id for node in facts])
        decisions = _Decisions()
        inserts: list[tuple[uuid.UUID, uuid.UUID, FleetAlarmKind, bool]] = []
        updates: dict[tuple[uuid.UUID, FleetAlarmKind], Evaluation] = {}
        pending: list[tuple[EvaluationFacts, FleetAlarmKind, Evaluation, bool]] = []
        for node in facts:
            if node.retired:
                # BR-GOB-76: nada se evalúa; lo que quedó abierto se cierra una vez.
                decisions.clear += [
                    (alarm, now)
                    for (node_id, _), alarm in sorted(open_alarms.items(), key=_by_key)
                    if node_id == node.node_id
                ]
                continue
            conditions = set(evaluate(node.inputs, node.thresholds, now))
            for kind in sorted(STATEFUL_KINDS):
                condition = kind in conditions
                found = stored.get((node.node_id, kind))
                if found is None:
                    inserts.append((node.plant_id, node.node_id, kind, condition))
                    pending.append((node, kind, observe(None, condition, now), True))
                    continue
                previous, evaluated_at = found
                if evaluated_at > now - OVERLAP_WINDOW:
                    continue  # ya evaluada por un ciclo solapado: no cuenta dos veces
                evaluation = observe(previous, condition, now)
                updates[(node.node_id, kind)] = evaluation
                pending.append((node, kind, evaluation, False))
            self._stateless(node, conditions, open_alarms, now, decisions)
        owned = await deps.store.insert_evaluations(transaction, inserts, now)
        await deps.store.update_evaluations(transaction, updates, now)
        for node, kind, evaluation, inserted in pending:
            if inserted and (node.node_id, kind) not in owned:
                continue  # otro ciclo la insertó antes: es suya
            self._decide(node, kind, evaluation, open_alarms, now, decisions)
        report.cleared += await clear_in(deps, transaction, decisions.clear, now)
        raised = await raise_in(deps, transaction, decisions.raise_, now)
        report.raised += raised
        for alarm in raised:
            if alarm.alarm_kind is SECURITY_ALERT_KIND:
                await self._security_alert(transaction, alarm)

    def _stateless(
        self,
        node: EvaluationFacts,
        conditions: set[FleetAlarmKind],
        open_alarms: dict[tuple[uuid.UUID, FleetAlarmKind], FleetAlarm],
        now: datetime,
        decisions: _Decisions,
    ) -> None:
        for kind in sorted(STATELESS_EVALUATED_KINDS):
            self._decide(
                node, kind, Evaluation(kind in conditions, 1, now), open_alarms, now, decisions
            )
        mute = open_alarms.get((node.node_id, FleetAlarmKind.NODE_MUTE))
        if mute is not None and node.communication_state is CommunicationState.REACHABLE:
            # El nodo volvió: el último latido aceptado prueba que el silencio terminó.
            since = node.inputs.last_heartbeat_at or now
            decisions.clear.append((mute, min(since, now)))
        certificate = open_alarms.get((node.node_id, FleetAlarmKind.CERTIFICATE_EXPIRING))
        if certificate is not None and FleetAlarmKind.CERTIFICATE_EXPIRING not in conditions:
            decisions.clear.append((certificate, now))  # rotada: la vigente ya no vence pronto

    def _decide(
        self,
        node: EvaluationFacts,
        kind: FleetAlarmKind,
        evaluation: Evaluation,
        open_alarms: dict[tuple[uuid.UUID, FleetAlarmKind], FleetAlarm],
        now: datetime,
        decisions: _Decisions,
    ) -> None:
        current = open_alarms.get((node.node_id, kind))
        immediate = (
            kind is FleetAlarmKind.ORPHAN_CLIPS_GROWING
            and node.inputs.orphan_clips > ORPHAN_CLIPS_MAX
        )
        step = transition(
            kind, evaluation, open_alarm=current is not None, now=now, immediate=immediate
        )
        if step is Transition.RAISE:
            zone = (
                node.productive_zone_id
                if kind is FleetAlarmKind.SIMULATED_ADAPTER_IN_PRODUCTIVE
                else None
            )
            decisions.raise_.append(
                NewAlarm(
                    alarm_kind=kind,
                    plant_id=node.plant_id,
                    node_id=node.node_id,
                    since=evaluation.since,
                    zone_id=zone,
                )
            )
        elif step is Transition.CLEAR and current is not None:
            decisions.clear.append((current, evaluation.since))

    async def _security_alert(self, transaction: Transaction, alarm: FleetAlarm) -> None:
        """NFR-GOB-36: la alerta en la auditoría inalterable de U-02, con nodo, zona, planta y
        ``correlation_id``, en la transacción de la alarma."""
        await self._audit.append(
            transaction.context,
            AuditOperation.FLEET_SECURITY_ALERT,
            plant_id=alarm.plant_id,
            zone_id=alarm.zone_id,
            resource=ResourceRef("node", alarm.node_id),
            filters={"alert_kind": alarm.alarm_kind.value, "alarm_id": str(alarm.alarm_id)},
            transaction=transaction,
        )


def _by_key(item: tuple[tuple[uuid.UUID, FleetAlarmKind], FleetAlarm]) -> tuple[str, str]:
    (node_id, kind), _ = item
    return str(node_id), kind.value


def register_evaluate_fleet_alarms(
    registry: PeriodicTaskRegistry, evaluator: FleetAlarmEvaluator
) -> PeriodicTask:
    """Registra ``evaluate_fleet_alarms`` cada 60 s, por organización (lo llama TASK-227)."""

    async def handler(transaction: Transaction) -> None:
        await evaluator.evaluate(transaction)

    return registry.register(
        EVALUATE_FLEET_ALARMS, EVALUATE_FLEET_ALARMS_SCHEDULE, handler, unit=ActorUnit.U03
    )


# --- GET /fleet/alarms ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlarmPage:
    """Una página del listado y el cursor de la siguiente, si hay más."""

    items: tuple[FleetAlarm, ...]
    next_after: AlarmCursor | None


def _context(value: object) -> ScopeContext:
    if not isinstance(value, ScopeContext):
        raise ContextAbsent()
    return value


@repository
class FleetAlarms:
    """``GET /fleet/alarms`` (SCR-07): las alarmas de flota del alcance de ``fleet.read``."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        authorizer: Authorizer,
        audit: AuditWriter,
        provider_organization_id: uuid.UUID,
    ) -> None:
        if type(provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._provider = provider_organization_id
        self._store = PostgresFleetAlarmStore()
        self._queries = PostgresInventoryQueries(database)

    def __repr__(self) -> str:
        return "FleetAlarms()"

    async def page(
        self,
        context: ScopeContext,
        *,
        filters: AlarmFilters | None = None,
        after: AlarmCursor | None = None,
        limit: int = MAX_ALARMS_PAGE,
    ) -> AlarmPage:
        """Una página con el alcance de ``fleet.read``; planta o nodo ajenos → ``not_found``."""
        context = _context(context)
        if type(limit) is not int or not 1 <= limit <= MAX_ALARMS_PAGE:
            raise ValueError(f"limit debe estar entre 1 y {MAX_ALARMS_PAGE}")
        filters = filters if filters is not None else AlarmFilters()
        scoped = await self._scoped(context, filters)
        async with self._database.transaction(scoped) as transaction:
            rows = await self._store.alarm_page(
                transaction, filters=filters, after=after, limit=limit + 1
            )
            if scoped.concession_id is not None:
                # BR-NUC-38: la lectura del proveedor, auditada en la misma transacción.
                await self._audit.append(
                    scoped,
                    AuditOperation.FLEET_READ,
                    plant_id=filters.plant_id,
                    resource=None
                    if filters.node_id is None
                    else ResourceRef("node", filters.node_id),
                    result_count=min(len(rows), limit),
                    transaction=transaction,
                )
        items = rows[:limit]
        last = items[-1] if len(rows) > limit and items else None
        return AlarmPage(
            items, None if last is None else AlarmCursor(last.raised_at, last.alarm_id)
        )

    async def _scoped(self, context: ScopeContext, filters: AlarmFilters) -> ScopeContext:
        """El contexto de la lectura: autorizado sobre la planta pedida (o la del nodo pedido), o
        reducido a las asignaciones con ``fleet.read``. Ajeno o fuera de alcance → ``not_found``."""
        plant_id = filters.plant_id
        if filters.node_id is not None:
            node_plant = await self._queries.node_plant(context, filters.node_id)
            if node_plant is None or (plant_id is not None and plant_id != node_plant):
                raise ResourceNotFound()
            plant_id = node_plant
        if plant_id is not None:
            if not await self._queries.plant_exists(context, plant_id):
                raise ResourceNotFound()
            return await self._authorizer.authorize(
                context, PermissionKey.FLEET_READ, Resource.plant(context.organization_id, plant_id)
            )
        reduced = narrowed(
            context, PermissionKey.FLEET_READ, provider_organization_id=self._provider
        )
        if reduced is None:
            # Ninguna asignación concede fleet.read: denegación auditada, igual que inexistente.
            await self._authorizer.deny(
                context, PermissionKey.FLEET_READ, Resource.organization(context.organization_id)
            )
        return reduced
