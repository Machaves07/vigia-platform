"""H-12, H-47 y H-53: la prueba de ejemplo de cada historia (TASK-229; NFR-GOB-62; PBT-10).

TASK-229 comprueba que la tarea de cada historia dejó su prueba de ejemplo y añade la que falta:

- **H-12** (TASK-216, VIG-158): ``tests/examples/test_h12_latency_segments.py``, la latencia del
  acta en cuatro tramos;
- **H-53** (TASK-214, VIG-150): ``tests/examples/test_h53_commissioning_hours.py``, las horas por
  tipo de paso y nunca por persona;
- **H-47** (TASK-224, VIG-159) dejó sus pruebas en ``tests/integration/
  test_fleet_inventory_routes.py`` con el estado del inventario sembrado; el ejemplo de punta a
  punta está **aquí**: el nodo envía sus latidos por la ruta del contrato y la persona ve en
  ``GET /fleet/nodes`` los avisos calculados en la lectura (BR-GOB-75, 78, 79; nota de
  NFR-GOB-45): cola por encima del umbral, desfase de reloj, cámara por debajo de su mínimo,
  lector simulado en una zona productiva y, sin latido, «sin latido desde» (nunca «sin eventos»,
  P2); un nodo revocado deja de mostrar avisos (BR-GOB-76).

Solo datos generados (NFR-CTR-43). Reloj simulado desde la hora de la base.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok
from vigia_platform.shared.context import Role

EXAMPLES = Path(__file__).resolve().parent
REVOKED = "Equipo retirado de la línea tras el cambio de la prensa"
TROUBLED: dict[str, Any] = {
    "node_clock": {"synchronized": True, "offset_ms": 6_500, "source": "ntp_local"},
    "local_queue": {"pending": 150, "dead_letter": [], "retained_sent": 0},
    "signal_reader": {"available": True, "adapter": "simulated"},
}


def test_h53_left_its_example() -> None:
    assert (EXAMPLES / "test_h53_commissioning_hours.py").is_file()


def _item(gob: GobPlatform, zone: GobZone, cookie: Any) -> dict[str, Any]:
    listed = ok(gob.call("GET", "/fleet/nodes", cookie=cookie))
    (item,) = [node for node in listed["nodes"] if node["node_id"] == str(zone.node)]
    return item


@pytest.mark.integration
def test_h47_the_inventory_shows_what_the_heartbeats_said(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    healthy = ok(flow.post_heartbeat(zone))
    assert healthy["server_time"]
    assert _item(gob, zone, zone.admin)["warnings"] == []

    beat = flow.heartbeat(zone, **TROUBLED)
    beat["cameras"][0]["measured_fps"] = 2.0  # por debajo de su mínimo declarado (5)
    gob.advance(16)  # el latido siguiente, dentro de su presupuesto (4 por minuto)
    assert flow.post_heartbeat(zone, beat).status_code == 200
    item = _item(gob, zone, zone.admin)
    assert set(item["warnings"]) >= {
        "queue_over_threshold",
        "clock_drift",
        "camera_below_min_fps",
        "simulated_adapter_in_productive",
    }, item["warnings"]
    shown = json.dumps(item)
    assert "simulated" in shown and str(zone.cameras[0]) in shown

    # Sin latidos más de cinco veces el intervalo: mudo, «sin latido desde», nunca «sin eventos».
    gob.advance(6 * 60)
    _, admin = gob.person(zone.organization_id, Role.ADMINISTRATOR)  # la sesión de antes venció
    muted = _item(gob, zone, admin)
    assert "node_mute" in muted["warnings"], muted["warnings"]
    assert "evento" not in json.dumps(muted).lower()

    # Revocado, deja de avisar (BR-GOB-76).
    gob.resync()
    ok(flow.as_installer(zone, "POST", f"/nodes/{zone.node}/revocation", {"reason_es": REVOKED}))
    assert _item(gob, zone, admin)["warnings"] == []
