"""Alta y rotación del nodo por las rutas del contrato (TASK-219; BR-GOB-58, 61 a 64, 68; LC-GOB-11;
NFR-GOB-10, 25, 34; PAT-GOB-REN-02).

Contra PostgreSQL 16 real como ``vigia_app`` (``tests/fleet_enrollment_support.py``): servicios
reales de TASK-218 y TASK-219, ``vigia-node-ca`` con un doble de KMS de clave P-256 de prueba, y
la aplicación real de las rutas del contrato.

- **Alta**: 200 con ``NodeEnrollmentResponse`` válida; el certificado verifica contra la raíz del
  paquete; la credencial ``active``, el nodo ``enrolled`` con ``enrolled_at`` y su huella, el código
  ``used``, el intento ``accepted``, el registro ``node_enrolled`` (``source_key = node_id +
  credential_id``) y su evento. La configuración inicial cumple D-11 y A-04 y sus ``zones`` y
  ``gate_states`` son los sobres guardados, sin firmar nada.
- **Concurrencia** (BR-GOB-58, 61; G-2): dos altas simultáneas con el mismo código dejan una
  credencial y un ``node_enrolled``; la otra recibe ``enrollment_code_used`` y deja su intento.
- **Re-alta** (U03-H-04): con la huella registrada vuelve a ``enrolled`` con credencial nueva; con
  otra, ``enrollment_code_invalid``, código ``active`` e intento registrado.
- **Rotación** (BR-GOB-64, NFR-GOB-34): la nueva ``active``, la anterior ``overlapping`` autentica
  hasta 24 h y a las 24 h + 1 s la consulta de identidad la rechaza; ``node_credential_rotated``;
  con la ``overlapping`` no se rota; dos rotaciones simultáneas dejan una sola credencial nueva.
- **Alcance** (BR-GOB-88): ni un código ni una CSR de otra organización producen una credencial
  de esa organización.
- **Telemetría** (NFR-GOB-25): ni el PEM, ni el código, ni la huella salen en registros,
  métricas, trazas ni eventos.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from opentelemetry import trace as otel_trace
from vigia_contracts.models.api import (
    parse_credential_rotation_response,
    parse_node_enrollment_response,
    parse_rejection_response,
)

from tests.fleet_credentials_support import (
    BASIC_CONSTRAINTS_OID_DER,
    SAN_OID_DER,
    X400_SAN_VALUE,
    basic_constraints_value,
    hostile_csr_pem,
    local_ip,
    rsa_key,
    san_value,
)
from tests.fleet_enrollment_support import (
    ENROLLMENT_PATH,
    INGEST_BASE_URL,
    ROTATION_PATH,
    BarrierIssuer,
    EnrollmentWorld,
    enrollment_world,
)
from tests.fleet_http_support import REASON
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import VERSION
from tests.properties.gob.telemetry_harness import TelemetryCapture
from vigia_platform.fleet.adapters.ca.csr import CLIENT_CSR_FIELD, SERVER_CSR_FIELD
from vigia_platform.fleet.adapters.postgres.node_fleet_store import PostgresNodeFleetStore
from vigia_platform.fleet.application.node_revocation import NodeRevocationService
from vigia_platform.fleet.domain.node_subject import NodeSubject, read_subject, serial_hex
from vigia_platform.node_api.observability import NodeResponses

pytestmark = pytest.mark.integration

HOUR: Final = timedelta(hours=1)
LOCK_WAIT_SECONDS: Final = 30.0
"""Tope de las esperas de la prueba de candados: nunca decide nada (retro 14)."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[EnrollmentWorld]:
    with enrollment_world(postgres_endpoint, "fleet_enrollment") as built:
        yield built


def _code(response: httpx.Response) -> str:
    return parse_rejection_response(response.content).code.value


def _organization_records(world: EnrollmentWorld, record_type: str, org: uuid.UUID) -> list[Any]:
    return world.fleet.records(record_type, org)


# --- Alta ---------------------------------------------------------------------------------------


