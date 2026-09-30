"""Tarea diaria ``evidence_sample``: la marca dentro del contenedor, sobre una muestra (TASK-121).

LC-NUC-15 parte 2; NFR-NUC-33, PAT-NUC-MAN-04, BR-NUC-64 (nota fechada) y pendiente nº 21
(adenda A-14). Al registrar, la escritura verificó la marca **por el metadato del objeto**
(``x-amz-meta-vigia-anonymized``) sin descargar el clip; esta tarea comprueba cada día, sobre una
muestra, que el **contenedor MP4** lleva la marca ``comment = vigia_anonymized=1`` (BR-CTR-14).

**La muestra** (el planificador la invoca una vez por organización): los clips ``video/mp4``
registrados el día UTC anterior (``verified_at`` en ``[día, día + 1)``); se toma el 1 % redondeado
hacia arriba y al menos 10, o todos si hay menos. La semilla son 32 bytes aleatorios que se
**registran** antes de muestrear en ``ledger.evidence_sample_run`` (una fila por organización y
día, de solo anexar) y en una entrada de auditoría ``integrity_verification``, encadenada. La
selección es una función pura de la semilla y del conjunto de identificadores
(``select_sample``: orden por ``SHA-256(semilla ‖ evidence_id)``), así que con la semilla
registrada se vuelve a calcular la misma muestra. Una segunda pasada del mismo día reutiliza la
semilla y solo verifica lo que sigue ``pending``.

**La verificación** de cada clip va fuera de toda transacción (PAT-NUC-RES-08): ``HEAD`` del
objeto, descarga **de esa versión** en el worker y comprobación de que los bytes son los
verificados al registrar (tamaño y ``sha256``); después, ``read_container_marker``:

- marca presente → ``intact``;
- contenedor legible sin la marca → ``broken`` con ``marker_missing``;
- contenedor que no se puede leer → ``broken`` con ``metadata_unreadable``;
- objeto ausente o con bytes distintos de los verificados → ``broken`` con
  ``marker_unverifiable`` (la marca de **esa** evidencia no se puede verificar).

**El resultado** se escribe en una transacción por evidencia: la única transición
``pending → intact | broken`` de ``marker_verification_result`` con ``marker_verified_at`` y
``container_marker_sampled_at`` (el disparador de ``nuc_0006`` rechaza cualquier otra). Si es
``broken``, en la misma transacción: el evento ``evidence_marker_verification_failed`` (solo
identificadores, enumeraciones y marcas), un ``security_alert`` con
``alert_kind = evidence_marker_mismatch`` y una entrada de auditoría ``integrity_verification``
con resultado ``error``. **Nunca se borra nada** (P4): el objeto y el hallazgo se conservan, y la
marca ``broken`` es la marca de revisión que U-04 lee por ``resultados_marca``.

Si el almacén no responde con un clip, ese clip sigue ``pending`` y la pasada termina en
``StorageUnavailable`` después de verificar los demás: el planificador la reintenta y la nueva
pasada, con la misma semilla, solo repite los pendientes.

Solo se muestrean clips ``video/mp4``: la marca del contenedor que fija el contrato es la del MP4
(BR-CTR-14); la de una imagen JPEG es un ``[objetivo propio]`` del stub de U-01.
"""

from __future__ import annotations

import enum
import hashlib
import math
import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.evidence_read import EVIDENCE_RESOURCE_KIND, MarkerResult
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.container_marker import ContainerMarker, read_container_marker
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ContextAbsent, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)
from vigia_platform.shared.storage import ObjectHead, StorageUnavailable

__all__ = [
    "EVIDENCE_SAMPLE",
    "EVIDENCE_SAMPLE_SCHEDULE",
    "MARKER_FAILED_EVENT",
    "MAX_SAMPLED_CLIP_BYTES",
    "SAMPLED_CONTENT_TYPE",
    "SAMPLE_MINIMUM",
    "SAMPLE_PERCENT",
    "SECURITY_ALERT_KIND",
    "SEED_BYTES",
    "VERIFICATION_METHOD",
    "EvidenceSampleStorage",
    "EvidenceSampler",
    "FailureReason",
    "SampleOutcome",
    "SampleRun",
    "evidence_sample_handler",
    "register_evidence_sample",
    "sample_size",
    "sample_window",
    "select_sample",
]

