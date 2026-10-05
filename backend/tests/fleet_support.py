"""Dobles en memoria de los servicios de identidad del nodo de ``fleet`` (TASK-218).

Sirven a las propiedades sin base (PR-GOB-15): el servicio real (``EnrollmentCodeService``) sobre
almacenes en memoria que guardan las mismas filas que ``gob_0018`` y comprueban, en cada
escritura, lo que la base garantiza con restricciones (a lo sumo un ``active`` por nodo, estados
solo hacia adelante). Los registros del expediente y la auditoría quedan en listas para buscar en
ellas el código en claro.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from tests.authz_support import sealed_context
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.fleet.adapters.postgres.enrollment_store import (
    MAX_CODES_COMPARED,
    AttemptCursor,
    PostgresEnrollmentStore,
)
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.adapters.postgres.revocation_mark_store import (
    PostgresRevocationMarkStore,
)
from vigia_platform.fleet.application.common import FleetDependencies
from vigia_platform.fleet.domain.enrollment_attempt import EnrollmentAttempt
from vigia_platform.fleet.domain.enrollment_code import EnrollmentCode
from vigia_platform.fleet.domain.enums import EnrollmentCodeStatus
from vigia_platform.fleet.domain.node_fleet_record import (
    CredentialState,
    FleetNode,
    NodeFleetRecord,
)
from vigia_platform.identity.authz.context import EnrollmentScope
from vigia_platform.ledger.application.writer import AcceptanceStatus, Receipt
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.observability.metrics import PlatformMetrics

__all__ = ["FakeFleet", "FakeTransaction", "RootObjects", "root_bundle"]


def root_bundle(roots: int) -> bytes:
    """Un ``ca/root.pem`` con ``roots`` raíces autofirmadas de prueba (``test-only``)."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    body = b""
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    for index in range(roots):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, f"Vigia Node CA test-only {index}")]
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(start + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256())
        )
        body += certificate.public_bytes(serialization.Encoding.PEM)
    return body


@dataclasses.dataclass
class RootObjects:
    """El depósito ``vigia-edge`` en memoria: solo ``ca/root.pem`` (``None``: no se lee)."""

    body: bytes | None

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        if self.body is None or key != "ca/root.pem":
            raise OSError("el depósito no responde")
        return self.body


_FORWARD = {
    EnrollmentCodeStatus.ACTIVE: {
        EnrollmentCodeStatus.USED,
        EnrollmentCodeStatus.EXPIRED,
        EnrollmentCodeStatus.SUPERSEDED,
    },
}


@dataclasses.dataclass
class FakeTransaction:
    context: ScopeContext


class FakeDatabase:
    @contextlib.asynccontextmanager
    async def transaction(self, context: ScopeContext) -> AsyncIterator[FakeTransaction]:
        yield FakeTransaction(context)

    async def read(self, *_: Any, **__: Any) -> Any:  # pragma: no cover - no se usa
        raise AssertionError("las lecturas van por los almacenes en memoria")


class FakeWriter:
    def __init__(self, clock: SimulatedClock) -> None:
        self.clock = clock
        self.records: list[tuple[str, dict[str, Any], tuple[Any, ...]]] = []

    async def write(
        self, context: ScopeContext, record_type: str, content: Mapping[str, Any], **kwargs: Any
    ) -> Receipt:
        self.records.append((record_type, dict(content), tuple(kwargs.get("events", ()))))
        return Receipt(uuid.uuid4(), self.clock.now(), AcceptanceStatus.ACCEPTED)


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    async def append(self, context: ScopeContext, operation: Any, **fields: Any) -> None:
        self.entries.append(
            (str(operation), {k: v for k, v in fields.items() if k != "transaction"})
        )


class FakeAuthorizer:
    async def authorize(self, context: ScopeContext, key: Any, resource: Any) -> ScopeContext:
        return context


class FakeIdentity:
    def __init__(self, nodes: dict[uuid.UUID, FleetNode]) -> None:
        self.nodes = nodes

    async def update_node(
        self, context: ScopeContext, node_id: uuid.UUID, status: str, url: str | None, **_: Any
    ) -> None:
        self.nodes[node_id] = dataclasses.replace(self.nodes[node_id], status=status)


class FakeNodes(PostgresNodeFleetStore):
    def __init__(self, nodes: dict[uuid.UUID, FleetNode]) -> None:
        self.nodes = nodes

    async def node(self, context: ScopeContext, node_id: uuid.UUID) -> FleetNode | None:
        return self.nodes.get(node_id)

    async def read(self, transaction: Any, node_id: uuid.UUID) -> FleetNode | None:
        return self.nodes.get(node_id)

    async def lock(self, transaction: Any, node_id: uuid.UUID) -> FleetNode | None:
        return self.nodes.get(node_id)

    async def credentials(
        self, transaction: Any, node_id: uuid.UUID
    ) -> tuple[CredentialState, ...]:
        return ()

    async def revoke_credentials(
        self, transaction: Any, node_id: uuid.UUID, revoked_at: datetime
    ) -> tuple[uuid.UUID, ...]:
        return ()

    async def clear_revocation(self, transaction: Any, node_id: uuid.UUID) -> None:
        return None


