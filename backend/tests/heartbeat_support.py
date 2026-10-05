"""Entorno de las pruebas del latido y del catálogo por zona (TASK-223) sobre PostgreSQL 16 real.

``heartbeat_stack`` levanta, sobre una base migrada propia y como ``vigia_app``:

- el ``EscritorExpediente`` con los tipos de U-02, los de la flota que se escriben y
  ``walk_test_regression_marked``; la bandeja con los eventos de U-02, de la flota y del catálogo;
- los servicios reales que usa el latido: ``GateService`` (renovación A-55), ``RegressionService``,
  ``HierarchyService`` (``update_node``), ``LiveViewTokenService`` (``incorporate``), y
  ``HeartbeatService`` y ``ZoneCatalogForNode``;
- un **doble del puerto de firma** (``CountingSigner``) que delega en el ``SigningService`` real y
  cuenta cada ``sign``, puede caerse (``down``) o retener la firma (``gate``); el conjunto de claves
  sale de la caché del servicio real, sin firmar;
- la ``NodeApiGate`` real (identidad del nodo por certificado contra la base, sin límite de tasa:
  no es lo que se prueba) y la aplicación con la cadena fija y solo las dos rutas de TASK-223.

``instance(database)`` construye **otra instancia** completa (servicios, ``NodeApiGate`` y
aplicación) sobre otra ``Database``: dos instancias no comparten nada en memoria (NFR-GOB-15).

``site`` siembra un nodo dado de alta con sus zonas asignadas, el catálogo vigente de cada zona
(cámaras y cobertura mínima) y el ``SignedEnvelope<GateState>`` firmado y guardado por el
repositorio real de compuertas. Solo datos generados (NFR-CTR-43). El reloj simulado arranca en la
hora de la base (retro 14); topes de la base de 60 s y de firma de 120 s (retro 15).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import secrets
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from cryptography import x509
from vigia_contracts.versioning import Version

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, authz_environment
from tests.examples.test_ledger_routes import StubEvidenceStorage
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_db import DbNode, issue
from tests.node_api_support import DAY, VERSION, TestAuthority, alb_headers, node_unit
from tests.outbox_support import app_database
from tests.signing_support import SigningWorld, bootstrapped_world
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.gate_repository import PostgresGateRepository
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.catalog.application.gates import GateService
from vigia_platform.catalog.application.regression import MARKED_RECORD_TYPE, RegressionService
from vigia_platform.catalog.domain.gates import ZoneGateState, gate_state_payload
from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.application.heartbeat import HeartbeatDependencies, HeartbeatService
from vigia_platform.fleet.application.zone_catalog_for_node import ZoneCatalogForNode
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.authz.context import PresentedNode
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.node_api.certificate_profile import serial_hex
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import NodeApiGate
from vigia_platform.node_api.routes.heartbeats import heartbeat_operation
from vigia_platform.node_api.routes.zone_catalogs import zone_catalog_operation
from vigia_platform.node_api.versioning import RetiringMinor, VersionPolicy
from vigia_platform.shared.api.declarations import NODE_GATE_STATE_KEY, NodeRoute
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import Database
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.ratelimit import Allowed, Budget, Limited, RateLimiter
from vigia_platform.shared.runtime.units import _fleet_event_types, _fleet_record_types
from vigia_platform.shared.signing import SigningKeyUnavailable, SigningPurpose
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.tokens import LiveViewTokenService

__all__ = [
    "LOCK_TIMEOUT_MS",
    "ROUTES",
    "CountingSigner",
    "HeartbeatStack",
    "Instance",
    "NodeSite",
    "heartbeat_stack",
]

LOCK_TIMEOUT_MS: Final = 60_000
SIGN_TIMEOUT_SECONDS: Final = 120.0
GATE_WAIT_SECONDS: Final = 60.0
ROUTES: Final = (NodeRoute.HEARTBEAT, NodeRoute.ZONE_CATALOG)
MODEL_VERSION: Final = "modelo-1.0"
SOFTWARE_VERSION: Final = "1.4.0"
LIVE_VIEW_URL: Final = "https://192.168.10.20:8443/"
SYNTHETIC_SIGNATURE: Final = "A" * 86 + "=="


class CountingSigner:
    """El puerto de firma: delega en el ``SigningService`` real y cuenta cada ``sign``."""

    def __init__(self, world: SigningWorld) -> None:
        self.world = world
        self.calls = 0
        self.down = False
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any:
        with self._lock:
            self.calls += 1
        self.entered.set()
        if self.down:
            raise SigningKeyUnavailable(purpose)
        gate = self.gate
        if gate is not None:
            gate.wait(GATE_WAIT_SECONDS)
        return self.world.service.sign(purpose, payload)

    def sign_detached(self, purpose: SigningPurpose, message: bytes) -> Any:
        return self.world.service.sign_detached(purpose, message)

    def public_keys(self, purpose: SigningPurpose) -> Any:
        return self.world.service.public_keys(purpose)

    def current_key_set_envelope(self) -> Any:
        return self.world.service.current_key_set_envelope()


class UnlimitedLimiter(RateLimiter):
    """Sin límite de tasa: no es lo que prueban estas pruebas (TASK-206 lo prueba)."""

    def check(self, key: str, budget: Budget) -> Allowed | Limited:
        return Allowed()


@dataclass(frozen=True)
class NodeSite:
    """Un nodo dado de alta con sus zonas asignadas, su catálogo y sus compuertas."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    node_id: uuid.UUID
    zones: tuple[uuid.UUID, ...]
    cameras: Mapping[uuid.UUID, tuple[uuid.UUID, ...]]
    certificate: x509.Certificate

    def headers(self, version: str = VERSION) -> dict[str, str]:
        return {**alb_headers(self.certificate), "X-Vigia-Contract-Version": version}

    def all_cameras(self) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for zone in self.zones:
            seen += [camera for camera in self.cameras[zone] if camera not in seen]
        return seen


