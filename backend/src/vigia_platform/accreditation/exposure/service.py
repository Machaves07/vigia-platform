"""``ExposureService``: el indicador de exposición de una zona y un mes (TASK-322; LC-LAZ-28;
SUP-U05-31; PAT-LAZ-REN-01, REN-04 y MAN-05; BR-LAZ-118 a 123).

``read`` es la operación de la ruta: la zona tiene que existir en la organización del contexto y
el actor necesita ``exposure.read`` sobre ella (``Authorizer``: si no, ``ResourceNotFound``, como
una zona inexistente). ``indicator`` es ``ExposurePort``, para el paquete mensual (VIG-239): el
mismo cálculo sin la comprobación de permiso de una persona.

**Mes en curso por recálculo a demanda con resultado guardado** (PAT-LAZ-REN-01):

- el resultado se guarda en ``accreditation.exposure_indicator`` con ``inputs_digest``, que se
  compone de la zona, el periodo, el último ``chain_sequence`` de la planta y el punto de
  aplicación de la proyección del lazo (``repository.inputs``): una escritura nueva de la planta,
  o la proyección que la incorpora, lo hace caducar antes que el reloj;
- una petición **dentro de 15 minutos** con el mismo ``inputs_digest`` devuelve lo guardado sin
  recalcular; si no, se recalcula y se guarda (``recomputed_at``);
- un mes ya terminado se calcula una vez entero y queda **materializado** (``materialized_at`` ≥
  fin del periodo): se sirve sin recálculo y ningún valor del mes en curso lo sobrescribe
  (``repository.save``);
- en los dos casos, lo guardado solo se sirve si sus cubos tienen el ancho que exige **hoy** la
  privacidad de la zona: una zona que pasa a unipersonal nunca sirve cubos más finos que su
  ventana (PAT-LAZ-MAN-05), aunque su mes ya esté materializado.

**Orden del cálculo**: la huella de entradas se lee **antes** que los insumos (una escritura entre
medias deja una huella vieja y la petición siguiente recalcula, nunca al revés). Después, la
señal dura (``CatalogQueryPort.catalog_at`` al final de lo observado del periodo): sin ninguna
señal no auxiliar declarada, ``not_computable`` sin cifras y **sin fila** (BR-LAZ-115: la fila no
admite cifras nulas y nunca se guarda un cero en su lugar). Con señal: la línea de tiempo de U-02
por tramos de 31 días (``export.timeline.zone_timeline``, la misma de la exportación), la
historia de la compuerta de uso de U-03 y los episodios de la proyección, y ``compute`` hace el
resto.

**Fallo cerrado**: sin línea de tiempo (zona que U-02 no ve, o que no es una partición),
``coverage_unavailable``; sin historia de la compuerta, ``gate_state_unavailable``; nada se
guarda. Si ``CatalogQueryPort.single_occupancy`` no responde, se agrega con la ventana por
defecto de la planta (PAT-LAZ-MAN-05: agregar de más es reversible, exponer de menos no). Un
fallo transitorio de la base sale tal cual.

Un solo reloj (retro 15): ``now`` sale del ``Clock`` inyectado, truncado al milisegundo como lo
guarda U-02, y de él salen el fin de lo observado, la frescura y las marcas guardadas.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

from vigia_contracts.models.enumerations import GateStatus, SignalRole

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.export.composer_port import ExportInputUnavailable
from vigia_platform.accreditation.export.timeline import (
    TimelineCoverage,
    timeline_annotations,
    timeline_json,
    zone_timeline,
)
from vigia_platform.accreditation.exposure.compute import (
    HourlyBucket,
    Span,
    bucket_width,
    compute_exposure,
)
from vigia_platform.accreditation.exposure.indicator import (
    NO_CATALOG_REASON_ES,
    NO_HARD_SIGNAL_REASON_ES,
    USAGE_NOTICE_ES,
    ExposureIndicator,
    partial_reason_es,
)
from vigia_platform.accreditation.exposure.repository import (
    DigestInputs,
    ExposureRepository,
    StoredExposure,
    ZonePlace,
)
from vigia_platform.accreditation.manifest.model import RevokedGate
from vigia_platform.catalog.domain.catalog_version import ZoneCatalogVersion
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.catalog.domain.ports import PortLimitExceeded, PortQueryInvalid, SingleOccupancy
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.domain.coverage import CoverageState
from vigia_platform.loop.calendar import month_bounds
from vigia_platform.loop.config.parameters import LoopSettings
from vigia_platform.loop.detail_codes import LoopDetailCode, U04DetailCode
from vigia_platform.loop.domain.enums import ExposureStatus
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7

__all__ = [
    "FRESHNESS",
    "HARD_SIGNAL_ROLES",
    "ExposureInputMissing",
    "ExposurePeriodInvalid",
    "ExposureService",
    "inputs_digest",
]

FRESHNESS: Final = timedelta(minutes=15)
"""Validez del resultado guardado del mes en curso (``[objetivo propio]``, respuesta 8)."""
HARD_SIGNAL_ROLES: Final = frozenset(
    {SignalRole.ENERGY.value, SignalRole.GUARD.value, SignalRole.START_COMMAND.value}
)
"""Una señal dura es una señal declarada no auxiliar: ``auxiliary`` nunca entra en un predicado
(U-01) y ``tier_1`` exige una señal no auxiliar válida (BR-CTR-05)."""
_FORMULA: Final = "exposure-v1"
"""Versión del cálculo dentro de ``inputs_digest``: cambiar ``compute`` cambia la huella."""
_MS: Final = timedelta(milliseconds=1)
_READ: Final = PermissionKey.EXPOSURE_READ


class ExposurePeriodInvalid(Exception):
    """El periodo no es un mes ``AAAA-MM`` válido o aún no ha empezado: ``invalid_request``."""


class ExposureInputMissing(Exception):
    """Falta un insumo obligatorio de otra unidad: fallo cerrado con su ``detail_code``."""

    def __init__(self, detail_code: U04DetailCode) -> None:
        super().__init__(detail_code.value)
        self.detail_code = detail_code


class _Database(Protocol):
    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


class ExposureCatalog(Protocol):
    """Lo que el indicador usa de ``CatalogQueryPort`` (U-03)."""

    async def catalog_at(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> ZoneCatalogVersion: ...

    async def single_occupancy(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime | None = None
    ) -> SingleOccupancy: ...


class ExposureGates(Protocol):
    """Lo que el indicador usa de ``GateQueryPort`` (U-03)."""

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[GateInterval, ...]: ...


class ExposureSettings(Protocol):
    """Los parámetros del lazo de la planta (``PostgresLoopConfigurationRepository``)."""

    async def settings_in(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> tuple[LoopSettings, int | None]: ...


def inputs_digest(*, zone_id: uuid.UUID, period: str, inputs: DigestInputs) -> str:
    """La huella de entradas del cálculo (hex de 64). El ancho de los cubos no entra: lo comprueba
    ``_reusable`` contra lo guardado, también en un mes ya materializado."""
    document = {
        "formula": _FORMULA,
        "zone_id": str(zone_id),
        "period": period,
        "ledger_sequence": inputs.ledger_sequence,
        "projection_sequence": inputs.projection_sequence,
        "projection_state": inputs.projection_state,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _floor_ms(moment: datetime) -> datetime:
    return moment - timedelta(microseconds=moment.microsecond % 1000)


def _stored_width(stored: StoredExposure) -> int | None:
    widths = {int(bucket["width_minutes"]) for bucket in stored.hourly_distribution}
    return widths.pop() if len(widths) == 1 else None


def _reusable(
    stored: StoredExposure, *, digest: str, width: int, now: datetime, period_end: datetime
) -> bool:
    """¿Se sirve lo guardado sin recalcular? Definitivo con el ancho vigente, o del mes en curso
    con la misma huella y dentro de su validez."""
    if _stored_width(stored) != width:
        return False
    if stored.materialized_at >= period_end:
        return True
    if stored.inputs_digest != digest:
        return False
    stamp = stored.recomputed_at if stored.recomputed_at is not None else stored.materialized_at
    return timedelta(0) <= now - stamp < FRESHNESS


@dataclass(frozen=True, slots=True)
class _Period:
    name: str
    start: datetime
    end: datetime

    @property
    def key(self) -> date:
        """La clave de la fila: el primer día del mes (``exposure_indicator_month``)."""
        return date.fromisoformat(f"{self.name}-01")


@repository
class ExposureService:
    """``ExposurePort`` sobre PostgreSQL y los puertos de U-02 y U-03."""

    def __init__(
        self,
        *,
        database: _Database,
        authorizer: Authorizer,
        catalog: ExposureCatalog,
        gates: ExposureGates,
        coverage: TimelineCoverage,
        settings: ExposureSettings,
        clock: Clock,
        repository: ExposureRepository | None = None,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._catalog = catalog
        self._gates = gates
        self._coverage = coverage
        self._settings = settings
        self._clock = clock
        self._repository = repository if repository is not None else ExposureRepository()

    def __repr__(self) -> str:
        return "ExposureService()"

    async def read(
        self, context: ScopeContext, *, zone_id: uuid.UUID, period: str
    ) -> ExposureIndicator:
        """``GET /accreditation/exposure``: ``exposure.read`` sobre la zona (SUP-U05-31)."""
        place = await self._place(context, zone_id)
        authorized = await self._authorizer.authorize(
            context, _READ, Resource.zone(context.organization_id, place.plant_id, zone_id)
        )
        return await self._indicator(authorized, zone_id, period, place)

    async def indicator(
        self, context: ScopeContext, zone_id: uuid.UUID, period: str
    ) -> ExposureIndicator:
        """``ExposurePort``: el indicador de ``period`` para el paquete mensual (VIG-239)."""
        place = await self._place(context, zone_id)
        return await self._indicator(context, zone_id, period, place)

    # --- pasos ---------------------------------------------------------------------------------

    async def _place(self, context: ScopeContext, zone_id: uuid.UUID) -> ZonePlace:
        async with self._database.transaction(context) as transaction:
            place = await self._repository.zone(transaction, zone_id)
        if place is None:
            raise ResourceNotFound()
        return place

    def _period(self, place: ZonePlace, period: str, now: datetime) -> _Period:
        try:
            start, end = month_bounds(place.timezone, period)
        except (ValueError, KeyError):
            raise ExposurePeriodInvalid() from None
        if start > now:
            raise ExposurePeriodInvalid()
        return _Period(period, start, end)

    async def _indicator(
        self, context: ScopeContext, zone_id: uuid.UUID, period_name: str, place: ZonePlace
    ) -> ExposureIndicator:
        now = _floor_ms(self._clock.now())
        period = self._period(place, period_name, now)
        definitive = period.end <= now
        async with self._database.transaction(context) as transaction:
            settings, _ = await self._settings.settings_in(transaction, place.plant_id)
            inputs = await self._repository.inputs(transaction, place.plant_id)
            stored = await self._repository.stored(transaction, zone_id=zone_id, period=period.key)
        single, window = await self._occupancy(context, zone_id, settings)
        width = bucket_width(single_occupancy=single, window_minutes=window)
        digest = inputs_digest(zone_id=zone_id, period=period.name, inputs=inputs)
        if stored is not None and _reusable(
            stored, digest=digest, width=width, now=now, period_end=period.end
        ):
            return _from_stored(stored, period)
        reason = await self._no_hard_signal(context, zone_id, min(now, period.end) - _MS)
        if reason is not None:
            return _not_computable(context, place, zone_id, period, reason)
        try:
            timeline = await zone_timeline(
                self._coverage, context, zone_id, period.start, period.end, now
            )
        except ExportInputUnavailable:
            raise ExposureInputMissing(AccreditationDetailCode.COVERAGE_UNAVAILABLE) from None
        gates = await self._revoked(context, zone_id, period)
        async with self._database.transaction(context) as transaction:
            episodes = await self._repository.episodes(
                transaction,
                plant_id=place.plant_id,
                zone_id=zone_id,
                period_start=period.start,
                period_end=period.end,
            )
        threshold = settings["exposure_coverage_threshold_percent"]
        figures = compute_exposure(
            period=Span(period.start, period.end),
            episodes=episodes,
            observable=tuple(
                Span(interval.starts_at, interval.ends_at)
                for interval in timeline.intervals
                if interval.state is CoverageState.OBSERVABLE
            ),
            revoked=tuple(Span(gate.since, gate.until or period.end) for gate in gates),
            hard_signal=True,
            threshold_percent=threshold,
            timezone=ZoneInfo(place.timezone),
            single_occupancy=single,
            window_minutes=window,
        )
        coverage_percent = figures.observable_coverage_percent
        if coverage_percent is None:  # pragma: no cover - con señal dura siempre hay cifras
            raise RuntimeError("un indicador con señal dura sin cobertura")
        values: dict[str, Any] = {
            "exposure_id": uuid7(self._clock),
            "plant_id": place.plant_id,
            "zone_id": zone_id,
            "period": period.key,
            "status": figures.status.value,
            "confirmed_minutes": figures.confirmed_minutes,
            "pending_classification_minutes": figures.pending_classification_minutes,
            "observable_coverage_percent": coverage_percent,
            "coverage_timeline_ref": timeline_json(
                timeline,
                timeline_annotations(zone_id, period.start, period.end, (), gates),
            ),
            "hourly_distribution": [
                {
                    "start_minute": bucket.start_minute,
                    "width_minutes": bucket.width_minutes,
                    "confirmed_ms": bucket.confirmed_ms,
                }
                for bucket in figures.hourly
            ],
            "partial_reason_es": (
                partial_reason_es(coverage_percent, threshold)
                if figures.status is ExposureStatus.PARTIAL
                else None
            ),
            "usage_notice_es": USAGE_NOTICE_ES,
            "computed_at": now,
            "recomputed_at": None if definitive else now,
            "inputs_digest": digest,
        }
        async with self._database.transaction(context) as transaction:
            saved = await self._repository.save(
                transaction, values=values, definitive=definitive, period_end=period.end
            )
            if saved is None:
                saved = await self._repository.stored(
                    transaction, zone_id=zone_id, period=period.key
                )
        if saved is None:  # pragma: no cover - la fila existe: el INSERT chocó con ella
            raise RuntimeError("la fila del indicador desapareció entre dos sentencias")
        return _from_stored(saved, period)

    async def _occupancy(
        self, context: ScopeContext, zone_id: uuid.UUID, settings: LoopSettings
    ) -> tuple[bool, int]:
        try:
            answer = await self._catalog.single_occupancy(context, zone_id)
        except (ResourceNotFound, PortQueryInvalid, PortLimitExceeded, LookupError):
            return True, settings["single_occupancy_default_window_minutes"]
        return answer.single_occupancy, answer.aggregation_window_minutes

    async def _no_hard_signal(
        self, context: ScopeContext, zone_id: uuid.UUID, at: datetime
    ) -> str | None:
        """El motivo de ``not_computable``, o ``None`` si la zona declara alguna señal dura."""
        try:
            catalog = await self._catalog.catalog_at(context, zone_id, at)
        except ResourceNotFound:
            return NO_CATALOG_REASON_ES
        signals: Sequence[Mapping[str, Any]] = catalog.payload.get("signals") or ()
        if any(signal.get("role") in HARD_SIGNAL_ROLES for signal in signals):
            return None
        return NO_HARD_SIGNAL_REASON_ES

    async def _revoked(
        self, context: ScopeContext, zone_id: uuid.UUID, period: _Period
    ) -> tuple[RevokedGate, ...]:
        """Los intervalos de compuerta de uso revocada que tocan el periodo (respuesta 12)."""
        try:
            intervals = await self._gates.gate_history(context, zone_id, period.start, period.end)
        except (ResourceNotFound, PortQueryInvalid, PortLimitExceeded, LookupError):
            raise ExposureInputMissing(LoopDetailCode.GATE_STATE_UNAVAILABLE) from None
        if any(interval.zone_id != zone_id for interval in intervals):
            raise ExposureInputMissing(LoopDetailCode.GATE_STATE_UNAVAILABLE)
        return tuple(
            RevokedGate(
                zone_id=zone_id,
                since=interval.effective_from,
                until=interval.effective_until,
                reason_es=interval.reason_es,
                record_id=interval.record_id,
            )
            for interval in intervals
            if interval.gate is GateKind.USAGE and interval.status.value == GateStatus.REVOKED.value
        )


def _from_stored(stored: StoredExposure, period: _Period) -> ExposureIndicator:
    return ExposureIndicator(
        exposure_id=stored.exposure_id,
        organization_id=stored.organization_id,
        plant_id=stored.plant_id,
        zone_id=stored.zone_id,
        period=period.name,
        period_from=period.start,
        period_to=period.end,
        status=ExposureStatus(stored.status),
        confirmed_minutes=stored.confirmed_minutes,
        pending_classification_minutes=stored.pending_classification_minutes,
        observable_coverage_percent=stored.observable_coverage_percent,
        hourly_distribution=tuple(
            HourlyBucket(
                start_minute=int(bucket["start_minute"]),
                width_minutes=int(bucket["width_minutes"]),
                confirmed_ms=int(bucket["confirmed_ms"]),
            )
            for bucket in stored.hourly_distribution
        ),
        coverage_timeline=stored.coverage_timeline_ref,
        not_computable_reason_es=None,
        partial_reason_es=stored.partial_reason_es,
        usage_notice_es=stored.usage_notice_es,
        materialized_at=stored.materialized_at,
        recomputed_at=stored.recomputed_at,
        inputs_digest=stored.inputs_digest,
    )


def _not_computable(
    context: ScopeContext, place: ZonePlace, zone_id: uuid.UUID, period: _Period, reason: str
) -> ExposureIndicator:
    return ExposureIndicator(
        exposure_id=None,
        organization_id=context.organization_id,
        plant_id=place.plant_id,
        zone_id=zone_id,
        period=period.name,
        period_from=period.start,
        period_to=period.end,
        status=ExposureStatus.NOT_COMPUTABLE,
        confirmed_minutes=None,
        pending_classification_minutes=None,
        observable_coverage_percent=None,
        hourly_distribution=(),
        coverage_timeline=None,
        not_computable_reason_es=reason,
        partial_reason_es=None,
        usage_notice_es=USAGE_NOTICE_ES,
        materialized_at=None,
        recomputed_at=None,
        inputs_digest=None,
    )
