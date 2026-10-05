"""Mundo en memoria de la ingesta (TASK-221) para las propiedades PR-GOB-01, 02, 04, 06, 07 y 17.

La aplicación es la **real**: la cadena fija, la ``NodeApiGate`` de ``node_api`` (versión,
certificado contra ``MemoryNodeStore``, tamaño y esquema con los modelos de U-01) y las tres
operaciones de ``node_api.routes`` sobre el ``IngestService`` real. Solo son dobles los puertos del
servicio:

- ``MemoryIngestStore``: asignaciones, zonas por organización, registros aceptados, retención,
  historia del catálogo (``CatalogVersionView`` de catálogos del kit de U-01), intervalos de la
  compuerta de uso, concesiones y cierres huérfanos; filtra por la organización del contexto como
  la seguridad a nivel de fila;
- ``MemoryWriter``: el ``EscritorExpediente`` en lo que la ingesta usa de él: valida el contenido y
  los eventos con los modelos registrados de U-03, idempotencia por (organización, tipo, clave)
  con el hash canónico, clips contra ``MemoryEvidence`` (paso 6), la ``projection`` antes de
  «escribir» y el ``Receipt`` después; si la proyección lanza, no queda nada;
- ``MemoryAudit`` y ``MemoryDatabase`` (transacciones que solo llevan el contexto).

Las pruebas con PostgreSQL y el escritor real están en ``tests/integration/test_fleet_ingest*.py``.
Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final

from fastapi.testclient import TestClient
from pydantic import ValidationError
from vigia_contracts.canonical import canonicalize
from vigia_contracts.models.enumerations import AcceptanceStatus

from tests.api_support import World
from tests.node_api_support import (
    DAY,
    VERSION,
    MemoryNodeStore,
    NodeFixture,
    Probe,
    TestAuthority,
    alb_headers,
    node_app,
    node_gate,
    scope_contexts,
)
from vigia_platform.fleet.adapters.postgres.ingest_queries import AcceptedRecord
from vigia_platform.fleet.application.ingest import IngestDependencies, IngestService
from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.ingest_order import (
    AssignmentSpan,
    CatalogVersionView,
    GateSpan,
    IngestKind,
    cited_clip_ids,
)
from vigia_platform.fleet.events import FLEET_EVENT_TYPES
from vigia_platform.fleet.record_types import FLEET_RECORD_TYPES
from vigia_platform.identity.authz.context import NodeAssignment
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.node_api.certificate_profile import NodeSubject
from vigia_platform.node_api.router import NodeApiGate
from vigia_platform.node_api.routes.detection_reviews import detection_review_operation
from vigia_platform.node_api.routes.findings import finding_operation
from vigia_platform.node_api.routes.observability_events import observability_event_operation
from vigia_platform.node_api.versioning import VersionPolicy
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "INGEST_ROUTES",
    "ROUTE_OF",
    "IngestWorld",
    "MemoryEvidence",
    "MemoryIngestStore",
    "MemoryWriter",
    "ingest_world",
    "place",
]

INGEST_ROUTES: Final = (
    NodeRoute.FINDING,
    NodeRoute.DETECTION_REVIEW,
    NodeRoute.OBSERVABILITY_EVENT,
)
ROUTE_OF: Final = {
    IngestKind.FINDING: NodeRoute.FINDING,
    IngestKind.DETECTION_FOR_REVIEW: NodeRoute.DETECTION_REVIEW,
    IngestKind.OBSERVABILITY_EVENT: NodeRoute.OBSERVABILITY_EVENT,
}
_RECORD_MODELS: Final = {item.record_type: item for item in FLEET_RECORD_TYPES}
_EVENT_MODELS: Final = {item.event_name: item.payload_model for item in FLEET_EVENT_TYPES}


# --- Dobles de los puertos -----------------------------------------------------------------------


@dataclass
class MemoryIngestStore:
    """``IngestStore`` en memoria; cada lectura filtra por la organización del contexto."""

    organization_of_zone: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    assignment_rows: list[tuple[uuid.UUID, AssignmentSpan]] = field(default_factory=list)
    records: dict[tuple[uuid.UUID, str, str], AcceptedRecord] = field(default_factory=dict)
    retention: dict[uuid.UUID, int] = field(default_factory=dict)
    catalogs: dict[uuid.UUID, list[CatalogVersionView]] = field(default_factory=dict)
    gates: dict[uuid.UUID, list[GateSpan]] = field(default_factory=dict)
    grants: dict[uuid.UUID, str] = field(default_factory=dict)
    orphan_closes: list[dict[str, Any]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def _own(self, organization_id: uuid.UUID, zone_id: uuid.UUID) -> bool:
        return self.organization_of_zone.get(zone_id) == organization_id

    async def assignments(
        self, context: ScopeContext, node_id: uuid.UUID, zone_id: uuid.UUID, at: dt.datetime
    ) -> tuple[AssignmentSpan, ...]:
        self.queries.append("assignments")
        if not self._own(context.organization_id, zone_id):
            return ()
        return tuple(
            span
            for node, span in self.assignment_rows
            if node == node_id
            and span.zone_id == zone_id
            and span.assigned_at <= at
            and (span.unassigned_at is None or span.unassigned_at > at)
        )

    async def zone_in_organization(self, context: ScopeContext, zone_id: uuid.UUID) -> bool:
        return self._own(context.organization_id, zone_id)

    async def accepted(
        self, context: ScopeContext, kind: IngestKind, source_key: str
    ) -> AcceptedRecord | None:
        self.queries.append("accepted")
        return self.records.get((context.organization_id, kind.record_type, source_key))

    async def retention_days(self, transaction: Any, node_id: uuid.UUID) -> int | None:
        return self.retention.get(node_id)

    async def catalog_versions(
        self, transaction: Any, zone_id: uuid.UUID, window: Window
    ) -> tuple[CatalogVersionView, ...]:
        self.queries.append("catalog")
        if not self._own(transaction.context.organization_id, zone_id):
            return ()
        return tuple(
            item
            for item in self.catalogs.get(zone_id, [])
            if window.overlaps(item.effective_from, item.effective_until)
        )

    async def usage_spans(
        self, transaction: Any, zone_id: uuid.UUID, window: Window
    ) -> tuple[GateSpan, ...]:
        self.queries.append("gate")
        if not self._own(transaction.context.organization_id, zone_id):
            return ()
        return tuple(self.gates.get(zone_id, []))

    async def event_accepted(self, transaction: Any, event_id: str) -> bool:
        key = (
            transaction.context.organization_id,
            IngestKind.OBSERVABILITY_EVENT.record_type,
            event_id,
        )
        return key in self.records

    async def mark_orphan_close(self, transaction: Any, **fields: Any) -> None:
        if not any(row["event_id"] == fields["event_id"] for row in self.orphan_closes):
            self.orphan_closes.append(dict(fields))

    async def mark_cited(
        self, transaction: Any, *, clip_ids: Sequence[uuid.UUID], **_: Any
    ) -> tuple[uuid.UUID, ...]:
        changed = tuple(clip for clip in clip_ids if self.grants.get(clip) == "issued")
        for clip in changed:
            self.grants[clip] = "used"
        return changed


@dataclass
class MemoryEvidence:
    """El paso 6: el resultado de cada ``clip_id`` (``ok`` por omisión) o el almacén caído."""

    outcomes: dict[uuid.UUID, LedgerRejectionCode] = field(default_factory=dict)
    down: bool = False

    def check(self, clip_ids: Sequence[uuid.UUID]) -> LedgerRejection | None:
        if not clip_ids:
            return None
        if self.down:
            raise StorageUnavailable("head_object")
        for clip in clip_ids:
            if clip in self.outcomes:
                return LedgerRejection.of(self.outcomes[clip], "/cameras/0/clips/0")
        return None


@dataclass
class MemoryWriter:
    """El ``EscritorExpediente`` en lo que la ingesta usa de él (pasos 3, 5, 6 y 9)."""

    store: MemoryIngestStore
    evidence: MemoryEvidence
    clock: Any
    events: list[NewEvent] = field(default_factory=list)
    rejections: list[dict[str, Any]] = field(default_factory=list)
    chain_locked: bool = False

    async def write(
        self,
        context: ScopeContext | None,
        record_type: str,
        content: Mapping[str, Any],
        *,
        scope: Any = None,
        events: Sequence[NewEvent] = (),
        occurred_at: dt.datetime | None = None,
        projection: Any = None,
        transaction: Any = None,
        record_id: uuid.UUID | None = None,
    ) -> Receipt | LedgerRejection:
        assert isinstance(context, ScopeContext)
        definition = _RECORD_MODELS[record_type]
        try:
            definition.content_model.model_validate_json(json.dumps(dict(content)))
            for event in events:
                _EVENT_MODELS[event.event_name].model_validate_json(json.dumps(event.payload))
        except ValidationError as error:
            raise AssertionError(f"contenido o evento inválido: {error}") from None
        organization = context.organization_id
        now = to_millisecond(self.clock.now())
        if record_type == "ingest_rejected":
            self.rejections.append(dict(content))
            return Receipt(uuid7(self.clock), now, AcceptanceStatus.ACCEPTED)
        path = definition.source_key_path
        assert path is not None
        key = (organization, record_type, str(content[path.lstrip("/")]))
        existing = self.store.records.get(key)
        if existing is not None:
            if canonicalize(dict(content)) == canonicalize(existing.content):
                return Receipt(
                    existing.record_id, existing.received_at, AcceptanceStatus.ACCEPTED_DUPLICATE
                )
            return LedgerRejection.of(LedgerRejectionCode.IDEMPOTENCY_CONFLICT)
        failed = self.evidence.check(cited_clip_ids(content))
        if failed is not None:
            return failed
        if self.chain_locked:
            return LedgerRejection.of(LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT)
        if projection is not None:
            await projection(SimpleNamespace(context=context))
        record = record_id if record_id is not None else uuid7(self.clock)
        self.store.records[key] = AcceptedRecord(record, now, json.loads(json.dumps(dict(content))))
        self.events.extend(events)
        return Receipt(record, now, AcceptanceStatus.ACCEPTED)


@dataclass
class MemoryAudit:
    entries: list[dict[str, Any]] = field(default_factory=list)

    async def append(self, context: ScopeContext, operation: Any, **fields: Any) -> None:
        fields.pop("transaction", None)
        self.entries.append(
            {"operation": str(operation), "correlation_id": context.correlation_id, **fields}
        )


class MemoryDatabase:
    """Solo transacciones con el contexto (lo que usa ``record_rejection``)."""

    @contextlib.asynccontextmanager
    async def transaction(self, context: ScopeContext) -> AsyncIterator[Any]:
        yield SimpleNamespace(context=context)

    async def read(self, *_: Any) -> list[Any]:  # pragma: no cover - no se usa
        return []


# --- El mundo -------------------------------------------------------------------------------------


def place(
    document: dict[str, Any],
    started_at: dt.datetime,
    *,
    duration: dt.timedelta = dt.timedelta(seconds=30),
    synchronized: bool = True,
    offset_ms: int = 12,
) -> dict[str, Any]:
    """``document`` con su ``node_time`` en ``started_at`` y el reloj declarado."""
    node_time = dict(document["node_time"])
    node_time["started_at"] = format_timestamp(started_at)
    node_time["ended_at"] = format_timestamp(started_at + duration)
    node_time["clock"] = {
        **node_time["clock"],
        "synchronized": synchronized,
        "offset_ms": offset_ms,
    }
    return {**document, "node_time": node_time}


@dataclass
class IngestWorld:
    world: World
    nodes: MemoryNodeStore
    authority: TestAuthority
    gate: NodeApiGate
    app: Any
    service: IngestService
    store: MemoryIngestStore
    writer: MemoryWriter
    audit: MemoryAudit
    evidence: MemoryEvidence
    a: NodeFixture
    zone: uuid.UUID
    """La zona asignada al nodo ``a`` desde hace 30 días."""
    other_zone: uuid.UUID
    """Una zona de la organización de ``a`` que el nodo nunca tuvo asignada."""
    foreign_zone: uuid.UUID
    """Una zona de otra organización."""
    certificate: Any = None
    client: TestClient | None = None
    """Cliente abierto para muchas peticiones (máquinas de estados); sin él, uno por petición."""

    def open(self) -> None:
        self.client = TestClient(self.app)
        self.client.__enter__()

    def close(self) -> None:
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None

    @property
    def now(self) -> dt.datetime:
        return to_millisecond(self.world.clock.now())

    def advance(self, delta: dt.timedelta) -> None:
        self.world.clock.advance(delta.total_seconds())

    def scoped(self, catalog: dict[str, Any], *, zone: uuid.UUID | None = None) -> dict[str, Any]:
        """Un catálogo del kit con la organización, la planta y la zona de ``a``."""
        return {
            **catalog,
            "organization_id": str(self.a.organization_id),
            "plant_id": str(self.a.plant_id),
            "zone_id": str(zone or self.zone),
        }

    def publish_catalog(
        self,
        catalog: Mapping[str, Any],
        effective_from: dt.datetime,
        effective_until: dt.datetime | None = None,
        *,
        version: int | None = None,
    ) -> CatalogVersionView:
        zone = uuid.UUID(str(catalog["zone_id"]))
        history = self.store.catalogs.setdefault(zone, [])
        number = version if version is not None else len(history) + 1
        view = CatalogVersionView.from_payload(number, effective_from, effective_until, catalog)
        history.append(view)
        return view

    def set_usage(self, approved: bool, at: dt.datetime, zone: uuid.UUID | None = None) -> None:
        """La compuerta de uso pasa a ``approved`` o deja de estarlo en ``at`` (cierra el abierto)."""
        spans = self.store.gates.setdefault(zone or self.zone, [])
        if spans and spans[-1].effective_until is None:
            last = spans[-1]
            if last.effective_from == at:
                spans.pop()
            else:
                spans[-1] = GateSpan(last.approved, last.effective_from, at)
        spans.append(GateSpan(approved, at, None))

    def headers(
        self, document: Mapping[str, Any] | None, kind: IngestKind, *, version: str | None = VERSION
    ) -> dict[str, str]:
        values = {**alb_headers(self.certificate), "Content-Type": "application/json"}
        if version is not None:
            values["X-Vigia-Contract-Version"] = version
        if document is not None and isinstance(document.get(kind.id_field), str):
            values["Idempotency-Key"] = document[kind.id_field]
        return values

    def post(
        self,
        kind: IngestKind,
        document: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> Any:
        route = ROUTE_OF[kind]
        content = body if body is not None else json.dumps(document).encode()
        sent = dict(headers) if headers is not None else self.headers(document, kind)
        if self.client is not None:
            return self.client.post(route.path, headers=sent, content=content)
        with TestClient(self.app) as client:
            return client.post(route.path, headers=sent, content=content)

    def accepted(self, kind: IngestKind) -> list[AcceptedRecord]:
        return [
            record
            for (_, record_type, _), record in self.store.records.items()
            if record_type == kind.record_type
        ]


def ingest_world(
    *, start: dt.datetime | None = None, policy: VersionPolicy | None = None
) -> IngestWorld:
    """Un nodo ``a`` de alta con una zona asignada; catálogo y compuerta los pone la prueba."""
    world = World()
    clock = world.clock
    if start is not None:
        clock.set(start)
    now = to_millisecond(clock.now())
    organization, other_organization = uuid.uuid4(), uuid.uuid4()
    plant, zone, other_zone, foreign_zone = (uuid.uuid4() for _ in range(4))
    node = NodeFixture(uuid.uuid4(), organization, plant, enrolled_at=now - 30 * DAY)
    node.assignments.append(NodeAssignment(zone, now - 30 * DAY, None))
    nodes = MemoryNodeStore()
    nodes.nodes[node.node_id] = node
    store = MemoryIngestStore(
        organization_of_zone={
            zone: organization,
            other_zone: organization,
            foreign_zone: other_organization,
        },
        assignment_rows=[(node.node_id, AssignmentSpan(zone, now - 30 * DAY, None))],
    )
    evidence = MemoryEvidence()
    writer = MemoryWriter(store, evidence, clock)
    audit = MemoryAudit()
    service = IngestService(
        IngestDependencies(
            database=MemoryDatabase(),  # type: ignore[arg-type]
            writer=writer,
            audit=audit,
            clock=clock,
            store=store,
        )
    )
    operations = {
        NodeRoute.FINDING: finding_operation(service),
        NodeRoute.DETECTION_REVIEW: detection_review_operation(service),
        NodeRoute.OBSERVABILITY_EVENT: observability_event_operation(service),
    }
    gate = node_gate(
        contexts=scope_contexts(clock),
        store=nodes,
        clock=clock,
        probe=Probe(),
        policy=policy,
        operations=operations,
    )
    app = node_app(world, gate, routes=INGEST_ROUTES)
    authority = TestAuthority()
    result = IngestWorld(
        world,
        nodes,
        authority,
        gate,
        app,
        service,
        store,
        writer,
        audit,
        evidence,
        node,
        zone,
        other_zone,
        foreign_zone,
    )
    certificate = authority.leaf(
        NodeSubject(node.node_id, organization, plant),
        not_before=now - DAY,
        not_after=now + 364 * DAY,
    )
    node.credentials[format(certificate.serial_number, "x")] = {
        "status": "active",
        "issued_at": now - DAY,
        "expires_at": now + 364 * DAY,
    }
    result.certificate = certificate
    return result
