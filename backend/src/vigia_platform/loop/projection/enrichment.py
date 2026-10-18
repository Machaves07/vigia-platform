"""Enriquecimiento de la proyección del lazo (TASK-341; LC-LAZ-02; domain-entities.md de U-04
§2.2 y §9 con su nota U04-H-05; NFR-LAZ-03 y 05; PAT-LAZ-REN-05 y RES-04).

Las columnas de ``FindingState`` que la bandeja necesita para no llamar a U-03 (presupuesto cero,
NFR-LAZ-03) y que el pliegue puro deja con el valor conservador:

- ``observability_state_at_event``: ``CoveragePort.estado_en(zona, instante)`` de U-02;
- ``zone_gate_status_at_event``: la compuerta de **uso** en el instante del hecho, por
  ``GateQueryPort.state_at`` (sin intervalo que lo contenga, ``pending``: A-60, toda zona nace
  ``pending/pending``);
- ``single_occupancy_zone`` y ``aggregation_window_minutes``: solo por la forma por lote
  ``CatalogQueryPort.single_occupancy_many`` (≤ 50 zonas por llamada: ``ceil(N/50)`` llamadas para
  N zonas). **Sin respuesta, fallo hacia la privacidad** (SUP-U03-04): la zona es unipersonal con
  ``single_occupancy_default_window_minutes`` de la configuración de la planta;
- ``walk_test_regression_pending`` y su causa: la **historia** de regresión de la zona, desde los
  registros ``walk_test_regression_marked`` y ``walk_test_regression_cleared`` del expediente
  (U04-H-05: ``regression_state`` solo da el estado actual), leída con **una** sentencia para
  todas las zonas; la anotación vale en ``[marked_at, cleared_at)``;
- ``evidence_flagged``: alguna evidencia del tramo con ``marker_verification_result = broken``,
  por ``EvidencePort.resultados_marca`` en una sola consulta (BR-LAZ-150). El evento de U-02 que
  avisaría de una marca rota después no existe (pendiente nº 21, adición E no aplicada): la
  proyección refleja la marca **con la latencia de la lectura al proyectar**, no de un evento.

``zone_agreement_revoked`` (desde ``gate_state_changed``) y ``assignee_inactive`` (desde
``user_deactivated``) no consultan ningún puerto: los fija el consumidor con la carga del evento.

**Sin llamadas dentro de un bucle sobre elementos**: cada consulta recibe la lista entera de zonas
(o de evidencias) y su número de llamadas depende solo del número de zonas, nunca del de filas.
La compuerta y la cobertura no tienen valor por defecto seguro: si no responden, la excepción sale
y el evento se reintenta por la bandeja de salida (la fila no se escribe con un valor inventado).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from sqlalchemy import text
from vigia_contracts.models.enumerations import GateStatus

from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.ports import (
    MAX_SINGLE_OCCUPANCY_ZONES,
    CatalogQueryPort,
    GateQueryPort,
)
from vigia_platform.ledger.application.coverage import CoveragePort
from vigia_platform.ledger.application.evidence_read import MarkerResult, MarkerVerification
from vigia_platform.loop.config.parameters import LoopSettings
from vigia_platform.loop.projection.fold import FindingStateRow, aggregation_bucket
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "DEFAULT_WINDOW_PARAMETER",
    "REGRESSION_RECORD_TYPES",
    "FindingFacts",
    "MarkerPort",
    "ProjectionEnrichment",
    "RegressionMark",
    "SettingsSource",
    "ZoneOccupancy",
    "regression_at",
    "with_occupancy",
    "with_regression",
]

DEFAULT_WINDOW_PARAMETER: Final = "single_occupancy_default_window_minutes"
"""El parámetro de la configuración que vale si ``single_occupancy_many`` no responde."""
REGRESSION_MARKED: Final = "walk_test_regression_marked"
REGRESSION_CLEARED: Final = "walk_test_regression_cleared"
REGRESSION_RECORD_TYPES: Final = (REGRESSION_MARKED, REGRESSION_CLEARED)

_REGRESSION_HISTORY: Final = text(
    "SELECT record_type, chain_sequence, content FROM ledger.ledger_record"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND scope_zone_id = ANY(CAST(:zone_ids AS uuid[]))"
    " AND record_type = ANY(CAST(:record_types AS text[]))"
    " ORDER BY chain_sequence"
)
"""La historia de regresión de **todas** las zonas pedidas en una sentencia (índice
``ledger_record_zone_type_occurred``)."""

_log = get_logger("loop.projection")


class MarkerPort(Protocol):
    """Lo que el enriquecimiento usa de ``EvidencePort``: el resultado de la marca por lote."""

    async def resultados_marca(
        self, context: ScopeContext, evidence_ids: Iterable[uuid.UUID]
    ) -> Mapping[uuid.UUID, MarkerVerification]: ...


class SettingsSource(Protocol):
    """``PostgresLoopConfigurationRepository.settings_in``: la configuración de la planta."""

    async def settings_in(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> tuple[LoopSettings, int | None]: ...


@dataclass(frozen=True, slots=True)
class ZoneOccupancy:
    """La marca unipersonal de una zona como la usa la proyección."""

    single_occupancy: bool
    aggregation_window_minutes: int
    answered: bool
    """Falso si ``single_occupancy_many`` no respondió y se aplicó el fallo hacia la privacidad."""


@dataclass(frozen=True, slots=True, order=True)
class RegressionMark:
    """Un registro de la historia de regresión de una zona."""

    at: datetime
    """``marked_at`` o ``cleared_at`` del registro."""
    chain_sequence: int
    zone_id: uuid.UUID
    pending: bool
    """Verdadero en ``walk_test_regression_marked``; falso en ``walk_test_regression_cleared``."""
    cause: str | None


def regression_at(history: Iterable[RegressionMark], instant: datetime) -> tuple[bool, str | None]:
    """La anotación «acta no vigente» en ``instant``: la del último registro con ``at <= instant``
    (en empate, el posterior en la cadena); sin ninguno, vigente."""
    applicable = [mark for mark in history if mark.at <= instant]
    if not applicable:
        return False, None
    last = max(applicable, key=lambda mark: (mark.at, mark.chain_sequence))
    return (True, last.cause) if last.pending else (False, None)


def with_occupancy(row: FindingStateRow, occupancy: ZoneOccupancy) -> FindingStateRow:
    """La fila con la marca unipersonal y la ventana de su zona (el cubo no cambia: es la clave
    de partición y se fija al nacer la fila)."""
    return dataclasses.replace(
        row,
        single_occupancy_zone=occupancy.single_occupancy,
        aggregation_window_minutes=occupancy.aggregation_window_minutes,
    )


def with_regression(row: FindingStateRow, history: Iterable[RegressionMark]) -> FindingStateRow:
    """La fila con la anotación de regresión en el instante de su hecho."""
    pending, cause = regression_at(history, row.occurred_at_exact)
    return dataclasses.replace(
        row, walk_test_regression_pending=pending, walk_test_regression_cause=cause
    )


@dataclass(frozen=True, slots=True)
class FindingFacts:
    """Lo que los puertos dijeron de un hallazgo nuevo (o del tramo de uno ya proyectado)."""

    observability_state: str | None
    gate_status: str | None
    occupancy: ZoneOccupancy | None
    regression: tuple[RegressionMark, ...]
    evidence_flagged: bool

    def enrich(self, previous: FindingStateRow | None, row: FindingStateRow) -> FindingStateRow:
        """``store.Enrich``: la fila nueva entera; en una fila que ya existía, solo la marca de
        evidencias se acumula (un tramo nuevo puede traer un clip marcado)."""
        if previous is not None:
            return dataclasses.replace(
                row, evidence_flagged=previous.evidence_flagged or self.evidence_flagged
            )
        if self.observability_state is None or self.gate_status is None or self.occupancy is None:
            raise ValueError("un hallazgo nuevo necesita cobertura, compuerta y ocupación")
        window = self.occupancy.aggregation_window_minutes
        enriched = dataclasses.replace(
            with_regression(with_occupancy(row, self.occupancy), self.regression),
            observability_state_at_event=self.observability_state,
            zone_gate_status_at_event=self.gate_status,
            occurred_at_bucket=aggregation_bucket(row.occurred_at_exact, window),
            evidence_flagged=self.evidence_flagged,
        )
        return enriched


def _instant(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


class ProjectionEnrichment:
    """Las consultas del enriquecimiento, siempre por lote (NFR-LAZ-03 y 05)."""

    def __init__(
        self,
        *,
        catalog: CatalogQueryPort,
        gates: GateQueryPort,
        coverage: CoveragePort,
        markers: MarkerPort,
        settings: SettingsSource,
    ) -> None:
        self._catalog = catalog
        self._gates = gates
        self._coverage = coverage
        self._markers = markers
        self._settings = settings

    async def default_window(self, transaction: Transaction, plant_id: uuid.UUID) -> int:
        """La ventana por defecto de la planta (la copia local de SUP-U03-04)."""
        settings, _ = await self._settings.settings_in(transaction, plant_id)
        return settings[DEFAULT_WINDOW_PARAMETER]

    async def occupancy(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], default_window: int
    ) -> dict[uuid.UUID, ZoneOccupancy]:
        """La marca unipersonal de cada zona: ``ceil(N/50)`` llamadas a ``single_occupancy_many``.

        Un lote que no responde (o que no nombra una zona) deja sus zonas unipersonales con
        ``default_window``: el fallo es hacia la privacidad, nunca hacia mostrar el instante."""
        zones = list(dict.fromkeys(zone_ids))
        fallback = ZoneOccupancy(True, default_window, False)
        result: dict[uuid.UUID, ZoneOccupancy] = {}
        for start in range(0, len(zones), MAX_SINGLE_OCCUPANCY_ZONES):
            batch = zones[start : start + MAX_SINGLE_OCCUPANCY_ZONES]
            try:
                answers = await self._catalog.single_occupancy_many(context, batch)
            except Exception as error:
                _log.warning(
                    "ocupacion unipersonal sin respuesta: se agrega por defecto",
                    zones=len(batch),
                    error=type(error).__name__,
                )
                answers = ()
            answered = {
                answer.zone_id: ZoneOccupancy(
                    answer.single_occupancy, answer.aggregation_window_minutes, True
                )
                for answer in answers
            }
            result.update({zone: answered.get(zone, fallback) for zone in batch})
        return result

    async def regression_history(
        self, transaction: Transaction, plant_id: uuid.UUID, zone_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[RegressionMark, ...]]:
        """La historia de regresión de cada zona, en una sola sentencia sobre el expediente."""
        zones = list(dict.fromkeys(zone_ids))
        history: dict[uuid.UUID, list[RegressionMark]] = {zone: [] for zone in zones}
        if not zones:
            return {}
        rows = (
            await transaction.execute(
                _REGRESSION_HISTORY,
                {
                    "organization_id": transaction.context.organization_id,
                    "plant_id": plant_id,
                    "zone_ids": zones,
                    "record_types": list(REGRESSION_RECORD_TYPES),
                },
            )
        ).all()
        for row in rows:
            document = json.loads(bytes(row.content))
            zone = uuid.UUID(str(document["zone_id"]))
            if zone not in history:
                continue
            pending = row.record_type == REGRESSION_MARKED
            history[zone].append(
                RegressionMark(
                    at=_instant(document["marked_at" if pending else "cleared_at"]),
                    chain_sequence=int(row.chain_sequence),
                    zone_id=zone,
                    pending=pending,
                    cause=str(document["cause"]) if pending else None,
                )
            )
        return {zone: tuple(marks) for zone, marks in history.items()}

    async def evidence_flagged(
        self, context: ScopeContext, evidence_ids: Sequence[uuid.UUID]
    ) -> bool:
        """Verdadero si alguna evidencia tiene la marca rota (una sola consulta)."""
        if not evidence_ids:
            return False
        results = await self._markers.resultados_marca(context, evidence_ids)
        return any(result.result is MarkerResult.BROKEN for result in results.values())

    async def finding_facts(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        occurred_at: datetime,
        evidence_ids: Sequence[uuid.UUID],
        new: bool,
    ) -> FindingFacts:
        """Los hechos de un hallazgo: con ``new``, una llamada a cada puerto; si no, solo la marca
        de sus evidencias."""
        context = transaction.context
        flagged = await self.evidence_flagged(context, evidence_ids)
        if not new:
            return FindingFacts(None, None, None, (), flagged)
        coverage = await self._coverage.estado_en(context, zone_id, occurred_at)
        interval = await self._gates.state_at(context, zone_id, GateKind.USAGE, occurred_at)
        default_window = await self.default_window(transaction, plant_id)
        occupancy = await self.occupancy(context, [zone_id], default_window)
        history = await self.regression_history(transaction, plant_id, [zone_id])
        return FindingFacts(
            observability_state=coverage.state.value,
            gate_status=(GateStatus.PENDING if interval is None else interval.status).value,
            occupancy=occupancy[zone_id],
            regression=history.get(zone_id, ()),
            evidence_flagged=flagged,
        )
