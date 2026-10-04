"""Entorno de prueba de ``node_api`` (TASK-206): certificados sintéticos, cabeceras del balanceador,
la ruta de prueba interna y la aplicación real con su cadena.

- ``TestAuthority``: una autoridad ECDSA P-256 **de prueba** (nunca la de KMS) que emite hojas con
  el perfil de ``certificate_profile`` y cabeceras ``X-Amzn-Mtls-Clientcert-*`` como las que
  entrega el balanceador de nodos (``alb_headers``).
- ``MemoryNodeStore``: un ``NodeContextStore`` en memoria (filas de ``context_from_node``) que
  cuenta sus consultas; las pruebas con base usan ``PostgresNodeContextStore``.
- **Ruta de prueba interna** (``probe_operations``): un manejador para cada una de las diez
  ``NodeRoute`` que solo devuelve lo que la verificación previa le entregó (ruta, nodo, zonas,
  tamaño del cuerpo); se registra **solo** aquí, nunca en ``platform_units``. ``failing`` hace que
  el manejador lance la excepción que la prueba elija.
- ``node_app``: ``create_app`` con la cadena fija, la unidad de prueba y una ``NodeApiGate`` real.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from vigia_contracts.versioning import CONTRACT_VERSION

from tests.api_support import World
from vigia_platform.identity.authz.context import (
    EnrollmentRow,
    NodeAssignment,
    NodeRow,
    ScopeContexts,
)
from vigia_platform.node_api.certificate_profile import NodeSubject, subject_name
from vigia_platform.node_api.identity import NodeIdentity
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import (
    NodeApiGate,
    NodeOperation,
    NodeReply,
    NodeRequest,
    node_router,
)
from vigia_platform.node_api.versioning import VersionPolicy
from vigia_platform.shared.api.app import UnitRegistration
from vigia_platform.shared.api.declarations import NODE_GATE_STATE_KEY, NodeRoute
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.ratelimit import Allowed, Budget, Limited, RateLimiter

__all__ = [
    "DAY",
    "PROVIDER",
    "SYSTEM_ACTOR",
    "VERSION",
    "MemoryNodeStore",
    "NodeFixture",
    "NodeWorld",
    "Probe",
    "TestAuthority",
    "UnlimitedNodeLimiter",
    "alb_headers",
    "gzip_bomb",
    "node_app",
    "node_gate",
    "node_world",
    "probe_operations",
    "scope_contexts",
    "zone_of",
]

PROVIDER = uuid.UUID("0192f0c4-0000-7000-8000-000000000001")
SYSTEM_ACTOR = uuid.UUID("0192f0c4-0000-7000-8000-0000000000ff")
VERSION = str(CONTRACT_VERSION)
"""La versión del contrato que presenta un nodo al día con la plataforma."""


# --- Autoridad de prueba y cabeceras del balanceador ---------------------------------------------


@dataclass
class TestAuthority:
    """Autoridad ECDSA P-256 de prueba (marca ``test-only`` en su nombre)."""

    __test__ = False  # no es una clase de pruebas de pytest

    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    name: x509.Name = field(
        default_factory=lambda: x509.Name(
            [
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Vigia"),
                x509.NameAttribute(NameOID.COMMON_NAME, "Vigia Node CA test-only"),
            ]
        )
    )

    def leaf(
        self,
        subject: NodeSubject,
        *,
        serial: int | None = None,
        not_before: dt.datetime,
        not_after: dt.datetime,
        name: x509.Name | None = None,
    ) -> x509.Certificate:
        """Una hoja de cliente con el perfil de nodo (o con ``name`` si la prueba lo cambia)."""
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(name if name is not None else subject_name(subject))
            .issuer_name(self.name)
            .public_key(key.public_key())
            .serial_number(serial if serial is not None else x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        )
        return builder.sign(self.key, hashes.SHA256())


def alb_headers(certificate: x509.Certificate) -> dict[str, str]:
    """Las cabeceras ``X-Amzn-Mtls-Clientcert-*`` que el balanceador entrega con ``certificate``."""
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    return {
        "X-Amzn-Mtls-Clientcert-Leaf": quote(pem, safe=""),
        "X-Amzn-Mtls-Clientcert-Serial-Number": format(certificate.serial_number, "X"),
        "X-Amzn-Mtls-Clientcert-Subject": certificate.subject.rfc4514_string(),
        "X-Amzn-Mtls-Clientcert-Issuer": certificate.issuer.rfc4514_string(),
        "X-Amzn-Mtls-Clientcert-Validity": (
            f"NotBefore={certificate.not_valid_before_utc.strftime(stamp)};"
            f"NotAfter={certificate.not_valid_after_utc.strftime(stamp)}"
        ),
    }


def gzip_bomb(limit: int) -> bytes:
    """Un ``gzip`` pequeño que se infla por encima de ``limit`` bytes."""
    return gzip.compress(b"{" + b" " * (limit * 4) + b"}", compresslevel=9)


# --- Almacén en memoria -------------------------------------------------------------------------


@dataclass
class NodeFixture:
    """Un nodo con sus credenciales y asignaciones, como las filas de ``context_from_node``."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    code: str = "ND-PRUEBA"
    node_status: str = "enrolled"
    enrolled_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    decommissioned_at: dt.datetime | None = None
    credentials: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Número de serie → ``status``, ``issued_at``, ``expires_at``, ``successor_issued_at``."""
    assignments: list[NodeAssignment] = field(default_factory=list)


@dataclass
class MemoryNodeStore:
    """``NodeContextStore`` en memoria: lo que vería la sentencia única bajo la organización."""

    nodes: dict[uuid.UUID, NodeFixture] = field(default_factory=dict)
    lookups: list[tuple[uuid.UUID, uuid.UUID, str]] = field(default_factory=list)

    async def node_row(
        self, lookup: ScopeContext, node_id: uuid.UUID, certificate_serial: str
    ) -> NodeRow | None:
        self.lookups.append((lookup.organization_id, node_id, certificate_serial))
        node = self.nodes.get(node_id)
        # Seguridad a nivel de fila: otra organización no ve la fila.
        if node is None or node.organization_id != lookup.organization_id:
            return None
        credential = node.credentials.get(certificate_serial)
        if credential is None:
            return None
        return NodeRow(
            node_id=node.node_id,
            organization_id=node.organization_id,
            plant_id=node.plant_id,
            code=node.code,
            node_status=node.node_status,
            enrolled_at=node.enrolled_at,
            revoked_at=node.revoked_at,
            decommissioned_at=node.decommissioned_at,
            credential_organization_id=node.organization_id,
            credential_plant_id=node.plant_id,
            credential_status=credential["status"],
            issued_at=credential["issued_at"],
            expires_at=credential["expires_at"],
            successor_issued_at=credential.get("successor_issued_at"),
            assignments=tuple(node.assignments),
        )

    async def enrollment_row(
        self, lookup: ScopeContext, node_id: uuid.UUID
    ) -> EnrollmentRow | None:
        node = self.nodes.get(node_id)
        if node is None or lookup.actor.kind.value != "system":
            return None
        return EnrollmentRow(
            node_id=node.node_id,
            organization_id=node.organization_id,
            plant_id=node.plant_id,
            code=node.code,
            node_status=node.node_status,
        )


class UnlimitedNodeLimiter(RateLimiter):
    """Limitador que siempre deja pasar (pruebas cuyo asunto no es la tasa)."""

    def check(self, key: str, budget: Budget) -> Allowed | Limited:
        return Allowed()


# --- Ruta de prueba interna ---------------------------------------------------------------------


@dataclass
class Probe:
    """Lo que vieron los manejadores de prueba y la excepción que deben lanzar."""

    seen: list[NodeRequest] = field(default_factory=list)
    failing: BaseException | None = None


def probe_operations(probe: Probe) -> dict[NodeRoute, NodeOperation]:
    """Un manejador por ``NodeRoute``: devuelve lo que recibió de la verificación previa."""

    async def handle(request: NodeRequest) -> NodeReply:
        probe.seen.append(request)
        if probe.failing is not None:
            raise probe.failing
        node = request.node
        document = {
            "route": request.route.name,
            "node_id": None if node is None else str(node.node_id),
            "organization_id": None if node is None else str(node.organization_id),
            "zones": [] if node is None else sorted(str(zone) for zone in node.zone_ids),
            "body_bytes": len(request.body),
            "compatibility_result": request.compatibility_result.value,
        }
        return NodeReply(content=json.dumps(document, sort_keys=True).encode())

    return {route: NodeOperation(handle=handle) for route in NodeRoute}


def node_unit(routes: tuple[NodeRoute, ...] = tuple(NodeRoute)) -> UnitRegistration:
    """La unidad de prueba: el enrutador del esqueleto con ``routes`` (solo en las pruebas)."""
    return UnitRegistration("node_api_prueba", routers=(node_router(routes),))


def node_gate(
    *,
    contexts: ScopeContexts,
    store: Any,
    clock: Clock,
    probe: Probe,
    limiter: RateLimiter | None = None,
    limits: NodeRateLimits | None = None,
    policy: VersionPolicy | None = None,
    responses: NodeResponses | None = None,
    operations: Mapping[NodeRoute, NodeOperation] | None = None,
) -> NodeApiGate:
    if limits is None:
        limits = NodeRateLimits(limiter if limiter is not None else UnlimitedNodeLimiter(clock))
    return NodeApiGate(
        identity=NodeIdentity(contexts=contexts, store=store),
        limits=limits,
        clock=clock,
        responses=responses if responses is not None else NodeResponses(clock),
        policy=policy,
        operations=operations if operations is not None else probe_operations(probe),
    )


def node_app(
    world: World,
    gate: NodeApiGate,
    *,
    routes: tuple[NodeRoute, ...] = tuple(NodeRoute),
    runtime: Mapping[str, Any] | None = None,
    extra_units: tuple[UnitRegistration, ...] = (),
) -> Any:
    """``create_app`` con la cadena real, la ruta de prueba interna y ``gate`` instalada."""
    values: dict[str, Any] = {"state": {NODE_GATE_STATE_KEY: gate}}
    values.update(runtime or {})
    return world.app(units=(node_unit(routes), *extra_units), runtime=values)


class _NoSessions:
    """``ContextStore`` sin sesiones: las rutas del contrato no usan el de personas."""

    async def session_row(self, *_: Any) -> None:  # pragma: no cover - no se usa
        return None

    async def operator_row(self, *_: Any) -> None:  # pragma: no cover - no se usa
        return None


def scope_contexts(clock: Clock) -> ScopeContexts:
    """``ScopeContexts`` real (el quinto constructor, ``context_from_node``)."""
    return ScopeContexts(
        store=_NoSessions(),  # type: ignore[arg-type]
        clock=clock,
        provider_organization_id=PROVIDER,
        system_actor_id=SYSTEM_ACTOR,
    )


DAY = dt.timedelta(days=1)


@dataclass
class NodeWorld:
    """La aplicación real con la ruta de prueba interna, un almacén en memoria y dos clientes.

    ``a`` es un nodo dado de alta de la organización A (planta y zona propias, credencial
    ``active``); ``b`` el de la organización B. ``headers(node)`` da las cabeceras del balanceador
    de su credencial vigente y la versión del contrato de la plataforma.
    """

    world: World
    store: MemoryNodeStore
    authority: TestAuthority
    probe: Probe
    gate: NodeApiGate
    app: Any
    a: NodeFixture
    b: NodeFixture
    certificates: dict[uuid.UUID, x509.Certificate] = field(default_factory=dict)

    @property
    def now(self) -> dt.datetime:
        return self.world.clock.now()

    def credential(
        self, node: NodeFixture, *, status: str = "active", **changes: Any
    ) -> x509.Certificate:
        """Emite una hoja nueva de ``node`` y su fila ``node_credential`` (``status``)."""
        now = self.now
        certificate = self.authority.leaf(
            NodeSubject(node.node_id, node.organization_id, node.plant_id),
            not_before=now - DAY,
            not_after=now + 364 * DAY,
        )
        row: dict[str, Any] = {
            "status": status,
            "issued_at": now - DAY,
            "expires_at": now + 364 * DAY,
        }
        row.update(changes)
        node.credentials[format(certificate.serial_number, "x")] = row
        self.certificates[node.node_id] = certificate
        return certificate

    def headers(
        self, node: NodeFixture | None = None, *, version: str | None = VERSION
    ) -> dict[str, str]:
        values: dict[str, str] = {}
        if node is not None:
            values.update(alb_headers(self.certificates[node.node_id]))
        if version is not None:
            values["X-Vigia-Contract-Version"] = version
        return values


def _fixture(organization: uuid.UUID, clock: Clock) -> NodeFixture:
    plant, zone = uuid.uuid4(), uuid.uuid4()
    node = NodeFixture(uuid.uuid4(), organization, plant, enrolled_at=clock.now() - 30 * DAY)
    node.assignments.append(NodeAssignment(zone, clock.now() - 30 * DAY, None))
    return node


def node_world(
    *,
    limiter: RateLimiter | None = None,
    policy: VersionPolicy | None = None,
    routes: tuple[NodeRoute, ...] = tuple(NodeRoute),
    runtime: Mapping[str, Any] | None = None,
) -> NodeWorld:
    """``NodeWorld`` con dos organizaciones y un nodo dado de alta en cada una."""
    world = World()
    clock = world.clock
    store = MemoryNodeStore()
    probe = Probe()
    a, b = _fixture(uuid.uuid4(), clock), _fixture(uuid.uuid4(), clock)
    store.nodes.update({a.node_id: a, b.node_id: b})
    gate = node_gate(
        contexts=scope_contexts(clock),
        store=store,
        clock=clock,
        probe=probe,
        limiter=limiter,
        policy=policy,
        responses=NodeResponses(clock),
    )
    app = node_app(world, gate, routes=routes, runtime=runtime)
    result = NodeWorld(world, store, TestAuthority(), probe, gate, app, a, b)
    result.credential(a)
    result.credential(b)
    return result


def zone_of(node: NodeFixture) -> uuid.UUID:
    """La zona asignada al nodo desde el principio."""
    return node.assignments[0].zone_id