def test_an_accepted_enrollment_writes_everything_in_one_transaction(
    world: EnrollmentWorld,
) -> None:
    setup = world.declared(zones=2, cameras=2)
    code = world.code(setup)
    signs_before = world.keys.signs
    enrolled = world.enroll(setup, code)

    response = parse_node_enrollment_response(json.dumps(dict(enrolled.body)).encode())
    assert response.node_id == str(setup.node_id)
    certificate = enrolled.certificate
    certificate.verify_directly_issued_by(world.root)
    assert read_subject(certificate.subject) == NodeSubject(
        setup.node_id, setup.organization_id, setup.plant_id
    )
    assert x509.load_pem_x509_certificates(response.ca_chain.encode()) == [world.root]
    (credential,) = world.credentials(setup.node_id)
    assert credential["status"] == "active"
    assert credential["certificate_serial"] == serial_hex(certificate.serial_number)
    assert credential["expires_at"] == certificate.not_valid_after_utc
    record = world.fleet_record(setup.node_id)
    assert record["status"] == "enrolled" and record["enrolled_at"] is not None
    assert record["hardware_fingerprint"] == setup.fingerprint
    assert world.code_statuses(setup.node_id) == ["used"]
    assert [row["result"] for row in world.attempts(setup.node_id)] == ["accepted"]
    (written,) = world.fleet.records("node_enrolled", setup.organization_id)
    content = written["content"]
    assert content["credential_id"] == str(credential["credential_id"])
    assert written["source_key"] == setup.node_id.hex + uuid.UUID(content["credential_id"]).hex
    assert sorted(content["zone_ids"]) == sorted(str(zone) for zone in setup.zones)
    assert content["hardware_fingerprint"] == setup.fingerprint
    zones = [uuid.UUID(zone) for zone in content["zone_ids"]]  # orden de asignación
    (event,) = world.fleet.events("node_enrolled", setup.organization_id)
    assert event == {
        "node_id": str(setup.node_id),
        "plant_id": str(setup.plant_id),
        "zone_ids": content["zone_ids"],
        "replaces_node_id": None,
    }
    # Cero firmas: los sobres salen de donde se guardaron (NFR-GOB-10, PAT-GOB-REN-02).
    assert world.keys.signs == signs_before
    configuration = enrolled.body["initial_configuration"]
    assert configuration["heartbeat"] == {"interval_seconds": 60, "mute_after_seconds": 300}
    assert configuration["gate_cache_ttl_seconds"] == 604_800
    assert configuration["sent_records_retention_days"] == 30
    assert configuration["live_view"] == {"token_max_age_seconds": 600}
    assert configuration["credential"] == {
        "validity_days": 365,
        "rotate_before_days": 30,
        "alert_before_days": 15,
    }
    assert configuration["endpoints"] == {"ingest_base_url": INGEST_BASE_URL}
    expected_cameras = []
    for index, zone in enumerate(zones):
        catalog, gate = world.stored(zone)
        # El sobre guardado tal cual: el mismo valor JSON y la misma firma.
        assert configuration["zones"][index] == catalog
        assert configuration["gate_states"][index] == gate
        expected_cameras += [
            {"camera_id": c["camera_id"], "code": c["code"], "stream_reference": f"cam-{n}"}
            for n, c in enumerate(catalog["payload"]["cameras"])
        ]
    assert configuration["cameras"] == expected_cameras


def test_the_enrollment_route_ignores_any_mtls_header(world: EnrollmentWorld) -> None:
    setup = world.declared()
    forged = {
        "X-Amzn-Mtls-Clientcert-Leaf": "basura",
        "X-Amzn-Mtls-Clientcert-Serial-Number": "ZZ",
        "X-Amzn-Mtls-Clientcert-Subject": "CN=otro",
    }
    response = world.post(ENROLLMENT_PATH, world.body(setup, world.code(setup)), forged)
    assert response.status_code == 200, response.text


