"""Entorno de las pruebas de compuertas, acta de alcance y política de planta (TASK-211).

``gates_world`` levanta sobre la base migrada (como ``vigia_app``) los servicios reales de
``catalog.gates``: ``GateService``, ``ScopeRecordService`` y ``PlantPolicyService``, con
``EscritorExpediente`` y la bandeja sincronizada con los tipos y eventos del catálogo, la política
de texto libre con el validador mínimo de U-03, ``DocumentService`` real sobre un almacén en memoria
(``MemoryDocumentStorage``: el objeto «subido» tiene exactamente los metadatos concedidos) y un
doble del puerto de firma que delega en el ``SigningService`` real y cuenta, cae o retiene cada
firma (``SignerDouble``).

``commissioning.run`` solo está en la columna ``provider_installer``: las escrituras las hace un
instalador del proveedor con una concesión vigente sobre el cliente. Las vigencias que compara la
base (concesiones) salen del mismo reloj simulado, que arranca en la hora de la base (retro 14).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

from vigia_contracts.models.enumerations import GateStatus
from vigia_contracts.signing import KeySet

from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.examples.test_ledger_routes import StubEvidenceStorage
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.signing_support import SigningWorld, bootstrapped_world
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.gate_repository import PostgresGateRepository
from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.adapters.postgres.scope_record_repository import (
    PostgresScopeRecordRepository,
)
from vigia_platform.catalog.adapters.s3.documents import DocumentObjectStore
from vigia_platform.catalog.application.documents import DocumentService
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.catalog.application.gates import GateService, GateTransition
from vigia_platform.catalog.application.plant_policy import PlantPolicyService
from vigia_platform.catalog.application.scope_record import ScopeRecordFiled, ScopeRecordService
from vigia_platform.catalog.domain.enums import DocumentKind, GateKind
from vigia_platform.catalog.domain.scope_record import CameraFraming, ScopeRecordRequest
from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService, ZoneSpec
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordType, RecordTypeRegistry
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.signing import NODE_PURPOSES, SigningKeyUnavailable, SigningPurpose
from vigia_platform.shared.storage import (
    ChecksumType,
    ObjectHead,
    PresignedRequest,
    StorageUnavailable,
)

GATE_TYPES: Final = (
    "gate_state_changed",
    "mounting_gate_record",
    "plant_policy_signed",
    "use_agreement_signed",
    "commissioning_step",
)
HOUR: Final = timedelta(hours=1)
SCOPE_TEXT: Final = "Se observa la celda de soldadura 3 durante el turno; nunca a las personas."
FRAMING: Final = "Encuadre cenital de la celda con la valla perimetral visible"
REASON: Final = "Retiro sintético de la compuerta por cambio de layout"
PDF: Final = b"%PDF-1.7\n% documento firmado sintetico\n"
PNG: Final = b"\x89PNG\r\n\x1a\n captura sintetica del difuminado"
TEST_LOCK_TIMEOUT_MS: Final = 60_000
TEST_SIGN_TIMEOUT_SECONDS: Final = 120.0
"""Topes generosos (retro 15) para las pruebas que no tratan de los topes."""
GATE_SECONDS: Final = 60.0
WAIT_SECONDS: Final = 30.0
POLL_SECONDS: Final = 0.05


# --- Dobles ------------------------------------------------------------------------------------


class SignerDouble:
    """Delegado del ``SigningService`` real: cuenta, se cae o retiene cada firma."""

    def __init__(self, world: SigningWorld) -> None:
        self.world = world
        self.calls = 0
        self.down = False
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any:
        with self._lock:
            self.calls += 1
        if self.down:
            raise SigningKeyUnavailable(purpose)
        gate = self.gate
        if gate is not None:
            gate.wait(GATE_SECONDS)
        return self.world.service.sign(purpose, payload)

    def keyset(self) -> KeySet:
        keyset = KeySet(self.world.clock)
        keyset.pin_initial(
            [k.to_contract() for p in NODE_PURPOSES for k in self.world.service.public_keys(p)]
        )
        return keyset


class MemoryDocumentStorage:
    """``vigia-evidence`` en memoria: ``put`` deja el objeto con los metadatos concedidos."""

    def __init__(self) -> None:
        self.objects: dict[str, ObjectHead] = {}
        self.down = False

    async def head_object(self, key: str) -> ObjectHead | None:
        if self.down:
            raise StorageUnavailable("head_object")
        return self.objects.get(key)

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = timedelta(minutes=15),
    ) -> PresignedRequest:
        return PresignedRequest(
            "PUT",
            f"https://almacen.vigia.test/{key}",
            {"content-type": content_type, "x-amz-checksum-sha256": checksum_sha256},
            BASE_TIME + ttl,
        )

    def put(self, ref: Mapping[str, Any]) -> None:
        digest = base64.b64encode(bytes.fromhex(ref["sha256"])).decode("ascii")
        self.objects[ref["storage_key"]] = ObjectHead(
            key=ref["storage_key"],
            size_bytes=ref["size_bytes"],
            checksum_sha256=digest,
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type=ref["content_type"],
            metadata={},
            version_id="v1",
        )


# --- Entorno -----------------------------------------------------------------------------------


@dataclass
class GatesWorld:
    authz: AuthzEnvironment
    database: Database
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry
    signer: SignerDouble
    storage: MemoryDocumentStorage
    documents: DocumentService
    hierarchy: HierarchyService
    hierarchy_deps: IdentityDependencies
    gates: GateService = field(init=False)
    records: ScopeRecordService = field(init=False)
    policies: PlantPolicyService = field(init=False)

    def __post_init__(self) -> None:
        self.gates = self.build_gates()
        self.records = self.build_records()
        self.policies = self.build_policies()

    # --- Servicios -----------------------------------------------------------------------------

    def build_gates(self, **changes: Any) -> GateService:
        fields: dict[str, Any] = {
            "repository": PostgresGateRepository(self.database),
            "catalog": PostgresCatalogRepository(self.database),
            "agreements": PostgresAgreementRepository(),
            "database": self.database,
            "writer": self.writer,
            "authorizer": self.authz.authorizer,
            "audit": self.authz.sessions.audit,
            "free_text": self.free_text,
            "signer": self.signer,
            "clock": self.authz.sessions.clock,
            # Retro 15: el tope de firma no es lo que prueban estas pruebas.
            "sign_timeout_seconds": TEST_SIGN_TIMEOUT_SECONDS,
        }
        fields.update(changes)
        return GateService(**fields)

    def build_records(self, gates: GateService | None = None, **changes: Any) -> ScopeRecordService:
        fields: dict[str, Any] = {
            "gates": gates or self.gates,
            "records": PostgresScopeRecordRepository(self.database),
            "catalog": PostgresCatalogRepository(self.database),
            "policies": PostgresPlantPolicyRepository(self.database),
            "documents": self.documents,
            "nodes": self.hierarchy,
            "writer": self.writer,
            "free_text": self.free_text,
        }
        fields.update(changes)
        return ScopeRecordService(**fields)

    def build_policies(self, **changes: Any) -> PlantPolicyService:
        fields: dict[str, Any] = {
            "repository": PostgresPlantPolicyRepository(self.database),
            "documents": self.documents,
            "database": self.database,
            "writer": self.writer,
            "authorizer": self.authz.authorizer,
            "audit": self.authz.sessions.audit,
            "free_text": self.free_text,
            "clock": self.authz.sessions.clock,
        }
        fields.update(changes)
        return PlantPolicyService(**fields)

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.authz.execute(sql, *args)

    def advance(self, seconds: float = 1.0) -> None:
        self.authz.sessions.clock.advance(seconds)

    # --- Personas y lugares --------------------------------------------------------------------

    def site(self, plants: int = 1, zones: int = 1) -> Site:
        return self.authz.add_site(plants=plants, zones_per_plant=zones)

    def installer(
        self,
        site: Site,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> ScopeContext:
        """Instalador del proveedor con una concesión vigente sobre ``site``."""
        authz = self.authz
        installer = authz.add_provider_user()
        concession = authz.add_concession(
            site.organization_id,
            installer,
            level=level,
            scope_id=scope_id,
            granted_at=authz.now() - HOUR,
        )
        cookie = authz.open_session(authz.provider_organization_id, installer)
        scope = self.run(authz.contexts.context_from_session(cookie, concession_id=concession))
        context: ScopeContext = scope.context
        return context

    def member(
        self,
        site: Site,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> ScopeContext:
        user_id = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user_id, role, level, scope_id)
        cookie = self.authz.open_session(site.organization_id, user_id)
        scope = self.run(self.authz.contexts.context_from_session(cookie))
        context: ScopeContext = scope.context
        return context

    def created_zone(
        self, site: Site, plant: uuid.UUID, gates: GateService | None = None
    ) -> uuid.UUID:
        """Una zona creada por ``create_zone`` de U-02 con ``gates`` como ``ZoneGateGenesis``
        (A-60), como la cablea la raíz de composición de ``vigia-api``."""
        administrator = self.member(site, Role.ADMINISTRATOR)
        hierarchy = HierarchyService(self.hierarchy_deps, zone_gates=gates or self.gates)
        code = f"ZN-{uuid.uuid4().hex[:8].upper()}"
        view = self.run(hierarchy.create_zone(administrator, plant, ZoneSpec(code, "Zona nueva")))
        zone_id: uuid.UUID = view.zone_id
        return zone_id

    def equip(
        self,
        site: Site,
        plant: uuid.UUID,
        zone: uuid.UUID,
        cameras: int = 2,
        *,
        node: bool = True,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[uuid.UUID, ...]:
        """Catálogo vigente con ``cameras`` cámaras en la zona y, si ``node``, un nodo asignado.

        ``payload`` añade campos al catálogo sintético (estándares, cobertura mínima…); si trae
        ``cameras``, esas son las cámaras de la zona."""
        camera_ids = tuple(uuid.uuid4() for _ in range(cameras))
        if payload is not None and "cameras" in payload:
            camera_ids = tuple(uuid.UUID(c["camera_id"]) for c in payload["cameras"])
        self.execute(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, ledger_record_id)"
            " VALUES ($1, $2, $3, 1, $4, $5, 'administrator', 'Catálogo sintético inicial',"
            " ARRAY['cameras'], $6, '{}', false, $7)",
            site.organization_id,
            plant,
            zone,
            BASE_TIME,
            self.authz.operator_id,
            json.dumps(
                {"cameras": [{"camera_id": str(c)} for c in camera_ids], **dict(payload or {})}
            ),
            uuid.uuid4(),
        )
        if node:
            self.assign_node(site, plant, zone)
        return camera_ids

    def assign_node(self, site: Site, plant: uuid.UUID, zone: uuid.UUID) -> uuid.UUID:
        node_id = uuid.uuid4()
        self.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node_id,
            site.organization_id,
            plant,
            f"ND-{node_id.hex[:6].upper()}",
            BASE_TIME,
        )
        self.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid.uuid4(),
            site.organization_id,
            plant,
            zone,
            node_id,
            BASE_TIME,
            self.authz.operator_id,
        )
        return node_id

    # --- Documentos ----------------------------------------------------------------------------

    def document(
        self,
        context: ScopeContext,
        plant: uuid.UUID,
        kind: DocumentKind,
        *,
        uploaded: bool = True,
    ) -> dict[str, Any]:
        """Un ``document_ref`` concedido (y subido, si ``uploaded``) en ``plant``."""
        data, content_type = (
            (PNG + uuid.uuid4().bytes, "image/png")
            if kind is DocumentKind.BLUR_CHECK_CAPTURE
            else (PDF + uuid.uuid4().bytes, "application/pdf")
        )
        issued = self.run(
            self.documents.issue(
                context,
                plant_id=plant,
                kind=kind.value,
                content_type=content_type,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        ref: dict[str, Any] = issued.grant.ref.to_json()
        if uploaded:
            self.storage.put(ref)
        return ref

    def grant_status(self, document_id: str) -> str:
        (row,) = self.fetch(
            "SELECT status FROM catalog.document_upload_grant WHERE document_id = $1",
            uuid.UUID(document_id),
        )
        status: str = row["status"]
        return status

    # --- Actas y transiciones ------------------------------------------------------------------

    def scope_request(
        self,
        context: ScopeContext,
        plant: uuid.UUID,
        camera_ids: Sequence[uuid.UUID],
        **changes: Any,
    ) -> ScopeRecordRequest:
        fields: dict[str, Any] = {
            "scope_text_es": SCOPE_TEXT,
            "cameras": tuple(
                CameraFraming(camera, FRAMING, index == 0)
                for index, camera in enumerate(camera_ids)
            ),
            "blur_declared": True,
            "capture_document_ref": self.document(context, plant, DocumentKind.BLUR_CHECK_CAPTURE),
            "document_ref": None,
        }
        fields.update(changes)
        return ScopeRecordRequest(**fields)

    def file(
        self,
        context: ScopeContext,
        zone: uuid.UUID,
        request: ScopeRecordRequest,
        service: ScopeRecordService | None = None,
    ) -> ScopeRecordFiled:
        self.advance()
        filed: ScopeRecordFiled = self.run(
            (service or self.records).file_scope_record(context, zone, request)
        )
        return filed

    def revoke(
        self,
        context: ScopeContext,
        zone: uuid.UUID,
        gate: GateKind,
        reason: str = REASON,
        service: GateService | None = None,
    ) -> GateTransition:
        self.advance()
        transition: GateTransition = self.run(
            (service or self.gates).revoke(context, zone, gate, reason)
        )
        return transition

    def approve_usage(
        self,
        context: ScopeContext,
        zone: uuid.UUID,
        agreement_id: uuid.UUID | None = None,
        service: GateService | None = None,
    ) -> GateTransition:
        """Lo que hará la aprobación del acuerdo (TASK-212): ``transition_gate`` en su
        transacción, tras autorizar ``commissioning.run`` sobre la zona."""
        gates = service or self.gates
        self.advance()

        async def approve() -> GateTransition:
            _, authorized = await gates.zone(context, zone, PermissionKey.COMMISSIONING_RUN)
            writer = with_unit(authorized, ActorUnit.U03)

            async def body(transaction: Any) -> GateTransition:
                return await gates.transition_gate(
                    transaction,
                    writer,
                    zone,
                    GateKind.USAGE,
                    GateStatus.APPROVED,
                    agreement_id or uuid.uuid4(),
                )

            return await gates.run(writer, body)

        transition: GateTransition = self.run(approve())
        return transition

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def history(self, zone: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT gate, status, effective_from, effective_until, decided_by, reason_es,"
            " record_id, ledger_record_id FROM catalog.gate_state_history WHERE zone_id = $1"
            " ORDER BY gate, effective_from",
            zone,
        )

    def projection(self, zone: uuid.UUID) -> dict[str, Any] | None:
        """La fila de ``zone_gate_state`` con ``mounting``, ``usage`` y ``envelope`` en JSON."""
        rows = self.fetch(
            "SELECT mounting::text AS mounting, usage::text AS usage, resulting_mode, issued_at,"
            " valid_until, envelope::text AS envelope FROM catalog.zone_gate_state"
            " WHERE zone_id = $1",
            zone,
        )
        if not rows:
            return None
        row = dict(rows[0])
        for column in ("mounting", "usage", "envelope"):
            row[column] = json.loads(row[column])
        return row

    def acta_rows(self, zone: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT record_id, scope_text_es, cameras::text AS cameras, blur_verification::text"
            " AS blur_verification, document_ref::text AS document_ref, signed_by, role_in_use,"
            " plant_policy_loaded_at_signing, ledger_record_id"
            " FROM catalog.mounting_gate_record WHERE zone_id = $1 ORDER BY record_id",
            zone,
        )

    def records_of(self, zone: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT record_id, record_type, plant_id, source_key, actor_role_in_use,"
            " actor_concession_id, record_hash, ledger.vigia_bytes_to_jsonb(content) AS content"
            " FROM ledger.ledger_record WHERE scope_zone_id = $1 ORDER BY chain_sequence",
            zone,
        )

    def events(self, plant: uuid.UUID, zone: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT event_name, payload::text AS payload FROM shared.outbox_event"
            " WHERE plant_id = $1 AND payload ->> 'zone_id' = $2 ORDER BY created_at, event_id",
            plant,
            str(zone),
        )

    def written(self, plant: uuid.UUID, zone: uuid.UUID) -> tuple[Any, ...]:
        """Historia, actas, registros, eventos y proyección de la zona."""
        projection = self.projection(zone)
        return (
            len(self.history(zone)),
            len(self.acta_rows(zone)),
            len(self.records_of(zone)),
            len(self.events(plant, zone)),
            projection,
        )


@contextmanager
def gates_world(
    endpoint: PostgresEndpoint, prefix: str, extra_types: Sequence[RecordType] = ()
) -> Iterator[GatesWorld]:
    """El entorno de ``GatesWorld`` sobre una base migrada propia.

    ``extra_types`` añade tipos de registro a los de las compuertas (p. ej. los de la prueba de
    oclusión, VIG-154)."""
    signing = asyncio.run(bootstrapped_world())
    with authz_environment(endpoint, prefix) as authz:
        sessions = authz.sessions
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(now["now"])
        registry = RecordTypeRegistry()
        for definition in (
            *U02_RECORD_TYPES,
            *(d for d in CATALOG_RECORD_TYPES if d.record_type in GATE_TYPES),
            *extra_types,
        ):
            registry.register(definition)
        outbox_catalog = OutboxCatalog()
        register_u02_event_types(outbox_catalog.event_types)
        register_catalog_event_types(outbox_catalog.event_types)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
                await outbox_catalog.synchronize(SqlOutboxCatalogStore(transaction), sessions.clock)
            registry.seal()

        authz.run(synchronize())
        free_text = FreeTextPolicyRegistry()
        register_u03_free_text_validator(free_text)
        free_text.seal()
        # Retro 15: la espera de los candados no depende del ``lock_timeout`` de 2 s, que no es
        # lo que se prueba; las pruebas concurrentes deciden por eventos.
        database = app_database(
            sessions.migrated, worker_pool_size=8, lock_timeout_ms=TEST_LOCK_TIMEOUT_MS
        )
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(StubEvidenceStorage(), sessions.clock),
            outbox=Outbox(outbox_catalog, sessions.clock),
            clock=sessions.clock,
        )
        storage = MemoryDocumentStorage()
        documents = DocumentService(
            database=database,
            audit=sessions.audit,
            authorizer=authz.authorizer,
            store=DocumentObjectStore(storage),
            clock=sessions.clock,
        )
        hierarchy_deps = IdentityDependencies(
            database=database,
            writer=writer,
            audit=sessions.audit,
            outbox=sessions.outbox,
            authorizer=authz.authorizer,
            free_text=free_text,
            clock=sessions.clock,
            provider_organization_id=authz.provider_organization_id,
        )
        world = GatesWorld(
            authz,
            database,
            writer,
            free_text,
            SignerDouble(signing),
            storage,
            documents,
            # Como la raíz de composición: firmantes por la consulta acotada de A-58.
            HierarchyService(hierarchy_deps, lookups=authz.contexts),
            hierarchy_deps,
        )
        try:
            yield world
        finally:
            authz.run(database.dispose())


def sqlstate(error: BaseException) -> str | None:
    """El SQLSTATE de ``error`` o de su cadena de causas."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "orig", None)):
            state = getattr(candidate, "sqlstate", None)
            if isinstance(state, str):
                return state
        current = current.__cause__ or current.__context__
    return None
