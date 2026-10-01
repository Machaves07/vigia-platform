"""Arrendamientos de las tareas periódicas (LC-NUC-24; PAT-NUC-RES-05; PR-NUC-42; FS-NUC-08).

Una tarea periódica (``shared.periodic_task``) la ejecuta un solo proceso a la vez. El proceso la
**toma** con una sola actualización atómica, la **renueva** mientras corre y la **libera** al
terminar; si muere, el arrendamiento vence y otro la retoma:

- ``acquire(task, owner, now)``: ``UPDATE ... SET lease_owner = yo, lease_until = ahora + 60 s
  WHERE task_name = t AND next_run_at <= ahora AND (lease_until IS NULL OR lease_until < ahora)
  RETURNING``. Gana como mucho un proceso. La ejecución se identifica por el ``next_run_at`` que
  venció (``Lease.run_at``); si es la misma que la del avance guardado, el avance se conserva y
  la retoma continúa por la organización siguiente (``Lease.resume_after``); si no, empieza de
  cero.
- ``renew(lease, now)``: alarga ``lease_until`` a ``ahora + 60 s`` solo si el arrendamiento sigue
  siendo del proceso **y no ha vencido** (``lease_until >= ahora``). Un arrendamiento vencido no
  se recupera aunque nadie lo haya tomado: el proceso que lo tenía deja de trabajar. Así, en
  cualquier instante, como mucho un proceso tiene un arrendamiento vigente de una tarea.
- ``release(lease, now, next_run_at, outcome)``: libera, fija la próxima ejecución, el resultado y,
  si no hubo fallos, ``last_success_at``. Basta con que el arrendamiento siga siendo del proceso:
  si venció y nadie lo tomó, nadie más pudo ejecutar la tarea.
- ``holds(tx, lease, now)`` y ``advance(tx, lease, organization, failed, now)``: dentro de la
  transacción de **cada organización**. ``holds`` comprueba el arrendamiento antes de invocar al
  manejador; ``advance`` mueve el cursor de avance a esa organización (y suma el fallo, si lo hubo)
  **solo si** el arrendamiento sigue vigente y es del proceso. Es la valla: si no actualiza
  nada, el planificador lanza ``LeaseLost`` y la transacción de la organización se deshace
  entera, con lo que el manejador hiciera en ella. La fila queda bloqueada por esa actualización
  hasta la confirmación, así que quien intente tomar la tarea espera a que termine y ve el avance
  ya confirmado: una organización nunca se confirma dos veces en la misma ejecución.

"Ahora" es el ``Clock`` inyectado del proceso, como en el despachador (PAT-NUC-RES-07). Con
renovación cada 20 s y arrendamiento de 60 s ``[objetivo propio]``, el proceso deja de empezar
organizaciones nuevas ``safety_margin`` antes de que su arrendamiento venza; la valla cubre una
desviación de reloj entre procesos menor que ese margen.

``InMemoryLeaseStore`` repite exactamente la semántica de ``SqlLeaseStore`` sin base (PR-NUC-42
con estado, ``tests/properties/test_leases_stateful.py``, la ejecuta contra los dos).
"""

from __future__ import annotations

import contextlib
import enum
import os
import re
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable

from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction

__all__ = [
    "LEASE_DURATION",
    "OWNER_MAX_CHARS",
    "RENEW_EVERY",
    "InMemoryLeaseStore",
    "Lease",
    "LeaseLost",
    "LeasePort",
    "LeaseSettings",
    "SqlLeaseStore",
    "TaskOutcome",
    "worker_owner_id",
]

LEASE_DURATION: Final = timedelta(seconds=60)
"""Vigencia de un arrendamiento: otro proceso retoma la tarea al vencer (PAT-NUC-RES-05)."""
RENEW_EVERY: Final = timedelta(seconds=20)
"""Intervalo de renovación mientras la tarea corre ``[objetivo propio]``."""
SAFETY_MARGIN: Final = timedelta(seconds=5)
"""Antes de vencer, el proceso deja de empezar organizaciones ``[objetivo propio]``."""

