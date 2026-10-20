"""Lecturas acotadas y guardado atómico de ProgramMetricSnapshot (RLS + organización explícita).

La restricción única existente y el upsert condicional dejan una sola escritura por huella
y ventana, incluso si dos procesos calcularon simultáneamente. No se toman candados de cadena
ni se escribe el expediente: es una proyección reconstruible.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import text

from vigia_platform.accreditation.metrics.model import MetricValue, ProgramMetricSnapshot
from vigia_platform.loop.calendar import month_bounds
from vigia_platform.loop.projection.fold import ProjectionInput
from vigia_platform.shared.context import repository
from vigia_platform.shared.db import Transaction

_VALUES = TypeAdapter(tuple[MetricValue, ...])
_PLANT = text(
    "SELECT timezone FROM identity.plant WHERE organization_id=:organization_id"
    " AND plant_id=:plant_id"
)
_ZONES = text(
    "SELECT zone_id, created_at FROM identity.zone WHERE organization_id=:organization_id"
    " AND plant_id=:plant_id ORDER BY zone_id"
)
_HEAD = text(
    "SELECT coalesce(max(chain_sequence),0) FROM ledger.ledger_record"
    " WHERE organization_id=:organization_id AND plant_id=:plant_id"
)
_RECORDS = text(
    "SELECT record_id, chain_sequence, record_type, received_at, content FROM ledger.ledger_record"
    " WHERE organization_id=:organization_id AND plant_id=:plant_id"
    " AND (scope_zone_id=:zone_id OR record_type='loop_configuration_changed')"
    " AND chain_sequence<=:head AND received_at<:period_end ORDER BY chain_sequence"
)
_GATE = text(
    "SELECT status FROM catalog.gate_state_history WHERE organization_id=:organization_id"
    " AND plant_id=:plant_id AND zone_id=:zone_id AND gate='usage'"
    " AND effective @> CAST(:at AS timestamptz)"
)
_ADOPTION = text(
    "SELECT coalesce(sum(session_count),0) FROM accreditation.adoption_counter"
    " WHERE organization_id=:organization_id AND plant_id=:plant_id AND zone_id=:zone_id"
    " AND day>=:start_day AND day<=:last_day"
)
_STORED = text(
    "SELECT * FROM accreditation.program_metric_snapshot WHERE organization_id=:organization_id"
    " AND zone_id=:zone_id AND period=:period"
)
_SAVE = text(
    "INSERT INTO accreditation.program_metric_snapshot AS kept"
    ' (snapshot_id, organization_id, plant_id, zone_id, period, granularity, "values",'
    " materialized_at, recomputed_at, inputs_digest)"
    " VALUES (:snapshot_id,:organization_id,:plant_id,:zone_id,:period,'month',"
    " CAST(:values AS jsonb),"
    " :materialized_at,:recomputed_at,:inputs_digest)"
    " ON CONFLICT (organization_id, zone_id, period) DO UPDATE SET"
    ' "values"=EXCLUDED."values", materialized_at=EXCLUDED.materialized_at,'
    " recomputed_at=EXCLUDED.recomputed_at, inputs_digest=EXCLUDED.inputs_digest"
    " WHERE (EXCLUDED.recomputed_at IS NULL OR kept.recomputed_at IS NOT NULL) AND ("
    " kept.inputs_digest<>EXCLUDED.inputs_digest OR"
    " (kept.recomputed_at IS NOT NULL AND (EXCLUDED.recomputed_at IS NULL"
    " OR kept.recomputed_at<=:expired_before))) RETURNING *"
)


def _snapshot(row: Any, timezone: str) -> ProgramMetricSnapshot:
    period = row.period.strftime("%Y-%m")
    start, end = month_bounds(timezone, period)
    return ProgramMetricSnapshot(
        snapshot_id=uuid.UUID(str(row.snapshot_id)),
        organization_id=uuid.UUID(str(row.organization_id)),
        plant_id=uuid.UUID(str(row.plant_id)),
        zone_id=uuid.UUID(str(row.zone_id)),
        period=period,
        period_from=start,
        period_to=end,
        values=_VALUES.validate_json(
            json.dumps(row.values) if not isinstance(row.values, str) else row.values
        ),
        materialized_at=row.materialized_at,
        recomputed_at=row.recomputed_at,
        inputs_digest=row.inputs_digest,
    )


@repository
class MetricsRepository:
    async def plant(self, transaction: Transaction, plant_id: uuid.UUID) -> str | None:
        value = (
            await transaction.execute(
                _PLANT,
                {"organization_id": transaction.context.organization_id, "plant_id": plant_id},
            )
        ).scalar()
        return None if value is None else str(value)

    async def zones(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> tuple[tuple[uuid.UUID, datetime], ...]:
        rows = (
            await transaction.execute(
                _ZONES,
                {"organization_id": transaction.context.organization_id, "plant_id": plant_id},
            )
        ).all()
        return tuple((uuid.UUID(str(r.zone_id)), r.created_at) for r in rows)

    async def head(self, transaction: Transaction, plant_id: uuid.UUID) -> int:
        return int(
            (
                await transaction.execute(
                    _HEAD,
                    {"organization_id": transaction.context.organization_id, "plant_id": plant_id},
                )
            ).scalar_one()
        )

    async def records(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        head: int,
        period_end: datetime,
    ) -> tuple[ProjectionInput, ...]:
        rows = (
            await transaction.execute(
                _RECORDS,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                    "head": head,
                    "period_end": period_end,
                },
            )
        ).all()
        return tuple(
            ProjectionInput(
                record_id=uuid.UUID(str(r.record_id)),
                organization_id=transaction.context.organization_id,
                plant_id=plant_id,
                chain_sequence=int(r.chain_sequence),
                record_type=str(r.record_type),
                received_at=r.received_at,
                document=json.loads(bytes(r.content)),
            )
            for r in rows
        )

    async def approved(
        self, transaction: Transaction, *, plant_id: uuid.UUID, zone_id: uuid.UUID, at: datetime
    ) -> bool:
        value = (
            await transaction.execute(
                _GATE,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                    "at": at,
                },
            )
        ).scalar()
        # A-60: una zona sin transición nace pendiente, nunca aprobada.
        return value == "approved"

    async def adoption(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        start_day: date,
        last_day: date,
    ) -> int:
        return int(
            (
                await transaction.execute(
                    _ADOPTION,
                    {
                        "organization_id": transaction.context.organization_id,
                        "plant_id": plant_id,
                        "zone_id": zone_id,
                        "start_day": start_day,
                        "last_day": last_day,
                    },
                )
            ).scalar_one()
        )

    async def stored(
        self, transaction: Transaction, *, zone_id: uuid.UUID, period: str, timezone: str
    ) -> ProgramMetricSnapshot | None:
        row = (
            await transaction.execute(
                _STORED,
                {
                    "organization_id": transaction.context.organization_id,
                    "zone_id": zone_id,
                    "period": date.fromisoformat(period + "-01"),
                },
            )
        ).first()
        return None if row is None else _snapshot(row, timezone)

    async def save(
        self,
        transaction: Transaction,
        snapshot: ProgramMetricSnapshot,
        *,
        timezone: str,
        expired_before: datetime,
    ) -> ProgramMetricSnapshot | None:
        if snapshot.organization_id != transaction.context.organization_id:
            raise ValueError("la instantánea no es de la organización del contexto")
        parameters = {
            "snapshot_id": snapshot.snapshot_id,
            "organization_id": snapshot.organization_id,
            "plant_id": snapshot.plant_id,
            "zone_id": snapshot.zone_id,
            "materialized_at": snapshot.materialized_at,
            "recomputed_at": snapshot.recomputed_at,
            "inputs_digest": snapshot.inputs_digest,
        }
        parameters.update(
            period=date.fromisoformat(snapshot.period + "-01"),
            values=_VALUES.dump_json(snapshot.values).decode(),
            expired_before=expired_before,
        )
        row = (await transaction.execute(_SAVE, parameters)).first()
        return None if row is None else _snapshot(row, timezone)
