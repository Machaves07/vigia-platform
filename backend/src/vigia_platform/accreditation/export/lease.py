"""Arrendamiento de una solicitud de exportación (TASK-317; PAT-LAZ-RES-01; DE §3.1, nota del
2026-09-21 y U04-H-08).

El mismo mecanismo que ``shared.worker.leases`` para ``PeriodicTask`` (PAT-NUC-RES-05), sobre la
fila de ``accreditation.export_request``: **no es un mecanismo nuevo**, son las mismas tres
condiciones con otra tabla.

- ``acquire(context, export_id, owner, now)``: **una sola sentencia** ``UPDATE … SET lease_owner =
  yo, lease_until = ahora + 60 s, attempt_count = attempt_count + 1, state = 'in_progress'
  WHERE export_id = … AND state IN ('requested', 'in_progress') AND (lease_owner IS NULL OR
  lease_until < ahora) RETURNING …``: gana como mucho un proceso. ``attempt_count`` sube en cada
  adquisición (el primer intento es el 1; una retoma, el siguiente) y la fila devuelve lo que hace
  falta para retomar: la subida por partes del intento anterior y lo fijado en el primero
  (``checkpoint_record_ids`` y ``generated_at``, U04-H-08).
- **Una exportación en curso por organización** (PAT-LAZ-RES-01): la sentencia exige además que
  ninguna otra solicitud de la organización esté ``in_progress`` con un arrendamiento vigente.
  Como esa condición mira **otras** filas, la transacción toma antes el candado consultivo de la
  organización (``accreditation.export_run|<organización>``): dos adquisiciones de la misma
  organización se serializan y la segunda ve la primera ya confirmada.
- ``renew(context, lease, now)``: alarga ``lease_until`` solo si sigue siendo del proceso **y no
  ha vencido** (``lease_until >= ahora``): un arrendamiento vencido no se recupera aunque nadie lo
  haya tomado, y el proceso deja de trabajar.
- Las escrituras del intento (``fix_first_attempt``, ``record_upload``, ``publish_progress``,
  ``finish_ready``, ``finish_failed``) llevan la **valla**: ``lease_owner = yo AND lease_until >=
  ahora``. Si no actualizan nada, ``LeaseLost`` y lo de esa transacción se deshace (el paquete
  incluido): un proceso que perdió la solicitud nunca la cierra.
- ``release(context, lease)`` (fallo no declarado del intento): libera si sigue siendo del
  proceso, aunque haya vencido; la solicitud sigue ``in_progress`` para la retoma.

"Ahora" es siempre el ``Clock`` inyectado (PAT-LAZ-MAN-06). ``InMemoryExportLeaseStore`` repite
la misma semántica sin base; la propiedad con estado (``tests/properties/laz/
test_laz_export_lease_stateful.py``) ejecuta las dos contra un modelo.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.accreditation.export.composer_port import ExportPackage
from vigia_platform.loop.domain.enums import ExportState, PackageForm
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.worker.leases import LeaseLost

__all__ = [
    "LEASE_DURATION",
    "PROGRESS_EVERY",
    "RENEW_EVERY",
    "RUN_LOCK_PREFIX",
    "Acquisition",
    "ExportLease",
    "ExportLeasePort",
    "ExportLeaseSettings",
    "InMemoryExportLeaseStore",
    "LeaseLost",
    "SqlExportLeaseStore",
]

LEASE_DURATION: Final = timedelta(seconds=60)
"""Vence a los 60 s (PAT-LAZ-RES-01 = PAT-NUC-RES-05)."""
RENEW_EVERY: Final = timedelta(seconds=20)
"""Se renueva cada 20 s mientras se compone."""
PROGRESS_EVERY: Final = timedelta(seconds=30)
"""El progreso se publica al menos cada 30 s (NFR-LAZ-12), además de al terminar cada artefacto."""
RUN_LOCK_PREFIX: Final = "accreditation.export_run|"
"""Clave del candado consultivo de la adquisición, seguida de la organización."""

_OPEN_STATES: Final = (ExportState.REQUESTED.value, ExportState.IN_PROGRESS.value)
_CURRENT_ARTIFACT_MAX: Final = 128
_UPLOAD_ID_MAX: Final = 256


@dataclass(frozen=True, slots=True)
class ExportLeaseSettings:
    """Tiempos del arrendamiento y del progreso; los de producción son los del diseño."""

    duration: timedelta = LEASE_DURATION
    renew_every: timedelta = RENEW_EVERY
    progress_every: timedelta = PROGRESS_EVERY

    def __post_init__(self) -> None:
        if not timedelta(0) < self.renew_every < self.duration:
            raise ValueError("renew_every debe ser positivo y menor que duration")
        if self.progress_every <= timedelta(0):
            raise ValueError("progress_every debe ser positivo")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportLease:
    """Un arrendamiento vigente tal como lo ve el proceso que lo tiene."""

    export_id: uuid.UUID
    owner: str
    until: datetime
    attempt_count: int
    """El número de este intento (1 en el primero)."""
    previous_upload_id: str | None
    """La subida por partes que dejó el intento anterior (abortarla antes de componer)."""
    checkpoint_record_ids: tuple[uuid.UUID, ...] | None
    generated_at: datetime | None
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_ids: tuple[uuid.UUID, ...]
    period_from: datetime
    period_to: datetime
    package_form: PackageForm
    requested_by: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: uuid.UUID

    @property
    def retaken(self) -> bool:
        """Hubo un intento anterior que no terminó (se compone de nuevo desde cero)."""
        return self.attempt_count > 1


@dataclass(frozen=True, slots=True)
class Acquisition:
    """Resultado de ``acquire``: el arrendamiento, o por qué no."""

    lease: ExportLease | None
    settled: bool = False
    """La solicitud no existe o ya terminó (``ready``, ``failed``): no hay nada que componer."""


class ExportLeasePort(Protocol):
    """Lo que el consumidor y la propiedad con estado usan."""

    async def acquire(
        self, context: ScopeContext, export_id: uuid.UUID, owner: str, now: datetime
    ) -> Acquisition: ...

    async def renew(
        self, context: ScopeContext, lease: ExportLease, now: datetime
    ) -> ExportLease | None: ...

    async def release(self, context: ScopeContext, lease: ExportLease) -> bool: ...


def _check_owner(owner: str) -> None:
    if not isinstance(owner, str) or not 1 <= len(owner) <= 128:
        raise ValueError("owner debe tener de 1 a 128 caracteres")


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


# --- PostgreSQL ------------------------------------------------------------------------------

_RUN_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_ACQUIRE: Final = text(
    "UPDATE accreditation.export_request AS request SET lease_owner = :owner,"
    " lease_until = :until, attempt_count = request.attempt_count + 1, state = 'in_progress',"
    " started_at = COALESCE(request.started_at, :now)"
    " WHERE request.export_id = :export_id AND request.state IN ('requested', 'in_progress')"
    " AND (request.lease_owner IS NULL OR request.lease_until < :now)"
    " AND NOT EXISTS (SELECT 1 FROM accreditation.export_request AS other"
    " WHERE other.organization_id = request.organization_id"
    " AND other.export_id <> request.export_id AND other.state = 'in_progress'"
    " AND other.lease_until >= :now)"
    " RETURNING request.lease_until, request.attempt_count, request.multipart_upload_id,"
    " request.checkpoint_record_ids, request.generated_at, request.organization_id,"
    " request.plant_id, request.zone_ids, request.period_from, request.period_to,"
    " request.package_form, request.requested_by, request.correlation_id"
)
_STATE: Final = text("SELECT state FROM accreditation.export_request WHERE export_id = :export_id")
# La valla de cada escritura del intento: ``lease_owner = yo AND lease_until >= ahora``.
_RENEW: Final = text(
    "UPDATE accreditation.export_request SET lease_until = :until"
    " WHERE export_id = :export_id AND lease_owner = :owner AND lease_until >= :now"
    " RETURNING lease_until"
)
_RELEASE: Final = text(
    "UPDATE accreditation.export_request SET lease_owner = NULL, lease_until = NULL"
    " WHERE export_id = :export_id AND lease_owner = :owner RETURNING export_id"
)
_FIX_FIRST_ATTEMPT: Final = text(
    "UPDATE accreditation.export_request SET checkpoint_record_ids ="
    " CAST(:checkpoint_record_ids AS uuid[]), generated_at = :generated_at"
    " WHERE export_id = :export_id AND lease_owner = :owner AND lease_until >= :now"
    " AND generated_at IS NULL RETURNING export_id"
)
_RECORD_UPLOAD: Final = text(
    "UPDATE accreditation.export_request SET multipart_upload_id = :upload_id"
    " WHERE export_id = :export_id AND lease_owner = :owner AND lease_until >= :now"
    " RETURNING export_id"
)
_PROGRESS: Final = text(
    "UPDATE accreditation.export_request SET progress_percent = :percent,"
    " current_artifact = :artifact, progress_updated_at = :now"
    " WHERE export_id = :export_id AND lease_owner = :owner AND lease_until >= :now"
    " RETURNING export_id"
)
_INSERT_PACKAGE: Final = text(
    "INSERT INTO accreditation.export_package (export_id, organization_id, plant_id,"
    " document_sha256, archive_sha256, manifest, size_bytes, artifact_count,"
    " checkpoint_record_ids, verifier_version)"
    " VALUES (:export_id, :organization_id, :plant_id, :document_sha256, :archive_sha256,"
    " CAST(:manifest AS jsonb), :size_bytes, :artifact_count,"
    " CAST(:checkpoint_record_ids AS uuid[]), :verifier_version)"
)
_READY: Final = text(
    "UPDATE accreditation.export_request SET state = 'ready', progress_percent = 100,"
    " progress_updated_at = :now, current_artifact = NULL, completed_at = :now,"
    " expires_at = :expires_at, multipart_upload_id = NULL, lease_owner = NULL,"
    " lease_until = NULL"
    " WHERE export_id = :export_id AND lease_owner = :owner AND lease_until >= :now"
    " RETURNING export_id"
)
_FAILED: Final = text(
    "UPDATE accreditation.export_request SET state = 'failed', failure_code = :failure_code,"
    " current_artifact = NULL, completed_at = :now, lease_owner = NULL, lease_until = NULL"
    " WHERE export_id = :export_id AND lease_owner = :owner AND lease_until >= :now"
    " RETURNING export_id"
)


class TransactionSource(Protocol):
    """``shared.db.Database``: el único camino a la base."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@repository
