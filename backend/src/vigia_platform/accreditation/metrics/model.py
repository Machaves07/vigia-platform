"""Salidas cerradas del cálculo y puerto consumible por el paquete mensual."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.loop.domain.enums import MetricKey, MetricStatus, MetricUnit
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.signing.keys import format_timestamp

FRESHNESS = timedelta(minutes=15)


class MetricValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    key: MetricKey
    status: MetricStatus
    unit: MetricUnit
    value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    numerator: int | None = Field(default=None, ge=0)
    denominator: int | None = Field(default=None, ge=0)
    excluded_unclosable_count: int = Field(ge=0)
    over_30_days_count: int | None = Field(default=None, ge=0)
    not_computable_reason_es: str | None = None
    detail_code: AccreditationDetailCode | None = None
    partial_reason_es: str | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.status is MetricStatus.NOT_COMPUTABLE:
            if (
                any(
                    v is not None
                    for v in (self.value, self.numerator, self.denominator, self.over_30_days_count)
                )
                or not self.not_computable_reason_es
            ):
                raise ValueError("sin insumos completos no hay cifras ni valores parciales")
            if self.detail_code is not AccreditationDetailCode.METRIC_NOT_COMPUTABLE:
                raise ValueError("una métrica no calculable declara su código")
        elif self.value is None:
            raise ValueError("una métrica calculada necesita valor")
        return self


class ProgramMetricSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    snapshot_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    period: str
    period_from: datetime
    period_to: datetime
    granularity: Literal["month"] = "month"
    values: tuple[MetricValue, ...]
    materialized_at: datetime
    recomputed_at: datetime | None
    inputs_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_serializer("period_from", "period_to", "materialized_at", "recomputed_at")
    def timestamp(self, value: datetime | None) -> str | None:
        return None if value is None else format_timestamp(value)

    @model_validator(mode="after")
    def seven(self) -> Self:
        if len(self.values) != len(MetricKey) or {v.key for v in self.values} != set(MetricKey):
            raise ValueError("cada instantánea lleva exactamente las siete métricas")
        return self


class ProgramMetricsPort(Protocol):
    async def snapshot(
        self, context: ScopeContext, zone_id: uuid.UUID, period: str
    ) -> ProgramMetricSnapshot: ...


def reusable(
    snapshot: ProgramMetricSnapshot, digest: str, now: datetime, *, definitive: bool
) -> bool:
    """El definitivo se fija una sola vez; solo la caché depende de huella y reloj."""
    if snapshot.recomputed_at is None:
        return True
    if snapshot.inputs_digest != digest:
        return False
    return not definitive and timedelta(0) <= now - snapshot.recomputed_at < FRESHNESS
