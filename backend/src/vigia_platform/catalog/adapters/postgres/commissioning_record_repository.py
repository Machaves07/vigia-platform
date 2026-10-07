"""Acta de comisionamiento y muestras de exposición sobre PostgreSQL (LC-GOB-08; gob_0017, 0027).

- ``commissioning_record`` ⛓: solo ``INSERT``, una por sesión (``commissioning_record_one_per_
  session``), dentro de la transacción del cierre y bajo el candado de la sesión. La restricción
  es el respaldo de la base; la exclusión entre cierres la da el candado.
- ``exposure_sample`` ⛓: solo ``INSERT … ON CONFLICT (pass_id) DO NOTHING``: dos muestras
  simultáneas del mismo pase dejan una sola fila (``exposure_sample_one_per_pass``) y la segunda
  devuelve la primera sin efecto. La clave foránea compuesta exige que el pase sea de la sesión.

Toda sentencia recibe una ``Transaction`` abierta con el ``ScopeContext`` (RLS por organización y
concesión) y además nombra la organización del contexto (defensa en profundidad).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.catalog.domain.commissioning_record import CommissioningRecord
from vigia_platform.catalog.domain.enums import WalkTestKind
from vigia_platform.catalog.domain.latency import ExposureSample
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["PostgresCommissioningRecordRepository", "StoredRecord"]

_INSERT_RECORD: Final = text(
    "INSERT INTO catalog.commissioning_record (commissioning_record_id, organization_id,"
    " plant_id, zone_id, session_id, catalog_version, matrix_results, false_negatives_total,"
    " false_alarm_rate_observed, false_alarm_threshold, false_alarm_acceptance, latency,"
    " installer_measurements, cameras_measured, occlusion_summary, total_hours, steps_summary,"
    " signatures, closed_at, ledger_record_id)"
    " VALUES (:commissioning_record_id, :organization_id, :plant_id, :zone_id, :session_id,"
    " :catalog_version, CAST(:matrix_results AS jsonb), :false_negatives_total,"
    " :false_alarm_rate_observed, :false_alarm_threshold,"
    " CAST(:false_alarm_acceptance AS jsonb), CAST(:latency AS jsonb),"
    " CAST(:installer_measurements AS jsonb), CAST(:cameras_measured AS jsonb),"
    " CAST(:occlusion_summary AS jsonb), :total_hours, CAST(:steps_summary AS jsonb),"
    " CAST(:signatures AS jsonb), :closed_at, :ledger_record_id)"
)
_RECORD: Final = text(
    "SELECT r.commissioning_record_id, r.organization_id, r.plant_id, r.zone_id, r.session_id,"
    " r.catalog_version, r.matrix_results, r.false_negatives_total, r.false_alarm_rate_observed,"
    " r.false_alarm_threshold, r.false_alarm_acceptance, r.latency, r.installer_measurements,"
    " r.cameras_measured, r.occlusion_summary, r.total_hours, r.steps_summary, r.signatures,"
    " r.closed_at, r.ledger_record_id, s.kind, s.passes_per_cell"
    " FROM catalog.commissioning_record AS r JOIN catalog.walk_test_session AS s"
    " ON s.organization_id = r.organization_id AND s.session_id = r.session_id"
    " WHERE r.organization_id = :organization_id"
    " AND r.commissioning_record_id = :commissioning_record_id"
)
_PASS_IN_SESSION: Final = text(
    "SELECT p.pass_id FROM catalog.walk_test_pass AS p"
    " WHERE p.organization_id = :organization_id AND p.session_id = :session_id"
    " AND p.pass_id = :pass_id"
)
_SAMPLE: Final = text(
    "SELECT sample_id, organization_id, plant_id, session_id, pass_id, fetched_at, displayed_at,"
    " recorded_by, recorded_at FROM catalog.exposure_sample"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " AND pass_id = :pass_id"
)
_SAMPLES: Final = text(
    "SELECT sample_id, organization_id, plant_id, session_id, pass_id, fetched_at, displayed_at,"
    " recorded_by, recorded_at FROM catalog.exposure_sample"
    " WHERE organization_id = :organization_id AND session_id = :session_id"
    " ORDER BY recorded_at, sample_id"
)
_INSERT_SAMPLE: Final = text(
    "INSERT INTO catalog.exposure_sample (sample_id, organization_id, plant_id, session_id,"
    " pass_id, fetched_at, displayed_at, recorded_by, recorded_at)"
    " VALUES (:sample_id, :organization_id, :plant_id, :session_id, :pass_id, :fetched_at,"
    " :displayed_at, :recorded_by, :recorded_at)"
    " ON CONFLICT (pass_id) DO NOTHING RETURNING sample_id"
)


@dataclass(frozen=True, slots=True)
class StoredRecord:
    """La fila del acta tal como está guardada, con la clase y los pases por celda de su sesión."""

    commissioning_record_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    session_id: uuid.UUID
    kind: WalkTestKind
    passes_per_cell: int
    catalog_version: int
    matrix_results: list[Mapping[str, Any]]
    false_negatives_total: int
    false_alarm_rate_observed: float
    false_alarm_threshold: float
    false_alarm_acceptance: Mapping[str, Any] | None
    latency: Mapping[str, Any]
    installer_measurements: Mapping[str, Any]
    cameras_measured: list[Mapping[str, Any]]
    occlusion_summary: list[Mapping[str, Any]]
    steps_summary: list[Mapping[str, Any]]
    signatures: list[Mapping[str, Any]]
    closed_at: datetime


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _json(value: object) -> Any:
    """``jsonb`` llega ya decodificado con asyncpg; como texto, se decodifica aquí."""
    return json.loads(value) if isinstance(value, str | bytes) else value


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _sample(row: Row[Any]) -> ExposureSample:
    return ExposureSample(
        sample_id=_uuid(row.sample_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        session_id=_uuid(row.session_id),
        pass_id=_uuid(row.pass_id),
        fetched_at=row.fetched_at,
        displayed_at=row.displayed_at,
        recorded_by=_uuid(row.recorded_by),
        recorded_at=row.recorded_at,
    )


def _stored(row: Row[Any]) -> StoredRecord:
    acceptance = _json(row.false_alarm_acceptance)
    return StoredRecord(
        commissioning_record_id=_uuid(row.commissioning_record_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        session_id=_uuid(row.session_id),
        kind=WalkTestKind(row.kind),
        passes_per_cell=int(row.passes_per_cell),
        catalog_version=int(row.catalog_version),
        matrix_results=list(_json(row.matrix_results)),
        false_negatives_total=int(row.false_negatives_total),
        false_alarm_rate_observed=float(row.false_alarm_rate_observed),
        false_alarm_threshold=float(row.false_alarm_threshold),
        false_alarm_acceptance=None if acceptance is None else dict(acceptance),
        latency=dict(_json(row.latency)),
        installer_measurements=dict(_json(row.installer_measurements)),
        cameras_measured=list(_json(row.cameras_measured)),
        occlusion_summary=list(_json(row.occlusion_summary)),
        steps_summary=list(_json(row.steps_summary)),
        signatures=list(_json(row.signatures)),
        closed_at=row.closed_at,
    )


@repository
class PostgresCommissioningRecordRepository:
    """Actas cerradas y muestras de exposición de las sesiones de walk-test."""

    # --- Acta ------------------------------------------------------------------------------------

    async def insert(self, transaction: Transaction, record: CommissioningRecord) -> None:
        """Anexa el acta (una por sesión; el candado de la sesión ya excluye otro cierre)."""
        if record.organization_id != transaction.context.organization_id:
            raise ValueError("el acta es de otra organización que la transacción")
        acceptance = record.false_alarm_acceptance
        await transaction.execute(
            _INSERT_RECORD,
            {
                "commissioning_record_id": record.commissioning_record_id,
                "organization_id": record.organization_id,
                "plant_id": record.plant_id,
                "zone_id": record.zone_id,
                "session_id": record.session.session_id,
                "catalog_version": record.session.catalog_version,
                "matrix_results": _dumps([dict(entry) for entry in record.matrix_results]),
                "false_negatives_total": record.false_negatives_total,
                "false_alarm_rate_observed": record.false_alarm_rate_observed,
                "false_alarm_threshold": record.false_alarm_threshold,
                "false_alarm_acceptance": None
                if acceptance is None
                else _dumps(acceptance.to_json()),
                "latency": _dumps(record.latency_json()),
                "installer_measurements": _dumps(dict(record.installer_measurements)),
                "cameras_measured": _dumps(
                    [camera.to_json() for camera in record.cameras_measured]
                ),
                "occlusion_summary": _dumps([dict(entry) for entry in record.occlusion_summary]),
                "total_hours": record.total_hours,
                "steps_summary": _dumps(record.steps_summary_json()),
                "signatures": _dumps([signature.to_json() for signature in record.signatures]),
                "closed_at": record.closed_at,
                "ledger_record_id": record.ledger_record_id,
            },
        )

    async def record(
        self, transaction: Transaction, commissioning_record_id: uuid.UUID
    ) -> StoredRecord | None:
        """El acta en la organización de la transacción (si la RLS la deja ver), o ``None``."""
        result = await transaction.execute(
            _RECORD,
            {
                "organization_id": transaction.context.organization_id,
                "commissioning_record_id": commissioning_record_id,
            },
        )
        row = result.first()
        return None if row is None else _stored(row)

    # --- Muestras de exposición ------------------------------------------------------------------

    async def pass_in_session(
        self, transaction: Transaction, session_id: uuid.UUID, pass_id: uuid.UUID
    ) -> bool:
        result = await transaction.execute(
            _PASS_IN_SESSION,
            {
                "organization_id": transaction.context.organization_id,
                "session_id": session_id,
                "pass_id": pass_id,
            },
        )
        return result.first() is not None

    async def sample(
        self, transaction: Transaction, session_id: uuid.UUID, pass_id: uuid.UUID
    ) -> ExposureSample | None:
        """La muestra del pase en la sesión, o ``None``."""
        result = await transaction.execute(
            _SAMPLE,
            {
                "organization_id": transaction.context.organization_id,
                "session_id": session_id,
                "pass_id": pass_id,
            },
        )
        row = result.first()
        return None if row is None else _sample(row)

    async def samples(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> tuple[ExposureSample, ...]:
        """Las muestras de la sesión en orden de recepción."""
        result = await transaction.execute(
            _SAMPLES,
            {"organization_id": transaction.context.organization_id, "session_id": session_id},
        )
        return tuple(_sample(row) for row in result.all())

    async def insert_sample(self, transaction: Transaction, sample: ExposureSample) -> bool:
        """Anexa la muestra; ``False`` si el pase ya tenía una (no cambia nada)."""
        if sample.organization_id != transaction.context.organization_id:
            raise ValueError("la muestra es de otra organización que la transacción")
        result = await transaction.execute(
            _INSERT_SAMPLE,
            {
                "sample_id": sample.sample_id,
                "organization_id": sample.organization_id,
                "plant_id": sample.plant_id,
                "session_id": sample.session_id,
                "pass_id": sample.pass_id,
                "fetched_at": sample.fetched_at,
                "displayed_at": sample.displayed_at,
                "recorded_by": sample.recorded_by,
                "recorded_at": sample.recorded_at,
            },
        )
        return result.first() is not None
