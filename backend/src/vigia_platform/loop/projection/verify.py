"""Verificación diaria por muestra de la proyección del lazo (TASK-306; LC-LAZ-04; PAT-LAZ-RES-03;
domain-entities.md de U-04 §9; BR-LAZ-146 a 148 y 156; NFR-LAZ-05 y 39).

``verify_loop_projection`` es la tarea diaria (``Schedule.daily``, por organización) que, para
cada planta de la organización, toma una muestra registrada (``loop.loop_projection_sample_run``,
``PAT-NUC-MAN-04``, como ``ledger.evidence_sample_run``) del **1 % de las filas y al menos 100 por
planta** ``[objetivos propios]`` y recalcula, desde el expediente, el ``projection_digest`` de
cada fila muestreada (incluidos los campos enriquecidos: el mismo ``enrich_finding`` que usa la
reconstrucción, así que con el mismo estado externo produce el mismo valor, PR-LAZ-44). El primer
día del mes ``[objetivo propio]`` la muestra es el total de la planta.

Ante cualquier divergencia: ``LoopProjectionCheckpoint.state = diverged``, el contador
``divergence_count`` avanza (se reinicia si la última divergencia fue hace más de 24 horas) y se
publica ``loop_projection_divergence`` **una sola vez por paso**, con ``divergence_count``. Hasta
la tercera divergencia en 24 horas ``[objetivo propio]`` se dispara ``rebuild_loop_projection``
para la partición; a partir de la cuarta, no: se escala sin reconstruir en bucle (NFR-LAZ-39). Una
reconstrucción que ya está en curso (``rebuild.AlreadyRebuilding``) no es un fallo de esta tarea:
otra pasada, u otro operador, ya la está corrigiendo.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import os
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.loop.domain.enums import SubjectKind
from vigia_platform.loop.projection.fold import (
    PROJECTED_RECORD_TYPES,
    FindingStateRow,
    ProjectionInput,
    step,
)
from vigia_platform.loop.projection.rebuild import (
    AlreadyRebuilding,
    ProjectionRebuilder,
    enrich_finding,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)

__all__ = [
    "DIVERGENCE_WINDOW",
    "ESCALATION_THRESHOLD",
    "PROJECTION_DIVERGENCE_EVENT",
    "SAMPLE_MINIMUM",
    "SAMPLE_PERCENT",
    "SEED_BYTES",
    "VERIFY_LOOP_PROJECTION",
    "VERIFY_LOOP_PROJECTION_SCHEDULE",
    "DivergenceOutcome",
    "ProjectionVerifier",
    "SampleRun",
    "is_full_scan_day",
    "register_verify_loop_projection",
    "sample_size",
    "select_sample",
    "verify_loop_projection_handler",
]

SAMPLE_PERCENT: Final = 1
SAMPLE_MINIMUM: Final = 100
"""1 % de las filas de la planta y al menos 100 ``[objetivos propios]`` (PAT-LAZ-RES-03)."""

SEED_BYTES: Final = 32

ESCALATION_THRESHOLD: Final = 3
"""Hasta esta cantidad de divergencias en la ventana se reconstruye; a partir de la siguiente, se
escala a incidente en vez de reconstruir en bucle ``[objetivo propio]`` (NFR-LAZ-39)."""
DIVERGENCE_WINDOW: Final = timedelta(hours=24)

PROJECTION_DIVERGENCE_EVENT: Final = "loop_projection_divergence"

_FINDING_STATE_COLUMNS: Final = tuple(field.name for field in dataclasses.fields(FindingStateRow))
_UUID_FIELDS: Final = frozenset(name for name in _FINDING_STATE_COLUMNS if name.endswith("_id"))


def sample_size(population: int) -> int:
    """El 1 % redondeado hacia arriba y al menos 100, sin pasar de la población."""
    if type(population) is not int or population < 0:
        raise ValueError("la población debe ser un entero no negativo")
    return min(population, max(SAMPLE_MINIMUM, math.ceil(population * SAMPLE_PERCENT / 100)))


def is_full_scan_day(day: date) -> bool:
    """El primer día del mes ``[objetivo propio]``: verificación sobre el total de la planta."""
    return day.day == 1


def _rank(seed: bytes, finding_state_id: uuid.UUID) -> bytes:
    return hashlib.sha256(seed + finding_state_id.bytes).digest()


def select_sample(seed: bytes, candidates: Iterable[uuid.UUID], size: int) -> tuple[uuid.UUID, ...]:
    """Los ``size`` identificadores de menor ``SHA-256(semilla ‖ id)``, en ese orden; con la misma
    semilla y el mismo conjunto, siempre la misma muestra (como ``ledger.evidence_sample``)."""
    if not isinstance(seed, bytes) or len(seed) != SEED_BYTES:
        raise ValueError(f"la semilla debe tener {SEED_BYTES} bytes")
    if type(size) is not int or size < 0:
        raise ValueError("el tamaño de la muestra debe ser un entero no negativo")
    unique = {uuid.UUID(int=candidate.int) for candidate in candidates}
    ranked = sorted(unique, key=lambda fsid: (_rank(seed, fsid), fsid.bytes))
    return tuple(ranked[:size])


def _next_divergence_count(previous_count: int, previous_at: datetime | None, now: datetime) -> int:
    """El contador tras una nueva divergencia: se reinicia si la última fue hace más de 24 horas
    (``LoopProjectionCheckpoint.divergence_count``, un contador simple, no un registro)."""
    if previous_at is None or now - previous_at > DIVERGENCE_WINDOW:
        return 1
    return previous_count + 1


@dataclass(frozen=True, slots=True)
class SampleRun:
    """Una fila de ``loop.loop_projection_sample_run``: la semilla registrada del día."""

    sample_day: date
    seed: str
    population: int
    sample_size: int
    started_at: datetime


@dataclass(frozen=True)
class DivergenceOutcome:
    """Lo que hizo una pasada de ``verify_loop_projection`` sobre una planta."""

    plant_id: uuid.UUID
    run: SampleRun
    checked: int
    diverged: tuple[uuid.UUID, ...] = ()
    divergence_count: int = 0
    rebuild_triggered: bool = False
    escalated: bool = False

    @property
    def is_diverged(self) -> bool:
        return bool(self.diverged)


_PLANTS: Final = text(
    "SELECT plant_id FROM identity.plant WHERE organization_id = :organization_id"
)

_ENSURE_CHECKPOINT: Final = text(
    "INSERT INTO loop.loop_projection_checkpoint (organization_id, plant_id)"
    " VALUES (:organization_id, :plant_id) ON CONFLICT (organization_id, plant_id) DO NOTHING"
)
_RUN: Final = text(
    "SELECT sample_day, seed, population, sample_size, started_at"
    " FROM loop.loop_projection_sample_run"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND sample_day = :sample_day"
)
_INSERT_RUN: Final = text(
    "INSERT INTO loop.loop_projection_sample_run"
    " (organization_id, plant_id, sample_day, seed, population, sample_size, started_at)"
    " VALUES (:organization_id, :plant_id, :sample_day, :seed, :population, :sample_size,"
    " :started_at)"
    " ON CONFLICT (organization_id, plant_id, sample_day) DO NOTHING"
    " RETURNING sample_day, seed, population, sample_size, started_at"
)
_POPULATION: Final = text(
    "SELECT finding_state_id FROM loop.finding_state"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
# Las columnas vienen de `dataclasses.fields(...)`, nunca de la petición (NFR-NUC-19); se arman
# aparte y `text()` solo envuelve el resultado ya hecho (VIG001).
_finding_state_columns_csv = ", ".join(_FINDING_STATE_COLUMNS)
_SELECTED_SQL = (
    f"SELECT {_finding_state_columns_csv} FROM loop.finding_state"  # noqa: S608
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND finding_state_id = ANY(CAST(:finding_state_ids AS uuid[]))"
)
_SELECTED: Final = text(_SELECTED_SQL)
_SUBJECT_RECORDS: Final = text(
    "SELECT record_id, organization_id, plant_id, chain_sequence, record_type, received_at, content"
    " FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND record_type = ANY(CAST(:record_types AS text[]))"
    " AND ("
    " content_json ->> 'subject_id' = :subject"
    " OR content_json -> 'episode' ->> 'episode_id' = :subject"
    " OR content_json ->> 'detection_id' = :subject"
    " OR content_json ->> 'episode_id' = :subject"
    " )"
    " ORDER BY chain_sequence"
)
_EPISODE_SEGMENTS: Final = text(
    "SELECT segment_record_ids FROM loop.episode"
    " WHERE organization_id = :organization_id AND episode_id = :episode_id"
)
_CHECKPOINT: Final = text(
    "SELECT divergence_count, last_divergence_at FROM loop.loop_projection_checkpoint"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_MARK_DIVERGED: Final = text(
    "UPDATE loop.loop_projection_checkpoint SET state = 'diverged',"
    " divergence_count = :divergence_count, last_divergence_at = :at"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_MARK_VERIFIED: Final = text(
    "UPDATE loop.loop_projection_checkpoint SET last_verified_at = :at"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _optional_uuid(value: Any) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _decode_run(row: Row[Any]) -> SampleRun:
    return SampleRun(
        sample_day=row.sample_day,
        seed=row.seed,
        population=int(row.population),
        sample_size=int(row.sample_size),
        started_at=row.started_at,
    )


def _decode_finding(row: Row[Any]) -> FindingStateRow:
    data = row._mapping
    values: dict[str, Any] = {}
    for name in _FINDING_STATE_COLUMNS:
        value = data[name]
        values[name] = _optional_uuid(value) if name in _UUID_FIELDS else value
    return FindingStateRow(**values)


async def _replay_subject(
    transaction: Transaction, context: ScopeContext, plant_id: uuid.UUID, subject: uuid.UUID
) -> FindingStateRow | None:
    """El estado de ``subject`` tal como lo produce el pliegue puro, desde su historia completa en
    el expediente (sin enriquecer: ``enrich_finding`` lo hace después, igual que la
    reconstrucción)."""
    rows = (
        await transaction.execute(
            _SUBJECT_RECORDS,
            {
                "organization_id": context.organization_id,
                "plant_id": plant_id,
                "record_types": list(PROJECTED_RECORD_TYPES),
                "subject": str(subject),
            },
        )
    ).all()
    episode = None
    finding = None
    for row in rows:
        document: dict[str, Any] = json.loads(bytes(row.content))
        item = ProjectionInput(
            record_id=_uuid(row.record_id),
            organization_id=_uuid(row.organization_id),
            plant_id=_uuid(row.plant_id),
            chain_sequence=int(row.chain_sequence),
            record_type=str(row.record_type),
            received_at=row.received_at,
            document=document,
        )
        result = step(episode, finding, item)
        if result.applied:
            episode, finding = result.episode, result.finding
    return finding


@repository
class ProjectionVerifier:
    """``verify_loop_projection`` (LC-LAZ-04, ``verificar``)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        rebuilder: ProjectionRebuilder,
        outbox: OutboxPort,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._rebuilder = rebuilder
        self._outbox = outbox
        self._clock = clock
        self._random_bytes = random_bytes

    async def verify_organization(
        self, context: ScopeContext, day: date | None = None
    ) -> tuple[DivergenceOutcome, ...]:
        """Verifica cada planta de la organización del contexto."""
        rows = await self._database.read(
            context, _PLANTS, {"organization_id": context.organization_id}
        )
        outcomes = [await self.verify_plant(context, _uuid(row.plant_id), day) for row in rows]
        return tuple(outcomes)

    async def verify_plant(
        self, context: ScopeContext, plant_id: uuid.UUID, day: date | None = None
    ) -> DivergenceOutcome:
        """Verifica una planta: muestra registrada (o el total, el primer día del mes)."""
        if day is None:
            day = self._clock.now().astimezone(UTC).date()
        full_scan = is_full_scan_day(day)
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                _ENSURE_CHECKPOINT,
                {"organization_id": context.organization_id, "plant_id": plant_id},
            )
            population = [
                _uuid(row.finding_state_id)
                for row in (
                    await transaction.execute(
                        _POPULATION,
                        {"organization_id": context.organization_id, "plant_id": plant_id},
                    )
                ).all()
            ]
            run = await self._run(transaction, context, plant_id, day, population, full_scan)
            selected = select_sample(bytes.fromhex(run.seed), population, run.sample_size)
            diverged = await self._check(transaction, context, plant_id, selected)
            outcome = await self._record(transaction, context, plant_id, run, selected, diverged)
        if outcome.rebuild_triggered:
            with contextlib.suppress(AlreadyRebuilding):
                await self._rebuilder.rebuild_plant(context, plant_id)
        return outcome

    # --- semilla registrada -------------------------------------------------------------------

    async def _run(
        self,
        transaction: Transaction,
        context: ScopeContext,
        plant_id: uuid.UUID,
        day: date,
        population: Sequence[uuid.UUID],
        full_scan: bool,
    ) -> SampleRun:
        params = {
            "organization_id": context.organization_id,
            "plant_id": plant_id,
            "sample_day": day,
        }
        existing = (await transaction.execute(_RUN, params)).first()
        if existing is not None:
            return _decode_run(existing)
        seed = self._random_bytes(SEED_BYTES)
        if not isinstance(seed, bytes) or len(seed) != SEED_BYTES:
            raise ValueError(f"random_bytes debe devolver {SEED_BYTES} bytes")
        size = len(population) if full_scan else sample_size(len(population))
        inserted = (
            await transaction.execute(
                _INSERT_RUN,
                {
                    **params,
                    "seed": seed.hex(),
                    "population": len(population),
                    "sample_size": size,
                    "started_at": self._clock.now(),
                },
            )
        ).first()
        if inserted is not None:
            return _decode_run(inserted)
        # Otra pasada registró la semilla entre la lectura y la inserción: manda la suya.
        return _decode_run((await transaction.execute(_RUN, params)).one())

    # --- comparación ---------------------------------------------------------------------------

    async def _check(
        self,
        transaction: Transaction,
        context: ScopeContext,
        plant_id: uuid.UUID,
        selected: Sequence[uuid.UUID],
    ) -> tuple[uuid.UUID, ...]:
        if not selected:
            return ()
        stored_rows = (
            await transaction.execute(
                _SELECTED,
                {
                    "organization_id": context.organization_id,
                    "plant_id": plant_id,
                    "finding_state_ids": list(selected),
                },
            )
        ).all()
        stored = {_uuid(row.finding_state_id): _decode_finding(row) for row in stored_rows}
        now = self._clock.now()
        diverged: list[uuid.UUID] = []
        for finding_state_id in selected:
            live = stored.get(finding_state_id)
            if live is None:
                diverged.append(finding_state_id)
                continue
            recomputed = await _replay_subject(transaction, context, plant_id, live.subject_id)
            if recomputed is None:
                diverged.append(finding_state_id)
                continue
            segments = await self._segments(transaction, context, recomputed)
            enriched = await enrich_finding(
                self._rebuilder.enrichment,
                transaction,
                context=context,
                plant_id=plant_id,
                finding=recomputed,
                segment_record_ids=segments,
                now=now,
            )
            if enriched.projection_digest != live.projection_digest:
                diverged.append(finding_state_id)
        return tuple(diverged)

    async def _segments(
        self, transaction: Transaction, context: ScopeContext, finding: FindingStateRow
    ) -> tuple[uuid.UUID, ...]:
        if finding.subject_kind != SubjectKind.FINDING:
            return ()
        row = (
            await transaction.execute(
                _EPISODE_SEGMENTS,
                {"organization_id": context.organization_id, "episode_id": finding.subject_id},
            )
        ).first()
        if row is None:
            return ()
        return tuple(_uuid(item) for item in row.segment_record_ids)

    # --- divergencia y escalamiento ------------------------------------------------------------

    async def _record(
        self,
        transaction: Transaction,
        context: ScopeContext,
        plant_id: uuid.UUID,
        run: SampleRun,
        selected: Sequence[uuid.UUID],
        diverged: tuple[uuid.UUID, ...],
    ) -> DivergenceOutcome:
        now = self._clock.now()
        if not diverged:
            await transaction.execute(
                _MARK_VERIFIED,
                {"organization_id": context.organization_id, "plant_id": plant_id, "at": now},
            )
            return DivergenceOutcome(plant_id=plant_id, run=run, checked=len(selected))
        checkpoint = (
            await transaction.execute(
                _CHECKPOINT, {"organization_id": context.organization_id, "plant_id": plant_id}
            )
        ).one()
        count = _next_divergence_count(
            int(checkpoint.divergence_count), checkpoint.last_divergence_at, now
        )
        await transaction.execute(
            _MARK_DIVERGED,
            {
                "organization_id": context.organization_id,
                "plant_id": plant_id,
                "divergence_count": count,
                "at": now,
            },
        )
        rebuild_triggered = count <= ESCALATION_THRESHOLD
        await self._outbox.publish(
            transaction,
            NewEvent(
                event_name=PROJECTION_DIVERGENCE_EVENT,
                plant_id=plant_id,
                payload={
                    "rows_checked": len(selected),
                    "rows_diverged": len(diverged),
                    "full_scan": len(selected) == run.population,
                    "divergence_count": count,
                },
            ),
        )
        return DivergenceOutcome(
            plant_id=plant_id,
            run=run,
            checked=len(selected),
            diverged=diverged,
            divergence_count=count,
            rebuild_triggered=rebuild_triggered,
            escalated=not rebuild_triggered,
        )


# --- Registro de la tarea -----------------------------------------------------------------------

VERIFY_LOOP_PROJECTION: Final = "verify_loop_projection"
VERIFY_LOOP_PROJECTION_SCHEDULE: Final = Schedule.daily(hour=2)
"""Diaria a las 02:00 UTC ``[objetivo propio]``: tras ``evidence_sample`` (01:00) y
``write_checkpoints`` (00:00)."""


def verify_loop_projection_handler(verifier: ProjectionVerifier) -> PeriodicHandler:
    """Manejador de ``verify_loop_projection`` para ``PeriodicTaskRegistry``."""

    async def handler(transaction: Transaction) -> None:
        await verifier.verify_organization(transaction.context)

    return handler


def register_verify_loop_projection(
    registry: PeriodicTaskRegistry, handler: PeriodicHandler
) -> PeriodicTask:
    """Registra la tarea diaria ``verify_loop_projection`` de U-04 (LC-LAZ-04)."""
    return registry.register(
        VERIFY_LOOP_PROJECTION, VERIFY_LOOP_PROJECTION_SCHEDULE, handler, unit=ActorUnit.U04
    )
