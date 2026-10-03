"""Apoyo de las pruebas de ``vigia-admin`` (TASK-132).

Dos mundos:

- **Unidad** (``FakeWorld``): ``AdminRuntime`` con dobles que anotan cada llamada en ``calls``
  (las de escritura y las de lectura por separado), un KMS en memoria con claves P-256 reales
  (``FakeKms``: hace de KMS, la clave privada solo existe en la prueba) y un depósito en memoria.
  El ``ScopeContexts`` y el ``NodeCaPublisher`` son los reales, igual que la validación de
  ``OrganizationGenesis.check``.
- **Integración** (``IntegrationWorld``): el constructor de ``AdminRuntime`` con todo real sobre
  la base migrada (como ``vigia_app``) y LocalStack (KMS con una clave ``ECC_NIST_P256``,
  Secrets Manager y S3): escritor del expediente, auditoría, bandeja, contextos, autorización,
  génesis, ``SigningService`` con ``SqlSigningKeyStore``, reproceso, particiones, ensayo y raíz.

``issue_node_certificate`` emite, con la misma firma de KMS, una credencial de cliente de un nodo
sintético (lo que hará U-03): sirve para comprobar que un nodo de la raíz anterior sigue
validando contra el paquete de dos raíces. Solo datos generados.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store
from sqlalchemy import text
from vigia_contracts.clock import SimulatedClock
from vigia_contracts.signing import public_key_base64

from vigia_platform.identity.adapters.authz_store import (
    PostgresAuthorizationAudit,
    PostgresContextStore,
)
from vigia_platform.identity.application.admin_cli import AdminConfig, AdminRuntime, run
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import (
    FirstOperator,
    GenesisRequest,
    GenesisResult,
    OrganizationGenesis,
    ProviderGenesisRequest,
    ProviderGenesisResult,
)
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationDelivery,
    InvitationOutcome,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource
from vigia_platform.identity.authz.context import (
    OperatorRow,
    ScopeContexts,
    SessionRow,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditReceipt,
    AuditWriter,
)
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.archive.partitions import (
    PartitionedTable,
    PartitionMaintenance,
    PartitionReport,
    PartitionResult,
)
from vigia_platform.shared.archive.restore_drill import DrillResult, RestoreDrills
from vigia_platform.shared.clock import Clock, SystemClock
from vigia_platform.shared.context import AllowedScope, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.key_rotation import LedgerKeyEventWriter, LedgerRotationRecorder
from vigia_platform.shared.node_ca import (
    NodeCaPublisher,
    PreparedRoot,
    PublishedRoot,
    sign_certificate,
)
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.replay import DeadLetterReplay, ReplayReceipt
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.secrets import (
    AwsCredentials,
    AwsSettings,
    KmsAdapter,
    SecretsManagerAdapter,
)
from vigia_platform.shared.signing.keys import (
    KEY_LIFETIME,
    KeySetPublicationRecord,
    KeyStatus,
    SigningKeyRecord,
    SigningPurpose,
)
from vigia_platform.shared.signing.service import RotationResult, SigningService
from vigia_platform.shared.signing_store import SqlSigningKeyStore
from vigia_platform.shared.storage import ChecksumType, ObjectHead, S3Storage

LINK_BASE: Final = "https://app.vigia.example"
START: Final = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
SYSTEM_ACTOR_ID: Final = uuid.UUID("00000000-0000-4000-8000-000000000132")
PROVIDER_ID: Final = uuid.UUID("0192f2b0-0000-4000-8000-0000000000aa")
OPERATOR_ID: Final = uuid.UUID("0192f2b0-0000-4000-8000-0000000000bb")
TOKEN: Final = "T" * 43
"""Token sintético de las invitaciones de los dobles (43 caracteres base64url)."""


# --- KMS y depósito en memoria ------------------------------------------------------------------


class FakeKms:
    """``kms:Sign`` y ``kms:GetPublicKey`` con claves P-256 en memoria (hace de KMS)."""

    def __init__(self) -> None:
        self.keys: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.signed: list[str] = []
        self.sign_with: dict[str, str] = {}
        """``key_id`` → otra clave con la que firmar de verdad (KMS que firma mal)."""

    def add(self, key_id: str, curve: ec.EllipticCurve | None = None) -> str:
        self.keys[key_id] = ec.generate_private_key(curve or ec.SECP256R1())
        return key_id

    async def sign(self, key_id: str, message: bytes) -> bytes:
        self.signed.append(key_id)
        signer = self.keys[self.sign_with.get(key_id, key_id)]
        return signer.sign(message, ec.ECDSA(hashes.SHA256()))

    async def get_public_key(self, key_id: str) -> bytes:
        return public_der(self.keys[key_id].public_key())


def public_der(public: Any) -> bytes:
    der: bytes = public.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return der


class MemoryStorage:
    """``get_object`` y ``put_object`` de un depósito con versiones, en memoria."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    async def head_object(self, key: str) -> ObjectHead | None:
        body = self.objects.get(key)
        if body is None:
            return None
        return ObjectHead(
            key=key,
            size_bytes=len(body),
            checksum_sha256=None,
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type="application/x-pem-file",
            metadata={},
            version_id=f"v{len(self.puts)}",
        )

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        return self.objects[key]

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead:
        self.objects[key] = body
        self.puts.append(key)
        return ObjectHead(
            key=key,
            size_bytes=len(body),
            checksum_sha256=None,
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type=content_type,
            metadata={},
            version_id=f"v{len(self.puts)}",
        )


