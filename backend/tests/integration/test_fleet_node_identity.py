"""Identidad del nodo de la flota contra PostgreSQL 16 real y por HTTP (TASK-218, LC-GOB-10).

La aplicación real (``create_app`` con la cadena fija, ``ScopeContexts`` y ``ContextAuthorizer``)
sobre la base migrada como ``vigia_app``, con los servicios reales de ``fleet`` y
``HierarchyService`` de U-02 como ``IdentityCommandPort`` (``tests/fleet_http_support.py``):

- **Declaración** (BR-GOB-57, 73): nodo ``declared`` con sus zonas, ``NodeFleetRecord`` y
  ``node_communication_state_changed`` con ``unknown``; si la segunda zona ya tiene nodo, nada
  queda escrito (ni ``node_declared``, ni asignaciones, ni la ficha); ``zone_in_other_plant`` y
  ``code_in_use`` con nombre de la flota; un fallo inyectado a mitad del reemplazo no deja estado
  intermedio.
- **Reemplazo** (BR-GOB-68): las zonas del viejo pasan al nuevo en el mismo instante en que se le
  retiran (``assignment_at`` explica el hueco), el viejo queda ``revoked`` con
  ``decommissioned_at``; el viejo de otra planta o dado de baja es ``replaced_node_not_found``
  (filtro de planta del almacén).
- **Zonas** (BR-GOB-70): añadir y retirar con motivo (``node_zone_unassigned`` v2), nunca borrar.
- **Código de alta** (BR-GOB-58 a 60, NFR-GOB-31): 12 caracteres solo en la 201; en la base solo
  hash y sal; nada del código en el expediente, la auditoría, los eventos, los registros
  estructurados ni las métricas; reemisión ``superseded``; ``node_ca_root_sha256`` con una y con
  dos raíces; re-alta desde credencial vencida, revocada o ``superseded`` y desde ``revoked``;
  ``node_not_declared`` en cualquier otro caso (también tras la baja).
- **Revocación y baja** (BR-GOB-66, 67): una transacción con estado, credenciales, registro,
  evento y la marca global; la petición siguiente del nodo responde ``node_revoked`` (ruta de
  prueba interna de VIG-144); la baja exige ``revoked`` y no borra nada.
- **Intentos** (BR-GOB-61): cada intento deja ``EnrollmentAttempt`` con ``source_ip_hash`` estable
  entre dos instancias; los rechazos, ``enrollment_attempt_rejected``; un nodo desconocido, sin
  fila. ``GET …/enrollment-attempts`` los lista.
- **Guardas de alcance** (BR-GOB-88, G-12): otra organización, otra planta fuera de la concesión
  o de la asignación responden ``not_found``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from vigia_contracts.models.api import parse_rejection_response

from tests.api_support import World
from tests.dispatch_support import metric_points, metrics_with_reader
from tests.fleet_http_support import REASON, FleetStack, fleet_stack, new_code
from tests.fleet_support import root_bundle
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import VERSION, Probe, alb_headers, node_app, node_gate
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.application.enrollment_codes import AttemptRequest
from vigia_platform.fleet.application.node_declaration import NodeDeclarationService
from vigia_platform.fleet.domain.enrollment_attempt import SourceIpHasher
from vigia_platform.fleet.domain.enums import EnrollmentAttemptResult
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.node_api.identity import PostgresNodeContextStore
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.node_ca import fingerprint, read_bundle
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import JsonFormatter
from vigia_platform.shared.observability.metrics import MetricName

pytestmark = pytest.mark.integration

CODE = re.compile(r'"code":\s*"([A-HJ-NP-Z2-9]{12})"')
DAY = timedelta(days=1)


@pytest.fixture(scope="module")
def fleet(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "fleet_node_identity") as stack:
        yield stack


def _code(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


def _json_logs(caplog: pytest.LogCaptureFixture) -> str:
    """Las líneas JSON que el formateador de la plataforma escribiría (con su redacción)."""
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


def _context(fleet: FleetStack, who: Any) -> ScopeContext:
    cookie, concession = who
    scope = fleet.run(fleet.authz.contexts.context_from_session(cookie, concession_id=concession))
    context: ScopeContext = scope.context
    return context


def _zones(site: Any, plant_index: int = 0) -> tuple[uuid.UUID, list[uuid.UUID]]:
    plant = list(site.plants)[plant_index]
    return plant, list(site.plants[plant])


def _assignments(fleet: FleetStack, node: uuid.UUID | str) -> list[Any]:
    return fleet.fetch(
        "SELECT zone_id, assigned_at, unassigned_at FROM identity.zone_node_assignment"
        " WHERE node_id = $1 ORDER BY assigned_at, assignment_id",
        uuid.UUID(str(node)),
    )


def _enroll(
    fleet: FleetStack, node: str, plant: uuid.UUID, zone: uuid.UUID, **issue_args: Any
) -> Any:
    """Simula el alta aceptada (VIG-151): nodo ``enrolled`` con una credencial ``active``."""
    return fleet.enroll(node, plant, zone, **issue_args)


# --- Declaración (BR-GOB-57, 73) -----------------------------------------------------------------


def test_a_declaration_creates_the_node_its_zones_its_record_and_unknown_state(
    fleet: FleetStack,
) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)

    response = fleet.declare(installer, plant, zones[:2])

    assert response.status_code == 201, response.text
    view = response.json()
    node = view["node_id"]
    assert view["status"] == "declared" and view["plant_id"] == str(plant)
    assert [a["zone_id"] for a in view["assignments"]] == [str(z) for z in zones[:2]]
    row = fleet.node_row(node)
    assert row["status"] == "declared" and row["revoked_at"] is None
    assert str(row["declared_by"]) == str(_context(fleet, installer).actor.id)
    assert sorted(str(a["zone_id"]) for a in _assignments(fleet, node)) == sorted(
        str(z) for z in zones[:2]
    )
    states = fleet.records("node_communication_state_changed", site.organization_id)
    assert [(s["content"]["node_id"], s["content"]["state"]) for s in states] == [(node, "unknown")]
    assert states[0]["plant_id"] == plant
    (declared,) = fleet.records("node_declared", site.organization_id)
    assert declared["content"]["node_id"] == node
    assert len(fleet.records("node_zone_assigned", site.organization_id)) == 2
    assert [e["outcome"] for e in fleet.audit(site.organization_id, "node_declared")] == ["success"]


def test_a_declaration_whose_second_zone_is_served_leaves_nothing_written(
    fleet: FleetStack,
) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    assert fleet.declare(installer, plant, [zones[1]]).status_code == 201
    before = {
        name: fleet.records(name, site.organization_id)
        for name in ("node_declared", "node_zone_assigned", "node_communication_state_changed")
    }
    nodes_before = fleet.fetch(
        "SELECT node_id FROM identity.node_identity WHERE organization_id = $1",
        site.organization_id,
    )

    code = new_code()
    response = fleet.send(
        installer,
        "POST",
        f"/plants/{plant}/nodes",
        {"code": code, "zone_ids": [str(zones[0]), str(zones[1])]},
    )

    assert _code(response) == (409, "conflict", "fleet_zone_already_served")
    assert not fleet.fetch("SELECT 1 FROM identity.node_identity WHERE code = $1", code)
    assert (
        fleet.fetch(
            "SELECT node_id FROM identity.node_identity WHERE organization_id = $1",
            site.organization_id,
        )
        == nodes_before
    )
    assert not fleet.fetch(
        "SELECT 1 FROM identity.zone_node_assignment WHERE zone_id = $1", zones[0]
    )
    for name, rows in before.items():
        assert fleet.records(name, site.organization_id) == rows, name
    assert len(fleet.fetch(
        "SELECT 1 FROM fleet.node_fleet_record WHERE organization_id = $1", site.organization_id
    )) == 1  # fmt: skip


def test_zone_of_another_plant_and_repeated_code_have_fleet_names(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    _, other_zones = _zones(site, 1)
    installer = fleet.installer(site)

    other_plant = fleet.declare(installer, plant, [other_zones[0]])
    first = fleet.send(
        installer, "POST", f"/plants/{plant}/nodes", {"code": "ND-REPEAT-1", "zone_ids": []}
    )
    repeated = fleet.send(
        installer, "POST", f"/plants/{plant}/nodes", {"code": "ND-REPEAT-1", "zone_ids": []}
    )
    duplicated_zones = fleet.send(
        installer,
        "POST",
        f"/plants/{plant}/nodes",
        {"code": new_code(), "zone_ids": [str(zones[0]), str(zones[0])]},
    )

    assert _code(other_plant) == (400, "invalid_request", "fleet_zone_in_other_plant")
    assert first.status_code == 201
    assert _code(repeated) == (409, "conflict", "fleet_code_in_use")
    assert _code(duplicated_zones)[:2] == (400, "invalid_request")


class FailingStore(PostgresNodeFleetStore):
    """Falla al dar de baja al viejo: después de declarar, retirar sus zonas y revocarlo."""

    async def mark_decommissioned(self, *args: Any, **kwargs: Any) -> bool:
        raise RuntimeError("fallo inyectado a mitad del reemplazo")


def test_a_failure_in_the_middle_of_a_replacement_leaves_no_intermediate_state(
    fleet: FleetStack,
) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    old = fleet.declare(installer, plant, zones[:2]).json()["node_id"]
    snapshot = (
        _assignments(fleet, old),
        fleet.node_row(old),
        fleet.revocation_state()["dirty_generation"],
        fleet.records("node_revoked", site.organization_id),
        fleet.fetch("SELECT count(*) AS n FROM identity.node_identity"),
    )
    failing = NodeDeclarationService(
        dataclasses.replace(fleet.deps, nodes=FailingStore(fleet.database))
    )
    code = new_code()
    fleet.tick()

    with pytest.raises(RuntimeError, match="fallo inyectado"):
        fleet.run(
            failing.declare(
                _context(fleet, installer),
                plant,
                code=code,
                zone_ids=(),
                replaces_node_id=uuid.UUID(old),
            )
        )

    assert (
        _assignments(fleet, old),
        fleet.node_row(old),
        fleet.revocation_state()["dirty_generation"],
        fleet.records("node_revoked", site.organization_id),
        fleet.fetch("SELECT count(*) AS n FROM identity.node_identity"),
    ) == snapshot
    assert not fleet.fetch("SELECT 1 FROM identity.node_identity WHERE code = $1", code)


# --- Reemplazo (BR-GOB-68) -----------------------------------------------------------------------


def test_a_replacement_moves_the_zones_and_retires_the_old_node(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    old = fleet.declare(installer, plant, zones[:2]).json()["node_id"]
    generation = fleet.revocation_state()["dirty_generation"]

    response = fleet.declare(installer, plant, [zones[2]], replaces_node_id=old)

    assert response.status_code == 201, response.text
    view = response.json()
    new = view["node_id"]
    assert view["replaces_node_id"] == old
    taken_zones = [a["zone_id"] for a in view["assignments"]]
    assert sorted(taken_zones[:2]) == sorted(str(z) for z in zones[:2])
    assert taken_zones[2:] == [str(zones[2])]
    old_row = fleet.node_row(old)
    assert old_row["status"] == "revoked"
    assert old_row["revoked_at"] is not None and old_row["decommissioned_at"] is not None
    assert str(fleet.node_row(new)["replaces_node_id"]) == old
    retired = _assignments(fleet, old)
    taken = {str(a["zone_id"]): a["assigned_at"] for a in _assignments(fleet, new)}
    assert all(a["unassigned_at"] is not None for a in retired)
    # El hueco: la asignación nueva empieza en el instante en que termina la vieja.
    for assignment in retired:
        assert taken[str(assignment["zone_id"])] == assignment["unassigned_at"]
    assert fleet.revocation_state()["dirty_generation"] == generation + 1
    (revoked,) = [
        r
        for r in fleet.records("node_revoked", site.organization_id)
        if r["content"]["node_id"] == old
    ]
    (decommissioned,) = [
        r
        for r in fleet.records("node_decommissioned", site.organization_id)
        if r["content"]["node_id"] == old
    ]
    assert revoked["plant_id"] == decommissioned["plant_id"] == plant
    events = [e for e in fleet.events("node_revoked", site.organization_id) if e["node_id"] == old]
    # Las zonas que servía el viejo, aunque ya se le retiraron en la misma transacción.
    assert len(events) == 1 and sorted(events[0]["zone_ids"]) == sorted(str(z) for z in zones[:2])
    assert events[0]["node_id"] == old and events[0].get("replaces_node_id") is None
    unassigned = fleet.records("node_zone_unassigned", site.organization_id)
    assert {r["schema_version"] for r in unassigned} == {2}
    assert all(r["content"]["reason_es"] for r in unassigned)


@pytest.mark.parametrize("case", ["other_plant", "decommissioned", "missing", "other_org"])
def test_the_replaced_node_must_be_live_and_of_the_route_plant(
    fleet: FleetStack, case: str
) -> None:
    site = fleet.site()
    plant, _ = _zones(site)
    other_plant, other_zones = _zones(site, 1)
    installer = fleet.installer(site)
    if case == "other_plant":
        old = fleet.declare(installer, other_plant, other_zones[:1]).json()["node_id"]
    elif case == "decommissioned":
        old = fleet.declare(installer, plant, []).json()["node_id"]
        assert fleet.revoke(installer, old).status_code == 200
        assert fleet.decommission(installer, old).status_code == 200
    elif case == "other_org":
        other = fleet.site()
        old = fleet.declare(fleet.installer(other), _zones(other)[0], []).json()["node_id"]
    else:
        old = str(uuid.uuid4())
    before = fleet.node_row(old), _assignments(fleet, old)

    response = fleet.declare(installer, plant, [], replaces_node_id=old)

    assert _code(response) == (400, "invalid_request", "fleet_replaced_node_not_found")
    assert (fleet.node_row(old), _assignments(fleet, old)) == before


# --- Zonas (BR-GOB-70) ---------------------------------------------------------------------------


def test_zones_are_added_and_retired_with_reason_never_deleted(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    _, other_zones = _zones(site, 1)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]

    added = fleet.send(installer, "POST", f"/nodes/{node}/zones", {"zone_id": str(zones[1])})
    foreign = fleet.send(
        installer, "POST", f"/nodes/{node}/zones", {"zone_id": str(other_zones[0])}
    )
    served = fleet.send(installer, "POST", f"/nodes/{node}/zones", {"zone_id": str(zones[1])})
    retired = fleet.send(
        installer,
        "POST",
        f"/nodes/{node}/zones/{zones[1]}/unassignment",
        {"reason_es": "Cambio de distribución de la línea"},
    )
    not_mine = fleet.send(
        installer,
        "POST",
        f"/nodes/{node}/zones/{zones[2]}/unassignment",
        {"reason_es": "Cambio de distribución de la línea"},
    )
    rejected = fleet.send(
        installer,
        "POST",
        f"/nodes/{node}/zones/{zones[0]}/unassignment",
        {"reason_es": "<b>marcado</b> no admitido"},
    )

    assert added.status_code == 201 and added.json()["zone_id"] == str(zones[1])
    assert _code(foreign) == (400, "invalid_request", "fleet_zone_in_other_plant")
    assert _code(served) == (409, "conflict", "fleet_zone_already_served")
    assert retired.status_code == 200 and retired.json()["unassigned_at"] is not None
    assert _code(not_mine)[:2] == (409, "zone_without_node")
    assert _code(rejected) == (400, "invalid_request", "fleet_free_text_rejected")
    rows = _assignments(fleet, node)
    assert [(str(r["zone_id"]), r["unassigned_at"] is None) for r in rows] == [
        (str(zones[0]), True),
        (str(zones[1]), False),
    ]
    (record,) = [
        r
        for r in fleet.records("node_zone_unassigned", site.organization_id)
        if r["content"]["node_id"] == node
    ]
    assert record["schema_version"] == 2
    assert record["content"]["reason_es"] == "Cambio de distribución de la línea"


def test_a_decommissioned_node_receives_no_zone(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, []).json()["node_id"]
    assert fleet.revoke(installer, node).status_code == 200
    assert fleet.decommission(installer, node).status_code == 200

    response = fleet.send(installer, "POST", f"/nodes/{node}/zones", {"zone_id": str(zones[0])})

    assert _code(response) == (409, "conflict", "fleet_node_not_declared")


# --- Código de alta (BR-GOB-58 a 60, NFR-GOB-31) -----------------------------------------------


def test_the_plain_code_only_exists_in_the_201(
    fleet: FleetStack, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    site = fleet.site()
    plant, _ = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, []).json()["node_id"]
    caplog.set_level(logging.DEBUG)

    first = fleet.issue(installer, node)
    second = fleet.issue(installer, node)

    for response in (first, second):
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
    codes = [response.json()["code"] for response in (first, second)]
    assert all(re.fullmatch(r"[A-HJ-NP-Z2-9]{12}", code) for code in codes)
    view = second.json()
    rows = fleet.codes(node)
    assert [row["status"] for row in rows] == ["superseded", "active"]
    for row, code in zip(rows, codes, strict=True):
        assert len(row["code_salt"]) == 16
        assert (
            row["code_hash"] == hashlib.sha256(bytes(row["code_salt"]) + code.encode()).hexdigest()
        )
        assert row["expires_at"] - row["issued_at"] == timedelta(hours=24)
        assert row["disclosed_at"] == row["issued_at"]
    assert view["expires_at"].endswith("Z") and view["disclosed_at"].endswith("Z")
    # No hay ruta que vuelva a mostrarlo y nada de lo guardado lo contiene.
    organization = site.organization_id
    dumps = [
        json.dumps([dict(r) for r in fleet.fetch(f"SELECT * FROM {table}")], default=str)  # noqa: S608
        for table in (
            "fleet.enrollment_code",
            "fleet.node_fleet_record",
            "fleet.enrollment_attempt",
            "shared.audit_entry",
            "shared.outbox_event",
        )
    ]
    dumps.append(
        json.dumps(
            [
                dict(r)
                for r in fleet.fetch(
                    "SELECT record_type, ledger.vigia_bytes_to_jsonb(content)::text AS content,"
                    " source_key FROM ledger.ledger_record WHERE organization_id = $1",
                    organization,
                )
            ],
            default=str,
        )
    )
    issued = fleet.records("enrollment_code_issued", organization)
    assert [r["source_key"] for r in issued] == [str(row["code_id"]) for row in rows]
    assert set(issued[0]["content"]) == {
        "code_id", "node_id", "issued_at", "expires_at", "disclosed_at", "issued_by"
    }  # fmt: skip
    logs = _json_logs(caplog) + capsys.readouterr().out
    cleaned = redaction.DEFAULT_POLICY.clean({"result": codes[0], "reason": codes[0]})
    for code in codes:
        assert all(code not in dump for dump in dumps)
        assert code not in logs
        assert code not in json.dumps(cleaned)  # una métrica nunca lo admite como atributo
    assert [e["outcome"] for e in fleet.audit(organization, "enrollment_code_issued")] == [
        "success",
        "success",
    ]


def test_the_plain_code_never_reaches_the_exported_metrics(fleet: FleetStack) -> None:
    # Revisión de VIG-147 (ronda 1, menor 1): lo que la ejecución exporta, no solo la política.
    metrics, reader = metrics_with_reader()
    service = fleet.codes_service(metrics=metrics)
    site = fleet.site()
    plant, _ = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, []).json()["node_id"]
    issued = fleet.run(service.issue(fleet.context(installer), uuid.UUID(node)))
    scope = fleet.run(
        fleet.authz.contexts.context_from_node_enrollment(
            PostgresNodeContextStore(fleet.database), uuid.UUID(node)
        )
    )
    for result in (
        EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID,
        EnrollmentAttemptResult.ACCEPTED,
    ):
        fleet.tick()
        fleet.run(service.register_attempt(scope, _request(issued.code), result))
    fleet.run(
        service.register_attempt(
            None, _request(issued.code), EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
        )
    )

    exported = reader.get_metrics_data()
    assert exported is not None
    points = metric_points(reader, MetricName.ENROLLMENT_ATTEMPTS_TOTAL)
    assert {(p[0]["result"], p[0]["reason"]) for p in points} == {
        ("enrollment_code_invalid", "node_declared"),
        ("accepted", "node_declared"),
        ("enrollment_code_invalid", "node_unknown"),
    }
    assert issued.code not in exported.to_json()


def test_the_root_fingerprints_follow_the_published_bundle(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, _ = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, []).json()["node_id"]
    try:
        for roots in (1, 2):
            fleet.roots.body = root_bundle(roots)
            expected = [fingerprint(root) for root in read_bundle(fleet.roots.body)]
            response = fleet.issue(installer, node)
            assert response.status_code == 201, response.text
            assert response.json()["node_ca_root_sha256"] == expected
            assert len(expected) == roots
        fleet.roots.body = None
        before = fleet.codes(node)
        down = fleet.issue(installer, node)
        assert _code(down)[:2] == (503, "storage_unavailable")
        assert fleet.codes(node) == before
    finally:
        fleet.roots.body = root_bundle(1)


@pytest.mark.parametrize("credential", ["expired", "revoked", "superseded"])
def test_re_enrollment_from_a_dead_credential(fleet: FleetStack, credential: str) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    now = fleet.authz.now()
    if credential == "expired":
        _, credential_id = _enroll(
            fleet, node, plant, zones[0], expires_at=now - timedelta(seconds=1)
        )
    else:
        _, credential_id = _enroll(fleet, node, plant, zones[0])
        fleet.execute(
            "UPDATE fleet.node_credential SET status = $2,"
            " revoked_at = CASE WHEN $2 = 'revoked' THEN $3::timestamptz END"
            " WHERE credential_id = $1",
            credential_id,
            credential,
            now,
        )
    generation = fleet.revocation_state()["dirty_generation"]

    response = fleet.issue(installer, node)

    assert response.status_code == 201, response.text
    assert fleet.node_row(node)["status"] == "re_enrollment_pending"
    (row,) = fleet.fetch(
        "SELECT status, revoked_at FROM fleet.node_credential WHERE credential_id = $1",
        credential_id,
    )
    expected = "superseded" if credential == "superseded" else "revoked"
    assert row["status"] == expected
    # Una credencial que seguía active (vencida) se revoca con la marca de la lista.
    marked = 1 if credential == "expired" else 0
    assert fleet.revocation_state()["dirty_generation"] == generation + marked
    # Reemitir mientras está pendiente es libre.
    assert fleet.issue(installer, node).status_code == 201
    assert [r["status"] for r in fleet.codes(node)] == ["superseded", "active"]


def test_re_enrollment_from_a_deliberate_revocation(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    _enroll(fleet, node, plant, zones[0])
    assert fleet.revoke(installer, node).status_code == 200

    response = fleet.issue(installer, node)

    assert response.status_code == 201, response.text
    row = fleet.node_row(node)
    assert row["status"] == "re_enrollment_pending"
    assert row["revoked_at"] is None and row["revocation_reason_es"] is None
    # La revocación sigue en el expediente.
    assert any(
        r["content"]["node_id"] == node for r in fleet.records("node_revoked", site.organization_id)
    )


@pytest.mark.parametrize("state", ["live_credential", "decommissioned"])
def test_any_other_state_answers_node_not_declared(fleet: FleetStack, state: str) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    if state == "live_credential":
        _enroll(fleet, node, plant, zones[0])
    else:
        assert fleet.revoke(installer, node).status_code == 200
        assert fleet.decommission(installer, node).status_code == 200
    before = (fleet.codes(node), fleet.node_row(node))

    response = fleet.issue(installer, node)

    assert _code(response) == (409, "conflict", "fleet_node_not_declared")
    assert (fleet.codes(node), fleet.node_row(node)) == before


# --- Revocación y baja (BR-GOB-66, 67) ------------------------------------------------------------


def test_revocation_is_one_transaction_and_the_next_node_request_is_rejected(
    fleet: FleetStack,
) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    certificate, credential = _enroll(fleet, node, plant, zones[0])
    gate = node_gate(
        contexts=fleet.authz.contexts,
        store=PostgresNodeContextStore(fleet.database),
        clock=fleet.authz.sessions.clock,
        probe=Probe(),
    )
    app = node_app(World(clock=fleet.authz.sessions.clock), gate)
    headers = {**alb_headers(certificate), "X-Vigia-Contract-Version": VERSION}

    def node_request() -> httpx.Response:
        async def call() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://nodes.vigia.test",
                timeout=60.0,
            ) as client:
                return await client.get(f"/api/nodes/zones/{zones[0]}/catalog", headers=headers)

        response: httpx.Response = fleet.run(call())
        return response

    assert node_request().status_code == 200
    generation = fleet.revocation_state()["dirty_generation"]

    response = fleet.revoke(installer, node, "Equipo comprometido en la planta")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "revoked"
    row = fleet.node_row(node)
    assert row["status"] == "revoked"
    assert row["revocation_reason_es"] == "Equipo comprometido en la planta"
    (credential_row,) = fleet.fetch(
        "SELECT status, revoked_at FROM fleet.node_credential WHERE credential_id = $1", credential
    )
    assert (
        credential_row["status"] == "revoked" and credential_row["revoked_at"] == row["revoked_at"]
    )
    (record,) = [
        r
        for r in fleet.records("node_revoked", site.organization_id)
        if r["content"]["node_id"] == node
    ]
    assert record["content"]["reason_es"] == "Equipo comprometido en la planta"
    assert record["content"]["revoked_by"] == str(_context(fleet, installer).actor.id)
    (event,) = [
        e for e in fleet.events("node_revoked", site.organization_id) if e["node_id"] == node
    ]
    assert {k: v for k, v in event.items() if v is not None} == {
        "node_id": node,
        "plant_id": str(plant),
        "zone_ids": [str(zones[0])],
    }
    assert "Equipo comprometido" not in json.dumps(
        fleet.events("node_revoked", site.organization_id)
    )
    state = fleet.revocation_state()
    assert state["dirty_generation"] == generation + 1 and state["dirty_since"] is not None
    assert [e["outcome"] for e in fleet.audit(site.organization_id, "node_revoked")] == ["success"]
    rejected = node_request()
    assert rejected.status_code == 401
    assert parse_rejection_response(rejected.content).code.value == "node_revoked"
    # Revocar otra vez no escribe otro registro (P4).
    assert fleet.revoke(installer, node).status_code == 200
    assert len([
        r
        for r in fleet.records("node_revoked", site.organization_id)
        if r["content"]["node_id"] == node
    ]) == 1  # fmt: skip
    assert fleet.revocation_state()["dirty_generation"] == generation + 1


def test_decommission_requires_revocation_and_keeps_everything(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    assert fleet.issue(installer, node).status_code == 201

    early = fleet.decommission(installer, node)
    assert _code(early) == (409, "conflict", "fleet_node_not_revoked")
    assert fleet.node_row(node)["decommissioned_at"] is None
    assert fleet.revoke(installer, node).status_code == 200
    ledger_before = fleet.fetch(
        "SELECT record_id FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type <> 'provider_query' ORDER BY record_id",
        site.organization_id,
    )

    response = fleet.decommission(installer, node, "Fin de vida útil del equipo")
    again = fleet.decommission(installer, node)

    assert response.status_code == 200 and again.status_code == 200
    assert response.json()["decommissioned_at"] == again.json()["decommissioned_at"]
    ledger_after = fleet.fetch(
        "SELECT record_id FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type <> 'provider_query' ORDER BY record_id",
        site.organization_id,
    )
    assert set(ledger_before) <= set(ledger_after) and len(ledger_after) == len(ledger_before) + 1
    (record,) = [
        r
        for r in fleet.records("node_decommissioned", site.organization_id)
        if r["content"]["node_id"] == node
    ]
    assert record["source_key"] == node
    assert record["content"]["reason_es"] == "Fin de vida útil del equipo"
    assert [e["node_id"] for e in fleet.events("node_decommissioned", site.organization_id)] == [
        node
    ]
    # Nada se borra: la ficha, las asignaciones y los códigos siguen ahí.
    assert fleet.node_row(node)["decommissioned_at"] is not None
    assert _assignments(fleet, node) and fleet.codes(node)


def _verify(fleet: FleetStack, node: str, code: str) -> str:
    """Lo que respondería el alta (VIG-151) a ``code`` para ``node``."""
    service = fleet.services.enrollment_codes
    assert service is not None
    scope = fleet.run(
        fleet.authz.contexts.context_from_node_enrollment(
            PostgresNodeContextStore(fleet.database), uuid.UUID(node)
        )
    )
    return str(fleet.run(service.verify(scope, code)).result.value)


@pytest.mark.parametrize("ending", ["revocation", "decommission"])
def test_revocation_and_decommission_leave_no_usable_enrollment_code(
    fleet: FleetStack, ending: str
) -> None:
    # Revisión de VIG-147 (ronda 1): un código emitido con el nodo declared no puede sobrevivir a
    # la revocación ni a la baja (BR-GOB-66, 67; BLM §3.4).
    site = fleet.site()
    plant, _ = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, []).json()["node_id"]
    code = fleet.issue(installer, node).json()["code"]
    assert _verify(fleet, node, code) == "accepted"

    assert fleet.revoke(installer, node).status_code == 200
    if ending == "decommission":
        assert fleet.decommission(installer, node).status_code == 200

    assert [row["status"] for row in fleet.codes(node)] == ["superseded"]
    assert _verify(fleet, node, code) == "enrollment_code_invalid"


def test_decommission_supersedes_a_code_left_active_on_a_revoked_node(fleet: FleetStack) -> None:
    # Revisión de VIG-147 (ronda 2, menor 2): un nodo revocado antes de que la revocación
    # invalidara su código (datos anteriores al cambio) llega a la baja con un código active; la
    # baja tampoco lo deja vivo.
    site = fleet.site()
    plant, _ = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, []).json()["node_id"]
    assert fleet.issue(installer, node).status_code == 201
    assert fleet.revoke(installer, node).status_code == 200
    # El registro no vuelve atrás (superseded → active, P4): se anexa un active con los datos del
    # anterior, como lo habría dejado una revocación sin este cambio.
    fleet.execute(
        "INSERT INTO fleet.enrollment_code (code_id, organization_id, plant_id, node_id, code_hash,"
        " code_salt, issued_at, issued_by, expires_at, disclosed_at, status, ledger_record_id)"
        " SELECT $2, organization_id, plant_id, node_id, code_hash, code_salt, issued_at,"
        " issued_by, expires_at, disclosed_at, 'active', ledger_record_id"
        " FROM fleet.enrollment_code WHERE node_id = $1",
        uuid.UUID(node),
        uuid.uuid4(),
    )
    assert sorted(row["status"] for row in fleet.codes(node)) == ["active", "superseded"]

    assert fleet.decommission(installer, node).status_code == 200

    assert [row["status"] for row in fleet.codes(node)] == ["superseded", "superseded"]


def test_a_replaced_node_keeps_no_usable_enrollment_code(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    old = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    code = fleet.issue(installer, old).json()["code"]

    assert fleet.declare(installer, plant, [], replaces_node_id=old).status_code == 201

    assert [row["status"] for row in fleet.codes(old)] == ["superseded"]
    assert _verify(fleet, old, code) == "enrollment_code_invalid"


def test_verify_refuses_a_live_code_of_a_node_that_cannot_enroll(fleet: FleetStack) -> None:
    # La guarda de ``verify`` por sí sola: el código sigue active, pero el nodo ya está enrolled.
    site = fleet.site()
    plant, zones = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    code = fleet.issue(installer, node).json()["code"]

    fleet.enroll(node, plant, zones[0])

    assert [row["status"] for row in fleet.codes(node)] == ["active"]
    assert _verify(fleet, node, code) == "enrollment_code_invalid"


# --- Intentos (BR-GOB-61) -------------------------------------------------------------------------


def _request(code: str, address: str = "198.51.100.23") -> AttemptRequest:
    return AttemptRequest(
        presented_code=code,
        hardware_fingerprint="cd" * 32,
        software_version="1.4.0",
        contract_version="1.0.0",
        source_address=address,
        correlation_id=uuid.uuid4(),
    )


def test_every_attempt_is_registered_and_rejections_go_to_the_ledger(
    fleet: FleetStack, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    site = fleet.site()
    plant, _ = _zones(site)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, []).json()["node_id"]
    code = fleet.issue(installer, node).json()["code"]
    store = PostgresNodeContextStore(fleet.database)
    scope = fleet.run(fleet.authz.contexts.context_from_node_enrollment(store, uuid.UUID(node)))
    first, second = fleet.services.enrollment_codes, fleet.codes_service()
    assert first is not None
    caplog.set_level(logging.DEBUG)

    check = fleet.run(first.verify(scope, code))
    wrong = fleet.run(first.verify(scope, "Z" * 11 + "2"))
    accepted = fleet.run(first.register_attempt(scope, _request(code), check.result))
    fleet.tick()
    rejected = fleet.run(second.register_attempt(scope, _request("Z" * 11 + "2"), wrong.result))
    unknown_scope = fleet.run(
        fleet.authz.contexts.context_from_node_enrollment(store, uuid.uuid4())
    )
    unknown = fleet.run(
        first.register_attempt(
            unknown_scope, _request("Z" * 11 + "3"), EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
        )
    )

    assert check.valid and wrong.result is EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
    assert unknown_scope is None and unknown is None
    assert accepted is not None and rejected is not None
    # El mismo origen, el mismo hash en dos instancias; nunca la dirección en claro.
    assert accepted.source_ip_hash == rejected.source_ip_hash
    assert accepted.source_ip_hash == SourceIpHasher(fleet.hash_key).hash("198.51.100.23")
    rows = fleet.fetch(
        "SELECT * FROM fleet.enrollment_attempt WHERE node_id = $1"
        " ORDER BY attempted_at, attempt_id",
        uuid.UUID(node),
    )
    assert [r["result"] for r in rows] == ["accepted", "enrollment_code_invalid"]
    assert rows[0]["ledger_record_id"] is None and rows[1]["ledger_record_id"] is not None
    assert rows[0]["presented_code_hash"] == hashlib.sha256(code.encode()).hexdigest()
    dump = json.dumps([dict(r) for r in rows], default=str)
    assert "198.51.100.23" not in dump and code not in dump
    (record,) = fleet.records("enrollment_attempt_rejected", site.organization_id)
    assert record["record_id"] == rows[1]["ledger_record_id"] and record["plant_id"] == plant
    assert record["source_key"] == str(rows[1]["attempt_id"])
    assert record["content"]["source_ip_hash"] == accepted.source_ip_hash
    assert record["content"]["hardware_fingerprint"] == "cd" * 32
    # El de un nodo desconocido: sin fila, con métrica y registro estructurado con el hash.
    logs = _json_logs(caplog) + capsys.readouterr().out
    assert "intento de alta de un nodo no declarado" in logs
    full_hash = SourceIpHasher(fleet.hash_key).hash("198.51.100.23")
    assert f'"source_ip_tag": "{full_hash[:16]}"' in logs.replace('":"', '": "')
    assert full_hash not in logs  # nunca un valor de 64 hexadecimales en un registro
    assert "198.51.100.23" not in logs
    admin = fleet.member(site)
    listed = fleet.send(admin, "GET", f"/nodes/{node}/enrollment-attempts")
    assert listed.status_code == 200, listed.text
    assert [a["result"] for a in listed.json()["attempts"]] == [
        "enrollment_code_invalid",
        "accepted",
    ]
    assert "source_ip_hash" not in listed.text and "presented_code_hash" not in listed.text
    page = fleet.send(admin, "GET", f"/nodes/{node}/enrollment-attempts", params={"limit": "1"})
    rest = fleet.send(
        admin,
        "GET",
        f"/nodes/{node}/enrollment-attempts",
        params={"limit": "1", "after": page.json()["next_after"]},
    )
    assert [a["attempt_id"] for a in page.json()["attempts"] + rest.json()["attempts"]] == [
        a["attempt_id"] for a in listed.json()["attempts"]
    ]


# --- Guardas de alcance (BR-GOB-88, NFR-GOB-30, G-12) --------------------------------------------


def test_routes_answer_not_found_outside_the_scope(fleet: FleetStack) -> None:
    site = fleet.site()
    plant, zones = _zones(site)
    other_plant, other_zones = _zones(site, 1)
    installer = fleet.installer(site)
    node = fleet.declare(installer, plant, [zones[0]]).json()["node_id"]
    elsewhere = fleet.declare(installer, other_plant, [other_zones[0]]).json()["node_id"]
    foreign_site = fleet.site()
    outsider = fleet.installer(foreign_site)
    plant_installer = fleet.installer(site, ScopeLevel.PLANT, plant)
    plant_admin = fleet.member(site, Role.ADMINISTRATOR, ScopeLevel.PLANT, plant)

    def calls(target: str, zone: uuid.UUID, at_plant: uuid.UUID) -> list[tuple[str, str, Any]]:
        return [
            ("POST", f"/plants/{at_plant}/nodes", {"code": new_code(), "zone_ids": []}),
            ("POST", f"/nodes/{target}/zones", {"zone_id": str(zone)}),
            ("POST", f"/nodes/{target}/zones/{zone}/unassignment", {"reason_es": REASON}),
            ("POST", f"/nodes/{target}/enrollment-codes", None),
            ("GET", f"/nodes/{target}/enrollment-attempts", None),
            ("POST", f"/nodes/{target}/revocation", {"reason_es": REASON}),
            ("POST", f"/nodes/{target}/decommission", {"reason_es": REASON}),
        ]

    before = (fleet.node_row(node), fleet.node_row(elsewhere), fleet.codes(node))
    failures = []
    # Otra organización: el instalador con concesión sobre otro cliente.
    for method, path, body in calls(node, zones[1], plant):
        response = fleet.send(outsider, method, path, body)
        if _code(response)[:2] != (404, "not_found"):
            failures.append(("otra organización", path, response.text))
    # Otra planta fuera de la concesión de una planta, y fuera de la asignación de una planta.
    for method, path, body in calls(elsewhere, other_zones[1], other_plant):
        response = fleet.send(plant_installer, method, path, body)
        if _code(response)[:2] != (404, "not_found"):
            failures.append(("otra planta (concesión)", path, response.text))
    listed = fleet.send(plant_admin, "GET", f"/nodes/{elsewhere}/enrollment-attempts")
    if _code(listed)[:2] != (404, "not_found"):
        failures.append(("otra planta (asignación)", "attempts", listed.text))
    assert not failures, failures
    assert (fleet.node_row(node), fleet.node_row(elsewhere), fleet.codes(node)) == before
    # Y dentro del alcance, sí.
    assert fleet.send(plant_admin, "GET", f"/nodes/{node}/enrollment-attempts").status_code == 200
    assert fleet.issue(plant_installer, node).status_code == 201


def test_the_service_refuses_a_node_outside_the_session_plant(fleet: FleetStack) -> None:
    site = fleet.site()
    _, zones = _zones(site)
    other_plant, other_zones = _zones(site, 1)
    installer = fleet.installer(site)
    elsewhere = fleet.declare(installer, other_plant, [other_zones[0]]).json()["node_id"]
    plant_admin = fleet.member(site, Role.ADMINISTRATOR, ScopeLevel.PLANT, next(iter(site.plants)))
    service = fleet.services.enrollment_codes
    assert service is not None

    with pytest.raises(ResourceNotFound):
        fleet.run(service.attempts(_context(fleet, plant_admin), uuid.UUID(elsewhere)))
    assert zones  # la planta propia tiene zonas; la ajena no se ve