class FakeEnrollment(PostgresEnrollmentStore):
    """Las filas de ``enrollment_code`` y ``enrollment_attempt`` con sus restricciones."""

    def __init__(self) -> None:
        self.codes_by_id: dict[uuid.UUID, EnrollmentCode] = {}
        self.attempts_list: list[EnrollmentAttempt] = []
        self.uses: dict[uuid.UUID, int] = {}

    def _set(self, code: EnrollmentCode, status: EnrollmentCodeStatus) -> None:
        if status is not code.status and status not in _FORWARD.get(code.status, set()):
            raise AssertionError(f"{code.status} no pasa a {status}")
        self.codes_by_id[code.code_id] = dataclasses.replace(code, status=status)

    def active_of(self, node_id: uuid.UUID) -> list[EnrollmentCode]:
        return [
            code
            for code in self.codes_by_id.values()
            if code.node_id == node_id and code.status is EnrollmentCodeStatus.ACTIVE
        ]

    async def codes(
        self, transaction: Any, node_id: uuid.UUID, *, limit: int = MAX_CODES_COMPARED
    ) -> tuple[EnrollmentCode, ...]:
        mine = [code for code in self.codes_by_id.values() if code.node_id == node_id]
        mine.sort(key=lambda code: (code.issued_at, code.code_id), reverse=True)
        return tuple(mine[:limit])

    async def supersede_active(self, transaction: Any, node_id: uuid.UUID) -> tuple[uuid.UUID, ...]:
        changed = self.active_of(node_id)
        for code in changed:
            self._set(code, EnrollmentCodeStatus.SUPERSEDED)
        return tuple(code.code_id for code in changed)

    async def insert_code(self, transaction: Any, code: EnrollmentCode) -> None:
        if code.status is EnrollmentCodeStatus.ACTIVE and self.active_of(code.node_id):
            raise AssertionError("enrollment_code_one_active_per_node: dos active")
        self.codes_by_id[code.code_id] = code

    async def consume(self, transaction: Any, code_id: uuid.UUID, now: datetime) -> bool:
        code = self.codes_by_id.get(code_id)
        if (
            code is None
            or code.status is not EnrollmentCodeStatus.ACTIVE
            or not now < code.expires_at
        ):
            return False
        self._set(code, EnrollmentCodeStatus.USED)
        self.uses[code_id] = self.uses.get(code_id, 0) + 1
        return True

    async def insert_attempt(self, transaction: Any, attempt: EnrollmentAttempt) -> None:
        self.attempts_list.append(attempt)

    async def attempts(
        self,
        transaction: Any,
        *,
        plant_id: uuid.UUID,
        node_id: uuid.UUID,
        after: AttemptCursor | None,
        limit: int,
    ) -> Sequence[EnrollmentAttempt]:  # pragma: no cover - no se usa
        return ()


class FakeMarks(PostgresRevocationMarkStore):
    def __init__(self) -> None:
        self.generation = 0

    async def mark_dirty(self, transaction: Any, marked_at: datetime) -> int:
        self.generation += 1
        return self.generation


@dataclasses.dataclass
class FakeFleet:
    """Los dobles con un servicio de dependencias y ``nodes`` nodos ``declared``."""

    clock: SimulatedClock
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    nodes: dict[uuid.UUID, FleetNode]
    writer: FakeWriter
    audit: FakeAudit
    enrollment: FakeEnrollment
    marks: FakeMarks
    deps: FleetDependencies
    person: ScopeContext

    @classmethod
    def build(cls, start: datetime, nodes: int, metrics: PlatformMetrics) -> FakeFleet:
        clock = SimulatedClock(start)
        organization_id, plant_id = uuid.uuid4(), uuid.uuid4()
        table: dict[uuid.UUID, FleetNode] = {}
        for index in range(nodes):
            node_id = uuid.uuid4()
            table[node_id] = FleetNode(
                node_id=node_id,
                organization_id=organization_id,
                plant_id=plant_id,
                code=f"ND-{index}",
                status="declared",
                live_view_local_url=None,
                record=NodeFleetRecord(
                    node_id=node_id,
                    organization_id=organization_id,
                    plant_id=plant_id,
                    replaces_node_id=None,
                    hardware_fingerprint=None,
                    declared_at=start,
                    declared_by=uuid.uuid4(),
                ),
            )
        free_text = FreeTextPolicyRegistry()
        register_u03_free_text_validator(free_text)
        free_text.seal()
        writer, audit, enrollment, marks = (
            FakeWriter(clock),
            FakeAudit(),
            FakeEnrollment(),
            FakeMarks(),
        )
        deps = FleetDependencies(
            database=FakeDatabase(),
            writer=writer,  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            authorizer=FakeAuthorizer(),  # type: ignore[arg-type]
            free_text=free_text,
            clock=clock,
            identity=FakeIdentity(table),  # type: ignore[arg-type]
            nodes=FakeNodes(table),
            enrollment=enrollment,
            marks=marks,
            metrics=metrics,
        )
        return cls(
            clock,
            organization_id,
            plant_id,
            table,
            writer,
            audit,
            enrollment,
            marks,
            deps,
            sealed_context(organization_id, ()),
        )

    def enrollment_scope(self, node_id: uuid.UUID) -> EnrollmentScope:
        return EnrollmentScope(
            context=sealed_context(self.organization_id, ()),
            node_id=node_id,
            plant_id=self.plant_id,
            node_status=self.nodes[node_id].status,
        )