@dataclass
class Instance:
    """Una instancia de ``vigia-api`` con sus servicios, su ``NodeApiGate`` y su aplicación."""

    database: Database
    service: HeartbeatService
    catalogs: ZoneCatalogForNode
    gates: GateService
    gate: NodeApiGate
    app: Any
    client: httpx.AsyncClient


@dataclass
class HeartbeatStack:
    authz: AuthzEnvironment
    signing: SigningWorld
    signer: CountingSigner
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry
    outbox: Outbox
    policy: VersionPolicy
    authority: TestAuthority
    metrics: PlatformMetrics | None = None
    primary: Instance = field(init=False)
    _extra: list[Instance] = field(default_factory=list)

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    def now(self) -> dt.datetime:
        return self.authz.now()

    def uuid7(self) -> uuid.UUID:
        return uuid7(self.authz.sessions.clock)

    def tick(self, seconds: float = 1.0) -> None:
        self.authz.sessions.clock.advance(seconds)

    # --- Instancias ----------------------------------------------------------------------------

    def database(self, **changes: Any) -> Database:
        fields: dict[str, Any] = {"worker_pool_size": 8, "lock_timeout_ms": LOCK_TIMEOUT_MS}
        fields.update(changes)
        return app_database(self.authz.sessions.migrated, **fields)

    def instance(
        self,
        database: Database | None = None,
        *,
        policy: VersionPolicy | None = None,
        **service_changes: Any,
    ) -> Instance:
        """Otra instancia completa sobre ``database`` (una nueva si es ``None``); ``policy`` es la
        política de versiones de su ``NodeApiGate`` y de su ``contract_notice``."""
        database = database if database is not None else self.database()
        policy = policy if policy is not None else self.policy
        sessions = self.authz.sessions
        clock = sessions.clock
        catalog = PostgresCatalogRepository(database)
        gates = GateService(
            repository=PostgresGateRepository(database),
            catalog=catalog,
            agreements=PostgresAgreementRepository(),
            database=database,
            writer=self.writer,
            authorizer=self.authz.authorizer,
            audit=sessions.audit,
            free_text=self.free_text,
            signer=self.signer,
            clock=clock,
            sign_timeout_seconds=SIGN_TIMEOUT_SECONDS,
        )
        regression = RegressionService(
            repository=PostgresRegressionRepository(database),
            catalog=catalog,
            database=database,
            writer=self.writer,
            authorizer=self.authz.authorizer,
            audit=sessions.audit,
            free_text=self.free_text,
            clock=clock,
        )
        hierarchy = HierarchyService(
            IdentityDependencies(
                database=database,
                writer=self.writer,
                audit=sessions.audit,
                outbox=self.outbox,
                authorizer=self.authz.authorizer,
                free_text=self.free_text,
                clock=clock,
                provider_organization_id=self.authz.provider_organization_id,
            )
        )
        live_view = LiveViewTokenService(
            database=database,
            authorizer=self.authz.authorizer,
            audit=sessions.audit,
            outbox=self.outbox,
            signer=self.signer,
            clock=clock,
        )

        def retires_at(version: str) -> str | None:
            return policy.retires_at(Version.parse(version))

        fields: dict[str, Any] = {
            "database": database,
            "writer": self.writer,
            "clock": clock,
            "key_sets": self.signer,
            "gates": gates,
            "regression": regression,
            "identity": hierarchy,
            "live_view": live_view,
            "retires_at": retires_at,
            "nodes": PostgresNodeFleetStore(database),
            "metrics": self.metrics,
        }
        fields.update(service_changes)
        service = HeartbeatService(HeartbeatDependencies(**fields))
        catalogs = ZoneCatalogForNode(database=database)
        gate = NodeApiGate(
            identity=NodeIdentity(
                contexts=self.authz.contexts, store=PostgresNodeContextStore(database)
            ),
            limits=NodeRateLimits(UnlimitedLimiter(clock)),
            clock=clock,
            responses=NodeResponses(clock),
            policy=policy,
            operations={
                NodeRoute.HEARTBEAT: heartbeat_operation(service),
                NodeRoute.ZONE_CATALOG: zone_catalog_operation(catalogs),
            },
        )
        app = World(clock=clock).app(
            units=(node_unit(ROUTES),), runtime={"state": {NODE_GATE_STATE_KEY: gate}}
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=120.0
        )
        built = Instance(database, service, catalogs, gates, gate, app, client)
        self._extra.append(built)
        return built

    async def close(self) -> None:
        for built in self._extra:
            await built.client.aclose()
            await built.database.dispose()

    # --- Siembra -------------------------------------------------------------------------------

    def site(
        self,
        zones: int = 1,
        cameras: int = 2,
        *,
        required: int | None = None,
        gate_issued_at: dt.datetime | None = None,
        gates: bool = True,
        catalogs: bool = True,
        interval: int | None = None,
    ) -> NodeSite:
        """Nodo dado de alta con ``zones`` zonas asignadas, ``cameras`` cámaras por zona (la
        primera requerida; ``required`` el ``required_count``, todas por omisión), catálogo
        vigente y sobre de compuertas emitido en ``gate_issued_at`` (ahora por omisión)."""
        authz = self.authz
        site = authz.add_site(plants=1, zones_per_plant=zones)
        ((plant, zone_ids),) = site.plants.items()
        now = self.now()
        node_id = uuid.uuid4()
        admin = authz.sessions.admin
        self.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node_id,
            site.organization_id,
            plant,
            f"ND-{secrets.token_hex(6).upper()}",
            now - 31 * DAY,
        )
        for zone in zone_ids:
            self.execute(
                "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
                " plant_id, zone_id, node_id, assigned_at, assigned_by)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                uuid.uuid4(),
                site.organization_id,
                plant,
                zone,
                node_id,
                now - 30 * DAY,
                authz.operator_id,
            )
        self.execute(
            "INSERT INTO fleet.node_fleet_record (node_id, organization_id, plant_id, declared_at,"
            " declared_by, enrolled_at, hardware_fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            node_id,
            site.organization_id,
            plant,
            now - 31 * DAY,
            authz.operator_id,
            now - 30 * DAY,
            secrets.token_hex(32),
        )
        if interval is not None:
            self.execute(
                "INSERT INTO fleet.node_configuration (node_id, organization_id, plant_id,"
                " time_sources, heartbeat_interval_seconds, mute_after_seconds, updated_at)"
                " VALUES ($1, $2, $3, '[\"ntp_local\"]', $4, $5, $6)",
                node_id,
                site.organization_id,
                plant,
                interval,
                5 * interval,
                now,
            )
        db_node = DbNode(node_id, site.organization_id, plant, zone_ids[0], authz.operator_id)
        certificate, _ = self.run(issue(admin, self.authority, db_node, now - DAY))
        camera_map: dict[uuid.UUID, tuple[uuid.UUID, ...]] = {}
        for zone in zone_ids:
            camera_map[zone] = tuple(uuid.uuid4() for _ in range(cameras))
            if catalogs:
                self.publish_catalog(
                    site.organization_id, plant, zone, camera_map[zone], required=required
                )
            if gates:
                self.store_gate(site.organization_id, plant, zone, gate_issued_at or now)
        return NodeSite(site.organization_id, plant, node_id, zone_ids, camera_map, certificate)

    def publish_catalog(
        self,
        organization_id: uuid.UUID,
        plant: uuid.UUID,
        zone: uuid.UUID,
        cameras: Sequence[uuid.UUID],
        *,
        version: int = 1,
        required: int | None = None,
    ) -> None:
        """Versión ``version`` vigente del catálogo de la zona, con un sobre guardado (sintético:
        la ruta lo sirve sin mirarlo)."""
        payload = {
            "zone_id": str(zone),
            "catalog_version": version,
            "cameras": [
                {"camera_id": str(camera), "role_in_zone": "primary", "declared_min_fps": 5.0}
                for camera in cameras
            ],
            "minimum_coverage": {
                "required_count": len(cameras) if required is None else required,
                "required_camera_ids": [str(cameras[0])],
            },
        }
        envelope = {
            "payload": payload,
            "payload_canonical_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
            "signature": SYNTHETIC_SIGNATURE,
            "key_id": "catalog-sintetica",
            "signed_at": format_timestamp(self.now()),
        }
        if version > 1:
            self.execute(
                "UPDATE catalog.zone_catalog_version SET superseded_at = $3"
                " WHERE zone_id = $1 AND catalog_version = $2",
                zone,
                version - 1,
                self.now(),
            )
        self.execute(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, 'administrator', 'Catálogo sintético del latido',"
            " ARRAY['cameras'], $7::jsonb, $8::jsonb, false, $9)",
            organization_id,
            plant,
            zone,
            version,
            self.now(),
            self.authz.operator_id,
            json.dumps(payload),
            json.dumps(envelope),
            uuid.uuid4(),
        )

    def store_gate(
        self, organization_id: uuid.UUID, plant: uuid.UUID, zone: uuid.UUID, issued: dt.datetime
    ) -> None:
        """El ``SignedEnvelope<GateState>`` de la zona, firmado por el servicio real (sin pasar
        por el doble que cuenta) y guardado con el repositorio de compuertas."""
        state = dataclasses.replace(
            ZoneGateState.initial(organization_id, plant, zone), issued_at=issued
        )
        envelope = self.signing.service.sign(
            SigningPurpose.GATE, gate_state_payload(state, issued)
        ).to_json_value()
        context = unit_context(organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)

        async def save() -> None:
            async with self.primary.database.transaction(context) as transaction:
                await PostgresGateRepository(self.primary.database).save_state(
                    transaction, state, envelope
                )

        self.run(save())

    # --- Peticiones ----------------------------------------------------------------------------

    def body(self, site: NodeSite, **changes: Any) -> dict[str, Any]:
        """Un ``Heartbeat`` válido de ``site`` (``heartbeat_id`` nuevo; ``changes`` lo altera)."""
        now = self.now()
        body: dict[str, Any] = {
            "heartbeat_id": str(self.uuid7()),
            "contract_version": VERSION,
            "organization_id": str(site.organization_id),
            "plant_id": str(site.plant_id),
            "node_id": str(site.node_id),
            "sent_at": format_timestamp(now),
            "node_clock": {"synchronized": True, "offset_ms": 12, "source": "ntp_local"},
            "software_version": SOFTWARE_VERSION,
            "model_version": MODEL_VERSION,
            "uptime_seconds": 3600,
            "cameras": [
                {
                    "camera_id": str(camera),
                    "connected": True,
                    "measured_fps": 12.0,
                    "observability_state": "observable",
                    "declared_min_fps": 5.0,
                }
                for camera in site.all_cameras()
            ],
            "zones": [
                {
                    "zone_id": str(zone),
                    "mode": "productive",
                    "observability_state": "observable",
                    "catalog_version": 1,
                    "gate_state_valid_until": format_timestamp(now + 7 * DAY),
                    "open_episodes": 0,
                }
                for zone in site.zones
            ],
            "signal_reader": {"available": True, "adapter": "modbus_rtu"},
            "local_queue": {"pending": 3, "dead_letter": [], "retained_sent": 10},
        }
        body.update(changes)
        return body

    async def send(
        self,
        site: NodeSite,
        body: Mapping[str, Any],
        instance: Instance | None = None,
        *,
        version: str = VERSION,
    ) -> httpx.Response:
        client = (instance or self.primary).client
        response: httpx.Response = await client.post(
            NodeRoute.HEARTBEAT.path,
            content=json.dumps(body).encode(),
            headers={**site.headers(version), "Content-Type": "application/json"},
        )
        return response

    def post(
        self,
        site: NodeSite,
        body: Mapping[str, Any] | None = None,
        instance: Instance | None = None,
        *,
        version: str = VERSION,
    ) -> httpx.Response:
        response: httpx.Response = self.run(
            self.send(
                site, body if body is not None else self.body(site), instance, version=version
            )
        )
        return response

    def get_catalog(
        self, site: NodeSite, zone: uuid.UUID | str, instance: Instance | None = None
    ) -> httpx.Response:
        client = (instance or self.primary).client
        response: httpx.Response = self.run(
            client.get(f"/api/nodes/zones/{zone}/catalog", headers=site.headers())
        )
        return response

    def node_scope(self, site: NodeSite) -> Any:
        """El ``NodeScope`` de ``site`` como lo resuelve la verificación previa (A-51)."""
        presented = PresentedNode(
            node_id=site.node_id,
            organization_id=site.organization_id,
            plant_id=site.plant_id,
            certificate_serial=serial_hex(site.certificate.serial_number),
        )
        return self.run(self.primary.gate.identity.resolve(presented, self.uuid7()))

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def inventory(self, node: uuid.UUID) -> dict[str, Any] | None:
        rows = self.fetch(
            "SELECT software_version, contract_version, model_version, contract_notice::text"
            " AS contract_notice, last_heartbeat_at, communication_state, local_queue::text AS"
            " local_queue, clock::text AS clock, signal_reader::text AS signal_reader,"
            " uptime_seconds, updated_at FROM fleet.node_inventory WHERE node_id = $1",
            node,
        )
        if not rows:
            return None
        row = dict(rows[0])
        for column in ("contract_notice", "local_queue", "clock", "signal_reader"):
            row[column] = json.loads(row[column])
        return row

    def cameras(self, node: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT camera_id, connected, measured_fps, declared_min_fps,"
                " observability_state, updated_at FROM fleet.camera_inventory WHERE node_id = $1"
                " ORDER BY camera_id",
                node,
            )
        ]

    def zones(self, node: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT zone_id, mode, observability_state, catalog_version_in_node,"
                " gate_state_valid_until, open_episodes, coverage_ok, updated_at"
                " FROM fleet.zone_node_state WHERE node_id = $1 ORDER BY zone_id",
                node,
            )
        ]

    def history(self, node: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {**dict(row), "payload_summary": json.loads(row["payload_summary"])}
            for row in self.fetch(
                "SELECT heartbeat_id, received_at, sent_at, payload_summary::text AS"
                " payload_summary FROM fleet.heartbeat_history WHERE node_id = $1"
                " ORDER BY received_at, heartbeat_id",
                node,
            )
        ]

    def records(self, record_type: str, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, plant_id, actor_kind,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
                " WHERE record_type = $1 AND organization_id = $2 ORDER BY chain_sequence",
                record_type,
                organization_id,
            )
        ]

    def communication(self, site: NodeSite) -> list[dict[str, Any]]:
        return [
            record["content"]
            for record in self.records("node_communication_state_changed", site.organization_id)
            if record["content"]["node_id"] == str(site.node_id)
        ]

    def regression_marks(self, site: NodeSite) -> list[dict[str, Any]]:
        return self.records(MARKED_RECORD_TYPE, site.organization_id)

    def urls(self, node: uuid.UUID) -> tuple[str | None, str | None]:
        (row,) = self.fetch(
            "SELECT f.live_view_local_url AS fleet, n.live_view_local_url AS identity"
            " FROM fleet.node_fleet_record AS f JOIN identity.node_identity AS n"
            " ON n.node_id = f.node_id WHERE f.node_id = $1",
            node,
        )
        return row["fleet"], row["identity"]

    def gate_text(self, zone: uuid.UUID) -> str:
        (row,) = self.fetch(
            "SELECT envelope::text AS envelope FROM catalog.zone_gate_state WHERE zone_id = $1",
            zone,
        )
        text: str = row["envelope"]
        return text

    def catalog_text(self, zone: uuid.UUID) -> bytes:
        (row,) = self.fetch(
            "SELECT envelope::text AS envelope FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 AND superseded_at IS NULL",
            zone,
        )
        value: str = row["envelope"]
        return value.encode("utf-8")

    def set_communication_state(self, node: uuid.UUID, state: str) -> None:
        """Lo que hará la tarea de mudo (TASK-224) sobre el inventario."""
        self.execute(
            "UPDATE fleet.node_inventory SET communication_state = $2 WHERE node_id = $1",
            node,
            state,
        )