EVIDENCE_SAMPLE: Final = "evidence_sample"
EVIDENCE_SAMPLE_SCHEDULE: Final = Schedule.daily(hour=1)
"""Diaria a la 01:00 UTC ``[objetivo propio]``: el día anterior ya está cerrado y no coincide con
``write_checkpoints`` (00:00)."""

SAMPLE_PERCENT: Final = 1
SAMPLE_MINIMUM: Final = 10
"""1 % de los clips del día, redondeado hacia arriba, y al menos 10 (NFR-NUC-33)."""

SEED_BYTES: Final = 32
SAMPLED_CONTENT_TYPE: Final = "video/mp4"
MAX_SAMPLED_CLIP_BYTES: Final = 52_428_800
"""Tamaño máximo de un clip del contrato (BR-CTR-06): nada mayor se descarga."""

VERIFICATION_METHOD: Final = "full_read"
"""``verification_method`` de la verificación diferida: lee el contenedor entero."""

MARKER_FAILED_EVENT: Final = "evidence_marker_verification_failed"
SECURITY_ALERT_EVENT: Final = "security_alert"
SECURITY_ALERT_KIND: Final = "evidence_marker_mismatch"
SAMPLE_CHECK: Final = "container_marker"

_log = get_logger("ledger.evidence_sample")


class FailureReason(enum.StrEnum):
    """``failure_reason`` de ``evidence_marker_verification_failed`` (lista cerrada, A-14)."""

    MARKER_MISSING = "marker_missing"
    MARKER_UNVERIFIABLE = "marker_unverifiable"
    METADATA_UNREADABLE = "metadata_unreadable"


# --- Selección (función pura) ------------------------------------------------------------------


def sample_size(population: int) -> int:
    """El 1 % redondeado hacia arriba y al menos 10, sin pasar de la población."""
    if type(population) is not int or population < 0:
        raise ValueError("la población debe ser un entero no negativo")
    return min(population, max(SAMPLE_MINIMUM, math.ceil(population * SAMPLE_PERCENT / 100)))


def _rank(seed: bytes, evidence_id: uuid.UUID) -> bytes:
    return hashlib.sha256(seed + evidence_id.bytes).digest()


def select_sample(seed: bytes, candidates: Iterable[uuid.UUID], size: int) -> tuple[uuid.UUID, ...]:
    """Los ``size`` identificadores de menor ``SHA-256(semilla ‖ id)``, en ese orden.

    No depende del orden de ``candidates`` ni de repetidos: con la misma semilla y el mismo
    conjunto devuelve siempre la misma muestra.
    """
    if not isinstance(seed, bytes) or len(seed) != SEED_BYTES:
        raise ValueError(f"la semilla debe tener {SEED_BYTES} bytes")
    if type(size) is not int or size < 0:
        raise ValueError("el tamaño de la muestra debe ser un entero no negativo")
    unique = {uuid.UUID(int=candidate.int) for candidate in candidates}
    ranked = sorted(unique, key=lambda evidence_id: (_rank(seed, evidence_id), evidence_id.bytes))
    return tuple(ranked[:size])


def sample_window(day: date) -> tuple[datetime, datetime]:
    """``[día 00:00, día + 1 00:00)`` en UTC."""
    start = datetime.combine(day, time(0), tzinfo=UTC)
    return start, start + timedelta(days=1)


