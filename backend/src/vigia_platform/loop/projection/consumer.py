"""Consumidor ``loop_projection`` (TASK-341; LC-LAZ-02; business-logic-model.md de U-04 §8.4;
domain-entities.md §9 con su nota U04-H-05; BR-LAZ-140, 146, 150, 151, 154 y 155; NFR-LAZ-05).

Aplica a la proyección los eventos que vienen de fuera de U-04, en la transacción que el
despachador abre con el ``ScopeContext`` derivado del evento (BR-LAZ-140) y con idempotencia por
``event_id`` (BR-LAZ-151, PAT-LAZ-RES-05):

- ``finding_received``: lee el ``Finding`` completo por ``LectorExpediente.get`` con el
  ``platform_record_id`` del evento (el evento no trae el episodio, U04-H-05), lo aplica con el
  pliegue (``LoopProjectionStore.apply_in_transaction``) y enriquece la fila **antes** de escribirla
  (``ProjectionEnrichment``). Un tramo tardío (BR-LAZ-155) escribe primero su registro
  ``late_segment_attached`` y lo aplica después;
- ``gate_state_changed`` de la compuerta de uso: ``zone_agreement_revoked`` (y desde cuándo) en
  **todas** las filas de la zona; con ``approved`` se levanta;
- ``catalog_updated``: la marca unipersonal y la ventana de la zona;
- ``regression_marked`` y ``regression_cleared``: la anotación «acta no vigente» de cada fila de la
  zona, desde la historia de regresión del expediente;
- ``node_enrolled``, ``node_revoked`` y ``node_decommissioned``: refresca lo enriquecido por zona
  (marca unipersonal e historia de regresión) de las zonas del nodo. La explicación «no se observó»
  la compone la bandeja (TASK-307) desde su propia lectura: ninguna columna de ``FindingState``
  depende del nodo;
- ``user_deactivated`` (U-02): ``assignee_inactive`` en las filas cuyo responsable es el usuario,
  **sin cambiar su estado** (BR-LAZ-154); la reasignación (``action_assigned``) la levanta.

Los cambios por zona van **en lote, con una sola sentencia** (``update_enrichment``), y solo a las
filas que aún no llevan el ``last_event_id`` del evento: la segunda entrega no escribe nada.

**Orden de candados** (el del módulo, ``store``): cadena de la planta (solo si hay tramo tardío,
al escribir su registro), control de la planta, filas. Todas las consultas a los puertos van
**antes** de tomar ningún candado de la proyección, en sus propias transacciones de lectura.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from vigia_contracts.models.enumerations import AcceptanceStatus, GateStatus

from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.ledger.application.reader import LedgerRecordView
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    Receipt,
    RecordScope,
)
from vigia_platform.loop.projection.enrichment import (
    ProjectionEnrichment,
    RegressionMark,
    ZoneOccupancy,
    with_occupancy,
    with_regression,
)
from vigia_platform.loop.projection.fold import (
    FindingStateRow,
    LateSegmentNotice,
    ProjectionInput,
)
from vigia_platform.loop.projection.store import LoopProjectionStore, ProjectionRecordMissing
from vigia_platform.loop.projection.store import projection_input as as_projection_input
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent, OutboxEvent
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "LOOP_PROJECTION",
    "LOOP_PROJECTION_CONCURRENCY",
    "LOOP_PROJECTION_EVENTS",
    "LateSegmentRace",
    "LoopProjectionConsumer",
    "RecordReader",
    "register_loop_projection",
]

LOOP_PROJECTION: Final = "loop_projection"
LOOP_PROJECTION_CONCURRENCY: Final = 4
"""El mamparo del diseño (``loop.worker.bulkheads.DEFAULT_CONCURRENCY``, LC-LAZ-40)."""
FINDING_RECEIVED: Final = "finding_received"
GATE_STATE_CHANGED: Final = "gate_state_changed"
CATALOG_UPDATED: Final = "catalog_updated"
REGRESSION_EVENTS: Final = frozenset({"regression_marked", "regression_cleared"})
NODE_EVENTS: Final = frozenset({"node_enrolled", "node_revoked", "node_decommissioned"})
USER_DEACTIVATED: Final = "user_deactivated"
LOOP_PROJECTION_EVENTS: Final = (
    FINDING_RECEIVED,
    GATE_STATE_CHANGED,
    CATALOG_UPDATED,
    "regression_marked",
    "regression_cleared",
    "node_enrolled",
    "node_revoked",
    "node_decommissioned",
    USER_DEACTIVATED,
)
"""Los eventos de U-03 de §8.4 que cambian la proyección y ``user_deactivated`` de U-02."""
LATE_SEGMENT_ATTACHED: Final = "late_segment_attached"


class RecordReader(Protocol):
    """Lo que el consumidor usa de ``LectorExpediente``."""

    async def get(
        self,
        context: ScopeContext,
        record_id: uuid.UUID,
        *,
        record_types: tuple[str, ...] | None = None,
    ) -> LedgerRecordView | None: ...


class LateSegmentRace(RuntimeError):
    """El tramo resultó tardío (o con otro estado de llegada) entre la previsión y la aplicación.

    La transacción se revierte, también el registro ``late_segment_attached`` escrito, y el
    despachador reintenta la entrega: la previsión siguiente ya ve el estado nuevo."""


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _instant(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _ZoneRefresh:
    """``RowChange`` por zona: la marca unipersonal y la anotación de regresión de cada fila
    (``None`` deja esa parte como estaba)."""

    occupancy: Mapping[uuid.UUID, ZoneOccupancy] | None
    regression: Mapping[uuid.UUID, tuple[RegressionMark, ...]] | None

    def __call__(self, row: FindingStateRow) -> FindingStateRow:
        if self.regression is not None:
            row = with_regression(row, self.regression.get(row.zone_id, ()))
        occupancy = None if self.occupancy is None else self.occupancy.get(row.zone_id)
        return row if occupancy is None else with_occupancy(row, occupancy)


class LoopProjectionConsumer:
    """El manejador de ``loop_projection`` (``ConsumerHandler``)."""

    def __init__(
        self,
        *,
        store: LoopProjectionStore,
        reader: RecordReader,
        enrichment: ProjectionEnrichment,
        writer: EscritorExpediente,
        clock: Clock,
    ) -> None:
        self._store = store
        self._reader = reader
        self._enrichment = enrichment
        self._writer = writer
        self._clock = clock

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        await self.apply(event, transaction)

    async def apply(self, event: OutboxEvent, transaction: Transaction) -> int:
        """Aplica ``event`` y devuelve cuántas filas de ``finding_state`` escribió (0 si ya
        estaba aplicado o no cambia ninguna)."""
        name = event.event_name
        if name == FINDING_RECEIVED:
            return await self._finding_received(event, transaction)
        if name == USER_DEACTIVATED:
            return await self._store.update_enrichment(
                transaction,
                event_id=event.event_id,
                at=event.created_at,
                assignee_user_id=_uuid(event.payload["user_id"]),
                change=lambda row: replace(row, assignee_inactive=True),
            )
        if event.plant_id is None:
            raise ValueError(f"{name} sin planta: no es de ninguna partición del lazo")
        if name == GATE_STATE_CHANGED:
            return await self._gate_state_changed(event, transaction, event.plant_id)
        if name == CATALOG_UPDATED:
            return await self._zones(event, transaction, event.plant_id, regression=False)
        if name in REGRESSION_EVENTS:
            return await self._zones(event, transaction, event.plant_id, occupancy=False)
        if name in NODE_EVENTS:
            return await self._zones(event, transaction, event.plant_id)
        raise ValueError(f"evento no suscrito a loop_projection: {name}")

    # --- finding_received ----------------------------------------------------------------------

    async def _finding_received(self, event: OutboxEvent, transaction: Transaction) -> int:
        record_id = _uuid(event.payload["platform_record_id"])
        view = await self._reader.get(
            transaction.context, record_id, record_types=(FINDING_RECEIVED,)
        )
        if view is None:
            raise ProjectionRecordMissing(str(record_id))
        item = as_projection_input(view, event.event_id)
        if item.plant_id is None:
            raise ProjectionRecordMissing(str(record_id))
        current, preview = await self._store.preview(transaction, item)
        if not preview.applied:
            return 0
        document = item.document
        facts = await self._enrichment.finding_facts(
            transaction,
            plant_id=item.plant_id,
            zone_id=_uuid(document["zone_id"]),
            occurred_at=_instant(document["node_time"]["started_at"]),
            evidence_ids=[evidence.evidence_id for evidence in view.evidences],
            new=current is None,
        )
        late = preview.late_segment
        receipt = await self._late_record(transaction, item, late) if late is not None else None
        result = await self._store.apply_in_transaction(transaction, item, enrich=facts.enrich)
        if result.late_segment != late:
            raise LateSegmentRace(str(item.record_id))
        if receipt is not None:
            await self._store.apply_in_transaction(transaction, receipt)
        return 1 if result.applied and result.finding is not None else 0

    async def _late_record(
        self, transaction: Transaction, item: ProjectionInput, notice: LateSegmentNotice
    ) -> Receipt:
        """El registro ``late_segment_attached`` del tramo (BR-LAZ-155), antes de la proyección:
        toma la cadena de la planta primero, como toda escritura de U-04."""
        record_id = uuid7(self._clock)
        zone_id = _uuid(item.document["zone_id"])
        content: dict[str, Any] = {
            "segment_record_id": str(notice.segment_record_id),
            "episode_id": str(notice.episode_id),
            "segment_index": notice.segment_index,
            "received_at": format_timestamp(notice.received_at),
            "episode_state_at_arrival": notice.episode_state_at_arrival.value,
            "reopened_loop": False,
            "delay_ms": notice.delay_ms,
        }
        if notice.previous_segment_record_id is not None:
            content["previous_segment_record_id"] = str(notice.previous_segment_record_id)
        written = await self._writer.write(
            transaction.context,
            LATE_SEGMENT_ATTACHED,
            content,
            scope=RecordScope(plant_id=item.plant_id, zone_id=zone_id),
            events=(
                NewEvent(
                    event_name=LATE_SEGMENT_ATTACHED,
                    payload={
                        "zone_id": str(zone_id),
                        "episode_id": str(notice.episode_id),
                        "segment_record_id": str(notice.segment_record_id),
                        "record_id": str(record_id),
                        "segment_index": notice.segment_index,
                    },
                ),
            ),
            occurred_at=notice.received_at,
            transaction=transaction,
            record_id=record_id,
        )
        if isinstance(written, LedgerRejection):
            raise RuntimeError(f"late_segment_attached rechazado: {written.code.value}")
        if written.status is not AcceptanceStatus.ACCEPTED:
            # Ya estaba escrito en una entrega anterior que no confirmó la proyección: imposible
            # en la misma transacción, así que el expediente y la proyección divergen.
            raise LateSegmentRace(str(item.record_id))
        return written

    # --- eventos por zona -----------------------------------------------------------------------

    async def _gate_state_changed(
        self, event: OutboxEvent, transaction: Transaction, plant_id: uuid.UUID
    ) -> int:
        payload = event.payload
        if payload["gate"] != GateKind.USAGE.value:
            return 0
        revoked = payload["status"] == GateStatus.REVOKED.value
        since = event.created_at

        def change(row: FindingStateRow) -> FindingStateRow:
            if not revoked:
                return replace(row, zone_agreement_revoked=False, zone_agreement_revoked_at=None)
            if row.zone_agreement_revoked:
                return row
            return replace(row, zone_agreement_revoked=True, zone_agreement_revoked_at=since)

        return await self._store.update_enrichment(
            transaction,
            event_id=event.event_id,
            at=event.created_at,
            plant_ids=[plant_id],
            zone_ids=[_uuid(payload["zone_id"])],
            change=change,
        )

    async def _zones(
        self,
        event: OutboxEvent,
        transaction: Transaction,
        plant_id: uuid.UUID,
        *,
        occupancy: bool = True,
        regression: bool = True,
    ) -> int:
        payload = event.payload
        zones = (
            [_uuid(zone) for zone in payload["zone_ids"]]
            if "zone_ids" in payload
            else [_uuid(payload["zone_id"])]
        )
        if not zones:
            return 0
        occupancies: Mapping[uuid.UUID, ZoneOccupancy] | None = None
        if occupancy:
            window = await self._enrichment.default_window(transaction, plant_id)
            occupancies = await self._enrichment.occupancy(transaction.context, zones, window)
        histories: Mapping[uuid.UUID, tuple[RegressionMark, ...]] | None = None
        if regression:
            histories = await self._enrichment.regression_history(transaction, plant_id, zones)
        return await self._store.update_enrichment(
            transaction,
            event_id=event.event_id,
            at=event.created_at,
            plant_ids=[plant_id],
            zone_ids=zones,
            change=_ZoneRefresh(occupancies, histories),
        )


def register_loop_projection(
    registry: ConsumerRegistry, consumer: LoopProjectionConsumer
) -> Consumer:
    """Registra ``loop_projection`` (U-04, sin dependencia externa: los puertos son en proceso)."""
    return registry.register(
        Consumer(
            consumer_name=LOOP_PROJECTION,
            unit=ActorUnit.U04,
            subscribed_events=LOOP_PROJECTION_EVENTS,
            handler=consumer,
        )
    )
