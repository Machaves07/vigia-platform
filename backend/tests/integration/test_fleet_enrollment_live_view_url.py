"""La URL de la vista en vivo que guarda el latido no bloquea la re-alta (VIG-185; U03-H-04,
U03-H-07, pendiente nº 29; A-35).

El latido acepta y guarda todo ``live_view_local_url`` válido según el contrato (bucle local,
enlace local, nombres sin sufijo local y el nombre más largo que admite el esquema). El alta no usa
como dirección del certificado de servidor una URL guardada que no admitiría: cae al nombre
alternativo de la CSR de servidor, como si no hubiera URL. El orden de las comprobaciones del alta
no cambia (A-37 no fija el código antes que la CSR de servidor).

Contra PostgreSQL 16 real como ``vigia_app`` (``tests/fleet_enrollment_support.py``), con la ruta
real del latido (``HeartbeatService``) en la misma aplicación que el alta: alta → latido con la URL
→ revocación → código nuevo → alta con un código inexistente (``enrollment_code_invalid``, 401) →
re-alta válida (200, certificado de servidor por la dirección de la CSR).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import ipaddress
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from cryptography import x509
from vigia_contracts.models.api import parse_rejection_response

from tests.api_support import World
from tests.fleet_enrollment_support import (
    ENROLLMENT_PATH,
    LONG_SECONDS,
    Enrolled,
    EnrollmentWorld,
    NodeSetup,
    enrollment_world,
)
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import (
    VERSION,
    UnlimitedNodeLimiter,
    node_app,
    node_gate,
    probe_operations,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.application.regression import RegressionService
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.application.heartbeat import HeartbeatDependencies, HeartbeatService
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.routes.credential_rotations import credential_rotation_operation
from vigia_platform.node_api.routes.enrollment import enrollment_operation
from vigia_platform.node_api.routes.heartbeats import heartbeat_operation
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.tokens import LiveViewTokenService

pytestmark = pytest.mark.integration

LONGEST_CONTRACT_HOST: Final = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 50])
"""El anfitrión más largo de ``Heartbeat.live_view_local_url`` (URL de 256 caracteres, el mismo
que dibuja el generador del kit de conformidad)."""
UNKNOWN_CODE: Final = "ABCDEFGHJKLM"
CSR_HOST: Final = ipaddress.ip_address("192.168.10.20")
"""La dirección que anuncia la CSR de servidor de ``EnrollmentWorld.body`` (``local_ip()``)."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[EnrollmentWorld]:
    with enrollment_world(postgres_endpoint, "fleet_enrollment_url") as built:
        yield built


@pytest.fixture(scope="module")
def client(world: EnrollmentWorld) -> Iterator[httpx.AsyncClient]:
    """La aplicación de las rutas del contrato con el alta, la rotación y el latido reales."""
    fleet = world.fleet
    authz = fleet.authz
    clock = authz.sessions.clock
    database = fleet.database
    outbox = fleet.outbox
    assert outbox is not None
    catalog = PostgresCatalogRepository(database)
    heartbeat = HeartbeatService(
        HeartbeatDependencies(
            database=database,
            writer=fleet.writer,
            clock=clock,
            key_sets=world.signing.service,
            gates=world.gate_service(),
            regression=RegressionService(
                repository=PostgresRegressionRepository(database),
                catalog=catalog,
                database=database,
                writer=fleet.writer,
                authorizer=authz.authorizer,
                audit=authz.sessions.audit,
                free_text=fleet.free_text,
                clock=clock,
            ),
            identity=fleet.deps.identity,
            live_view=LiveViewTokenService(
                database=database,
                authorizer=authz.authorizer,
                audit=authz.sessions.audit,
                outbox=outbox,
                signer=world.signing.service,
                clock=clock,
            ),
            retires_at=lambda _version: None,
            nodes=PostgresNodeFleetStore(database),
        )
    )
    store = PostgresNodeContextStore(database)
    limits = NodeRateLimits(UnlimitedNodeLimiter(clock))
    gate = node_gate(
        contexts=authz.contexts,
        store=store,
        clock=clock,
        probe=world.probe,
        limits=limits,
        operations={
            **probe_operations(world.probe),
            NodeRoute.ENROLLMENT: enrollment_operation(
                world.enrollment, NodeIdentity(contexts=authz.contexts, store=store), limits
            ),
            NodeRoute.CREDENTIAL_ROTATION: credential_rotation_operation(world.rotation),
            NodeRoute.HEARTBEAT: heartbeat_operation(heartbeat),
        },
    )
    built = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=node_app(World(clock=clock), gate)),
        base_url="https://nodes.vigia.test",
        timeout=LONG_SECONDS,
    )
    try:
        yield built
    finally:
        world.run(built.aclose())