async def issue_node_certificate(
    kms: Any, key_id: str, root: x509.Certificate, now: datetime
) -> x509.Certificate:
    """Credencial de cliente de un nodo sintético firmada por ``key_id`` (como U-03)."""
    node_key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nodo-sintetico")]))
        .issuer_name(root.subject)
        .public_key(node_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("nodo-sintetico.nodes.vigia.test")]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                cast(ec.EllipticCurvePublicKey, root.public_key())
            ),
            critical=False,
        )
    )
    return await sign_certificate(builder, kms, key_id)


def validates(leaf: x509.Certificate, roots: Iterable[x509.Certificate], at: datetime) -> bool:
    """``True`` si ``leaf`` valida como cliente contra el almacén ``roots``."""
    verifier = PolicyBuilder().store(Store(list(roots))).time(at).build_client_verifier()
    try:
        verifier.verify(leaf, [])
    except Exception:
        return False
    return True


# --- Dobles de la orden ---------------------------------------------------------------------------


class Unused:
    """Dependencia que la operación probada no debe tocar."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"dependencia inesperada: {name}")


@dataclass
class Calls:
    writes: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)


class FakeContextStore:
    """``ContextStore`` con un operador activo (``OPERATOR_ID``) en la proveedora."""

    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.active: set[uuid.UUID] = {OPERATOR_ID}

    async def session_row(
        self,
        lookup: ScopeContext,
        session_id_hash: str,
        now: datetime,
        concession_id: uuid.UUID | None,
    ) -> SessionRow | None:  # pragma: no cover - la orden no usa sesiones
        raise AssertionError("sin sesiones en vigia-admin")

    async def operator_row(self, lookup: ScopeContext, user_id: uuid.UUID) -> OperatorRow | None:
        self.calls.reads.append("operator_row")
        if user_id not in self.active:
            return None
        scope = AllowedScope(ScopeLevel.ORGANIZATION, PROVIDER_ID, Role.PLATFORM_OPERATOR)
        return OperatorRow(user_id, "Operadora sintética", (scope,))


@dataclass
class FakeTransaction:
    context: ScopeContext


class FakeDatabase:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.disposed = False

    @contextlib.asynccontextmanager
    async def transaction(self, context: ScopeContext) -> AsyncIterator[Transaction]:
        self.calls.writes.append("transaction")
        yield cast(Transaction, FakeTransaction(context))

    async def dispose(self) -> None:
        self.disposed = True


def _invitation(user_id: uuid.UUID, now: datetime) -> InvitationOutcome:
    return InvitationOutcome(
        user_id=user_id,
        invitation_id=uuid.uuid4(),
        expires_at=now + timedelta(hours=72),
        delivery=InvitationDelivery.LINK_DISCLOSED,
        link=f"{LINK_BASE}/invitacion#{TOKEN}",
    )


class FakeGenesis:
    """``OrganizationGenesis`` con la validación real y la escritura anotada."""

    def __init__(self, calls: Calls, clock: Clock) -> None:
        deps = IdentityDependencies(
            database=cast(Any, Unused()),
            writer=cast(Any, Unused()),
            audit=cast(Any, Unused()),
            outbox=cast(Any, Unused()),
            authorizer=cast(Any, Unused()),
            free_text=FreeTextPolicyRegistry(),
            clock=clock,
            provider_organization_id=PROVIDER_ID,
        )
        self.real = OrganizationGenesis(deps, senders=EmailSenderRegistry(), link_base=LINK_BASE)
        self.calls = calls
        self.clock = clock
        self.operator: FirstOperator | None = None

    def check(self, request: GenesisRequest) -> GenesisRequest:
        return self.real.check(request)

    def check_provider(self, request: ProviderGenesisRequest) -> ProviderGenesisRequest:
        return self.real.check_provider(request)

    async def create_client_organization(
        self, operator_context: ScopeContext, request: GenesisRequest
    ) -> GenesisResult:
        self.calls.writes.append("create_client_organization")
        user_id = uuid.uuid4()
        return GenesisResult(
            uuid.uuid4(), uuid.uuid4(), user_id, _invitation(user_id, self.clock.now())
        )

    async def create_provider_organization(
        self, bootstrap_context: ScopeContext, request: ProviderGenesisRequest
    ) -> ProviderGenesisResult:
        self.calls.writes.append("create_provider_organization")
        operator_id = bootstrap_context.actor.id
        return ProviderGenesisResult(
            bootstrap_context.organization_id,
            operator_id,
            _invitation(operator_id, self.clock.now()),
        )

    async def first_operator(self, provider_context: ScopeContext) -> FirstOperator | None:
        self.calls.reads.append("first_operator")
        return self.operator

    async def reissue_operator_invitation(
        self, bootstrap_context: ScopeContext, operator: FirstOperator
    ) -> InvitationOutcome | None:
        self.calls.writes.append("reissue_operator_invitation")
        return _invitation(operator.user_id, self.clock.now())


class FakeSigning:
    """``SigningService`` mínimo: una clave nueva por rotación, sin material fuera."""

    def __init__(self, calls: Calls, clock: Clock) -> None:
        self.calls = calls
        self.clock = clock
        self.ready = False
        self.keys: list[SigningKeyRecord] = []
        self.rotated: list[SigningPurpose] = []
        self.records_rotation = True
        """Como ``SigningService`` con ``SqlSigningKeyStore`` y ``LedgerRotationRecorder``."""

    async def start(self, *, required: Iterable[SigningPurpose] = tuple(SigningPurpose)) -> None:
        self.calls.reads.append("signing.start")
        self.ready = True

    def all_keys(self) -> tuple[SigningKeyRecord, ...]:
        return tuple(self.keys)

    def current_publication(self) -> KeySetPublicationRecord | None:
        return None

    async def rotate(self, purpose: SigningPurpose, *, context: ScopeContext) -> RotationResult:
        self.calls.writes.append(f"rotate:{purpose.value}")
        self.rotated.append(purpose)
        now = self.clock.now()
        previous = next(
            (k for k in self.keys if k.purpose is purpose and k.status is KeyStatus.ACTIVE), None
        )
        key = SigningKeyRecord(
            key_id=f"{purpose.value}-{len(self.keys)}",
            purpose=purpose,
            public_key=public_key_base64(ec_ed25519()),
            private_key_ref=f"vigia/test/signing/{purpose.value}/{len(self.keys)}",
            valid_from=now,
            valid_until=now + KEY_LIFETIME,
            status=KeyStatus.ACTIVE,
            created_at=now,
            rotated_by=context.actor.id,
        )
        self.keys = [k for k in self.keys if k is not previous] + [key]
        return RotationResult(
            new_key=key,
            previous_key_id=None if previous is None else previous.key_id,
            publication=None,
        )


def ec_ed25519() -> Any:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


class FakeAuthorizer:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls

    async def authorize(
        self, context: ScopeContext, key: PermissionKey, resource: Resource
    ) -> ScopeContext:
        self.calls.reads.append(f"authorize:{key.value}")
        return context


class FakeReplay:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls

    async def replay(
        self, context: ScopeContext, event_id: uuid.UUID, consumer_name: str
    ) -> ReplayReceipt:
        self.calls.writes.append("replay")
        return ReplayReceipt(event_id, consumer_name, uuid.uuid4())


class FakePartitions:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls

    async def create(
        self, transaction: Transaction, *, until: date | None = None
    ) -> PartitionReport:
        self.calls.writes.append("partitions.create")
        assert until is not None
        month = date(until.year, until.month, 1)
        result = PartitionResult(
            PartitionedTable.AUDIT_ENTRY, "shared.audit_entry_x", month, True, False
        )
        return PartitionReport(month, month, (result,), {})


class FakeDrills:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls

    async def record(self, operator_context: ScopeContext, result: DrillResult) -> AuditReceipt:
        self.calls.writes.append(f"drill:{result.value}")
        return AuditReceipt(uuid.uuid4(), 7, START)


class FakeSecrets:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.values: dict[str, list[str]] = {}

    async def put_one_time(self, name: str, value: str) -> str:
        self.calls.writes.append("put_one_time")
        self.values.setdefault(name, []).append(value)
        return f"arn:aws:secretsmanager:us-east-1:000000000000:secret:{name}-abcdef"


class FakeArchive:
    def __init__(self, calls: Calls, data: bytes = b"") -> None:
        self.calls = calls
        self.data = data

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        self.calls.reads.append("archive.get_object")
        return self.data


class RecordingStorage(MemoryStorage):
    def __init__(self, calls: Calls) -> None:
        super().__init__()
        self.calls = calls

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        self.calls.reads.append("edge.get_object")
        return await super().get_object(key)

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead:
        self.calls.writes.append("edge.put_object")
        return await super().put_object(key, body, content_type)


@dataclass
class FakeWorld:
    """``AdminRuntime`` de dobles y lo necesario para ejecutar ``run``."""

    calls: Calls = field(default_factory=Calls)
    clock: SimulatedClock = field(default_factory=lambda: SimulatedClock(START))
    kms: FakeKms = field(default_factory=FakeKms)
    environ: dict[str, str] = field(default_factory=dict)
    archive_data: bytes = b""
    synchronized: int = 0

    def __post_init__(self) -> None:
        self.kms.add("vigia-node-ca")
        self.kms.add("vigia-node-ca-2")
        self.storage = RecordingStorage(self.calls)
        self.secrets = FakeSecrets(self.calls)
        self.genesis = FakeGenesis(self.calls, self.clock)
        self.signing = FakeSigning(self.calls, self.clock)
        self.store = FakeContextStore(self.calls)
        self.database = FakeDatabase(self.calls)
        self.archive = FakeArchive(self.calls, self.archive_data)
        self.audit = FakeAudit(self.calls)
        self.provider_ids: list[uuid.UUID] = []
        self.environ = {
            "VIGIA_ENVIRONMENT": "test",
            "VIGIA_PROVIDER_ORGANIZATION_ID": str(PROVIDER_ID),
            "VIGIA_PUBLIC_ORIGIN": LINK_BASE,
            "VIGIA_NODE_CA_KEY_ARN": "vigia-node-ca",
            "VIGIA_EDGE_BUCKET": "vigia-edge-test",
            "VIGIA_ARCHIVE_BUCKET": "vigia-archive-test",
        } | self.environ

    async def builder(self, config: AdminConfig, provider_id: uuid.UUID) -> AdminRuntime:
        self.provider_ids.append(provider_id)
        contexts = ScopeContexts(
            store=self.store,
            clock=self.clock,
            provider_organization_id=provider_id,
            system_actor_id=SYSTEM_ACTOR_ID,
        )

        async def synchronize() -> None:
            self.calls.writes.append("synchronize")
            self.synchronized += 1

        return AdminRuntime(
            clock=self.clock,
            database=self.database,
            contexts=contexts,
            authorizer=FakeAuthorizer(self.calls),
            genesis=self.genesis,
            signing=self.signing,
            replay=FakeReplay(self.calls),
            partitions=FakePartitions(self.calls),
            drills=FakeDrills(self.calls),
            secrets=self.secrets,
            audit=self.audit,
            node_ca=RecordingNodeCa(
                NodeCaPublisher(
                    storage=self.storage,
                    kms=self.kms,
                    environment=config.environment,
                    random_bytes=os.urandom,
                ),
                self.calls,
            ),
            archive=self.archive,
            registries=(synchronize,),
        )

    def run(
        self, *argv: str, stdin: str = "", environ: Mapping[str, str] | None = None
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = run(
            list(argv),
            environ=self.environ if environ is None else environ,
            builder=self.builder,
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()


class RecordingNodeCa:
    """``NodeCaPublisher`` real con sus llamadas anotadas."""

    def __init__(self, publisher: NodeCaPublisher, calls: Calls) -> None:
        self.publisher = publisher
        self.calls = calls

    async def check_key(self, key_id: str) -> None:
        self.calls.reads.append("check_key")
        await self.publisher.check_key(key_id)

    async def prepare_root(self, key_id: str, *, now: datetime) -> PreparedRoot:
        self.calls.reads.append("prepare_root")
        return await self.publisher.prepare_root(key_id, now=now)

    async def prepare_rotation(self, new_key_id: str, *, now: datetime) -> PreparedRoot:
        self.calls.reads.append("prepare_rotation")
        return await self.publisher.prepare_rotation(new_key_id, now=now)

    async def publish(self, prepared: PreparedRoot) -> PublishedRoot:
        self.calls.writes.append("publish")
        return await self.publisher.publish(prepared)


class FakeAudit:
    """``AuditWriter.append`` que anota cada entrada; ``fail_on`` hace fallar una operación."""

    def __init__(self, calls: Calls) -> None:
        self.calls = calls
        self.entries: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_on: str | None = None

    async def append(
        self,
        context: ScopeContext,
        operation: AuditOperation,
        *,
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        filters: Mapping[str, Any] | None = None,
    ) -> AuditReceipt:
        if operation.value == self.fail_on:
            raise ConnectionError("la auditoría se cae (sintético)")
        self.calls.writes.append(f"audit:{operation.value}")
        self.entries.append((operation.value, outcome.value, dict(filters or {})))
        return AuditReceipt(uuid.uuid4(), len(self.entries), START)


def output(stdout: str) -> dict[str, Any]:
    """La única línea JSON de la salida."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, stdout
    document: dict[str, Any] = json.loads(lines[0])
    return document


