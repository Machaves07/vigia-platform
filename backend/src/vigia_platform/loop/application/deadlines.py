"""``loop.deadlines`` (LC-LAZ-11; C-PLA-25; TASK-313): vencimientos, escalamiento y su panel.

**Tarea ``evaluate_deadlines``** (horaria, por organización, ``U04_TASKS``; BLM §3, BR-LAZ-46 y
47): con el arrendamiento del planificador de U-02 y la transacción de la organización
(``ScopeContext`` de iteración periódica: el fallo de una organización no detiene a las demás),
declara el **vencimiento** de cada acción vigente cuyo ``due_at`` pasó, en un hallazgo en
``action_assigned`` u ``overdue`` (sin cierre, sin declaración de incerrable y **no** suspendido por
reclasificación: esos estados no entran). Un registro ``action_overdue`` **por nivel** de
escalamiento (``EscalationLevel``):

- ``coordinator``: al llegar ``due_at``. Es T08 (``action_assigned → overdue``) de
  ``loop.domain.lifecycle``; publica ``finding_overdue`` y ``finding_escalated`` con su nivel;
- ``manager_weekly``: al cumplirse ``manager_highlight_overdue_days`` días naturales de vencimiento
  (30, fuente: PRD §10 «más de 30 días escala a gerente» y DE §2.2 «≥ 30 lo destaca ante el
  gerente»), leído de la configuración **vigente** de la planta. No es una transición (el pliegue
  solo sube ``overdue_days``); publica ``finding_escalated`` con su nivel.

**Clave determinista** (nota U04-H-07 de DE §2.13 y §5.2, que manda sobre BR-LAZ-46):
``overdue_id = UUID v5 (action_id, due_at, escalation_level)`` (``overdue_id``). El contenido de
cada registro es función **solo** de la acción y del nivel: ``declared_at`` es el instante en que se
alcanzó el nivel (``due_at``, o ``due_at`` más el umbral) y ``days_overdue`` sus días (0 o el
umbral). Así, tras una caída, la puesta al día declara **todos** los vencimientos que no se
procesaron, cada uno con la marca de su día, y escribe exactamente los mismos registros que la
ejecución continua (NFR-LAZ-13, PR-LAZ-48); el instante real de la escritura queda en el sobre del
expediente (``received_at``). Una clave ya escrita no se vuelve a escribir: ejecutar la tarea dos
veces, o dos a la vez con el arrendamiento vencido (FS-LAZ-10), no crea un segundo registro, y el
paso de ``coordinator`` a ``manager_weekly`` escribe su propio registro en vez de chocar con el
primero (``idempotency_conflict``).

**Candados** (``loop.application.locks``, el orden de TASK-311): la tarea recorre varios
hallazgos, así que primero **lee** sin candados (fase 1: candidatos por el índice parcial
``finding_state_overdue`` y las claves ya escritas, por páginas, hasta ``batch_size`` pendientes) y
después, antes de escribir nada, toma **todos** los candados de una vez y en orden: la exclusión de
cada hallazgo ordenada por ``subject_id`` (paso 1) y las filas de ``loop.corrective_action`` de
todas sus acciones con ``SELECT … ORDER BY action_id FOR UPDATE`` (paso 2). Con todo tomado
relee la proyección y las claves y solo entonces escribe, por
``LoopProjectionStore.write_and_apply`` (pasos 3 a 5: cadena de la planta, control de la
proyección, filas del sujeto), el registro, sus
eventos y la proyección en la misma transacción, y pasa la acción a ``overdue`` en
``loop.corrective_action``. Lo que pasó a otro estado entre las dos fases (un cierre, una
reasignación, una reclasificación) se descarta sin escribir. Más de ``batch_size`` pendientes se
declaran en la ejecución siguiente, con el mismo contenido.

**Declarar un vencimiento no cierra, no oculta y no retira la gestionabilidad** (P5, BR-LAZ-41 y
46): ningún camino de este módulo escribe ``closure``, ni cambia un estado que no sea T08.

**Panel de vencidos** (SUP-U05-15 ``consultar_vencidos``, ``OverdueService.page``):
``executive_summary.read`` sobre la **planta** (BR-LAZ-47 y 48, H-27). Desde la proyección, los
hallazgos vencidos: todo ``overdue`` declarado (también si su acción se reasignó después con otro
plazo: sigue vencido, P5) y todo ``action_assigned`` con el plazo pasado aún sin declarar (lo
vencido se ve aunque la tarea esté atrasada), por zona, con paginación por clave sobre
``(due_at, finding_state_id)``. Los días cuentan desde ``overdue_since`` (la primera declaración;
sin declarar, el plazo) y se **destacan** los de ``manager_highlight_overdue_days`` o más.
Ningún dato de persona observada: nombra la zona y al responsable de la acción (un usuario de la
plataforma, BR-LAZ-38). Una planta o una zona inexistente, de otra organización o
fuera de alcance responde ``not_found`` (nunca ``forbidden``). Sin auditoría de consulta
(BR-LAZ-09).

La hora sale siempre del ``Clock`` inyectado; ninguna sentencia lee la hora de la base.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text
from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    RecordScope,
)
from vigia_platform.loop.application.locks import lock_finding
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import (
    EscalationLevel,
    FindingLifecycleState,
    InboxPartitionNotice,
)
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.outbox.registries import (
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "ACTION_OVERDUE",
    "EVALUATE_DEADLINES_BATCH_SIZE",
    "EVALUATE_DEADLINES_SCHEDULE",
    "EVALUATE_DEADLINES_TASK",
    "FINDING_ESCALATED",
    "FINDING_OVERDUE",
    "MAX_OVERDUE_PAGE",
    "MAX_OVERDUE_ZONES",
    "OVERDUE_NAMESPACE",
    "DeadlineEvaluator",
    "LevelDue",
    "OverdueCursor",
    "OverdueDeclared",
    "OverdueFilters",
    "OverdueItem",
    "OverduePage",
    "OverdueRequestInvalid",
    "OverdueService",
    "OverdueZone",
    "days_overdue",
    "levels_due",
    "overdue_id",
    "register_evaluate_deadlines",
]

EVALUATE_DEADLINES_TASK: Final = "evaluate_deadlines"
EVALUATE_DEADLINES_SCHEDULE: Final = Schedule.every(3600)
"""Horaria (BLM §3, LC-LAZ-11)."""
EVALUATE_DEADLINES_BATCH_SIZE: Final = 200
"""Registros pendientes que una ejecución declara por organización; el resto, en la siguiente (con
el mismo contenido). Acota la transacción de la organización y sus candados (NFR-LAZ-01: p95 ≤ 30 s
por organización, ``[objetivo propio]``)."""
_PAGE_SIZE: Final = 500
"""Candidatos que se leen por página en la fase 1 (sin candados)."""

ACTION_OVERDUE: Final = "action_overdue"
FINDING_OVERDUE: Final = "finding_overdue"
FINDING_ESCALATED: Final = "finding_escalated"

OVERDUE_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "urn:vigia:loop:action_overdue")
"""Espacio de nombres del UUID v5 de ``overdue_id`` (fijo: cambiarlo cambia todas las claves)."""

MAX_OVERDUE_PAGE: Final = 200
"""Página máxima del panel de vencidos (LC-LAZ §6: ``limit`` ≤ 200)."""
MAX_OVERDUE_ZONES: Final = 50
"""Zonas por consulta del panel (``zone_id`` repetible), como la bandeja."""
MAX_OVERDUE_DAYS: Final = 3_660
"""Tope de ``min_overdue_days`` (el de ``Days`` de los registros)."""

S = FindingLifecycleState
_ONE_DAY: Final = timedelta(days=1)

_CANDIDATES: Final = text(
    "SELECT finding_state_id, plant_id, zone_id, subject_kind, subject_id, lifecycle_state,"
    " current_action_id, due_at FROM loop.finding_state"
    " WHERE organization_id = :organization_id"
    " AND lifecycle_state IN ('action_assigned', 'overdue')"
    " AND current_action_id IS NOT NULL AND due_at IS NOT NULL AND due_at <= :now"
    " AND (CAST(:after_plant AS uuid) IS NULL"
    " OR (plant_id, due_at, finding_state_id)"
    " > (CAST(:after_plant AS uuid), CAST(:after_due AS timestamptz),"
    " CAST(:after_id AS uuid)))"
    " ORDER BY plant_id, due_at, finding_state_id LIMIT :limit"
)
"""Fase 1: el índice parcial ``finding_state_overdue`` (organización, planta, ``due_at``)."""
_WRITTEN_KEYS: Final = text(
    "SELECT source_key FROM ledger.record_source_key"
    " WHERE organization_id = :organization_id AND record_type = 'action_overdue'"
    " AND source_key = ANY(CAST(:keys AS text[]))"
)
"""Las claves de ``action_overdue`` ya escritas (la unicidad del expediente, BR-NUC-48)."""
_LOCK_ACTIONS: Final = text(
    "SELECT action_id FROM loop.corrective_action"
    " WHERE organization_id = :organization_id AND action_id = ANY(CAST(:action_ids AS uuid[]))"
    " ORDER BY action_id FOR UPDATE"
)
"""Paso 2 de ``locks.LOCK_ORDER`` para varios hallazgos: todas las filas de una vez y en orden."""
_FRESH: Final = text(
    "SELECT finding_state_id, plant_id, zone_id, subject_kind, subject_id, lifecycle_state,"
    " current_action_id, due_at, assignee_user_id, assignee_inactive FROM loop.finding_state"
    " WHERE organization_id = :organization_id"
    " AND subject_id = ANY(CAST(:subject_ids AS uuid[]))"
)
_MARK_OVERDUE: Final = text(
    "UPDATE loop.corrective_action SET state = 'overdue'"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND action_id = :action_id AND state = 'assigned'"
)

_DECLARABLE: Final = frozenset({S.ACTION_ASSIGNED, S.OVERDUE})


# --- piezas puras ---------------------------------------------------------------------------------


def overdue_id(action_id: uuid.UUID, due_at: datetime, level: EscalationLevel) -> uuid.UUID:
    """``UUID v5 (action_id, due_at, escalation_level)`` (nota U04-H-07 de DE §2.13 y §5.2)."""
    if type(action_id) is not uuid.UUID or not isinstance(level, EscalationLevel):
        raise TypeError("overdue_id necesita un action_id UUID y un EscalationLevel")
    return uuid.uuid5(OVERDUE_NAMESPACE, f"{action_id}|{format_timestamp(due_at)}|{level.value}")


def days_overdue(due_at: datetime, now: datetime) -> int:
    """Días naturales completos desde ``due_at`` hasta ``now`` (0 antes de cumplirse uno)."""
    return max(0, (now - due_at) // _ONE_DAY)


@dataclass(frozen=True, slots=True)
class LevelDue:
    """Un nivel de escalamiento ya alcanzado por una acción, con su contenido determinista."""

    level: EscalationLevel
    declared_at: datetime
    """El instante en que se alcanzó el nivel (nunca el de la ejecución)."""
    days_overdue: int


def levels_due(due_at: datetime, now: datetime, highlight_days: int) -> tuple[LevelDue, ...]:
    """Los niveles que una acción con plazo ``due_at`` ya alcanzó en ``now``, en orden.

    ``coordinator`` desde ``due_at`` (el plazo es el fin exclusivo de su último día hábil);
    ``manager_weekly`` desde ``due_at + highlight_days`` días naturales."""
    if type(highlight_days) is not int or highlight_days < 1:
        raise ValueError("highlight_days es un entero ≥ 1")
    if now < due_at:
        return ()
    due: list[LevelDue] = [LevelDue(EscalationLevel.COORDINATOR, due_at, 0)]
    escalated_at = due_at + highlight_days * _ONE_DAY
    if now >= escalated_at:
        due.append(LevelDue(EscalationLevel.MANAGER_WEEKLY, escalated_at, highlight_days))
    return tuple(due)


# --- la tarea -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Finding:
    finding_state_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    subject_id: uuid.UUID
    state: FindingLifecycleState
    action_id: uuid.UUID
    due_at: datetime


@dataclass(frozen=True, slots=True)
class _Pending:
    finding: _Finding
    due: LevelDue
    overdue_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class OverdueDeclared:
    """Un registro ``action_overdue`` escrito por esta ejecución."""

    finding_state_id: uuid.UUID
    action_id: uuid.UUID
    overdue_id: uuid.UUID
    record_id: uuid.UUID
    escalation_level: EscalationLevel
    declared_at: datetime
    days_overdue: int
    transition_id: str | None
    """``T08`` si el hallazgo pasó a ``overdue``; ``None`` en un escalamiento."""


def _uuid(value: object) -> uuid.UUID:
    # El controlador devuelve su propia subclase de UUID: se normaliza a ``uuid.UUID``.
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


def _finding(row: Any) -> _Finding:
    data = row._mapping
    return _Finding(
        finding_state_id=_uuid(data["finding_state_id"]),
        plant_id=_uuid(data["plant_id"]),
        zone_id=_uuid(data["zone_id"]),
        subject_kind=str(data["subject_kind"]),
        subject_id=_uuid(data["subject_id"]),
        state=S(data["lifecycle_state"]),
        action_id=_uuid(data["current_action_id"]),
        due_at=data["due_at"],
    )


@repository
class DeadlineEvaluator:
    """``evaluate_deadlines``: declara los vencimientos y su escalamiento, sin cerrar nada (P5)."""

    def __init__(
        self,
        *,
        writer: EscritorExpediente,
        clock: Clock,
        configuration: PostgresLoopConfigurationRepository,
        store: LoopProjectionStore | None = None,
        batch_size: int = EVALUATE_DEADLINES_BATCH_SIZE,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._writer = writer
        self._clock = clock
        self._configuration = configuration
        self._store = store if store is not None else LoopProjectionStore()
        self._batch_size = batch_size
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "DeadlineEvaluator()"

    async def evaluate(self, transaction: Transaction) -> tuple[OverdueDeclared, ...]:
        """Una pasada sobre la organización de ``transaction`` (la del planificador)."""
        organization_id = transaction.context.organization_id
        now = to_millisecond(self._clock.now())
        pending = await self._pending(transaction, organization_id, now)
        if not pending:
            return ()
        # Pasos 1 y 2 de locks.LOCK_ORDER para todos los hallazgos, antes de escribir nada.
        for subject_id in sorted({item.finding.subject_id for item in pending}):
            await lock_finding(transaction, organization_id, subject_id)
        await self._between_locks(transaction)
        await transaction.execute(
            _LOCK_ACTIONS,
            {
                "organization_id": organization_id,
                "action_ids": sorted({str(item.finding.action_id) for item in pending}),
            },
        )
        fresh = await self._fresh(transaction, organization_id, pending)
        written = await self._written_keys(
            transaction, organization_id, [str(item.overdue_id) for item in pending]
        )
        confirmed = [
            item
            for item in pending
            if str(item.overdue_id) not in written and _still_due(item, fresh)
        ]
        await self._checked(transaction)
        states = {subject: S(row["lifecycle_state"]) for subject, row in fresh.items()}
        declared: list[OverdueDeclared] = []
        for item in confirmed:
            subject_id = item.finding.subject_id
            done = await self._declare(transaction, fresh[subject_id], states[subject_id], item)
            # Los dos niveles de una acción en la misma pasada: el segundo ya ve ``overdue``.
            states[subject_id] = S.OVERDUE
            declared.append(done)
        return tuple(declared)

    async def _between_locks(self, transaction: Transaction) -> None:
        """Entre la exclusión de los hallazgos y las filas de sus acciones: no-op en producción;
        gancho de las pruebas de concurrencia."""

    async def _checked(self, transaction: Transaction) -> None:
        """Con todos los candados tomados y lo pendiente releído, antes de escribir: no-op en
        producción; gancho de las pruebas de concurrencia."""

    # --- fase 1: sin candados --------------------------------------------------------------------

    async def _pending(
        self, transaction: Transaction, organization_id: uuid.UUID, now: datetime
    ) -> list[_Pending]:
        thresholds: dict[uuid.UUID, int] = {}
        pending: list[_Pending] = []
        after: _Finding | None = None
        while len(pending) < self._batch_size:
            rows = (
                await transaction.execute(
                    _CANDIDATES,
                    {
                        "organization_id": organization_id,
                        "now": now,
                        "after_plant": None if after is None else str(after.plant_id),
                        "after_due": None if after is None else after.due_at,
                        "after_id": None if after is None else str(after.finding_state_id),
                        "limit": _PAGE_SIZE,
                    },
                )
            ).all()
            candidates: list[_Pending] = []
            for row in rows:
                finding = _finding(row)
                if finding.plant_id not in thresholds:
                    settings, _version = await self._configuration.settings_in(
                        transaction, finding.plant_id
                    )
                    thresholds[finding.plant_id] = settings["manager_highlight_overdue_days"]
                for due in levels_due(finding.due_at, now, thresholds[finding.plant_id]):
                    key = overdue_id(finding.action_id, finding.due_at, due.level)
                    candidates.append(_Pending(finding, due, key))
            if candidates:
                written = await self._written_keys(
                    transaction, organization_id, [str(item.overdue_id) for item in candidates]
                )
                pending.extend(item for item in candidates if str(item.overdue_id) not in written)
            if len(rows) < _PAGE_SIZE:
                break
            after = _finding(rows[-1])
        return pending[: self._batch_size]

    @staticmethod
    async def _written_keys(
        transaction: Transaction, organization_id: uuid.UUID, keys: Sequence[str]
    ) -> frozenset[str]:
        if not keys:
            return frozenset()
        result = await transaction.execute(
            _WRITTEN_KEYS, {"organization_id": organization_id, "keys": list(keys)}
        )
        return frozenset(str(row.source_key) for row in result.all())

    # --- fase 2: con los candados ----------------------------------------------------------------

    @staticmethod
    async def _fresh(
        transaction: Transaction, organization_id: uuid.UUID, pending: Sequence[_Pending]
    ) -> dict[uuid.UUID, Mapping[str, Any]]:
        result = await transaction.execute(
            _FRESH,
            {
                "organization_id": organization_id,
                "subject_ids": sorted({str(item.finding.subject_id) for item in pending}),
            },
        )
        return {_uuid(row.subject_id): dict(row._mapping) for row in result.all()}

    async def _declare(
        self,
        transaction: Transaction,
        row: Mapping[str, Any],
        state: FindingLifecycleState,
        item: _Pending,
    ) -> OverdueDeclared:
        finding = item.finding
        due = item.due
        record_id = uuid7(self._clock, self._random_bytes)
        content: dict[str, Any] = {
            "overdue_id": str(item.overdue_id),
            "action_id": str(finding.action_id),
            "subject_kind": finding.subject_kind,
            "subject_id": str(finding.subject_id),
            "due_at": format_timestamp(finding.due_at),
            "declared_at": format_timestamp(due.declared_at),
            "days_overdue": due.days_overdue,
            "escalation_level": due.level.value,
            "assignee_user_id": str(row["assignee_user_id"]),
            "assignee_inactive": bool(row["assignee_inactive"]),
        }
        payload = {
            "zone_id": str(finding.zone_id),
            "subject_kind": finding.subject_kind,
            "subject_id": str(finding.subject_id),
            "action_id": str(finding.action_id),
            "overdue_id": str(item.overdue_id),
            "record_id": str(record_id),
            "due_at": format_timestamp(finding.due_at),
            "days_overdue": due.days_overdue,
            "escalation_level": due.level.value,
            "assignee_user_id": str(row["assignee_user_id"]),
        }
        names = (
            (FINDING_OVERDUE, FINDING_ESCALATED)
            if due.level is EscalationLevel.COORDINATOR
            else (FINDING_ESCALATED,)
        )
        result, step = await self._store.write_and_apply(
            transaction,
            self._writer,
            transaction.context,
            ACTION_OVERDUE,
            content,
            scope=RecordScope(plant_id=finding.plant_id, zone_id=finding.zone_id),
            events=tuple(NewEvent(event_name=name, payload=dict(payload)) for name in names),
            occurred_at=due.declared_at,
            record_id=record_id,
        )
        if isinstance(result, LedgerRejection):
            raise RuntimeError(f"action_overdue rechazado: {result.code.value}")
        if result.status is not AcceptanceStatus.ACCEPTED or not step.applied:
            # La clave se releyó con los candados tomados: un duplicado aquí es un defecto.
            raise RuntimeError("action_overdue con una clave ya escrita tras releerla")
        expected = "T08" if state is S.ACTION_ASSIGNED else None
        if step.transition_id != expected:
            raise RuntimeError("la proyección no aplicó el vencimiento recién escrito")
        if due.level is EscalationLevel.COORDINATOR:
            await transaction.execute(
                _MARK_OVERDUE,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": finding.plant_id,
                    "action_id": finding.action_id,
                },
            )
        return OverdueDeclared(
            finding_state_id=finding.finding_state_id,
            action_id=finding.action_id,
            overdue_id=item.overdue_id,
            record_id=result.record_id,
            escalation_level=due.level,
            declared_at=due.declared_at,
            days_overdue=due.days_overdue,
            transition_id=step.transition_id,
        )


def _still_due(item: _Pending, fresh: Mapping[uuid.UUID, Mapping[str, Any]]) -> bool:
    """¿Sigue vigente lo leído en la fase 1, ya con los candados? (mismo estado declarable, misma
    acción y mismo plazo)."""
    row = fresh.get(item.finding.subject_id)
    if row is None or row["current_action_id"] is None or row["due_at"] is None:
        return False
    return (
        S(row["lifecycle_state"]) in _DECLARABLE
        and _uuid(row["current_action_id"]) == item.finding.action_id
        and row["due_at"] == item.finding.due_at
        and row["assignee_user_id"] is not None
    )


def register_evaluate_deadlines(
    registry: PeriodicTaskRegistry, evaluator: DeadlineEvaluator
) -> PeriodicTask:
    """Registra ``evaluate_deadlines`` (horaria, por organización, TASK-313)."""

    async def handler(transaction: Transaction) -> None:
        await evaluator.evaluate(transaction)

    return registry.register(
        EVALUATE_DEADLINES_TASK,
        EVALUATE_DEADLINES_SCHEDULE,
        handler,
        unit=ActorUnit.U04,
        iteration=TaskIteration.PER_ORGANIZATION,
    )


# --- el panel de vencidos (SUP-U05-15) ------------------------------------------------------------


class OverdueRequestInvalid(Exception):
    """Un filtro con valor imposible: zonas vacías o de más, periodo vacío, límite o días fuera de
    rango."""


class _Database(Protocol):
    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True)
class OverdueFilters:
    """Los filtros del panel (BLM SUP-U05-15: zonas, periodo y umbral de días)."""

    zone_ids: tuple[uuid.UUID, ...] | None = None
    due_from: datetime | None = None
    """Periodo ``[due_from, due_to)`` sobre ``due_at``."""
    due_to: datetime | None = None
    min_overdue_days: int = 0


@dataclass(frozen=True, slots=True)
class OverdueCursor:
    due_at: datetime
    finding_state_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class OverdueItem:
    """Una acción vencida: la zona, el hallazgo, la acción y su responsable (un usuario de la
    plataforma, BR-LAZ-38); **ningún** dato de persona observada (BR-LAZ-47, H-27)."""

    finding_state_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    subject_kind: str
    family: str
    tier: str
    lifecycle_state: FindingLifecycleState
    declared: bool
    """``True`` si la tarea ya declaró el vencimiento (``overdue``); ``False`` si el plazo pasó y
    la declaración llega en la próxima ejecución (P5: lo vencido se ve igual)."""
    action_id: uuid.UUID
    action_type: str | None
    assignee_user_id: uuid.UUID | None
    assignee_inactive: bool
    due_at: datetime
    """El plazo de la acción **vigente** (otro, posterior, si se reasignó tras vencer)."""
    overdue_since: datetime
    """Desde cuándo está vencido el hallazgo: la primera declaración (``overdue_since`` de la
    proyección) o, sin declarar todavía, el plazo."""
    overdue_days: int
    """Días naturales desde ``overdue_since``."""
    highlighted: bool
    cursor: OverdueCursor


@dataclass(frozen=True, slots=True)
class OverdueZone:
    zone_id: uuid.UUID
    overdue_count: int
    highlighted_count: int


@dataclass(frozen=True, slots=True)
class OverduePage:
    plant_id: uuid.UUID
    highlight_threshold_days: int
    items: tuple[OverdueItem, ...]
    zones: tuple[OverdueZone, ...]
    """Las zonas con al menos una acción vencida en el filtro (todas las páginas)."""
    partition_notice: InboxPartitionNotice | None
    """La proyección de la planta en reconstrucción o divergente (BR-LAZ-156)."""
    next_cursor: OverdueCursor | None


_PLANT: Final = text(
    "SELECT plant_id FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_ZONES: Final = text(
    "SELECT zone_id FROM identity.zone WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
)
_ITEMS: Final = text(
    "SELECT f.finding_state_id, f.plant_id, f.zone_id, f.subject_kind, f.family, f.tier,"
    " f.lifecycle_state, f.current_action_id, f.current_action_type, f.assignee_user_id,"
    " f.assignee_inactive, f.due_at, f.overdue_since FROM loop.finding_state AS f"
    " WHERE f.organization_id = :organization_id AND f.plant_id = :plant_id"
    " AND f.lifecycle_state IN ('action_assigned', 'overdue')"
    " AND f.current_action_id IS NOT NULL AND f.due_at IS NOT NULL"
    " AND COALESCE(f.overdue_since, f.due_at) <= :latest_due"
    " AND (CAST(:zone_ids AS uuid[]) IS NULL OR f.zone_id = ANY(CAST(:zone_ids AS uuid[])))"
    " AND (CAST(:due_from AS timestamptz) IS NULL OR f.due_at >= CAST(:due_from AS timestamptz))"
    " AND (CAST(:due_to AS timestamptz) IS NULL OR f.due_at < CAST(:due_to AS timestamptz))"
    " AND (CAST(:after_due AS timestamptz) IS NULL"
    " OR (f.due_at, f.finding_state_id)"
    " > (CAST(:after_due AS timestamptz), CAST(:after_id AS uuid)))"
    " ORDER BY f.due_at, f.finding_state_id LIMIT :limit"
)
"""Las acciones vencidas de la planta, por el índice parcial ``finding_state_overdue``."""
_BY_ZONE: Final = text(
    "SELECT f.zone_id, count(*) AS overdue_count,"
    " count(*) FILTER (WHERE COALESCE(f.overdue_since, f.due_at) <= :highlight_due)"
    " AS highlighted_count"
    " FROM loop.finding_state AS f"
    " WHERE f.organization_id = :organization_id AND f.plant_id = :plant_id"
    " AND f.lifecycle_state IN ('action_assigned', 'overdue')"
    " AND f.current_action_id IS NOT NULL AND f.due_at IS NOT NULL"
    " AND COALESCE(f.overdue_since, f.due_at) <= :latest_due"
    " AND (CAST(:zone_ids AS uuid[]) IS NULL OR f.zone_id = ANY(CAST(:zone_ids AS uuid[])))"
    " AND (CAST(:due_from AS timestamptz) IS NULL OR f.due_at >= CAST(:due_from AS timestamptz))"
    " AND (CAST(:due_to AS timestamptz) IS NULL OR f.due_at < CAST(:due_to AS timestamptz))"
    " GROUP BY f.zone_id ORDER BY f.zone_id"
)
"""El recuento por zona con el mismo filtro que ``_ITEMS`` (todas las páginas)."""
_PARTITION: Final = text(
    "SELECT state FROM loop.loop_projection_checkpoint"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND state IN ('rebuilding', 'diverged')"
)
_PARTITION_NOTICES: Final[Mapping[str, InboxPartitionNotice]] = {
    "rebuilding": InboxPartitionNotice.PROJECTION_REBUILDING,
    "diverged": InboxPartitionNotice.PROJECTION_DIVERGED,
}
_READ: Final = PermissionKey.EXECUTIVE_SUMMARY_READ


def _validated(filters: OverdueFilters, limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_OVERDUE_PAGE:
        raise OverdueRequestInvalid("limit")
    if filters.zone_ids is not None and not 1 <= len(filters.zone_ids) <= MAX_OVERDUE_ZONES:
        raise OverdueRequestInvalid("zone_id")
    days = filters.min_overdue_days
    if type(days) is not int or not 0 <= days <= MAX_OVERDUE_DAYS:
        raise OverdueRequestInvalid("min_overdue_days")
    if (
        filters.due_from is not None
        and filters.due_to is not None
        and filters.due_from >= filters.due_to
    ):
        raise OverdueRequestInvalid("period")


@repository
class OverdueService:
    """``consultar_vencidos`` (SUP-U05-15): el panel de vencidos de la planta."""

    def __init__(
        self,
        *,
        database: _Database,
        authorizer: Authorizer,
        configuration: PostgresLoopConfigurationRepository,
        clock: Clock,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._configuration = configuration
        self._clock = clock

    def __repr__(self) -> str:
        return "OverdueService()"

    async def page(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        *,
        filters: OverdueFilters | None = None,
        after: OverdueCursor | None = None,
        limit: int = MAX_OVERDUE_PAGE,
    ) -> OverduePage:
        filters = filters if filters is not None else OverdueFilters()
        _validated(filters, limit)
        if not isinstance(context, ScopeContext) or type(plant_id) is not uuid.UUID:
            raise ResourceNotFound()
        organization_id = context.organization_id
        resource = Resource.plant(organization_id, plant_id)
        async with self._database.transaction(context) as transaction:
            exists = (
                await transaction.execute(
                    _PLANT, {"organization_id": organization_id, "plant_id": plant_id}
                )
            ).first()
        if exists is None:
            # Inexistente o de otra organización: la misma denegación auditada que fuera de alcance.
            await self._authorizer.deny(context, _READ, resource)
        authorized = await self._authorizer.authorize(context, _READ, resource)
        if authorized.concession_id is not None:  # pragma: no cover - la matriz ya lo deniega
            await self._authorizer.deny(authorized, _READ, resource)
        now = to_millisecond(self._clock.now())
        async with self._database.transaction(authorized) as transaction:
            if filters.zone_ids is not None:
                await self._zones_of_plant(transaction, authorized, plant_id, filters.zone_ids)
            settings, _version = await self._configuration.settings_in(transaction, plant_id)
            threshold = settings["manager_highlight_overdue_days"]
            parameters = {
                "organization_id": organization_id,
                "plant_id": plant_id,
                "latest_due": now - filters.min_overdue_days * _ONE_DAY,
                "zone_ids": None
                if filters.zone_ids is None
                else [str(zone) for zone in filters.zone_ids],
                "due_from": filters.due_from,
                "due_to": filters.due_to,
            }
            rows = (
                await transaction.execute(
                    _ITEMS,
                    {
                        **parameters,
                        "after_due": None if after is None else after.due_at,
                        "after_id": None if after is None else str(after.finding_state_id),
                        "limit": limit + 1,
                    },
                )
            ).all()
            zones = (
                await transaction.execute(
                    _BY_ZONE, {**parameters, "highlight_due": now - threshold * _ONE_DAY}
                )
            ).all()
            partition = (
                await transaction.execute(
                    _PARTITION, {"organization_id": organization_id, "plant_id": plant_id}
                )
            ).first()
        items = tuple(_item(dict(row._mapping), now, threshold) for row in rows[:limit])
        more = len(rows) > limit
        return OverduePage(
            plant_id=plant_id,
            highlight_threshold_days=threshold,
            items=items,
            zones=tuple(
                OverdueZone(
                    zone_id=_uuid(row.zone_id),
                    overdue_count=int(row.overdue_count),
                    highlighted_count=int(row.highlighted_count),
                )
                for row in zones
            ),
            partition_notice=None
            if partition is None
            else _PARTITION_NOTICES[str(partition.state)],
            next_cursor=items[-1].cursor if more and items else None,
        )

    async def _zones_of_plant(
        self,
        transaction: Transaction,
        context: ScopeContext,
        plant_id: uuid.UUID,
        zone_ids: tuple[uuid.UUID, ...],
    ) -> None:
        """Cada zona pedida es de la planta; si no (inexistente, de otra planta o de otra
        organización), ``not_found`` con la denegación auditada."""
        found = {
            _uuid(row.zone_id)
            for row in (
                await transaction.execute(
                    _ZONES,
                    {
                        "organization_id": context.organization_id,
                        "plant_id": plant_id,
                        "zone_ids": [str(zone) for zone in zone_ids],
                    },
                )
            ).all()
        }
        for zone_id in zone_ids:
            if zone_id not in found:
                await self._authorizer.deny(
                    context, _READ, Resource(context.organization_id, "zone", zone_id)
                )


def _item(row: Mapping[str, Any], now: datetime, threshold: int) -> OverdueItem:
    due_at: datetime = row["due_at"]
    # Vencido desde la primera declaración: una reasignación da otro plazo a la acción, pero el
    # hallazgo sigue vencido desde entonces (P5); sin declarar todavía, desde su plazo.
    since: datetime = row["overdue_since"] or due_at
    days = days_overdue(since, now)
    state = S(row["lifecycle_state"])
    finding_state_id = _uuid(row["finding_state_id"])
    assignee = row["assignee_user_id"]
    return OverdueItem(
        finding_state_id=finding_state_id,
        plant_id=_uuid(row["plant_id"]),
        zone_id=_uuid(row["zone_id"]),
        subject_kind=str(row["subject_kind"]),
        family=str(row["family"]),
        tier=str(row["tier"]),
        lifecycle_state=state,
        declared=state is S.OVERDUE,
        action_id=_uuid(row["current_action_id"]),
        action_type=row["current_action_type"],
        assignee_user_id=None if assignee is None else _uuid(assignee),
        assignee_inactive=bool(row["assignee_inactive"]),
        due_at=due_at,
        overdue_since=since,
        overdue_days=days,
        highlighted=days >= threshold,
        cursor=OverdueCursor(due_at, finding_state_id),
    )
