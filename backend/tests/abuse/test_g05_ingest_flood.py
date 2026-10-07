"""G-5 · Inundación de la ingesta (business-rules §12; NFR-GOB-33; SECURITY-11).

**Qué intenta**: saturar la plataforma con peticiones de un nodo, con altas desde un mismo origen o
con cuerpos enormes.

**Qué lo detiene**:

- BR-GOB-62: límite de tasa **propio por sujeto** (el nodo del certificado) y, en el alta, **por
  origen** (5 cada 15 minutos) y por ``node_id``; superado, ``rate_limited`` transitorio con
  ``retry_after_seconds`` de 1 a 60 (nota de NFR-GOB-33). Los registros de la ingesta comparten
  un cubo de 240 por minuto con ráfaga de 60: con el reloj quieto, **nunca** entran más de 60,
  aunque lleguen a la vez; el cubo de un nodo no toca el de otro;
- BR-GOB-86: límites de cuerpo por tipo sobre el cuerpo descomprimido (256 KB hallazgos y
  detecciones; 64 KB eventos, latidos y resultados de actualización; 16 KB alta, rotación y
  concesiones de subida): uno más que el límite es ``payload_too_large`` permanente;
- BR-GOB-96: fallo cerrado: lo que no entra no deja nada escrito y nunca se acepta de forma
  optimista.

La ráfaga concurrente va en oleadas de 25 peticiones simultáneas: por debajo del mamparo de nodos
(35 por trabajador, NFR-GOB-19), que responde ``temporarily_unavailable`` al instante cuando está
lleno, para medir el límite de tasa y no el mamparo (``tests/integration/
test_bulkhead_concurrency.py``). La frontera de la ráfaga cae **dentro** de una oleada.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import collections
from datetime import timedelta
from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, Onboarding
from vigia_platform.shared.api.declarations import NodeRoute

pytestmark = pytest.mark.integration

WAVE = 25
WAVES = 4
BURST = 60
"""La ráfaga del cubo ``ingest`` (``node_api.limits.NODE_BUDGETS``)."""
ORIGIN_QUARTER = 5
NODE_QUARTER = 5


CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
"""El alfabeto del código de alta (sin 0, O, 1 ni I; BR-GOB-58)."""


def _code(index: int) -> str:
    """Un código bien formado que nunca se emitió (``enrollment_code_invalid``)."""
    digits = ""
    for _ in range(4):
        index, rest = divmod(index, len(CODE_ALPHABET))
        digits += CODE_ALPHABET[rest]
    return "ZZZZZZZZ" + digits


def _assert_rate_limited(response: Any) -> None:
    assert response.status_code == 429, response.text
    body = response.json()
    assert body["code"] == "rate_limited" and body["retryable"] is True
    assert 1 <= body["retry_after_seconds"] <= 60


def test_g05_a_simultaneous_flood_never_gets_more_than_the_burst_in(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone, neighbour = flow.zone(), flow.zone()
    base = gob.now() - timedelta(minutes=5)
    documents = [flow.event(zone, base + timedelta(seconds=i)) for i in range(WAVE * WAVES)]
    statuses: collections.Counter[int] = collections.Counter()
    limited: list[Any] = []
    # El reloj no avanza entre oleadas: el cubo no se repone.
    for wave in range(WAVES):
        batch = documents[wave * WAVE : (wave + 1) * WAVE]
        responses = gob.gather(
            *(
                flow.submit(zone, NodeRoute.OBSERVABILITY_EVENT, document, "event_id")
                for document in batch
            )
        )
        statuses.update(response.status_code for response in responses)
        limited.extend(response for response in responses if response.status_code == 429)
    assert statuses == {200: BURST, 429: WAVE * WAVES - BURST}, statuses
    for response in limited:
        _assert_rate_limited(response)
    written = gob.records(zone.organization_id, "observability_event_received")
    assert len(written) == BURST
    # El cubo es del sujeto: el nodo vecino sigue entrando.
    assert flow.post_event(neighbour, flow.event(neighbour)).status_code == 200


def test_g05_enrollment_is_limited_by_origin_even_when_simultaneous(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone(enrolled=False)
    address = gob.address()
    attempts = ORIGIN_QUARTER + 3
    responses = gob.gather(
        *(
            gob.node_send(
                "POST",
                NodeRoute.ENROLLMENT.path,
                certificate=None,
                body=flow.enrollment_body(zone, _code(index)),
                address=address,
            )
            for index in range(attempts)
        )
    )
    codes = collections.Counter(response.json()["code"] for response in responses)
    assert codes == {"enrollment_code_invalid": ORIGIN_QUARTER, "rate_limited": 3}, codes
    for response in responses:
        if response.status_code == 429:
            _assert_rate_limited(response)
    # Otro origen no hereda el límite del primero…
    other = gob.node_call(
        "POST",
        NodeRoute.ENROLLMENT.path,
        certificate=None,
        body=flow.enrollment_body(zone, _code(999)),
    )
    # …pero el ``node_id`` ya agotó sus 5 intentos del cuarto de hora.
    _assert_rate_limited(other)


def test_g05_enrollment_is_limited_by_node_id_across_origins(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone(enrolled=False)
    codes = []
    for index in range(NODE_QUARTER + 2):
        response = gob.node_call(
            "POST",
            NodeRoute.ENROLLMENT.path,
            certificate=None,
            body=flow.enrollment_body(zone, _code(100 + index)),
            address=gob.address(),
        )
        codes.append(response.json()["code"])
        if response.status_code == 429:
            _assert_rate_limited(response)
    assert codes == ["enrollment_code_invalid"] * NODE_QUARTER + ["rate_limited"] * 2
    attempts = gob.fetch(
        "SELECT result FROM fleet.enrollment_attempt WHERE node_id = $1", zone.node
    )
    assert len(attempts) == NODE_QUARTER  # lo limitado no llega a contarse como intento


def _oversized(limit: int) -> bytes:
    """Un JSON de ``limit + 1`` bytes exactos."""
    head, tail = b'{"relleno":"', b'"}'
    return head + b"a" * (limit + 1 - len(head) - len(tail)) + tail


def test_g05_each_record_type_has_its_own_body_limit(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    expected = {
        NodeRoute.FINDING: 262_144,
        NodeRoute.DETECTION_REVIEW: 262_144,
        NodeRoute.OBSERVABILITY_EVENT: 65_536,
        NodeRoute.HEARTBEAT: 65_536,
        NodeRoute.UPDATE_RESULT: 65_536,
        NodeRoute.ENROLLMENT: 16_384,
        NodeRoute.CREDENTIAL_ROTATION: 16_384,
        NodeRoute.CLIP_UPLOAD: 16_384,
    }
    for route, limit in expected.items():
        assert route.max_body_bytes == limit, route
        certificate = None if route is NodeRoute.ENROLLMENT else zone.cert
        too_large = gob.node_call(
            "POST", route.path, certificate=certificate, content=_oversized(limit)
        )
        assert too_large.status_code == 413, (route, too_large.text)
        body = too_large.json()
        assert (body["code"], body["retryable"]) == ("payload_too_large", False), route
        # En el límite exacto no es «demasiado grande» (lo rechaza después el esquema).
        at_limit = gob.node_call(
            "POST", route.path, certificate=certificate, content=_oversized(limit - 1)
        )
        assert at_limit.status_code != 413, (route, at_limit.text)
    assert gob.records(zone.organization_id, "finding_received") == []


def test_g05_a_burst_without_certificate_neither_limits_nodes_nor_people(
    gob: GobPlatform,
) -> None:
    """Nota de la revisión de VIG-144: las peticiones sin certificado válido no tienen límite por
    nodo ni por origen. Una ráfaga de ellas (con el mamparo de nodos sin llenar) no gasta el cubo
    de ningún nodo legítimo ni ocupa a las rutas de personas."""
    flow = Onboarding(gob)
    zone = flow.zone()
    anonymous = [
        gob.node_send(
            "POST",
            NodeRoute.OBSERVABILITY_EVENT.path,
            certificate=None,
            body=flow.event(zone),
            address=gob.address(),
        )
        for _ in range(WAVE)
    ]
    legit = flow.submit(zone, NodeRoute.OBSERVABILITY_EVENT, flow.event(zone), "event_id")
    person = gob.send("GET", f"/zones/{zone.zone_id}/gates", cookie=zone.admin)
    *burst, node_answer, person_answer = gob.gather(*anonymous, legit, person)
    assert {response.status_code for response in burst} == {401}
    assert {response.json()["code"] for response in burst} == {"node_not_enrolled"}
    assert node_answer.status_code == 200, node_answer.text
    assert person_answer.status_code == 200, person_answer.text
    # Y después de la ráfaga, el nodo conserva su ráfaga entera menos lo que él mismo gastó.
    base = gob.now() - timedelta(minutes=4)
    for index in range(BURST - 1):
        response = flow.post_event(zone, flow.event(zone, base + timedelta(seconds=index)))
        assert response.status_code == 200, (index, response.text)