def test_a_common_name_that_is_no_declared_node_is_schema_invalid(world: EnrollmentWorld) -> None:
    setup = world.declared()
    code = world.code(setup)
    response = world.post(ENROLLMENT_PATH, world.body(setup, code, common_name=str(uuid.uuid4())))
    rejection = parse_rejection_response(response.content)
    assert (response.status_code, rejection.code.value) == (422, "schema_invalid")
    assert rejection.field == CLIENT_CSR_FIELD
    assert world.code_statuses(setup.node_id) == ["active"]
    assert world.credentials(setup.node_id) == []


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"algorithm": hashes.SHA384()}, CLIENT_CSR_FIELD),
        ({"key": rsa_key()}, CLIENT_CSR_FIELD),
        ({"names": []}, SERVER_CSR_FIELD),
        ({"names": [local_ip("8.8.8.8")]}, SERVER_CSR_FIELD),
    ],
)
def test_an_invalid_csr_is_schema_invalid_before_the_code(
    world: EnrollmentWorld, change: dict[str, Any], field: str
) -> None:
    setup = world.declared()
    code = world.code(setup)
    arguments = dict(change)
    if "key" in arguments:
        arguments["client_key"] = arguments.pop("key")
    response = world.post(ENROLLMENT_PATH, world.body(setup, code, **arguments))
    rejection = parse_rejection_response(response.content)
    assert (response.status_code, rejection.code.value, rejection.field) == (
        422,
        "schema_invalid",
        field,
    )
    # Antes que el código: ni se consume ni deja intento.
    assert world.code_statuses(setup.node_id) == ["active"]
    assert world.attempts(setup.node_id) == []


HOSTILE = {
    "san_duplicada": (
        (SAN_OID_DER, san_value(x509.DNSName("nodo.local"))),
        (SAN_OID_DER, san_value(x509.DNSName("otro.local"))),
    ),
    "basic_constraints_duplicada": (
        (BASIC_CONSTRAINTS_OID_DER, basic_constraints_value()),
        (BASIC_CONSTRAINTS_OID_DER, basic_constraints_value()),
    ),
    "san_x400": ((SAN_OID_DER, X400_SAN_VALUE),),
}


@pytest.mark.parametrize("field", [CLIENT_CSR_FIELD, SERVER_CSR_FIELD])
@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_a_hostile_csr_is_schema_invalid_in_enrollment_and_rotation(
    world: EnrollmentWorld, case: str, field: str
) -> None:
    # Revisión de VIG-151, ronda 1: una extensión repetida o un GeneralName no soportado salían
    # de read_csr como excepción no controlada y la ruta respondía 503 reintentable.
    setup = world.declared()
    code = world.code(setup)
    body = world.body(setup, code)
    body[field] = hostile_csr_pem(str(setup.node_id), HOSTILE[case])
    response = world.post(ENROLLMENT_PATH, body)
    rejection = parse_rejection_response(response.content)
    assert (response.status_code, rejection.code.value, rejection.field) == (
        422,
        "schema_invalid",
        field,
    )
    assert rejection.retryable is False
    assert world.code_statuses(setup.node_id) == ["active"]
    assert world.attempts(setup.node_id) == []

    enrolled = world.enroll(setup, code)
    rotation = world.rotation_body(setup.node_id)
    rotation[field] = hostile_csr_pem(str(setup.node_id), HOSTILE[case])
    refused = world.post(ROTATION_PATH, rotation, enrolled.headers)
    rejection = parse_rejection_response(refused.content)
    assert (refused.status_code, rejection.code.value, rejection.field) == (
        422,
        "schema_invalid",
        field,
    )
    assert [row["status"] for row in world.credentials(setup.node_id)] == ["active"]


def test_a_wrong_code_is_rejected_and_its_attempt_is_registered(world: EnrollmentWorld) -> None:
    setup = world.declared()
    world.code(setup)
    response = world.post(ENROLLMENT_PATH, world.body(setup, "ABCDEFGHJKLM"))
    assert (response.status_code, _code(response)) == (401, "enrollment_code_invalid")
    assert [row["result"] for row in world.attempts(setup.node_id)] == ["enrollment_code_invalid"]
    assert world.code_statuses(setup.node_id) == ["active"]
    assert len(world.fleet.records("enrollment_attempt_rejected", setup.organization_id)) == 1


