"""``POST heartbeats`` contra PostgreSQL 16 real como ``vigia_app`` (TASK-223; LC-GOB-14).

Sobre ``tests/heartbeat_support.py`` (servicios reales, ``NodeApiGate`` real con la identidad por
certificado contra la base y la aplicación con la cadena fija):

- el primer latido proyecta ``NodeInventory``, ``CameraInventory`` y ``ZoneNodeState`` (con
  ``coverage_ok`` de BR-GOB-97), anexa ``HeartbeatHistory`` y escribe la vuelta a ``reachable``;
  un ``heartbeat_id`` repetido no cambia nada y recibe la misma respuesta byte a byte, con la hora
  del original, en la misma instancia y en otra (BR-CTR-26, BR-GOB-71; VIG-182);
- ``reachable`` solo tras ``unknown`` o ``mute`` (BR-GOB-73); ``last_heartbeat_at`` nunca retrocede;
- la respuesta: sobres de compuertas guardados **byte a byte**, versiones de catálogo,
  ``mute_after_seconds`` cinco veces el intervalo (15, 60 y 600 s), ``target_software_version`` sin
  ventana (D-5), ``contract_notice`` con ``retires_at`` (A-44) y cero llamadas a ``sign`` en 1 000
  latidos (NFR-GOB-10);
- ``live_view_local_url`` a la ficha y a ``identity.node_identity`` (y de vuelta a nulo); los
  accesos locales llegan a U-02 una sola vez aunque el latido se repita (nº 24 y 31);
- un cambio de ``model_version`` marca la regresión de las zonas del nodo una vez; uno solo de
  ``software_version`` no (BR-GOB-51);
- organización, planta o nodo del cuerpo ajenos al certificado: ``node_zone_mismatch`` antes del
  esquema; las zonas ya no asignadas se ignoran (BR-GOB-70);
- la base pausada (al empezar y a mitad de la transacción) responde ``temporarily_unavailable`` sin
  escribir nada; el puerto de firma caído no afecta al latido (FS-GOB-02 y 03);
- A-55: un sobre a menos de 24 h de vencer se renueva una vez; uno con más de 24 h, nunca;
- ``payload_summary`` y las métricas sin texto libre ni etiquetas de zona (NFR-GOB-13, 25).

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base (retro 14).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from vigia_contracts.models import api
from vigia_contracts.signing import KeySet, verify

from tests.heartbeat_support import (
    LIVE_VIEW_URL,
    MODEL_VERSION,
    HeartbeatStack,
    NodeSite,
    heartbeat_stack,
    retiring_policy,
)
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import DAY, VERSION
from tests.writer_support import unit_context
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.signing import NODE_PURPOSES
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

HOUR: Final = dt.timedelta(hours=1)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "fleet_heartbeat") as built:
        yield built


@pytest.fixture(autouse=True)
def _release_instances(stack: HeartbeatStack) -> Iterator[None]:
    # Cada prueba cierra las instancias que crea: sus pools no se acumulan en el módulo.
    yield
    stack.run(stack.release())


def _accepted(stack: HeartbeatStack, site: NodeSite, **changes: Any) -> Any:
    stack.tick()
    response = stack.post(site, stack.body(site, **changes))
    assert response.status_code == 200, response.text
    return response


# --- Proyección, historia y vuelta a reachable ---------------------------------------------------


def test_a_first_heartbeat_projects_the_inventory_and_returns_to_reachable(
    stack: HeartbeatStack,
) -> None:
    site = stack.site(zones=2, cameras=2)
    stack.tick()
    body = stack.body(site)
    received = to_millisecond(stack.now())
    response = stack.post(site, body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    parsed = api.parse_heartbeat_response(response.content)
    assert parsed.revoked is False
    assert response.json()["server_time"] == format_timestamp(received)
    assert parsed.heartbeat_interval_seconds == 60 and parsed.mute_after_seconds == 300
    assert {entry.zone_id for entry in parsed.catalog_versions_available} == {
        str(zone) for zone in site.zones
    }
    inventory = stack.inventory(site.node_id)
    assert inventory is not None
    assert inventory["communication_state"] == "reachable"
    assert inventory["model_version"] == MODEL_VERSION
    assert inventory["contract_notice"] == {"result": "accepted"}
    assert inventory["last_heartbeat_at"] == received
    assert inventory["clock"] == {"synchronized": True, "offset_ms": 12}
    assert inventory["signal_reader"] == {"available": True, "adapter": "modbus_rtu"}
    assert inventory["local_queue"] == body["local_queue"]
    assert [row["camera_id"] for row in stack.cameras(site.node_id)] == sorted(
        site.all_cameras(), key=str
    )
    zones = stack.zones(site.node_id)
    assert {row["zone_id"] for row in zones} == set(site.zones)
    assert all(row["coverage_ok"] for row in zones)
    (history,) = stack.history(site.node_id)
    assert str(history["heartbeat_id"]) == body["heartbeat_id"]
    (transition,) = stack.communication(site)
    assert transition == {
        "node_id": str(site.node_id),
        "state": "reachable",
        "since": format_timestamp(received),
        "last_heartbeat_at": format_timestamp(received),
    }


def test_a_repeated_heartbeat_id_changes_nothing_and_answers_the_current_state(
    stack: HeartbeatStack,
) -> None:
    site = stack.site()
    stack.tick()
    body = stack.body(site)
    received = to_millisecond(stack.now())
    first = stack.post(site, body)
    assert first.status_code == 200, first.text
    before = (stack.inventory(site.node_id), stack.cameras(site.node_id), stack.zones(site.node_id))
    stack.tick(30)
    changed = {**body, "uptime_seconds": 99_999, "model_version": "otro-modelo"}
    again = stack.post(site, changed)
    assert again.status_code == 200, again.text
    after = (stack.inventory(site.node_id), stack.cameras(site.node_id), stack.zones(site.node_id))
    assert after == before
    assert len(stack.history(site.node_id)) == 1
    assert len(stack.communication(site)) == 1
    assert stack.regression_marks(site) == []
    # La misma respuesta, byte a byte, con la hora del original (BR-CTR-26, VIG-182).
    assert again.content == first.content
    assert json.loads(again.content)["server_time"] == format_timestamp(received)


def test_a_repeated_heartbeat_id_on_another_instance_gets_the_same_bytes(
    stack: HeartbeatStack,
) -> None:
    # NFR-GOB-15: la hora del original sale de ``HeartbeatHistory``, no de la instancia que lo
    # atendió; el duplicado llega a otra instancia (otro pool) segundos después y con otro cuerpo.
    site = stack.site(zones=2)
    other = stack.instance()
    stack.tick()
    body = stack.body(site)
    received = to_millisecond(stack.now())
    first = stack.post(site, body)
    assert first.status_code == 200, first.text
    before = (stack.inventory(site.node_id), stack.cameras(site.node_id), stack.zones(site.node_id))
    records = len(stack.communication(site))
    stack.tick(45)
    again = stack.post(site, {**body, "uptime_seconds": body["uptime_seconds"] + 45}, other)
    assert again.status_code == 200, again.text
    assert again.content == first.content
    assert json.loads(first.content)["server_time"] == format_timestamp(received)
    after = (stack.inventory(site.node_id), stack.cameras(site.node_id), stack.zones(site.node_id))
    assert after == before
    assert len(stack.history(site.node_id)) == 1
    assert len(stack.communication(site)) == records


def test_reachable_is_written_only_after_unknown_or_mute(stack: HeartbeatStack) -> None:
    site = stack.site()
    _accepted(stack, site)
    _accepted(stack, site)
    _accepted(stack, site)
    assert [record["state"] for record in stack.communication(site)] == ["reachable"]
    stack.set_communication_state(site.node_id, "mute")
    stack.tick(600)
    _accepted(stack, site)
    _accepted(stack, site)
    assert [record["state"] for record in stack.communication(site)] == ["reachable", "reachable"]
    inventory = stack.inventory(site.node_id)
    assert inventory is not None and inventory["communication_state"] == "reachable"


def test_last_heartbeat_at_never_goes_back(stack: HeartbeatStack) -> None:
    site = stack.site()
    _accepted(stack, site)
    future = stack.now() + HOUR
    stack.execute(
        "UPDATE fleet.node_inventory SET last_heartbeat_at = $2 WHERE node_id = $1",
        site.node_id,
        future,
    )
    _accepted(stack, site, uptime_seconds=7200)
    inventory = stack.inventory(site.node_id)
    assert inventory is not None
    assert inventory["last_heartbeat_at"] == future
    assert inventory["uptime_seconds"] == 7200  # el resto es del último latido aceptado


# --- Respuesta -----------------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [15, 60, 600])
def test_mute_after_seconds_is_five_times_the_interval(
    stack: HeartbeatStack, interval: int
) -> None:
    site = stack.site(interval=interval)
    parsed = api.parse_heartbeat_response(_accepted(stack, site).content)
    assert parsed.heartbeat_interval_seconds == interval
    assert parsed.mute_after_seconds == 5 * interval


def test_the_target_version_travels_without_maintenance_window(stack: HeartbeatStack) -> None:
    site = stack.site()
    response = _accepted(stack, site)
    assert "target_software_version" not in response.json()
    for version, published in (("1.5.0", stack.now() - HOUR), ("1.6.0", stack.now())):
        stack.execute(
            "INSERT INTO fleet.target_version_publication (publication_id, organization_id,"
            " plant_id, target_version, node_ids, maintenance_window_from, maintenance_window_to,"
            " published_by, published_at, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            stack.uuid7(),
            site.organization_id,
            site.plant_id,
            version,
            [site.node_id],
            published,
            published + DAY,
            stack.authz.operator_id,
            published,
            uuid.uuid4(),
        )
    document = _accepted(stack, site).json()
    assert document["target_software_version"] == "1.6.0"
    assert "maintenance_window" not in json.dumps(document)
    other = stack.site()
    assert "target_software_version" not in _accepted(stack, other).json()


def test_the_contract_notice_carries_retires_at_when_the_version_retires(
    stack: HeartbeatStack,
) -> None:
    retires_at = format_timestamp(stack.now() + 120 * DAY)
    instance = stack.instance(policy=retiring_policy(VERSION, retires_at))
    site = stack.site()
    stack.tick()
    response = stack.post(site, stack.body(site), instance)
    assert response.status_code == 200, response.text
    notice = api.parse_heartbeat_response(response.content).contract_notice
    assert notice.result.value == "accepted_with_notice"
    assert notice.retires_at == retires_at
    assert VERSION in notice.message_es and retires_at in notice.message_es
    inventory = stack.inventory(site.node_id)
    assert inventory is not None
    assert inventory["contract_notice"] == {
        "result": "accepted_with_notice",
        "retires_at": retires_at,
    }


def test_the_gate_envelopes_are_the_stored_bytes(stack: HeartbeatStack) -> None:
    site = stack.site(zones=3)
    response = _accepted(stack, site)
    for zone in site.zones:
        assert stack.gate_text(zone).encode("utf-8") in response.content
    stored = {zone: json.loads(stack.gate_text(zone)) for zone in site.zones}
    served = {
        uuid.UUID(envelope["payload"]["zone_id"]): envelope
        for envelope in response.json()["gate_states"]
    }
    assert served == stored


def test_a_thousand_heartbeats_never_call_sign(stack: HeartbeatStack) -> None:
    site = stack.site(zones=2)
    stored = [stack.gate_text(zone).encode("utf-8") for zone in site.zones]
    before = stack.signer.calls

    async def beat() -> None:
        for _ in range(1000):
            stack.tick()
            response = await stack.send(site, stack.body(site))
            assert response.status_code == 200, response.text
            assert all(text in response.content for text in stored)

    stack.run(beat())
    assert stack.signer.calls == before
    assert len(stack.history(site.node_id)) == 1000


def _gate_payload(stack: HeartbeatStack, envelope: Any) -> dict[str, Any]:
    """La carga verificada de un sobre ``GateState`` con la clave ``gate`` publicada."""
    keyset = KeySet(stack.signing.clock)
    keyset.pin_initial(
        [k.to_contract() for p in NODE_PURPOSES for k in stack.signing.service.public_keys(p)]
    )
    payload: dict[str, Any] = verify(envelope, keyset, "gate", stack.signing.clock)
    return payload


def _assert_no_operate(payload: dict[str, Any]) -> None:
    assert (payload["mounting_gate"], payload["usage_gate"], payload["resulting_mode"]) == (
        {"status": "pending"},
        {"status": "pending"},
        "no_capture",
    )


def test_a_node_in_a_newly_created_zone_receives_the_creation_envelope_without_signing(
    stack: HeartbeatStack,
) -> None:
    # A-60: lo que deja ``create_zone`` (``sign_initial`` antes y ``save_initial`` en su
    # transacción, con el mismo ``GateService``); el latido lo sirve tal cual.
    site = stack.site(gates=False)
    (zone,) = site.zones
    gates = stack.primary.gates
    context = unit_context(site.organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)

    async def create() -> None:
        initial = await gates.sign_initial(site.organization_id, site.plant_id, zone, stack.now())
        async with stack.primary.database.transaction(context) as transaction:
            await gates.save_initial(transaction, initial)

    stack.run(create())
    stored = stack.gate_text(zone)
    before = stack.signer.calls

    for _ in range(3):
        response = _accepted(stack, site)
        (served,) = response.json()["gate_states"]
        assert stored.encode("utf-8") in response.content
        _assert_no_operate(_gate_payload(stack, served))
    assert stack.signer.calls == before


def test_a_zone_from_before_a60_receives_its_signed_initial_envelope_once(
    stack: HeartbeatStack,
) -> None:
    site = stack.site(gates=False)
    (zone,) = site.zones
    before = stack.signer.calls

    first = _accepted(stack, site)

    assert stack.signer.calls == before + 1
    stored = stack.gate_text(zone)
    assert stored.encode("utf-8") in first.content
    (served,) = first.json()["gate_states"]
    _assert_no_operate(_gate_payload(stack, served))
    second = _accepted(stack, site)
    assert stack.signer.calls == before + 1  # ya guardado: no se vuelve a firmar
    assert stored.encode("utf-8") in second.content


def test_with_signing_down_a_zone_without_envelope_is_unavailable_and_writes_nothing(
    stack: HeartbeatStack,
) -> None:
    site = stack.site(gates=False)
    stack.tick()
    stack.signer.down = True
    try:
        response = stack.post(site)
    finally:
        stack.signer.down = False
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "temporarily_unavailable"
    assert stack.inventory(site.node_id) is None
    assert stack.history(site.node_id) == [] and stack.communication(site) == []


def test_no_zone_with_a_catalog_is_temporarily_unavailable_and_writes_nothing(
    stack: HeartbeatStack,
) -> None:
    site = stack.site(catalogs=False)
    stack.tick()
    response = stack.post(site)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "temporarily_unavailable"
    assert stack.inventory(site.node_id) is None
    assert stack.history(site.node_id) == [] and stack.communication(site) == []


# --- live_view_local_url y accesos locales -------------------------------------------------------


def test_the_local_url_reaches_the_fleet_record_and_identity_and_goes_back_to_null(
    stack: HeartbeatStack,
) -> None:
    site = stack.site()
    _accepted(stack, site, live_view_local_url=LIVE_VIEW_URL)
    assert stack.urls(site.node_id) == (LIVE_VIEW_URL, LIVE_VIEW_URL)
    _accepted(stack, site)
    assert stack.urls(site.node_id) == (None, None)
    identity = stack.fetch(
        "SELECT status FROM identity.node_identity WHERE node_id = $1", site.node_id
    )
    assert identity[0]["status"] == "enrolled"  # sin cambiar el estado


def test_local_accesses_reach_u02_once_even_if_the_heartbeat_repeats(
    stack: HeartbeatStack,
) -> None:
    site = stack.site()
    access = {
        "access_id": str(stack.uuid7()),
        "jti": str(stack.uuid7()),
        "sub": str(uuid.uuid4()),
        "role": "coordinator_sst",
        "zone_id": str(site.zones[0]),
        "opened_at": format_timestamp(stack.now() - HOUR),
        "outcome": "rejected_signature",
    }
    stack.tick()
    body = stack.body(site, live_view_accesses=[access])
    assert stack.post(site, body).status_code == 200
    assert stack.post(site, body).status_code == 200
    _accepted(stack, site, live_view_accesses=[access])
    reported = stack.fetch(
        "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation IN ('live_view_access_local', 'unknown_token_reported')"
        " AND filters_json ->> 'access_id' = $2",
        site.organization_id,
        access["access_id"],
    )
    assert reported[0]["n"] == 1


# --- Regresión por model_version -----------------------------------------------------------------


def test_a_model_version_change_marks_the_zones_once_and_software_alone_does_not(
    stack: HeartbeatStack,
) -> None:
    site = stack.site(zones=2)
    _accepted(stack, site)
    assert stack.regression_marks(site) == []
    _accepted(stack, site, software_version="1.4.1")
    assert stack.regression_marks(site) == []
    _accepted(stack, site, model_version="modelo-2.0")
    marks = stack.regression_marks(site)
    assert len(marks) == 2
    assert {mark["content"]["zone_id"] for mark in marks} == {str(zone) for zone in site.zones}
    assert all(mark["content"]["cause"] == "model_version_change" for mark in marks)
    _accepted(stack, site, model_version="modelo-2.0")
    _accepted(stack, site, model_version="modelo-2.0", software_version="1.5.0")
    assert len(stack.regression_marks(site)) == 2


# --- Alcance -------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["organization_id", "plant_id", "node_id"])
def test_a_body_outside_the_certificate_is_node_zone_mismatch(
    stack: HeartbeatStack, field: str
) -> None:
    site, other = stack.site(), stack.site()
    stack.tick()
    foreign = {
        "organization_id": str(other.organization_id),
        "plant_id": str(other.plant_id),
        "node_id": str(other.node_id),
    }[field]
    response = stack.post(site, stack.body(site, **{field: foreign}))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "node_zone_mismatch"
    # El alcance gana al esquema (PR-GOB-02): con el cuerpo además inválido, el mismo código.
    broken = stack.post(site, stack.body(site, **{field: foreign}, uptime_seconds="mucho"))
    assert broken.json()["code"] == "node_zone_mismatch"
    assert stack.inventory(site.node_id) is None and stack.history(site.node_id) == []


@pytest.mark.parametrize("field", ["organization_id", "plant_id", "node_id"])
def test_the_service_itself_refuses_a_body_outside_the_certificate(
    stack: HeartbeatStack, field: str
) -> None:
    # Sin la verificación previa (``before_schema``): la segunda capa también lo rechaza.
    from vigia_contracts.models.enumerations import CompatibilityResult
    from vigia_contracts.models.heartbeat import Heartbeat

    from vigia_platform.fleet.application.heartbeat import NodeScopeMismatch

    site, other = stack.site(), stack.site()
    node = stack.node_scope(site)
    foreign = {
        "organization_id": str(other.organization_id),
        "plant_id": str(other.plant_id),
        "node_id": str(other.node_id),
    }[field]
    heartbeat = Heartbeat.model_validate_json(json.dumps(stack.body(site, **{field: foreign})))
    with pytest.raises(NodeScopeMismatch):
        stack.run(stack.primary.service.accept(node, heartbeat, CompatibilityResult.ACCEPTED))
    assert stack.inventory(site.node_id) is None and stack.inventory(other.node_id) is None


def test_zones_the_node_no_longer_has_are_ignored(stack: HeartbeatStack) -> None:
    site, other = stack.site(zones=2), stack.site()
    kept, retired = site.zones
    # Una zona de su planta que ya no tiene asignada (la que solo protege el filtro) y otra ajena.
    stack.execute(
        "UPDATE identity.zone_node_assignment SET unassigned_at = $3"
        " WHERE node_id = $1 AND zone_id = $2 AND unassigned_at IS NULL",
        site.node_id,
        retired,
        stack.now(),
    )
    stack.tick()
    body = stack.body(site)
    body["zones"] = [
        *body["zones"],
        {
            "zone_id": str(other.zones[0]),
            "mode": "productive",
            "observability_state": "observable",
            "catalog_version": 1,
            "gate_state_valid_until": format_timestamp(stack.now() + DAY),
            "open_episodes": 0,
        },
    ]
    response = stack.post(site, body)
    assert response.status_code == 200, response.text
    assert [row["zone_id"] for row in stack.zones(site.node_id)] == [kept]
    assert stack.zones(other.node_id) == []
    (history,) = stack.history(site.node_id)
    assert history["payload_summary"]["zones"]["ignored"] == 2
    assert [entry["zone_id"] for entry in response.json()["catalog_versions_available"]] == [
        str(kept)
    ]


# --- Cobertura mínima (BR-GOB-97) ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (("observable", "observable", "observable"), True),
        (("observable", "degraded", "observable"), True),  # solo una no requerida
        (("degraded", "observable", "observable"), False),  # la requerida
        (("observable", "not_observable", "degraded"), False),  # por debajo de required_count
    ],
)
def test_coverage_ok_follows_the_minimum_coverage_of_the_zone(
    stack: HeartbeatStack, states: tuple[str, str, str], expected: bool
) -> None:
    site = stack.site(cameras=3, required=2)
    stack.tick()
    body = stack.body(site)
    for camera, state in zip(body["cameras"], states, strict=True):
        camera["observability_state"] = state
    assert stack.post(site, body).status_code == 200
    (zone,) = stack.zones(site.node_id)
    assert zone["coverage_ok"] is expected


# --- Revocación leída en la transacción ---------------------------------------------------------


def test_a_revocation_after_the_identity_check_answers_revoked_and_writes_no_transition(
    stack: HeartbeatStack,
) -> None:
    site = stack.site()
    stack.tick()

    async def revoke_in_between() -> Any:
        from vigia_contracts.models.enumerations import CompatibilityResult
        from vigia_contracts.models.heartbeat import Heartbeat

        from vigia_platform.identity.authz.context import PresentedNode
        from vigia_platform.node_api.certificate_profile import serial_hex

        presented = PresentedNode(
            node_id=site.node_id,
            organization_id=site.organization_id,
            plant_id=site.plant_id,
            certificate_serial=serial_hex(site.certificate.serial_number),
        )
        node = await stack.primary.gate.identity.resolve(presented, stack.uuid7())
        # La revocación confirma entre la verificación previa y la transacción del latido.
        await stack.authz.sessions.admin.execute(
            "UPDATE fleet.node_fleet_record SET revoked_at = $2,"
            " revocation_reason_es = 'Equipo retirado por mantenimiento' WHERE node_id = $1",
            site.node_id,
            stack.now(),
        )
        heartbeat = Heartbeat.model_validate_json(json.dumps(stack.body(site)))
        return await stack.primary.service.accept(node, heartbeat, CompatibilityResult.ACCEPTED)

    reply = stack.run(revoke_in_between())
    document = json.loads(reply.content)
    assert document["revoked"] is True
    assert stack.communication(site) == []


# --- Firma caída y renovación (A-55) ------------------------------------------------------------


def test_with_signing_down_the_heartbeat_still_answers_with_the_stored_envelopes(
    stack: HeartbeatStack,
) -> None:
    site = stack.site(gate_issued_at=stack.now() - dt.timedelta(days=6, hours=12))
    stored = stack.gate_text(site.zones[0])
    stack.signer.down = True
    try:
        response = _accepted(stack, site)
    finally:
        stack.signer.down = False
    assert stored.encode("utf-8") in response.content
    assert stack.gate_text(site.zones[0]) == stored


def test_an_envelope_about_to_expire_is_renewed_once_and_a_fresh_one_never(
    stack: HeartbeatStack,
) -> None:
    expiring = stack.site(gate_issued_at=stack.now() - dt.timedelta(days=6, hours=12))
    fresh = stack.site(gate_issued_at=stack.now() - dt.timedelta(days=5))
    old = stack.gate_text(expiring.zones[0])
    before = stack.signer.calls
    response = _accepted(stack, expiring)
    assert stack.signer.calls == before + 1
    renewed = stack.gate_text(expiring.zones[0])
    assert renewed != old and renewed.encode("utf-8") in response.content
    payload = json.loads(renewed)["payload"]
    assert payload["valid_until"] > format_timestamp(stack.now() + 6 * DAY)
    _accepted(stack, expiring)
    _accepted(stack, fresh)
    _accepted(stack, fresh)
    assert stack.signer.calls == before + 1


# --- Sin texto libre -----------------------------------------------------------------------------


_ALLOWED_TEXT: Final = frozenset(
    {"observable", "degraded", "not_observable", "productive", "commissioning", "no_capture"}
    | {"modbus_rtu", "file", "simulated", "closed", "open", "half_open"}
)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item)
            del key
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_payload_summary_has_no_free_text_nor_zone_labels(stack: HeartbeatStack) -> None:
    site = stack.site(zones=2)
    _accepted(
        stack,
        site,
        live_view_local_url=LIVE_VIEW_URL,
        local_queue={
            "pending": 4,
            "dead_letter": [{"code": "schema_invalid", "count": 2}],
            "retained_sent": 1,
            "circuit_state": "closed",
            "lost_episodes": 0,
        },
    )
    (history,) = stack.history(site.node_id)
    summary = history["payload_summary"]
    assert set(_strings(summary)) <= _ALLOWED_TEXT, summary
    labels = stack.fetch(
        "SELECT code, name FROM identity.zone WHERE zone_id = ANY($1::uuid[])", list(site.zones)
    )
    text = json.dumps(summary)
    for label in labels:
        assert label["code"] not in text and label["name"] not in text
    for forbidden in (LIVE_VIEW_URL, "ntp_local", MODEL_VERSION, "1.4.0"):
        assert forbidden not in text
    assert len(text.encode()) < 1536  # NFR-GOB-07


def test_the_node_metrics_are_counters_and_gauges_by_node_without_zone(
    stack: HeartbeatStack,
) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader

    from vigia_platform.shared.observability.metrics import PlatformMetrics

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    instance = stack.instance(metrics=PlatformMetrics(provider.get_meter("vigia_platform")))
    site = stack.site(zones=2)
    stack.tick()
    body = stack.body(site)
    for payload in (
        body,
        body,
        stack.body(site, local_queue={"pending": 7, "dead_letter": [], "retained_sent": 0}),
    ):
        stack.tick(60)
        assert stack.post(site, payload, instance).status_code == 200
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[str, list[Any]] = {}
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name.startswith("fleet_"):
                    assert not isinstance(metric.data, Histogram), metric.name
                    points[metric.name] = list(metric.data.data_points)
    assert set(points) == {
        "fleet_heartbeats_total",
        "fleet_heartbeat_gap_seconds",
        "fleet_node_reachable",
        "fleet_node_queue_pending",
        "fleet_node_clock_offset_ms",
    }
    for name, series in points.items():
        for point in series:
            attributes = dict(point.attributes or {})
            assert set(attributes) <= {"node_id", "result"}, (name, attributes)
            assert attributes["node_id"] == str(site.node_id)
    counted = {
        dict(point.attributes)["result"]: point.value for point in points["fleet_heartbeats_total"]
    }
    assert counted == {"accepted": 2, "ignored": 1}
    assert [point.value for point in points["fleet_node_queue_pending"]] == [7]
    assert [point.value for point in points["fleet_heartbeat_gap_seconds"]] == [120]
    assert [point.value for point in points["fleet_node_reachable"]] == [1]
    provider.shutdown()


# --- Base pausada -------------------------------------------------------------------------------


def test_a_paused_database_is_temporarily_unavailable_and_writes_nothing(
    stack: HeartbeatStack, postgres_endpoint: PostgresEndpoint
) -> None:
    from tests.fault_proxy import ProxyMode, fault_proxy

    site = stack.site()
    _accepted(stack, site)
    before = (stack.inventory(site.node_id), stack.history(site.node_id))
    migrated = stack.authz.sessions.migrated
    with fault_proxy(postgres_endpoint.host, postgres_endpoint.port) as proxy:
        database = stack.database(
            url=migrated.as_role("vigia_app").sqlalchemy_url.replace(
                f"{postgres_endpoint.host}:{postgres_endpoint.port}", f"127.0.0.1:{proxy.port}"
            ),
            statement_timeout_ms=2_000,
            connect_timeout_seconds=2.0,
            pool_timeout_seconds=2.0,
        )
        paused = stack.instance(database)
        stack.tick()
        assert stack.post(site, stack.body(site), paused).status_code == 200
        before = (stack.inventory(site.node_id), stack.history(site.node_id))
        # Pausada desde el principio.
        proxy.set_mode(ProxyMode.FREEZE)
        stack.tick()
        response = stack.post(site, stack.body(site), paused)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "temporarily_unavailable"
        assert response.json()["retryable"] is True and response.json()["retry_after_seconds"] >= 1
        proxy.set_mode(ProxyMode.FORWARD)
        assert (stack.inventory(site.node_id), stack.history(site.node_id)) == before
        # Pausada a mitad de la transacción: después del candado del nodo, antes del anexo.
        history = paused.service._deps.history
        original = history.append

        async def frozen_append(*args: Any, **kwargs: Any) -> None:
            proxy.set_mode(ProxyMode.FREEZE)
            await original(*args, **kwargs)

        history.append = frozen_append  # type: ignore[method-assign]
        try:
            stack.tick()
            response = stack.post(site, stack.body(site), paused)
        finally:
            history.append = original  # type: ignore[method-assign]
            proxy.set_mode(ProxyMode.FORWARD)
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "temporarily_unavailable"
        assert (stack.inventory(site.node_id), stack.history(site.node_id)) == before
        stack.run(asyncio.sleep(0))
