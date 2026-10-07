"""Pruebas de ejemplo de H-49, H-50, H-51 y H-52 (TASK-229; NFR-GOB-62; PBT-10).

De extremo a extremo por las rutas reales de la aplicación completa
(``tests/gob_platform_support.py``: todas las unidades de ``platform_units()``, PostgreSQL 16
como ``vigia_app`` y LocalStack), como lo harán el nodo, el instalador y U-05:

- **H-49** (BR-GOB-19, 23, 24 y nota T-02): sin acta de alcance no hay montaje (``no_capture``, el
  walk-test no abre); el acta sin la declaración de difuminado o sin su captura
  (``blur_check_capture``, subida de verdad a ``vigia-evidence``) es ``catalog_blur_not_verified``;
  con las dos, la compuerta de montaje se aprueba y la zona pasa a ``commissioning`` (también en
  el sobre firmado que recibe el nodo);
- **H-50** (BR-GOB-25 a 30): acuerdo a tres firmas con ``copasst``; sin acuerdo aprobado, un
  hallazgo es ``zone_gate_not_approved``; desde la aprobación los hallazgos persisten. El acta de
  comisionamiento cerrada que exige la aprobación (BR-GOB-29) se siembra por repositorio hasta que
  su cierre por la ruta (TASK-216, VIG-158) esté en ``main``;
- **H-52** (BR-GOB-41 a 43, NFR-GOB-44): oclusión por turno: ``verified`` con el evento de la
  cámara en la ventana; ``failed`` solo vencida la fecha límite (5 minutos y 30 s); ``declared``
  con motivo, que nunca se presenta como ``verified``.

H-51 (el falso negativo que bloquea el cierre y la tasa de falsas alarmas en el acta) necesita la
ruta de cierre del acta de TASK-216 (VIG-158).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from vigia_contracts.signing import KeySet, verify

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, detail_of, ok, stamp
from vigia_platform.shared.signing import NODE_PURPOSES

pytestmark = pytest.mark.integration

REASON = "La cámara lateral quedó tapada por la grúa durante el turno de la mañana"


def _gates(gob: GobPlatform, zone: GobZone) -> dict[str, Any]:
    body: dict[str, Any] = ok(gob.call("GET", f"/zones/{zone.zone_id}/gates", cookie=zone.admin))
    return body


# --- H-49 ----------------------------------------------------------------------------------------


def test_h49_no_scope_record_no_mounting_and_both_blur_parts_to_commission(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    gates = _gates(gob, zone)
    assert (gates["mounting"]["status"], gates["resulting_mode"]) == ("pending", "no_capture")
    # Sin montaje, el walk-test no abre.
    closed = flow.as_installer(
        zone, "POST", f"/zones/{zone.zone_id}/walk-tests", {"passes_per_cell": 3}
    )
    assert detail_of(closed)[2] == "catalog_mounting_gate_pending"

    path = f"/zones/{zone.zone_id}/gates/mounting/scope-record"
    capture = flow.document(zone, "blur_check_capture")
    for blur in (
        None,
        {"declared": False, "capture_document_ref": capture},
        {"declared": True},
    ):
        body = flow.scope_record_body(zone)
        if blur is None:
            body.pop("blur_verification")
        else:
            body["blur_verification"] = blur
        refused = flow.as_installer(zone, "POST", path, body)
        assert detail_of(refused) == (409, "conflict", "catalog_blur_not_verified"), refused.text
    assert _gates(gob, zone)["resulting_mode"] == "no_capture"

    filed = flow.mount(zone)  # declaración y captura difuminada subida
    assert filed["gates"]["mounting"]["status"] == "approved"
    assert filed["gates"]["resulting_mode"] == "commissioning"
    record = filed["record_id"]
    assert _gates(gob, zone)["mounting"]["record_id"] == record
    # El nodo lo sabe por el sobre firmado de su latido.
    keyset = KeySet(gob.clock)
    keyset.pin_initial(
        [k.to_contract() for p in NODE_PURPOSES for k in gob.services.signing.public_keys(p)]
    )
    (gate,) = ok(flow.post_heartbeat(zone))["gate_states"]
    assert verify(gate, keyset, "gate", gob.clock)["resulting_mode"] == "commissioning"
    changes = gob.contents(zone.organization_id, "gate_state_changed")
    assert [c["resulting_mode"] for c in changes][-1] == "commissioning"


# --- H-50 ----------------------------------------------------------------------------------------


def test_h50_three_signatures_with_copasst_open_use_and_findings_persist_from_then(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    flow.closed_record(zone)  # TASK-216 (VIG-158): su cierre por la ruta, todavía sin fusionar
    flow.plant_policy(zone)
    ok(flow.signatory_policy(zone))
    before = flow.finding(zone)
    refused = flow.post_finding(zone, before)
    assert (refused.status_code, refused.json()["code"]) == (403, "zone_gate_not_approved")

    signers = flow.signatories(zone)
    agreement = ok(flow.agreement(zone, signers), 201)
    assert {s["role"] for s in agreement["signatories"]} == {
        "coordinator_sst",
        "plant_manager",
        "copasst",
    }
    for signer in signers:
        assert flow.confirm(agreement["agreement_id"], signer).status_code == 201
    approved = ok(flow.approve(zone, agreement["agreement_id"]))
    assert approved["agreement"]["status"] == "approved"
    assert approved["gates"]["resulting_mode"] == "productive"
    approved_at = approved["agreement"]["approved_at"]
    activated = gob.events(zone.organization_id, "zone_activated")
    assert [event["zone_id"] for event in activated] == [str(zone.zone_id)]

    gob.advance(600)
    accepted = ok(flow.post_finding(zone, flow.finding(zone)))
    assert accepted["status"] == "accepted"
    (record,) = gob.records(zone.organization_id, "finding_received")
    assert json.loads(record["content_json"])["zone_id"] == str(zone.zone_id)
    # Lo de antes de la aprobación sigue fuera (BR-GOB-30: desde ese instante, nunca antes).
    assert before["node_time"]["started_at"] < approved_at
    late = flow.post_finding(zone, before)
    assert (late.status_code, late.json()["code"]) == (403, "zone_gate_not_approved")


# --- H-52 ----------------------------------------------------------------------------------------


def _occlusion(
    flow: Onboarding,
    zone: GobZone,
    session_id: str,
    camera: int,
    ended_at: dt.datetime,
    reason: str | None = None,
) -> Any:
    body: dict[str, Any] = {
        "camera_id": str(zone.cameras[camera]),
        "started_at": stamp(ended_at - dt.timedelta(seconds=20)),
        "ended_at": stamp(ended_at),
    }
    if reason is not None:
        body["declared_reason_es"] = reason
    return flow.as_installer(zone, "POST", f"/walk-tests/{session_id}/occlusion-tests", body)


def _tests(flow: Onboarding, zone: GobZone) -> list[dict[str, Any]]:
    current = ok(flow.as_installer(zone, "GET", f"/zones/{zone.zone_id}/walk-tests/current"))
    tests: list[dict[str, Any]] = current["session"]["occlusion_tests"]
    return tests


def test_h52_occlusion_by_turn_verified_failed_only_after_the_deadline_and_declared(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    # Un solo reloj (retro 14): el simulado vuelve a la hora de la base, porque la ventana se
    # compara con ``received_at``, que pone la base; 20 s de ventaja, dentro de la tolerancia de
    # ±30 s, dejan los hechos después de la asignación del nodo.
    gob.resync()
    gob.advance(20)
    session = ok(
        flow.as_installer(
            zone, "POST", f"/zones/{zone.zone_id}/walk-tests", {"passes_per_cell": 3}
        ),
        201,
    )
    session_id = session["session_id"]
    ended = gob.now() - dt.timedelta(seconds=2)

    # Cámara 1 (redundante): el nodo ve la oclusión en la ventana y la zona se conserva.
    event = flow.event(zone, ended - dt.timedelta(seconds=8), camera=1)
    assert flow.post_event(zone, event).status_code == 200
    first = ok(_occlusion(flow, zone, session_id, 1, ended), 201)
    assert first["deadline"] == stamp(ended + dt.timedelta(minutes=5))
    # Zona redundante: su conservación solo se afirma vencida la fecha límite (P2).
    gob.advance(2 * 60)
    assert _tests(flow, zone)[0]["verification"] == "pending"

    # Cámara 0 (requerida), con su propia ventana: silencio.
    silent_end = gob.now() - dt.timedelta(seconds=2)
    second = ok(_occlusion(flow, zone, session_id, 0, silent_end), 201)
    assert second["verification"] == "pending"
    gob.advance(4 * 60)  # la cámara 1 ya venció; la 0, todavía no: el silencio aún no es fallo
    by_camera = {test["camera_id"]: test for test in _tests(flow, zone)}
    verified = by_camera[str(zone.cameras[1])]
    assert verified["verification"] == "verified", verified
    assert verified["correlated_event_ids"] == [event["event_id"]]
    assert by_camera[str(zone.cameras[0])]["verification"] == "pending"

    gob.advance(2 * 60)  # vencida su fecha límite (5 min y la tolerancia de 30 s)
    by_camera = {test["camera_id"]: test for test in _tests(flow, zone)}
    failed = by_camera[str(zone.cameras[0])]
    assert (failed["verification"], failed["failure_reason"]) == (
        "failed",
        "no_observability_events_in_window",
    )

    # Tras el fallo, otra prueba de la cámara 0, declarada con motivo: nunca ``verified``.
    retry_end = gob.now() - dt.timedelta(seconds=2)
    ok(_occlusion(flow, zone, session_id, 0, retry_end), 201)
    declared = ok(_occlusion(flow, zone, session_id, 0, retry_end, reason=REASON), 201)
    assert (declared["verification"], declared["declared_reason_es"]) == ("declared", REASON)
    tests = [t for t in _tests(flow, zone) if t["camera_id"] == str(zone.cameras[0])]
    assert [t["verification"] for t in tests] == ["failed", "declared"]
    results = gob.contents(zone.organization_id, "occlusion_test_result")
    assert sorted(r["verification"] for r in results) == ["declared", "failed", "verified"]