def test_a_zone_without_its_gate_envelope_is_transient_and_keeps_the_code(
    world: EnrollmentWorld,
) -> None:
    setup = world.declared(published=False)
    world.publish(setup, setup.zones[0], gate=False)
    code = world.code(setup)
    response = world.post(ENROLLMENT_PATH, world.body(setup, code))
    rejection = parse_rejection_response(response.content)
    assert (response.status_code, rejection.code.value) == (503, "temporarily_unavailable")
    assert rejection.retryable and rejection.retry_after_seconds == 60
    assert world.code_statuses(setup.node_id) == ["active"]
    assert world.credentials(setup.node_id) == []
    assert world.attempts(setup.node_id) == []
    # Con el sobre guardado, el mismo código sirve.
    world.publish_gate(setup, setup.zones[0])
    assert world.post(ENROLLMENT_PATH, world.body(setup, code)).status_code == 200


# --- Concurrencia (BR-GOB-58, 61; G-2) ----------------------------------------------------------


def test_two_simultaneous_enrollments_with_one_code_leave_one_credential(
    world: EnrollmentWorld,
) -> None:
    setup = world.declared()
    code = world.code(setup)
    issuer = BarrierIssuer(world.issuer(), 2)
    enrollment, rotation = world.services(issuer=issuer)  # type: ignore[arg-type]
    _, app = world.app(enrollment, rotation)
    calls = [(ENROLLMENT_PATH, world.body(setup, code), {}) for _ in range(2)]

    responses = world.race(app, calls)

    assert sorted(r.status_code for r in responses) == [200, 401], [r.text for r in responses]
    loser = next(response for response in responses if response.status_code == 401)
    assert _code(loser) == "enrollment_code_used"
    assert issuer.passes == 2  # las dos verificaron y firmaron antes de que ninguna consumiera
    assert len(world.credentials(setup.node_id)) == 1
    assert len(world.fleet.records("node_enrolled", setup.organization_id)) == 1
    assert len(world.fleet.events("node_enrolled", setup.organization_id)) == 1
    assert sorted(row["result"] for row in world.attempts(setup.node_id)) == [
        "accepted",
        "enrollment_code_used",
    ]
    assert world.code_statuses(setup.node_id) == ["used"]


# --- Re-alta (U03-H-04, G-2) --------------------------------------------------------------------


def test_re_enrollment_needs_the_registered_fingerprint(world: EnrollmentWorld) -> None:
    setup = world.declared()
    first = world.enroll(setup)
    assert world.fleet.revoke(setup.installer, setup.node_id).status_code == 200
    code = world.code(setup)
    assert world.fleet_record(setup.node_id)["status"] == "re_enrollment_pending"

    other = world.post(ENROLLMENT_PATH, world.body(setup, code, fingerprint="cd" * 32))
    assert (other.status_code, _code(other)) == (401, "enrollment_code_invalid")
    assert world.code_statuses(setup.node_id)[-1] == "active"
    results = [row["result"] for row in world.attempts(setup.node_id)]
    assert results[-1] == "enrollment_code_invalid"
    assert world.fleet_record(setup.node_id)["hardware_fingerprint"] == setup.fingerprint

    again = world.enroll(setup, code)
    assert world.fleet_record(setup.node_id)["status"] == "enrolled"
    assert [row["status"] for row in world.credentials(setup.node_id)] == ["revoked", "active"]
    assert again.certificate.serial_number != first.certificate.serial_number
    assert len(world.fleet.records("node_enrolled", setup.organization_id)) == 2
    assert world.probe_get(first.certificate, setup.zones[0]).status_code == 401
    assert world.probe_get(again.certificate, setup.zones[0]).status_code == 200


# --- Rotación (BR-GOB-64, NFR-GOB-34) -----------------------------------------------------------


