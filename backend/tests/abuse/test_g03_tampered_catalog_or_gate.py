"""G-3 · Catálogo o compuerta manipulados en el nodo (business-rules §12; SECURITY-11).

**Qué intenta**: hacer que el nodo evalúe con un estándar, un umbral o una compuerta alterados
(editando su caché o interceptando la respuesta) y que la plataforma acepte lo que sale de ahí.

**Qué lo detiene**:

- BR-GOB-06: cada versión de catálogo y cada estado de compuertas viaja firmado por
  ``SigningPort`` con su clave de propósito (``catalog``, ``gate``); el sobre alterado no
  verifica con las claves que el nodo fijó en el alta (``vigia_contracts.signing.verify``):
  cambiar la carga rompe el resumen, y rehacer el resumen rompe la firma;
- BR-GOB-07: el nodo solo aplica un sobre que verifica y que es de su zona;
- BR-GOB-08: la plataforma **revalida** al recibir: un hallazgo que cita una versión del estándar
  que no estuvo vigente en el instante del hecho (o un estándar de otra zona) se rechaza con
  ``schema_invalid`` en ``standard``;
- BR-GOB-92: y revalida la compuerta en el instante del hecho: un nodo convencido de que su zona
  es ``productive`` por una compuerta manipulada recibe ``zone_gate_not_approved``.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from vigia_contracts.canonical import canonical_sha256
from vigia_contracts.signing import KeySet, SignatureInvalidError, verify

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok
from vigia_platform.shared.signing import NODE_PURPOSES

pytestmark = pytest.mark.integration


def _keyset(gob: GobPlatform) -> KeySet:
    """Las claves públicas que el nodo fija en su alta (propósitos de nodo)."""
    keyset = KeySet(gob.clock)
    keyset.pin_initial(
        [
            key.to_contract()
            for purpose in NODE_PURPOSES
            for key in gob.services.signing.public_keys(purpose)
        ]
    )
    return keyset


def _tampered(envelope: dict[str, Any], change: Any, *, rehash: bool) -> dict[str, Any]:
    forged = copy.deepcopy(envelope)
    change(forged["payload"])
    if rehash:
        forged["payload_canonical_sha256"] = canonical_sha256(forged["payload"])
    return forged


def _lower_threshold(payload: dict[str, Any]) -> None:
    payload["thresholds"]["publication"] = 0.99  # ver menos hallazgos


def _open_gate(payload: dict[str, Any]) -> None:
    payload["usage_gate"] = dict(payload["mounting_gate"])
    payload["resulting_mode"] = "productive"


def test_g03_a_tampered_catalog_or_gate_envelope_does_not_verify(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)  # compuerta de montaje aprobada: ``commissioning``
    keyset = _keyset(gob)
    catalog = ok(
        gob.node_call("GET", f"/api/nodes/zones/{zone.zone_id}/catalog", certificate=zone.cert)
    )
    payload = verify(catalog, keyset, "catalog", gob.clock)
    assert payload["zone_id"] == str(zone.zone_id)
    beat = ok(flow.post_heartbeat(zone, flow.heartbeat(zone)))
    (gate,) = beat["gate_states"]
    assert verify(gate, keyset, "gate", gob.clock)["resulting_mode"] == "commissioning"

    for envelope, change, purpose in (
        (catalog, _lower_threshold, "catalog"),
        (gate, _open_gate, "gate"),
    ):
        # Cambiar la carga rompe el resumen; rehacer el resumen rompe la firma.
        for rehash in (False, True):
            with pytest.raises(SignatureInvalidError):
                verify(_tampered(envelope, change, rehash=rehash), keyset, purpose, gob.clock)
    # Una clave de otro propósito no sirve: un sobre de compuerta no pasa por catálogo.
    with pytest.raises(SignatureInvalidError):
        verify(gate, keyset, "catalog", gob.clock)


def _rejected_on_standard(flow: Onboarding, zone: GobZone, document: dict[str, Any]) -> None:
    response = flow.post_finding(zone, document)
    assert response.status_code == 422, response.text
    body = response.json()
    assert (body["code"], body["field"]) == ("schema_invalid", "standard")


def test_g03_the_platform_revalidates_the_standard_and_the_gate_on_receipt(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    findings = len(gob.records(zone.organization_id, "finding_received"))

    # BR-GOB-08: una versión del estándar que nunca estuvo vigente…
    _rejected_on_standard(flow, zone, flow.finding(zone, standard_version=2))
    # …o el estándar de otra zona (aunque sea de la misma planta).
    other = flow.zone(within=zone)
    foreign = flow.finding(zone)
    foreign["standard"] = {"standard_id": str(other.standard_id), "version": 1}
    _rejected_on_standard(flow, zone, foreign)
    assert len(gob.records(zone.organization_id, "finding_received")) == findings

    # BR-GOB-92: la zona de ``other`` no tiene compuerta de uso; su nodo, convencido por un
    # sobre manipulado de que es ``productive``, envía un hallazgo bien formado.
    flow.mount(other)
    response = flow.post_finding(other, flow.finding(other))
    assert response.status_code == 403, response.text
    assert (response.json()["code"], response.json()["field"]) == (
        "zone_gate_not_approved",
        "zone_id",
    )
    assert len(gob.records(zone.organization_id, "finding_received")) == findings
    # Lo que sí cumple sigue entrando: la revalidación no es un bloqueo general.
    assert flow.post_finding(zone, flow.finding(zone)).status_code == 200
