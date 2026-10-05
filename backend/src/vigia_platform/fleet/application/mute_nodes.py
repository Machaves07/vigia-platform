"""``detect_mute_nodes``: el nodo sin latido pasa a ``mute`` y levanta ``node_mute`` (TASK-225).

BR-GOB-74 y 76; BL §2.6 (pseudocódigo y nota de cadencias); D-9; A-04 y A-09; PR-GOB-08;
NFR-GOB-08, 47. Cada 60 s, por organización (``register_detect_mute_nodes``; el registro en la raíz
es de TASK-227), por lotes de ``batch_size`` nodos en orden de (planta, nodo):

1. los nodos ``enrolled``, no revocados ni dados de baja, con ``now - last_heartbeat_at`` **mayor
   que** cinco veces su ``heartbeat_interval_seconds`` efectivo (60 s sin configuración) y no
   ``mute`` — o ya ``mute`` sin su alarma abierta —, con la fila de ``node_inventory``
   **bloqueada** y la ficha de flota compartida: la condición se vuelve a comprobar sobre la fila
   bloqueada, así que un latido que llega a la vez (VIG-157 escribe ``reachable`` con esa misma
   fila bloqueada) o una revocación que confirma antes dejan fuera al nodo y nunca quedan pisados;
2. ``communication_state = mute`` en el inventario y, por cada transición, el registro
   ``node_communication_state_changed {state: mute, since: last_heartbeat_at,
   last_heartbeat_at}`` en la cadena de la planta: el silencio **empieza en el último latido
   aceptado**, nunca en el instante del barrido; un nodo que nunca latió sigue ``unknown``;
3. ``node_mute`` por transición (en la primera evaluación, sin histéresis), con ``since =
   last_heartbeat_at``, salvo que ya esté abierta. La baja la hace ``evaluate_fleet_alarms`` cuando
   el nodo vuelve a ``reachable``.

Dos ejecuciones solapadas se serializan en el candado de cada fila del inventario: la segunda la
encuentra ya ``mute`` (con su alarma) y no escribe nada. **Orden de los candados**, el del latido
(``fleet.application.heartbeat``) para lo que comparten: (1) las filas de ``node_inventory``, por
(planta, nodo); (2) las fichas de flota de esos nodos (``FOR SHARE``); (3) la cadena de cada planta,
en orden de planta; (4) las ranuras de ``open_fleet_alarm`` de las alarmas que abre.

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import uuid
from typing import Final

from vigia_platform.fleet.application.common import FleetWriteFailed
from vigia_platform.fleet.application.fleet_alarms import (
    BATCH_SIZE,
    AlarmDependencies,
    AlarmReport,
    now_of,
    raise_in,
)
from vigia_platform.fleet.application.node_declaration import COMMUNICATION_RECORD_TYPE
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_alarm import NewAlarm
from vigia_platform.fleet.domain.mute_detection import mute_transition
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    RecordScope,
)
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule

__all__ = [
    "DETECT_MUTE_NODES",
    "DETECT_MUTE_NODES_SCHEDULE",
    "MuteDetector",
    "register_detect_mute_nodes",
]

DETECT_MUTE_NODES: Final = "detect_mute_nodes"
DETECT_MUTE_NODES_SCHEDULE: Final = Schedule.every(60)
"""Cada 60 s (BR-GOB-74; nota de cadencias de BL §2.6)."""


@repository
class MuteDetector:
    """El manejador de ``detect_mute_nodes`` para la transacción de una organización."""

    def __init__(
        self,
        deps: AlarmDependencies,
        *,
        writer: EscritorExpediente,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._deps = deps
        self._writer = writer
        self._batch_size = batch_size

    def __repr__(self) -> str:
        return "MuteDetector()"

    async def detect(self, transaction: Transaction) -> AlarmReport:
        """Una pasada sobre los nodos de la organización de ``transaction``."""
        deps = self._deps
        now = now_of(deps.clock)
        report = AlarmReport()
        after: tuple[uuid.UUID, uuid.UUID] | None = None
        while True:
            # (1) y (2): las filas del inventario y las fichas, bloqueadas en orden.
            candidates = await deps.store.lock_mute_candidates(
                transaction, now=now, after=after, limit=self._batch_size
            )
            if candidates:
                transitioning = [node for node in candidates if node.transitions]
                await deps.store.mark_mute(transaction, [node.node_id for node in transitioning])
                for node in transitioning:  # (3) en orden de planta
                    written = await self._writer.write(
                        transaction.context,
                        COMMUNICATION_RECORD_TYPE,
                        mute_transition(node.node_id, node.last_heartbeat_at),
                        scope=RecordScope(plant_id=node.plant_id),
                        occurred_at=now,
                        transaction=transaction,
                    )
                    if isinstance(written, LedgerRejection):
                        raise FleetWriteFailed(written)
                    report.transitions.append(node.node_id)
                opened = await deps.store.open_alarms(
                    transaction, [node.node_id for node in candidates]
                )
                report.raised += await raise_in(
                    deps,
                    transaction,
                    [
                        NewAlarm(
                            alarm_kind=FleetAlarmKind.NODE_MUTE,
                            plant_id=node.plant_id,
                            node_id=node.node_id,
                            since=node.last_heartbeat_at,
                        )
                        for node in candidates
                        if (node.node_id, FleetAlarmKind.NODE_MUTE) not in opened
                    ],
                    now,
                )
            if len(candidates) < self._batch_size:
                return report
            last = candidates[-1]
            after = (last.plant_id, last.node_id)


def register_detect_mute_nodes(
    registry: PeriodicTaskRegistry, detector: MuteDetector
) -> PeriodicTask:
    """Registra ``detect_mute_nodes`` cada 60 s, por organización (lo llama TASK-227)."""

    async def handler(transaction: Transaction) -> None:
        await detector.detect(transaction)

    return registry.register(
        DETECT_MUTE_NODES, DETECT_MUTE_NODES_SCHEDULE, handler, unit=ActorUnit.U03
    )