def test_rotation_leaves_the_previous_overlapping_and_it_cannot_rotate(
    world: EnrollmentWorld,
) -> None:
    setup = world.declared()
    zone = setup.zones[0]
    old = world.enroll(setup)
    world.advance(1)
    response = world.rotate(old, setup.node_id)
    assert response.status_code == 200, response.text
    document = parse_credential_rotation_response(response.content).to_json_value()
    assert "initial_configuration" not in document
    assert document["node_id"] == str(setup.node_id)
    new = x509.load_pem_x509_certificate(document["certificate"].encode())
    new.verify_directly_issued_by(world.root)
    first, second = world.credentials(setup.node_id)
    assert (first["status"], second["status"]) == ("overlapping", "active")
    assert second["rotated_from"] == first["credential_id"]
    (rotated,) = world.fleet.records("node_credential_rotated", setup.organization_id)
    assert rotated["source_key"] == str(second["credential_id"])
    assert rotated["content"]["rotated_from"] == str(first["credential_id"])
    assert rotated["content"]["certificate_serial"] == serial_hex(new.serial_number)
    # Las dos autentican mientras dura el solapamiento.
    assert world.probe_get(old.certificate, zone).status_code == 200
    assert world.probe_get(new, zone).status_code == 200
    # Con la overlapping no se rota (BR-GOB-64).
    refused = world.rotate(old, setup.node_id)
    assert (refused.status_code, _code(refused)) == (401, "node_revoked")
    assert len(world.credentials(setup.node_id)) == 2


def test_two_simultaneous_rotations_leave_one_new_credential(world: EnrollmentWorld) -> None:
    setup = world.declared()
    enrolled = world.enroll(setup)
    world.advance(1)
    issuer = BarrierIssuer(world.issuer(), 2)
    enrollment, rotation = world.services(issuer=issuer)  # type: ignore[arg-type]
    _, app = world.app(enrollment, rotation)
    calls = [
        (ROTATION_PATH, world.rotation_body(setup.node_id), enrolled.headers) for _ in range(2)
    ]

    responses = world.race(app, calls)

    assert sorted(r.status_code for r in responses) == [200, 401], [r.text for r in responses]
    loser = next(response for response in responses if response.status_code == 401)
    assert _code(loser) == "node_revoked"
    assert issuer.passes == 2
    statuses = sorted(row["status"] for row in world.credentials(setup.node_id))
    assert statuses == ["active", "overlapping"]
    assert len(world.fleet.records("node_credential_rotated", setup.organization_id)) == 1


def test_a_rotation_naming_another_node_is_schema_invalid(world: EnrollmentWorld) -> None:
    setup, other = world.declared(), world.declared()
    enrolled = world.enroll(setup)
    body = world.rotation_body(setup.node_id)
    body["node_id"] = str(other.node_id)
    response = world.post(ROTATION_PATH, body, enrolled.headers)
    rejection = parse_rejection_response(response.content)
    assert (response.status_code, rejection.code.value, rejection.field) == (
        422,
        "schema_invalid",
        "node_id",
    )
    foreign = world.post(ROTATION_PATH, world.rotation_body(other.node_id) | {
        "node_id": str(setup.node_id)}, enrolled.headers)  # fmt: skip
    assert (foreign.status_code, _code(foreign)) == (422, "schema_invalid")
    assert len(world.credentials(setup.node_id)) == 1


# --- Alcance (BR-GOB-88, NFR-GOB-30) ------------------------------------------------------------


def test_a_code_of_another_organization_never_enrolls(world: EnrollmentWorld) -> None:
    own, foreign = world.declared(), world.declared()
    assert own.organization_id != foreign.organization_id
    foreign_code = world.code(foreign)
    world.code(own)
    # CSR con el node_id propio y el código del nodo de la otra organización.
    response = world.post(ENROLLMENT_PATH, world.body(own, foreign_code))
    assert (response.status_code, _code(response)) == (401, "enrollment_code_invalid")
    # Y al revés: CSR con el node_id ajeno y el código propio.
    crossed = world.post(
        ENROLLMENT_PATH, world.body(own, world.code(own), common_name=str(foreign.node_id))
    )
    assert (crossed.status_code, _code(crossed)) == (401, "enrollment_code_invalid")
    assert world.credentials(own.node_id) == [] and world.credentials(foreign.node_id) == []
    assert world.code_statuses(foreign.node_id) == ["active"]
    assert world.code_statuses(own.node_id)[-1] == "active"


