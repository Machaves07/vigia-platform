"""``fleet.heartbeat``: el latido del nodo y su respuesta (LC-GOB-14; S-PLA-08; BR-GOB-70 a 82).

La ruta ``POST heartbeats`` (``node_api.routes.heartbeats``) llega aquí con la verificación previa
ya hecha **fuera de la transacción** (versión, certificado y alcance, tamaño y esquema con el modelo
de U-01; PAT-GOB-REN-03) y el contexto de nodo de A-51. ``accept``:

1. comprueba que organización, planta y nodo del cuerpo son los del certificado
   (``node_zone_mismatch``; las zonas que el nodo ya no tiene asignadas **no** rechazan: se
   ignoran, BR-GOB-70) y toma el conjunto de claves de la caché de ``SigningService``
   (NFR-NUC-36): nunca firma, salvo el sobre de compuertas inicial (A-60, VIG-180) de una zona
   asignada anterior a él que aún no tiene ninguno (``ensure_initial_envelopes``: una lectura y,
   solo para esa zona, una firma una sola vez, en su propia transacción **antes** de la del latido,
   así que no se anida con sus candados); con la firma caída la zona sigue omitida;
2. abre **una transacción corta** (BR-GOB-72, 73):

   - candado de la fila del nodo en ``NodeInventory`` (``SELECT … FOR UPDATE``; el primer latido
     la inserta con ``ON CONFLICT DO NOTHING``) y, con él, la búsqueda del duplicado en
     ``HeartbeatHistory`` (BR-GOB-71, NFR-GOB-47): un ``heartbeat_id`` ya aceptado no escribe nada
     y recibe la respuesta compuesta del estado vigente;
   - la ficha de flota del nodo (``FOR UPDATE``): ``revoked`` se lee aquí (BR-GOB-66);
   - ``NodeInventory`` (``last_heartbeat_at`` nunca retrocede), ``CameraInventory``,
     ``ZoneNodeState`` con ``coverage_ok`` (BR-GOB-97) y el ``INSERT`` en ``HeartbeatHistory``;
   - ``live_view_local_url`` (nº 31) a ``NodeFleetRecord`` y a U-02 por
     ``IdentityCommandPort.update_node`` con la misma transacción, sin cambiar el estado, solo si
     cambió (su ausencia la deja nula);
   - un cambio de ``model_version`` marca la regresión de las zonas del nodo
     (``mark_model_version_change``, BR-GOB-51);
   - si el nodo no estaba ``reachable`` (``unknown`` o ``mute``), el registro
     ``node_communication_state_changed`` con ``reachable``: la **única** escritura propia del
     latido en el expediente (BR-GOB-73). Un nodo revocado o dado de baja no genera transiciones
     ni marcas (BR-GOB-76);
   - la respuesta se compone **dentro** de la transacción, con lo que acaba de leer: si no se puede
     componer (ninguna zona asignada tiene ya catálogo y sobre de compuertas), nada se confirma y la
     ruta responde ``temporarily_unavailable``;
3. tras confirmar, **fuera** de la transacción (PAT-GOB-REN-03; decisión del redactor, declarada en
   el PR): los accesos locales a la vista en vivo (nº 24; A-02, A-30) a
   ``LiveViewTokenService.incorporate``, idempotente por ``access_id`` (también en un duplicado:
   así un latido repetido tras un fallo los entrega una sola vez), y la renovación de los sobres de
   compuertas a los que les quedan menos de 24 h (A-55: ``renew_gate_envelope`` con el umbral
   vuelto a comprobar con la exclusión de la zona, así que se firma a lo sumo una vez por zona y
   renovación aunque lleguen latidos concurrentes). Con la firma caída se sirve el sobre guardado.

**Respuesta** (BR-GOB-82; ``HeartbeatResponse`` de U-01 §2.5): hora del servidor; ``gate_states``
con el texto **guardado** de cada ``SignedEnvelope<GateState>`` (insertado sin volver a
serializarlo); ``catalog_versions_available``; ``target_software_version`` de la última
publicación que alcanza al nodo (sin ventana, D-5); ``contract_notice`` (A-44);
``platform_public_keys`` de la caché; ``revoked``; ``heartbeat_interval_seconds`` de
``NodeConfiguration`` y ``mute_after_seconds``, cinco veces el intervalo (nº 35, A-04). Se valida
con el modelo estricto de U-01 antes de enviarla.

**Orden de los candados** del latido, el mismo en toda operación que comparte alguno (encaja con
el de ``fleet.application.common``: ficha → identidad → cadena; y con el de la regresión:
regresión → cadena):

1. la fila del nodo en ``fleet.node_inventory``;
2. la ficha de flota del nodo (``fleet.node_fleet_record``);
3. la fila de ``identity.node_identity`` (``update_node``, solo si cambió la URL);
4. las filas de regresión de las zonas del nodo (``mark_model_version_change``);
5. la cadena de la planta (el primer registro: la marca de regresión o la transición).

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import enum
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from vigia_contracts.models.enumerations import CompatibilityResult
from vigia_contracts.models.heartbeat import Heartbeat, LiveViewAccess
from vigia_contracts.models.heartbeat_response import HeartbeatResponse

from vigia_platform.catalog.application.gates import GateUnavailable
from vigia_platform.fleet.adapters.postgres.heartbeat_history_store import (
    PostgresHeartbeatHistoryStore,
)
from vigia_platform.fleet.adapters.postgres.inventory_projection import (
    PostgresInventoryProjection,
)
from vigia_platform.fleet.adapters.postgres.node_catalog_store import (
    PostgresNodeCatalogStore,
    StoredGateEnvelope,
    ZoneSnapshot,
)
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.application.common import FleetWriteFailed
from vigia_platform.fleet.application.node_declaration import COMMUNICATION_RECORD_TYPE
from vigia_platform.fleet.domain.communication_state import mute_after_seconds
from vigia_platform.fleet.domain.heartbeat import (
    GATE_RENEWAL_MARGIN,
    HISTORY_RETENTION,
    ContractNoticeState,
    HeartbeatProjection,
    PreviousInventory,
    project,
)
from vigia_platform.fleet.domain.node_fleet_record import FleetNode
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    RecordScope,
)
from vigia_platform.ledger.domain.coverage import CommunicationState
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.signing import SigningNotReady
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "COMMUNICATION_RECORD_TYPE",
    "HeartbeatDependencies",
    "HeartbeatReply",
    "HeartbeatResult",
    "HeartbeatService",
    "HeartbeatUnavailable",
    "NodeScopeMismatch",
]


_log = get_logger("fleet.heartbeat")


# --- Puertos y errores ---------------------------------------------------------------------------


class NodeScopeMismatch(Exception):
    """Organización, planta, nodo o zona fuera del alcance del certificado (node_zone_mismatch)."""

    def __init__(self, field: str) -> None:
        super().__init__("fuera del alcance del nodo")
        self.field = field


class HeartbeatUnavailable(Exception):
    """La respuesta no se puede componer ahora (sin claves publicadas, o ninguna zona asignada con
    catálogo y sobre de compuertas): ``temporarily_unavailable``, nada escrito."""


class KeySetSource(Protocol):
    """La caché del conjunto de claves de ``SigningService`` (NFR-NUC-36)."""

    def current_key_set_envelope(self) -> Any: ...


class GateRenewal(Protocol):
    """``GateService.renew_gate_envelope`` (A-55) y ``ensure_initial_envelopes`` (A-60)."""

    async def renew_gate_envelope(
        self, context: ScopeContext, zone_id: uuid.UUID, *, expiring_before: datetime | None = None
    ) -> Mapping[str, Any] | None: ...

    async def ensure_initial_envelopes(
        self, context: ScopeContext, zone_ids: Iterable[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]: ...


class ModelRegressionMarker(Protocol):
    """``RegressionService.mark_model_version_change`` (BR-GOB-51)."""

    async def mark_model_version_change(
        self,
        context: ScopeContext,
        zone_ids: Iterable[uuid.UUID],
        model_version: str,
        *,
        transaction: Transaction | None = None,
        node: NodeScope | None = None,
    ) -> Any: ...


class NodeUrlPort(Protocol):
    """``IdentityCommandPort.update_node`` de U-02 (la URL local, sin cambiar el estado)."""

    async def update_node(
        self,
        context: ScopeContext,
        node_id: uuid.UUID,
        status: Any,
        live_view_local_url: str | None,
        *,
        transaction: Transaction | None = None,
    ) -> Any: ...


class LocalAccessPort(Protocol):
    """``LiveViewTokenService.incorporate`` de U-02 (idempotente por ``access_id``)."""

    async def incorporate(
        self, context: ScopeContext, node_id: uuid.UUID, records: Iterable[object]
    ) -> Any: ...


class HeartbeatResult(enum.StrEnum):
    """Atributo ``result`` de ``fleet_heartbeats_total``."""

    ACCEPTED = "accepted"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True, kw_only=True)
class HeartbeatDependencies:
    """Lo que recibe el servicio del latido (lo construye la raíz de composición)."""

    database: LedgerDatabase
    writer: EscritorExpediente
    clock: Clock
    key_sets: KeySetSource
    gates: GateRenewal
    regression: ModelRegressionMarker
    identity: NodeUrlPort
    live_view: LocalAccessPort
    retires_at: Callable[[str], str | None]
    """La fecha de retiro anunciada para la menor de una versión del contrato (la política de
    versiones de ``node_api``), o ``None``."""
    nodes: PostgresNodeFleetStore
    inventory: PostgresInventoryProjection = field(default_factory=PostgresInventoryProjection)
    history: PostgresHeartbeatHistoryStore = field(default_factory=PostgresHeartbeatHistoryStore)
    catalog: PostgresNodeCatalogStore = field(default_factory=PostgresNodeCatalogStore)
    metrics: PlatformMetrics | None = None

    def platform_metrics(self) -> PlatformMetrics:
        return self.metrics if self.metrics is not None else get_metrics()


@dataclass(frozen=True, slots=True)
class HeartbeatReply:
    """La respuesta serializada (con los sobres guardados tal cual) y si fue un duplicado."""

    content: bytes
    duplicate: bool


@dataclass(frozen=True, slots=True)
class _Composed:
    """Lo que la respuesta necesita, leído en la transacción del latido."""

    zones: tuple[uuid.UUID, ...]
    catalog_versions: Mapping[uuid.UUID, int]
    gates: dict[uuid.UUID, StoredGateEnvelope]
    target_version: str | None
    interval: int
    revoked: bool


@dataclass(frozen=True, slots=True)
class _Outcome:
    composed: _Composed
    duplicate: bool
    projection: HeartbeatProjection | None
    previous: PreviousInventory | None


# --- Servicio ------------------------------------------------------------------------------------


def _uuid(value: Any) -> uuid.UUID:
    return value if type(value) is uuid.UUID else uuid.UUID(str(value))


def _retired(node: FleetNode) -> bool:
    return node.status == "revoked" or node.record.revoked or node.record.decommissioned


class HeartbeatService:
    """``fleet.heartbeat`` (LC-GOB-14)."""

    def __init__(self, deps: HeartbeatDependencies) -> None:
        self._deps = deps

    def __repr__(self) -> str:
        return "HeartbeatService()"

    async def accept(
        self, node: NodeScope, heartbeat: Heartbeat, compatibility: CompatibilityResult
    ) -> HeartbeatReply:
        """Acepta ``heartbeat`` de ``node`` (o lo ignora si es duplicado) y compone la respuesta."""
        if not isinstance(node, NodeScope) or not isinstance(heartbeat, Heartbeat):
            raise TypeError("accept recibe el alcance del nodo y el latido del contrato")
        _check_scope(node, heartbeat)
        deps = self._deps
        keys = self._key_set()
        notice = ContractNoticeState(
            CompatibilityResult(compatibility),
            deps.retires_at(heartbeat.contract_version)
            if compatibility is CompatibilityResult.ACCEPTED_WITH_NOTICE
            else None,
        )
        await self._initial_gates(node)
        async with deps.database.transaction(node.context) as transaction:
            outcome = await self._within(transaction, node, heartbeat, notice)
            # Compuesta y validada dentro: si no se puede, la transacción se revierte entera.
            content = self._compose(outcome.composed, notice, heartbeat, keys)
        if heartbeat.live_view_accesses:
            await deps.live_view.incorporate(
                node.context, node.node_id, _accesses(heartbeat.live_view_accesses)
            )
        renewed = await self._renew_expiring(node, outcome.composed)
        if renewed is not None:
            content = self._compose(renewed, notice, heartbeat, keys)
        self._measure(node, heartbeat, outcome)
        return HeartbeatReply(content=content, duplicate=outcome.duplicate)

    # --- Transacción ---------------------------------------------------------------------------

    async def _within(
        self,
        transaction: Transaction,
        node: NodeScope,
        heartbeat: Heartbeat,
        notice: ContractNoticeState,
    ) -> _Outcome:
        deps = self._deps
        node_id = node.node_id
        # (1) La fila del nodo: serializa los latidos del mismo nodo.
        previous = await deps.inventory.lock(transaction, node_id)
        snapshot = await deps.catalog.zone_snapshot(transaction, node.zone_ids)
        received_at = to_millisecond(deps.clock.now())
        fresh = False
        if previous is None:
            draft = self._project(node, heartbeat, received_at, None, snapshot, notice, False)
            fresh = await deps.inventory.insert_first(transaction, draft.inventory)
            if not fresh:
                # Otro primer latido confirmó antes: su fila ya existe y queda bloqueada aquí.
                previous = await deps.inventory.lock(transaction, node_id)
        # Con la fila del nodo bloqueada: ¿ya se aceptó este ``heartbeat_id``? (BR-GOB-71)
        duplicate = not fresh and await deps.history.seen(
            transaction,
            plant_id=node.plant_id,
            node_id=node_id,
            heartbeat_id=_uuid(heartbeat.heartbeat_id),
            since=received_at - HISTORY_RETENTION,
        )
        if duplicate:
            # Nada se escribe: la respuesta del estado vigente, sin bloquear la ficha.
            fleet = await deps.nodes.read(transaction, node_id)
            if fleet is None:
                raise HeartbeatUnavailable("el nodo no tiene ficha de flota")
            composed = await self._read_response(transaction, node, snapshot, _retired(fleet))
            return _Outcome(composed, duplicate=True, projection=None, previous=previous)
        # (2) La ficha de flota: ``revoked`` leído en la transacción.
        fleet = await deps.nodes.lock(transaction, node_id)
        if fleet is None:
            raise HeartbeatUnavailable("el nodo no tiene ficha de flota")
        retired = _retired(fleet)
        composed = await self._read_response(transaction, node, snapshot, retired)
        projection = self._project(
            node, heartbeat, received_at, previous, snapshot, notice, retired
        )
        if not fresh:
            await deps.inventory.update(transaction, projection.inventory)
        await deps.inventory.save_cameras(transaction, projection.inventory, projection.cameras)
        await deps.inventory.save_zones(transaction, projection.inventory, projection.zones)
        await deps.history.append(
            transaction, plant_id=node.plant_id, node_id=node_id, row=projection.history
        )
        if not retired:
            await self._live_view_url(transaction, node, fleet, heartbeat.live_view_local_url)
        if projection.model_changed and node.zone_ids:
            # (4) Regresión → (5) cadena: antes de cualquier otro registro (orden del módulo).
            await deps.regression.mark_model_version_change(
                node.context,
                sorted(node.zone_ids, key=str),
                heartbeat.model_version,
                transaction=transaction,
                node=node,
            )
        if projection.reachable_transition:
            await self._write_reachable(transaction, node, projection)
        return _Outcome(composed, duplicate=False, projection=projection, previous=previous)

    def _project(
        self,
        node: NodeScope,
        heartbeat: Heartbeat,
        received_at: datetime,
        previous: PreviousInventory | None,
        snapshot: ZoneSnapshot,
        notice: ContractNoticeState,
        retired: bool,
    ) -> HeartbeatProjection:
        return project(
            heartbeat,
            organization_id=node.organization_id,
            plant_id=node.plant_id,
            received_at=received_at,
            previous=previous,
            assigned_zones=node.zone_ids,
            catalogs=snapshot.coverage,
            notice=notice,
            retired=retired,
        )

    async def _read_response(
        self, transaction: Transaction, node: NodeScope, snapshot: ZoneSnapshot, retired: bool
    ) -> _Composed:
        deps = self._deps
        zones = tuple(
            sorted(
                (
                    zone
                    for zone in node.zone_ids
                    if zone in snapshot.gates and zone in snapshot.catalog_versions
                ),
                key=str,
            )
        )
        if not zones:
            # El contrato exige al menos una zona con sobre y versión: el nodo reintenta y sigue
            # con su caché de 7 días (TASK-219 decide igual en el alta).
            raise HeartbeatUnavailable("ninguna zona asignada tiene catálogo y compuertas")
        return _Composed(
            zones=zones,
            catalog_versions={zone: snapshot.catalog_versions[zone] for zone in zones},
            gates={zone: snapshot.gates[zone] for zone in zones},
            target_version=await deps.inventory.target_version(
                transaction, node.plant_id, node.node_id
            ),
            interval=await deps.inventory.heartbeat_interval(transaction, node.node_id),
            revoked=retired,
        )

    async def _live_view_url(
        self, transaction: Transaction, node: NodeScope, fleet: FleetNode, url: str | None
    ) -> None:
        """``live_view_local_url`` a la ficha y a U-02 (misma transacción), solo si cambió."""
        if fleet.record.live_view_local_url != url:
            await self._deps.nodes.set_live_view_local_url(transaction, node.node_id, url)
        if fleet.live_view_local_url != url:
            # (3) La identidad: sin cambiar el estado (el que ya tiene).
            await self._deps.identity.update_node(
                node.context, node.node_id, fleet.status, url, transaction=transaction
            )

    async def _write_reachable(
        self, transaction: Transaction, node: NodeScope, projection: HeartbeatProjection
    ) -> None:
        """``node_communication_state_changed`` con ``reachable`` (BR-GOB-73), en la cadena de la
        planta del nodo y dentro de la transacción del latido."""
        inventory = projection.inventory
        written = await self._deps.writer.write(
            node.context,
            COMMUNICATION_RECORD_TYPE,
            {
                "node_id": str(node.node_id),
                "state": CommunicationState.REACHABLE.value,
                "since": format_timestamp(projection.history.received_at),
                "last_heartbeat_at": format_timestamp(inventory.last_heartbeat_at),
            },
            scope=RecordScope(plant_id=node.plant_id),
            occurred_at=projection.history.received_at,
            transaction=transaction,
        )
        if isinstance(written, LedgerRejection):
            raise FleetWriteFailed(written)

    # --- Respuesta -----------------------------------------------------------------------------

    def _key_set(self) -> Any:
        try:
            keys = self._deps.key_sets.current_key_set_envelope()
        except SigningNotReady:
            raise HeartbeatUnavailable("el conjunto de claves no está cargado") from None
        if keys is None:
            raise HeartbeatUnavailable("no hay conjunto de claves publicado")
        return keys

    def _compose(
        self,
        composed: _Composed,
        notice: ContractNoticeState,
        heartbeat: Heartbeat,
        keys: Any,
    ) -> bytes:
        """``HeartbeatResponse`` validada con el modelo estricto, con los sobres de compuertas
        insertados **como se guardaron** (sin volver a serializarlos ni firmarlos)."""
        notice_document: dict[str, Any] = {
            "result": notice.result.value,
            "message_es": notice.message_es(heartbeat.contract_version),
        }
        if notice.retires_at is not None:
            notice_document["retires_at"] = notice.retires_at
        document: dict[str, Any] = {
            "server_time": format_timestamp(self._deps.clock.now()),
            "catalog_versions_available": [
                {"zone_id": str(zone), "version": composed.catalog_versions[zone]}
                for zone in composed.zones
            ],
            "contract_notice": notice_document,
            "platform_public_keys": keys.to_json_value(),
            "revoked": composed.revoked,
            "heartbeat_interval_seconds": composed.interval,
            "mute_after_seconds": mute_after_seconds(composed.interval),
        }
        if composed.target_version is not None:
            document["target_software_version"] = composed.target_version
        stored = [composed.gates[zone].text for zone in composed.zones]
        head = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        content = (head[:-1] + ',"gate_states":[' + ",".join(stored) + "]}").encode("utf-8")
        # Se valida exactamente lo que se envía, con el lector estricto de U-01 (fallo cerrado).
        HeartbeatResponse.model_validate_json(content)
        return content

    async def _initial_gates(self, node: NodeScope) -> None:
        """A-60: la zona asignada anterior al sobre inicial lo recibe antes de la transacción del
        latido (una lectura; firma solo la zona que no tiene ninguno, una vez)."""
        if not node.zone_ids:
            return
        try:
            await self._deps.gates.ensure_initial_envelopes(node.context, node.zone_ids)
        except GateUnavailable:
            # Firma caída: la zona sigue sin sobre y se omite, como antes; el nodo no opera en ella.
            _log.warning("sobre inicial de compuertas aplazado: firma no disponible")

    async def _renew_expiring(self, node: NodeScope, composed: _Composed) -> _Composed | None:
        """A-55: renueva los sobres que vencen en menos de 24 h; ``None`` si no hubo ninguno."""
        deps = self._deps
        threshold = deps.clock.now() + GATE_RENEWAL_MARGIN
        expiring = [zone for zone in composed.zones if composed.gates[zone].valid_until < threshold]
        if not expiring:
            return None
        for zone in expiring:
            try:
                await deps.gates.renew_gate_envelope(node.context, zone, expiring_before=threshold)
            except GateUnavailable:
                # Firma caída (FS-GOB-03): se sirve el sobre guardado; el nodo sigue con su caché.
                _log.warning("renovación del sobre de compuertas aplazada: firma no disponible")
        async with deps.database.transaction(node.context) as transaction:
            fresh = await deps.catalog.gate_envelopes(transaction, expiring)
        gates = dict(composed.gates)
        gates.update({zone: envelope for zone, envelope in fresh.items() if zone in gates})
        return _Composed(
            zones=composed.zones,
            catalog_versions=composed.catalog_versions,
            gates=gates,
            target_version=composed.target_version,
            interval=composed.interval,
            revoked=composed.revoked,
        )

    # --- Métricas ------------------------------------------------------------------------------

    def _measure(self, node: NodeScope, heartbeat: Heartbeat, outcome: _Outcome) -> None:
        """NFR-GOB-55 por nodo, solo contadores y medidores (NFR-GOB-13): sin zona ni texto."""
        metrics = self._deps.platform_metrics()
        attributes = {"node_id": node.node_id}
        if outcome.duplicate or outcome.projection is None:
            metrics.fleet_heartbeats_total.add(1, {**attributes, "result": HeartbeatResult.IGNORED})
            return
        metrics.fleet_heartbeats_total.add(1, {**attributes, "result": HeartbeatResult.ACCEPTED})
        previous = outcome.previous
        if previous is not None and previous.last_heartbeat_at is not None:
            gap = outcome.projection.history.received_at - previous.last_heartbeat_at
            metrics.fleet_heartbeat_gap_seconds.set(max(0.0, gap.total_seconds()), attributes)
        metrics.fleet_node_reachable.set(0 if outcome.composed.revoked else 1, attributes)
        metrics.fleet_node_queue_pending.set(heartbeat.local_queue.pending, attributes)
        metrics.fleet_node_clock_offset_ms.set(heartbeat.node_clock.offset_ms, attributes)


def _check_scope(node: NodeScope, heartbeat: Heartbeat) -> None:
    """Organización, planta y nodo del cuerpo son los del certificado (BR-CTR-03, BR-GOB-70)."""
    for name, expected in (
        ("organization_id", node.organization_id),
        ("plant_id", node.plant_id),
        ("node_id", node.node_id),
    ):
        if _uuid(getattr(heartbeat, name)) != expected:
            raise NodeScopeMismatch(name)


def _accesses(accesses: Sequence[LiveViewAccess]) -> list[LiveViewAccess]:
    return list(accesses)
