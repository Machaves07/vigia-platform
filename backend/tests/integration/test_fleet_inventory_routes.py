"""Inventario de flota y umbrales por planta por HTTP contra PostgreSQL 16 real (TASK-224).

La aplicación real (cadena fija, ``ScopeContexts`` y ``ContextAuthorizer``) como ``vigia_app``, con
``FleetInventory`` y ``FleetThresholdsService`` (``tests/fleet_inventory_support.py``):

- ``GET /fleet/nodes``: un nodo declarado sin latido aparece ``unknown`` con «sin latido recibido»;
  el objeto completo con ``last_update_result = failed``, ``live_view_local_url``, cámaras y zonas
  del **último** latido y el ``code`` de cada cámara del catálogo vigente; una página de 100 nodos
  con 8 cámaras en **una** sentencia contada, ``READ ONLY`` y sin cambiar ninguna tabla; nodo
  revocado o dado de baja sin avisos (BR-GOB-76); estado de comunicación del expediente aunque la
  proyección diga ``reachable`` (revisión de VIG-157, menor 1); filtros, cursor y parámetros;
- ``GET /fleet/nodes/{node_id}``: la historia de latidos de 90 días por cursor, solo
  ``payload_summary``;
- guardas de alcance (PR-GOB-12, NFR-GOB-30, H-47): otra organización igual que inexistente; un
  administrador de planta no ve la otra planta; uno de zona solo los nodos de su zona, con
  ``zones`` y ``zone_states`` reducidas, y nunca el detalle; bajo concesión, ``fleet_read``;
- umbrales: valores por defecto, ``PUT`` con auditoría ``fleet_thresholds_changed``,
  ``fleet_threshold_invalid`` sin escribir nada, solo ``fleet.manage``; dos ``PUT`` concurrentes
  sobre una planta sin fila (``INSERT … ON CONFLICT``).

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base, al milisegundo.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from tests.fleet_http_support import FleetStack, fleet_stack
from tests.fleet_inventory_support import (
    InventoryWorld,
    NodeState,
    counting,
    fingerprint,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

ALL_WARNINGS = [kind.value for kind in FleetAlarmKind]
LIVE_VIEW_URL = "https://nodo-01.planta.local:8443/"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "fleet_inventory_routes") as built:
        clock = built.authz.sessions.clock
        clock.set(to_millisecond(clock.now()))
        yield built


@pytest.fixture
def world(stack: FleetStack) -> InventoryWorld:
    return InventoryWorld.build(stack)


def _by_id(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {node["node_id"]: node for node in nodes}


def _error(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: httpx.Response) -> tuple[int, Any]:
    body = response.json()
    body.pop("correlation_id", None)
    return response.status_code, body


EVERYTHING = NodeState(
    heartbeat_age_ms=3_600_000,
    pending=500,
    offset_ms=-60_000,
    retires_at="2027-01-01T00:00:00.000Z",
    adapter="simulated",
    certificate_in_ms=3_600_000,
    cameras=((1.0, 10.0),),
    orphan_clips=60,
    day_clips=100,
)
"""Un estado con las ocho condiciones a la vez (con una zona productiva)."""


# --- GET /fleet/nodes ---------------------------------------------------------------------------


def test_a_declared_node_without_heartbeat_is_unknown_and_says_no_heartbeat_received(
    world: InventoryWorld,
) -> None:
    plant = world.plants[0]
    node = world.declare(plant, [world.zones(plant)[0]])
    declared = world.now()
    nodes = _by_id(world.list_all(world.admin))
    item = nodes[str(node)]
    assert item["status"] == "declared"
    assert item["plant_id"] == str(plant)
    assert item["zones"] == [str(world.zones(plant)[0])]
    assert item["communication_state"] == "unknown"
    assert item["since"] == format_timestamp(declared)
    assert item["last_heartbeat_at"] is None
    assert item["heartbeat_notice"] == "no_heartbeat_received"
    assert item["warnings"] == []
    assert item["software_version"] is None and item["cameras"] == []


def test_the_full_inventory_object_of_the_last_heartbeat(world: InventoryWorld) -> None:
    plant = world.plants[0]
    zone, other_zone = world.zones(plant)[:2]
    node = world.declare(plant, [zone, other_zone])
    now = world.next_now()
    state = NodeState(
        pending=3,
        oldest_pending_age_ms=60_000,
        offset_ms=12,
        cameras=((25.0, 10.0), (12.5, 5.0)),
        stale_cameras=((0.0, 10.0),),
        last_update_result="failed",
        target_version="1.5.0",
        live_view_local_url=LIVE_VIEW_URL,
    )
    world.write_state(node, state, now)
    cameras = world.fetch_cameras(node)
    world.add_catalog(zone, [(cameras[0], "CAM-01")])
    heard = now - timedelta(seconds=1)
    for zone_id, mode in ((zone, "productive"), (other_zone, "commissioning")):
        world.stack.execute(
            "INSERT INTO fleet.zone_node_state (organization_id, plant_id, node_id, zone_id, mode,"
            " observability_state, catalog_version_in_node, gate_state_valid_until,"
            " open_episodes, coverage_ok, updated_at)"
            " VALUES ($1, $2, $3, $4, $5, 'degraded', 3, $6, 0, false, $7)",
            world.organization,
            plant,
            node,
            zone_id,
            mode,
            now + timedelta(days=7),
            heard,
        )
    item = _by_id(world.list_all(world.admin))[str(node)]
    assert item["last_update_result"] == "failed"
    assert item["target_version"] == "1.5.0"
    assert item["live_view_local_url"] == LIVE_VIEW_URL
    assert item["software_version"] == "1.4.0" and item["contract_version"] == "1.0.0"
    assert item["model_version"] == "modelo-1"
    assert item["contract_notice"] == {"result": "accepted", "retires_at": None}
    assert item["last_heartbeat_at"] == format_timestamp(heard)
    assert item["local_queue"] == {
        "pending": 3,
        "oldest_pending_at": format_timestamp(now - timedelta(minutes=1)),
        "dead_letter": [{"code": "schema_invalid", "count": 2}],
    }
    assert item["clock"] == {"synchronized": True, "offset_ms": 12}
    assert item["signal_reader"] == {"available": True, "adapter": "modbus_rtu"}
    # Solo las cámaras del último latido; el code del catálogo vigente (o null si no lo declara).
    assert sorted((c["measured_fps"], c["declared_min_fps"]) for c in item["cameras"]) == [
        (12.5, 5.0),
        (25.0, 10.0),
    ]
    codes = {c["camera_id"]: c["code"] for c in item["cameras"]}
    assert codes[str(cameras[0])] == "CAM-01"
    assert codes[str(cameras[1])] is None
    assert sorted(z["zone_id"] for z in item["zone_states"]) == sorted([str(zone), str(other_zone)])
    assert {z["mode"] for z in item["zone_states"]} == {"productive", "commissioning"}
    assert all(z["catalog_version"] == 3 and z["coverage_ok"] is False for z in item["zone_states"])
    assert item["warnings"] == []
    assert item["heartbeat_notice"] is None


def test_a_page_of_100_nodes_with_8_cameras_is_one_read_only_statement_without_writes(
    world: InventoryWorld,
) -> None:
    plant = world.plants[1]
    now = world.next_now()
    eight = tuple((float(10 + i), 5.0) for i in range(8))
    nodes = [world.add_node(plant, []) for _ in range(101)]
    world.write_states({node: NodeState(cameras=eight) for node in nodes}, now)
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    before = fingerprint(world.stack, world.organization)
    with counting(world.stack) as log:
        response = world.get(world.admin, "/fleet/nodes", {"plant_id": str(plant)})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["nodes"]) == 100
    assert all(len(node["cameras"]) == 8 for node in body["nodes"])
    assert body["next_after"] is not None
    statements = log.data_statements()
    assert len(statements) == 1, statements
    assert statements[0].startswith("WITH base AS")
    assert fingerprint(world.stack, world.organization) == before
    rest = world.get(
        world.admin, "/fleet/nodes", {"plant_id": str(plant), "after": body["next_after"]}
    )
    assert len(rest.json()["nodes"]) == 1 and rest.json()["next_after"] is None


def test_a_revoked_or_decommissioned_node_shows_no_warnings(world: InventoryWorld) -> None:
    plant = world.plants[0]
    for zone in world.zones(plant):
        world.set_gate(zone, mounting="approved", usage="approved")
    live, revoked, retired = (world.add_node(plant, [zone]) for zone in world.zones(plant))
    now = world.next_now()
    world.write_state(live, EVERYTHING, now)
    world.write_state(revoked, NodeState(**{**_fields(EVERYTHING), "status": "revoked"}), now)
    world.write_state(
        retired,
        NodeState(**{**_fields(EVERYTHING), "status": "revoked", "decommissioned": True}),
        now,
    )
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    nodes = _by_id(world.list_all(world.admin))
    assert nodes[str(live)]["warnings"] == ALL_WARNINGS
    assert nodes[str(revoked)]["warnings"] == []
    assert nodes[str(retired)]["warnings"] == []
    assert nodes[str(retired)]["decommissioned_at"] is not None


def test_the_communication_state_is_the_ledger_s_not_the_projection(world: InventoryWorld) -> None:
    # Revisión de VIG-157 (menor 1): un latido que llegó mientras se revocaba el nodo deja la
    # proyección en reachable, pero no escribe la transición: manda el expediente.
    plant = world.plants[0]
    node = world.declare(plant, [])
    now = world.next_now()
    last = now - timedelta(minutes=20)
    # Posterior a la declaración (unknown); el intervalo mute empieza en last_heartbeat_at.
    world.communication(node, "mute", now - timedelta(milliseconds=500), last)
    world.write_state(node, NodeState(status="revoked", heartbeat_age_ms=1_000), now)
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    item = _by_id(world.list_all(world.admin))[str(node)]
    assert item["communication_state"] == "mute"
    assert item["since"] == format_timestamp(last)  # mute empieza en last_heartbeat_at
    assert item["heartbeat_notice"] == "no_heartbeat_since"
    assert item["warnings"] == []
    text = json.dumps(item).lower()
    assert "sin eventos" not in text and "no_events" not in text


def test_filters_cursor_and_parameters(world: InventoryWorld) -> None:
    first, second = world.plants
    now = world.next_now()
    mute = [world.add_node(first) for _ in range(3)]
    quiet = [world.add_node(first) for _ in range(2)]
    elsewhere = world.add_node(second)
    for node in mute:
        world.write_state(node, NodeState(heartbeat_age_ms=3_600_000), now)
    for node in [*quiet, elsewhere]:
        world.write_state(node, NodeState(), now)
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    # Por planta y por aviso, con páginas de dos sin repetir ni saltar ningún nodo.
    seen: list[str] = []
    params = {"plant_id": str(first), "warning": "node_mute", "limit": "2"}
    while True:
        world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
        body = world.get(world.admin, "/fleet/nodes", params).json()
        assert len(body["nodes"]) <= 2
        seen += [node["node_id"] for node in body["nodes"]]
        if body["next_after"] is None:
            break
        params["after"] = body["next_after"]
    assert sorted(seen) == sorted(str(n) for n in mute)
    codes = [
        world.stack.fetch(
            "SELECT code FROM identity.node_identity WHERE node_id = $1", uuid.UUID(n)
        )[0]["code"]
        for n in seen
    ]
    assert codes == sorted(codes)
    # Por estado de comunicación (sembrado sin registro del expediente: el de la proyección).
    for state, expected in (("reachable", [str(elsewhere)]), ("unknown", []), ("mute", [])):
        found = world.list_all(world.admin, {"communication_state": state, "plant_id": str(second)})
        assert [node["node_id"] for node in found] == expected, state
    for bad in (
        {"warning": "sin_eventos"},
        {"communication_state": "despejada"},
        {"limit": "101"},
        {"limit": "0"},
        {"after": "%%%"},
        {"after": "Zm9v|"},
        {"desconocido": "1"},
        {"plant_id": "no-es-uuid"},
    ):
        response = world.get(world.admin, "/fleet/nodes", bad)
        assert _error(response)[:2] == (400, "invalid_request"), (bad, response.text)
    repeated = world.stack.run(
        world.stack.client.get(
            "/fleet/nodes?limit=1&limit=2",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Cookie": f"{SESSION_COOKIE_NAME}={world.admin[0].value}",
            },
        )
    )
    assert repeated.status_code == 400


# --- GET /fleet/nodes/{node_id} -----------------------------------------------------------------


def test_the_detail_has_90_days_of_heartbeats_by_cursor_only_the_summary(
    world: InventoryWorld,
) -> None:
    plant = world.plants[0]
    node = world.declare(plant, [])
    now = world.next_now()
    world.write_state(node, NodeState(), now)
    received = [now - timedelta(days=days, seconds=5) for days in (0, 1, 2, 89, 91)]
    for moment in received:
        world.stack.execute(
            "INSERT INTO fleet.heartbeat_history (heartbeat_id, organization_id, plant_id,"
            " node_id, received_at, sent_at, payload_summary)"
            " VALUES ($1, $2, $3, $4, $5, $5, $6)",
            uuid.uuid4(),
            world.organization,
            plant,
            node,
            moment,
            json.dumps({"cameras": {"total": 1}, "uptime_seconds": 7}),
        )
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    pages: list[dict[str, Any]] = []
    params = {"history_limit": "2"}
    while True:
        world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
        response = world.get(world.admin, f"/fleet/nodes/{node}", params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["node"]["node_id"] == str(node)
        pages += body["heartbeat_history"]
        if body["next_history_after"] is None:
            break
        params["history_after"] = body["next_history_after"]
    assert [entry["received_at"] for entry in pages] == [
        format_timestamp(moment) for moment in received[:4]
    ]
    assert all(
        set(entry) == {"heartbeat_id", "received_at", "sent_at", "payload_summary"}
        for entry in pages
    )
    assert pages[0]["payload_summary"] == {"cameras": {"total": 1}, "uptime_seconds": 7}
    assert _error(world.get(world.admin, f"/fleet/nodes/{node}", {"history_after": "x|y"}))[:2] == (
        400,
        "invalid_request",
    )


# --- Guardas de alcance (PR-GOB-12, NFR-GOB-30, H-47) -------------------------------------------


def test_another_organization_is_exactly_like_a_missing_node(world: InventoryWorld) -> None:
    other = InventoryWorld.build(world.stack)
    theirs = other.declare(other.plants[0], [])
    mine = world.declare(world.plants[0], [])
    listed = {node["node_id"] for node in world.list_all(world.admin)}
    assert str(mine) in listed and str(theirs) not in listed
    known = world.get(world.admin, f"/fleet/nodes/{theirs}")
    missing = world.get(world.admin, f"/fleet/nodes/{uuid.uuid4()}")
    assert _comparable(known) == _comparable(missing)
    assert _error(known)[:2] == (404, "not_found")
    # Ni con el filtro de planta de la otra organización.
    assert world.list_all(world.admin, {"plant_id": str(other.plants[0])}) == []


def test_a_plant_administrator_never_sees_the_other_plant(world: InventoryWorld) -> None:
    first, second = world.plants
    mine = world.declare(first, [])
    theirs = world.declare(second, [])
    plant_admin = world.member(ScopeLevel.PLANT, first)
    listed = {node["node_id"] for node in world.list_all(plant_admin)}
    assert str(mine) in listed and str(theirs) not in listed
    assert world.list_all(plant_admin, {"plant_id": str(second)}) == []
    known = world.get(plant_admin, f"/fleet/nodes/{theirs}")
    assert _comparable(known) == _comparable(world.get(plant_admin, f"/fleet/nodes/{uuid.uuid4()}"))
    assert world.get(plant_admin, f"/fleet/nodes/{mine}").status_code == 200


def test_a_zone_administrator_sees_only_the_nodes_of_its_zone_and_never_the_detail(
    world: InventoryWorld,
) -> None:
    plant = world.plants[0]
    zone, other_zone, third = world.zones(plant)
    serving = world.declare(plant, [zone, other_zone])
    other = world.declare(plant, [third])
    now = world.next_now()
    world.write_state(serving, NodeState(), now)
    for zone_id in (zone, other_zone):
        world.stack.execute(
            "INSERT INTO fleet.zone_node_state (organization_id, plant_id, node_id, zone_id, mode,"
            " observability_state, catalog_version_in_node, gate_state_valid_until,"
            " open_episodes, coverage_ok, updated_at)"
            " VALUES ($1, $2, $3, $4, 'commissioning', 'observable', 1, $5, 0, true, $6)",
            world.organization,
            plant,
            serving,
            zone_id,
            now + timedelta(days=7),
            now - timedelta(seconds=1),
        )
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    zone_admin = world.member(ScopeLevel.ZONE, zone)
    listed = _by_id(world.list_all(zone_admin))
    assert set(listed) == {str(serving)}
    assert listed[str(serving)]["zones"] == [str(zone)]
    assert [z["zone_id"] for z in listed[str(serving)]["zone_states"]] == [str(zone)]
    assert str(other) not in listed
    assert _error(world.get(zone_admin, f"/fleet/nodes/{serving}"))[:2] == (404, "not_found")


def test_only_the_assignments_with_fleet_read_widen_the_list(world: InventoryWorld) -> None:
    # Coordinador de toda la organización (sin fleet.read) y administrador de una planta: la lista
    # se reduce a la planta del rol que tiene la clave (narrowed), no a la organización.
    first, second = world.plants
    mine = world.declare(first, [])
    theirs = world.declare(second, [])
    authz = world.stack.authz
    user = authz.add_user(world.organization)
    authz.assign(world.organization, user, Role.COORDINATOR_SST)
    authz.assign(world.organization, user, Role.ADMINISTRATOR, ScopeLevel.PLANT, first)
    both = (authz.open_session(world.organization, user), None)
    listed = {node["node_id"] for node in world.list_all(both)}
    assert str(mine) in listed and str(theirs) not in listed


def test_without_fleet_read_the_inventory_is_not_found(world: InventoryWorld) -> None:
    world.declare(world.plants[0], [])
    coordinator = world.stack.member(world.site, Role.COORDINATOR_SST)
    assert _error(world.get(coordinator, "/fleet/nodes"))[:2] == (404, "not_found")


def test_under_a_plant_concession_the_installer_reads_its_plant_and_it_is_audited(
    world: InventoryWorld,
) -> None:
    first, second = world.plants
    mine = world.declare(first, [])
    theirs = world.declare(second, [])
    installer = world.stack.installer(world.site, ScopeLevel.PLANT, first)
    before = len(world.stack.audit(world.organization, "fleet_read"))
    listed = {node["node_id"] for node in world.list_all(installer)}
    assert str(mine) in listed and str(theirs) not in listed
    assert world.get(installer, f"/fleet/nodes/{mine}").status_code == 200
    assert _error(world.get(installer, f"/fleet/nodes/{theirs}"))[:2] == (404, "not_found")
    entries = world.stack.audit(world.organization, "fleet_read")
    assert len(entries) == before + 2
    assert all(entry["actor_concession_id"] == installer[1] for entry in entries[before:])


# --- Umbrales ------------------------------------------------------------------------------------


def test_thresholds_default_put_audited_and_validated(world: InventoryWorld) -> None:
    plant = world.plants[0]
    path = f"/plants/{plant}/fleet-thresholds"
    default = world.get(world.admin, path)
    assert default.status_code == 200, default.text
    assert default.json() == {
        "plant_id": str(plant),
        "queue_pending_threshold": 100,
        "queue_age_threshold_minutes": 30,
        "clock_drift_threshold_ms": 5000,
        "configured": False,
        "updated_by": None,
        "updated_at": None,
    }
    body = {
        "queue_pending_threshold": 7,
        "queue_age_threshold_minutes": 1,
        "clock_drift_threshold_ms": 2_147_483_647,
    }
    saved = world.stack.send(world.installer, "PUT", path, body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["configured"] is True
    assert world.get(world.admin, path).json() | {"updated_at": None} == saved.json() | {
        "updated_at": None
    }
    entries = world.stack.fetch(
        "SELECT operation, actor_concession_id, scope_plant_id,"
        " convert_from(filters, 'UTF8') AS filters"
        " FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'fleet_thresholds_changed'",
        world.organization,
    )
    assert len(entries) == 1
    assert entries[0]["scope_plant_id"] == plant
    assert entries[0]["actor_concession_id"] == world.installer[1]
    assert json.loads(entries[0]["filters"]) == body
    # < 1 o > 2^31 - 1: invalid_request con su detail_code, sin escribir nada.
    for field in body:
        for value in (0, -5, 2_147_483_648):
            rejected = world.stack.send(world.installer, "PUT", path, {**body, field: value})
            assert _error(rejected) == (400, "invalid_request", "fleet_threshold_invalid")
    for value in ("5", 1.5, True, None):
        rejected = world.stack.send(
            world.installer, "PUT", path, {**body, "queue_pending_threshold": value}
        )
        assert _error(rejected)[:2] == (400, "invalid_request"), value
    assert len(world.stack.audit(world.organization, "fleet_thresholds_changed")) == 1
    (row,) = world.stack.fetch(
        "SELECT queue_pending_threshold FROM fleet.plant_fleet_thresholds WHERE plant_id = $1",
        plant,
    )
    assert row["queue_pending_threshold"] == 7


def test_only_fleet_manage_changes_thresholds_and_other_plants_are_not_found(
    world: InventoryWorld,
) -> None:
    first, second = world.plants
    body = {
        "queue_pending_threshold": 5,
        "queue_age_threshold_minutes": 5,
        "clock_drift_threshold_ms": 5,
    }
    # El administrador lee pero no fija (fleet.manage es solo del instalador).
    denied = world.stack.send(world.admin, "PUT", f"/plants/{first}/fleet-thresholds", body)
    assert _error(denied)[:2] == (404, "not_found")
    other = InventoryWorld.build(world.stack)
    for who, plant in ((world.installer, other.plants[0]), (world.admin, other.plants[0])):
        for method, payload in (("PUT", body), ("GET", None)):
            known = world.stack.send(who, method, f"/plants/{plant}/fleet-thresholds", payload)
            missing = world.stack.send(
                who, method, f"/plants/{uuid.uuid4()}/fleet-thresholds", payload
            )
            assert _comparable(known) == _comparable(missing)
            assert _error(known)[:2] == (404, "not_found")
    plant_installer = world.stack.installer(world.site, ScopeLevel.PLANT, first)
    outside = world.stack.send(plant_installer, "PUT", f"/plants/{second}/fleet-thresholds", body)
    assert _error(outside)[:2] == (404, "not_found")
    zone_admin = world.member(ScopeLevel.ZONE, world.zones(first)[0])
    assert _error(world.get(zone_admin, f"/plants/{first}/fleet-thresholds"))[:2] == (
        404,
        "not_found",
    )
    assert (
        world.stack.fetch(
            "SELECT 1 FROM fleet.plant_fleet_thresholds WHERE plant_id = ANY($1::uuid[])",
            [first, second],
        )
        == []
    )


def test_two_concurrent_puts_on_a_plant_without_row_both_succeed(world: InventoryWorld) -> None:
    plant = world.plants[1]
    path = f"/plants/{plant}/fleet-thresholds"
    cookie, concession = world.installer
    headers = {
        "Sec-Fetch-Site": "same-origin",
        "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}",
        "X-Vigia-Concession": str(concession),
    }

    async def both() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(
                    world.stack.client.put(
                        path,
                        json={
                            "queue_pending_threshold": value,
                            "queue_age_threshold_minutes": value,
                            "clock_drift_threshold_ms": value,
                        },
                        headers=headers,
                    )
                    for value in (11, 22)
                )
            )
        )

    responses = world.stack.run(both())
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    rows = world.stack.fetch(
        "SELECT queue_pending_threshold FROM fleet.plant_fleet_thresholds WHERE plant_id = $1",
        plant,
    )
    assert len(rows) == 1 and rows[0]["queue_pending_threshold"] in (11, 22)
    assert len(world.stack.audit(world.organization, "fleet_thresholds_changed")) == 2


def test_a_threshold_change_is_seen_in_the_next_read_without_other_writes(
    world: InventoryWorld,
) -> None:
    plant = world.plants[0]
    node = world.declare(plant, [])
    now = world.next_now()
    world.write_state(node, NodeState(pending=50, offset_ms=100), now)
    world.stack.authz.sessions.clock.set(now - timedelta(seconds=1))
    assert _by_id(world.list_all(world.admin))[str(node)]["warnings"] == []
    before = fingerprint(world.stack, world.organization)
    records = _record_types(world)
    changed = world.stack.send(
        world.installer,
        "PUT",
        f"/plants/{plant}/fleet-thresholds",
        {"queue_pending_threshold": 49, "queue_age_threshold_minutes": 30,
         "clock_drift_threshold_ms": 99},
    )  # fmt: skip
    assert changed.status_code == 200, changed.text
    after = fingerprint(world.stack, world.organization)
    changed_tables = {table for table in before if before[table] != after[table]}
    # Los umbrales, su auditoría y el provider_query de la petición bajo concesión (BR-NUC-38).
    assert changed_tables == {
        "fleet.plant_fleet_thresholds",
        "shared.audit_entry",
        "ledger.ledger_record",
    }
    assert Counter(_record_types(world)) - Counter(records) == Counter(["provider_query"])
    assert _by_id(world.list_all(world.admin))[str(node)]["warnings"] == [
        "queue_over_threshold",
        "clock_drift",
    ]


def _record_types(world: InventoryWorld) -> list[str]:
    return [
        row["record_type"]
        for row in world.stack.fetch(
            "SELECT record_type FROM ledger.ledger_record WHERE organization_id = $1"
            " ORDER BY chain_sequence, record_id",
            world.organization,
        )
    ]


def _fields(state: NodeState) -> dict[str, Any]:
    return {name: getattr(state, name) for name in NodeState.__dataclass_fields__}