# --- Integración ------------------------------------------------------------------------------

_SAVE_RECORD_TYPE: Final = text(
    "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
    " content_schema, source_key_path, free_text_paths, evidence_paths, label_rule,"
    " outbox_events, chain_follows_scope) VALUES (:record_type, :writer_unit, :chain_level,"
    " :schema_version, CAST(:content_schema AS jsonb), :source_key_path,"
    " CAST(:free_text_paths AS text[]), CAST(:evidence_paths AS text[]),"
    " CAST(:label_rule AS jsonb), CAST(:outbox_events AS text[]), :chain_follows_scope)"
    " ON CONFLICT DO NOTHING"
)


class NoEvidenceStorage:
    """Los tipos de U-02 no tienen evidencias: el escritor nunca consulta el almacén."""

    async def head_object(self, key: str) -> ObjectHead | None:
        raise AssertionError("los registros de U-02 no llevan evidencias")


async def _save_record_types(
    database: Database, context: ScopeContext, registry: RecordTypeRegistry
) -> None:
    async with database.transaction(context) as transaction:
        for compiled in registry.latest():
            row = compiled.to_persisted()
            await transaction.execute(
                _SAVE_RECORD_TYPE,
                {
                    "record_type": row.record_type,
                    "writer_unit": row.writer_unit,
                    "chain_level": row.chain_level,
                    "schema_version": row.schema_version,
                    "content_schema": json.dumps(row.content_schema),
                    "source_key_path": row.source_key_path,
                    "free_text_paths": list(row.free_text_paths),
                    "evidence_paths": list(row.evidence_paths),
                    "label_rule": None if row.label_rule is None else json.dumps(row.label_rule),
                    "outbox_events": list(row.outbox_events),
                    "chain_follows_scope": row.chain_follows_scope,
                },
            )


