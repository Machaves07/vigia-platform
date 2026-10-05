"""Versión objetivo y resultado de actualización contra PostgreSQL 16 real (TASK-226; LC-GOB-17).

**Sin propiedades nuevas** (BL §6, C-PLA-16): no transforma datos ni tiene máquina de estados
propia; la decisión de ventana ya la cubre PR-GOB-07 por oráculo (``is_compatible`` de U-01).
Aquí, como ``vigia_app`` y con los servicios reales:

- ``POST /fleet/target-versions`` por HTTP (``tests/fleet_http_support.py``, instalador del
  proveedor bajo concesión, la única persona con ``fleet.manage``): dentro de la ventana deja la
  publicación, el registro ``node_target_version_published`` con la ventana de mantenimiento, la
  proyección ``NodeInventory.target_version``, **un** evento por nodo sin la ventana y su auditoría,
  en **una** transacción (un fallo del escritor no deja nada); fuera de la ventana,
  ``fleet_version_outside_contract_window`` sin nada escrito (BR-GOB-101, 102); ``group``; cuerpos
  inválidos; guardas de alcance (otra organización, otra planta, concesión de otra planta →
  ``not_found`` sin publicar, NFR-GOB-30);
- D-5 con el latido real (``tests/heartbeat_support.py``): la respuesta lleva
  ``target_software_version`` y **nunca** la ventana de mantenimiento;
- ``POST update-results`` (``tests/fleet_versions_support.py``): cinco envíos simultáneos del mismo
  resultado → un registro, un evento y cuatro ``accepted_duplicate`` con el mismo ``Receipt``; otro
  ``result`` con el mismo ``update_result_id`` → ``idempotency_conflict``; otra organización, planta
  o nodo que los del certificado → ``node_zone_mismatch``; ``schema_invalid`` de lo que el esquema
  no expresa;
- **orden de candados** (filas del inventario por ``node_id`` → cadena de la planta): publicación y
  resultado del mismo nodo a la vez, publicación y latido a la vez, y dos publicaciones con los
  nodos en orden inverso terminan siempre, sin ``temporarily_unavailable``.

Topes: la base 60 s, la retención de la primera operación 3 s (no es un tope de «llegó a tiempo»).
Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest

from tests.authz_support import Site
from tests.fleet_http_support import FleetStack, fleet_stack
from tests.fleet_versions_support import (
    NodeApp,
    installer_context,
    inventory_row,
    node_app,
    publisher,
    send_update,
    update_result,
)
from tests.heartbeat_support import HeartbeatStack, NodeSite, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import verify_ledger_chains
from vigia_platform.fleet.application.target_versions import TargetVersionService
from vigia_platform.fleet.domain.fleet_versions import NodeGroup
from vigia_platform.node_api.routes.heartbeats import heartbeat_operation
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.context import ScopeContext, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

IN_WINDOW: Final = "1.0.4"
"""La plataforma implementa la 1.0.0 del contrato: la serie 1.0 está en ventana."""
HOLD_SECONDS: Final = 3.0
"""Cuánto retiene la primera operación sus candados para que la otra arranque. No es un tope de
«llegó a tiempo»: con el orden bueno el resultado no depende de él."""
ROUNDS: Final = 5


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "fleet_target_versions") as built:
        clock = built.authz.sessions.clock
        clock.set(to_millisecond(clock.now()))
        yield built


@pytest.fixture(scope="module")
def hb(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "fleet_update_results") as built:
        yield built


@pytest.fixture(scope="module")
def nodes_app(hb: HeartbeatStack) -> Iterator[NodeApp]:
    built = node_app(
        hb.authz,
        hb.primary.database,
        hb.writer,
        {NodeRoute.HEARTBEAT: heartbeat_operation(hb.primary.service)},
    )
    yield built
    hb.run(built.client.aclose())


# --- POST /fleet/target-versions por HTTP -------------------------------------------------------


@dataclass
class Fleet:
    """Una organización con dos plantas, su instalador bajo concesión y nodos declarados."""

    stack: FleetStack
    site: Site
    who: Any
    plants: tuple[uuid.UUID, uuid.UUID]

    @classmethod
    def build(cls, stack: FleetStack) -> Fleet:
        site = stack.site(plants=2, zones=1)
        first, second = site.plants
        return cls(stack, site, stack.installer(site), (first, second))

    @property
    def organization(self) -> uuid.UUID:
        return self.site.organization_id

    def declare(self, plant: uuid.UUID) -> uuid.UUID:
        response = self.stack.declare(self.who, plant, [])
        assert response.status_code == 201, response.text
        return uuid.UUID(response.json()["node_id"])

    def with_inventory(self, plant: uuid.UUID, node: uuid.UUID) -> None:
        inventory_row(
            self.stack.authz,
            organization_id=self.organization,
            plant_id=plant,
            node_id=node,
            at=self.stack.authz.now(),
        )

    def publish(self, body: dict[str, Any], who: Any = None) -> httpx.Response:
        return self.stack.send(who or self.who, "POST", "/fleet/target-versions", body)

    def written(self) -> dict[str, int]:
        """Lo que hay escrito de la versión objetivo en la organización."""
        stack, organization = self.stack, self.organization
        (row,) = stack.fetch(
            "SELECT count(*) AS n FROM fleet.target_version_publication WHERE organization_id = $1",
            organization,
        )
        return {
            "publications": int(row["n"]),
            "records": len(stack.records("node_target_version_published", organization)),
            "events": len(stack.events("target_version_published", organization)),
            "audit": len(stack.audit(organization, "target_version_published")),
            "projected": sum(
                1
                for inventory in stack.fetch(
                    "SELECT target_version FROM fleet.node_inventory WHERE organization_id = $1",
                    organization,
                )
                if inventory["target_version"] is not None
            ),
        }


NOTHING: Final = {"publications": 0, "records": 0, "events": 0, "audit": 0, "projected": 0}


def _body(
    plant: uuid.UUID,
    nodes: Sequence[uuid.UUID] | None = None,
    *,
    group: str | None = None,
    version: str = IN_WINDOW,
    start: str = "2026-11-02T01:00:00.000Z",
    end: str = "2026-11-02T03:30:00.000Z",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "plant_id": str(plant),
        "target_version": version,
        "maintenance_window": {"from": start, "to": end},
    }
    if nodes is not None:
        body["node_ids"] = [str(node) for node in nodes]
    if group is not None:
        body["group"] = group
    return body


def _error(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _comparable(response: httpx.Response) -> tuple[int, Any]:
    body = response.json()
    body.pop("correlation_id", None)
    return response.status_code, body


def test_br_gob_101_102_inside_the_window_everything_is_written_with_one_event_per_node(
    stack: FleetStack,
) -> None:
    fleet = Fleet.build(stack)
    plant = fleet.plants[0]
    nodes = [fleet.declare(plant) for _ in range(3)]
    fleet.with_inventory(plant, nodes[0])
    response = fleet.publish(_body(plant, nodes))
    assert response.status_code == 201, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["target_version"] == IN_WINDOW
    assert body["plant_id"] == str(plant)
    assert body["node_ids"] == [str(node) for node in nodes]
    assert body["maintenance_window"] == {
        "from": "2026-11-02T01:00:00.000Z",
        "to": "2026-11-02T03:30:00.000Z",
    }
    publication_id = body["publication_id"]
    # La publicación ⛓ con su ventana informativa (D-5) y su registro.
    (row,) = stack.fetch(
        "SELECT plant_id, target_version, node_ids, maintenance_window_from,"
        " maintenance_window_to, published_by, published_at, ledger_record_id"
        " FROM fleet.target_version_publication WHERE publication_id = $1",
        uuid.UUID(publication_id),
    )
    assert row["node_ids"] == nodes and row["target_version"] == IN_WINDOW
    assert format_timestamp(row["maintenance_window_from"]) == "2026-11-02T01:00:00.000Z"
    assert format_timestamp(row["maintenance_window_to"]) == "2026-11-02T03:30:00.000Z"
    assert str(row["published_by"]) == body["published_by"]
    assert format_timestamp(row["published_at"]) == body["published_at"]
    (record,) = stack.records("node_target_version_published", fleet.organization)
    assert record["record_id"] == row["ledger_record_id"]
    assert str(record["record_id"]) == body["ledger_record_id"]
    assert record["source_key"] == publication_id
    assert record["plant_id"] == plant
    assert record["content"] == {
        "publication_id": publication_id,
        "target_version": IN_WINDOW,
        "node_ids": [str(node) for node in nodes],
        "maintenance_window": {
            "starts_at": "2026-11-02T01:00:00.000Z",
            "ends_at": "2026-11-02T03:30:00.000Z",
        },
    }
    # Un evento por nodo, sin la ventana (interfaces §2, D-5).
    events = stack.events("target_version_published", fleet.organization)
    assert sorted(events, key=lambda event: event["node_id"]) == sorted(
        ({"node_id": str(node), "target_version": IN_WINDOW} for node in nodes),
        key=lambda event: event["node_id"],
    )
    # NodeInventory.target_version: la proyección del nodo con fila y, sin fila, la lectura.
    (inventory,) = stack.fetch(
        "SELECT target_version FROM fleet.node_inventory WHERE node_id = $1", nodes[0]
    )
    assert inventory["target_version"] == IN_WINDOW
    for node in nodes:
        detail = stack.send(fleet.who, "GET", f"/fleet/nodes/{node}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["node"]["target_version"] == IN_WINDOW
    (audit,) = stack.audit(fleet.organization, "target_version_published")
    assert audit["scope_plant_id"] == plant and str(audit["resource_id"]) == publication_id
    assert fleet.written() == {
        "publications": 1,
        "records": 1,
        "events": 3,
        "audit": 1,
        "projected": 1,
    }


@pytest.mark.parametrize("version", ["1.1.0", "2.0.0", "0.9.9", "1.1.0-rc.1"])
def test_br_gob_101_outside_the_window_answers_its_detail_code_and_writes_nothing(
    stack: FleetStack, version: str
) -> None:
    fleet = Fleet.build(stack)
    plant = fleet.plants[0]
    node = fleet.declare(plant)
    fleet.with_inventory(plant, node)
    response = fleet.publish(_body(plant, [node], version=version))
    assert _error(response) == (400, "invalid_request", "fleet_version_outside_contract_window")
    assert fleet.written() == NOTHING


def test_a_newer_publication_replaces_the_target_of_the_inventory(stack: FleetStack) -> None:
    fleet = Fleet.build(stack)
    plant = fleet.plants[0]
    node = fleet.declare(plant)
    fleet.with_inventory(plant, node)
    assert fleet.publish(_body(plant, [node], version="1.0.4")).status_code == 201
    assert fleet.publish(_body(plant, [node], version="1.0.5")).status_code == 201
    (inventory,) = stack.fetch(
        "SELECT target_version FROM fleet.node_inventory WHERE node_id = $1", node
    )
    assert inventory["target_version"] == "1.0.5"
    assert stack.send(fleet.who, "GET", f"/fleet/nodes/{node}").json()["node"][
        "target_version"
    ] == ("1.0.5")


class FailingWriter:
    """El escritor que falla al escribir el registro, ya con la publicación y la proyección
    hechas en la transacción."""

    async def write(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fallo sintético del expediente")


def test_bl_2_7_one_transaction_a_failure_of_the_record_leaves_nothing(stack: FleetStack) -> None:
    fleet = Fleet.build(stack)
    plant = fleet.plants[0]
    node = fleet.declare(plant)
    fleet.with_inventory(plant, node)
    service = publisher(stack.authz, stack.database, FailingWriter())  # type: ignore[arg-type]
    context = stack.context(fleet.who)
    start = stack.authz.now() + dt.timedelta(days=1)
    with pytest.raises(RuntimeError, match="fallo sintético"):
        stack.run(
            service.publish(
                context,
                plant,
                node_ids=[node],
                group=None,
                target_version=IN_WINDOW,
                window_from=start,
                window_to=start + dt.timedelta(hours=2),
            )
        )
    assert fleet.written() == NOTHING


def test_group_publishes_the_live_nodes_of_the_plant_and_keeps_the_resolved_list(
    stack: FleetStack,
) -> None:
    fleet = Fleet.build(stack)
    plant, other = fleet.plants
    live = [fleet.declare(plant) for _ in range(2)]
    revoked = fleet.declare(plant)
    retired = fleet.declare(plant)
    fleet.declare(other)  # de la otra planta: nunca entra
    assert stack.revoke(fleet.who, revoked).status_code == 200
    assert stack.revoke(fleet.who, retired).status_code == 200
    assert stack.decommission(fleet.who, retired).status_code == 200
    response = fleet.publish(_body(plant, group=NodeGroup.PLANT.value))
    assert response.status_code == 201, response.text
    resolved = sorted(live)
    assert response.json()["node_ids"] == [str(node) for node in resolved]
    (record,) = stack.records("node_target_version_published", fleet.organization)
    assert record["content"]["node_ids"] == [str(node) for node in resolved]
    events = stack.events("target_version_published", fleet.organization)
    assert sorted(event["node_id"] for event in events) == sorted(str(node) for node in live)


def test_group_without_live_nodes_is_invalid_and_writes_nothing(stack: FleetStack) -> None:
    fleet = Fleet.build(stack)
    plant, other = fleet.plants
    fleet.declare(other)
    response = fleet.publish(_body(plant, group=NodeGroup.PLANT.value))
    assert _error(response) == (400, "invalid_request", None)
    assert fleet.written() == NOTHING


def test_invalid_bodies_are_invalid_request_and_write_nothing(stack: FleetStack) -> None:
    fleet = Fleet.build(stack)
    plant = fleet.plants[0]
    node = fleet.declare(plant)
    fleet.with_inventory(plant, node)
    both = _body(plant, [node], group=NodeGroup.PLANT.value)
    neither = _body(plant)
    same = _body(plant, [node], start="2026-11-02T01:00:00.000Z", end="2026-11-02T01:00:00.000Z")
    reversed_window = _body(
        plant, [node], start="2026-11-02T03:00:00.000Z", end="2026-11-02T01:00:00.000Z"
    )
    repeated = _body(plant, [node, node])
    mixed_case = _body(plant, [node], version="1.0.4-RC1")
    naive = _body(plant, [node], start="2026-11-02T01:00:00", end="2026-11-02T03:00:00")
    extra = {**_body(plant, [node]), "target_maintenance_window": "x"}
    empty = _body(plant, [])
    for body in (both, neither, same, reversed_window, repeated, mixed_case, naive, extra, empty):
        assert _error(fleet.publish(body))[:2] == (400, "invalid_request"), body
    assert fleet.written() == NOTHING


def test_nfr_gob_30_a_node_of_another_organization_or_plant_is_not_found_and_nothing_is_published(
    stack: FleetStack,
) -> None:
    fleet = Fleet.build(stack)
    stranger = Fleet.build(stack)
    plant, other_plant = fleet.plants
    own = fleet.declare(plant)
    fleet.with_inventory(plant, own)
    in_other_plant = fleet.declare(other_plant)
    of_other_organization = stranger.declare(stranger.plants[0])
    missing = fleet.publish(_body(plant, [own, uuid.uuid4()]))
    assert _error(missing) == (404, "not_found", None)
    for intruder in (in_other_plant, of_other_organization):
        response = fleet.publish(_body(plant, [own, intruder]))
        assert _comparable(response) == _comparable(missing), intruder
    # La planta de otra organización tampoco existe para este contexto.
    foreign_plant = fleet.publish(_body(stranger.plants[0], [of_other_organization]))
    assert _error(foreign_plant) == (404, "not_found", None)
    assert fleet.written() == NOTHING
    assert stranger.written() == NOTHING


def test_a_plant_concession_does_not_publish_in_the_other_plant(stack: FleetStack) -> None:
    fleet = Fleet.build(stack)
    plant, other_plant = fleet.plants
    node = fleet.declare(plant)
    scoped = stack.installer(fleet.site, ScopeLevel.PLANT, other_plant)
    response = fleet.publish(_body(plant, [node]), who=scoped)
    assert _error(response) == (404, "not_found", None)
    assert fleet.written() == NOTHING
    own = fleet.declare(other_plant)
    assert fleet.publish(_body(other_plant, [own]), who=scoped).status_code == 201


def test_the_inventory_reads_target_and_result_of_a_node_that_never_sent_a_heartbeat(
    stack: FleetStack,
) -> None:
    fleet = Fleet.build(stack)
    plant = fleet.plants[0]
    node = fleet.declare(plant)
    assert fleet.publish(_body(plant, [node])).status_code == 201
    stack.execute(
        "INSERT INTO fleet.update_result (update_result_id, organization_id, plant_id, node_id,"
        " target_version, result, reported_at, ledger_record_id)"
        " VALUES ($1, $2, $3, $4, $5, 'reverted', $6, $7)",
        uuid.uuid4(),
        fleet.organization,
        plant,
        node,
        IN_WINDOW,
        stack.authz.now(),
        uuid.uuid4(),
    )
    item = stack.send(fleet.who, "GET", f"/fleet/nodes/{node}").json()["node"]
    assert item["target_version"] == IN_WINDOW
    assert item["last_update_result"] == "reverted"
    assert item["software_version"] is None


# --- D-5: la ventana de mantenimiento no viaja en el latido -------------------------------------


def _publish_in(
    hb: HeartbeatStack,
    site: NodeSite,
    nodes: Sequence[uuid.UUID],
    version: str = IN_WINDOW,
    *,
    service: TargetVersionService | None = None,
    context: ScopeContext | None = None,
) -> Any:
    service = service or publisher(hb.authz, hb.primary.database, hb.writer)
    context = context or installer_context(hb.authz, site.organization_id)
    start = hb.now() + dt.timedelta(days=2)
    return service.publish(
        context,
        site.plant_id,
        node_ids=list(nodes),
        group=None,
        target_version=version,
        window_from=start,
        window_to=start + dt.timedelta(hours=3),
    )


def test_d5_the_heartbeat_carries_the_target_version_and_never_the_maintenance_window(
    hb: HeartbeatStack,
) -> None:
    site = hb.site()
    first = hb.post(site)
    assert first.status_code == 200, first.text
    assert "target_software_version" not in first.json()
    publication = hb.run(_publish_in(hb, site, [site.node_id]))
    hb.tick()
    second = hb.post(site)
    assert second.status_code == 200, second.text
    document = second.json()
    assert document["target_software_version"] == IN_WINDOW
    text = second.text
    assert "maintenance" not in text
    for moment in (publication.window.starts_at, publication.window.ends_at):
        assert format_timestamp(moment) not in text
    # En la publicación y en el registro sí está.
    (record,) = [
        r
        for r in hb.records("node_target_version_published", site.organization_id)
        if r["content"]["publication_id"] == str(publication.publication_id)
    ]
    assert record["content"]["maintenance_window"] == publication.window.content()
    assert hb.fetch(
        "SELECT target_version FROM fleet.node_inventory WHERE node_id = $1", site.node_id
    )[0]["target_version"] == (IN_WINDOW)


# --- POST update-results ------------------------------------------------------------------------


def _document(hb: HeartbeatStack, site: NodeSite, **changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "organization_id": site.organization_id,
        "plant_id": site.plant_id,
        "node_id": site.node_id,
        "update_result_id": hb.uuid7(),
        "verified_at": hb.now() - dt.timedelta(minutes=2),
        "target_version": IN_WINDOW,
    }
    fields.update(changes)
    return update_result(**fields)


def _post(
    hb: HeartbeatStack, app: NodeApp, site: NodeSite, document: dict[str, Any], **kwargs: Any
) -> httpx.Response:
    response: httpx.Response = hb.run(send_update(app.client, site.certificate, document, **kwargs))
    return response


def _results(hb: HeartbeatStack, site: NodeSite) -> dict[str, Any]:
    organization = site.organization_id
    return {
        "records": hb.records("update_result_received", organization),
        "events": hb.fetch(
            "SELECT payload::text AS payload FROM shared.outbox_event"
            " WHERE event_name = 'update_result_received' AND organization_id = $1",
            organization,
        ),
        "rows": hb.fetch(
            "SELECT update_result_id, result, reported_at, ledger_record_id"
            " FROM fleet.update_result WHERE organization_id = $1",
            organization,
        ),
    }


def _counts(hb: HeartbeatStack, site: NodeSite) -> tuple[int, int, int]:
    found = _results(hb, site)
    return len(found["records"]), len(found["events"]), len(found["rows"])


class Barrier:
    """Retiene la búsqueda previa del duplicado hasta que llegan las ``count`` peticiones: todas
    pasan la comprobación y la carrera se resuelve **siempre** en la escritura."""

    def __init__(self, app: NodeApp, count: int) -> None:
        self.count = count
        self.arrived = 0
        self.all_here = asyncio.Event()
        store: Any = app.updates._store
        self.store = store
        self.original = store.accepted_result

        async def accepted_result(*args: Any, **kwargs: Any) -> Any:
            found = await self.original(*args, **kwargs)
            if self.arrived < self.count:
                self.arrived += 1
                if self.arrived == self.count:
                    self.all_here.set()
                await asyncio.wait_for(self.all_here.wait(), 60.0)
            return found

        store.accepted_result = accepted_result

    def remove(self) -> None:
        self.store.accepted_result = self.original


def test_five_simultaneous_identical_results_leave_one_record_one_event_and_four_duplicates(
    hb: HeartbeatStack, nodes_app: NodeApp
) -> None:
    site = hb.site()
    assert hb.post(site).status_code == 200
    document = _document(hb, site)
    barrier = Barrier(nodes_app, 5)
    try:

        async def run() -> list[httpx.Response]:
            return list(
                await asyncio.gather(
                    *(send_update(nodes_app.client, site.certificate, document) for _ in range(5))
                )
            )

        responses = hb.run(run())
    finally:
        barrier.remove()
    assert [response.status_code for response in responses] == [200] * 5, [
        response.text for response in responses
    ]
    receipts = [response.json() for response in responses]
    assert Counter(receipt["status"] for receipt in receipts) == {
        "accepted": 1,
        "accepted_duplicate": 4,
    }
    assert len({(r["platform_record_id"], r["received_at"]) for r in receipts}) == 1
    found = _results(hb, site)
    assert len(found["records"]) == 1 and len(found["events"]) == 1 and len(found["rows"]) == 1
    (record,) = found["records"]
    assert str(record["record_id"]) == receipts[0]["platform_record_id"]
    assert record["content"]["reported_at"] == receipts[0]["received_at"]
    hb.run(verify_ledger_chains(hb.authz.sessions.migrated, site.organization_id))


def test_the_same_id_with_another_result_or_version_is_idempotency_conflict(
    hb: HeartbeatStack, nodes_app: NodeApp
) -> None:
    site = hb.site()
    assert hb.post(site).status_code == 200
    document = _document(hb, site)
    accepted = _post(hb, nodes_app, site, document)
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    hb.tick()
    again = _post(hb, nodes_app, site, document)
    assert again.json() == {**accepted.json(), "status": "accepted_duplicate"}
    for change in ({"outcome": "failed"}, {"target_version": "1.0.5"}):
        conflicting = {**document, **change}
        if "outcome" in change:
            conflicting["detail_code"] = "download_hash_mismatch"
        response = _post(hb, nodes_app, site, conflicting)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "idempotency_conflict"
    # Otro detalle del nodo con el mismo contenido de la plataforma: duplicado, no conflicto.
    detail = {**document, "detail_code": "health_check_passed_late"}
    assert _post(hb, nodes_app, site, detail).json()["status"] == "accepted_duplicate"
    assert _counts(hb, site) == (1, 1, 1)
    (row,) = hb.fetch(
        "SELECT last_update_result FROM fleet.node_inventory WHERE node_id = $1", site.node_id
    )
    assert row["last_update_result"] == "applied"


def test_two_simultaneous_results_with_the_same_id_and_different_content_leave_one(
    hb: HeartbeatStack, nodes_app: NodeApp
) -> None:
    site = hb.site()
    assert hb.post(site).status_code == 200
    applied = _document(hb, site)
    failed = {**applied, "outcome": "failed", "detail_code": "download_hash_mismatch"}
    barrier = Barrier(nodes_app, 2)
    try:

        async def run() -> list[httpx.Response]:
            return list(
                await asyncio.gather(
                    send_update(nodes_app.client, site.certificate, applied),
                    send_update(nodes_app.client, site.certificate, failed),
                )
            )

        responses = hb.run(run())
    finally:
        barrier.remove()
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert _counts(hb, site) == (1, 1, 1)


def test_another_organizations_identifier_is_a_conflict_that_names_nobody(
    hb: HeartbeatStack, nodes_app: NodeApp
) -> None:
    first, second = hb.site(), hb.site()
    shared_id = hb.uuid7()
    assert _post(hb, nodes_app, first, _document(hb, first, update_result_id=shared_id)).json()[
        "status"
    ] == ("accepted")
    response = _post(hb, nodes_app, second, _document(hb, second, update_result_id=shared_id))
    assert response.status_code == 409 and response.json()["code"] == "idempotency_conflict"
    assert str(first.organization_id) not in response.text
    assert _counts(hb, second) == (0, 0, 0)
    assert _counts(hb, first) == (1, 1, 1)


@pytest.mark.parametrize("name", ["organization_id", "plant_id", "node_id"])
def test_nfr_gob_30_a_result_of_another_certificate_is_node_zone_mismatch(
    hb: HeartbeatStack, nodes_app: NodeApp, name: str
) -> None:
    site, other = hb.site(), hb.site()
    document = _document(hb, site)
    document[name] = str(getattr(other, name))
    response = _post(hb, nodes_app, site, document)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "node_zone_mismatch"
    assert response.json()["field"] == name
    assert _counts(hb, site) == (0, 0, 0)
    assert _counts(hb, other) == (0, 0, 0)


def test_what_the_schema_does_not_express_is_schema_invalid_and_writes_nothing(
    hb: HeartbeatStack, nodes_app: NodeApp
) -> None:
    site = hb.site()
    document = _document(hb, site)
    other_key = _post(hb, nodes_app, site, document, key=str(hb.uuid7()))
    assert (other_key.status_code, other_key.json()["code"]) == (400, "schema_invalid")
    assert other_key.json()["field"] == "Idempotency-Key"
    no_key = _post(hb, nodes_app, site, document, key="")
    assert no_key.json()["code"] == "schema_invalid"
    newer_body = {**document, "contract_version": "1.0.1"}
    version = _post(hb, nodes_app, site, newer_body)
    assert (version.status_code, version.json()["field"]) == (422, "contract_version")
    for target in ("1.0.4-RC1", "999.999.999-rc." + "a" * 49):
        mixed = _document(hb, site, target_version=target)
        response = _post(hb, nodes_app, site, mixed)
        assert (response.status_code, response.json()["code"], response.json()["field"]) == (
            422,
            "schema_invalid",
            "target_version",
        )
    not_preserved = {**_document(hb, site), "queue_preserved": False}
    assert _post(hb, nodes_app, site, not_preserved).json()["code"] == "schema_invalid"
    assert _counts(hb, site) == (0, 0, 0)


def test_a_result_of_a_node_without_inventory_row_is_written_without_projection(
    hb: HeartbeatStack, nodes_app: NodeApp
) -> None:
    site = hb.site()
    response = _post(hb, nodes_app, site, _document(hb, site, outcome="reverted"))
    assert response.status_code == 200, response.text
    assert _counts(hb, site) == (1, 1, 1)
    assert hb.fetch("SELECT 1 FROM fleet.node_inventory WHERE node_id = $1", site.node_id) == []


def test_the_last_result_is_the_one_received_last(hb: HeartbeatStack, nodes_app: NodeApp) -> None:
    site = hb.site()
    assert hb.post(site).status_code == 200
    for outcome in ("failed", "reverted", "applied"):
        hb.tick()
        assert _post(hb, nodes_app, site, _document(hb, site, outcome=outcome)).status_code == 200
        (row,) = hb.fetch(
            "SELECT last_update_result FROM fleet.node_inventory WHERE node_id = $1",
            site.node_id,
        )
        assert row["last_update_result"] == outcome


# --- Orden de candados entre operaciones --------------------------------------------------------


def _extra_nodes(hb: HeartbeatStack, site: NodeSite, count: int) -> list[uuid.UUID]:
    """Nodos más de la planta de ``site``, cada uno con su fila de inventario."""
    nodes: list[uuid.UUID] = []
    now = hb.now()
    for _ in range(count):
        node = uuid.uuid4()
        hb.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
            " status, created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node,
            site.organization_id,
            site.plant_id,
            f"ND-{secrets.token_hex(6).upper()}",
            now - dt.timedelta(days=3),
        )
        inventory_row(
            hb.authz,
            organization_id=site.organization_id,
            plant_id=site.plant_id,
            node_id=node,
            at=now,
        )
        nodes.append(node)
    return nodes


def _holding(service: TargetVersionService, entered: asyncio.Event) -> None:
    """La publicación retiene sus candados del inventario ``HOLD_SECONDS`` tras tomarlos."""
    store: Any = service._store
    original = store.lock_inventory

    async def lock_inventory(*args: Any, **kwargs: Any) -> Any:
        locked = await original(*args, **kwargs)
        entered.set()
        await asyncio.sleep(HOLD_SECONDS)
        return locked

    store.lock_inventory = lock_inventory


def test_lock_order_a_publication_and_a_result_of_the_same_node_both_finish(
    hb: HeartbeatStack, nodes_app: NodeApp
) -> None:
    site = hb.site()
    assert hb.post(site).status_code == 200
    context = installer_context(hb.authz, site.organization_id)
    for round_ in range(ROUNDS):
        service = publisher(hb.authz, hb.primary.database, hb.writer)
        entered = asyncio.Event()
        _holding(service, entered)
        version = f"1.0.{10 + round_}"
        document = _document(hb, site, target_version=version, outcome="reverted")

        async def run(service: Any = service, entered: Any = entered, doc: Any = document) -> Any:
            publication = asyncio.ensure_future(
                _publish_in(hb, site, [site.node_id], doc["target_version"], service=service,
                            context=context)
            )  # fmt: skip
            await asyncio.wait_for(entered.wait(), 60.0)
            result = await send_update(nodes_app.client, site.certificate, doc)
            return await publication, result

        publication, result = hb.run(run())
        assert result.status_code == 200, result.text
        assert publication.target_version == version
        (row,) = hb.fetch(
            "SELECT target_version, last_update_result FROM fleet.node_inventory"
            " WHERE node_id = $1",
            site.node_id,
        )
        assert (row["target_version"], row["last_update_result"]) == (version, "reverted")
    hb.run(verify_ledger_chains(hb.authz.sessions.migrated, site.organization_id))


def test_lock_order_a_publication_and_a_heartbeat_of_the_same_node_both_finish(
    hb: HeartbeatStack,
) -> None:
    site = hb.site()
    assert hb.post(site).status_code == 200
    context = installer_context(hb.authz, site.organization_id)
    for round_ in range(ROUNDS):
        service = publisher(hb.authz, hb.primary.database, hb.writer)
        entered = asyncio.Event()
        _holding(service, entered)
        version = f"1.0.{20 + round_}"
        hb.tick()

        async def run(service: Any = service, entered: Any = entered, v: str = version) -> Any:
            publication = asyncio.ensure_future(
                _publish_in(hb, site, [site.node_id], v, service=service, context=context)
            )
            await asyncio.wait_for(entered.wait(), 60.0)
            heartbeat = await hb.send(site, hb.body(site))
            return await publication, heartbeat

        _, heartbeat = hb.run(run())
        assert heartbeat.status_code == 200, heartbeat.text
        # El latido esperó a la publicación: ya la ve.
        assert heartbeat.json()["target_software_version"] == version


def test_two_publications_with_the_nodes_in_opposite_order_both_finish(hb: HeartbeatStack) -> None:
    site = hb.site()
    nodes = _extra_nodes(hb, site, 4)
    context = installer_context(hb.authz, site.organization_id)
    for round_ in range(ROUNDS):
        hb.tick()

        async def run(r: int = round_) -> list[Any]:
            return list(
                await asyncio.gather(
                    _publish_in(hb, site, nodes, f"1.0.{30 + r}", context=context),
                    _publish_in(hb, site, nodes[::-1], f"1.0.{40 + r}", context=context),
                )
            )

        first, second = hb.run(run())
        # Las dos terminan; el inventario de cada nodo lleva la versión de la publicación más
        # reciente por (published_at, publication_id), la misma que lee el latido, sea cual sea el
        # orden en que confirmaron (con el reloj simulado quieto, decide publication_id).
        winner = max((first, second), key=lambda p: (p.published_at, p.publication_id))
        rows = hb.fetch(
            "SELECT DISTINCT target_version FROM fleet.node_inventory"
            " WHERE node_id = ANY($1::uuid[])",
            nodes,
        )
        assert [row["target_version"] for row in rows] == [winner.target_version]


def test_json_payloads_of_the_published_events_have_only_identifiers(hb: HeartbeatStack) -> None:
    site = hb.site()
    hb.run(_publish_in(hb, site, [site.node_id]))
    rows = hb.fetch(
        "SELECT payload::text AS payload FROM shared.outbox_event"
        " WHERE event_name = 'target_version_published' AND organization_id = $1",
        site.organization_id,
    )
    assert [json.loads(row["payload"]) for row in rows] == [
        {"node_id": str(site.node_id), "target_version": IN_WINDOW}
    ]