def _heartbeat(world: EnrollmentWorld, setup: NodeSetup, url: str) -> dict[str, Any]:
    """Un ``Heartbeat`` válido del nodo con ``live_view_local_url = url``."""
    now = world.now()
    (zone,) = setup.zones
    cameras = world.fetch(
        "SELECT camera_id, declared_min_fps FROM catalog.zone_camera WHERE zone_id = $1"
        " ORDER BY camera_id",
        zone,
    )
    return {
        "heartbeat_id": str(uuid7(world.fleet.authz.sessions.clock)),
        "contract_version": VERSION,
        "organization_id": str(setup.organization_id),
        "plant_id": str(setup.plant_id),
        "node_id": str(setup.node_id),
        "sent_at": format_timestamp(now),
        "node_clock": {"synchronized": True, "offset_ms": 12, "source": "ntp_local"},
        "software_version": "1.4.0",
        "model_version": "modelo-1.0",
        "uptime_seconds": 3600,
        "cameras": [
            {
                "camera_id": str(row["camera_id"]),
                "connected": True,
                "measured_fps": 12.0,
                "observability_state": "observable",
                "declared_min_fps": float(row["declared_min_fps"]),
            }
            for row in cameras
        ],
        "zones": [
            {
                "zone_id": str(zone),
                "mode": "productive",
                "observability_state": "observable",
                "catalog_version": 1,
                "gate_state_valid_until": format_timestamp(now),
                "open_episodes": 0,
            }
        ],
        "signal_reader": {"available": True, "adapter": "modbus_rtu"},
        "local_queue": {"pending": 0, "dead_letter": [], "retained_sent": 0},
        "live_view_local_url": url,
    }


def _send_heartbeat(
    world: EnrollmentWorld, client: httpx.AsyncClient, enrolled: Enrolled, body: dict[str, Any]
) -> httpx.Response:
    response: httpx.Response = world.run(
        client.post(
            NodeRoute.HEARTBEAT.path,
            content=json.dumps(body).encode(),
            headers={**enrolled.headers, "Content-Type": "application/json"},
        )
    )
    return response


def _stored_urls(world: EnrollmentWorld, node_id: uuid.UUID) -> tuple[str | None, str | None]:
    (row,) = world.fetch(
        "SELECT f.live_view_local_url AS fleet, n.live_view_local_url AS identity"
        " FROM fleet.node_fleet_record AS f JOIN identity.node_identity AS n"
        " ON n.node_id = f.node_id WHERE f.node_id = $1",
        node_id,
    )
    return row["fleet"], row["identity"]


def _server_host(document: Any) -> list[Any]:
    certificate = x509.load_pem_x509_certificate(document["server_certificate_pem"].encode())
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return list(names)


@pytest.mark.parametrize(
    "host",
    ["[::1]", "[fe80::1]", LONGEST_CONTRACT_HOST, "nodo-01.example.com"],
    ids=["bucle-local-ipv6", "enlace-local-ipv6", "nombre-mas-largo", "nombre-de-internet"],
)
def test_a_stored_url_that_enrollment_would_not_admit_does_not_block_re_enrollment(
    world: EnrollmentWorld, client: httpx.AsyncClient, host: str
) -> None:
    url = f"https://{host}:8443/"
    setup = world.declared()
    first = world.enroll(setup)

    # El latido sigue aceptando y guardando la URL válida según el contrato.
    accepted = _send_heartbeat(world, client, first, _heartbeat(world, setup, url))
    assert accepted.status_code == 200, accepted.text
    assert _stored_urls(world, setup.node_id) == (url, url)

    assert world.fleet.revoke(setup.installer, setup.node_id).status_code == 200
    code = world.code(setup)

    # Un código inexistente con CSR válidas es enrollment_code_invalid, no schema_invalid.
    refused = world.post(ENROLLMENT_PATH, world.body(setup, UNKNOWN_CODE), client=client)
    rejection = parse_rejection_response(refused.content)
    assert (refused.status_code, rejection.code.value) == (401, "enrollment_code_invalid")
    assert [row["result"] for row in world.attempts(setup.node_id)][-1] == (
        "enrollment_code_invalid"
    )
    assert world.code_statuses(setup.node_id)[-1] == "active"

    # La re-alta válida se acepta, con el certificado de servidor por la dirección de la CSR.
    again = world.post(ENROLLMENT_PATH, world.body(setup, code), client=client)
    assert again.status_code == 200, again.text
    assert world.fleet_record(setup.node_id)["status"] == "enrolled"
    assert _server_host(again.json()) == [x509.IPAddress(CSR_HOST)]
    assert [row["status"] for row in world.credentials(setup.node_id)] == ["revoked", "active"]


def test_a_stored_local_url_still_wins_over_the_csr(
    world: EnrollmentWorld, client: httpx.AsyncClient
) -> None:
    # Sin regresión: una URL guardada que sí es local sigue mandando sobre la CSR.
    url = "https://camara-norte.local:8443/"
    setup = world.declared()
    first = world.enroll(setup)
    accepted = _send_heartbeat(world, client, first, _heartbeat(world, setup, url))
    assert accepted.status_code == 200, accepted.text
    assert world.fleet.revoke(setup.installer, setup.node_id).status_code == 200
    again = world.post(ENROLLMENT_PATH, world.body(setup, world.code(setup)), client=client)
    assert again.status_code == 200, again.text
    assert _server_host(again.json()) == [x509.DNSName("camara-norte.local")]
