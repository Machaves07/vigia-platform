"""Entorno de integración del alta y la rotación del nodo (TASK-219; LC-GOB-11, LC-GOB-19).

Sobre ``fleet_stack`` (PostgreSQL 16 real como ``vigia_app``, servicios reales de TASK-218, el
``EscritorExpediente`` con los tipos de la flota y la bandeja con sus eventos):

- ``vigia-node-ca`` es ``MemoryKms`` (clave P-256 de prueba, A-47) con su raíz publicada en un
  ``RootStore`` en memoria (``ca/root.pem``); ``NodeCaIssuer`` real con plazo holgado (60 s, retro
  15) salvo en ``fragile`` (el de producción, 4 s), solo para las pruebas del fallo cerrado;
- las claves públicas de la plataforma salen de un ``SigningService`` real (``bootstrapped_world``
  arrancado en la hora de la base, el mismo reloj de todo lo que se compara); ``CountingSigning``
  cuenta cada ``sign`` para demostrar que el alta no firma nada;
- los sobres del catálogo y de la compuerta de cada zona se **firman** con ese servicio y se
  guardan con los repositorios de VIG-145 y VIG-146 (``PostgresCatalogRepository`` y
  ``PostgresGateRepository``), como haría su publicación;
- la aplicación de las rutas del contrato (``create_app`` con la cadena fija y la ``NodeApiGate``
  real con ``PostgresNodeContextStore``) con los manejadores de TASK-219 y la ruta de prueba
  interna de TASK-206 en las demás (``Probe``).

Solo datos generados (NFR-CTR-43). Topes de la base y de las esperas: 60 s (retro 15).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import secrets
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from vigia_contracts.models.enumerations import CameraRoleInZone

from tests.api_support import World
from tests.fleet_credentials_support import (
    MemoryKms,
    RootStore,
    csr_pem,
    local_ip,
    new_key,
    root_bundle_for,
)
from tests.fleet_http_support import FleetStack, fleet_stack
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import (
    VERSION,
    Probe,
    UnlimitedNodeLimiter,
    alb_headers,
    node_app,
    node_gate,
    probe_operations,
)
from tests.signing_support import SigningWorld, bootstrapped_world
from tests.writer_support import unit_context
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.gate_repository import PostgresGateRepository
from vigia_platform.catalog.domain.catalog_version import (
    InitialZoneParameters,
    NewStandard,
    StandardDraft,
    ZoneCatalogVersion,
    ZoneRef,
    plan_publication,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.gates import ZoneGateState, gate_state_payload
from vigia_platform.catalog.domain.standard import DeclaredBy
from vigia_platform.catalog.domain.zone_camera import ZoneCamera
from vigia_platform.fleet.adapters.ca.certificate_profiles import NodeCaIssuer
from vigia_platform.fleet.application.common import FleetDependencies
from vigia_platform.fleet.application.credential_rotation import CredentialRotationService
from vigia_platform.fleet.application.enrollment import EnrollmentService, FixedSourceKey
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.router import NodeApiGate, NodeOperation
from vigia_platform.node_api.routes.credential_rotations import credential_rotation_operation
from vigia_platform.node_api.routes.enrollment import enrollment_operation
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.context import ActorKind, ActorUnit, Role
from vigia_platform.shared.signing.keys import SigningKeyRecord, SigningPurpose

__all__ = [
    "ENROLLMENT_PATH",
    "INGEST_BASE_URL",
    "ROTATION_PATH",
    "BarrierIssuer",
    "CountingSigning",
    "Enrolled",
    "EnrollmentWorld",
    "NodeSetup",
    "enrollment_world",
]

ENROLLMENT_PATH: Final = NodeRoute.ENROLLMENT.path
ROTATION_PATH: Final = NodeRoute.CREDENTIAL_ROTATION.path
CATALOG_PATH: Final = "/api/nodes/zones/{zone}/catalog"
INGEST_BASE_URL: Final = "https://nodes.vigia.test/api/nodes"
LONG_SECONDS: Final = 60.0
REASON: Final = "Publicación sintética del catálogo de la zona"


class BarrierIssuer:
    """``NodeCaIssuer`` que retiene cada emisión hasta que ``parties`` han firmado: todas
    verificaron el código (o leyeron la credencial) y ninguna ha entrado en su transacción."""

    def __init__(self, target: NodeCaIssuer, parties: int) -> None:
        self.target = target
        self.barrier = asyncio.Barrier(parties)
        self.passes = 0

    async def issue(self, *args: Any, **kwargs: Any) -> Any:
        issued = await self.target.issue(*args, **kwargs)
        self.passes += 1
        async with asyncio.timeout(LONG_SECONDS):
            await self.barrier.wait()
        return issued


class CountingSigning:
    """``PlatformKeys`` sobre el ``SigningService`` real que cuenta cada ``sign`` (debe ser 0)."""

    def __init__(self, world: SigningWorld) -> None:
        self.world = world
        self.signs = 0
        self._lock = threading.Lock()

    def public_keys(self, purpose: SigningPurpose) -> tuple[SigningKeyRecord, ...]:
        return self.world.service.public_keys(purpose)

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any:
        with self._lock:
            self.signs += 1
        return self.world.service.sign(purpose, payload)


@dataclass
class NodeSetup:
    """Un nodo declarado con sus zonas y el instalador que lo declaró."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zones: tuple[uuid.UUID, ...]
    installer: Any
    fingerprint: str = field(default_factory=lambda: secrets.token_hex(32))