class OffsetClock:
    """La hora real más ``offset``: la base fija ``received_at`` y ``occurred_at`` con su propio
    reloj, así que las pruebas de integración van con la hora real y avanzan con el desfase."""

    def __init__(self) -> None:
        self._system = SystemClock()
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return self._system.now() + self.offset

    def monotonic(self) -> float:
        return self._system.monotonic() + self.offset.total_seconds()

    def sleep(self, seconds: float) -> None:  # pragma: no cover - no se usa
        self._system.sleep(seconds)


@dataclass
class BuiltRuntime:
    """Lo que el constructor de integración dejó a mano para las comprobaciones."""

    runtime: AdminRuntime
    deps: IdentityDependencies
    contexts: ScopeContexts
    signing: SigningService
    database: Database


@dataclass
class IntegrationWorld:
    """Constructor real de ``AdminRuntime`` sobre la base migrada y LocalStack."""

    database_factory: Callable[[], Database]
    localstack_url: str
    region: str
    node_ca_key_id: str
    edge_bucket: str
    archive_bucket: str
    secrets_key_id: str
    environment: str = field(default_factory=lambda: f"it-{secrets.token_hex(4)}")
    clock: OffsetClock = field(default_factory=OffsetClock)
    built: list[BuiltRuntime] = field(default_factory=list)
    on_build: Callable[[BuiltRuntime], Awaitable[None]] | None = None

    def aws_settings(self) -> AwsSettings:
        return AwsSettings(
            region=self.region,
            endpoint_url=self.localstack_url,
            credentials=AwsCredentials("test", "test"),
        )

    def environ(self, provider_id: uuid.UUID | None = None, **extra: str) -> dict[str, str]:
        values = {
            "VIGIA_ENVIRONMENT": "test",
            "VIGIA_PUBLIC_ORIGIN": LINK_BASE,
            "VIGIA_NODE_CA_KEY_ARN": self.node_ca_key_id,
            "VIGIA_EDGE_BUCKET": self.edge_bucket,
            "VIGIA_ARCHIVE_BUCKET": self.archive_bucket,
            "VIGIA_BOOTSTRAP_INVITATION_SECRET": f"vigia/{self.environment}/bootstrap/invitation",
        }
        if provider_id is not None:
            values["VIGIA_PROVIDER_ORGANIZATION_ID"] = str(provider_id)
        return values | extra

    async def builder(self, config: AdminConfig, provider_id: uuid.UUID) -> AdminRuntime:
        clock = self.clock
        database = self.database_factory()
        contexts = ScopeContexts(
            store=PostgresContextStore(database),
            clock=clock,
            provider_organization_id=provider_id,
            system_actor_id=SYSTEM_ACTOR_ID,
        )
        audit = AuditWriter(database=database, clock=clock, provider_organization_id=provider_id)
        catalog = OutboxCatalog()
        register_u02_event_types(catalog.event_types)
        outbox = Outbox(catalog, clock)
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)
        free_text = FreeTextPolicyRegistry()
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(NoEvidenceStorage(), clock),
            outbox=outbox,
            clock=clock,
        )
        authorizer = Authorizer(
            audit=PostgresAuthorizationAudit(
                database=database, audit=audit, outbox=outbox, clock=clock
            ),
            provider_organization_id=provider_id,
        )
        deps = IdentityDependencies(
            database=database,
            writer=writer,
            audit=audit,
            outbox=outbox,
            authorizer=authorizer,
            free_text=free_text,
            clock=clock,
            provider_organization_id=provider_id,
        )
        aws = self.aws_settings()
        secrets_adapter = SecretsManagerAdapter(aws, clock, kms_key_id=self.secrets_key_id)
        kms = KmsAdapter(aws)
        signing = SigningService(
            provider_organization_id=provider_id,
            store=SqlSigningKeyStore(
                database=database,
                context=contexts.provider_audit_context,
                recorder=LedgerRotationRecorder(database=database, writer=writer, audit=audit),
            ),
            secrets=secrets_adapter,
            events=LedgerKeyEventWriter(writer),
            clock=clock,
            environment=self.environment,
        )

        async def synchronize() -> None:
            system = contexts.provider_audit_context()
            await _save_record_types(database, system, registry)
            registry.seal()
            async with database.transaction(system) as transaction:
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)

        runtime = AdminRuntime(
            clock=clock,
            database=database,
            contexts=contexts,
            authorizer=authorizer,
            genesis=OrganizationGenesis(deps, senders=EmailSenderRegistry(), link_base=LINK_BASE),
            signing=signing,
            replay=DeadLetterReplay(
                database=database, authorizer=authorizer, audit=audit, clock=clock
            ),
            partitions=PartitionMaintenance(clock=clock),
            drills=RestoreDrills(database=database, audit=audit, clock=clock),
            secrets=secrets_adapter,
            audit=audit,
            node_ca=NodeCaPublisher(
                storage=S3Storage(self._storage_settings(self.edge_bucket), clock),
                kms=kms,
                environment=config.environment,
                random_bytes=os.urandom,
                object_key=config.root_certificate_key,
            ),
            archive=S3Storage(self._storage_settings(self.archive_bucket), clock),
            registries=(synchronize,),
        )
        built = BuiltRuntime(runtime, deps, contexts, signing, database)
        self.built.append(built)
        if self.on_build is not None:
            await self.on_build(built)
        return runtime

    def _storage_settings(self, bucket: str) -> Any:
        from tests.integration.conftest import LocalStackEndpoint

        return LocalStackEndpoint(self.localstack_url, self.region).storage_settings(bucket)

    def run(
        self,
        *argv: str,
        environ: Mapping[str, str],
        stdin: str = "",
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = run(
            list(argv),
            environ=environ,
            builder=self.builder,
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()
