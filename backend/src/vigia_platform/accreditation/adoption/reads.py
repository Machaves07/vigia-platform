"""La consulta de adopción por zona (SUP-U05-32 ``consultar_adopcion_por_zona``; LC-LAZ-29;
BR-LAZ-134 a 136; H-33).

``AdoptionReads.report(context, plant_id=… | zone_id=…, period_from=, period_to=)``, con
``metrics.read`` sobre la **planta** (todas sus zonas) o sobre una **zona** (solo esa):

- por zona y por **día local** del periodo ``[period_from, period_to]`` (fechas, ambos incluidos,
  a lo sumo ``MAX_ADOPTION_DAYS``), la actividad del lazo por clase (``loop_activity_kind``, las
  seis, con cero las que no hubo) y su total. Solo salen los días con alguna actividad: un día que
  no aparece no tuvo ninguna;
- por zona, si está **activa** (``productive``) y, si lo está, sus **días de silencio** de hoy
  (la misma racha que evalúa ``adoption_check``, ``streak.silence_streak``), el inicio de la racha
  y la última actividad. Una zona no activa no tiene días de silencio (``None``): nunca alerta;
- el **umbral vigente** ``adoption_silence_days`` de la planta y el día local evaluado.

**Nunca por persona** (BR-LAZ-135, P3, P7): el contador no tiene ninguna columna de usuario y la
consulta no admite ningún parámetro de persona, de equipo ni de turno; la respuesta lleva el texto
fijo ``USAGE_NOTICE_ES``. Una planta o una zona inexistente, de otra organización o fuera de
alcance responde ``not_found`` (denegación auditada, nunca ``forbidden``). Sin auditoría de
consulta (es una agregación, como las métricas). La hora sale del ``Clock`` inyectado.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.accreditation.adoption.streak import silence_streak
from vigia_platform.accreditation.adoption.task import ZoneActivations
from vigia_platform.identity.authz.authorize import Authorizer, Resource
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.loop.calendar import local_date, today
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import LoopActivityKind
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction

__all__ = [
    "MAX_ADOPTION_DAYS",
    "USAGE_NOTICE_ES",
    "AdoptionDay",
    "AdoptionReads",
    "AdoptionReport",
    "AdoptionRequestInvalid",
    "AdoptionZone",
]

MAX_ADOPTION_DAYS: Final = 366
"""Días como mucho del periodo consultado (``[estimación propia]``, el tope de la exportación)."""

USAGE_NOTICE_ES: Final = (
    "La adopción se mide por zona: describe el uso del lazo en cada zona y nunca evalúa el"
    " desempeño de una persona."
)
"""Texto fijo de BR-LAZ-135."""

_READ: Final = PermissionKey.METRICS_READ

_PLANT: Final = text(
    "SELECT plant_id, timezone FROM identity.plant"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
)
_ZONE: Final = text(
    "SELECT plant_id FROM identity.zone"
    " WHERE organization_id = :organization_id AND zone_id = :zone_id"
)
_ZONES_OF_PLANT: Final = text(
    "SELECT zone_id FROM identity.zone WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id ORDER BY code, zone_id"
)
_DAYS: Final = text(
    "SELECT zone_id, day, activity_counts, session_count FROM accreditation.adoption_counter"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = ANY(CAST(:zone_ids AS uuid[])) AND day >= :period_from AND day <= :period_to"
    " ORDER BY zone_id, day"
)
_LAST: Final = text(
    "SELECT zone_id, max(day) AS last_day, max(last_activity_at) AS last_at"
    " FROM accreditation.adoption_counter WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
    " GROUP BY zone_id"
)


class AdoptionRequestInvalid(Exception):
    """Los filtros no forman una consulta válida: ``invalid_request``."""


class _Database(Protocol):
    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True)
class AdoptionDay:
    """La actividad del lazo de una zona en un día local, por clase y en total."""

    day: date
    activity_counts: Mapping[LoopActivityKind, int]
    activity_count: int


@dataclass(frozen=True, slots=True)
class AdoptionZone:
    zone_id: uuid.UUID
    active: bool
    """``True`` si la zona está en ``productive`` (montaje y uso aprobados)."""
    silent_days: int | None
    """Días completos de la racha de silencio de hoy; ``None`` si la zona no está activa."""
    streak_start: date | None
    last_activity_at: datetime | None
    days: tuple[AdoptionDay, ...]


@dataclass(frozen=True, slots=True)
class AdoptionReport:
    plant_id: uuid.UUID
    period_from: date
    period_to: date
    evaluated_day: date
    """El día local de la planta según el reloj (hoy): el de los días de silencio."""
    threshold_days: int
    """``adoption_silence_days`` vigente de la planta."""
    zones: tuple[AdoptionZone, ...]


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


def _validated(
    plant_id: uuid.UUID | None, zone_id: uuid.UUID | None, period_from: date, period_to: date
) -> None:
    if (plant_id is None) == (zone_id is None):
        raise AdoptionRequestInvalid("plant_id o zone_id, uno solo")
    for value in (plant_id, zone_id):
        if value is not None and type(value) is not uuid.UUID:
            raise AdoptionRequestInvalid("identificador")
    if type(period_from) is not date or type(period_to) is not date:
        raise AdoptionRequestInvalid("periodo")
    if period_from > period_to or (period_to - period_from).days + 1 > MAX_ADOPTION_DAYS:
        raise AdoptionRequestInvalid("periodo")


def _day(row: Any) -> AdoptionDay:
    raw: Mapping[str, Any] = row.activity_counts
    counts = {kind: int(raw.get(kind.value, 0)) for kind in LoopActivityKind}
    return AdoptionDay(day=row.day, activity_counts=counts, activity_count=int(row.session_count))


@repository
class AdoptionReads:
    """``consultar_adopcion_por_zona`` (SUP-U05-32)."""

    def __init__(
        self,
        *,
        database: _Database,
        authorizer: Authorizer,
        activations: ZoneActivations,
        configuration: PostgresLoopConfigurationRepository,
        clock: Clock,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._activations = activations
        self._configuration = configuration
        self._clock = clock

    def __repr__(self) -> str:
        return "AdoptionReads()"

    async def report(
        self,
        context: ScopeContext,
        *,
        plant_id: uuid.UUID | None = None,
        zone_id: uuid.UUID | None = None,
        period_from: date,
        period_to: date,
    ) -> AdoptionReport:
        """La adopción de la planta (todas sus zonas) o de una zona en el periodo."""
        _validated(plant_id, zone_id, period_from, period_to)
        organization_id = context.organization_id
        if zone_id is not None:
            async with self._database.transaction(context) as transaction:
                row = (
                    await transaction.execute(
                        _ZONE, {"organization_id": organization_id, "zone_id": zone_id}
                    )
                ).first()
            if row is None:
                # Inexistente o de otra organización: la misma denegación auditada que fuera de
                # alcance (nunca ``forbidden``).
                await self._authorizer.deny(
                    context, _READ, Resource(organization_id, "zone", zone_id)
                )
            plant_id = _uuid(row.plant_id)
        if plant_id is None:  # pragma: no cover - ``_validated`` exige uno de los dos
            raise AdoptionRequestInvalid("plant_id o zone_id, uno solo")
        resource = (
            Resource.plant(organization_id, plant_id)
            if zone_id is None
            else Resource.zone(organization_id, plant_id, zone_id)
        )
        async with self._database.transaction(context) as transaction:
            plant = (
                await transaction.execute(
                    _PLANT, {"organization_id": organization_id, "plant_id": plant_id}
                )
            ).first()
        if plant is None:
            await self._authorizer.deny(context, _READ, resource)
        authorized = await self._authorizer.authorize(context, _READ, resource)
        timezone = str(plant.timezone)
        async with self._database.transaction(authorized) as transaction:
            zones = (
                [zone_id]
                if zone_id is not None
                else [
                    _uuid(row.zone_id)
                    for row in (
                        await transaction.execute(
                            _ZONES_OF_PLANT,
                            {"organization_id": organization_id, "plant_id": plant_id},
                        )
                    ).all()
                ]
            )
            settings, _version = await self._configuration.settings_in(transaction, plant_id)
            parameters = {
                "organization_id": organization_id,
                "plant_id": plant_id,
                "zone_ids": [str(zone) for zone in zones],
            }
            days = (
                await transaction.execute(
                    _DAYS, {**parameters, "period_from": period_from, "period_to": period_to}
                )
            ).all()
            last = (await transaction.execute(_LAST, parameters)).all()
            active = await self._activations.active_zones(transaction, plant_id)
        evaluated = today(self._clock, timezone)
        return AdoptionReport(
            plant_id=plant_id,
            period_from=period_from,
            period_to=period_to,
            evaluated_day=evaluated,
            threshold_days=settings["adoption_silence_days"],
            zones=_zones(zones, days, last, active, evaluated, timezone),
        )


def _zones(
    zones: Sequence[uuid.UUID],
    days: Sequence[Any],
    last: Sequence[Any],
    active: Mapping[uuid.UUID, datetime],
    evaluated: date,
    timezone: str,
) -> tuple[AdoptionZone, ...]:
    by_zone: dict[uuid.UUID, list[AdoptionDay]] = {zone: [] for zone in zones}
    for row in days:
        by_zone.setdefault(_uuid(row.zone_id), []).append(_day(row))
    latest = {_uuid(row.zone_id): (row.last_day, row.last_at) for row in last}
    result: list[AdoptionZone] = []
    for zone in zones:
        last_day, last_at = latest.get(zone, (None, None))
        since = active.get(zone)
        streak = (
            None
            if since is None
            else silence_streak(
                today=evaluated,
                active_since=local_date(since, timezone),
                last_activity_day=last_day,
            )
        )
        result.append(
            AdoptionZone(
                zone_id=zone,
                active=since is not None,
                silent_days=None if streak is None else streak.silent_days,
                streak_start=None if streak is None else streak.start,
                last_activity_at=last_at,
                days=tuple(by_zone.get(zone, ())),
            )
        )
    return tuple(result)
