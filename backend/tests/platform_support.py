"""La aplicación completa contra PostgreSQL 16 real, para los escenarios de abuso y las historias.

TASK-140 (NFR-NUC-48). ``platform_world`` monta ``create_app`` con la cadena fija de middleware y
**todas** las unidades de ``platform_units()`` (``identity``, ``ledger`` y ``platform``), como
``vigia_app``, con los servicios reales de cada ruta: inicio de sesión con segundo factor real
(TOTP con cifrado de sobre sobre un KMS en memoria), invitaciones, usuarios, roles, concesiones,
``EscritorExpediente`` con los tipos de U-02 y los de U-03 que lee la cobertura, servicio de firma
con las cinco claves, puntos de control, verificación de integridad y ``ContextAuthorizer`` con su
``provider_query``. Es la misma composición que ``tests/isolation`` (TASK-139) con las piezas que
los escenarios necesitan además: contraseñas que se pueden comprobar, el verificador de cadenas,
un almacén de evidencias configurable y la política de texto libre con un validador enchufado.

Dobles, solo donde el módulo real tiene sus propias pruebas: el hash de contraseñas (``fake$`` más
la contraseña, sin Argon2id de 64 MB), el segundo factor **de la activación**
(``FakeActivationSecondFactor``, acepta ``246810``), el almacén de evidencias y el validador de
datos de contacto que U-04 registrará en ``FreeTextPolicyRegistry`` (A-45).

Cada petición sale, salvo que se diga otra, de una dirección de red nueva: el límite por origen de
las rutas públicas (60 por minuto) es de un proceso, y los escenarios que lo prueban fijan la suya.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.authz_support import Site
from tests.examples.test_auth_routes import Passwords, cookie_of
from tests.factories import uuid7
from tests.hierarchy_support import (
    LINK_BASE,
    FakeActivationPasswords,
    FakeActivationSecondFactor,
    new_code,
    new_email,
)
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_coverage_port import COVERAGE_TYPES
from tests.live_view_support import LIVE_VIEW_URL, LiveViewEnvironment, live_view_environment
from tests.second_factor_support import FakeKms
from tests.session_support import ORIGIN_KEY, User
from tests.signing_support import ENVIRONMENT
from tests.writer_support import ORDER_TYPE, PROBE_TYPES, save_record_types, unit_context
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.adapters.concession_store import PostgresConcessionStore
from vigia_platform.identity.adapters.http import IdentityHttp
from vigia_platform.identity.adapters.second_factor_store import PostgresSecondFactorStore
from vigia_platform.identity.adapters.session_store import PostgresSessionStore
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.concessions import ConcessionService
from vigia_platform.identity.application.hierarchy import (
    GenesisRequest,
    HierarchyService,
    OrganizationGenesis,
    PlantSpec,
)
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationService,
)
from vigia_platform.identity.application.me import MeService
from vigia_platform.identity.application.organization import OrganizationSettingsService
from vigia_platform.identity.application.password_change import PasswordChangeService
from vigia_platform.identity.application.privacy_notice import PrivacyNoticeService
from vigia_platform.identity.application.roles import RoleService
from vigia_platform.identity.application.users import SecondFactorResetService, UserService
from vigia_platform.identity.auth.login import LoginService
from vigia_platform.identity.auth.second_factor import SecondFactorService
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie, SessionService
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.adapters.http import LedgerHttp
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityResults, SqlIntegrityStore
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.integrity_requests import IntegrityRequests
from vigia_platform.ledger.application.labels import LabelService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente, LedgerRejection, Receipt
from vigia_platform.ledger.chain.checkpoints import CheckpointChain, CheckpointService
from vigia_platform.ledger.chain.verify import IntegrityService
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextRejected,
)
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.adapters.http import PlatformHttp
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.crypto import EnvelopeCipher
from vigia_platform.shared.key_rotation import LedgerRotationRecorder
from vigia_platform.shared.outbox.replay import DeadLetterReplay
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.storage import (
    ANONYMIZED_METADATA_KEY,
    ChecksumType,
    ObjectHead,
    PresignedRequest,
)

__all__ = [
    "CLIP",
    "CONTACT_VALIDATOR",
    "EVIDENCE_TYPE",
    "HOUR",
    "SAME_ORIGIN",
    "T0",
    "Platform",
    "code_of",
    "comparable",
    "platform_world",
    "stamp",
]

STATIC: Final = Path(__file__).resolve().parent / "fixtures" / "static"
ORIGIN: Final = "https://app.vigia.test"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
T0: Final = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
"""Inicio de los periodos de cobertura (las marcas del contenido no dependen del reloj)."""
HOUR: Final = timedelta(hours=1)
CLIP: Final = b"clip sintetico: bytes que el almacen dice tener"
CONTACT_VALIDATOR: Final = "contact_data_probe"
EVIDENCE_TYPE: Final = ORDER_TYPE
"""Tipo de prueba con ``evidence_paths`` (``/clips[*]``); sin eventos propios en esta bandeja."""

_EMAIL: Final = re.compile(r"[^\s@]+@[^\s@]+\.[a-z]{2,}")
_PHONE: Final = re.compile(r"(?<![\w/-])\+?\d(?:[ .-]?\d){6,}(?![\w/-])")
"""Siete o más dígitos con separadores; nunca un trozo de un identificador (``…-4567-8000-…``)."""


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def code_of(response: httpx.Response) -> str | None:
    """El ``code`` del cuerpo de error, o ``None`` si no hay."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def comparable(response: httpx.Response) -> tuple[int, Any]:
    """Estado y cuerpo sin ``correlation_id`` (lo único que cambia entre dos peticiones)."""
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k != "correlation_id"}
    return response.status_code, body


