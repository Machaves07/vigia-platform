"""PR-GOB-12 sobre las diez rutas del contrato (TASK-228; PAT-GOB-SEG-05; NFR-GOB-30; A-51).

La aplicación real de las rutas del contrato (``create_app`` con la cadena fija y la
``NodeApiGate`` de producción con ``PostgresNodeContextStore``) y los manejadores de negocio de las
diez rutas publicadas, compuestos como los compone la raíz (``shared.runtime.units``
``_node_operations``; el alta y la rotación, con la autoridad ``vigia-node-ca`` de prueba de
``tests/fleet_enrollment_support.py``), contra PostgreSQL 16 real como ``vigia_app``.

El mundo es el de ``gob_world``: A y B con dos plantas, dos zonas por planta y un nodo
``enrolled`` por zona, con su certificado de la autoridad efímera (NFR-GOB-63), el catálogo
publicado y las compuertas aprobadas.

**Catálogo de casos** (``NODE_CASES``): una entrada por ``NodeRoute`` (la lista cerrada de A-51:
las diez obligatorias, sin ``conformance-profile``), con las variantes que nombran recursos y el
código que fija A-37 para cada ruta:

- con el certificado del nodo de A y la organización, la planta, el nodo, la zona o el clip de B:
  la respuesta es **idéntica** (estado y cuerpo, salvo ``correlation_id``) a la de un
  identificador inexistente, con ``node_zone_mismatch`` (la rotación, ``schema_invalid`` en
  ``node_id``); nunca datos de B y **nada** escrito en B (huella de cada tabla con
  ``organization_id``);
- el alta (sin certificado) con el ``node_id`` de un nodo de B (declarado o dado de alta) y el
  código de un nodo de A: el rechazo genérico ``enrollment_code_invalid``, idéntico al de un
  ``node_id`` inexistente (A-51, nota U03-H-13);
- **guarda de planta**: el certificado de un nodo de la planta 1 de A con la planta o una zona de
  la planta 2 de A, ``node_zone_mismatch``;
- **guarda de zona**: una zona de la misma planta asignada a otro nodo, o la propia en un
  ``node_time.started_at`` anterior a su asignación, ``node_zone_mismatch``; la propia en un
  instante en que sí estaba asignada pasa la guarda (y la rechaza después el esquema).

Al final, ninguna fila de A contiene un identificador de B ni al revés.

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, cast
from unittest.mock import Mock

import httpx
import pytest
from cryptography import x509

from tests.api_support import World
from tests.factories import uuid7
from tests.fleet_credentials_support import csr_pem, local_ip
from tests.fleet_enrollment_support import EnrollmentWorld, enrollment_world
from tests.fleet_http_support import FleetStack
from tests.integration.conftest import PostgresEndpoint
from tests.isolation.gob_world import (
    GobNode,
    GobOrganization,
    changed_tables,
    fingerprint,
    organization_tables,
    publish_zone,
    two_organizations,
)
from tests.node_api_support import (
    VERSION,
    TestAuthority,
    UnlimitedNodeLimiter,
    alb_headers,
    node_app,
)
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import NodeApiGate
from vigia_platform.node_api.routes.credential_rotations import credential_rotation_operation
from vigia_platform.node_api.routes.enrollment import enrollment_operation
from vigia_platform.node_api.versioning import VersionPolicy
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.runtime.units import (
    PUBLISHED_NODE_ROUTES,
    UnitServices,
    _node_operations,
)
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import ObjectHead, PresignedRequest

DAY: Final = timedelta(days=1)
MISMATCH: Final = "node_zone_mismatch"
SCOPE_FIELDS: Final = ("organization_id", "plant_id", "node_id")
ATTEMPT_TRAIL: Final = frozenset(
    {
        "fleet.enrollment_attempt",
        "ledger.chain_head",
        "ledger.ledger_record",
        "ledger.record_identity",
        "ledger.record_source_key",
    }
)
"""Lo que el alta con el ``node_id`` de un nodo de B escribe en B: el intento y su
``enrollment_attempt_rejected`` en la cadena de la planta del nodo (BR-GOB-62)."""


# --- Peticiones y casos ------------------------------------------------------------------------


@dataclass(frozen=True)
class Ids:
    """Los recursos que nombra una petición del nodo: los de B (conocidos) o inexistentes."""

    organization: uuid.UUID
    plant: uuid.UUID
    node: uuid.UUID
    zone: uuid.UUID
    clip: uuid.UUID

    @classmethod
    def missing(cls) -> Ids:
        return cls(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid7())

    def values(self) -> tuple[uuid.UUID, ...]:
        return (self.organization, self.plant, self.node, self.zone, self.clip)


@dataclass(frozen=True)
class NodeCall:
    method: str
    path: str
    body: Any = None
    certificate: bool = True
    """``False`` en el alta, la única ruta sin certificado."""


Builder = Callable[["NodeWorld", GobNode, Ids], NodeCall]


@dataclass(frozen=True)
class NodeCase:
    code: str
    """El ``rejection_code`` de A-37 con un recurso ajeno o inexistente."""
    variants: Mapping[str, Builder]
    """Cada forma de nombrar un recurso ajeno en la petición (campo del cuerpo, ruta, CSR)."""


def _scope(node: GobNode, ids: Ids, replaced: str) -> dict[str, str]:
    """La organización, la planta, el nodo (y la zona) del certificado, salvo ``replaced``."""
    own = {
        "organization_id": node.organization_id,
        "plant_id": node.plant_id,
        "node_id": node.node_id,
        "zone_id": node.zone_id,
    }
    foreign = {
        "organization_id": ids.organization,
        "plant_id": ids.plant,
        "node_id": ids.node,
        "zone_id": ids.zone,
    }
    return {key: str(foreign[key] if key == replaced else value) for key, value in own.items()}


def _fact(started_at: datetime) -> dict[str, Any]:
    return {
        "started_at": format_timestamp(started_at),
        "ended_at": format_timestamp(started_at + timedelta(seconds=5)),
        "clock": {"synchronized": True, "offset_ms": 4, "source": "ntp_local"},
    }


def _ingest(path: str) -> dict[str, Builder]:
    """Las variantes de una ruta de la ingesta: cada campo de alcance, el de B."""

    def build(name: str) -> Builder:
        def call(world: NodeWorld, node: GobNode, ids: Ids) -> NodeCall:
            body: dict[str, Any] = _scope(node, ids, name)
            body["node_time"] = _fact(world.now() - timedelta(minutes=5))
            return NodeCall("POST", path, body)

        return call

    return {name: build(name) for name in (*SCOPE_FIELDS, "zone_id")}


def _heartbeat_like(path: str) -> dict[str, Builder]:
    def build(name: str) -> Builder:
        def call(world: NodeWorld, node: GobNode, ids: Ids) -> NodeCall:
            body = _scope(node, ids, name)
            del body["zone_id"]
            return NodeCall("POST", path, body)

        return call

    return {name: build(name) for name in SCOPE_FIELDS}


def _clip_upload(world: NodeWorld, node: GobNode, ids: Ids) -> NodeCall:
    return NodeCall(
        "POST",
        NodeRoute.CLIP_UPLOAD.path,
        {
            "clip_id": str(uuid7()),
            "camera_id": str(uuid.uuid4()),
            "zone_id": str(ids.zone),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": "ab" * 32,
            "size_bytes": 1024,
            "duration_ms": 10_000,
            "purpose": "verification",
        },
    )


SERVER_NAMES: Final[dict[str, tuple[x509.GeneralName, ...]]] = {
    "un SAN local": (local_ip(),),
    "sin SAN": (),
    "dos SAN locales": (local_ip(), local_ip("192.168.10.21")),
    "SAN público": (local_ip("8.8.8.8"),),
}
"""Las CSR de servidor del alta: la válida y las que la plataforma rechaza por su nombre
alternativo (``announced_host``). Sin el código, ninguna puede responder distinto según exista el
nodo (revisión de VIG-165)."""


def _enrollment_with(names: tuple[x509.GeneralName, ...]) -> Builder:
    def call(world: NodeWorld, node: GobNode, ids: Ids) -> NodeCall:
        # Sin certificado: el nombre común de las dos CSR es el node_id de B y el código, el de A.
        name = str(ids.node)
        return NodeCall(
            "POST",
            NodeRoute.ENROLLMENT.path,
            {
                "enrollment_code": world.a_code,
                "key_algorithm": "ecdsa_p256",
                "certificate_signing_request": csr_pem(name),
                "server_certificate_signing_request": csr_pem(name, names=list(names)),
                "software_version": "1.4.0",
                "contract_version": VERSION,
                "hardware_fingerprint": "ab" * 32,
                "requested_at": "2026-10-05T12:00:00.000Z",
            },
            certificate=False,
        )

    return call


_enrollment: Final = _enrollment_with(SERVER_NAMES["un SAN local"])


def _rotation(world: NodeWorld, node: GobNode, ids: Ids) -> NodeCall:
    return NodeCall(
        "POST",
        NodeRoute.CREDENTIAL_ROTATION.path,
        {
            "node_id": str(ids.node),
            "certificate_signing_request": csr_pem(str(ids.node)),
            "server_certificate_signing_request": csr_pem(str(ids.node), names=[local_ip()]),
            "requested_at": "2026-10-05T12:00:00.000Z",
        },
    )


NODE_CASES: Final[dict[NodeRoute, NodeCase]] = {
    NodeRoute.HEARTBEAT: NodeCase(MISMATCH, _heartbeat_like(NodeRoute.HEARTBEAT.path)),
    NodeRoute.ZONE_CATALOG: NodeCase(
        MISMATCH,
        {"zone_id": lambda w, n, i: NodeCall("GET", f"/api/nodes/zones/{i.zone}/catalog")},
    ),
    NodeRoute.CLIP_UPLOAD: NodeCase(MISMATCH, {"zone_id": _clip_upload}),
    NodeRoute.CLIP_CONFIRMATION: NodeCase(
        MISMATCH,
        {
            "clip_id": lambda w, n, i: NodeCall(
                "POST", f"/api/nodes/clip-uploads/{i.clip}/confirmation"
            )
        },
    ),
    NodeRoute.FINDING: NodeCase(MISMATCH, _ingest(NodeRoute.FINDING.path)),
    NodeRoute.DETECTION_REVIEW: NodeCase(MISMATCH, _ingest(NodeRoute.DETECTION_REVIEW.path)),
    NodeRoute.OBSERVABILITY_EVENT: NodeCase(MISMATCH, _ingest(NodeRoute.OBSERVABILITY_EVENT.path)),
    NodeRoute.ENROLLMENT: NodeCase(
        "enrollment_code_invalid",
        {f"node_id, {label}": _enrollment_with(names) for label, names in SERVER_NAMES.items()},
    ),
    NodeRoute.CREDENTIAL_ROTATION: NodeCase("schema_invalid", {"node_id": _rotation}),
    NodeRoute.UPDATE_RESULT: NodeCase(MISMATCH, _heartbeat_like(NodeRoute.UPDATE_RESULT.path)),
}
"""Un caso por ruta de la lista cerrada ``NodeRoute`` (A-51)."""


def test_every_contract_route_has_its_case() -> None:
    # Sin base: las diez de la lista cerrada, todas publicadas y todas con su caso.
    assert set(NODE_CASES) == set(NodeRoute) == set(PUBLISHED_NODE_ROUTES)
    assert len(NODE_CASES) == 10
    missing = sorted(route.name for route in set(NodeRoute) - set(NODE_CASES))
    assert not missing, f"rutas del contrato sin caso de aislamiento: {missing}"


# --- El mundo ------------------------------------------------------------------------------------


class _EvidenceStub:
    """``vigia-evidence`` sin objetos: ninguna petición de esta prueba llega a emitir un clip."""

    async def head_object(self, key: str) -> ObjectHead | None:
        return None

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = timedelta(minutes=10),
    ) -> PresignedRequest:  # pragma: no cover - no se emite ninguna concesión
        raise AssertionError("la prueba no emite concesiones de clip")


@dataclass
class NodeWorld:
    enrollment: EnrollmentWorld
    client: httpx.AsyncClient
    a: GobOrganization
    b: GobOrganization
    a_code: str
    b_declared: uuid.UUID
    """Un nodo ``declared`` de B con su código vigente (el alta llega a comprobar el código)."""
    b_clip: uuid.UUID
    """Una concesión de clip de verificación del primer nodo de B."""
    tables: list[str] = field(default_factory=list)

    @property
    def fleet(self) -> FleetStack:
        return self.enrollment.fleet

    def now(self) -> datetime:
        now: datetime = self.fleet.authz.now()
        return now

    def foreign(self) -> Ids:
        node = self.b.node(0, 0)
        return Ids(node.organization_id, node.plant_id, node.node_id, node.zone_id, self.b_clip)

    def send(self, call: NodeCall, node: GobNode) -> httpx.Response:
        headers = {"X-Vigia-Contract-Version": VERSION}
        if call.certificate:
            headers.update(alb_headers(node.certificate))
        if call.method == "GET":
            request = self.client.get(call.path, headers=headers)
        else:
            content = b"" if call.body is None else json.dumps(call.body).encode()
            if call.body is not None:
                headers["Content-Type"] = "application/json"
            request = self.client.post(call.path, content=content, headers=headers)
        response: httpx.Response = self.fleet.run(request)
        return response

    def fingerprint(self, organization_id: uuid.UUID) -> dict[str, str]:
        return fingerprint(self.fleet.fetch, organization_id, self.tables)


def _declared_with_code(world: EnrollmentWorld, organization: GobOrganization) -> tuple[Any, str]:
    """Un nodo ``declared`` de la organización y su código de alta vigente (por las rutas)."""
    fleet = world.fleet
    installer = fleet.installer(organization.site)
    response = fleet.declare(installer, organization.plants[0], [])
    assert response.status_code == 201, response.text
    node = uuid.UUID(response.json()["node_id"])
    issued = fleet.issue(installer, node)
    assert issued.status_code == 201, issued.text
    return node, str(issued.json()["code"])


def _verification_clip(fleet: FleetStack, node: GobNode) -> uuid.UUID:
    clip = uuid7()
    now = fleet.authz.now()
    fleet.execute(
        "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
        " node_id, purpose, storage_key, content_type, max_size_bytes, required_headers,"
        " issued_at, expires_at, status) VALUES ($1, $2, $3, $4, $5, 'verification', $6,"
        " 'video/mp4', 1000, $8::jsonb, $7::timestamptz, $7::timestamptz"
        " + interval '10 minutes', 'issued')",
        clip,
        node.organization_id,
        node.plant_id,
        node.zone_id,
        node.node_id,
        f"org/{node.organization_id}/plant/{node.plant_id}/zone/{node.zone_id}/node/"
        f"{node.node_id}/{clip}.mp4",
        now,
        json.dumps(
            {
                "x-amz-checksum-sha256": base64.b64encode(
                    hashlib.sha256(b"clip").digest()
                ).decode(),
                "x-amz-meta-vigia-anonymized": "true",
            }
        ),
    )
    return clip


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[NodeWorld]:
    with enrollment_world(postgres_endpoint, "node_route_isolation") as built:
        fleet = built.fleet
        authz = fleet.authz
        sessions = authz.sessions
        clock = sessions.clock
        now = authz.now()
        a, b = two_organizations(
            fleet, TestAuthority(), now, lambda zone: publish_zone(fleet, zone, now)
        )
        _, a_code = _declared_with_code(built, a)
        b_declared, _ = _declared_with_code(built, b)
        inert: Any = Mock()
        services = UnitServices(
            clock=clock,
            metrics=get_metrics(),
            provider_organization_id=authz.provider_organization_id,
            database=fleet.database,
            contexts=authz.contexts,
            authorizer=authz.authorizer,
            audit=sessions.audit,
            outbox=cast(Any, fleet.outbox),
            writer=fleet.writer,
            free_text=fleet.free_text,
            signing=built.signing.service,
            checkpoints=inert,
            kms=cast(Any, built.kms),
            evidence=cast(Any, _EvidenceStub()),
        )
        store = PostgresNodeContextStore(fleet.database)
        identity = NodeIdentity(contexts=authz.contexts, store=store)
        limits = NodeRateLimits(UnlimitedNodeLimiter(clock))
        policy = VersionPolicy()
        operations = {
            **_node_operations(services, policy, identity, limits),
            # El alta y la rotación con la autoridad de prueba (la raíz las compone con KMS y la
            # clave del hash de origen de la configuración, que esta prueba no tiene).
            NodeRoute.ENROLLMENT: enrollment_operation(built.enrollment, identity, limits),
            NodeRoute.CREDENTIAL_ROTATION: credential_rotation_operation(built.rotation),
        }
        assert set(operations) == set(PUBLISHED_NODE_ROUTES)
        gate = NodeApiGate(
            identity=identity,
            limits=limits,
            clock=clock,
            responses=NodeResponses(clock),
            policy=policy,
            operations=operations,
        )
        app = node_app(World(clock=clock), gate, routes=PUBLISHED_NODE_ROUTES)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://nodes.vigia.test",
            timeout=60.0,
        )
        world = NodeWorld(
            built,
            client,
            a,
            b,
            a_code,
            b_declared,
            _verification_clip(fleet, b.node(0, 0)),
            organization_tables(fleet.fetch),
        )
        try:
            yield world
        finally:
            fleet.run(client.aclose())


def replace_node(ids: Ids, node: uuid.UUID) -> Ids:
    return Ids(ids.organization, ids.plant, node, ids.zone, ids.clip)


def _comparable(response: httpx.Response) -> tuple[int, Any]:
    """Estado y cuerpo, sin ``correlation_id`` (lo único que cambia entre dos peticiones)."""
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if isinstance(body, dict):
        body = {key: value for key, value in body.items() if key != "correlation_id"}
    return response.status_code, body


def _code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def _mentions(response: httpx.Response, ids: Ids) -> list[str]:
    return [str(value) for value in ids.values() if str(value) in response.text]


# --- PR-GOB-12: el certificado de A contra los recursos de B -------------------------------------


@pytest.mark.integration
def test_pr_gob_12_a_foreign_resource_answers_exactly_like_a_missing_one(world: NodeWorld) -> None:
    node = world.a.node(0, 0)
    failures: list[str] = []
    for route, case in NODE_CASES.items():
        for variant, build in case.variants.items():
            label = f"{route.name} ({variant})"
            for foreign in (world.foreign(), replace_node(world.foreign(), world.b_declared)):
                if route is not NodeRoute.ENROLLMENT and foreign.node == world.b_declared:
                    continue  # el nodo declarado de B solo cambia algo en el alta
                before = world.fingerprint(foreign.organization)
                known = world.send(build(world, node, foreign), node)
                missing = world.send(build(world, node, Ids.missing()), node)
                changed = changed_tables(before, world.fingerprint(foreign.organization))
                if _comparable(known) != _comparable(missing):
                    failures.append(f"{label}: {_comparable(known)} != {_comparable(missing)}")
                if _code(known) != case.code:
                    failures.append(f"{label}: {known.status_code} {known.text}")
                if known.status_code == 403 and _code(known) != MISMATCH:
                    failures.append(f"{label}: forbidden revela que el recurso existe")
                if _mentions(known, foreign):
                    failures.append(f"{label}: devuelve identificadores de B")
                # El alta registra el intento contra el nodo de B en B (BR-GOB-62): es el rastro
                # de B sobre su propio nodo, no un dato que A ve.
                if changed and not (
                    route is NodeRoute.ENROLLMENT and set(changed) <= ATTEMPT_TRAIL
                ):
                    failures.append(f"{label}: cambió filas de B en {changed}")
    assert not failures, "\n".join(failures)


@pytest.mark.integration
def test_the_enrollment_never_reveals_whether_a_node_exists(world: NodeWorld) -> None:
    # Revisión de VIG-165: sin el código, ni la CSR de servidor (sin SAN, con dos o con una IP
    # pública) distingue un node_id inexistente de uno declarado o dado de alta de B, ni de un
    # nodo de A cuyo código no es el presentado.
    node = world.a.node(0, 0)
    foreign = world.foreign()
    candidates = {
        "declarado de B": replace_node(foreign, world.b_declared),
        "dado de alta de B": foreign,
        "dado de alta de A": replace_node(foreign, world.a.node(1, 0).node_id),
    }
    failures: list[str] = []
    for label, names in SERVER_NAMES.items():
        build = _enrollment_with(names)
        absent = _comparable(world.send(build(world, node, Ids.missing()), node))
        if absent[:1] != (401,) or absent[1].get("code") != "enrollment_code_invalid":
            failures.append(f"{label}, inexistente: {absent}")
        for name, target in candidates.items():
            seen = _comparable(world.send(build(world, node, target), node))
            if seen != absent:
                failures.append(f"{label}, {name}: {seen} != {absent}")
    assert not failures, "\n".join(failures)


@pytest.mark.integration
def test_the_enrollment_attempt_against_a_node_of_b_stays_in_b(world: NodeWorld) -> None:
    # El intento con el node_id de B queda en B (su rastro, BR-GOB-62), sin nada de A.
    node = world.a.node(0, 0)
    target = replace_node(world.foreign(), world.b_declared)
    a_before = world.fingerprint(world.a.organization_id)
    response = world.send(_enrollment(world, node, target), node)
    assert (response.status_code, _code(response)) == (401, "enrollment_code_invalid")
    assert changed_tables(a_before, world.fingerprint(world.a.organization_id)) == []
    attempts = world.fleet.fetch(
        "SELECT organization_id, result FROM fleet.enrollment_attempt WHERE node_id = $1",
        world.b_declared,
    )
    assert {(row["organization_id"], row["result"]) for row in attempts} == {
        (world.b.organization_id, "enrollment_code_invalid")
    }
    # Su registro, en la cadena de B y sin nada de A.
    records = world.fleet.fetch(
        "SELECT ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
        " WHERE organization_id = $1 AND record_type = 'enrollment_attempt_rejected'",
        world.b.organization_id,
    )
    assert records
    assert not any(str(world.a.organization_id) in row["content"] for row in records)


# --- Guardas de planta y de zona ------------------------------------------------------------------


def _rejection(response: httpx.Response) -> tuple[int, str | None, Any]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("field")


@pytest.mark.integration
def test_the_plant_guard_another_plant_of_the_same_organization(world: NodeWorld) -> None:
    a = world.a
    node = a.node(0, 0)
    other = a.zone(1, 0)
    elsewhere = Ids(a.organization_id, other.plant_id, a.node(1, 0).node_id, other.zone_id, uuid7())
    calls = {
        "catálogo": NODE_CASES[NodeRoute.ZONE_CATALOG].variants["zone_id"],
        "concesión de clip": _clip_upload,
        "latido (plant_id)": NODE_CASES[NodeRoute.HEARTBEAT].variants["plant_id"],
        "hallazgo (plant_id)": NODE_CASES[NodeRoute.FINDING].variants["plant_id"],
        "hallazgo (zone_id)": NODE_CASES[NodeRoute.FINDING].variants["zone_id"],
        "resultado (plant_id)": NODE_CASES[NodeRoute.UPDATE_RESULT].variants["plant_id"],
    }
    for label, build in calls.items():
        response = world.send(build(world, node, elsewhere), node)
        assert _rejection(response)[:2] == (403, MISMATCH), (label, response.text)
    # La prueba no pasa por una guarda que lo rechaza todo: la zona propia sí se sirve.
    own = world.send(NodeCall("GET", f"/api/nodes/zones/{node.zone_id}/catalog"), node)
    assert own.status_code == 200, own.text


@pytest.mark.integration
def test_the_zone_guard_a_zone_of_the_same_plant_not_assigned_to_the_node(
    world: NodeWorld,
) -> None:
    a = world.a
    node = a.node(0, 0)
    sibling = a.zone(0, 1)  # misma planta, asignada al segundo nodo
    target = Ids(a.organization_id, node.plant_id, node.node_id, sibling.zone_id, uuid7())
    for build in (
        NODE_CASES[NodeRoute.ZONE_CATALOG].variants["zone_id"],
        _clip_upload,
        NODE_CASES[NodeRoute.FINDING].variants["zone_id"],
        NODE_CASES[NodeRoute.DETECTION_REVIEW].variants["zone_id"],
        NODE_CASES[NodeRoute.OBSERVABILITY_EVENT].variants["zone_id"],
    ):
        response = world.send(build(world, node, target), node)
        assert _rejection(response)[:2] == (403, MISMATCH), response.text
    # El clip de verificación del otro nodo de la misma planta tampoco es suyo.
    clip = _verification_clip(world.fleet, a.node(0, 1))
    confirmed = world.send(NodeCall("POST", f"/api/nodes/clip-uploads/{clip}/confirmation"), node)
    missing = world.send(NodeCall("POST", f"/api/nodes/clip-uploads/{uuid7()}/confirmation"), node)
    assert _comparable(confirmed) == _comparable(missing)
    assert _rejection(confirmed)[:2] == (403, MISMATCH)


@pytest.mark.integration
@pytest.mark.parametrize(
    "route", [NodeRoute.FINDING, NodeRoute.DETECTION_REVIEW, NodeRoute.OBSERVABILITY_EVENT]
)
def test_the_zone_guard_looks_at_the_assignment_in_node_time_started_at(
    world: NodeWorld, route: NodeRoute
) -> None:
    node = world.a.node(0, 0)
    path = route.path
    own = _scope(node, Ids.missing(), "none")
    before = dict(own, node_time=_fact(node.assigned_at - timedelta(minutes=1)))
    rejected = world.send(NodeCall("POST", path, before), node)
    assert _rejection(rejected) == (403, MISMATCH, "zone_id"), rejected.text
    # Asignada en el instante del hecho: pasa la guarda y la rechaza el esquema (cuerpo mínimo).
    during = dict(own, node_time=_fact(node.assigned_at + timedelta(minutes=1)))
    passed = world.send(NodeCall("POST", path, during), node)
    assert _code(passed) == "schema_invalid", passed.text


# --- Ninguna fila de una organización con datos de la otra ----------------------------------------


def _identifiers(organization: GobOrganization) -> set[str]:
    values = {str(organization.organization_id)}
    for plant, zones in zip(organization.plants, organization.zones, strict=True):
        values.add(str(plant))
        values.update(str(zone.zone_id) for zone in zones)
    for nodes in organization.nodes:
        values.update(str(node.node_id) for node in nodes)
    return values


@pytest.mark.integration
def test_no_row_of_one_organization_names_the_other(world: NodeWorld) -> None:
    # Después de todas las peticiones de A con recursos de B (orden de las pruebas: este módulo
    # corre las de arriba antes), ninguna fila de A contiene un identificador de B ni al revés.
    node = world.a.node(0, 0)
    for case in NODE_CASES.values():
        for build in case.variants.values():
            world.send(build(world, node, world.foreign()), node)
    leaks: list[str] = []
    for mine, theirs in ((world.a, world.b), (world.b, world.a)):
        forbidden = _identifiers(theirs)
        for table in world.tables:
            rows = world.fleet.fetch(
                f"SELECT to_jsonb(t)::text AS row FROM {table} AS t"  # noqa: S608
                " WHERE organization_id = $1",
                mine.organization_id,
            )
            for row in rows:
                found = sorted(value for value in forbidden if value in row["row"])
                if found:
                    leaks.append(f"{table} de {mine.organization_id}: {found}")
    assert not leaks, "\n".join(leaks)
