"""Entorno de las pruebas de la ingesta (TASK-221) sobre PostgreSQL 16 real, como ``vigia_app``.

``ingest_stack`` levanta, sobre una base migrada propia:

- el ``EscritorExpediente`` real con los tipos de U-02 y los trece de la flota, la bandeja con los
  eventos de U-02 y de la flota **sin consumidores registrados** (D-3: la ingesta no depende de
  U-04) y el ``EvidenceVerifier`` real sobre ``EvidenceStore``, un almacén de metadatos en memoria
  que puede caerse (``down``) y que anota si alguna consulta llegó con una transacción abierta;
- la auditoría real de U-02 (``AuditWriter``) y el ``IngestService`` real con ``PostgresIngestStore``;
- la ``NodeApiGate`` real (identidad por certificado contra la base, sin límite de tasa: no es lo
  que se prueba) y la aplicación con la cadena fija y las tres rutas de la ingesta.

``instance(database)`` construye **otra instancia** completa (escritor, servicio, ``NodeApiGate`` y
aplicación) sobre otra ``Database``: dos instancias no comparten nada en memoria salvo el almacén.

``site`` siembra un nodo dado de alta con una zona asignada (desde ``assigned_since``), otra zona de
la misma organización sin asignar, un catálogo vigente con un estándar y una señal, la compuerta de
uso aprobada (o no) y las concesiones ``issued`` de sus clips; ``finding``, ``detection`` y
``event`` construyen presentaciones válidas del contrato, con sus objetos en el almacén. Solo datos
generados (NFR-CTR-43). Reloj simulado desde la hora de la base (retro 14); topes de la base de 60 s
(retro 15).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import json
import secrets
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from cryptography import x509
from vigia_contracts.models import api

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, authz_environment
from tests.heartbeat_support import UnlimitedLimiter
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_db import DbNode, issue
from tests.node_api_support import DAY, VERSION, TestAuthority, alb_headers, node_unit
from tests.outbox_support import app_database
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.fleet.adapters.postgres.ingest_queries import PostgresIngestStore
from vigia_platform.fleet.application.ingest import IngestDependencies, IngestService
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import ANONYMIZED_METADATA_KEY, EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import NodeApiGate
from vigia_platform.node_api.routes.detection_reviews import detection_review_operation
from vigia_platform.node_api.routes.findings import finding_operation
from vigia_platform.node_api.routes.observability_events import observability_event_operation
from vigia_platform.shared.api.declarations import NODE_GATE_STATE_KEY, NodeRoute
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import Database
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.runtime.units import _fleet_event_types, _fleet_record_types
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond
from vigia_platform.shared.storage import ChecksumType, ObjectHead, StorageUnavailable

__all__ = [
    "LOCK_TIMEOUT_MS",
    "ROUTES",
    "EvidenceStore",
    "IngestInstance",
    "IngestSite",
    "IngestStack",
    "ingest_stack",
]

LOCK_TIMEOUT_MS: Final = 60_000
ROUTES: Final = (NodeRoute.FINDING, NodeRoute.DETECTION_REVIEW, NodeRoute.OBSERVABILITY_EVENT)
ROUTE_OF: Final = {
    IngestKind.FINDING: NodeRoute.FINDING,
    IngestKind.DETECTION_FOR_REVIEW: NodeRoute.DETECTION_REVIEW,
    IngestKind.OBSERVABILITY_EVENT: NodeRoute.OBSERVABILITY_EVENT,
}
REVIEW, PUBLICATION = 0.4, 0.7
CLIP_WINDOW_SECONDS: Final = 5
SOFTWARE_VERSION: Final = "1.4.0"
MODEL_VERSION: Final = "yolov8n-2026.09"


class EvidenceStore:
    """``head_object`` en memoria: objetos por clave, caída a voluntad y consultas contadas."""

    def __init__(self) -> None:
        self.objects: dict[str, ObjectHead] = {}
        self.down = False
        self.calls = 0

    async def head_object(self, key: str) -> ObjectHead | None:
        self.calls += 1
        await asyncio.sleep(0)
        if self.down:
            raise StorageUnavailable("head_object")
        return self.objects.get(key)

    def put(self, clip: Mapping[str, Any], *, marker: str | None = "1") -> None:
        self.objects[str(clip["storage_key"])] = ObjectHead(
            key=str(clip["storage_key"]),
            size_bytes=int(clip["size_bytes"]),
            checksum_sha256=base64.b64encode(bytes.fromhex(str(clip["sha256"]))).decode(),
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type=str(clip["content_type"]),
            metadata={} if marker is None else {ANONYMIZED_METADATA_KEY: marker},
            version_id="v1",
        )

    def remove(self, clip: Mapping[str, Any]) -> None:
        self.objects.pop(str(clip["storage_key"]), None)


@dataclass(frozen=True)
class IngestSite:
    """Un nodo dado de alta con su zona asignada, su catálogo y su compuerta de uso."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    node_id: uuid.UUID
    zone_id: uuid.UUID
    other_zone_id: uuid.UUID
    """De la misma organización y planta, nunca asignada al nodo."""
    camera_id: uuid.UUID
    signal_id: uuid.UUID
    standard_id: uuid.UUID
    certificate: x509.Certificate

    def headers(self, document: Mapping[str, Any], kind: IngestKind) -> dict[str, str]:
        return {
            **alb_headers(self.certificate),
            "X-Vigia-Contract-Version": VERSION,
            "Idempotency-Key": str(document[kind.id_field]),
            "Content-Type": "application/json",
        }


