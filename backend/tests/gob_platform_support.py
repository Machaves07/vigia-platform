"""La aplicación completa de U-03 para ``tests/abuse`` y ``tests/examples`` (TASK-229; NFR-GOB-62).

``gob_platform`` monta ``create_app`` con la cadena fija de middleware y **todas** las unidades de
``platform_units()`` (``shared``, ``identity``, ``ledger``, ``catalog``, ``fleet`` y
``node_api``): las rutas de personas y las diez del contrato en **una sola** aplicación, con el
estado de producción de cada unidad (``_catalog_state``, ``_fleet_state`` y ``_node_operations``
de ``shared.runtime.units``, más la ``NodeApiGate`` con ``RateLimiter`` y freno global reales,
como ``_node_api_state``), contra:

- PostgreSQL 16 real como ``vigia_app`` (testcontainers), con los tipos de registro, los eventos y
  los validadores de texto libre de todas las unidades registrados y sincronizados;
- LocalStack S3 para ``vigia-evidence`` (concesiones de clip y de documentos, cabeceras de los
  objetos que verifica el escritor) y ``vigia-edge`` (``ca/root.pem`` de ``vigia-node-ca``).

**Dobles**, solo los que admite TASK-229:

- **KMS**: la clave de ``vigia-node-ca`` es ``MemoryKms`` (P-256 de prueba); el cifrado de sobre del
  segundo factor usa ``FakeKms``; la clave estable del hash de origen del alta (que en producción
  sale del gestor de secretos) es fija por mundo;
- **almacén de confianza**: el balanceador termina el mTLS y entrega el certificado en las
  cabeceras ``X-Amzn-Mtls-Clientcert-*`` (``alb_headers``);
- **reloj**: ``SimulatedClock`` arrancado en la hora de la base (VIG-135).

El hash de contraseñas es el de ``tests.examples.test_auth_routes`` (``Passwords``): ningún
escenario de U-03 entra por contraseña.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import secrets
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from cryptography import x509
from vigia_contracts.models import api

from tests.api_support import World
from tests.authz_support import Site
from tests.examples.test_auth_routes import Passwords
from tests.factories import uuid7
from tests.fleet_credentials_support import (
    MemoryKms,
    csr_pem,
    local_ip,
    new_key,
    root_bundle_for,
)
from tests.hierarchy_support import LINK_BASE, FakeActivationPasswords, FakeActivationSecondFactor
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.isolation.gob_world import unique_node_code
from tests.live_view_support import LiveViewEnvironment, live_view_environment
from tests.node_api_support import VERSION, alb_headers
from tests.outbox_support import app_database
from tests.second_factor_support import FakeKms
from tests.session_support import ORIGIN_KEY
from tests.signing_support import ENVIRONMENT
from tests.writer_support import save_record_types, unit_context
from vigia_platform.fleet.adapters.ca.certificate_profiles import NodeCaIssuer
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY
from vigia_platform.fleet.application.credential_rotation import CredentialRotationService
from vigia_platform.fleet.application.enrollment import EnrollmentService, FixedSourceKey
from vigia_platform.fleet.application.enrollment_codes import BundleRoots, EnrollmentCodeService
from vigia_platform.fleet.registration import catalog_tasks, fleet_tasks
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.adapters.http import IdentityHttp
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.concessions import ConcessionService
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.identity.application.invitations import EmailSenderRegistry, InvitationService
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.organization import OrganizationSettingsService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.application.roles import RoleService
from vigia_platform.identity.application.users import SecondFactorResetService, UserService
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.second_factor import SecondFactorService
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie, SessionService
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.adapters.http import LedgerHttp
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityResults
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.integrity_requests import IntegrityRequests
from vigia_platform.ledger.application.labels import LabelService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import AuditBrakeSource, EmergencyBrake, NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import NodeApiGate
from vigia_platform.node_api.routes.credential_rotations import credential_rotation_operation
from vigia_platform.node_api.routes.enrollment import enrollment_operation
from vigia_platform.node_api.versioning import VersionPolicy
from vigia_platform.shared.adapters.http import PlatformHttp
from vigia_platform.shared.api.declarations import (
    NODE_GATE_STATE_KEY,
    NodeRoute,
    iter_declared_routes,
)
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeLevel
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog, PeriodicTaskRegistry
from vigia_platform.shared.outbox.replay import DeadLetterReplay
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.ratelimit import RateLimiter
from vigia_platform.shared.runtime.units import (
    UnitServices,
    _catalog_state,
    _fleet_dependencies,
    _fleet_state,
    _node_operations,
    free_text_registry,
    gate_service,
    record_type_registry,
    registered_units,
)
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.storage import S3Storage

__all__ = [
    "CLOSE_ROUTE",
    "INGEST_BASE_URL",
    "LONG_SECONDS",
    "NODE_BASE",
    "REASON",
    "GobPlatform",
    "GobZone",
    "HttpsPresign",
    "Onboarding",
    "Signatory",
    "close_body",
    "detail_of",
    "gob_platform",
    "local_url",
    "ok",
    "require_close_route",
    "stamp",
]

STATIC: Final = Path(__file__).resolve().parent / "fixtures" / "static"
ORIGIN: Final = "https://app.vigia.test"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
NODE_BASE: Final = "https://nodes.vigia.test"
INGEST_BASE_URL: Final = f"{NODE_BASE}/api/nodes"
LONG_SECONDS: Final = 60.0
"""Topes de las esperas de prueba (retro 15): nunca deciden si algo llegó a tiempo."""
TEST_LOCK_TIMEOUT_MS: Final = 60_000


class HttpsPresign:
    """``vigia-evidence`` de LocalStack con ``https`` en las URL firmadas de subida: el contrato lo
    exige en ``upload_url`` y LocalStack firma ``http`` (como ``fleet_clip_support.HttpsUrls``).
    Todo lo demás va al ``S3Storage`` real; la prueba deshace el cambio al subir (``local_url``)."""

    def __init__(self, target: S3Storage) -> None:
        self.target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    async def presign_put(self, *arguments: Any, **options: Any) -> Any:
        presigned = await self.target.presign_put(*arguments, **options)
        return dataclasses.replace(presigned, url=presigned.url.replace("http://", "https://", 1))


def local_url(url: str) -> str:
    """La URL firmada tal como la sirve LocalStack (``http``)."""
    return url.replace("https://", "http://", 1)


@dataclass
class GobPlatform:
    """La aplicación completa, su base y sus depósitos (ver el docstring del módulo)."""

    env: LiveViewEnvironment
    app: Any
    writer: EscritorExpediente
    outbox: Outbox
    storage: S3Storage
    s3: Any
    """Cliente boto3 de LocalStack: lo que de verdad hay en ``vigia-evidence``."""
    evidence_bucket: str
    kms: MemoryKms
    root: x509.Certificate
    limiter: RateLimiter
    """El cubo de fichas de la ``NodeApiGate`` (por proceso, R10)."""
    services: UnitServices
    addresses: set[str] = field(default_factory=set)

    # --- Base y reloj -----------------------------------------------------------------------

    @property
    def authz(self) -> Any:
        return self.env.authz

    @property
    def clock(self) -> Any:
        return self.env.clock

    @property
    def provider(self) -> uuid.UUID:
        provider: uuid.UUID = self.authz.provider_organization_id
        return provider

    def run(self, awaitable: Any) -> Any:
        return self.env.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.env.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.env.execute(sql, *args)

    def now(self) -> datetime:
        now: datetime = self.authz.now()
        return now

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def resync(self) -> None:
        """La hora simulada vuelve a la de la base al empezar cada escenario (como ``Platform``).

        Las concesiones comparan su vigencia con ``now()`` de la base (nuc_0009): un escenario que
        avanzó el reloj horas no deja al siguiente con concesiones «futuras». El cubo de fichas no
        se vacía: cada escenario usa nodos y orígenes propios.
        """
        (row,) = self.fetch("SELECT now() AS now")
        self.clock.set(row["now"])

    # --- Personas ---------------------------------------------------------------------------

    def site(self, plants: int = 1, zones_per_plant: int = 1) -> Site:
        site: Site = self.authz.add_site(plants=plants, zones_per_plant=zones_per_plant)
        return site

    def person(
        self,
        organization_id: uuid.UUID,
        *roles: Role,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> tuple[uuid.UUID, SessionCookie]:
        """Una persona con ``roles`` sobre el alcance dado y su sesión ya verificada."""
        user_id: uuid.UUID = self.authz.add_user(organization_id)
        for role in roles:
            self.authz.assign(organization_id, user_id, role, level, scope_id)
        cookie: SessionCookie = self.authz.open_session(organization_id, user_id)
        return user_id, cookie

    def installer(self, site: Site) -> tuple[uuid.UUID, SessionCookie, uuid.UUID]:
        """Instalador del proveedor con una concesión vigente sobre la organización de ``site``."""
        user_id: uuid.UUID = self.authz.add_provider_user()
        concession: uuid.UUID = self.authz.add_concession(
            site.organization_id, user_id, granted_at=self.now() - timedelta(hours=1)
        )
        cookie: SessionCookie = self.authz.open_session(self.provider, user_id)
        return user_id, cookie, concession

    # --- HTTP -------------------------------------------------------------------------------

    def address(self) -> str:
        """Una dirección de 198.18.0.0/15 que ningún otro escenario del proceso usó."""
        while True:
            host = f"198.18.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"
            if host not in self.addresses:
                self.addresses.add(host)
                return host

    def client(self, address: str | None = None, base_url: str = "http://testserver") -> Any:
        transport = httpx.ASGITransport(app=self.app, client=(address or self.address(), 40_000))
        return httpx.AsyncClient(transport=transport, base_url=base_url, timeout=LONG_SECONDS)

    async def send(
        self,
        method: str,
        path: str,
        *,
        cookie: SessionCookie | None = None,
        concession: uuid.UUID | None = None,
        params: Any = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        address: str | None = None,
    ) -> httpx.Response:
        """Una petición de persona (mismo origen, cookie y, si la hay, concesión)."""
        values = dict(SAME_ORIGIN)
        if cookie is not None:
            values["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value}"
        if concession is not None:
            values[CONCESSION_HEADER] = str(concession)
        values.update(headers or {})
        async with self.client(address) as client:
            response: httpx.Response = await client.request(
                method, path, params=params, json=json_body, headers=values
            )
            return response

    def call(self, method: str, path: str, **options: Any) -> httpx.Response:
        # Un milisegundo por petición: dos peticiones nunca comparten el instante.
        self.advance(0.001)
        response: httpx.Response = self.run(self.send(method, path, **options))
        return response

    async def node_send(
        self,
        method: str,
        path: str,
        *,
        certificate: x509.Certificate | None,
        body: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        address: str | None = None,
    ) -> httpx.Response:
        """Una petición del nodo: con ``certificate``, las cabeceras mTLS del balanceador."""
        values = {"X-Vigia-Contract-Version": VERSION}
        if certificate is not None:
            values.update(alb_headers(certificate))
        if body is not None:
            content = json.dumps(body).encode()
        if content is not None:
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        async with self.client(address, base_url=NODE_BASE) as client:
            response: httpx.Response = await client.request(
                method, path, content=content, headers=values
            )
            return response

    def node_call(self, method: str, path: str, **options: Any) -> httpx.Response:
        self.advance(0.001)
        response: httpx.Response = self.run(self.node_send(method, path, **options))
        return response

    def gather(self, *requests: Any) -> list[httpx.Response]:
        """Las corrutinas ``requests`` a la vez (``asyncio.gather``), en el bucle de la pila."""

        async def run() -> list[httpx.Response]:
            async with asyncio.timeout(10 * LONG_SECONDS):
                return list(await asyncio.gather(*requests))

        responses: list[httpx.Response] = self.run(run())
        return responses

    # --- Tareas periódicas -----------------------------------------------------------------

    def run_task(self, name: str, organization_id: uuid.UUID) -> None:
        """Una iteración de la tarea ``name`` de U-03 sobre una organización, como el
        planificador de ``vigia-worker``: el manejador real que registran ``fleet_tasks`` y
        ``catalog_tasks`` con los servicios del mundo, en una transacción con el contexto de la
        organización."""
        registry = PeriodicTaskRegistry()
        fleet_tasks(registry, self.services)
        catalog_tasks(registry, self.services)
        (task,) = (task for task in registry.tasks() if task.task_name == name)
        context = self.authz.contexts.context_for_organization(task, organization_id)

        async def iterate() -> None:
            async with self.services.database.transaction(context) as transaction:
                await task.handler(transaction)

        self.run(iterate())

    # --- Lecturas como superusuario ---------------------------------------------------------

    def records(self, organization_id: uuid.UUID, record_type: str) -> list[Any]:
        return self.fetch(
            "SELECT record_id, record_type, plant_id, source_key, content_json,"
            " chain_sequence FROM ledger.ledger_record WHERE organization_id = $1"
            " AND record_type = $2 ORDER BY plant_id, chain_sequence",
            organization_id,
            record_type,
        )

    def contents(self, organization_id: uuid.UUID, record_type: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["content_json"]) for row in self.records(organization_id, record_type)
        ]

    def audit_entries(self, organization_id: uuid.UUID, operation: str) -> list[Any]:
        return self.fetch(
            "SELECT * FROM shared.audit_entry WHERE organization_id = $1 AND operation = $2"
            " ORDER BY chain_sequence",
            organization_id,
            operation,
        )

    def events(self, organization_id: uuid.UUID, name: str) -> list[dict[str, Any]]:
        return [
            json.loads(row["payload"])
            for row in self.fetch(
                "SELECT payload FROM shared.outbox_event WHERE organization_id = $1"
                " AND event_name = $2 ORDER BY publish_seq",
                organization_id,
                name,
            )
        ]


def _identity_http(
    deps: IdentityDependencies,
    env: LiveViewEnvironment,
    pool: CpuPool,
    zone_gates: Any,
) -> IdentityHttp:
    """``IdentityHttp`` como el de ``compose_api_runtime``, con los dobles de U-02 de
    ``platform_support`` (contraseñas sin Argon2id y activación con el segundo factor fijo)."""
    sessions = env.authz.sessions
    authz = env.authz
    provider = authz.provider_organization_id
    store = PostgresSessionStore(sessions.database, sessions.audit, deps.outbox)
    second_factor = SecondFactorService(
        PostgresSecondFactorStore(sessions.database, sessions.audit),
        EnvelopeCipher(FakeKms(), "alias/vigia-secrets", sessions.clock),
        pool,
        sessions.clock,
    )
    passwords = Passwords()
    return IdentityHttp(
        login=LoginService(
            store=store,
            sessions=store,
            passwords=passwords,
            second_factor=second_factor,
            contexts=authz.contexts,
            clock=sessions.clock,
            provider_organization_id=provider,
            origin_key=ORIGIN_KEY,
        ),
        sessions=SessionService(store, authz.contexts, sessions.clock),
        invitations=InvitationService(
            deps,
            contexts=authz.contexts,
            passwords=FakeActivationPasswords(),
            second_factor=FakeActivationSecondFactor(),
        ),
        privacy_notice=PrivacyNoticeService(deps),
        passwords=PasswordChangeService(
            database=sessions.database,
            audit=sessions.audit,
            passwords=passwords,
            throttle=store,
            contexts=authz.contexts,
            clock=sessions.clock,
        ),
        me=MeService(sessions.database, contexts=authz.contexts, provider_organization_id=provider),
        provider_organization_id=provider,
        users=UserService(deps, senders=EmailSenderRegistry(), link_base=LINK_BASE),
        roles=RoleService(deps),
        second_factor_reset=SecondFactorResetService(deps, second_factor),
        # A-60 (VIG-180): la zona nace con su sobre GateState inicial firmado.
        hierarchy=HierarchyService(deps, zone_gates=zone_gates),
        organization=OrganizationSettingsService(deps),
        concessions=ConcessionService(
            store=PostgresConcessionStore(database=sessions.database, audit=sessions.audit),
            writer=deps.writer,
            authorizer=authz.authorizer,
            contexts=authz.contexts,
            clock=sessions.clock,
        ),
    )


def _node_gate(
    services: UnitServices,
    limiter: RateLimiter,
    kms: MemoryKms,
    edge: S3Storage,
    hash_key: bytes,
) -> NodeApiGate:
    """``_node_api_state`` de producción; el alta y la rotación firman con ``kms`` (el doble de
    ``vigia-node-ca``) y leen ``ca/root.pem`` del depósito ``vigia-edge`` de LocalStack."""
    database = services.database
    contexts = services.contexts
    clock = services.clock
    policy = VersionPolicy()
    brake = EmergencyBrake(
        AuditBrakeSource(database=database, provider_context=contexts.provider_audit_context),
        clock,
    )
    identity = NodeIdentity(contexts=contexts, store=PostgresNodeContextStore(database))
    limits = NodeRateLimits(limiter, brake=brake, metrics=services.metrics)
    issuer = NodeCaIssuer(
        kms=kms,
        key_id=kms.key_id,
        roots=edge,
        clock=clock,
        random_bytes=secrets.token_bytes,
        metrics=services.metrics,
        deadline_seconds=LONG_SECONDS,
    )
    deps = _fleet_dependencies(services)
    operations = {
        **_node_operations(services, policy, identity, limits),
        NodeRoute.ENROLLMENT: enrollment_operation(
            EnrollmentService(
                deps,
                issuer=issuer,
                keys=services.signing,
                source_key=FixedSourceKey(hash_key),
                ingest_base_url=INGEST_BASE_URL,
                gates=gate_service(services),
            ),
            identity,
            limits,
        ),
        NodeRoute.CREDENTIAL_ROTATION: credential_rotation_operation(
            CredentialRotationService(deps, issuer=issuer, keys=services.signing)
        ),
    }
    return NodeApiGate(
        identity=identity,
        limits=limits,
        clock=clock,
        responses=NodeResponses(clock, metrics=services.metrics),
        policy=policy,
        operations=operations,
    )


@contextlib.contextmanager
def gob_platform(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint, prefix: str
) -> Iterator[GobPlatform]:
    s3 = localstack_endpoint.aws_client("s3")
    with (
        live_view_environment(postgres_endpoint, prefix, at_database_time=True) as env,
        versioned_bucket(s3, "abuse-evidence") as evidence_bucket,
        versioned_bucket(s3, "abuse-edge") as edge_bucket,
    ):
        authz = env.authz
        sessions = authz.sessions
        clock = sessions.clock
        units = registered_units()
        registry = record_type_registry(units)
        events = OutboxCatalog()
        for unit in units:
            unit.event_types(events.event_types)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
                await events.synchronize(SqlOutboxCatalogStore(transaction), clock)
            registry.seal()

        env.run(synchronize())
        # Topes generosos en la base de la fixture (retro 15): ninguna prueba de aquí trata de
        # ellos, y G-5 lanza decenas de escrituras a la vez sobre la cadena de una planta.
        database = app_database(
            sessions.migrated, worker_pool_size=16, lock_timeout_ms=TEST_LOCK_TIMEOUT_MS
        )
        storage = S3Storage(localstack_endpoint.storage_settings(evidence_bucket), clock)
        edge = S3Storage(localstack_endpoint.storage_settings(edge_bucket), clock)
        kms = MemoryKms()
        body, root = env.run(root_bundle_for(kms, authz.now()))
        s3.put_object(Bucket=edge_bucket, Key="ca/root.pem", Body=body)
        free_text = free_text_registry(units)
        outbox = Outbox(events, clock)
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(storage, clock),
            outbox=outbox,
            clock=clock,
        )
        provider = authz.provider_organization_id
        signing = SigningService(
            provider_organization_id=provider,
            store=env.store,
            secrets=env.secrets,
            events=env.events,
            clock=clock,
            environment=ENVIRONMENT,
        )
        env.run(signing.start())
        checkpoints = CheckpointService(
            store=SqlCheckpointStore(
                database=database, writer=writer, audit=sessions.audit, outbox=outbox
            ),
            signer=signing,
            clock=clock,
        )
        services = UnitServices(
            clock=clock,
            metrics=get_metrics(),
            provider_organization_id=provider,
            database=database,
            contexts=authz.contexts,
            authorizer=authz.authorizer,
            audit=sessions.audit,
            outbox=outbox,
            writer=writer,
            free_text=free_text,
            signing=signing,
            checkpoints=checkpoints,
            kms=kms,  # type: ignore[arg-type]
            evidence=HttpsPresign(storage),  # type: ignore[arg-type]
        )
        deps = IdentityDependencies(
            database=database,
            writer=writer,
            audit=sessions.audit,
            outbox=outbox,
            authorizer=authz.authorizer,
            free_text=free_text,
            clock=clock,
            provider_organization_id=provider,
        )
        pool = CpuPool(clock, max_workers=2)
        limiter = RateLimiter(clock)
        fleet = _fleet_state(services)[FLEET_STATE_KEY]
        # ``_node_ca_roots`` lee ``ca/root.pem`` de ``VIGIA_EDGE_BUCKET``: aquí, el mismo depósito
        # de LocalStack que la emisión del alta.
        fleet = dataclasses.replace(
            fleet,  # type: ignore[type-var]
            enrollment_codes=EnrollmentCodeService(
                _fleet_dependencies(services), roots=BundleRoots(edge)
            ),
        )
        ledger = LedgerHttp(
            reader=LectorExpediente(database=database, audit=sessions.audit),
            evidence=EvidenceService(database=database, audit=sessions.audit, storage=storage),
            labels=LabelService(database=database, audit=sessions.audit),
            coverage=CoverageService(database=database, audit=sessions.audit),
            integrity_results=SqlIntegrityResults(database=database),
            integrity_requests=IntegrityRequests(database=database, outbox=outbox, clock=clock),
            checkpoints=checkpoints,
            live_view=env.service(),
            authorizer=authz.authorizer,
            provider_organization_id=provider,
        )
        platform = PlatformHttp(
            signing=signing,
            dead_letter=DeadLetterReplay(
                database=database,
                authorizer=authz.authorizer,
                audit=sessions.audit,
                clock=clock,
            ),
            operators=authz.contexts,
            authorizer=authz.authorizer,
            audit=sessions.audit,
            provider_organization_id=provider,
        )
        app = World(clock=clock).app(
            units=None,
            permissions=None,
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=provider,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=clock,
                ),
                "identity": _identity_http(deps, env, pool, gate_service(services)),
                "ledger": ledger,
                "platform": platform,
                "state": {
                    **_catalog_state(services),
                    FLEET_STATE_KEY: fleet,
                    NODE_GATE_STATE_KEY: _node_gate(
                        services, limiter, kms, edge, secrets.token_bytes(32)
                    ),
                },
            },
            static_dir=STATIC,
            public_origin=ORIGIN,
        )
        world = GobPlatform(
            env=env,
            app=app,
            writer=writer,
            outbox=outbox,
            storage=storage,
            s3=s3,
            evidence_bucket=evidence_bucket,
            kms=kms,
            root=root,
            limiter=limiter,
            services=services,
        )
        world.resync()
        try:
            yield world
        finally:
            pool.shutdown()
            env.run(database.dispose())


# --- Una zona de punta a punta por las rutas ---------------------------------------------------

REASON: Final = "Cambio sintético del catálogo de la zona de prueba"
SCOPE_TEXT: Final = "Celda de la prensa 2: el perímetro de la zona y su acceso frontal."
FRAMING: Final = "Encuadre frontal de la celda con el marcador de referencia en el suelo."
PNG: Final = b"\x89PNG\r\n\x1a\n captura difuminada sintetica "
PDF: Final = b"%PDF-1.7 documento firmado sintetico "
SOFTWARE_VERSION: Final = "1.4.0"
MODEL_VERSION: Final = "yolov8n-2026.09"
SIGNATORY_ROLES: Final = (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST)
CLIP_WINDOW_SECONDS: Final = 10
APPROVAL_MARGIN_SECONDS: Final = 600


@dataclass
class Signatory:
    user_id: uuid.UUID
    role: Role
    cookie: SessionCookie


@dataclass
class GobZone:
    """Una zona de una organización nueva, con su nodo y lo que la prueba necesita de ellos."""

    site: Site
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    admin_id: uuid.UUID
    admin: SessionCookie
    """Administración de la organización (``catalog.manage``, ``fleet.manage``...)."""
    installer_id: uuid.UUID
    installer: SessionCookie
    concession: uuid.UUID
    """El instalador del proveedor y su concesión sobre la organización (``commissioning.run``)."""
    cameras: tuple[uuid.UUID, ...]
    signal_id: uuid.UUID
    standard_id: uuid.UUID
    node_id: uuid.UUID | None = None
    certificate: x509.Certificate | None = None
    hardware_fingerprint: str = field(default_factory=lambda: secrets.token_hex(32))
    signatories: tuple[Signatory, ...] = ()
    agreement_id: uuid.UUID | None = None

    @property
    def organization_id(self) -> uuid.UUID:
        return self.site.organization_id

    @property
    def cert(self) -> x509.Certificate:
        assert self.certificate is not None, "la zona no tiene nodo dado de alta"
        return self.certificate

    @property
    def node(self) -> uuid.UUID:
        assert self.node_id is not None, "la zona no tiene nodo declarado"
        return self.node_id


def ok(response: httpx.Response, status: int = 200) -> Any:
    """El cuerpo de ``response`` si su estado es ``status`` (si no, falla con el cuerpo)."""
    assert response.status_code == status, f"{response.status_code}: {response.text}"
    return response.json()


def detail_of(response: httpx.Response) -> tuple[int, str | None, str | None]:
    """Estado, ``code`` y ``detail_code`` de una respuesta de error de una ruta de personas."""
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def zone_parameters(cameras: Sequence[uuid.UUID], signal_id: uuid.UUID) -> dict[str, Any]:
    """Los parámetros de la versión 1 de una zona (como ``first_standard_body``), con cámaras y
    señal propias de la zona."""
    return {
        "cameras": [
            {
                "camera_id": str(camera),
                "code": f"CM-{index}",
                "role_in_zone": "primary" if index == 0 else "redundant",
                "declared_min_fps": 5.0,
                "stream_reference": f"cam-{index}",
            }
            for index, camera in enumerate(cameras)
        ],
        "minimum_coverage": {"required_count": 1, "required_camera_ids": [str(cameras[0])]},
        "signals": [
            {
                "signal_id": str(signal_id),
                "code": "SG-1",
                "role": "energy",
                "asserted_level": "high",
                "source": {"reader": "plc-1", "channel": 1},
                "description_es": "Energía de la prensa",
            }
        ],
        "thresholds": {"review": 0.4, "publication": 0.8},
        "clip_window": {"pre_seconds": CLIP_WINDOW_SECONDS, "post_seconds": CLIP_WINDOW_SECONDS},
        "episode": {"grouping_window_ms": 3000, "max_segment_ms": 900000},
    }


COEXISTENCE: Final = {
    "family": "coexistence",
    "title_es": "Coexistencia en la celda",
    "declared_text": "Nadie permanece en la celda mientras la máquina está energizada.",
    "predicate": {
        "all_of": [{"presence": True}, {"signal_role": "energy", "value": "asserted"}],
        "min_duration_ms": 0,
    },
}


def stamp(moment: datetime) -> str:
    return format_timestamp(moment)


class Onboarding:
    """Los pasos de H-42 a H-50 por las rutas reales, sobre ``GobPlatform``."""

    def __init__(self, gob: GobPlatform) -> None:
        self.gob = gob

    # --- Catálogo y nodo --------------------------------------------------------------------

    def zone(
        self, *, cameras: int = 2, enrolled: bool = True, within: GobZone | None = None
    ) -> GobZone:
        """Una zona con su nodo: familia admitida, catálogo 1 publicado, nodo declarado por el
        instalador y, con ``enrolled``, dado de alta con su código y una CSR.

        Sin ``within``, en una organización nueva; con ``within``, otra zona de la misma planta
        (mismas personas y concesión; la familia ya está admitida en la planta, BR-GOB-14). Al
        terminar, el reloj avanza ``APPROVAL_MARGIN_SECONDS``: los hechos por defecto (5 minutos
        antes de la hora) caen después de la asignación del nodo a la zona."""
        gob = self.gob
        if within is None:
            site = gob.site(plants=1, zones_per_plant=1)
            plant_id = next(iter(site.plants))
            zone_id = site.plants[plant_id][0]
            admin_id, admin = gob.person(site.organization_id, Role.ADMINISTRATOR)
            installer_id, installer, concession = gob.installer(site)
            admitted = gob.call(
                "POST",
                f"/plants/{plant_id}/admissions",
                cookie=admin,
                json_body={
                    "family": "coexistence",
                    "answers": {"standard": True, "remedy": True, "subject": True},
                },
            )
            ok(admitted, 201)
        else:
            site, plant_id = within.site, within.plant_id
            admin_id, admin = within.admin_id, within.admin
            installer_id, installer = within.installer_id, within.installer
            concession = within.concession
            zone_id = uuid.uuid4()
            gob.authz.add_zone(site.organization_id, plant_id, zone_id)
        camera_ids = tuple(uuid.uuid4() for _ in range(cameras))
        signal_id = uuid.uuid4()
        ok(
            gob.call(
                "POST",
                f"/zones/{zone_id}/standards",
                cookie=admin,
                json_body={
                    **COEXISTENCE,
                    "reason_es": REASON,
                    "zone_parameters": zone_parameters(camera_ids, signal_id),
                },
            ),
            201,
        )
        (row,) = gob.fetch(
            "SELECT payload FROM catalog.zone_catalog_version WHERE zone_id = $1"
            " AND catalog_version = 1",
            zone_id,
        )
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        standard_id = uuid.UUID(payload["standards"][0]["standard_id"])
        zone = GobZone(
            site=site,
            plant_id=plant_id,
            zone_id=zone_id,
            admin_id=admin_id,
            admin=admin,
            installer_id=installer_id,
            installer=installer,
            concession=concession,
            cameras=camera_ids,
            signal_id=signal_id,
            standard_id=standard_id,
        )
        declared = ok(
            self.as_installer(
                zone,
                "POST",
                f"/plants/{plant_id}/nodes",
                {"code": unique_node_code(), "zone_ids": [str(zone_id)]},
            ),
            201,
        )
        zone.node_id = uuid.UUID(declared["node_id"])
        if enrolled:
            zone.certificate = self.enroll(zone)
        gob.advance(APPROVAL_MARGIN_SECONDS)
        return zone

    def as_installer(
        self, zone: GobZone, method: str, path: str, body: Any = None
    ) -> httpx.Response:
        return self.gob.call(
            method, path, cookie=zone.installer, concession=zone.concession, json_body=body
        )

    def code(self, zone: GobZone) -> str:
        issued = ok(self.as_installer(zone, "POST", f"/nodes/{zone.node}/enrollment-codes"), 201)
        code: str = issued["code"]
        return code

    def enrollment_body(
        self, zone: GobZone, code: str, *, fingerprint: str | None = None
    ) -> dict[str, Any]:
        name = str(zone.node)
        return {
            "enrollment_code": code,
            "key_algorithm": "ecdsa_p256",
            "certificate_signing_request": csr_pem(name, key=new_key()),
            "server_certificate_signing_request": csr_pem(name, key=new_key(), names=[local_ip()]),
            "software_version": SOFTWARE_VERSION,
            "contract_version": VERSION,
            "hardware_fingerprint": fingerprint or zone.hardware_fingerprint,
            "requested_at": stamp(self.gob.now()),
        }

    def enroll(self, zone: GobZone, code: str | None = None) -> x509.Certificate:
        """Alta aceptada por ``POST /api/nodes/enrollment``; el certificado que guarda el nodo."""
        body = self.enrollment_body(zone, code if code is not None else self.code(zone))
        document = ok(
            self.gob.node_call("POST", NodeRoute.ENROLLMENT.path, certificate=None, body=body)
        )
        return x509.load_pem_x509_certificate(document["certificate"].encode())

    # --- Documentos y montaje ---------------------------------------------------------------

    def document(self, zone: GobZone, kind: str) -> dict[str, Any]:
        """Un documento firmado de planta: concesión por ``POST /documents`` y ``PUT`` real a
        ``vigia-evidence`` (LocalStack) con las cabeceras de la concesión."""
        data, content_type = (
            (PNG + uuid.uuid4().bytes, "image/png")
            if kind == "blur_check_capture"
            else (PDF + uuid.uuid4().bytes, "application/pdf")
        )
        granted = ok(
            self.as_installer(
                zone,
                "POST",
                "/documents",
                {
                    "plant_id": str(zone.plant_id),
                    "kind": kind,
                    "content_type": content_type,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            ),
            201,
        )
        upload = granted["upload"]
        put = httpx.put(
            local_url(upload["url"]),
            content=data,
            headers=upload["required_headers"],
            timeout=LONG_SECONDS,
        )
        assert put.status_code == 200, put.text
        ref: dict[str, Any] = granted["document_ref"]
        return {**ref, "document_id": str(ref["document_id"])}

    def scope_record_body(self, zone: GobZone, **changes: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "scope_text_es": SCOPE_TEXT,
            "cameras": [
                {
                    "camera_id": str(camera),
                    "framing_description_es": FRAMING,
                    "reference_marker": index == 0,
                }
                for index, camera in enumerate(zone.cameras)
            ],
            "blur_verification": {
                "declared": True,
                "capture_document_ref": self.document(zone, "blur_check_capture"),
            },
        }
        body.update(changes)
        return body

    def mount(self, zone: GobZone) -> Any:
        """Acta de alcance por la ruta: la compuerta de montaje aprobada (``commissioning``)."""
        return ok(
            self.as_installer(
                zone,
                "POST",
                f"/zones/{zone.zone_id}/gates/mounting/scope-record",
                self.scope_record_body(zone),
            ),
            201,
        )

    # --- Acuerdo de uso ---------------------------------------------------------------------

    def closed_record(self, zone: GobZone) -> uuid.UUID:
        """Acta de comisionamiento cerrada de la zona, **por repositorio**: su cierre por la ruta
        es de TASK-216 (VIG-158, todavía sin fusionar); la guarda de la aprobación solo mira que
        exista (``commissioning_record_missing``)."""
        gob = self.gob
        session_id, record_id = uuid.uuid4(), uuid.uuid4()
        now = gob.now()
        gob.execute(
            "INSERT INTO catalog.walk_test_session (session_id, organization_id, plant_id, zone_id,"
            " node_id, catalog_version, kind, status, passes_per_cell, matrix_rows, started_at,"
            " last_activity_at, closed_at, commissioning_record_id)"
            " VALUES ($1, $2, $3, $4, $5, 1, 'initial', 'closed', 3, '[]', $6, $6, $6, $7)",
            session_id,
            zone.organization_id,
            zone.plant_id,
            zone.zone_id,
            zone.node,
            now,
            record_id,
        )
        gob.execute(
            "INSERT INTO catalog.commissioning_record (commissioning_record_id, organization_id,"
            " plant_id, zone_id, session_id, catalog_version, matrix_results,"
            " false_negatives_total, false_alarm_rate_observed, false_alarm_threshold, latency,"
            " installer_measurements, cameras_measured, occlusion_summary, total_hours,"
            " steps_summary, signatures, closed_at, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, 1, '[]', 0, 0, 0, '{}', '{}', '[]', '[]', 0, '[]', '[]',"
            " $6, $7)",
            record_id,
            zone.organization_id,
            zone.plant_id,
            zone.zone_id,
            session_id,
            now,
            uuid.uuid4(),
        )
        return record_id

    def plant_policy(self, zone: GobZone) -> Any:
        return ok(
            self.as_installer(
                zone,
                "POST",
                f"/plants/{zone.plant_id}/policy",
                {
                    "version": 1,
                    "signed_at": stamp(self.gob.now()),
                    "signed_by_display_name": "Gerencia sintética de la planta",
                    "legal_opinion_reference": "CONCEPTO-2026-17",
                    "criteria_summary_es": "Criterios sintéticos de hallazgos incerrables.",
                    "document_ref": self.document(zone, "plant_policy"),
                },
            ),
            201,
        )

    def signatory_policy(self, zone: GobZone, roles: Sequence[Role] = SIGNATORY_ROLES) -> Any:
        return self.as_installer(
            zone,
            "PUT",
            f"/plants/{zone.plant_id}/signatory-policy",
            {"required_roles": [role.value for role in roles], "minimum": len(roles)},
        )

    def signatories(self, zone: GobZone) -> tuple[Signatory, ...]:
        """Una persona de la organización por rol firmante, con su sesión propia."""
        zone.signatories = tuple(self.signatory(zone, role) for role in SIGNATORY_ROLES)
        return zone.signatories

    def signatory(self, zone: GobZone, role: Role) -> Signatory:
        user_id, cookie = self.gob.person(zone.organization_id, role)
        return Signatory(user_id, role, cookie)

    def agreement(self, zone: GobZone, signatories: Sequence[Signatory] | None = None) -> Any:
        chosen = signatories if signatories is not None else zone.signatories
        return self.as_installer(
            zone,
            "POST",
            f"/zones/{zone.zone_id}/use-agreements",
            {"signatories": [{"role": s.role.value, "user_id": str(s.user_id)} for s in chosen]},
        )

    def confirm(self, agreement_id: uuid.UUID | str, signatory: Signatory) -> httpx.Response:
        return self.gob.call(
            "POST", f"/use-agreements/{agreement_id}/confirmations", cookie=signatory.cookie
        )

    def approve(self, zone: GobZone, agreement_id: uuid.UUID | str) -> httpx.Response:
        return self.as_installer(zone, "POST", f"/use-agreements/{agreement_id}/approval")

    def productive(self, zone: GobZone) -> Any:
        """De ``commissioning`` a ``productive`` por las rutas de H-50 (acta cerrada sembrada)."""
        self.closed_record(zone)
        self.plant_policy(zone)
        ok(self.signatory_policy(zone))
        signatories = self.signatories(zone)
        created = ok(self.agreement(zone, signatories), 201)
        zone.agreement_id = uuid.UUID(created["agreement_id"])
        for signatory in signatories:
            assert self.confirm(zone.agreement_id, signatory).status_code == 201
        approved = ok(self.approve(zone, zone.agreement_id))
        # Los hechos por defecto (5 minutos antes de la hora) caen después de la aprobación.
        self.gob.advance(APPROVAL_MARGIN_SECONDS)
        return approved

    def productive_zone(self, *, cameras: int = 2) -> GobZone:
        """Una zona ``productive`` de punta a punta: catálogo, alta, montaje y acuerdo."""
        zone = self.zone(cameras=cameras)
        self.mount(zone)
        self.productive(zone)
        return zone

    def mode(self, zone: GobZone) -> str:
        (row,) = self.gob.fetch(
            "SELECT resulting_mode FROM catalog.zone_gate_state WHERE zone_id = $1", zone.zone_id
        )
        mode: str = row["resulting_mode"]
        return mode

    # --- Peticiones del nodo ----------------------------------------------------------------

    def scope(self, zone: GobZone) -> dict[str, str]:
        return {
            "contract_version": VERSION,
            "organization_id": str(zone.organization_id),
            "plant_id": str(zone.plant_id),
            "zone_id": str(zone.zone_id),
            "node_id": str(zone.node),
        }

    def node_time(self, started: datetime, ended: datetime) -> dict[str, Any]:
        return {
            "started_at": stamp(started),
            "ended_at": stamp(ended),
            "clock": {"synchronized": True, "offset_ms": 12, "source": "ntp.local"},
        }

    def clip(
        self,
        zone: GobZone,
        started: datetime,
        ended: datetime,
        data: bytes | None = None,
        *,
        camera: int = 0,
    ) -> dict[str, Any]:
        """Un clip de evidencia **subido de verdad**: concesión por ``POST clip-uploads`` y
        ``PUT`` a LocalStack con las cabeceras exactas de la concesión."""
        data = data if data is not None else secrets.token_bytes(256)
        window = timedelta(seconds=CLIP_WINDOW_SECONDS)
        request = {
            "clip_id": str(uuid7()),
            "camera_id": str(zone.cameras[camera]),
            "zone_id": str(zone.zone_id),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "duration_ms": int((ended - started + 2 * window).total_seconds() * 1000),
            "purpose": "evidence",
        }
        grant = ok(
            self.gob.node_call(
                "POST", NodeRoute.CLIP_UPLOAD.path, certificate=zone.cert, body=request
            )
        )
        self.put(grant, data)
        return {
            "clip_id": request["clip_id"],
            "camera_id": request["camera_id"],
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": request["sha256"],
            "size_bytes": len(data),
            "duration_ms": request["duration_ms"],
            "starts_at": stamp(started - window),
            "ends_at": stamp(ended + window),
            "segment": "full",
            "anonymized": True,
            "storage_key": grant["storage_key"],
        }

    def verification_clip(self, zone: GobZone, camera: int = 0) -> str:
        """El clip de verificación del comisionamiento (nota T-02, pendiente nº 32): concesión con
        ``purpose = verification``, ``PUT`` real con la marca de anonimización y confirmación por
        ``POST clip-uploads/{clip_id}/confirmation``."""
        data = secrets.token_bytes(512)
        request = {
            "clip_id": str(uuid7()),
            "camera_id": str(zone.cameras[camera]),
            "zone_id": str(zone.zone_id),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "duration_ms": 10_000,
            "purpose": "verification",
        }
        grant = ok(
            self.gob.node_call(
                "POST", NodeRoute.CLIP_UPLOAD.path, certificate=zone.cert, body=request
            )
        )
        self.put(grant, data)
        ok(
            self.gob.node_call(
                "POST",
                NodeRoute.CLIP_CONFIRMATION.path.format(clip_id=request["clip_id"]),
                certificate=zone.cert,
            )
        )
        clip_id: str = request["clip_id"]
        return clip_id

    @staticmethod
    def put(grant: Mapping[str, Any], data: bytes) -> httpx.Response:
        upload = grant.get("upload", grant)
        response = httpx.put(
            local_url(upload["upload_url"] if "upload_url" in upload else upload["url"]),
            content=data,
            headers=dict(upload["required_headers"]),
            timeout=LONG_SECONDS,
        )
        assert response.status_code == 200, response.text
        return response

    def finding(
        self,
        zone: GobZone,
        started: datetime | None = None,
        *,
        clip: Mapping[str, Any] | None = None,
        standard_version: int = 1,
    ) -> dict[str, Any]:
        """Un ``FindingSubmission`` válido de la zona con su clip ya subido."""
        started = started if started is not None else self.gob.now() - timedelta(minutes=5)
        ended = started + timedelta(seconds=30)
        clip = clip if clip is not None else self.clip(zone, started, ended)
        document = {
            "finding_id": str(uuid7()),
            **self.scope(zone),
            "episode": {
                "episode_id": str(uuid7()),
                "segment_index": 0,
                "continues": False,
                "max_segment_ms": 60000,
            },
            "family": "coexistence",
            "standard": {"standard_id": str(zone.standard_id), "version": standard_version},
            "tier": "tier_1",
            "node_time": self.node_time(started, ended),
            "signals": [
                {
                    "signal_id": str(zone.signal_id),
                    "role": "energy",
                    "value": "asserted",
                    "valid": True,
                    "read_at": stamp(started),
                }
            ],
            "observability_state": "observable",
            "cameras": [{"camera_id": str(zone.cameras[0]), "originating": True, "clips": [clip]}],
            "max_confidence": 0.9,
            "episode_duration_ms": 30_000,
            "condition_duration_ms": 30_000,
            "automatic_classification": {
                "trigger": "publication_threshold",
                "corroborating_camera_ids": [],
            },
            "model_version": MODEL_VERSION,
            "software_version": SOFTWARE_VERSION,
        }
        api.parse_finding_submission(json.dumps(document).encode())
        return document

    def detection(self, zone: GobZone, started: datetime | None = None) -> dict[str, Any]:
        """Un ``DetectionForReviewSubmission`` válido (banda de revisión del catálogo)."""
        document = self.finding(zone, started)
        document["detection_id"] = document.pop("finding_id")
        for key in ("tier", "automatic_classification"):
            document.pop(key)
        document |= {"max_confidence": 0.5, "review_threshold": 0.4, "publication_threshold": 0.8}
        api.parse_detection_for_review_submission(json.dumps(document).encode())
        return document

    def post_detection(self, zone: GobZone, document: Mapping[str, Any]) -> Any:
        self.gob.advance(0.001)
        return self.gob.run(self.submit(zone, NodeRoute.DETECTION_REVIEW, document, "detection_id"))

    def event(
        self, zone: GobZone, started: datetime | None = None, *, camera: int = 0
    ) -> dict[str, Any]:
        """Un ``ObservabilityEventSubmission`` de apertura de una cámara de la zona."""
        started = started if started is not None else self.gob.now() - timedelta(minutes=5)
        document: dict[str, Any] = {
            "event_id": str(uuid7()),
            **self.scope(zone),
            "subject": {"kind": "camera", "camera_id": str(zone.cameras[camera])},
            "started_at": stamp(started),
            "node_time": self.node_time(started, started + timedelta(seconds=30)),
            "evidence": [],
            "software_version": SOFTWARE_VERSION,
            "phase": "opened",
            "state": "degraded",
            "causes": ["obstruction"],
        }
        api.parse_observability_event_submission(json.dumps(document).encode())
        return document

    def heartbeat(self, zone: GobZone, **changes: Any) -> dict[str, Any]:
        """Un ``Heartbeat`` válido del nodo de la zona (``changes`` lo altera)."""
        now = self.gob.now()
        body: dict[str, Any] = {
            "heartbeat_id": str(uuid7()),
            "contract_version": VERSION,
            "organization_id": str(zone.organization_id),
            "plant_id": str(zone.plant_id),
            "node_id": str(zone.node),
            "sent_at": stamp(now),
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
                for camera in zone.cameras
            ],
            "zones": [
                {
                    "zone_id": str(zone.zone_id),
                    "mode": "productive",
                    "observability_state": "observable",
                    "catalog_version": 1,
                    "gate_state_valid_until": stamp(now + timedelta(days=7)),
                    "open_episodes": 0,
                }
            ],
            "signal_reader": {"available": True, "adapter": "modbus_rtu"},
            "local_queue": {"pending": 0, "dead_letter": [], "retained_sent": 0},
        }
        body.update(changes)
        return body

    def submit(
        self,
        zone: GobZone,
        route: NodeRoute,
        document: Mapping[str, Any],
        id_field: str,
        *,
        certificate: x509.Certificate | None = None,
    ) -> Any:
        """La corrutina de ``POST`` de un registro con su ``Idempotency-Key``."""
        return self.gob.node_send(
            "POST",
            route.path,
            certificate=certificate or zone.cert,
            body=document,
            headers={"Idempotency-Key": str(document[id_field])},
        )

    def post_finding(self, zone: GobZone, document: Mapping[str, Any], **options: Any) -> Any:
        self.gob.advance(0.001)
        return self.gob.run(self.submit(zone, NodeRoute.FINDING, document, "finding_id", **options))

    def post_event(self, zone: GobZone, document: Mapping[str, Any], **options: Any) -> Any:
        self.gob.advance(0.001)
        return self.gob.run(
            self.submit(zone, NodeRoute.OBSERVABILITY_EVENT, document, "event_id", **options)
        )

    def post_heartbeat(self, zone: GobZone, body: Mapping[str, Any] | None = None) -> Any:
        return self.gob.node_call(
            "POST",
            NodeRoute.HEARTBEAT.path,
            certificate=zone.cert,
            body=body if body is not None else self.heartbeat(zone),
        )


CLOSE_ROUTE: Final = "/walk-tests/{session_id}/close"


def require_close_route(gob: GobPlatform) -> None:
    """El cierre del acta por la ruta es de TASK-216 (VIG-158): sin ella en la aplicación, la
    prueba que lo ejercita se omite y lo dice (nunca se da por cumplida)."""
    paths = {route.path for route in iter_declared_routes(gob.app.routes)}
    if CLOSE_ROUTE not in paths:
        pytest.skip("POST /walk-tests/{session_id}/close (TASK-216, VIG-158) no está en main")


def close_body(gob: GobPlatform, zone: GobZone, acceptance: str | None = None) -> dict[str, Any]:
    """Cuerpo de cierre válido: la firma de una coordinación de la organización, las medidas del
    instalador con la línea base de cada cámara y, si se da, la aceptación de falsas alarmas."""
    signer, _ = gob.person(zone.organization_id, Role.COORDINATOR_SST)
    body: dict[str, Any] = {
        "signatures": [{"user_id": str(signer)}],
        "installer_measurements": {
            "beacon_latency_ms_p95": 180,
            "baselines": [
                {
                    "camera_id": str(camera),
                    "zone_id": str(zone.zone_id),
                    "captured_at": "2026-10-01T08:00:00.000Z",
                }
                for camera in zone.cameras
            ],
        },
    }
    if acceptance is not None:
        body["false_alarm_acceptance"] = {"reason_es": acceptance}
    return body
