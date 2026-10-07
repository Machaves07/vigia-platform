"""G-4 · Hallazgo de una zona sin acuerdo de uso (business-rules §12; P9).

**Qué intenta**: persistir hallazgos o detecciones antes de que la compuerta de uso esté
aprobada, por un nodo mal configurado o a propósito (el nodo dice ``productive``).

**Qué lo detiene**:

- BR-GOB-92: la compuerta se evalúa en el **instante del hecho**: un hallazgo o una detección de
  una zona sin uso ``approved`` en ese instante se rechaza con ``zone_gate_not_approved`` (403,
  ``field = zone_id``), aunque el nodo lo haya enviado; los eventos de observabilidad sí se
  aceptan en cualquier modo, porque describen al observador;
- BR-GOB-30: aprobado el acuerdo, los hallazgos persisten **desde** ese instante y nunca antes:
  un hecho anterior a la aprobación sigue rechazado aunque llegue después;
- BR-GOB-96: el rechazo escribe ``ingest_rejected`` en la cadena de la planta con identificadores
  y código, **nunca** el contenido rechazado, y una entrada ``ingest_rejected`` en la auditoría.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.gob_platform_support import GobPlatform, Onboarding

pytestmark = pytest.mark.integration

INGEST_REJECTED_FIELDS = {
    "node_id",
    "zone_id",
    "record_kind",
    "code",
    "correlation_id",
    "received_at",
}
CONTENT_KEYS = ("standard", "cameras", "signals", "episode", "max_confidence", "family")


def _gate_rejected(response: object) -> None:
    assert response.status_code == 403, response.text  # type: ignore[attr-defined]
    body = response.json()  # type: ignore[attr-defined]
    assert (body["code"], body["field"], body["retryable"]) == (
        "zone_gate_not_approved",
        "zone_id",
        False,
    )


def test_g04_findings_of_a_zone_without_agreement_are_rejected_and_traced(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)  # ``commissioning``: montaje aprobado, uso pendiente
    assert flow.mode(zone) == "commissioning"

    # El nodo «cree» que su zona es productiva: lo dice su latido, y aun así nada entra.
    assert flow.post_heartbeat(zone).status_code == 200
    _gate_rejected(flow.post_finding(zone, flow.finding(zone)))
    _gate_rejected(flow.post_detection(zone, flow.detection(zone)))
    # El observador sí se describe en cualquier modo.
    assert flow.post_event(zone, flow.event(zone)).status_code == 200

    organization = zone.organization_id
    assert gob.records(organization, "finding_received") == []
    assert gob.records(organization, "detection_for_review_received") == []
    rejected = gob.contents(organization, "ingest_rejected")
    assert [row["record_kind"] for row in rejected] == ["finding", "detection_for_review"]
    for row in rejected:
        assert set(row) == INGEST_REJECTED_FIELDS
        assert (row["zone_id"], row["node_id"], row["code"]) == (
            str(zone.zone_id),
            str(zone.node),
            "zone_gate_not_approved",
        )
        assert not any(key in row for key in CONTENT_KEYS)
    audits = gob.audit_entries(organization, "ingest_rejected")
    assert len(audits) == 2
    assert {entry["scope_zone_id"] for entry in audits} == {zone.zone_id}
    assert gob.events(organization, "finding_received") == []


def test_g04_a_fact_before_the_approval_stays_rejected_after_it(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    before = gob.now() - timedelta(minutes=2)
    early = flow.finding(zone, before)  # su clip, subido en ``commissioning``
    flow.productive(zone)  # aprobación y 10 minutos después
    assert flow.mode(zone) == "productive"

    _gate_rejected(flow.post_finding(zone, early))
    assert flow.post_finding(zone, flow.finding(zone)).status_code == 200
    (row,) = gob.contents(zone.organization_id, "ingest_rejected")
    assert row["code"] == "zone_gate_not_approved"
    assert len(gob.records(zone.organization_id, "finding_received")) == 1
