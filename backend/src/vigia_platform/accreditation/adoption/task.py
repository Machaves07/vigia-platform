"""Tarea periódica ``adoption_check`` (TASK-323; LC-LAZ-29; BR-LAZ-136 y 141; PAT-LAZ-RES-05).

**Cadencia** (el diseño no la fija): **diaria, por organización** (``U04_TASKS``), a las 05:00 UTC
``[estimación propia]``. La hora solo arranca el ciclo: el día que se evalúa es el **día local de
cada planta** según el ``Clock`` inyectado (``loop.calendar.today``), y la racha solo cuenta días
locales completos (``streak``), así que la hora UTC del arranque no cambia el resultado.

En la transacción de la organización (``ScopeContext`` de iteración periódica; el fallo de una
organización no detiene a las demás, BR-LAZ-141), recorre sus plantas en orden y, en cada una:

1. las zonas **activas** (``productive``: montaje y uso aprobados, lo que anuncia
   ``zone_activated``) con el instante de su activación, de la proyección de compuertas de U-03
   (``ProductiveZones``). Una zona que no está activa **nunca** genera alerta (BR-LAZ-136);
2. ``adoption_silence_days`` vigente de la planta (``loop.config``; 14 por defecto
   ``[estimación propia]``, configurable por planta);
3. el último día con actividad de cada zona activa (``accreditation.adoption_counter``) y su racha
   de silencio (``streak.silence_streak``);
4. si la racha llega al umbral, la **marca** de la racha en ``accreditation.adoption_alert``
   (``INSERT … ON CONFLICT DO NOTHING``, clave ``(organization_id, zone_id, streak_start)``) y,
   **solo si la marca es nueva**, el evento ``adoption_low`` con ``zone_id``, en la misma
   transacción;
5. antes de la racha vigente, las rachas **ya cerradas** por una actividad que llegaron al umbral
   sin que la tarea corriera (``_CLOSED_STREAKS``), con la misma marca y el mismo evento.

**La recuperación no se salta nada** (NFR-LAZ-13; decisión del dueño en VIG-241, 2026-10-21). Una
racha que alcanza el umbral y se cierra por actividad mientras la tarea no corre (una caída de
48 h, un hueco del planificador, o la actividad del día en que se cumple el umbral antes de la
hora de arranque) se publica a posteriori, una vez, con sus días completos de silencio. Así la
ejecución tras una caída deja el mismo conjunto de rachas alertadas que la continua (PR-LAZ-48).
La recuperación mira ``max_catchup_days`` hacia atrás (``MAX_CATCHUP_DAYS``): una racha que llegó
al umbral antes ya no se alerta. Se evalúa con el umbral vigente: bajarlo puede alertar rachas
cerradas dentro de ese margen que con el anterior no llegaban.

**Una sola vez por racha** (PAT-LAZ-RES-05: clave ``(zone_id, día)``, aquí el día de inicio de la
racha): la tarea corrida dos veces el mismo día, o dos a la vez (arrendamiento vencido), no publica
dos ``adoption_low``: la segunda inserción espera a la primera en la clave única y, confirmada
esta, no inserta nada y no publica. Al día siguiente la racha sigue con la misma clave y tampoco
vuelve a publicar; una actividad nueva o una activación nueva empiezan otra racha (otra clave).
Tras una caída, la primera ejecución publica, una vez cada una, las rachas que alcanzaron el umbral
mientras tanto: las cerradas y la vigente.
Si la transacción se revierte, no queda ni la marca ni el evento.

**Candados**: solo la fila nueva de ``adoption_alert`` (la clave única). No lee con ``FOR UPDATE``
el contador ni toma ningún candado del lazo, así que no entra en el orden de
``loop.projection.store``.

Los destinatarios (coordinador SST de la zona, gerente si no hay ninguno activo, copia al
proveedor; BR-LAZ-137) los resuelve el catálogo de ``notifications`` a partir del evento.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, Protocol

from sqlalchemy import text

from vigia_platform.accreditation.adoption.streak import (
    MAX_REPORTED_SILENT_DAYS,
    SilenceStreak,
    adoption_due,
    silence_streak,
)
from vigia_platform.loop.calendar import local_date, today
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "ADOPTION_CHECK",
    "ADOPTION_CHECK_SCHEDULE",
    "ADOPTION_LOW",
    "MAX_CATCHUP_DAYS",
    "AdoptionAlert",
    "AdoptionCheck",
    "ProductiveZones",
    "ZoneActivations",
    "activation_instant",
    "register_adoption_check",
]

ADOPTION_CHECK: Final = "adoption_check"
ADOPTION_CHECK_SCHEDULE: Final = Schedule.daily(hour=5)
"""Diaria a las 05:00 UTC ``[estimación propia]``: el diseño no fija la frecuencia; el día que
cuenta es el local de cada planta, no esta hora."""
ADOPTION_LOW: Final = "adoption_low"
MAX_CATCHUP_DAYS: Final = 31
"""Días hacia atrás en que la recuperación busca rachas ya cerradas que llegaron al umbral
``[estimación propia]``: cubre de sobra la caída de 48 h de NFR-LAZ-13 y acota el trabajo de un
ciclo; una alerta de hace meses ya no ayuda a reactivar la zona."""
_ONE_DAY: Final = timedelta(days=1)

_PLANTS: Final = text(
    "SELECT plant_id, timezone FROM identity.plant WHERE organization_id = :organization_id"
    " ORDER BY plant_id"
)
_LAST_ACTIVITY: Final = text(
    "SELECT zone_id, max(day) AS last_day, max(last_activity_at) AS last_at"
    " FROM accreditation.adoption_counter WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
    " GROUP BY zone_id"
)
_CLOSED_STREAKS: Final = text(
    "SELECT zone_id, day, previous_day, previous_at FROM ("
    " SELECT zone_id, day, lag(day) OVER zone_days AS previous_day,"
    " lag(last_activity_at) OVER zone_days AS previous_at"
    " FROM accreditation.adoption_counter WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND zone_id = ANY(CAST(:zone_ids AS uuid[])) AND day >= :floor"
    " WINDOW zone_days AS (PARTITION BY zone_id ORDER BY day)) AS gaps"
    " WHERE day >= :since ORDER BY zone_id, day"
)
"""Cada día con actividad desde ``since`` y el día con actividad anterior de su zona: entre los
dos hay una racha cerrada. ``floor`` (``since`` menos el umbral y un día) acota la lectura; un
anterior más viejo que ``floor`` no cambia nada, porque esa racha llegó al umbral antes de
``since``."""
_MARK: Final = text(
    "INSERT INTO accreditation.adoption_alert (organization_id, plant_id, zone_id, streak_start,"
    " silent_days, threshold_days, evaluated_day, emitted_at)"
    " VALUES (:organization_id, :plant_id, :zone_id, :streak_start, :silent_days,"
    " :threshold_days, :evaluated_day, :emitted_at)"
    " ON CONFLICT (organization_id, zone_id, streak_start) DO NOTHING RETURNING streak_start"
)
"""La marca de la racha: una fila nueva o ninguna (otra ejecución ya la publicó)."""
_PRODUCTIVE_ZONES: Final = text(
    "SELECT zone_id, mounting ->> 'decided_at' AS mounting_at, usage ->> 'decided_at' AS usage_at"
    " FROM catalog.zone_gate_state WHERE organization_id = :organization_id"
    " AND plant_id = :plant_id AND resulting_mode = 'productive' ORDER BY zone_id"
)
"""Las zonas activas de la planta: montaje y uso aprobados (``zone_activated``)."""


class ZoneActivations(Protocol):
    """Las zonas activas de una planta con el instante de su activación."""

    async def active_zones(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> Mapping[uuid.UUID, datetime]: ...


def activation_instant(mounting_at: object, usage_at: object) -> datetime | None:
    """El instante desde el que una zona ``productive`` está activa: la última de sus dos
    aprobaciones (``decided_at`` del montaje y del uso, ISO 8601). ``None`` si falta alguna, que
    sería una proyección rota: la zona no se trata como activa (nunca una alerta inventada)."""
    moments: list[datetime] = []
    for value in (mounting_at, usage_at):
        if not isinstance(value, str):
            return None
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return None
        if moment.tzinfo is None:
            return None
        moments.append(moment)
    return max(moments)


@repository
class ProductiveZones:
    """``ZoneActivations`` sobre la proyección de compuertas de U-03 (``catalog.zone_gate_state``,
    ``resulting_mode = productive``), con **una** sentencia por planta en la transacción del
    llamador y la seguridad a nivel de fila de su organización.

    No usa ``GateQueryPort.states_by_plant``: el puerto exige que la zona esté en
    ``allowed_scopes`` y el contexto de iteración periódica del planificador no tiene ninguno
    (``ScopeContexts._system``, BR-NUC-03), así que vería la planta vacía y ninguna zona alertaría
    nunca. La proyección es la misma que lee el puerto (``PostgresGateQuery._STATES_BY_PLANT``)."""

    async def active_zones(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> Mapping[uuid.UUID, datetime]:
        rows = (
            await transaction.execute(
                _PRODUCTIVE_ZONES,
                {"organization_id": transaction.context.organization_id, "plant_id": plant_id},
            )
        ).all()
        active: dict[uuid.UUID, datetime] = {}
        for row in rows:
            since = activation_instant(row.mounting_at, row.usage_at)
            if since is not None:
                active[_uuid(row.zone_id)] = since
        return active


@dataclass(frozen=True, slots=True)
class AdoptionAlert:
    """Un ``adoption_low`` publicado: la zona, su racha y el umbral vigente."""

    plant_id: uuid.UUID
    zone_id: uuid.UUID
    streak: SilenceStreak
    threshold_days: int
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class _Plant:
    plant_id: uuid.UUID
    timezone: str


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


@repository
class AdoptionCheck:
    """El manejador de ``adoption_check`` para la transacción de una organización."""

    def __init__(
        self,
        *,
        activations: ZoneActivations,
        configuration: PostgresLoopConfigurationRepository,
        outbox: OutboxPort,
        clock: Clock,
        max_catchup_days: int = MAX_CATCHUP_DAYS,
    ) -> None:
        if type(max_catchup_days) is not int or max_catchup_days < 1:
            raise ValueError("max_catchup_days debe ser al menos 1")
        self._activations = activations
        self._configuration = configuration
        self._outbox = outbox
        self._clock = clock
        self._max_catchup_days = max_catchup_days

    def __repr__(self) -> str:
        return "AdoptionCheck()"

    async def __call__(self, transaction: Transaction) -> None:
        """Delegado sin guarda (``@repository`` no vigila ``__call__``): ver ``run``."""
        await self.run(transaction)

    async def run(self, transaction: Transaction) -> tuple[AdoptionAlert, ...]:
        """Una pasada sobre las plantas de la organización de ``transaction``; devuelve los
        ``adoption_low`` que publicó."""
        organization_id = transaction.context.organization_id
        rows = (await transaction.execute(_PLANTS, {"organization_id": organization_id})).all()
        alerts: list[AdoptionAlert] = []
        for row in rows:
            plant = _Plant(_uuid(row.plant_id), str(row.timezone))
            alerts.extend(await self._plant(transaction, plant))
        return tuple(alerts)

    async def _plant(self, transaction: Transaction, plant: _Plant) -> list[AdoptionAlert]:
        active = await self._activations.active_zones(transaction, plant.plant_id)
        if not active:
            return []
        settings, _version = await self._configuration.settings_in(transaction, plant.plant_id)
        threshold: int = settings["adoption_silence_days"]
        now = to_millisecond(self._clock.now())
        day = today(self._clock, plant.timezone)
        zones = sorted(active)
        since = day - self._max_catchup_days * _ONE_DAY
        active_since = {zone: local_date(at, plant.timezone) for zone, at in active.items()}
        closed = await self._closed_streaks(
            transaction, plant.plant_id, active_since, threshold, since
        )
        last = await self._last_activity(transaction, plant.plant_id, zones)
        alerts: list[AdoptionAlert] = []
        for zone_id in zones:
            last_day, last_at = last.get(zone_id, (None, None))
            streak = silence_streak(
                today=day, active_since=active_since[zone_id], last_activity_day=last_day
            )
            due = [*closed.get(zone_id, ())]
            if adoption_due(streak, threshold):
                due.append(AdoptionAlert(plant.plant_id, zone_id, streak, threshold, last_at))
            for alert in due:
                if await self._publish_once(transaction, alert, day, now):
                    alerts.append(alert)
        return alerts

    async def _closed_streaks(
        self,
        transaction: Transaction,
        plant_id: uuid.UUID,
        active_since: Mapping[uuid.UUID, date],
        threshold: int,
        since: date,
    ) -> dict[uuid.UUID, list[AdoptionAlert]]:
        """Las rachas que una actividad cerró después de llegar al umbral, en orden, desde las
        que lo alcanzaron el día ``since``: la ejecución continua las habría publicado ese día."""
        rows = (
            await transaction.execute(
                _CLOSED_STREAKS,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_ids": [str(zone) for zone in sorted(active_since)],
                    "since": since,
                    "floor": since - (threshold + 1) * _ONE_DAY,
                },
            )
        ).all()
        closed: dict[uuid.UUID, list[AdoptionAlert]] = {}
        for row in rows:
            zone_id = _uuid(row.zone_id)
            streak = silence_streak(
                today=row.day,
                active_since=active_since[zone_id],
                last_activity_day=row.previous_day,
            )
            if adoption_due(streak, threshold) and streak.start + threshold * _ONE_DAY >= since:
                alert = AdoptionAlert(plant_id, zone_id, streak, threshold, row.previous_at)
                closed.setdefault(zone_id, []).append(alert)
        return closed

    async def _last_activity(
        self, transaction: Transaction, plant_id: uuid.UUID, zones: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[date, datetime | None]]:
        rows = (
            await transaction.execute(
                _LAST_ACTIVITY,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_ids": [str(zone) for zone in zones],
                },
            )
        ).all()
        return {_uuid(row.zone_id): (row.last_day, row.last_at) for row in rows}

    async def _publish_once(
        self, transaction: Transaction, alert: AdoptionAlert, day: date, now: datetime
    ) -> bool:
        """La marca de la racha y, solo si es nueva, ``adoption_low``; ``False`` si otra
        ejecución ya la había publicado."""
        silent_days = min(alert.streak.silent_days, MAX_REPORTED_SILENT_DAYS)
        marked = (
            await transaction.execute(
                _MARK,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": alert.plant_id,
                    "zone_id": alert.zone_id,
                    "streak_start": alert.streak.start,
                    "silent_days": silent_days,
                    "threshold_days": alert.threshold_days,
                    "evaluated_day": day,
                    "emitted_at": now,
                },
            )
        ).first()
        if marked is None:
            return False
        payload: dict[str, object] = {"zone_id": str(alert.zone_id), "silent_days": silent_days}
        if alert.last_activity_at is not None:
            payload["last_activity_at"] = format_timestamp(alert.last_activity_at)
        await self._outbox.publish(
            transaction,
            NewEvent(event_name=ADOPTION_LOW, payload=payload, plant_id=alert.plant_id),
        )
        return True


def register_adoption_check(registry: PeriodicTaskRegistry, task: AdoptionCheck) -> PeriodicTask:
    """Registra ``adoption_check`` una vez al día, por organización, con ``unit = U-04``."""

    async def handler(transaction: Transaction) -> None:
        await task(transaction)

    return registry.register(
        ADOPTION_CHECK,
        ADOPTION_CHECK_SCHEDULE,
        handler,
        unit=ActorUnit.U04,
        iteration=TaskIteration.PER_ORGANIZATION,
    )