@dataclass
class IngestInstance:
    """Una instancia de ``vigia-api``: su base, su escritor, su servicio y su aplicación."""

    database: Database
    writer: EscritorExpediente
    service: IngestService
    gate: NodeApiGate
    app: Any
    client: httpx.AsyncClient


@dataclass
class IngestStack:
    authz: AuthzEnvironment
    registry: RecordTypeRegistry
    free_text: FreeTextPolicyRegistry
    outbox: Outbox
    storage: EvidenceStore
    authority: TestAuthority
    primary: IngestInstance = field(init=False)
    _extra: list[IngestInstance] = field(default_factory=list)

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    def now(self) -> dt.datetime:
        return to_millisecond(self.authz.now())

    def tick(self, seconds: float = 1.0) -> None:
        self.authz.sessions.clock.advance(seconds)

    def uuid7(self) -> uuid.UUID:
        return uuid7(self.authz.sessions.clock)

    # --- Instancias ----------------------------------------------------------------------------

    def database(self, **changes: Any) -> Database:
        fields: dict[str, Any] = {"worker_pool_size": 8, "lock_timeout_ms": LOCK_TIMEOUT_MS}
        fields.update(changes)
        return app_database(self.authz.sessions.migrated, **fields)

    def instance(self, database: Database | None = None) -> IngestInstance:
        database = database if database is not None else self.database()
        sessions = self.authz.sessions
        clock = sessions.clock
        writer = EscritorExpediente(
            database=database,
            registry=self.registry,
            free_text=self.free_text,
            evidence=EvidenceVerifier(self.storage, clock),
            outbox=self.outbox,
            clock=clock,
        )
        service = IngestService(
            IngestDependencies(
                database=database,
                writer=writer,
                audit=sessions.audit,
                clock=clock,
                store=PostgresIngestStore(database),
            )
        )
        gate = NodeApiGate(
            identity=NodeIdentity(
                contexts=self.authz.contexts, store=PostgresNodeContextStore(database)
            ),
            limits=NodeRateLimits(UnlimitedLimiter(clock)),
            clock=clock,
            responses=NodeResponses(clock),
            operations={
                NodeRoute.FINDING: finding_operation(service),
                NodeRoute.DETECTION_REVIEW: detection_review_operation(service),
                NodeRoute.OBSERVABILITY_EVENT: observability_event_operation(service),
            },
        )
        app = World(clock=clock).app(
            units=(node_unit(ROUTES),), runtime={"state": {NODE_GATE_STATE_KEY: gate}}
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=120.0
        )
        built = IngestInstance(database, writer, service, gate, app, client)
        self._extra.append(built)
        return built

    async def release(self) -> None:
        for built in self._extra:
            if built is not self.primary:
                await built.client.aclose()
                await built.database.dispose()
        self._extra = [self.primary]

    async def close(self) -> None:
        for built in self._extra:
            await built.client.aclose()
            await built.database.dispose()

    # --- Siembra -------------------------------------------------------------------------------

    def site(
        self,
        *,
        assigned_since: dt.timedelta = 30 * DAY,
        usage_approved: bool = True,
        retention_days: int | None = None,
    ) -> IngestSite:
        authz = self.authz
        site = authz.add_site(plants=1, zones_per_plant=2)
        ((plant, zones),) = site.plants.items()
        zone, other_zone = zones
        now = self.now()
        node_id = uuid.uuid4()
        self.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node_id,
            site.organization_id,
            plant,
            f"ND-{secrets.token_hex(6).upper()}",
            now - 400 * DAY,
        )
        self.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid.uuid4(),
            site.organization_id,
            plant,
            zone,
            node_id,
            now - assigned_since,
            authz.operator_id,
        )
        self.execute(
            "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id, declared_at,"
            " declared_by, enrolled_at, hardware_fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            node_id,
            site.organization_id,
            plant,
            now - 400 * DAY,
            authz.operator_id,
            now - 399 * DAY,
            secrets.token_hex(32),
        )
        if retention_days is not None:
            self.execute(
                "INSERT INTO fleet.node_configuration (node_id, organization_id, plant_id,"
                " time_sources, sent_records_retention_days, updated_at)"
                " VALUES ($1, $2, $3, '[\"ntp_local\"]', $4, $5)",
                node_id,
                site.organization_id,
                plant,
                retention_days,
                now,
            )
        db_node = DbNode(node_id, site.organization_id, plant, zone, authz.operator_id)
        certificate, _ = self.run(issue(authz.sessions.admin, self.authority, db_node, now - DAY))
        result = IngestSite(
            organization_id=site.organization_id,
            plant_id=plant,
            node_id=node_id,
            zone_id=zone,
            other_zone_id=other_zone,
            camera_id=uuid.uuid4(),
            signal_id=uuid.uuid4(),
            standard_id=uuid.uuid4(),
            certificate=certificate,
        )
        self.publish_catalog(result, now - 400 * DAY)
        if usage_approved:
            self.usage(result, approved=True, at=now - 400 * DAY)
        return result

    def catalog(self, site: IngestSite, issued_at: dt.datetime, version: int = 1) -> dict[str, Any]:
        document = {
            "zone_id": str(site.zone_id),
            "zone_code": "ZN-01",
            "plant_id": str(site.plant_id),
            "organization_id": str(site.organization_id),
            "version": version,
            "issued_at": format_timestamp(issued_at),
            "cameras": [
                {
                    "camera_id": str(site.camera_id),
                    "code": "CAM-01",
                    "role_in_zone": "primary",
                    "declared_min_fps": 5.0,
                }
            ],
            "minimum_coverage": {"required_count": 1, "required_camera_ids": [str(site.camera_id)]},
            "signals": [
                {
                    "signal_id": str(site.signal_id),
                    "code": "SIG-01",
                    "role": "energy",
                    "asserted_level": "high",
                    "source": {"reader": "a", "channel": 0},
                    "description_es": "Señal de energía de la prensa",
                }
            ],
            "standards": [
                {
                    "standard_id": str(site.standard_id),
                    "version": version,
                    "family": "coexistence",
                    "title_es": "Zona de prensa",
                    "declared_text": "Ninguna persona dentro del perímetro con la máquina energizada.",
                    "declared_by": {
                        "user_id": str(self.authz.operator_id),
                        "display_name": "Coordinación SST sintética",
                        "role": "coordinator_sst",
                    },
                    "effective_from": format_timestamp(issued_at),
                    "tier_policy": "tier_1_when_signal_valid",
                    "predicate": {
                        "all_of": [
                            {"presence": True},
                            {"signal_role": "energy", "value": "asserted"},
                        ],
                        "min_duration_ms": 0,
                    },
                }
            ],
            "thresholds": {"review": REVIEW, "publication": PUBLICATION},
            "clip_window": {
                "pre_seconds": CLIP_WINDOW_SECONDS,
                "post_seconds": CLIP_WINDOW_SECONDS,
            },
            "episode": {"grouping_window_ms": 3000, "max_segment_ms": 60000},
            "commissioning_watermark": True,
        }
        api.parse_zone_catalog(json.dumps(document).encode())
        return document

    def publish_catalog(
        self, site: IngestSite, issued_at: dt.datetime, version: int = 1
    ) -> dict[str, Any]:
        """La versión ``version`` vigente desde ``issued_at`` (cierra la anterior en ese instante)."""
        payload = self.catalog(site, issued_at, version)
        if version > 1:
            self.execute(
                "UPDATE catalog.zone_catalog_version SET superseded_at = $3"
                " WHERE zone_id = $1 AND catalog_version = $2",
                site.zone_id,
                version - 1,
                issued_at,
            )
        self.execute(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, 'administrator', 'Catálogo sintético de la ingesta',"
            " ARRAY['standards'], $7::jsonb, $8::jsonb, false, $9)",
            site.organization_id,
            site.plant_id,
            site.zone_id,
            version,
            issued_at,
            self.authz.operator_id,
            json.dumps(payload),
            json.dumps({"payload": payload}),
            uuid.uuid4(),
        )
        return payload

    def usage(self, site: IngestSite, *, approved: bool, at: dt.datetime) -> None:
        """La compuerta de uso pasa a ``approved`` o ``revoked`` en ``at`` (cierra la abierta)."""
        self.execute(
            "UPDATE catalog.gate_state_history SET effective_until = $2"
            " WHERE zone_id = $1 AND gate = 'usage' AND effective_until IS NULL",
            site.zone_id,
            at,
        )
        self.execute(
            "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
            " status, effective_from, decided_by, reason_es, ledger_record_id, record_id)"
            " VALUES ($1, $2, $3, 'usage', $4, $5, $6, $7, $8, $9)",
            site.organization_id,
            site.plant_id,
            site.zone_id,
            "approved" if approved else "revoked",
            at,
            self.authz.operator_id,
            None if approved else "Revocación sintética del uso de la zona",
            uuid.uuid4(),
            uuid.uuid4(),
        )

    def grant(self, site: IngestSite, clip: Mapping[str, Any], issued_at: dt.datetime) -> None:
        """La concesión ``issued`` del clip (emitida en ``issued_at``)."""
        self.execute(
            "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
            " node_id, purpose, storage_key, content_type, max_size_bytes, required_headers,"
            " issued_at, expires_at, status)"
            " VALUES ($1, $2, $3, $4, $5, 'evidence', $6, 'video/mp4', $7, $8::jsonb, $9,"
            " $9::timestamptz + interval '15 minutes', 'issued')",
            uuid.UUID(str(clip["clip_id"])),
            site.organization_id,
            site.plant_id,
            site.zone_id,
            site.node_id,
            str(clip["storage_key"]),
            int(clip["size_bytes"]),
            json.dumps(
                {
                    "content-type": "video/mp4",
                    "x-amz-checksum-sha256": base64.b64encode(
                        bytes.fromhex(str(clip["sha256"]))
                    ).decode(),
                    "x-amz-meta-vigia-anonymized": "1",
                }
            ),
            issued_at,
        )

    # --- Presentaciones ------------------------------------------------------------------------

    def _clip(
        self, site: IngestSite, zone: uuid.UUID, started: dt.datetime, ended: dt.datetime
    ) -> dict[str, Any]:
        clip_id = self.uuid7()
        data = secrets.token_bytes(64)
        window = dt.timedelta(seconds=CLIP_WINDOW_SECONDS)
        return {
            "clip_id": str(clip_id),
            "camera_id": str(site.camera_id),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "duration_ms": int((ended - started + 2 * window).total_seconds() * 1000),
            "starts_at": format_timestamp(started - window),
            "ends_at": format_timestamp(ended + window),
            "segment": "full",
            "anonymized": True,
            "storage_key": (
                f"org/{site.organization_id}/plant/{site.plant_id}/zone/{zone}"
                f"/node/{site.node_id}/{clip_id}.mp4"
            ),
        }

    def _common(
        self,
        site: IngestSite,
        started: dt.datetime,
        *,
        zone: uuid.UUID | None,
        duration: dt.timedelta,
        synchronized: bool,
        offset_ms: int,
    ) -> tuple[dict[str, Any], dict[str, Any], uuid.UUID, dt.datetime]:
        zone_id = zone or site.zone_id
        ended = started + duration
        common = {
            "contract_version": VERSION,
            "organization_id": str(site.organization_id),
            "plant_id": str(site.plant_id),
            "zone_id": str(zone_id),
            "node_id": str(site.node_id),
        }
        node_time = {
            "started_at": format_timestamp(started),
            "ended_at": format_timestamp(ended),
            "clock": {"synchronized": synchronized, "offset_ms": offset_ms, "source": "ntp.local"},
        }
        return common, node_time, zone_id, ended

    def finding(
        self,
        site: IngestSite,
        started: dt.datetime | None = None,
        *,
        zone: uuid.UUID | None = None,
        duration: dt.timedelta = dt.timedelta(seconds=30),
        synchronized: bool = True,
        offset_ms: int = 12,
        standard_version: int = 1,
        store: bool = True,
        grant: bool = True,
    ) -> dict[str, Any]:
        """Un ``FindingSubmission`` válido de ``site``, con su clip en el almacén y su concesión."""
        started = started if started is not None else self.now() - dt.timedelta(minutes=10)
        common, node_time, zone_id, ended = self._common(
            site,
            started,
            zone=zone,
            duration=duration,
            synchronized=synchronized,
            offset_ms=offset_ms,
        )
        clip = self._clip(site, zone_id, started, ended)
        milliseconds = int(duration.total_seconds() * 1000)
        document = {
            "finding_id": str(self.uuid7()),
            **common,
            "episode": {
                "episode_id": str(self.uuid7()),
                "segment_index": 0,
                "continues": False,
                "max_segment_ms": 60000,
            },
            "family": "coexistence",
            "standard": {"standard_id": str(site.standard_id), "version": standard_version},
            "tier": "tier_1",
            "node_time": node_time,
            "signals": [
                {
                    "signal_id": str(site.signal_id),
                    "role": "energy",
                    "value": "asserted",
                    "valid": True,
                    "read_at": format_timestamp(started),
                }
            ],
            "observability_state": "observable",
            "cameras": [{"camera_id": str(site.camera_id), "originating": True, "clips": [clip]}],
            "max_confidence": 0.9,
            "episode_duration_ms": milliseconds,
            "condition_duration_ms": milliseconds,
            "automatic_classification": {
                "trigger": "publication_threshold",
                "corroborating_camera_ids": [],
            },
            "model_version": MODEL_VERSION,
            "software_version": SOFTWARE_VERSION,
        }
        api.parse_finding_submission(json.dumps(document).encode())
        self._prepare_clip(site, clip, store=store, grant=grant and zone_id == site.zone_id)
        return document

    def detection(
        self,
        site: IngestSite,
        started: dt.datetime | None = None,
        *,
        zone: uuid.UUID | None = None,
        duration: dt.timedelta = dt.timedelta(seconds=30),
        store: bool = True,
        grant: bool = True,
    ) -> dict[str, Any]:
        """Un ``DetectionForReviewSubmission`` válido (banda de revisión del catálogo)."""
        started = started if started is not None else self.now() - dt.timedelta(minutes=10)
        common, node_time, zone_id, ended = self._common(
            site, started, zone=zone, duration=duration, synchronized=True, offset_ms=12
        )
        clip = self._clip(site, zone_id, started, ended)
        milliseconds = int(duration.total_seconds() * 1000)
        document = {
            "detection_id": str(self.uuid7()),
            **common,
            "episode": {
                "episode_id": str(self.uuid7()),
                "segment_index": 0,
                "continues": False,
                "max_segment_ms": 60000,
            },
            "family": "coexistence",
            "standard": {"standard_id": str(site.standard_id), "version": 1},
            "node_time": node_time,
            "signals": [
                {
                    "signal_id": str(site.signal_id),
                    "role": "energy",
                    "value": "asserted",
                    "valid": True,
                    "read_at": format_timestamp(started),
                }
            ],
            "observability_state": "observable",
            "cameras": [{"camera_id": str(site.camera_id), "originating": True, "clips": [clip]}],
            "max_confidence": 0.5,
            "review_threshold": REVIEW,
            "publication_threshold": PUBLICATION,
            "episode_duration_ms": milliseconds,
            "condition_duration_ms": milliseconds,
            "model_version": MODEL_VERSION,
            "software_version": SOFTWARE_VERSION,
        }
        api.parse_detection_for_review_submission(json.dumps(document).encode())
        self._prepare_clip(site, clip, store=store, grant=grant and zone_id == site.zone_id)
        return document

    def event(
        self,
        site: IngestSite,
        started: dt.datetime | None = None,
        *,
        opened_event_id: str | None = None,
        zone: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Un ``ObservabilityEventSubmission`` de cámara: apertura, o cierre de ``opened_event_id``."""
        started = started if started is not None else self.now() - dt.timedelta(minutes=10)
        common, node_time, _, ended = self._common(
            site,
            started,
            zone=zone,
            duration=dt.timedelta(seconds=30),
            synchronized=True,
            offset_ms=12,
        )
        document: dict[str, Any] = {
            "event_id": str(self.uuid7()),
            **common,
            "subject": {"kind": "camera", "camera_id": str(site.camera_id)},
            "started_at": format_timestamp(started),
            "node_time": node_time,
            "evidence": [],
            "software_version": SOFTWARE_VERSION,
        }
        if opened_event_id is None:
            document |= {"phase": "opened", "state": "degraded", "causes": ["obstruction"]}
        else:
            document |= {
                "phase": "closed",
                "opened_event_id": opened_event_id,
                "state": "observable",
                "causes": [],
                "ended_at": format_timestamp(ended),
            }
        api.parse_observability_event_submission(json.dumps(document).encode())
        return document

    def _prepare_clip(
        self, site: IngestSite, clip: Mapping[str, Any], *, store: bool, grant: bool
    ) -> None:
        if store:
            self.storage.put(clip)
        if grant:
            self.grant(site, clip, self.now() - dt.timedelta(minutes=12))

    # --- Peticiones ----------------------------------------------------------------------------

    async def send(
        self,
        site: IngestSite,
        kind: IngestKind,
        document: Mapping[str, Any],
        instance: IngestInstance | None = None,
    ) -> httpx.Response:
        client = (instance or self.primary).client
        response: httpx.Response = await client.post(
            ROUTE_OF[kind].path,
            content=json.dumps(document).encode(),
            headers=site.headers(document, kind),
        )
        return response

    def post(
        self,
        site: IngestSite,
        kind: IngestKind,
        document: Mapping[str, Any],
        instance: IngestInstance | None = None,
    ) -> httpx.Response:
        response: httpx.Response = self.run(self.send(site, kind, document, instance))
        return response

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def records(self, record_type: str, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, plant_id, scope_zone_id, scope_node_id, actor_kind, received_at,"
                " occurred_at, correlation_id, ledger.vigia_bytes_to_jsonb(content)::text AS content"
                " FROM ledger.ledger_record WHERE record_type = $1 AND organization_id = $2"
                " ORDER BY chain_sequence",
                record_type,
                organization_id,
            )
        ]

    def events(self, event_name: str, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {**dict(row), "payload": json.loads(row["payload"])}
            for row in self.fetch(
                "SELECT event_id, plant_id, ledger_sequence, payload::text AS payload"
                " FROM shared.outbox_event WHERE event_name = $1 AND organization_id = $2"
                " ORDER BY ledger_sequence",
                event_name,
                organization_id,
            )
        ]

    def deliveries(self, organization_id: uuid.UUID) -> int:
        (row,) = self.fetch(
            "SELECT count(*) AS n FROM shared.outbox_delivery AS d JOIN shared.outbox_event AS e"
            " ON e.event_id = d.event_id WHERE e.organization_id = $1",
            organization_id,
        )
        return int(row["n"])

    def audit(self, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {
                **dict(row),
                "filters": None if row["filters"] is None else json.loads(bytes(row["filters"])),
            }
            for row in self.fetch(
                "SELECT operation, outcome, scope_plant_id, scope_zone_id, resource_kind,"
                " resource_id, filters, correlation_id FROM shared.audit_entry"
                " WHERE organization_id = $1 AND operation = 'ingest_rejected'"
                " ORDER BY chain_sequence",
                organization_id,
            )
        ]

    def grant_status(self, clip: Mapping[str, Any]) -> tuple[str, Any]:
        (row,) = self.fetch(
            "SELECT status, used_at FROM fleet.clip_upload_grant WHERE clip_id = $1",
            uuid.UUID(str(clip["clip_id"])),
        )
        return str(row["status"]), row["used_at"]

    def orphan_closes(self, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT event_id, opened_event_id, zone_id, node_id, ledger_record_id, received_at"
                " FROM fleet.observability_orphan_close WHERE organization_id = $1",
                organization_id,
            )
        ]

    def evidence(self, record_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT clip_id, zone_id, node_id FROM ledger.evidence WHERE record_id = $1",
                record_id,
            )
        ]


@contextlib.contextmanager
def ingest_stack(endpoint: PostgresEndpoint, prefix: str) -> Iterator[IngestStack]:
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(now["now"])
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)
        _fleet_record_types(registry)
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        _fleet_event_types(catalog.event_types)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), sessions.clock)
            registry.seal()

        authz.run(synchronize())
        free_text = FreeTextPolicyRegistry()
        register_u03_free_text_validator(free_text)
        free_text.seal()
        stack = IngestStack(
            authz=authz,
            registry=registry,
            free_text=free_text,
            outbox=Outbox(catalog, sessions.clock),
            storage=EvidenceStore(),
            authority=TestAuthority(),
        )
        stack.primary = stack.instance()
        try:
            yield stack
        finally:
            authz.run(stack.close())