OWNER_MAX_CHARS: Final = 128
"""``periodic_task_lease_owner_length`` (nuc_0001)."""
_HOST_CHARS: Final = re.compile(r"[^A-Za-z0-9.-]+")


class TaskOutcome(enum.StrEnum):
    """``last_outcome`` de una ejecución (``periodic_task_last_outcome_format``)."""

    SUCCEEDED = "succeeded"
    PARTIAL_FAILURE = "partial_failure"
    """Alguna organización falló; las demás terminaron (BR-NUC-81)."""
    INTERRUPTED = "interrupted"
    """Parada ordenada o base caída a mitad: ``next_run_at`` no avanza y otro proceso continúa."""


class LeaseLost(Exception):
    """El proceso ya no tiene el arrendamiento: lo que iba a confirmar se deshace."""

    code = "lease_lost"


@dataclass(frozen=True, slots=True)
class LeaseSettings:
    """Tiempos del arrendamiento; los de producción son los de PAT-NUC-RES-05."""

    duration: timedelta = LEASE_DURATION
    renew_every: timedelta = RENEW_EVERY
    safety_margin: timedelta = SAFETY_MARGIN

    def __post_init__(self) -> None:
        zero = timedelta(0)
        if not zero < self.renew_every < self.duration:
            raise ValueError("renew_every debe ser positivo y menor que duration")
        if not zero <= self.safety_margin < self.duration - self.renew_every:
            raise ValueError("safety_margin debe ser menor que duration - renew_every")


@dataclass(frozen=True, slots=True)
class Lease:
    """Un arrendamiento vigente tal como lo ve el proceso que lo tiene."""

    task_name: str
    owner: str
    until: datetime
    run_at: datetime
    """``next_run_at`` que venció al tomar la tarea: identifica la ejecución."""
    resume_after: uuid.UUID | None = None
    """Última organización ya terminada de esta ejecución (por otro proceso o por este)."""
    failures: int = 0
    """Organizaciones con fallo registrado en esta ejecución."""


def worker_owner_id(random_bytes: int = 6) -> str:
    """Identificador del proceso de trabajo: ``host:pid:aleatorio`` (≤ 128 caracteres)."""
    host = _HOST_CHARS.sub("-", socket.gethostname())[:64] or "worker"
    return f"{host}:{os.getpid()}:{os.urandom(random_bytes).hex()}"[:OWNER_MAX_CHARS]


class LeasePort(Protocol):
    """Lo que el modelo de PR-NUC-42 ejercita; ``SqlLeaseStore`` e ``InMemoryLeaseStore``."""

    async def acquire(self, task_name: str, owner: str, now: datetime) -> Lease | None: ...

    async def renew(self, lease: Lease, now: datetime) -> Lease | None: ...

    async def release(
        self, lease: Lease, *, now: datetime, next_run_at: datetime, outcome: TaskOutcome
    ) -> bool: ...


def _check_owner(owner: str) -> None:
    if not isinstance(owner, str) or not 1 <= len(owner) <= OWNER_MAX_CHARS:
        raise ValueError(f"owner debe tener de 1 a {OWNER_MAX_CHARS} caracteres")


# --- En memoria ------------------------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    next_run_at: datetime
    lease_owner: str | None = None
    lease_until: datetime | None = None
    last_outcome: str | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    progress_run_at: datetime | None = None
    progress_organization_id: uuid.UUID | None = None
    progress_failures: int = 0