def _timestamp(moment: datetime) -> str:
    """``Timestamp`` del contrato: UTC con milisegundos y ``Z``."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


# --- Valores -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SampleRun:
    """Una fila de ``ledger.evidence_sample_run``: la semilla registrada de un día."""

    sample_day: date
    seed: str
    population: int
    sample_size: int
    started_at: datetime


@dataclass(frozen=True)
class SampleOutcome:
    """Lo que hizo una pasada de ``evidence_sample`` sobre una organización."""

    run: SampleRun
    selected: tuple[uuid.UUID, ...]
    intact: tuple[uuid.UUID, ...] = ()
    broken: dict[uuid.UUID, FailureReason] = field(default_factory=dict)
    already_verified: tuple[uuid.UUID, ...] = ()
    unavailable: tuple[uuid.UUID, ...] = ()


class EvidenceSampleStorage(Protocol):
    """Lo que la muestra usa de ``StoragePort``: ``head_object`` y ``get_object``."""

    async def head_object(self, key: str) -> ObjectHead | None: ...

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes: ...


# --- Sentencias -------------------------------------------------------------------------------

_RUN: Final = text(
    "SELECT sample_day, seed, population, sample_size, started_at"
    " FROM ledger.evidence_sample_run"
    " WHERE organization_id = :organization_id AND sample_day = :sample_day"
)

_INSERT_RUN: Final = text(
    "INSERT INTO ledger.evidence_sample_run"
    " (organization_id, sample_day, seed, population, sample_size, started_at)"
    " VALUES (:organization_id, :sample_day, :seed, :population, :sample_size, :started_at)"
    " ON CONFLICT (organization_id, sample_day) DO NOTHING"
    " RETURNING sample_day, seed, population, sample_size, started_at"
)

_POPULATION: Final = text(
    "SELECT e.evidence_id FROM ledger.evidence AS e"
    " WHERE e.organization_id = :organization_id AND e.content_type = :content_type"
    " AND e.verified_at >= :window_start AND e.verified_at < :window_end"
)

_SELECTED: Final = text(
    "SELECT e.evidence_id, e.verified_at, e.record_id, e.plant_id, e.zone_id, e.node_id,"
    " e.clip_id, e.storage_key, e.sha256, e.size_bytes, e.marker_verification_result"
    " FROM ledger.evidence AS e"
    " WHERE e.organization_id = :organization_id"
    " AND e.evidence_id = ANY(CAST(:evidence_ids AS uuid[]))"
    " AND e.verified_at >= :window_start AND e.verified_at < :window_end"
)

_MARK: Final = text(
    "UPDATE ledger.evidence SET marker_verification_result = :result,"
    " marker_verified_at = :marker_verified_at,"
    " container_marker_sampled_at = :container_marker_sampled_at"
    " WHERE organization_id = :organization_id AND evidence_id = :evidence_id"
    " AND verified_at = :verified_at AND marker_verification_result = 'pending'"
    " RETURNING evidence_id"
)


def _plain(value: uuid.UUID) -> uuid.UUID:
    return uuid.UUID(int=value.int)


def _run(row: Row[Any]) -> SampleRun:
    return SampleRun(
        sample_day=row.sample_day,
        seed=row.seed,
        population=int(row.population),
        sample_size=int(row.sample_size),
        started_at=row.started_at,
    )


@dataclass(frozen=True, slots=True)
class _Inspection:
    result: MarkerResult
    reason: FailureReason | None
    sampled_at: datetime


# --- El servicio --------------------------------------------------------------------------------


@repository
class EvidenceSampler:
    """La muestra diaria de una organización (``evidence_sample``)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        audit: AuditWriter,
        outbox: OutboxPort,
        storage: EvidenceSampleStorage,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._audit = audit
        self._outbox = outbox
        self._storage = storage
        self._clock = clock
        self._random_bytes = random_bytes

    async def sample_day(self, context: ScopeContext, day: date | None = None) -> SampleOutcome:
        """Muestrea ``day`` (por omisión, el día UTC anterior al reloj) en la organización."""
        if not isinstance(context, ScopeContext):
            raise ContextAbsent()
        if day is None:
            day = self._clock.now().astimezone(UTC).date() - timedelta(days=1)
        elif not isinstance(day, date) or isinstance(day, datetime):
            raise TypeError("day debe ser datetime.date")
        window_start, window_end = sample_window(day)
        window = {
            "organization_id": context.organization_id,
            "window_start": window_start,
            "window_end": window_end,
        }
        population = [
            _plain(row.evidence_id)
            for row in await self._database.read(
                context, _POPULATION, {**window, "content_type": SAMPLED_CONTENT_TYPE}
            )
        ]
        run = await self._run(context, day, len(population))
        if run.population != len(population):
            _log.warning(
                "la población del día muestreado cambió desde que se registró la semilla",
                registered=run.population,
                current=len(population),
            )
        selected = select_sample(bytes.fromhex(run.seed), population, run.sample_size)
        if not selected:
            return SampleOutcome(run=run, selected=())
        rows = {
            _plain(row.evidence_id): row
            for row in await self._database.read(
                context, _SELECTED, {**window, "evidence_ids": list(selected)}
            )
        }
        intact: list[uuid.UUID] = []
        broken: dict[uuid.UUID, FailureReason] = {}
        already: list[uuid.UUID] = []
        unavailable: list[uuid.UUID] = []
        for evidence_id in selected:
            row = rows[evidence_id]
            if row.marker_verification_result != MarkerResult.PENDING:
                already.append(evidence_id)
                continue
            try:
                inspection = await self._inspect(row)
            except StorageUnavailable:
                unavailable.append(evidence_id)
                continue
            if not await self._record(context, row, inspection):
                already.append(evidence_id)
            elif inspection.reason is None:
                intact.append(evidence_id)
            else:
                broken[evidence_id] = inspection.reason
        return SampleOutcome(
            run=run,
            selected=selected,
            intact=tuple(intact),
            broken=broken,
            already_verified=tuple(already),
            unavailable=tuple(unavailable),
        )

    # --- semilla registrada ---------------------------------------------------------------------

    async def _run(self, context: ScopeContext, day: date, population: int) -> SampleRun:
        """La semilla del día: la registrada o, si no hay, una nueva registrada ahora."""
        existing = await self._database.read(
            context, _RUN, {"organization_id": context.organization_id, "sample_day": day}
        )
        if existing:
            return _run(existing[0])
        seed = self._random_bytes(SEED_BYTES)
        if not isinstance(seed, bytes) or len(seed) != SEED_BYTES:
            raise ValueError(f"random_bytes debe devolver {SEED_BYTES} bytes")
        size = sample_size(population)
        async with self._database.transaction(context) as transaction:
            inserted = (
                await transaction.execute(
                    _INSERT_RUN,
                    {
                        "organization_id": context.organization_id,
                        "sample_day": day,
                        "seed": seed.hex(),
                        "population": population,
                        "sample_size": size,
                        "started_at": self._clock.now(),
                    },
                )
            ).first()
            if inserted is not None:
                await self._audit.append(
                    context,
                    AuditOperation.INTEGRITY_VERIFICATION,
                    filters={
                        "task": EVIDENCE_SAMPLE,
                        "sample_day": day.isoformat(),
                        "seed": seed.hex(),
                        "population": population,
                        "sample_size": size,
                    },
                    result_count=size,
                    transaction=transaction,
                )
        if inserted is not None:
            return _run(inserted)
        # Otra pasada registró la semilla entre la lectura y la inserción: manda la suya.
        rows = await self._database.read(
            context, _RUN, {"organization_id": context.organization_id, "sample_day": day}
        )
        return _run(rows[0])

    # --- verificación de un clip ----------------------------------------------------------------

    async def _inspect(self, row: Row[Any]) -> _Inspection:
        """Lee el contenedor de la versión verificada; ``StorageUnavailable`` si no se pudo."""
        size = int(row.size_bytes)
        try:
            head = await self._storage.head_object(row.storage_key)
            content: bytes | None = None
            if (
                head is not None
                and head.size_bytes == size <= MAX_SAMPLED_CLIP_BYTES
                and head.full_object_sha256_hex == row.sha256
            ):
                content = await self._storage.get_object(
                    row.storage_key, version_id=head.version_id
                )
        except ValueError:
            # Clave o versión que el puerto no acepta: la marca de esta evidencia no se verifica.
            content = None
        sampled_at = self._clock.now()
        if (
            content is None
            or len(content) != size
            or hashlib.sha256(content).hexdigest() != row.sha256
        ):
            return _Inspection(MarkerResult.BROKEN, FailureReason.MARKER_UNVERIFIABLE, sampled_at)
        marker = read_container_marker(content)
        if marker is ContainerMarker.PRESENT:
            return _Inspection(MarkerResult.INTACT, None, sampled_at)
        reason = (
            FailureReason.MARKER_MISSING
            if marker is ContainerMarker.ABSENT
            else FailureReason.METADATA_UNREADABLE
        )
        return _Inspection(MarkerResult.BROKEN, reason, sampled_at)

    # --- escritura del resultado ----------------------------------------------------------------

    async def _record(self, context: ScopeContext, row: Row[Any], inspection: _Inspection) -> bool:
        """Escribe la transición y, si es ``broken``, evento, alerta y auditoría; atómico.

        ``False`` si la evidencia ya no estaba ``pending`` (otra pasada la verificó).
        """
        evidence_id = _plain(row.evidence_id)
        plant_id = _plain(row.plant_id)
        zone_id = _plain(row.zone_id)
        verified_at = self._clock.now()
        async with self._database.transaction(context) as transaction:
            updated = (
                await transaction.execute(
                    _MARK,
                    {
                        "result": inspection.result.value,
                        "marker_verified_at": verified_at,
                        "container_marker_sampled_at": inspection.sampled_at,
                        "organization_id": context.organization_id,
                        "evidence_id": evidence_id,
                        "verified_at": row.verified_at,
                    },
                )
            ).first()
            if updated is None:
                return False
            if inspection.reason is not None:
                await self._declare(
                    transaction, context, row, inspection.reason, inspection.sampled_at
                )
        if inspection.reason is not None:
            _log.warning(
                "la muestra diaria no encontró la marca de anonimización en el contenedor",
                evidence_id=str(evidence_id),
                plant_id=str(plant_id),
                zone_id=str(zone_id),
                failure_reason=inspection.reason.value,
            )
        return True

    async def _declare(
        self,
        transaction: Transaction,
        context: ScopeContext,
        row: Row[Any],
        reason: FailureReason,
        sampled: datetime,
    ) -> None:
        evidence_id = _plain(row.evidence_id)
        plant_id = _plain(row.plant_id)
        zone_id = _plain(row.zone_id)
        sampled_at = _timestamp(sampled)
        await self._outbox.publish(
            transaction,
            NewEvent(
                event_name=MARKER_FAILED_EVENT,
                plant_id=plant_id,
                payload={
                    "evidence_id": str(evidence_id),
                    "record_id": str(_plain(row.record_id)),
                    "organization_id": str(context.organization_id),
                    "plant_id": str(plant_id),
                    "zone_id": str(zone_id),
                    "node_id": str(_plain(row.node_id)),
                    "clip_id": str(_plain(row.clip_id)),
                    "verification_method": VERIFICATION_METHOD,
                    "container_marker_sampled_at": sampled_at,
                    "failure_reason": reason.value,
                },
            ),
        )
        await self._outbox.publish(
            transaction,
            NewEvent(
                event_name=SECURITY_ALERT_EVENT,
                plant_id=plant_id,
                payload={
                    "alert_kind": SECURITY_ALERT_KIND,
                    "resource_kind": EVIDENCE_RESOURCE_KIND,
                    "resource_id": str(evidence_id),
                    "occurred_at": sampled_at,
                },
            ),
        )
        await self._audit.append(
            context,
            AuditOperation.INTEGRITY_VERIFICATION,
            outcome=AuditOutcome.ERROR,
            plant_id=plant_id,
            zone_id=zone_id,
            resource=ResourceRef(EVIDENCE_RESOURCE_KIND, evidence_id),
            filters={
                "task": EVIDENCE_SAMPLE,
                "check": SAMPLE_CHECK,
                "verification_method": VERIFICATION_METHOD,
                "failure_reason": reason.value,
            },
            result_count=1,
            transaction=transaction,
        )


# --- Registro de la tarea -----------------------------------------------------------------------


def evidence_sample_handler(sampler: EvidenceSampler) -> PeriodicHandler:
    """Manejador de ``evidence_sample`` para ``PeriodicTaskRegistry``.

    Termina en ``StorageUnavailable`` si algún clip no pudo leerse, después de verificar los
    demás, para que el planificador reintente la pasada (con la misma semilla).
    """

    async def handler(transaction: Transaction) -> None:
        outcome = await sampler.sample_day(transaction.context)
        if outcome.unavailable:
            raise StorageUnavailable(EVIDENCE_SAMPLE)

    return handler


def register_evidence_sample(
    registry: PeriodicTaskRegistry, handler: PeriodicHandler
) -> PeriodicTask:
    """Registra la tarea diaria ``evidence_sample`` de U-02 (nota del 2026-09-20 de §4.3)."""
    return registry.register(EVIDENCE_SAMPLE, EVIDENCE_SAMPLE_SCHEDULE, handler, unit=ActorUnit.U02)