def contact_data_validator(candidate: FreeTextCandidate, field: FreeTextField) -> None:
    """Doble del validador de U-04 (A-45): correos y teléfonos, sobre la forma canónica."""
    if _EMAIL.search(candidate.canonical) or _PHONE.search(candidate.canonical):
        raise FreeTextRejected("person_attribution", field, "el texto contiene datos de contacto")


# --- Almacén de evidencias --------------------------------------------------------------------


@dataclass
class EvidenceStorage:
    """``HEAD`` configurable por clave; sin entrada, el objeto ``CLIP`` íntegro y anonimizado."""

    objects: dict[str, ObjectHead | None] = field(default_factory=dict)
    presigned: list[str] = field(default_factory=list)

    async def head_object(self, key: str) -> ObjectHead | None:
        if key in self.objects:
            return self.objects[key]
        return self.head(key, CLIP)

    @staticmethod
    def head(
        key: str, data: bytes, *, marker: str | None = "1", size: int | None = None
    ) -> ObjectHead:
        return ObjectHead(
            key=key,
            size_bytes=len(data) if size is None else size,
            checksum_sha256=base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type="video/mp4",
            metadata={} if marker is None else {ANONYMIZED_METADATA_KEY: marker},
            version_id="v-sintetica-1",
        )

    async def presign_get(
        self, key: str, ttl: timedelta = timedelta(minutes=5), *, version_id: str | None = None
    ) -> PresignedRequest:
        self.presigned.append(key)
        return PresignedRequest(
            method="GET",
            url=f"https://almacen.vigia.test/{key}?versionId={version_id}&X-Amz-Expires=300",
            headers={},
            expires_at=datetime(2026, 9, 29, 10, 35, tzinfo=UTC),
        )


