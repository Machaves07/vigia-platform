"""SUP-U05-30 y ProgramMetricsPort sobre el mismo cálculo y la misma tabla."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from vigia_platform.accreditation.exposure.indicator import ExposureIndicator
from vigia_platform.accreditation.exposure.repository import ExposureRepository
from vigia_platform.accreditation.exposure.service import ExposureInputMissing
from vigia_platform.accreditation.metrics.compute import compute_metrics, missing
from vigia_platform.accreditation.metrics.model import (
    FRESHNESS,
    MetricValue,
    ProgramMetricSnapshot,
    reusable,
)
from vigia_platform.accreditation.metrics.repository import MetricsRepository
from vigia_platform.accreditation.monthly.package import TransactionSource
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.calendar import local_date, month_bounds
from vigia_platform.loop.domain.enums import ExposureStatus, MetricKey, MetricStatus, MetricUnit
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorKind, ScopeContext, repository
from vigia_platform.shared.ids import uuid7


class MetricsRequestInvalid(ValueError):
    """Los filtros no forman una consulta válida."""


class MetricsExposure(Protocol):
    async def indicator(
        self, context: ScopeContext, zone_id: uuid.UUID, period: str
    ) -> ExposureIndicator: ...


def inputs_digest(
    *,
    head: int,
    zones: tuple[uuid.UUID, ...],
    period: str,
    adoption: int,
    projection_sequence: int | None = None,
    projection_state: str | None = None,
) -> str:
    """La última secuencia, conjunto canónico de zonas y periodo; incluye el contador operativo."""
    raw = json.dumps(
        {
            "formula": "program-metrics-v1",
            "chain_sequence": head,
            "zones": sorted(map(str, set(zones))),
            "period": period,
            "adoption_count": adoption,
            "projection_sequence": projection_sequence,
            "projection_state": projection_state,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class MetricsReport:
    plant_id: uuid.UUID
    period: str
    zones: tuple[ProgramMetricSnapshot, ...]


@repository
class ProgramMetricsService:
    def __init__(
        self,
        *,
        database: TransactionSource,
        authorizer: Authorizer,
        exposure: MetricsExposure,
        clock: Clock,
        repository: MetricsRepository | None = None,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._exposure = exposure
        self._clock = clock
        self._repository = repository if repository is not None else MetricsRepository()

    async def read(
        self,
        context: ScopeContext,
        *,
        period: str,
        zone_id: uuid.UUID | None = None,
        plant_id: uuid.UUID | None = None,
    ) -> MetricsReport:
        if (zone_id is None) == (plant_id is None):
            raise MetricsRequestInvalid("zone_id o plant_id, uno solo")
        if zone_id is not None:
            async with self._database.transaction(context) as tx:
                place = await ExposureRepository().zone(tx, zone_id)
            if place is None:
                raise ResourceNotFound()
            plant_id = place.plant_id
        if plant_id is None:
            raise MetricsRequestInvalid()
        resource = (
            Resource.plant(context.organization_id, plant_id)
            if zone_id is None
            else Resource.zone(context.organization_id, plant_id, zone_id)
        )
        authorized = await self._authorizer.authorize(context, PermissionKey.METRICS_READ, resource)
        async with self._database.transaction(authorized) as tx:
            timezone = await self._repository.plant(tx, plant_id)
            places = await self._repository.zones(tx, plant_id)
        if timezone is None:
            raise ResourceNotFound()
        try:
            start, end = month_bounds(timezone, period)
            if start > self._clock.now():
                raise MetricsRequestInvalid()
        except (ValueError, KeyError, OverflowError):
            raise MetricsRequestInvalid() from None
        selected = tuple(
            z
            for z, created_at in places
            if (z == zone_id if zone_id is not None else created_at < end)
        )
        return MetricsReport(
            plant_id,
            period,
            tuple([await self.snapshot(authorized, zone, period) for zone in selected]),
        )

    async def snapshot(
        self, context: ScopeContext, zone_id: uuid.UUID, period: str
    ) -> ProgramMetricSnapshot:
        """Puerto interno: el llamador ya tiene su contexto de sistema o de sesión autorizado."""
        now = self._clock.now()
        now -= timedelta(microseconds=now.microsecond % 1000)
        async with self._database.transaction(context) as tx:
            place = await ExposureRepository().zone(tx, zone_id)
        if place is None:
            raise ResourceNotFound()
        if context.actor.kind is not ActorKind.SYSTEM:
            context = await self._authorizer.authorize(
                context,
                PermissionKey.METRICS_READ,
                Resource.zone(context.organization_id, place.plant_id, zone_id),
            )
        async with self._database.transaction(context) as tx:
            try:
                start, end = month_bounds(place.timezone, period)
            except (ValueError, KeyError, OverflowError):
                raise MetricsRequestInvalid() from None
            if start > now or end <= place.created_at:
                raise MetricsRequestInvalid()
            head = await self._repository.head(tx, place.plant_id)
            # La cabeza puede incorporar una recepción que llegó mientras esperábamos la
            # consulta. El corte temporal se toma después: nunca una huella nueva con una
            # recepción omitida por el instante anterior a la lectura de la cabeza.
            now = self._clock.now()
            now -= timedelta(microseconds=now.microsecond % 1000)
            cutoff = min(end, now)
            projection = await ExposureRepository().inputs(tx, place.plant_id)
            adoption = await self._repository.adoption(
                tx,
                plant_id=place.plant_id,
                zone_id=zone_id,
                start_day=local_date(start, place.timezone),
                last_day=local_date(
                    cutoff if now < end else end - timedelta(microseconds=1), place.timezone
                ),
            )
            digest = inputs_digest(
                head=head,
                zones=(zone_id,),
                period=period,
                adoption=adoption,
                projection_sequence=projection.projection_sequence,
                projection_state=projection.projection_state,
            )
            stored = await self._repository.stored(
                tx, zone_id=zone_id, period=period, timezone=place.timezone
            )
            definitive = end <= now
            if stored is not None and reusable(stored, digest, now, definitive=definitive):
                return stored
            records = await self._repository.records(
                tx, plant_id=place.plant_id, zone_id=zone_id, head=head, period_end=end
            )
            approved = await self._repository.approved(
                tx,
                plant_id=place.plant_id,
                zone_id=zone_id,
                at=cutoff if now < end else end - timedelta(microseconds=1),
            )
        exposure = await self._exposure_value(context, zone_id, period)
        previous_period = (
            (start - timedelta(days=1)).astimezone(ZoneInfo(place.timezone)).strftime("%Y-%m")
        )
        previous_start, _ = month_bounds(place.timezone, previous_period)
        values = compute_metrics(
            records,
            organization_id=context.organization_id,
            plant_id=place.plant_id,
            zone_id=zone_id,
            start=start,
            end=end,
            previous_start=previous_start,
            cutoff=cutoff,
            timezone=place.timezone,
            approved=approved,
            adoption_count=adoption,
            exposure=exposure,
        )
        snapshot = ProgramMetricSnapshot(
            snapshot_id=uuid7(self._clock),
            organization_id=context.organization_id,
            plant_id=place.plant_id,
            zone_id=zone_id,
            period=period,
            period_from=start,
            period_to=end,
            values=values,
            materialized_at=now,
            recomputed_at=None if definitive else now,
            inputs_digest=digest,
        )
        async with self._database.transaction(context) as tx:
            saved = await self._repository.save(
                tx, snapshot, timezone=place.timezone, expired_before=self._clock.now() - FRESHNESS
            )
            if saved is None:
                saved = await self._repository.stored(
                    tx, zone_id=zone_id, period=period, timezone=place.timezone
                )
        if saved is None:
            raise RuntimeError("la instantánea desapareció durante el guardado")
        return saved

    async def _exposure_value(
        self, context: ScopeContext, zone_id: uuid.UUID, period: str
    ) -> MetricValue:
        try:
            value = await self._exposure.indicator(context, zone_id, period)
        except (ExposureInputMissing, ResourceNotFound):
            return missing(
                MetricKey.EXPOSURE,
                MetricUnit.MINUTES,
                "No están disponibles los insumos de exposición.",
            )
        if value.status is ExposureStatus.NOT_COMPUTABLE or value.confirmed_minutes is None:
            return missing(
                MetricKey.EXPOSURE,
                MetricUnit.MINUTES,
                value.not_computable_reason_es or "No está disponible la exposición.",
            )
        return MetricValue(
            key=MetricKey.EXPOSURE,
            unit=MetricUnit.MINUTES,
            status=MetricStatus(value.status.value),
            value=value.confirmed_minutes,
            excluded_unclosable_count=0,
            partial_reason_es=value.partial_reason_es,
        )
