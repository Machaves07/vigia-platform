"""G-2 · Código de alta reutilizado, vencido o sustituido en otro equipo (business-rules §12).

**Qué intenta**: dar de alta un equipo distinto (otra huella, otra dirección) con un código de
alta que ya se usó, que una emisión posterior dejó ``superseded``, que venció, o con el código
vigente de un nodo que vuelve a darse de alta.

**Qué lo detiene**:

- BR-GOB-58: el código es de un solo uso, vale 24 horas y está ligado al ``node_id`` que el
  instalador declaró (el nombre común de la CSR, nota U03-H-13);
- BR-GOB-59: se guarda solo su hash; el valor en claro no vuelve a aparecer en ninguna respuesta,
  registro del expediente, línea de registro ni evento;
- BR-GOB-60: cada emisión deja la anterior ``superseded`` (a lo sumo un código ``active``); en la
  re-alta de un nodo revocado, la huella de hardware debe coincidir (nota U03-H-04): el código
  vigente presentado desde **otro equipo** se rechaza y **no se consume**;
- BR-GOB-61: cada intento se registra, aceptado o no: ``fleet.enrollment_attempt`` con
  ``source_ip_hash`` (nunca la dirección) y ``enrollment_attempt_rejected`` en la cadena de la
  planta con ``presented_code_hash``. Hacia fuera, el código usado, el sustituido y el de otro
  equipo reciben el mismo rechazo genérico (``enrollment_code_invalid``, 401); solo quien
  presenta el código correcto ya vencido sabe que venció (``enrollment_code_expired``).

Nota de TASK-229: el diseño citaba el intento «con ``hardware_fingerprint``»; la huella viaja ya
resumida (64 hexadecimales del contrato), se guarda tal cual en el intento y nunca sale en una
línea de registro.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok
from tests.platform_support import comparable
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = pytest.mark.integration

HEX64 = re.compile(r"[0-9a-f]{64}")
DAY_AND_A_SECOND = 24 * 3600 + 1
REASON = "Equipo enviado a reparación por una falla de la fuente"


def _attempt(
    gob: GobPlatform,
    flow: Onboarding,
    zone: GobZone,
    code: str,
    *,
    fingerprint: str | None = None,
    address: str | None = None,
) -> Any:
    """Un intento de alta; sin ``fingerprint``, de **otro equipo** (otra huella)."""
    body = flow.enrollment_body(zone, code, fingerprint=fingerprint or secrets.token_hex(32))
    return gob.node_call(
        "POST",
        NodeRoute.ENROLLMENT.path,
        certificate=None,
        body=body,
        address=address or gob.address(),
    )


def _rejections(gob: GobPlatform, zone: GobZone) -> list[Any]:
    return gob.fetch(
        "SELECT result, hardware_fingerprint, source_ip_hash, presented_code_hash,"
        " ledger_record_id FROM fleet.enrollment_attempt WHERE node_id = $1"
        " AND result <> 'accepted' ORDER BY attempted_at",
        zone.node,
    )


def test_g02_used_and_superseded_codes_get_the_same_generic_rejection(
    gob: GobPlatform, caplog: pytest.LogCaptureFixture
) -> None:
    flow = Onboarding(gob)
    caplog.set_level(logging.INFO)
    used_zone = flow.zone(enrolled=False)
    used_code = flow.code(used_zone)
    flow.enroll(used_zone, used_code)
    superseded_zone = flow.zone(enrolled=False)
    superseded_code = flow.code(superseded_zone)
    flow.code(superseded_zone)
    statuses = gob.fetch(
        "SELECT status FROM fleet.enrollment_code WHERE node_id = $1 ORDER BY issued_at",
        superseded_zone.node,
    )
    assert [row["status"] for row in statuses] == ["superseded", "active"]  # BR-GOB-60

    cases = {"usado": (used_zone, used_code), "sustituido": (superseded_zone, superseded_code)}
    addresses = {name: gob.address() for name in cases}
    answers = {
        name: _attempt(gob, flow, zone, code, address=addresses[name])
        for name, (zone, code) in cases.items()
    }
    unknown = _attempt(gob, flow, superseded_zone, "ABCDEFGH2345")
    assert {response.status_code for response in answers.values()} == {401}
    # El mismo rechazo genérico que un código que nunca existió (salvo ``correlation_id``).
    assert comparable(answers["usado"]) == comparable(answers["sustituido"]) == comparable(unknown)
    assert answers["usado"].json()["code"] == "enrollment_code_invalid"

    for name, (zone, code) in cases.items():
        attempt = _rejections(gob, zone)[0]
        assert HEX64.fullmatch(attempt["source_ip_hash"]), name
        assert HEX64.fullmatch(attempt["presented_code_hash"]), name
        (record,) = gob.fetch(
            "SELECT record_type, content_json FROM ledger.ledger_record WHERE record_id = $1",
            attempt["ledger_record_id"],
        )
        assert record["record_type"] == "enrollment_attempt_rejected"
        content = json.loads(record["content_json"])
        assert content["source_ip_hash"] == attempt["source_ip_hash"]
        assert content["result"] == attempt["result"]
        assert code not in record["content_json"], name
        assert addresses[name] not in record["content_json"], name

    # BR-GOB-59: ninguna línea de registro ni evento lleva el código ni la dirección.
    logged = json.dumps([getattr(record, "vigia_fields", {}) for record in caplog.records])
    events = json.dumps(
        [
            row["payload"]
            for zone, _ in cases.values()
            for row in gob.fetch(
                "SELECT payload::text AS payload FROM shared.outbox_event"
                " WHERE organization_id = $1",
                zone.organization_id,
            )
        ]
    )
    for name, (_, code) in cases.items():
        for secret in (code, addresses[name]):
            assert secret not in logged and secret not in events, name


def test_g02_an_expired_code_only_tells_its_holder_and_never_enrolls(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone(enrolled=False)
    code = flow.code(zone)
    gob.advance(DAY_AND_A_SECOND)  # BR-GOB-58: 24 horas
    expired = _attempt(gob, flow, zone, code)
    assert expired.status_code == 401 and expired.json()["code"] == "enrollment_code_expired"
    (attempt,) = _rejections(gob, zone)
    assert attempt["result"] == "enrollment_code_expired"
    # Sin código vigente: nada se dio de alta.
    (row,) = gob.fetch("SELECT status FROM identity.node_identity WHERE node_id = $1", zone.node)
    assert row["status"] == "declared"
    assert gob.fetch("SELECT 1 FROM fleet.node_credential WHERE node_id = $1", zone.node) == []


def test_g02_the_live_code_of_a_re_enrollment_from_another_device_is_not_consumed(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()  # alta con ``zone.hardware_fingerprint``
    ok(flow.as_installer(zone, "POST", f"/nodes/{zone.node}/revocation", {"reason_es": REASON}))
    code = flow.code(zone)  # re-alta del mismo ``node_id`` (nota U03-H-04)

    other = _attempt(gob, flow, zone, code)
    assert other.status_code == 401 and other.json()["code"] == "enrollment_code_invalid"
    (attempt,) = _rejections(gob, zone)
    assert attempt["result"] == "enrollment_code_invalid"
    assert attempt["hardware_fingerprint"] != zone.hardware_fingerprint
    statuses = gob.fetch(
        "SELECT status FROM fleet.enrollment_code WHERE node_id = $1 ORDER BY issued_at", zone.node
    )
    assert [row["status"] for row in statuses][-1] == "active"  # no se consumió

    # El equipo verdadero, con su huella, sí vuelve con ese mismo código.
    again = _attempt(gob, flow, zone, code, fingerprint=zone.hardware_fingerprint)
    assert again.status_code == 200, again.text
    listed = ok(flow.as_installer(zone, "GET", f"/nodes/{zone.node}/enrollment-attempts"))
    assert sorted(item["result"] for item in listed["attempts"]) == [
        "accepted",
        "accepted",
        "enrollment_code_invalid",
    ]
    # La lista muestra la huella presentada, nunca el hash del origen ni el código.
    assert "source_ip_hash" not in json.dumps(listed) and code not in json.dumps(listed)