@dataclass
class InMemoryLeaseStore:
    """Las mismas condiciones que ``SqlLeaseStore``, sobre filas en memoria."""

    settings: LeaseSettings = field(default_factory=LeaseSettings)
    rows: dict[str, _Row] = field(default_factory=dict)

    def add_task(self, task_name: str, next_run_at: datetime) -> None:
        self.rows[task_name] = _Row(next_run_at=next_run_at)

    async def acquire(self, task_name: str, owner: str, now: datetime) -> Lease | None:
        _check_owner(owner)
        row = self.rows.get(task_name)
        if row is None or row.next_run_at > now:
            return None
        if row.lease_until is not None and not row.lease_until < now:
            return None
        if row.progress_run_at != row.next_run_at:
            row.progress_organization_id = None
            row.progress_failures = 0
        row.progress_run_at = row.next_run_at
        row.lease_owner = owner
        row.lease_until = now + self.settings.duration
        return Lease(
            task_name=task_name,
            owner=owner,
            until=row.lease_until,
            run_at=row.next_run_at,
            resume_after=row.progress_organization_id,
            failures=row.progress_failures,
        )

    def _held(self, lease: Lease, now: datetime) -> _Row | None:
        row = self.rows.get(lease.task_name)
        if (
            row is None
            or row.lease_owner != lease.owner
            or row.lease_until is None
            or row.lease_until < now
        ):
            return None
        return row

    async def renew(self, lease: Lease, now: datetime) -> Lease | None:
        row = self._held(lease, now)
        if row is None:
            return None
        row.lease_until = now + self.settings.duration
        return replace(lease, until=row.lease_until)

    async def release(
        self, lease: Lease, *, now: datetime, next_run_at: datetime, outcome: TaskOutcome
    ) -> bool:
        row = self.rows.get(lease.task_name)
        if row is None or row.lease_owner != lease.owner:
            return False
        row.lease_owner = None
        row.lease_until = None
        row.next_run_at = next_run_at
        row.last_run_at = now
        row.last_outcome = TaskOutcome(outcome).value
        if outcome is TaskOutcome.SUCCEEDED:
            row.last_success_at = now
        return True

    async def advance(
        self, lease: Lease, organization_id: uuid.UUID, *, failed: bool, now: datetime
    ) -> Lease | None:
        row = self._held(lease, now)
        if row is None or row.progress_run_at != lease.run_at:
            return None
        row.progress_organization_id = organization_id
        row.progress_failures += int(failed)
        return replace(lease, resume_after=organization_id, failures=row.progress_failures)


# --- PostgreSQL ------------------------------------------------------------------------------

# En ``SET`` todas las expresiones leen la fila **anterior**: ``progress_run_at = next_run_at``
# compara el avance guardado con la ejecución que vence ahora.
_ACQUIRE: Final = text(
    "UPDATE shared.periodic_task SET lease_owner = :owner, lease_until = :until,"
    " progress_organization_id = CASE WHEN progress_run_at = next_run_at"
    " THEN progress_organization_id END,"
    " progress_failures = CASE WHEN progress_run_at = next_run_at"
    " THEN progress_failures ELSE 0 END,"
    " progress_run_at = next_run_at"
    " WHERE task_name = :task AND next_run_at <= :now"
    " AND (lease_until IS NULL OR lease_until < :now)"
    " RETURNING next_run_at, lease_until, progress_organization_id, progress_failures"
)
_RENEW: Final = text(
    "UPDATE shared.periodic_task SET lease_until = :until"
    " WHERE task_name = :task AND lease_owner = :owner AND lease_until >= :now"
    " RETURNING lease_until"
)
_RELEASE: Final = text(
    "UPDATE shared.periodic_task SET lease_owner = NULL, lease_until = NULL,"
    " next_run_at = :next_run_at, last_run_at = :now, last_outcome = :outcome,"
    " last_success_at = CASE WHEN :succeeded THEN :now ELSE last_success_at END"
    " WHERE task_name = :task AND lease_owner = :owner"
    " RETURNING task_name"
)
_HOLDS: Final = text(
    "SELECT 1 FROM shared.periodic_task"
    " WHERE task_name = :task AND lease_owner = :owner AND lease_until >= :now"
    " AND progress_run_at = :run_at"
)
_ADVANCE: Final = text(
    "UPDATE shared.periodic_task SET progress_organization_id = :organization_id,"
    " progress_failures = progress_failures + :failed"
    " WHERE task_name = :task AND lease_owner = :owner AND lease_until >= :now"
    " AND progress_run_at = :run_at"
    " RETURNING progress_failures"
)


