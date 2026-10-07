"""G-12 · Cuenta de flota que intenta revocar o dar de baja nodos de otra organización (§12).

**Qué intenta**: con su propia sesión y su concesión (``fleet.manage``) sobre la organización B,
revocar, dar de baja, reasignar o pedir códigos de alta para un nodo de A cuyo identificador
conoce, o enumerar los nodos de A por la diferencia de respuesta.

**Qué lo detiene**:

- BR-GOB-57 (y BR-NUC-02): toda operación sobre un nodo se resuelve con el ``ScopeContext`` de la
  sesión y la seguridad a nivel de fila: el nodo de A no existe para B;
- BR-GOB-66: la revocación solo actúa sobre nodos del alcance; la de un nodo ajeno no llega a la
  aplicación ni a la lista de revocación;
- PR-GOB-12: la respuesta es ``not_found`` **idéntica** a la de un identificador inexistente
  (estado y cuerpo, salvo ``correlation_id``), y ninguna fila de A cambia (huella de cada tabla
  con ``organization_id``, calculada como superusuario).

El aislamiento de cada ruta y de cada puerto es de TASK-228 (``tests/isolation``); aquí, el
abuso completo con un nodo de A dado de alta y operando.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding
from tests.isolation.gob_world import changed_tables, fingerprint
from tests.platform_support import comparable
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

REASON = "Equipo sospechoso de manipulación en la línea de montaje"


def _attempts(node: uuid.UUID, zone: uuid.UUID) -> dict[str, tuple[str, str, Any]]:
    return {
        "revocación": ("POST", f"/nodes/{node}/revocation", {"reason_es": REASON}),
        "baja": ("POST", f"/nodes/{node}/decommission", {"reason_es": REASON}),
        "código de alta": ("POST", f"/nodes/{node}/enrollment-codes", None),
        "asignación": ("POST", f"/nodes/{node}/zones", {"zone_id": str(zone)}),
        "intentos": ("GET", f"/nodes/{node}/enrollment-attempts", None),
        "inventario": ("GET", f"/fleet/nodes/{node}", None),
    }


def _as(flow: Onboarding, attacker: GobZone, call: tuple[str, str, Any]) -> Any:
    method, path, body = call
    return flow.as_installer(attacker, method, path, body)


def test_g12_another_organization_cannot_touch_or_detect_a_node(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    victim = flow.productive_zone()
    attacker = flow.zone()  # B, con su instalador y su concesión sobre B
    assert flow.post_finding(victim, flow.finding(victim)).status_code == 200
    before = fingerprint(gob.fetch, victim.organization_id)

    known = _attempts(victim.node, attacker.zone_id)
    missing = _attempts(uuid.uuid4(), attacker.zone_id)
    for name, call in known.items():
        answer = _as(flow, attacker, call)
        assert answer.status_code == 404, (name, answer.text)
        assert comparable(answer) == comparable(_as(flow, attacker, missing[name])), name
        assert str(victim.node) not in answer.text, name
    # La administración de B tampoco (ni con su propia clave de lectura de flota).
    _, admin_b = gob.person(attacker.organization_id, Role.ADMINISTRATOR)
    answer = gob.call("GET", f"/fleet/nodes/{victim.node}", cookie=admin_b)
    assert answer.status_code == 404

    assert changed_tables(before, fingerprint(gob.fetch, victim.organization_id)) == []
    # El nodo de A sigue operando: ni revocado ni dado de baja.
    assert flow.post_heartbeat(victim).status_code == 200
    assert flow.post_finding(victim, flow.finding(victim)).status_code == 200
    (row,) = gob.fetch("SELECT status FROM identity.node_identity WHERE node_id = $1", victim.node)
    assert row["status"] == "enrolled"
    assert gob.records(victim.organization_id, "node_revoked") == []
    assert gob.records(attacker.organization_id, "node_revoked") == []