def test_a_csr_naming_another_organization_gets_the_platform_subject(
    world: EnrollmentWorld,
) -> None:
    own, foreign = world.declared(), world.declared()
    extra = (
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, str(foreign.organization_id)),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, str(foreign.plant_id)),
    )
    enrolled = world.enroll(own, extra_subject=extra)
    assert read_subject(enrolled.certificate.subject) == NodeSubject(
        own.node_id, own.organization_id, own.plant_id
    )
    (credential,) = world.credentials(own.node_id)
    assert credential["organization_id"] == own.organization_id
    assert json.loads(credential["subject"])["organization_id"] == str(own.organization_id)
    assert world.credentials(foreign.node_id) == []


# --- Telemetría (NFR-GOB-25, parte de PR-GOB-31) ------------------------------------------------


def test_no_pem_code_or_fingerprint_reaches_logs_metrics_traces_or_events(
    world: EnrollmentWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = world.declared()
    code = world.code(setup)
    with TelemetryCapture() as capture:
        monkeypatch.setattr(otel_trace, "get_tracer", lambda *_, **__: capture.tracer)
        metrics = capture.telemetry.metrics
        deps = dataclasses.replace(world.fleet.deps, metrics=metrics)
        issuer = world.issuer(metrics=metrics)
        enrollment, rotation = world.services(issuer=issuer, deps=deps)
        _, app = world.app(
            enrollment,
            rotation,
            responses=NodeResponses(world.fleet.authz.sessions.clock, metrics=metrics),
        )
        wrong = world.body(setup, "ABCDEFGHJKLM")
        right = world.body(setup, code)
        responses = world.race(app, [(ENROLLMENT_PATH, wrong, {})])
        responses += world.race(app, [(ENROLLMENT_PATH, right, {})])
        assert [r.status_code for r in responses] == [401, 200], [r.text for r in responses]
        enrolled_pem = responses[1].json()["certificate"]
        certificate = x509.load_pem_x509_certificate(enrolled_pem.encode())
        rotation_body = world.rotation_body(setup.node_id)
        rotated = world.race(
            app,
            [(ROTATION_PATH, rotation_body, world.headers_for(certificate))],
        )
        assert rotated[0].status_code == 200, rotated[0].text
    sensitive = [
        code,
        "ABCDEFGHJKLM",
        setup.fingerprint,
        right["certificate_signing_request"],
        right["server_certificate_signing_request"],
        rotation_body["certificate_signing_request"],
        enrolled_pem,
        rotated[0].json()["certificate"],
        "-----BEGIN",
    ]
    assert capture.leaks(sensitive) == []
    emitted = "\n".join(capture.emitted())
    assert "enrollment_attempts_total" in emitted  # el arnés sí ve lo legítimo
    assert "node_ca_sign_duration_ms" in emitted
    events = world.fetch(
        "SELECT payload::text AS payload FROM shared.outbox_event WHERE organization_id = $1",
        setup.organization_id,
    )
    assert events and not any(value in row["payload"] for row in events for value in sensitive)


# --- Orden de candados: alta y revocación del mismo nodo a la vez -------------------------------


class SignalingNodes(PostgresNodeFleetStore):
    """El alta: avisa justo antes de pedir el candado de la ficha (su primer candado)."""

    def __init__(self, database: Any, arrived: asyncio.Event) -> None:
        super().__init__(database)
        self.arrived = arrived

    async def lock(self, transaction: Any, node_id: uuid.UUID) -> Any:
        self.arrived.set()
        return await super().lock(transaction, node_id)


class HoldingNodes(PostgresNodeFleetStore):
    """La revocación: con la ficha bloqueada, espera a que el alta pida su candado y a que la base
    la vea esperando un candado (``pg_stat_activity``); solo entonces sigue hacia la cadena."""

    def __init__(self, database: Any, held: asyncio.Event, arrived: asyncio.Event, admin: Any):
        super().__init__(database)
        self.held, self.arrived, self.admin = held, arrived, admin
        self.saw_waiter = False

    async def lock(self, transaction: Any, node_id: uuid.UUID) -> Any:
        node = await super().lock(transaction, node_id)
        self.held.set()
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            await self.arrived.wait()
            while not self.saw_waiter:
                waiting = await self.admin.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                    " AND datname = current_database()"
                )
                self.saw_waiter = waiting > 0
                await asyncio.sleep(0.05)
        return node