class TransactionSource(Protocol):
    """``shared.db.Database``: el único camino a la base."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


class SystemContexts(Protocol):
    def provider_audit_context(self) -> ScopeContext:
        """Contexto del sistema en la proveedora: ``shared.periodic_task`` es global."""
        ...


def _uuid(value: object) -> uuid.UUID | None:
    """asyncpg devuelve su propio tipo de UUID; el avance usa ``uuid.UUID``."""
    return None if value is None else uuid.UUID(str(value))


class SqlLeaseStore:
    """Arrendamientos sobre ``shared.periodic_task``, cada operación en su transacción.

    ``holds`` y ``advance`` van en la transacción de la organización que abre el planificador.
    """

    def __init__(
        self,
        *,
        database: TransactionSource,
        contexts: SystemContexts,
        settings: LeaseSettings | None = None,
    ) -> None:
        self._database = database
        self._contexts = contexts
        self.settings = settings if settings is not None else LeaseSettings()

    async def _write(
        self, statement: Executable, parameters: Mapping[str, object]
    ) -> Row[Any] | None:
        async with self._database.transaction(self._contexts.provider_audit_context()) as tx:
            return (await tx.execute(statement, parameters)).first()

    async def acquire(self, task_name: str, owner: str, now: datetime) -> Lease | None:
        _check_owner(owner)
        row = await self._write(
            _ACQUIRE,
            {"task": task_name, "owner": owner, "now": now, "until": now + self.settings.duration},
        )
        if row is None:
            return None
        return Lease(
            task_name=task_name,
            owner=owner,
            until=row.lease_until,
            run_at=row.next_run_at,
            resume_after=_uuid(row.progress_organization_id),
            failures=int(row.progress_failures),
        )

    async def renew(self, lease: Lease, now: datetime) -> Lease | None:
        row = await self._write(
            _RENEW,
            {
                "task": lease.task_name,
                "owner": lease.owner,
                "now": now,
                "until": now + self.settings.duration,
            },
        )
        if row is None:
            return None
        return replace(lease, until=row.lease_until)

    async def release(
        self, lease: Lease, *, now: datetime, next_run_at: datetime, outcome: TaskOutcome
    ) -> bool:
        outcome = TaskOutcome(outcome)
        row = await self._write(
            _RELEASE,
            {
                "task": lease.task_name,
                "owner": lease.owner,
                "now": now,
                "next_run_at": next_run_at,
                "outcome": outcome.value,
                "succeeded": outcome is TaskOutcome.SUCCEEDED,
            },
        )
        return row is not None

    @staticmethod
    def _fence(lease: Lease, now: datetime) -> dict[str, object]:
        return {"task": lease.task_name, "owner": lease.owner, "now": now, "run_at": lease.run_at}

    async def holds(self, transaction: Transaction, lease: Lease, now: datetime) -> bool:
        """¿Sigue el arrendamiento vigente y de este proceso, en esta ejecución?"""
        result = await transaction.execute(_HOLDS, self._fence(lease, now))
        return result.first() is not None

    async def advance(
        self,
        transaction: Transaction,
        lease: Lease,
        organization_id: uuid.UUID,
        *,
        failed: bool,
        now: datetime,
    ) -> Lease | None:
        """Mueve el cursor a ``organization_id`` en ``transaction``; ``None`` si ya no es suyo."""
        row = (
            await transaction.execute(
                _ADVANCE,
                {
                    **self._fence(lease, now),
                    "organization_id": organization_id,
                    "failed": int(failed),
                },
            )
        ).first()
        if row is None:
            return None
        return replace(
            lease,
            resume_after=organization_id,
            failures=int(row.progress_failures),
        )
