"""Lecturas y guardado del indicador de exposición sobre PostgreSQL (TASK-322; LC-LAZ-28;
PAT-LAZ-REN-01).

- ``zone``: la planta y la zona horaria de la zona en la organización del contexto.
- ``episodes``: los episodios de ``loop.episode`` de la zona que se solapan con el periodo, de
  ``coexistence``/``dwell`` y ``tier_1``, con la última clasificación de su hallazgo
  (``loop.finding_state``); sin fila de estado todavía, cuenta como pendiente (nunca se omite).
- ``inputs``: lo que entra en ``inputs_digest``: el último ``chain_sequence`` de la cadena de la
  planta (toda escritura del mes lo mueve: clasificaciones, cobertura, compuerta, catálogo,
  configuración) y el punto de aplicación de la proyección del lazo, que avanza cuando la
  proyección incorpora lo escrito.
- ``stored`` y ``save``: la fila de ``accreditation.exposure_indicator`` por
  ``(organización, zona, periodo)``. ``save`` es un ``INSERT … ON CONFLICT DO UPDATE``: dos
  cálculos simultáneos del mismo periodo dejan una sola fila y ninguno falla. Un valor del mes en
  curso **nunca** sobrescribe la materialización definitiva (``materialized_at ≥ period_to``); la
  definitiva sí sobrescribe cualquier fila. Sin fila afectada (el definitivo ya estaba),
  ``save`` devuelve ``None`` y el servicio sirve lo guardado.

Toda sentencia nombra la organización del contexto (defensa en profundidad sobre la RLS).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Row

from vigia_platform.accreditation.exposure.compute import (
    COUNTED_FAMILIES,
    COUNTED_TIER,
    EpisodeInput,
)
from vigia_platform.loop.domain.enums import ClassificationOutcome
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

__all__ = ["DigestInputs", "ExposureRepository", "StoredExposure", "ZonePlace"]

_ZONE: Final = text(
    "SELECT z.plant_id, p.timezone FROM identity.zone AS z"
    " JOIN identity.plant AS p"
    " ON p.organization_id = z.organization_id AND p.plant_id = z.plant_id"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
)
_EPISODES: Final = text(
    "SELECT e.episode_id, e.family, e.tier, e.started_at, e.ended_at,"
    " e.total_condition_duration_ms, fs.current_classification_outcome"
    " FROM loop.episode AS e"
    " LEFT JOIN loop.finding_state AS fs"
    " ON fs.organization_id = e.organization_id AND fs.plant_id = e.plant_id"
    " AND fs.zone_id = e.zone_id AND fs.subject_kind = 'finding' AND fs.subject_id = e.episode_id"
    " WHERE e.organization_id = :organization_id AND e.plant_id = :plant_id"
    " AND e.zone_id = :zone_id AND e.family = ANY(CAST(:families AS text[])) AND e.tier = :tier"
    " AND e.started_at < :period_end AND e.ended_at > :period_start"
    " AND (fs.current_classification_outcome IS NULL"
    " OR fs.current_classification_outcome = :confirmed)"
    " ORDER BY e.started_at, e.episode_id"
)
_LEDGER_HEAD: Final = text(
    "SELECT max(chain_sequence) AS last_sequence FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_PROJECTION_HEAD: Final = text(
    "SELECT last_applied_ledger_sequence, state FROM loop.loop_projection_checkpoint"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_COLUMNS: Final = (
    "exposure_id, organization_id, plant_id, zone_id, period, status, confirmed_minutes,"
    " pending_classification_minutes, observable_coverage_percent, coverage_timeline_ref,"
    " hourly_distribution, not_computable_reason_es, partial_reason_es, usage_notice_es,"
    " materialized_at, recomputed_at, inputs_digest"
)
# Las columnas son una constante del módulo: ningún dato entra por formato (NFR-NUC-19).
_STORED_SQL: Final = (
    f"SELECT {_COLUMNS} FROM accreditation.exposure_indicator"  # noqa: S608
    " WHERE organization_id = :organization_id AND zone_id = :zone_id AND period = :period"
)
_STORED: Final = text(_STORED_SQL)
_SAVE_SQL: Final = (
    f"INSERT INTO accreditation.exposure_indicator AS kept ({_COLUMNS})"  # noqa: S608
    " VALUES (:exposure_id, :organization_id, :plant_id, :zone_id, :period, :status,"
    " :confirmed_minutes, :pending_classification_minutes, :observable_coverage_percent,"
    " CAST(:coverage_timeline_ref AS jsonb), CAST(:hourly_distribution AS jsonb), NULL,"
    " :partial_reason_es, :usage_notice_es, :computed_at, :recomputed_at, :inputs_digest)"
    " ON CONFLICT (organization_id, zone_id, period) DO UPDATE SET"
    " status = EXCLUDED.status,"
    " confirmed_minutes = EXCLUDED.confirmed_minutes,"
    " pending_classification_minutes = EXCLUDED.pending_classification_minutes,"
    " observable_coverage_percent = EXCLUDED.observable_coverage_percent,"
    " coverage_timeline_ref = EXCLUDED.coverage_timeline_ref,"
    " hourly_distribution = EXCLUDED.hourly_distribution,"
    " not_computable_reason_es = NULL,"
    " partial_reason_es = EXCLUDED.partial_reason_es,"
    " usage_notice_es = EXCLUDED.usage_notice_es,"
    " materialized_at = CASE WHEN CAST(:definitive AS boolean) THEN EXCLUDED.materialized_at"
    " ELSE kept.materialized_at END,"
    " recomputed_at = EXCLUDED.recomputed_at,"
    " inputs_digest = EXCLUDED.inputs_digest"
    " WHERE CAST(:definitive AS boolean) OR kept.materialized_at < :period_end"
    f" RETURNING {_COLUMNS}"
)
_SAVE: Final = text(_SAVE_SQL)


@dataclass(frozen=True, slots=True)
class ZonePlace:
    plant_id: uuid.UUID
    timezone: str


@dataclass(frozen=True, slots=True)
class DigestInputs:
    """Lo que hace caducar un resultado guardado antes que el reloj (PAT-LAZ-REN-01)."""

    ledger_sequence: int | None
    projection_sequence: int | None
    projection_state: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredExposure:
    """Una fila de ``accreditation.exposure_indicator``."""

    exposure_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    period: date
    status: str
    confirmed_minutes: float
    pending_classification_minutes: float
    observable_coverage_percent: float
    coverage_timeline_ref: Mapping[str, Any]
    hourly_distribution: Sequence[Mapping[str, Any]]
    partial_reason_es: str | None
    usage_notice_es: str
    materialized_at: datetime
    recomputed_at: datetime | None
    inputs_digest: str


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str | bytes) else value


def _uuid(value: Any) -> uuid.UUID:
    """Siempre ``uuid.UUID`` exacto: el controlador devuelve su propia subclase."""
    return uuid.UUID(str(value))


def _stored(row: Row[Any]) -> StoredExposure:
    return StoredExposure(
        exposure_id=_uuid(row.exposure_id),
        organization_id=_uuid(row.organization_id),
        plant_id=_uuid(row.plant_id),
        zone_id=_uuid(row.zone_id),
        period=row.period,
        status=str(row.status),
        confirmed_minutes=float(row.confirmed_minutes),
        pending_classification_minutes=float(row.pending_classification_minutes),
        observable_coverage_percent=float(row.observable_coverage_percent),
        coverage_timeline_ref=dict(_json(row.coverage_timeline_ref)),
        hourly_distribution=tuple(dict(item) for item in _json(row.hourly_distribution)),
        partial_reason_es=row.partial_reason_es,
        usage_notice_es=str(row.usage_notice_es),
        materialized_at=row.materialized_at,
        recomputed_at=row.recomputed_at,
        inputs_digest=str(row.inputs_digest),
    )


@repository
class ExposureRepository:
    """Las lecturas del cálculo y la fila guardada del indicador."""

    async def zone(self, transaction: Transaction, zone_id: uuid.UUID) -> ZonePlace | None:
        """La planta y su zona horaria; ``None`` si la zona no existe en la organización."""
        row = (
            await transaction.execute(
                _ZONE, {"organization_id": transaction.context.organization_id, "zone_id": zone_id}
            )
        ).first()
        if row is None:
            return None
        return ZonePlace(plant_id=_uuid(row.plant_id), timezone=str(row.timezone))

    async def episodes(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        period_start: datetime,
        period_end: datetime,
    ) -> tuple[EpisodeInput, ...]:
        """Los episodios que cuentan o están pendientes y se solapan con ``[start, end)``."""
        rows = (
            await transaction.execute(
                _EPISODES,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                    "families": sorted(COUNTED_FAMILIES),
                    "tier": COUNTED_TIER,
                    "confirmed": ClassificationOutcome.CONFIRMED.value,
                    "period_start": period_start,
                    "period_end": period_end,
                },
            )
        ).all()
        return tuple(
            EpisodeInput(
                episode_id=_uuid(row.episode_id),
                family=str(row.family),
                tier=str(row.tier),
                started_at=row.started_at,
                ended_at=row.ended_at,
                condition_duration_ms=int(row.total_condition_duration_ms),
                classification_outcome=row.current_classification_outcome,
            )
            for row in rows
        )

    async def inputs(self, transaction: Transaction, plant_id: uuid.UUID) -> DigestInputs:
        parameters = {
            "organization_id": transaction.context.organization_id,
            "plant_id": plant_id,
        }
        ledger = (await transaction.execute(_LEDGER_HEAD, parameters)).first()
        projection = (await transaction.execute(_PROJECTION_HEAD, parameters)).first()
        return DigestInputs(
            ledger_sequence=None
            if ledger is None or ledger.last_sequence is None
            else int(ledger.last_sequence),
            projection_sequence=None
            if projection is None
            else int(projection.last_applied_ledger_sequence),
            projection_state=None if projection is None else str(projection.state),
        )

    async def stored(
        self, transaction: Transaction, *, zone_id: uuid.UUID, period: date
    ) -> StoredExposure | None:
        row = (
            await transaction.execute(
                _STORED,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "period": period,
                },
            )
        ).first()
        return None if row is None else _stored(row)

    async def save(
        self,
        transaction: Transaction,
        *,
        values: Mapping[str, Any],
        definitive: bool,
        period_end: datetime,
    ) -> StoredExposure | None:
        """Inserta o actualiza la fila; ``None`` si ya había una definitiva que no se toca."""
        parameters = {
            **values,
            "organization_id": transaction.context.organization_id,
            "coverage_timeline_ref": json.dumps(values["coverage_timeline_ref"]),
            "hourly_distribution": json.dumps(values["hourly_distribution"]),
            "definitive": definitive,
            "period_end": period_end,
        }
        row = (await transaction.execute(_SAVE, parameters)).first()
        return None if row is None else _stored(row)
