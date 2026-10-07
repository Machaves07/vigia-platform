"""G-1 · Nodo revocado que sigue enviando (business-rules §12; SECURITY-11).

**Qué intenta**: seguir escribiendo hallazgos, latidos y lecturas del catálogo con el certificado de
un nodo ya revocado (equipo robado o retirado que conserva su clave).

**Qué lo detiene**:

- BR-GOB-66: la revocación es inmediata y se comprueba en la aplicación en **cada** petición de
  las rutas del contrato: la siguiente responde ``node_revoked`` (401, permanente), sea hallazgo,
  latido o catálogo; la credencial queda ``revoked`` (la lista de revocación del balanceador es la
  segunda capa, ``tests/integration/test_fleet_revocation_list_localstack.py``);
- BR-GOB-88: el certificado ya no da alcance sobre ninguna zona: nada de lo que envía se escribe;
- BR-GOB-96: el rechazo deja rastro: la línea de registro de la petición con ``rejection_code`` y
  el ``node_id`` del certificado presentado (nunca el contenido), y la revocación misma queda en
  la cadena de la planta con su autor, su motivo y su evento.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import logging

import pytest

from tests.gob_platform_support import GobPlatform, Onboarding, ok
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = pytest.mark.integration

REASON = "Equipo retirado de la línea 2 tras su sustitución"


def _node_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Los campos de cada línea de petición de nodo (``NodeResponses.record``)."""
    return [
        dict(getattr(record, "vigia_fields", {}))
        for record in caplog.records
        if record.name == "vigia.node_api" and "route" in getattr(record, "vigia_fields", {})
    ]


def test_g01_a_revoked_node_is_rejected_on_its_next_request_and_writes_nothing(
    gob: GobPlatform, caplog: pytest.LogCaptureFixture
) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    accepted = flow.post_finding(zone, flow.finding(zone))
    assert accepted.status_code == 200, accepted.text
    pending = flow.finding(zone)  # con su clip ya subido, antes de la revocación

    ok(flow.as_installer(zone, "POST", f"/nodes/{zone.node}/revocation", {"reason_es": REASON}))
    findings_before = len(gob.records(zone.organization_id, "finding_received"))

    caplog.set_level(logging.INFO)
    attempts = {
        "hallazgo": flow.post_finding(zone, pending),
        "latido": flow.post_heartbeat(zone),
        "catálogo": gob.node_call(
            "GET", f"/api/nodes/zones/{zone.zone_id}/catalog", certificate=zone.cert
        ),
        "evento": flow.post_event(zone, flow.event(zone)),
    }
    for what, response in attempts.items():
        assert response.status_code == 401, (what, response.text)
        body = response.json()
        assert body["code"] == "node_revoked" and body["retryable"] is False, (what, body)

    # BR-GOB-88: nada de lo que envió después se escribió.
    assert len(gob.records(zone.organization_id, "finding_received")) == findings_before
    assert gob.records(zone.organization_id, "observability_event_received") == []
    # BR-GOB-96: cada intento deja su línea con el código y el nodo del certificado.
    lines = [line for line in _node_lines(caplog) if line.get("rejection_code") == "node_revoked"]
    assert len(lines) == len(attempts)
    assert {line["node_id"] for line in lines} == {str(zone.node)}
    assert {line["route"] for line in lines} == {
        NodeRoute.FINDING.path,
        NodeRoute.HEARTBEAT.path,
        NodeRoute.ZONE_CATALOG.path,
        NodeRoute.OBSERVABILITY_EVENT.path,
    }
    for line in lines:
        assert set(line) <= {
            "route",
            "status",
            "correlation_id",
            "duration_ms",
            "rejection_code",
            "organization_id",
            "plant_id",
            "node_id",
            "zone_id",
        }, line


def test_g01_the_revocation_is_in_the_chain_with_author_reason_and_event(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    # ``fleet.manage`` es de la columna del instalador (bajo su concesión).
    ok(flow.as_installer(zone, "POST", f"/nodes/{zone.node}/revocation", {"reason_es": REASON}))
    (record,) = gob.records(zone.organization_id, "node_revoked")
    assert record["plant_id"] == zone.plant_id
    (row,) = gob.fetch(
        "SELECT actor_id FROM ledger.ledger_record WHERE record_id = $1", record["record_id"]
    )
    assert row["actor_id"] == zone.installer_id
    assert [event["node_id"] for event in gob.events(zone.organization_id, "node_revoked")] == [
        str(zone.node)
    ]
    statuses = gob.fetch(
        "SELECT n.status AS node, c.status AS credential FROM identity.node_identity AS n"
        " JOIN fleet.node_credential AS c ON c.node_id = n.node_id WHERE n.node_id = $1",
        zone.node,
    )
    assert [(row["node"], row["credential"]) for row in statuses] == [("revoked", "revoked")]
    # La revocación no se deshace con otra petición del nodo: sigue rechazado.
    again = flow.post_heartbeat(zone)
    assert again.status_code == 401 and again.json()["code"] == "node_revoked"