# --- El mundo ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Genesis:
    """Una organización de la orden de alta con su primer administrador ya activado."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    admin_id: uuid.UUID
    admin_email: str
    password: str


@dataclass
class Platform:
    env: LiveViewEnvironment
    app: Any
    writer: EscritorExpediente
    checkpoints: CheckpointService
    verifier: IntegrityService
    signing: SigningService
    storage: EvidenceStorage
    deps: IdentityDependencies
    activation: FakeActivationSecondFactor
    senders: EmailSenderRegistry
    passwords: Passwords

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

    async def connect_as(self, role: str | None) -> Any:
        """Conexión directa a la base como ``role`` (``None``: superusuario).

        Los roles son del clúster y cada base migrada en la sesión les cambia la contraseña, así
        que se entra como superusuario y se cambia de rol con ``SET ROLE``: los permisos y la
        seguridad de fila son los de ``role``.
        """
        connection = await self.authz.sessions.migrated.connect()
        if role is not None:
            await connection.execute(f"SET ROLE {role}")
        return connection

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def resync(self) -> None:
        """Deja el mundo como al montarlo antes de cada escenario.

        - La hora de pared simulada vuelve a la de la base (el reloj monótono no retrocede): la
          RLS de las concesiones compara la vigencia con ``now()`` de la base (nuc_0009), y un
          escenario que avanzó horas el reloj no debe dejar al siguiente con concesiones
          «futuras».
        - Las contraseñas de ``vigia_app`` y ``vigia_migrate`` vuelven a ser las de esta base: los
          roles son del clúster y cada base que migra otro escenario (N-2, N-10) se las cambia;
          sin esto, la conexión nueva que abre el pool en una prueba concurrente fallaría.
        """
        migrated = self.authz.sessions.migrated
        for role, password in (
            ("vigia_app", migrated.app_password),
            ("vigia_migrate", migrated.migrate_password),
        ):
            # ``ALTER ROLE`` no admite parámetros; la contraseña es un token URL-safe generado.
            assert password.replace("-", "").replace("_", "").isalnum()
            self.execute(f"ALTER ROLE {role} PASSWORD '{password}'")
        (row,) = self.fetch("SELECT now() AS now")
        self.clock.set(row["now"])

    # --- HTTP -------------------------------------------------------------------------------

    def client(self, address: str | None = None) -> httpx.AsyncClient:
        """Un cliente desde ``address`` (por defecto, una dirección nueva de 198.18.0.0/15)."""
        host = address or f"198.18.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"
        transport = httpx.ASGITransport(app=self.app, client=(host, 40_000))
        return httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30.0)

    def headers(
        self,
        cookie: SessionCookie | None = None,
        concession: uuid.UUID | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = dict(SAME_ORIGIN)
        if cookie is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value}"
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        headers.update(extra or {})
        return headers

    async def send(
        self,
        method: str,
        path: str,
        *,
        cookie: SessionCookie | None = None,
        concession: uuid.UUID | None = None,
        params: Any = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        address: str | None = None,
    ) -> httpx.Response:
        async with self.client(address) as client:
            response: httpx.Response = await client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=self.headers(cookie, concession, headers),
            )
            return response

    def call(self, method: str, path: str, **options: Any) -> httpx.Response:
        response: httpx.Response = self.run(self.send(method, path, **options))
        return response

    def login(
        self, email: str, password: str, *, address: str | None = None
    ) -> tuple[httpx.Response, SessionCookie | None]:
        response = self.call(
            "POST",
            "/auth/login",
            json_body={"email": email, "password": password},
            address=address,
        )
        return response, cookie_of(response)

    # --- Altas ------------------------------------------------------------------------------

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

    def account(self, organization_id: uuid.UUID, *roles: Role) -> User:
        """Una cuenta con contraseña (correo y contraseña) y ``roles`` de organización."""
        user: User = self.authz.sessions.add_user(organization_id)
        for role in roles:
            self.authz.assign(organization_id, user.user_id, role)
        return user

    def installer(self) -> tuple[uuid.UUID, SessionCookie]:
        user_id: uuid.UUID = self.authz.add_provider_user()
        cookie: SessionCookie = self.authz.open_session(self.provider, user_id)
        return user_id, cookie

    def operator(self) -> SessionCookie:
        cookie: SessionCookie = self.authz.open_session(self.provider, self.authz.operator_id)
        return cookie

    def node(self, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID) -> uuid.UUID:
        node_id: uuid.UUID = self.env.add_node(site.organization_id, plant_id, url=LIVE_VIEW_URL)
        self.env.assign_node(site.organization_id, plant_id, zone_id, node_id, at=T0 - HOUR)
        return node_id

    def genesis(self) -> Genesis:
        """Organización de la orden de alta con su administrador activado por la invitación.

        La activación pasa el segundo factor del doble, que no lo guarda: en su primer inicio de
        sesión el administrador tiene que inscribirlo (el caso de H-58)."""
        email = new_email("admin")
        operator = self.run(self.authz.contexts.context_from_operator(self.authz.operator_id))
        result = self.run(
            OrganizationGenesis(
                self.deps, senders=self.senders, link_base=LINK_BASE
            ).create_client_organization(
                operator,
                GenesisRequest(
                    code=new_code("ORG"),
                    name="Organización sintética",
                    plant=PlantSpec(
                        new_code("PL"), "Planta sintética", "CO", "us-east-1", "America/Bogota"
                    ),
                    administrator_email=email,
                    administrator_display_name="Administración sintética",
                ),
            )
        )
        link = result.invitation.link
        assert link is not None
        password = f"clave-sintetica-{secrets.token_hex(4)}"
        activated = self.call(
            "POST",
            f"/invitations/{link.split('#', 1)[1]}/accept",
            json_body={
                "step": "complete",
                "password": password,
                "notice_version": CURRENT_PRIVACY_NOTICE_VERSION,
                "second_factor_code": "246810",
            },
        )
        assert activated.status_code == 200, activated.text
        return Genesis(
            result.organization_id,
            result.plant_id,
            result.administrator_user_id,
            email,
            password,
        )

    def session_context(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> ScopeContext:
        context: ScopeContext = self.env.session_context(organization_id, user_id)
        return context

    # --- Expediente -------------------------------------------------------------------------

    def write(
        self,
        organization_id: uuid.UUID,
        record_type: str,
        document: dict[str, Any],
        occurred_at: datetime | None = None,
        unit: ActorUnit = ActorUnit.U03,
    ) -> Receipt | LedgerRejection:
        context = unit_context(organization_id, unit, kind=ActorKind.SYSTEM)
        result: Receipt | LedgerRejection = self.run(
            self.writer.write(context, record_type, document, occurred_at=occurred_at)
        )
        return result

    def written(self, organization_id: uuid.UUID, record_type: str, document: Any) -> uuid.UUID:
        receipt = self.write(organization_id, record_type, document)
        assert isinstance(receipt, Receipt), receipt
        return receipt.record_id

    def gate(self, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID, at: datetime) -> uuid.UUID:
        receipt = self.write(
            site.organization_id,
            "gate_state_changed",
            {
                "zone_id": str(zone_id),
                "plant_id": str(plant_id),
                "gate": "use",
                "status": "approved",
                "resulting_mode": "productive",
            },
            occurred_at=at,
        )
        assert isinstance(receipt, Receipt), receipt
        return receipt.record_id

    def checkpoint(self, organization_id: uuid.UUID, plant_id: uuid.UUID) -> Any:
        context = unit_context(organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)
        (result,) = self.run(
            self.checkpoints.write_checkpoints_now(context, [CheckpointChain.plant(plant_id)])
        )
        return result.checkpoint

    def observability(
        self,
        site: Site,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        node_id: uuid.UUID,
        started: datetime,
        state: str = "observable",
        causes: tuple[str, ...] = (),
    ) -> uuid.UUID:
        """``observability_event_received`` de la zona: lo que el nodo **declara**."""
        return self.written(
            site.organization_id,
            "observability_event_received",
            {
                "event_id": str(uuid7()),
                "contract_version": "1.0.0",
                "organization_id": str(site.organization_id),
                "plant_id": str(plant_id),
                "zone_id": str(zone_id),
                "node_id": str(node_id),
                "subject": {"kind": "zone"},
                "phase": "opened",
                "state": state,
                "causes": list(causes),
                "started_at": stamp(started),
                "node_time": {
                    "started_at": stamp(started),
                    "ended_at": stamp(started),
                    "clock": {"synchronized": True, "offset_ms": 0, "source": "ntp.local"},
                },
                "evidence": [],
                "software_version": "1.0.0",
            },
        )

    def communication(
        self,
        site: Site,
        plant_id: uuid.UUID,
        node_id: uuid.UUID,
        state: str,
        since: datetime,
        last_heartbeat: datetime | None = None,
    ) -> uuid.UUID:
        """``node_communication_state_changed``: lo que la **plataforma** supo del nodo."""
        document: dict[str, Any] = {
            "node_id": str(node_id),
            "plant_id": str(plant_id),
            "state": state,
            "since": stamp(since),
        }
        if last_heartbeat is not None:
            document["last_heartbeat_at"] = stamp(last_heartbeat)
        return self.written(site.organization_id, "node_communication_state_changed", document)

    # --- Lecturas como superusuario ---------------------------------------------------------

    def audit_entries(self, organization_id: uuid.UUID, operation: str) -> list[Any]:
        return self.fetch(
            "SELECT * FROM shared.audit_entry WHERE organization_id = $1 AND operation = $2"
            " ORDER BY chain_sequence",
            organization_id,
            operation,
        )

    def events(self, organization_id: uuid.UUID, name: str) -> list[Any]:
        return self.fetch(
            "SELECT * FROM shared.outbox_event WHERE organization_id = $1 AND event_name = $2"
            " ORDER BY publish_seq",
            organization_id,
            name,
        )

    def alerts(self, organization_id: uuid.UUID, alert_kind: str) -> list[dict[str, Any]]:
        return [
            payload
            for payload in (
                json.loads(row["payload"]) for row in self.events(organization_id, "security_alert")
            )
            if payload["alert_kind"] == alert_kind
        ]

    def records(self, organization_id: uuid.UUID, record_type: str | None = None) -> list[Any]:
        return self.fetch(
            "SELECT * FROM ledger.ledger_record WHERE organization_id = $1"
            " AND ($2::text IS NULL OR record_type = $2) ORDER BY plant_id, chain_sequence",
            organization_id,
            record_type,
        )


@contextlib.contextmanager
def platform_world(postgres_endpoint: PostgresEndpoint, prefix: str) -> Iterator[Platform]:
    # resync() lleva el reloj al now() de la base: las claves de firma nacen a esa hora para que
    # sigan vigentes sea cual sea la fecha real (VIG-135).
    with live_view_environment(postgres_endpoint, prefix, at_database_time=True) as env:
        authz = env.authz
        sessions = authz.sessions
        registry = RecordTypeRegistry()
        evidence_type = replace(
            next(t for t in PROBE_TYPES if t.record_type == EVIDENCE_TYPE), outbox_events=()
        )
        for definition in (*U02_RECORD_TYPES, *COVERAGE_TYPES, evidence_type):
            registry.register(definition)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
            registry.seal()

        env.run(synchronize())
        storage = EvidenceStorage()
        free_text = FreeTextPolicyRegistry()
        free_text.register(CONTACT_VALIDATOR, contact_data_validator)
        free_text.seal()
        writer = EscritorExpediente(
            database=sessions.database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(storage, env.clock),
            outbox=sessions.outbox,
            clock=env.clock,
        )
        provider = authz.provider_organization_id
        deps = IdentityDependencies(
            database=sessions.database,
            writer=writer,
            audit=sessions.audit,
            outbox=sessions.outbox,
            authorizer=authz.authorizer,
            free_text=free_text,
            clock=sessions.clock,
            provider_organization_id=provider,
        )
        pool = CpuPool(sessions.clock, max_workers=2)
        store = PostgresSessionStore(sessions.database, sessions.audit, sessions.outbox)
        second_factor = SecondFactorService(
            PostgresSecondFactorStore(sessions.database, sessions.audit),
            EnvelopeCipher(FakeKms(), "alias/vigia-secrets", sessions.clock),
            pool,
            sessions.clock,
        )
        passwords = Passwords()
        activation = FakeActivationSecondFactor()
        senders = EmailSenderRegistry()
        identity = IdentityHttp(
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
                second_factor=activation,
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
            me=MeService(
                sessions.database, contexts=authz.contexts, provider_organization_id=provider
            ),
            provider_organization_id=provider,
            users=UserService(deps, senders=senders, link_base=LINK_BASE),
            roles=RoleService(deps),
            second_factor_reset=SecondFactorResetService(deps, second_factor),
            hierarchy=HierarchyService(deps),
            organization=OrganizationSettingsService(deps),
            concessions=ConcessionService(
                store=PostgresConcessionStore(database=sessions.database, audit=sessions.audit),
                writer=writer,
                authorizer=authz.authorizer,
                contexts=authz.contexts,
                clock=sessions.clock,
            ),
        )
        # El servicio de firma de la proveedora sembrada: mismas claves y mismo gestor que el del
        # entorno de vista en vivo.
        signing = SigningService(
            provider_organization_id=provider,
            store=env.store,
            secrets=env.secrets,
            events=env.events,
            clock=env.clock,
            environment=ENVIRONMENT,
        )
        env.run(signing.start())
        # Como en producción (SqlSigningKeyStore): cada rotación escribe sus registros y su
        # auditoría antes de quedar aplicada (VIG-88, revisión de VIG-93).
        env.store.recorder = LedgerRotationRecorder(
            database=sessions.database, writer=writer, audit=sessions.audit
        )
        checkpoints = CheckpointService(
            store=SqlCheckpointStore(
                database=sessions.database,
                writer=writer,
                audit=sessions.audit,
                outbox=sessions.outbox,
            ),
            signer=signing,
            clock=env.clock,
        )
        verifier = IntegrityService(
            store=SqlIntegrityStore(
                database=sessions.database, audit=sessions.audit, outbox=sessions.outbox
            ),
            keys=checkpoints,
            clock=env.clock,
        )
        ledger = LedgerHttp(
            reader=LectorExpediente(database=sessions.database, audit=sessions.audit),
            evidence=EvidenceService(
                database=sessions.database, audit=sessions.audit, storage=storage
            ),
            labels=LabelService(database=sessions.database, audit=sessions.audit),
            coverage=CoverageService(database=sessions.database, audit=sessions.audit),
            integrity_results=SqlIntegrityResults(database=sessions.database),
            integrity_requests=IntegrityRequests(
                database=sessions.database, outbox=sessions.outbox, clock=env.clock
            ),
            checkpoints=checkpoints,
            live_view=env.service(),
            authorizer=authz.authorizer,
            provider_organization_id=provider,
        )
        platform = PlatformHttp(
            signing=signing,
            dead_letter=DeadLetterReplay(
                database=sessions.database,
                authorizer=authz.authorizer,
                audit=sessions.audit,
                clock=env.clock,
            ),
            operators=authz.contexts,
            authorizer=authz.authorizer,
            audit=sessions.audit,
            provider_organization_id=provider,
        )
        app = World(clock=sessions.clock).app(
            units=None,
            permissions=None,
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=provider,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=sessions.clock,
                ),
                "identity": identity,
                "ledger": ledger,
                "platform": platform,
            },
            static_dir=STATIC,
            public_origin=ORIGIN,
        )
        world = Platform(
            env=env,
            app=app,
            writer=writer,
            checkpoints=checkpoints,
            verifier=verifier,
            signing=signing,
            storage=storage,
            deps=deps,
            activation=activation,
            senders=senders,
            passwords=passwords,
        )
        world.resync()
        try:
            yield world
        finally:
            pool.shutdown()