@dataclass
class Enrolled:
    """Lo que el nodo guarda de su alta: certificado, claves y la respuesta."""

    certificate: x509.Certificate
    client_key: ec.EllipticCurvePrivateKey
    server_key: ec.EllipticCurvePrivateKey
    body: Mapping[str, Any]

    @property
    def headers(self) -> dict[str, str]:
        return {**alb_headers(self.certificate), "X-Vigia-Contract-Version": VERSION}


@dataclass
class EnrollmentWorld:
    fleet: FleetStack
    signing: SigningWorld
    keys: CountingSigning
    kms: MemoryKms
    roots: RootStore
    root: x509.Certificate
    hash_key: bytes
    probe: Probe
    enrollment: EnrollmentService
    rotation: CredentialRotationService
    gate: NodeApiGate
    client: httpx.AsyncClient
    catalog: PostgresCatalogRepository = field(init=False)
    gates: PostgresGateRepository = field(init=False)

    def __post_init__(self) -> None:
        self.catalog = PostgresCatalogRepository(self.fleet.database)
        self.gates = PostgresGateRepository(self.fleet.database)

    # --- Básicos ------------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.fleet.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.fleet.fetch(sql, *args)

    def now(self) -> datetime:
        return self.fleet.authz.now()

    def advance(self, seconds: float) -> None:
        self.fleet.authz.sessions.clock.advance(seconds)

    def issuer(
        self, *, deadline: float = LONG_SECONDS, kms: Any = None, metrics: Any = None
    ) -> NodeCaIssuer:
        return NodeCaIssuer(
            kms=kms if kms is not None else self.kms,
            key_id=self.kms.key_id,
            roots=self.roots,
            clock=self.fleet.authz.sessions.clock,
            random_bytes=secrets.token_bytes,
            metrics=metrics,
            deadline_seconds=deadline,
        )

    def services(
        self, *, issuer: NodeCaIssuer | None = None, deps: FleetDependencies | None = None
    ) -> tuple[EnrollmentService, CredentialRotationService]:
        """Servicios nuevos sobre la misma base (``issuer`` y ``deps`` para las sondas)."""
        chosen = issuer if issuer is not None else self.issuer()
        base = deps if deps is not None else self.fleet.deps
        return (
            EnrollmentService(
                base,
                issuer=chosen,
                keys=self.keys,
                source_key=FixedSourceKey(self.hash_key),
                ingest_base_url=INGEST_BASE_URL,
            ),
            CredentialRotationService(base, issuer=chosen, keys=self.keys),
        )

    def app(
        self,
        enrollment: EnrollmentService | None = None,
        rotation: CredentialRotationService | None = None,
        *,
        database: Any = None,
        **gate_args: Any,
    ) -> tuple[NodeApiGate, Any]:
        """Otra aplicación con los manejadores de TASK-219 (otra instancia, otra base)."""
        authz = self.fleet.authz
        clock = authz.sessions.clock
        store = PostgresNodeContextStore(database if database is not None else self.fleet.database)
        identity = NodeIdentity(contexts=authz.contexts, store=store)
        limits = gate_args.pop("limits", None) or NodeRateLimits(UnlimitedNodeLimiter(clock))
        operations: dict[NodeRoute, NodeOperation] = {
            **probe_operations(self.probe),
            NodeRoute.ENROLLMENT: enrollment_operation(
                enrollment if enrollment is not None else self.enrollment, identity, limits
            ),
            NodeRoute.CREDENTIAL_ROTATION: credential_rotation_operation(
                rotation if rotation is not None else self.rotation
            ),
        }
        gate = node_gate(
            contexts=authz.contexts,
            store=store,
            clock=clock,
            probe=self.probe,
            limits=limits,
            operations=operations,
            **gate_args,
        )
        return gate, node_app(World(clock=clock), gate)

    # --- Planta, nodo y sobres ----------------------------------------------------------------

    def declared(self, zones: int = 1, *, cameras: int = 1, published: bool = True) -> NodeSetup:
        """Nodo ``declared`` con ``zones`` zonas propias, publicadas con su compuerta."""
        fleet = self.fleet
        site = fleet.site(plants=1, zones=zones)
        plant = next(iter(site.plants))
        chosen = tuple(site.plants[plant])
        installer = fleet.installer(site)
        response = fleet.declare(installer, plant, list(chosen))
        assert response.status_code == 201, response.text
        setup = NodeSetup(
            uuid.UUID(response.json()["node_id"]), site.organization_id, plant, chosen, installer
        )
        if published:
            for zone in chosen:
                self.publish(setup, zone, cameras=cameras)
        return setup

    def publish(
        self, setup: NodeSetup, zone: uuid.UUID, *, cameras: int = 1, gate: bool = True
    ) -> None:
        """Versión 1 del catálogo de ``zone`` y su sobre de compuerta ``pending``, firmados con el
        ``SigningService`` real y guardados con los repositorios de VIG-145 y VIG-146."""
        user = self.fleet.authz.add_user(setup.organization_id)
        (row,) = self.fetch("SELECT code FROM identity.zone WHERE zone_id = $1", zone)
        ref = ZoneRef(
            organization_id=setup.organization_id,
            plant_id=setup.plant_id,
            zone_id=zone,
            zone_code=row["code"],
        )
        declared = tuple(_camera(zone, index) for index in range(cameras))
        now = self.now()
        plan = plan_publication(
            None,
            NewStandard(draft=_draft(), initial=_initial(declared)),
            zone=ref,
            issued_at=now,
            declared_by=DeclaredBy(user, "Coordinación sintética", "coordinator_sst"),
            reason_es=REASON,
            new_standard_id=uuid.uuid4(),
        )
        envelope = self.signing.service.sign(SigningPurpose.CATALOG, plan.catalog).to_json_value()
        context = unit_context(setup.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
        state = dataclasses.replace(
            ZoneGateState.initial(setup.organization_id, setup.plant_id, zone), issued_at=now
        )
        gate_envelope = self.signing.service.sign(
            SigningPurpose.GATE, gate_state_payload(state, now)
        ).to_json_value()

        async def save() -> None:
            async with self.fleet.database.transaction(context) as transaction:
                await self.catalog.insert_version(
                    transaction,
                    ZoneCatalogVersion(
                        organization_id=setup.organization_id,
                        plant_id=setup.plant_id,
                        zone_id=zone,
                        catalog_version=1,
                        issued_at=now,
                        issued_by=user,
                        role_in_use=Role.COORDINATOR_SST,
                        reason_es=REASON,
                        changed_fields=(CatalogChangedField.STANDARDS,),
                        payload=plan.catalog,
                        envelope=envelope,
                        single_occupancy=False,
                        aggregation_window_minutes=60,
                        ledger_record_id=uuid.uuid4(),
                    ),
                )
                await self.catalog.upsert_cameras(transaction, ref, declared, now)
                if gate:
                    await self.gates.save_state(transaction, state, gate_envelope)

        self.run(save())

    def publish_gate(self, setup: NodeSetup, zone: uuid.UUID) -> None:
        """Solo el sobre de compuerta ``pending`` de ``zone`` (firmado y guardado)."""
        now = self.now()
        state = dataclasses.replace(
            ZoneGateState.initial(setup.organization_id, setup.plant_id, zone), issued_at=now
        )
        envelope = self.signing.service.sign(
            SigningPurpose.GATE, gate_state_payload(state, now)
        ).to_json_value()
        context = unit_context(setup.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)

        async def save() -> None:
            async with self.fleet.database.transaction(context) as transaction:
                await self.gates.save_state(transaction, state, envelope)

        self.run(save())

    def race(
        self, app: Any, calls: Sequence[tuple[str, Any, Mapping[str, str]]]
    ) -> list[httpx.Response]:
        """Las peticiones ``calls`` a la vez sobre ``app`` (``asyncio.gather``)."""

        async def run() -> list[httpx.Response]:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://nodes.vigia.test",
                timeout=LONG_SECONDS,
            ) as client:
                return list(
                    await asyncio.gather(
                        *(
                            client.post(
                                path,
                                json=body,
                                headers={"X-Vigia-Contract-Version": VERSION, **headers},
                            )
                            for path, body, headers in calls
                        )
                    )
                )

        responses: list[httpx.Response] = self.run(run())
        return responses

    def stored(self, zone: uuid.UUID) -> tuple[Any, Any]:
        """Los dos sobres guardados de ``zone`` (catálogo vigente y compuerta), como JSON."""
        (catalog,) = self.fetch(
            "SELECT envelope::text AS envelope FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 AND superseded_at IS NULL",
            zone,
        )
        gates = self.fetch(
            "SELECT envelope::text AS envelope FROM catalog.zone_gate_state WHERE zone_id = $1",
            zone,
        )
        return (
            json.loads(catalog["envelope"]),
            json.loads(gates[0]["envelope"]) if gates else None,
        )

    def code(self, setup: NodeSetup) -> str:
        response = self.fleet.issue(setup.installer, setup.node_id)
        assert response.status_code == 201, response.text
        code: str = response.json()["code"]
        return code

    # --- Peticiones del nodo ------------------------------------------------------------------

    def body(
        self,
        setup: NodeSetup,
        code: str,
        *,
        client_key: Any = None,
        server_key: Any = None,
        fingerprint: str | None = None,
        common_name: str | None = None,
        names: Sequence[x509.GeneralName] | None = None,
        **csr_args: Any,
    ) -> dict[str, Any]:
        name = common_name if common_name is not None else str(setup.node_id)
        return {
            "enrollment_code": code,
            "key_algorithm": "ecdsa_p256",
            "certificate_signing_request": csr_pem(name, key=client_key, **csr_args),
            "server_certificate_signing_request": csr_pem(
                name, key=server_key, names=[local_ip()] if names is None else names
            ),
            "software_version": "1.4.0",
            "contract_version": VERSION,
            "hardware_fingerprint": fingerprint if fingerprint is not None else setup.fingerprint,
            "requested_at": "2026-10-05T12:00:00.000Z",
        }

    def post(
        self,
        path: str,
        body: Any,
        headers: Mapping[str, str] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        values = {"X-Vigia-Contract-Version": VERSION, **dict(headers or {})}
        response: httpx.Response = self.run(
            (client or self.client).post(path, json=body, headers=values)
        )
        return response

    def enroll(self, setup: NodeSetup, code: str | None = None, **body: Any) -> Enrolled:
        """Alta aceptada (``assert`` 200) con claves nuevas; devuelve lo que guarda el nodo."""
        client_key, server_key = new_key(), new_key()
        payload = self.body(
            setup,
            code if code is not None else self.code(setup),
            client_key=client_key,
            server_key=server_key,
            **body,
        )
        response = self.post(ENROLLMENT_PATH, payload)
        assert response.status_code == 200, response.text
        document = response.json()
        certificate = x509.load_pem_x509_certificate(document["certificate"].encode())
        return Enrolled(certificate, client_key, server_key, document)

    @staticmethod
    def headers_for(certificate: x509.Certificate) -> dict[str, str]:
        return {**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}

    @staticmethod
    def enrolled_from(response: httpx.Response, previous: Enrolled) -> Enrolled:
        """Lo que guarda el nodo tras una rotación aceptada (``response``)."""
        document = response.json()
        certificate = x509.load_pem_x509_certificate(document["certificate"].encode())
        return Enrolled(certificate, previous.client_key, previous.server_key, document)

    def rotation_body(self, node_id: uuid.UUID, **csr_args: Any) -> dict[str, Any]:
        return {
            "node_id": str(node_id),
            "certificate_signing_request": csr_pem(str(node_id), **csr_args),
            "server_certificate_signing_request": csr_pem(str(node_id), names=[local_ip()]),
            "requested_at": "2026-10-05T12:00:00.000Z",
        }

    def rotate(self, enrolled: Enrolled, node_id: uuid.UUID, **csr_args: Any) -> httpx.Response:
        return self.post(ROTATION_PATH, self.rotation_body(node_id, **csr_args), enrolled.headers)

    def probe_get(
        self, certificate: x509.Certificate, zone: uuid.UUID, *, client: Any = None
    ) -> httpx.Response:
        """La ruta de prueba interna de TASK-206 (la petición siguiente del nodo)."""
        headers = {**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}
        response: httpx.Response = self.run(
            (client or self.client).get(CATALOG_PATH.format(zone=zone), headers=headers)
        )
        return response

    # --- Lo que quedó escrito -----------------------------------------------------------------

    def credentials(self, node_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT credential_id, organization_id, certificate_serial, status, rotated_from,"
            " issued_at, expires_at, subject::text AS subject FROM fleet.node_credential"
            " WHERE node_id = $1 ORDER BY issued_at, credential_id",
            node_id,
        )

    def attempts(self, node_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT result, hardware_fingerprint, source_ip_hash, ledger_record_id"
            " FROM fleet.enrollment_attempt WHERE node_id = $1 ORDER BY attempted_at, attempt_id",
            node_id,
        )

    def fleet_record(self, node_id: uuid.UUID) -> Any:
        (row,) = self.fetch(
            "SELECT n.status, f.enrolled_at, f.hardware_fingerprint FROM identity.node_identity"
            " AS n JOIN fleet.node_fleet_record AS f ON f.node_id = n.node_id"
            " WHERE n.node_id = $1",
            node_id,
        )
        return row

    def code_statuses(self, node_id: uuid.UUID) -> list[str]:
        return [row["status"] for row in self.fleet.codes(node_id)]


def _camera(zone: uuid.UUID, index: int) -> ZoneCamera:
    return ZoneCamera(
        camera_id=uuid.uuid5(zone, f"camara-{index}"),
        code=f"CM-{index}",
        role_in_zone=CameraRoleInZone.PRIMARY if index == 0 else CameraRoleInZone.REDUNDANT,
        declared_min_fps=5.0,
        stream_reference=f"cam-{index}",
    )


def _draft() -> StandardDraft:
    return StandardDraft(
        family="coexistence",  # type: ignore[arg-type]
        title_es="Coexistencia en la celda",
        declared_text="Nadie permanece en la celda mientras la máquina está energizada.",
        predicate={
            "all_of": [{"presence": True}, {"signal_role": "energy", "value": "asserted"}],
            "min_duration_ms": 0,
        },
    )


def _initial(cameras: tuple[ZoneCamera, ...]) -> InitialZoneParameters:
    return InitialZoneParameters(
        cameras=cameras,
        required_count=1,
        required_camera_ids=(cameras[0].camera_id,),
        signals=(
            {
                "signal_id": str(uuid.UUID(int=40_000, version=4)),
                "code": "SG-1",
                "role": "energy",
                "asserted_level": "high",
                "source": {"reader": "plc-1", "channel": 1},
                "description_es": "Energía de la prensa",
            },
        ),
        thresholds={"review": 0.4, "publication": 0.8},
        clip_window={"pre_seconds": 10, "post_seconds": 10},
        episode={"grouping_window_ms": 3000, "max_segment_ms": 900000},
    )


@contextlib.contextmanager
def enrollment_world(
    endpoint: PostgresEndpoint, prefix: str, *, pool: int = 12
) -> Iterator[EnrollmentWorld]:
    with fleet_stack(endpoint, prefix, pool=pool) as fleet:
        now = fleet.authz.now()
        signing = asyncio.run(bootstrapped_world(start=now))
        kms = MemoryKms()
        body, root = asyncio.run(root_bundle_for(kms, now))
        keys = CountingSigning(signing)
        hash_key = secrets.token_bytes(32)
        probe = Probe()
        world = EnrollmentWorld(
            fleet=fleet,
            signing=signing,
            keys=keys,
            kms=kms,
            roots=RootStore(body),
            root=root,
            hash_key=hash_key,
            probe=probe,
            enrollment=None,  # type: ignore[arg-type]
            rotation=None,  # type: ignore[arg-type]
            gate=None,  # type: ignore[arg-type]
            client=None,  # type: ignore[arg-type]
        )
        world.enrollment, world.rotation = world.services()
        world.gate, app = world.app()
        world.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nodes.vigia.test",
            timeout=LONG_SECONDS,
        )
        try:
            yield world
        finally:
            fleet.run(world.client.aclose())
