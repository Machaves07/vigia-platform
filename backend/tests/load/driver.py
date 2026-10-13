"""Proceso de los nodos simulados de un perfil de carga (LC-GOB-22; LC-14 de U-01; TASK-231).

``python -m tests.load.driver --plan plan.json --result result.json``: una tarea ``asyncio`` por
nodo de la flota ya dada de alta (``fleet.json`` de ``tests.load.provision``), cada una con el
**cliente real del contrato** de U-01: su credencial (``FileCredentialStore``), su bandeja SQLite
(``Outbox``), su ``AsyncClient`` con reintentos, cortacircuitos y claves de idempotencia, y su
constructor de registros (``RecordBuilder``) sobre los catálogos publicados en la plataforma. El
plan de cada nodo sale de ``plan_day`` del nodo simulado con la semilla registrada (un día por
cada 24 horas simuladas del perfil, cada uno con su semilla derivada), recortado a la duración
del perfil; los clips son los sintéticos del kit (``ClipCache``) sobre el conjunto sellado
sintético. Corre en un proceso aparte de la plataforma para no competir con ella por el GIL.

Lo que este proceso añade al nodo simulado, sin tocar su cliente:

- **Indisponibilidad** (``Reachability``): en las fases ``unreachable`` del perfil, toda petición
  a la plataforma o al almacén falla con ``httpx.ConnectError`` antes de salir, como un
  balanceador que no responde. Los nodos encolan; el cortacircuitos se abre y solo late. Al
  volver, el primer latido aceptado lo cierra y la cola se vacía (reconexión masiva).
- **Respuestas perdidas** (``LostReplies``, PR-GOB-01 bajo carga): con la fracción del perfil,
  decidida por la semilla y el resumen del cuerpo, la respuesta a un registro **ya aceptado** se
  descarta y el cliente ve ``httpx.ReadTimeout``: lo reenvía con la misma clave y la plataforma
  responde ``accepted_duplicate``. Cada cuerpo se descarta como mucho una vez.
- **Latido** cada ``max(intervalo / speed_factor, 15 s)`` reales: acelerado como el resto, pero
  sin pasar de los 4 latidos por minuto y nodo de la plataforma (``node_api.limits``), así que un
  ``rate_limited`` en el latido también es un defecto. Como el nodo real, un latido vencido no se
  recupera: si el nodo estuvo ocupado (un vaciado largo tras la reconexión) o su cliente ya latió
  por su cuenta, el latido del calendario que saldría a menos de un intervalo del anterior se omite
  (``heartbeats_skipped``) en lugar de salir en ráfaga.
- **Vaciado final** (VIG-192): tras las fases, cada nodo sigue hasta vaciar su bandeja durante
  ``flush_seconds``; si a esa hora el cliente aún tiene un reintento programado
  (``next_attempt_at``, con el retroceso de hasta 5 minutos de PAT-RES-02), el tope se alarga hasta
  ese reintento más dos ``heartbeat_seconds``, sin pasar de ``FLUSH_EXTENSION_SECONDS`` más. Lo que
  quede sin enviar es pérdida y sale en ``unsent`` con su recorrido (sin identificadores).
- **Mediciones**: latencia por ruta del contrato (sin la URL de las subidas, que es prefirmada),
  eventos del cliente por operación, resultado y ``rejection_code``, instante de emisión y de
  aceptación de cada registro, recibos ``accepted_duplicate`` de la bandeja.

El resultado (JSON) no lleva credenciales, códigos de alta, cuerpos ni URL prefirmadas.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import ssl
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
from vigia_contracts.client import AsyncClient, ClientConfig, ClientEvent
from vigia_contracts.client.core import DEFAULT_CAP_MS, Wait
from vigia_contracts.clock import SystemClock
from vigia_contracts.conformance.simulated_node.clips import ClipCache, SyntheticClip
from vigia_contracts.conformance.simulated_node.dataset import load_sealed_dataset
from vigia_contracts.conformance.simulated_node.journal import Journal
from vigia_contracts.conformance.simulated_node.records import (
    NodeIdentityIds,
    RecordBuilder,
    format_timestamp,
)
from vigia_contracts.conformance.simulated_node.runner import (
    SimulatedNode,
    deterministic_bytes,
    node_report,
)
from vigia_contracts.conformance.simulated_node.schedule import (
    DAY_MS,
    DEFAULT_GROUPING_WINDOW_MS,
    PlannedDegradation,
    PlannedEpisode,
    derive_seed,
    duration_distribution,
    plan_day,
    resolve_hourly_weights,
)
from vigia_contracts.credentials import FileCredentialStore, NodeCredentials, NodeIdentity
from vigia_contracts.models.tolerant.node_enrollment import NodeInitialConfiguration
from vigia_contracts.outbox import Outbox, OutboxItem, OutboxState

from tests.load.profiles import PROFILES, LoadProfile, simulation_profile
from tests.load.report import latency_summary

__all__ = [
    "FLUSH_EXTENSION_SECONDS",
    "HEARTBEAT_FLOOR_SECONDS",
    "HEARTBEAT_SLACK_SECONDS",
    "RECORD_ROUTES",
    "drive",
    "main",
    "plan_items",
    "route_label",
]

HEARTBEAT_FLOOR_SECONDS: Final = 15.0
"""Intervalo real mínimo entre latidos: 4 por minuto y nodo (``node_api.limits``)."""
HEARTBEAT_SLACK_SECONDS: Final = 1.0
"""Retraso del latido anterior que se tolera sin omitir el siguiente del calendario: el bucle de
eventos lo saca unos milisegundos tarde; más de esto es un nodo ocupado y el siguiente se omite."""
RECORD_ROUTES: Final = (
    "/api/nodes/findings",
    "/api/nodes/detection-reviews",
    "/api/nodes/observability-events",
)
HEARTBEAT_PATH: Final = "/heartbeats"
"""Final de la ruta del latido (``/api/nodes/heartbeats``)."""
LIVE_VIEW_HOST: Final = "10.0.0.5"
SOFTWARE_VERSION: Final = "1.0.0"
UPLOAD_LABEL: Final = "PUT almacén (URL prefirmada)"
FLUSH_EXTENSION_SECONDS: Final = 2 * DEFAULT_CAP_MS / 1000
"""Lo más que se alarga el vaciado final por reintentos programados: dos esperas en el tope del
retroceso del cliente (300 s, PAT-RES-02), que no decrece mientras un registro siga fallando."""
HISTORY_EVENTS: Final = 16
"""Eventos del cliente que se guardan por registro sin aceptar (para ``unsent``)."""
_MAX_DRAIN_ROUNDS: Final = 100_000
_UUID_LENGTH: Final = 36

PlannedItem = PlannedEpisode | PlannedDegradation


def route_label(request: httpx.Request, *, upload: bool) -> str:
    """``MÉTODO /ruta`` con los identificadores como plantilla; una subida al almacén nunca lleva
    su URL (es prefirmada, PR-GOB-31)."""
    if upload:
        return UPLOAD_LABEL
    parts = request.url.path.split("/")
    names = {"zones": "{zone_id}", "clip-uploads": "{clip_id}"}
    for position in range(1, len(parts)):
        if len(parts[position]) == _UUID_LENGTH and parts[position - 1] in names:
            parts[position] = names[parts[position - 1]]
    return f"{request.method} {'/'.join(parts)}"


# --- Transporte -----------------------------------------------------------------------------------


@dataclass
class Reachability:
    """Ventanas reales en que la plataforma no responde (fases ``unreachable``)."""

    clock: SystemClock
    windows: list[tuple[datetime, datetime]] = field(default_factory=list)

    def down(self) -> bool:
        now = self.clock.now()
        return any(start <= now < end for start, end in self.windows)


@dataclass
class LostReplies:
    """Pierde la respuesta a un registro aceptado con la fracción dada, una vez por cuerpo."""

    seed: int
    fraction: float
    lost: int = 0
    _seen: set[str] = field(default_factory=set)

    def drop(self, request: httpx.Request) -> bool:
        if self.fraction <= 0 or request.method != "POST":
            return False
        if request.url.path not in RECORD_ROUTES:
            return False
        digest = hashlib.sha256(request.content).hexdigest()
        if digest in self._seen:
            return False
        self._seen.add(digest)
        return derive_seed(self.seed, "lost-reply", digest) % 10_000 < self.fraction * 10_000


@dataclass
class Stats:
    """Lo que mide el proceso (ver el módulo)."""

    events: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    statuses: dict[str, Counter[int]] = field(default_factory=lambda: defaultdict(Counter))
    emitted_at: dict[str, tuple[str, int]] = field(default_factory=dict)
    accepted_at: dict[str, str] = field(default_factory=dict)
    rate_limited: list[dict[str, Any]] = field(default_factory=list)
    sends: dict[int, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    """Por nodo: ``(operación, instante)`` de cada registro o concesión enviados."""
    receipts: Counter[str] = field(default_factory=Counter)
    halted: dict[int, str] = field(default_factory=dict)
    heartbeats_skipped: int = 0
    """Latidos del calendario omitidos porque el anterior salió hace menos de un intervalo."""
    history: dict[str, list[list[str]]] = field(default_factory=dict)
    """Por registro aún sin aceptar: sus últimos ``[instante, operación, resultado, código]``;
    se descarta al aceptarse, así que al final solo queda el de lo que no se envió."""
    unsent: list[dict[str, Any]] = field(default_factory=list)
    """Lo que quedó en una bandeja al cerrar el vaciado, con su recorrido (sin identificadores)."""
    flush_seconds: dict[int, float] = field(default_factory=dict)
    """Por nodo, cuánto duró su vaciado final."""
    flush_extended: dict[int, float] = field(default_factory=dict)
    """Por nodo, cuánto se alargó el vaciado más allá de ``flush_seconds`` por reintentos."""


class LoadTransport(httpx.AsyncBaseTransport):
    """Transporte de un nodo: indisponibilidad, respuestas perdidas y latencia por ruta."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        reachability: Reachability,
        stats: Stats,
        *,
        upload: bool = False,
        lost: LostReplies | None = None,
    ) -> None:
        self._inner = inner
        self._reachability = reachability
        self._stats = stats
        self._upload = upload
        self._lost = lost
        self.heartbeat_sent_at: float | None = None
        """Instante monótono en que salió el último latido (del calendario o del cliente); uno que
        no llega a salir (indisponibilidad simulada) no gasta ficha en la plataforma y no cuenta."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._reachability.down():
            raise httpx.ConnectError("plataforma inalcanzable (indisponibilidad simulada)")
        clock = self._reachability.clock
        started = clock.monotonic()
        if request.method == "POST" and request.url.path.endswith(HEARTBEAT_PATH):
            self.heartbeat_sent_at = started
        response = await self._inner.handle_async_request(request)
        try:
            # Los bytes tal cual (sin descomprimir): el cliente los decodifica como siempre.
            content = b"".join([chunk async for chunk in response.aiter_raw()])
        finally:
            await response.aclose()
        elapsed_ms = (clock.monotonic() - started) * 1000
        label = route_label(request, upload=self._upload)
        self._stats.latencies[label].append(elapsed_ms)
        self._stats.statuses[label][response.status_code] += 1
        if self._lost is not None and response.status_code == 200 and self._lost.drop(request):
            self._lost.lost += 1
            raise httpx.ReadTimeout("respuesta perdida después de la aceptación (inyectada)")
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=content,
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


# --- Bitácora de eventos --------------------------------------------------------------------------


class Sink:
    """``on_event`` del cliente: la bitácora del kit más los contadores de ``Stats``."""

    def __init__(self, journal: Journal, stats: Stats, index: int) -> None:
        self.journal = journal
        self.stats = stats
        self.index = index
        self.node: SimulatedNode | None = None

    def __call__(self, event: ClientEvent) -> None:
        code = None if event.code is None else event.code.value
        at = format_timestamp(event.at)
        self.stats.events[(event.operation, event.result, code or "")] += 1
        if event.operation in ("submit_record", "request_grant"):
            self.stats.sends[self.index].append((event.operation, at))
        if event.result == "rate_limited":
            self.stats.rate_limited.append(
                {"node_index": self.index, "operation": event.operation, "at": at}
            )
        if self.node is None or event.subject is None:
            return
        if event.operation == "submit_record":
            self.journal.response(event.subject, event.result, code=code)
            if event.result == "success":
                self.stats.accepted_at.setdefault(event.subject, at)
                self.stats.history.pop(event.subject, None)
            else:
                self._remember(event.subject, at, event.operation, event.result, code)
        elif event.operation in ("request_grant", "upload_clip"):
            record = self.node.clip_records.get(event.subject)
            if record is None:
                return
            self._remember(record, at, event.operation, event.result, code)
            if event.result != "success":
                self.journal.response(record, event.result, code=code)

    def _remember(
        self, record: str, at: str, operation: str, result: str, code: str | None
    ) -> None:
        if record in self.stats.accepted_at:
            return
        events = self.stats.history.setdefault(record, [])
        events.append([at, operation, result, code or ""])
        del events[:-HISTORY_EVENTS]


# --- Un nodo --------------------------------------------------------------------------------------


@dataclass
class NodeRun:
    node: SimulatedNode
    plant_id: str
    timeline: list[tuple[int, int, PlannedItem | None]]
    builder: RecordBuilder
    transport: LoadTransport


async def _sleep_until(clock: SystemClock, when: datetime) -> None:
    remaining = (when - clock.now()).total_seconds()
    await asyncio.sleep(max(0.0, remaining))


async def _beat(run: NodeRun, stats: Stats, clock: SystemClock, heartbeat_seconds: float) -> None:
    """El latido del calendario, salvo si el anterior salió hace menos de un intervalo (con
    ``HEARTBEAT_SLACK_SECONDS`` de holgura): tras un vaciado largo los latidos vencidos no salen en
    ráfaga contra el cubo de 4 por minuto de la plataforma; se omiten, como en el nodo real."""
    sent = run.transport.heartbeat_sent_at
    if sent is not None and clock.monotonic() - sent < heartbeat_seconds - HEARTBEAT_SLACK_SECONDS:
        stats.heartbeats_skipped += 1
        return
    await run.node.client.heartbeat_now()


async def _drain(client: AsyncClient) -> Wait | None:
    """Pasos hasta que el planificador pide esperar; ``None`` si no hay nada que enviar."""
    for _ in range(_MAX_DRAIN_ROUNDS):
        outcome = await client.run_once()
        if outcome is None:
            return None
        if isinstance(outcome, Wait):
            return outcome
    return None


def _unsent(outbox: Outbox) -> list[OutboxItem]:
    return outbox.records(OutboxState.PENDING) + outbox.records(OutboxState.READY)


def _flush_deadline(
    unsent: Sequence[OutboxItem], deadline: datetime, limit: datetime, grace: timedelta
) -> datetime:
    """El tope del vaciado: ``deadline`` o, si el cliente tiene un reintento programado más
    tarde (``next_attempt_at``), ese reintento más ``grace``, nunca después de ``limit``.

    Tras una caída larga un registro acumula intentos y su espera llega al tope del retroceso
    (5 minutos, y no decrece mientras siga fallando): si el vaciado de la flota aún satura la
    plataforma cuando acaba ``deadline``, cortar ahí da por perdido lo que el nodo real enviaría
    en su próximo reintento (VIG-192)."""
    retries = [item.next_attempt_at for item in unsent if item.next_attempt_at is not None]
    if not retries:
        return deadline
    return min(limit, max(deadline, max(retries) + grace))


async def run_node(
    run: NodeRun,
    clips: dict[tuple[str, int], SyntheticClip],
    journal: Journal,
    stats: Stats,
    clock: SystemClock,
    *,
    start: datetime,
    speed_factor: float,
    heartbeat_seconds: float,
    flush_seconds: float,
) -> None:
    node, client = run.node, run.node.client
    await _beat(run, stats, clock, heartbeat_seconds)
    wait: Wait | None = None
    for offset, _, item in run.timeline:
        due = start + timedelta(milliseconds=offset / speed_factor)
        while wait is not None and wait.until < due:
            await _sleep_until(clock, wait.until)
            wait = await _drain(client)
        await _sleep_until(clock, due)
        if item is None:
            await _beat(run, stats, clock, heartbeat_seconds)
        else:
            now = clock.now()
            built = (
                run.builder.episode(item, clips[(item.image_name, item.variant)], now)
                if isinstance(item, PlannedEpisode)
                else run.builder.degradation(item, now)
            )
            if built is not None:
                node.outbox.enqueue(built.submission, built.clips)
                for clip_id in built.clips:
                    node.clip_records[clip_id] = built.record_id
                journal.emitted(
                    built.record_id,
                    built.kind,
                    node_index=node.index,
                    zone_id=built.zone_id,
                    clip_bytes=built.clip_bytes,
                )
                stats.emitted_at[built.record_id] = (format_timestamp(now), node.index)
        wait = await _drain(client)
    flush_start = clock.now()
    base = deadline = flush_start + timedelta(seconds=flush_seconds)
    limit = base + timedelta(seconds=FLUSH_EXTENSION_SECONDS)
    grace = timedelta(seconds=2 * heartbeat_seconds)
    beat = clock.now() + timedelta(seconds=heartbeat_seconds)
    while unsent := _unsent(node.outbox):
        deadline = _flush_deadline(unsent, deadline, limit, grace)
        if clock.now() >= deadline:
            break
        if clock.now() >= beat:
            await _beat(run, stats, clock, heartbeat_seconds)
            beat = clock.now() + timedelta(seconds=heartbeat_seconds)
        wait = await _drain(client)
        until = min(deadline, beat) if wait is None else min(wait.until, deadline, beat)
        await _sleep_until(clock, until)
    stats.flush_seconds[node.index] = (clock.now() - flush_start).total_seconds()
    if deadline > base:
        stats.flush_extended[node.index] = (deadline - base).total_seconds()
    if client.halted_reason is not None:
        stats.halted[node.index] = str(client.halted_reason)
    _reconcile(node, journal, stats, start)


def _reconcile(node: SimulatedNode, journal: Journal, stats: Stats, start: datetime) -> None:
    """Estado final de cada registro según la bandeja, como el nodo simulado del kit, y el
    recibo de cada aceptado (``accepted`` o ``accepted_duplicate``). De lo que quedó sin enviar
    guarda su recorrido en ``stats.unsent``: tipo, nodo, segundos desde el inicio (emisión y
    próximo reintento), intentos, estado de sus clips y sus últimos eventos, nunca su
    identificador."""
    for item in node.outbox.records(OutboxState.RETAINED):
        status = "accepted" if item.receipt is None else item.receipt.status.value
        stats.receipts[status] += 1
        journal.response(item.record_id, status)
    for item in node.outbox.records(OutboxState.DEAD_LETTER):
        code = "unknown" if item.dead_letter_code is None else item.dead_letter_code.value
        journal.dead_letter(item.record_id, code)
    for state in (OutboxState.PENDING, OutboxState.READY):
        for item in node.outbox.records(state):
            journal.response(item.record_id, "unsent")
            stats.unsent.append(_unsent_entry(item, node.index, stats, start))


def _unsent_entry(item: OutboxItem, index: int, stats: Stats, start: datetime) -> dict[str, Any]:
    def since(moment: datetime | None) -> float | None:
        return None if moment is None else round((moment - start).total_seconds(), 1)

    emitted = stats.emitted_at.get(item.record_id)
    history = stats.history.get(item.record_id, [])
    return {
        "node_index": index,
        "kind": item.kind.value,
        "state": item.state.value,
        "attempts": item.attempts,
        "emitted_s": None if emitted is None else since(_instant(emitted[0])),
        "next_attempt_s": since(item.next_attempt_at),
        "clips": [clip.state.value for clip in item.clips],
        "history": [
            [since(_instant(at)), operation, result, code]
            for at, operation, result, code in history
        ],
    }


def _instant(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# --- Plan -----------------------------------------------------------------------------------------


def plan_items(
    profile: LoadProfile,
    seed: int,
    names: Sequence[str],
    node_index: int,
    zones: int,
) -> list[tuple[int, PlannedItem]]:
    """Los elementos del plan del nodo en la duración del perfil, un día simulado por cada 24 h
    (el primero con la semilla de la ejecución y los demás con una derivada)."""
    base = simulation_profile(seed, profile.speed_factor, profile.episodes_per_day)
    weights = resolve_hourly_weights(base, None, DEFAULT_GROUPING_WINDOW_MS)
    durations = duration_distribution(None, DEFAULT_GROUPING_WINDOW_MS)
    span = profile.span_ms
    items: list[tuple[int, PlannedItem]] = []
    for day in range(math.ceil(span / DAY_MS)):
        daily = (
            base if day == 0 else base.model_copy(update={"seed": derive_seed(seed, "day", day)})
        )
        plan = plan_day(daily, names, weights, durations, node_index=node_index, zones=zones)
        for item in plan.items:
            offset = day * DAY_MS + item.offset_ms
            if offset < span:
                items.append((offset, item))
    return items


def _timeline(
    items: Sequence[tuple[int, PlannedItem]], span_ms: int, heartbeat_ms: int
) -> list[tuple[int, int, PlannedItem | None]]:
    entries: list[tuple[int, int, PlannedItem | None]] = [
        (offset, order, item) for order, (offset, item) in enumerate(items)
    ]
    order = len(entries)
    beat = heartbeat_ms
    while beat < span_ms:
        entries.append((beat, order, None))
        order += 1
        beat += heartbeat_ms
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return entries


# --- Ejecución ------------------------------------------------------------------------------------


async def drive(plan: dict[str, Any]) -> dict[str, Any]:
    profile = PROFILES[plan["profile"]]
    if "steady_minutes" in plan:  # ``soak.py --hours``
        profile = replace(profile, steady_minutes=float(plan["steady_minutes"]))
    seed = int(plan["seed"])
    fleet = json.loads(Path(plan["fleet"]).read_text(encoding="utf-8"))
    verify = str(fleet["verify"])
    clock = SystemClock()
    dataset = load_sealed_dataset(Path(plan["dataset"]))
    cache = ClipCache(Path(plan["cache"]), dataset, None)
    journal = Journal(clock, None, run_id=f"carga-{profile.name}")
    stats = Stats()
    reachability = Reachability(clock)
    lost = LostReplies(seed, profile.lost_reply_fraction)
    work = Path(plan["work"])
    runs: list[NodeRun] = []
    clips: dict[tuple[str, int], SyntheticClip] = {}
    heartbeat_seconds = 0.0
    try:
        for entry in fleet["nodes"]:
            index = int(entry["index"])
            catalogs = [dict(catalog) for catalog in entry["catalogs"]]
            ids = NodeIdentityIds(
                organization_id=entry["organization_id"],
                plant_id=entry["plant_id"],
                node_id=entry["node_id"],
            )
            credentials = NodeCredentials(
                NodeIdentity(ids.node_id, ids.organization_id, ids.plant_id),
                FileCredentialStore(entry["credential"]),
                clock,
                live_view_host=LIVE_VIEW_HOST,
                software_version=SOFTWARE_VERSION,
            )
            configuration = NodeInitialConfiguration.model_validate_json(
                Path(entry["configuration"]).read_text(encoding="utf-8")
            )
            config = ClientConfig.from_initial_configuration(
                configuration, ingest_base_url=fleet["ingest_base_url"], cafile=verify
            )
            heartbeat_seconds = max(
                config.heartbeat_interval_seconds / profile.speed_factor, HEARTBEAT_FLOOR_SECONDS
            )
            node_tls = credentials.client_ssl_context(cafile=verify)
            transport = LoadTransport(
                httpx.AsyncHTTPTransport(verify=node_tls), reachability, stats, lost=lost
            )
            upload = LoadTransport(
                httpx.AsyncHTTPTransport(verify=ssl.create_default_context(cafile=verify)),
                reachability,
                stats,
                upload=True,
            )
            outbox = Outbox(work / f"outbox-{index:05d}.sqlite", clock)
            sink = Sink(journal, stats, index)
            client = AsyncClient(
                config,
                credentials,
                outbox,
                clock,
                status=node_report(ids, catalogs),
                rng=random.Random(derive_seed(seed, "retry", index)),  # noqa: S311
                transport=transport,
                upload_transport=upload,
                on_event=sink,
                random_bytes=deterministic_bytes(seed, index),
            )
            client.seed(configuration)
            node = SimulatedNode(
                index,
                ids,
                client,
                outbox,
                catalogs,
                heartbeat_interval_seconds=config.heartbeat_interval_seconds,
            )
            sink.node = node
            items = plan_items(profile, seed, dataset.names, index, len(catalogs))
            for _, item in items:
                if isinstance(item, PlannedEpisode):
                    key = (item.image_name, item.variant)
                    if key not in clips:
                        clips[key] = cache.get(cache.spec(*key))
            journal.planned(index, len(items), math.ceil(profile.span_ms / DAY_MS))
            timeline = _timeline(
                items, profile.span_ms, round(heartbeat_seconds * 1000 * profile.speed_factor)
            )
            builder = RecordBuilder(ids, catalogs, software_version=SOFTWARE_VERSION)
            runs.append(NodeRun(node, ids.plant_id, timeline, builder, transport))
        start = clock.now() + timedelta(seconds=2)
        phases = []
        for phase in profile.phases():
            begins = start + timedelta(milliseconds=phase.start_ms / profile.speed_factor)
            ends = start + timedelta(milliseconds=phase.end_ms / profile.speed_factor)
            if phase.unreachable:
                reachability.windows.append((begins, ends))
            phases.append(
                {
                    "name": phase.name,
                    "unreachable": phase.unreachable,
                    "start": format_timestamp(begins),
                    "end": format_timestamp(ends),
                }
            )
        await asyncio.gather(
            *(
                run_node(
                    run,
                    clips,
                    journal,
                    stats,
                    clock,
                    start=start,
                    speed_factor=profile.speed_factor,
                    heartbeat_seconds=heartbeat_seconds,
                    flush_seconds=float(plan["flush_seconds"]),
                )
                for run in runs
            )
        )
        finished = clock.now()
    finally:
        for run in runs:
            await run.node.client.aclose()
            run.node.outbox.close()
    summary = journal.summary()
    plant_of = {run.node.index: run.plant_id for run in runs}
    return {
        "seed": seed,
        "profile": profile.name,
        "started_at": format_timestamp(start),
        "finished_at": format_timestamp(finished),
        "heartbeat_seconds": heartbeat_seconds,
        "phases": phases,
        "journal": summary.to_json_value(),
        "events": [[op, result, code, count] for (op, result, code), count in stats.events.items()],
        "receipts": dict(stats.receipts),
        "lost_replies": lost.lost,
        "records": {
            record: {
                "emitted_at": emitted,
                "accepted_at": stats.accepted_at.get(record),
                "node_index": index,
                "plant_id": plant_of[index],
            }
            for record, (emitted, index) in stats.emitted_at.items()
        },
        "rate_limited": stats.rate_limited,
        "heartbeats_skipped": stats.heartbeats_skipped,
        "flush": {
            "base_seconds": float(plan["flush_seconds"]),
            "extension_limit_seconds": FLUSH_EXTENSION_SECONDS,
            "max_seconds": round(max(stats.flush_seconds.values(), default=0.0), 1),
            "extended_nodes": len(stats.flush_extended),
            "max_extension_seconds": round(max(stats.flush_extended.values(), default=0.0), 1),
        },
        "unsent": stats.unsent,
        "sends": {str(index): sends for index, sends in stats.sends.items()},
        "latency": {
            label: latency_summary(values, stats.statuses[label])
            for label, values in sorted(stats.latencies.items())
        },
        "halted": {str(index): reason for index, reason in stats.halted.items()},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.load.driver", description="Nodos simulados de un perfil de carga."
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    arguments = parser.parse_args(argv)
    plan = json.loads(arguments.plan.read_text(encoding="utf-8"))
    result = asyncio.run(drive(plan))
    arguments.result.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    sys.stdout.write(
        f"perfil {result['profile']} semilla {result['seed']}:"
        f" {len(result['journal']['emitted'])} emitidos,"
        f" {len(result['journal']['accepted'])} aceptados\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
