"""Pruebas de ejemplo de H-13, H-43, H-46 y H-48 (TASK-229; NFR-GOB-62; PBT-10).

De extremo a extremo por las rutas reales de la aplicación completa
(``tests/gob_platform_support.py``: todas las unidades de ``platform_units()``, PostgreSQL 16
como ``vigia_app`` y LocalStack), como lo harán el nodo y U-05:

- **H-13** (BR-GOB-89): un hallazgo reenviado con la misma clave es ``accepted_duplicate`` con el
  ``Receipt`` original; con contenido distinto, ``idempotency_conflict``; la cadena tiene uno;
- **H-43** (BR-GOB-13, 15): las tres preguntas, aprobada y rechazada con su ``failed_criterion``,
  ambas registradas y listadas;
- **H-46** (BR-GOB-58 a 66): alta con CSR, certificado de 365 días propio del nodo, rotación con
  solapamiento de 24 horas y revocación que rechaza la petición siguiente; cada paso en la cadena
  con su autor y su fecha;
- **H-48** (BR-GOB-101 a 103, nota T-05): versión objetivo publicada y los tres resultados
  ``applied``, ``reverted`` (la cola local entra entera después) y ``failed``, proyectados en el
  inventario. El nodo es el de la plataforma con su certificado y presentaciones del contrato
  validadas con el lector estricto de U-01: el ``SimulatedNode`` del kit exige un conjunto sellado
  y ``GET conformance-profile``, fuera de v1 (A-51).

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from cryptography import x509

from tests.factories import uuid7
from tests.fleet_credentials_support import csr_pem, local_ip, new_key
from tests.fleet_versions_support import update_result
from tests.gob_platform_support import (
    GobPlatform,
    GobZone,
    Onboarding,
    detail_of,
    ok,
    stamp,
)
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = pytest.mark.integration

DAY = dt.timedelta(days=1)


# --- H-13 ----------------------------------------------------------------------------------------


def test_h13_a_resent_finding_is_a_duplicate_and_a_rewrite_a_conflict(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    finding = flow.finding(zone)
    first = ok(flow.post_finding(zone, finding))
    again = ok(flow.post_finding(zone, finding))
    assert again["status"] == "accepted_duplicate"
    assert {k: v for k, v in again.items() if k != "status"} == {
        k: v for k, v in first.items() if k != "status"
    }
    rewritten = {**finding, "max_confidence": 0.95}
    conflict = flow.post_finding(zone, rewritten)
    assert conflict.status_code == 409, conflict.text
    assert (conflict.json()["code"], conflict.json()["retryable"]) == (
        "idempotency_conflict",
        False,
    )
    (record,) = gob.records(zone.organization_id, "finding_received")
    assert json.loads(record["content_json"])["max_confidence"] == finding["max_confidence"]
    assert len(gob.events(zone.organization_id, "finding_received")) == 1


# --- H-43 ----------------------------------------------------------------------------------------


def test_h43_the_three_questions_admit_or_reject_and_both_are_recorded(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone(enrolled=False)  # ``coexistence`` ya admitida en la planta
    rejected = gob.call(
        "POST",
        f"/plants/{zone.plant_id}/admissions",
        cookie=zone.admin,
        json_body={
            "family": "guard_bypass",
            "answers": {"standard": False, "remedy": True, "subject": True},
        },
    )
    assert detail_of(rejected) == (400, "invalid_request", "catalog_admission_rejected")
    admitted = ok(
        gob.call(
            "POST",
            f"/plants/{zone.plant_id}/admissions",
            cookie=zone.admin,
            json_body={
                "family": "guard_bypass",
                "answers": {"standard": True, "remedy": True, "subject": True},
                "justification_es": "El resguardo frontal tiene procedimiento escrito.",
            },
        ),
        201,
    )
    assert admitted["result"] == "admitted"
    listed = ok(gob.call("GET", f"/plants/{zone.plant_id}/admissions", cookie=zone.admin))
    guard = [item for item in listed["admissions"] if item["family"] == "guard_bypass"]
    assert sorted((item["result"], item["failed_criterion"]) for item in guard) == [
        ("admitted", None),
        ("rejected", "standard"),
    ]
    recorded = [
        content
        for content in gob.contents(zone.organization_id, "standard_admission_test")
        if content["family"] == "guard_bypass"
    ]
    assert sorted((c["result"], c.get("failed_criterion")) for c in recorded) == [
        ("admitted", None),
        ("rejected", "standard"),
    ]


# --- H-46 ----------------------------------------------------------------------------------------


def _actor(gob: GobPlatform, record: Any) -> Any:
    (row,) = gob.fetch(
        "SELECT actor_id, actor_kind, occurred_at FROM ledger.ledger_record WHERE record_id = $1",
        record["record_id"],
    )
    return row


def test_h46_enroll_and_rotate_with_a_24_hour_overlap(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()  # declarado por el instalador y dado de alta con su CSR
    certificate = zone.cert
    certificate.verify_directly_issued_by(gob.root)
    lifetime = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    assert dt.timedelta(days=365) <= lifetime <= dt.timedelta(days=365, minutes=5)
    common = certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    assert [attribute.value for attribute in common] == [str(zone.node)]
    (enrolled,) = gob.records(zone.organization_id, "node_enrolled")
    assert _actor(gob, enrolled)["actor_kind"] == "node"

    # Rotación: el certificado nuevo, y el anterior sigue valiendo 24 horas.
    gob.advance(60)
    rotated = ok(
        gob.node_call(
            "POST",
            NodeRoute.CREDENTIAL_ROTATION.path,
            certificate=certificate,
            body={
                "node_id": str(zone.node),
                "certificate_signing_request": csr_pem(str(zone.node), key=new_key()),
                "server_certificate_signing_request": csr_pem(
                    str(zone.node), key=new_key(), names=[local_ip()]
                ),
                "requested_at": stamp(gob.now()),
            },
        )
    )
    new = x509.load_pem_x509_certificate(rotated["certificate"].encode())
    assert new.serial_number != certificate.serial_number
    statuses = gob.fetch(
        "SELECT status FROM fleet.node_credential WHERE node_id = $1 ORDER BY issued_at",
        zone.node,
    )
    assert [row["status"] for row in statuses] == ["overlapping", "active"]
    zone.certificate = certificate
    assert flow.post_heartbeat(zone).status_code == 200  # el anterior, dentro del solapamiento
    zone.certificate = new
    gob.advance(30)
    assert flow.post_heartbeat(zone).status_code == 200
    (record,) = gob.records(zone.organization_id, "node_credential_rotated")
    assert _actor(gob, record)["occurred_at"] is not None

    # Pasadas las 24 horas, el anterior ya no entra; el nuevo, sí.
    gob.advance(DAY.total_seconds() + 60)
    zone.certificate = certificate
    stale = flow.post_heartbeat(zone)
    assert stale.status_code == 401, stale.text
    zone.certificate = new
    assert flow.post_heartbeat(zone).status_code == 200


def test_h46_a_revocation_is_seen_by_the_next_request_with_author_and_date(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    assert flow.post_heartbeat(zone).status_code == 200
    reason = "Equipo retirado tras la sustitución de la placa"
    ok(flow.as_installer(zone, "POST", f"/nodes/{zone.node}/revocation", {"reason_es": reason}))
    refused = flow.post_heartbeat(zone)
    assert (refused.status_code, refused.json()["code"]) == (401, "node_revoked")
    (revoked,) = gob.records(zone.organization_id, "node_revoked")
    actor = _actor(gob, revoked)
    assert actor["actor_id"] == zone.installer_id and actor["occurred_at"] is not None
    detail = ok(gob.call("GET", f"/fleet/nodes/{zone.node}", cookie=zone.admin))
    assert "revoked" in json.dumps(detail)


# --- H-48 ----------------------------------------------------------------------------------------

PREVIOUS = "1.0.2"
TARGET = "1.0.3"


def _publish(gob: GobPlatform, flow: Onboarding, zone: GobZone) -> Any:
    now = gob.now()
    return ok(
        flow.as_installer(
            zone,
            "POST",
            "/fleet/target-versions",
            {
                "plant_id": str(zone.plant_id),
                "node_ids": [str(zone.node)],
                "target_version": TARGET,
                "maintenance_window": {
                    "from": stamp(now + dt.timedelta(hours=1)),
                    "to": stamp(now + dt.timedelta(hours=3)),
                },
            },
        ),
        201,
    )


def _report(gob: GobPlatform, zone: GobZone, outcome: str) -> Any:
    document = update_result(
        organization_id=zone.organization_id,
        plant_id=zone.plant_id,
        node_id=zone.node,
        update_result_id=uuid7(),
        verified_at=gob.now() - dt.timedelta(seconds=30),
        target_version=TARGET,
        previous_version=PREVIOUS,
        outcome=outcome,
    )
    return gob.node_call(
        "POST",
        NodeRoute.UPDATE_RESULT.path,
        certificate=zone.cert,
        body=document,
        headers={"Idempotency-Key": document["update_result_id"]},
    )


def _inventory(gob: GobPlatform, zone: GobZone) -> Any:
    (row,) = gob.fetch(
        "SELECT target_version, last_update_result FROM fleet.node_inventory WHERE node_id = $1",
        zone.node,
    )
    return row


@pytest.mark.parametrize("outcome", ["applied", "reverted", "failed"])
def test_h48_the_target_version_and_each_result_reach_the_inventory(
    gob: GobPlatform, outcome: str
) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    assert flow.post_heartbeat(zone, flow.heartbeat(zone, software_version=PREVIOUS)).status_code
    _publish(gob, flow, zone)
    beat = ok(flow.post_heartbeat(zone, flow.heartbeat(zone, software_version=PREVIOUS)))
    assert TARGET in json.dumps(beat)  # el nodo la recibe en la respuesta al latido

    # La cola del nodo: hallazgos ya construidos (y con su clip subido) antes de actualizar.
    queued = [flow.finding(zone) for _ in range(3)] if outcome == "reverted" else []
    reported = _report(gob, zone, outcome)
    assert reported.status_code == 200, reported.text
    row = _inventory(gob, zone)
    assert (row["target_version"], row["last_update_result"]) == (TARGET, outcome)
    (record,) = gob.records(zone.organization_id, "update_result_received")
    content = json.loads(record["content_json"])
    assert (content["result"], content["target_version"]) == (outcome, TARGET)
    detail = ok(gob.call("GET", f"/fleet/nodes/{zone.node}", cookie=zone.admin))
    assert outcome in json.dumps(detail)

    if outcome == "reverted":
        # BR-GOB-103: revertir conserva la cola; entra entera y sin pérdida después.
        pending = flow.heartbeat(
            zone,
            software_version=PREVIOUS,
            local_queue={"pending": len(queued), "dead_letter": [], "retained_sent": 0},
        )
        assert flow.post_heartbeat(zone, pending).status_code == 200
        for finding in queued:
            assert flow.post_finding(zone, finding).status_code == 200
        assert len(gob.records(zone.organization_id, "finding_received")) == len(queued)