def retiring_policy(version: str, retires_at: str) -> VersionPolicy:
    """La política con la menor de ``version`` en aviso de retiro hasta ``retires_at``."""
    return VersionPolicy(retiring=(RetiringMinor(version, retires_at),))


@contextlib.contextmanager
def heartbeat_stack(
    endpoint: PostgresEndpoint,
    prefix: str,
    *,
    policy: VersionPolicy | None = None,
    metrics: PlatformMetrics | None = None,
) -> Iterator[HeartbeatStack]:
    signing = asyncio.run(bootstrapped_world())
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(now["now"])
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)
        _fleet_record_types(registry)
        for definition in CATALOG_RECORD_TYPES:
            if definition.record_type == MARKED_RECORD_TYPE:
                registry.register(definition)
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        _fleet_event_types(catalog.event_types)
        register_catalog_event_types(catalog.event_types)

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
        writer_database = app_database(
            sessions.migrated, worker_pool_size=8, lock_timeout_ms=LOCK_TIMEOUT_MS
        )
        outbox = Outbox(catalog, sessions.clock)
        writer = EscritorExpediente(
            database=writer_database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(StubEvidenceStorage(), sessions.clock),
            outbox=outbox,
            clock=sessions.clock,
        )
        stack = HeartbeatStack(
            authz=authz,
            signing=signing,
            signer=CountingSigner(signing),
            writer=writer,
            free_text=free_text,
            outbox=outbox,
            policy=policy if policy is not None else VersionPolicy(),
            authority=TestAuthority(),
            metrics=metrics,
        )
        stack.primary = stack.instance(writer_database)
        try:
            yield stack
        finally:
            authz.run(stack.close())