class SqlExportLeaseStore:
    """Arrendamientos y escrituras con valla sobre ``accreditation.export_request``.

    Cada operación va en su transacción corta con el contexto de la organización del evento,
    independiente de la composición (PAT-LAZ-RES-01).
    """

    def __init__(
        self, *, database: TransactionSource, settings: ExportLeaseSettings | None = None
    ) -> None:
        self._database = database
        self.settings = settings if settings is not None else ExportLeaseSettings()

    async def acquire(
        self, context: ScopeContext, export_id: uuid.UUID, owner: str, now: datetime
    ) -> Acquisition:
        _check_owner(owner)
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                _RUN_LOCK, {"key": f"{RUN_LOCK_PREFIX}{context.organization_id}"}
            )
            row = (
                (
                    await transaction.execute(
                        _ACQUIRE,
                        {
                            "export_id": export_id,
                            "owner": owner,
                            "now": now,
                            "until": now + self.settings.duration,
                        },
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                state = (await transaction.execute(_STATE, {"export_id": export_id})).first()
                return Acquisition(None, settled=state is None or state[0] not in _OPEN_STATES)
        return Acquisition(_lease(export_id, owner, dict(row)))

    async def renew(
        self, context: ScopeContext, lease: ExportLease, now: datetime
    ) -> ExportLease | None:
        async with self._database.transaction(context) as transaction:
            row = (
                await transaction.execute(
                    _RENEW, {**_fence(lease, now), "until": now + self.settings.duration}
                )
            ).first()
        return None if row is None else replace(lease, until=row[0])

    async def release(self, context: ScopeContext, lease: ExportLease) -> bool:
        async with self._database.transaction(context) as transaction:
            row = (
                await transaction.execute(
                    _RELEASE, {"export_id": lease.export_id, "owner": lease.owner}
                )
            ).first()
        return row is not None

    async def fix_first_attempt(
        self,
        context: ScopeContext,
        lease: ExportLease,
        checkpoint_record_ids: Sequence[uuid.UUID],
        generated_at: datetime,
        now: datetime,
    ) -> ExportLease:
        """Fija los puntos de control y ``generated_at`` del primer intento (U04-H-08)."""
        ids = tuple(checkpoint_record_ids)
        await self._fenced(
            context,
            _FIX_FIRST_ATTEMPT,
            {
                **_fence(lease, now),
                "checkpoint_record_ids": [str(value) for value in ids],
                "generated_at": generated_at,
            },
        )
        return replace(lease, checkpoint_record_ids=ids, generated_at=generated_at)

    async def record_upload(
        self, context: ScopeContext, lease: ExportLease, upload_id: str | None, now: datetime
    ) -> None:
        """Guarda (o limpia, con ``None``) la subida por partes en curso del intento."""
        if upload_id is not None and not 1 <= len(upload_id) <= _UPLOAD_ID_MAX:
            raise ValueError(f"multipart_upload_id debe tener de 1 a {_UPLOAD_ID_MAX} caracteres")
        await self._fenced(context, _RECORD_UPLOAD, {**_fence(lease, now), "upload_id": upload_id})

    async def publish_progress(
        self,
        context: ScopeContext,
        lease: ExportLease,
        percent: int,
        artifact: str | None,
        now: datetime,
    ) -> None:
        """Progreso en una transacción corta, independiente de la composición (NFR-LAZ-12)."""
        if not 0 <= percent <= 100:
            raise ValueError("percent debe ir de 0 a 100")
        if artifact is not None and not 1 <= len(artifact) <= _CURRENT_ARTIFACT_MAX:
            raise ValueError("current_artifact debe tener de 1 a 128 caracteres")
        await self._fenced(
            context, _PROGRESS, {**_fence(lease, now), "percent": percent, "artifact": artifact}
        )

    async def finish_ready(
        self,
        context: ScopeContext,
        lease: ExportLease,
        package: ExportPackage,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        """El paquete y ``ready`` en **una** transacción: nunca uno sin el otro."""
        checkpoint_ids = lease.checkpoint_record_ids or ()
        async with self._database.transaction(context) as transaction:
            await transaction.execute(
                _INSERT_PACKAGE,
                {
                    "export_id": lease.export_id,
                    "organization_id": lease.organization_id,
                    "plant_id": lease.plant_id,
                    "document_sha256": package.document_sha256,
                    "archive_sha256": package.archive_sha256,
                    "manifest": json.dumps(dict(package.manifest), ensure_ascii=False),
                    "size_bytes": package.size_bytes,
                    "artifact_count": package.artifact_count,
                    "checkpoint_record_ids": [str(value) for value in checkpoint_ids],
                    "verifier_version": package.verifier_version,
                },
            )
            row = (
                await transaction.execute(_READY, {**_fence(lease, now), "expires_at": expires_at})
            ).first()
            if row is None:
                raise LeaseLost()

    async def finish_failed(
        self, context: ScopeContext, lease: ExportLease, failure_code: str, now: datetime
    ) -> None:
        """Fallo declarado (BR-LAZ-101): ``failed`` con su código y sin paquete."""
        await self._fenced(context, _FAILED, {**_fence(lease, now), "failure_code": failure_code})

    async def _fenced(
        self, context: ScopeContext, statement: Any, parameters: Mapping[str, object]
    ) -> None:
        async with self._database.transaction(context) as transaction:
            row = (await transaction.execute(statement, dict(parameters))).first()
            if row is None:
                raise LeaseLost()


def _fence(lease: ExportLease, now: datetime) -> dict[str, object]:
    return {"export_id": lease.export_id, "owner": lease.owner, "now": now}


def _lease(export_id: uuid.UUID, owner: str, row: Mapping[str, Any]) -> ExportLease:
    checkpoints = row["checkpoint_record_ids"]
    requested_by = row["requested_by"]
    if isinstance(requested_by, str):
        requested_by = json.loads(requested_by)
    return ExportLease(
        export_id=export_id,
        owner=owner,
        until=row["lease_until"],
        attempt_count=int(row["attempt_count"]),
        previous_upload_id=row["multipart_upload_id"],
        checkpoint_record_ids=(
            None if checkpoints is None else tuple(_uuid(value) for value in checkpoints)
        ),
        generated_at=row["generated_at"],
        organization_id=_uuid(row["organization_id"]),
        plant_id=_uuid(row["plant_id"]),
        zone_ids=tuple(_uuid(zone) for zone in row["zone_ids"]),
        period_from=row["period_from"],
        period_to=row["period_to"],
        package_form=PackageForm(row["package_form"]),
        requested_by=dict(requested_by),
        correlation_id=_uuid(row["correlation_id"]),
    )


# --- En memoria ------------------------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    organization_id: uuid.UUID
    state: ExportState = ExportState.REQUESTED
    lease_owner: str | None = None
    lease_until: datetime | None = None
    attempt_count: int = 0


@repository
@dataclass
class InMemoryExportLeaseStore:
    """Las mismas condiciones que ``SqlExportLeaseStore`` (adquisición, renovación y
    liberación), sobre filas en memoria."""

    settings: ExportLeaseSettings = field(default_factory=ExportLeaseSettings)
    rows: dict[uuid.UUID, _Row] = field(default_factory=dict)

    def add(self, export_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self.rows[export_id] = _Row(organization_id)

    async def acquire(
        self, context: ScopeContext, export_id: uuid.UUID, owner: str, now: datetime
    ) -> Acquisition:
        _check_owner(owner)
        row = self.rows.get(export_id)
        if row is None or row.state.value not in _OPEN_STATES:
            return Acquisition(None, settled=True)
        if row.lease_until is not None and not row.lease_until < now:
            return Acquisition(None)
        if any(
            other is not row
            and other.organization_id == row.organization_id
            and other.state is ExportState.IN_PROGRESS
            and other.lease_until is not None
            and other.lease_until >= now
            for other in self.rows.values()
        ):
            return Acquisition(None)
        row.lease_owner = owner
        row.lease_until = now + self.settings.duration
        row.attempt_count += 1
        row.state = ExportState.IN_PROGRESS
        return Acquisition(
            ExportLease(
                export_id=export_id,
                owner=owner,
                until=row.lease_until,
                attempt_count=row.attempt_count,
                previous_upload_id=None,
                checkpoint_record_ids=None,
                generated_at=None,
                organization_id=row.organization_id,
                plant_id=row.organization_id,
                zone_ids=(),
                period_from=now,
                period_to=now,
                package_form=PackageForm.WITHOUT_CLIPS,
                correlation_id=export_id,
            )
        )

    def _held(self, lease: ExportLease, now: datetime) -> _Row | None:
        row = self.rows.get(lease.export_id)
        if (
            row is None
            or row.lease_owner != lease.owner
            or row.lease_until is None
            or row.lease_until < now
        ):
            return None
        return row

    async def renew(
        self, context: ScopeContext, lease: ExportLease, now: datetime
    ) -> ExportLease | None:
        row = self._held(lease, now)
        if row is None:
            return None
        row.lease_until = now + self.settings.duration
        return replace(lease, until=row.lease_until)

    async def release(self, context: ScopeContext, lease: ExportLease) -> bool:
        row = self.rows.get(lease.export_id)
        if row is None or row.lease_owner != lease.owner:
            return False
        row.lease_owner = None
        row.lease_until = None
        return True

    async def finish(self, lease: ExportLease, state: ExportState, now: datetime) -> bool:
        """``ready`` o ``failed`` con la valla (la propiedad con estado lo ejercita)."""
        row = self._held(lease, now)
        if row is None:
            return False
        row.state = state
        row.lease_owner = None
        row.lease_until = None
        return True