def test_an_enrollment_and_a_revocation_of_one_node_both_finish(world: EnrollmentWorld) -> None:
    # Orden único (fleet.application.common): ficha del nodo → identidad, credenciales y códigos
    # → cadena de la planta. La revocación tiene la ficha y espera; el alta pide la ficha y espera
    # detrás. Con el alta tomando la cadena antes que la ficha, cada una esperaría a la otra.
    setup = world.declared()
    code = world.code(setup)
    context = world.fleet.context(setup.installer)
    held, arrived = asyncio.Event(), asyncio.Event()
    base = world.fleet.deps
    holding = HoldingNodes(base.database, held, arrived, world.fleet.authz.sessions.admin)
    revocations = NodeRevocationService(dataclasses.replace(base, nodes=holding))
    enrollment, rotation = world.services(
        deps=dataclasses.replace(base, nodes=SignalingNodes(base.database, arrived))
    )
    _, app = world.app(enrollment, rotation)
    body = world.body(setup, code)

    async def race() -> list[Any]:
        revocation = asyncio.ensure_future(revocations.revoke(context, setup.node_id, REASON))
        async with asyncio.timeout(LOCK_WAIT_SECONDS):
            await held.wait()  # la revocación ya tiene la ficha
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://nodes.vigia.test", timeout=60
        ) as client:
            enrollment_call = client.post(
                ENROLLMENT_PATH, json=body, headers={"X-Vigia-Contract-Version": VERSION}
            )
            return list(await asyncio.gather(revocation, enrollment_call, return_exceptions=True))

    revoked, response = world.run(race())

    assert not isinstance(revoked, BaseException), revoked
    assert holding.saw_waiter  # el alta llegó a esperar detrás de la revocación
    assert isinstance(response, httpx.Response)
    assert (response.status_code, _code(response)) == (401, "enrollment_code_invalid")
    assert world.fleet_record(setup.node_id)["status"] == "revoked"
    assert world.credentials(setup.node_id) == []
    assert world.code_statuses(setup.node_id) == ["superseded"]


# --- Solapamiento de 24 h con el reloj simulado (al final: adelanta el reloj del módulo) --------


def test_the_overlapping_credential_authenticates_24_hours_then_is_superseded_lazily(
    world: EnrollmentWorld,
) -> None:
    # Todo lo que pasa por una concesión del instalador va antes de adelantar el reloj: su
    # seguridad a nivel de fila compara con la hora de la base. Las rutas del nodo usan solo el
    # reloj simulado (la identidad de cada petición, la rotación y su transacción).
    setup = world.declared()
    zone = setup.zones[0]
    old = world.enroll(setup)
    world.advance(1)
    rotated = world.rotate(old, setup.node_id)
    assert rotated.status_code == 200, rotated.text
    current = world.enrolled_from(rotated, old)
    first, second = world.credentials(setup.node_id)
    assert (first["status"], second["status"]) == ("overlapping", "active")
    deadline = second["issued_at"] + 24 * HOUR
    world.advance((deadline - world.now()).total_seconds() - 1)  # 24 h - 1 s
    assert world.probe_get(old.certificate, zone).status_code == 200
    world.advance(2)  # 24 h + 1 s
    late = world.probe_get(old.certificate, zone)
    assert (late.status_code, _code(late)) == (401, "node_revoked")
    late_rotation = world.rotate(old, setup.node_id)
    assert (late_rotation.status_code, _code(late_rotation)) == (401, "node_revoked")
    assert world.probe_get(current.certificate, zone).status_code == 200
    # La fila sigue overlapping hasta la siguiente rotación, que la materializa superseded.
    assert [row["status"] for row in world.credentials(setup.node_id)] == [
        "overlapping",
        "active",
    ]
    third = world.rotate(current, setup.node_id)
    assert third.status_code == 200, third.text
    assert [row["status"] for row in world.credentials(setup.node_id)] == [
        "superseded",
        "overlapping",
        "active",
    ]
