"""G-11 · Nodo que declara un lector de señales ``simulated`` en modo productivo (§12; P10).

**Qué intenta**: producir hallazgos en una zona productiva con señales inventadas (adaptador
``simulated`` o ``file``) sin que nadie lo note.

**Qué lo detiene**:

- BR-GOB-78: un latido con ``signal_reader.adapter`` igual a ``simulated`` o ``file`` y alguna zona
  del nodo en ``productive`` levanta la alarma ``simulated_adapter_in_productive`` y queda
  registrado (evento ``fleet_alarm_raised`` y entrada de auditoría ``fleet_security_alert``);
  nunca hay degradación silenciosa: el latido se acepta, el inventario muestra el adaptador, y la
  alarma no se repite mientras la condición dura (BR-GOB-81). Sin zona productiva, no hay alarma.

El latido entra por la ruta del contrato; la evaluación es la tarea periódica real
``evaluate_fleet_alarms`` de ``vigia-worker`` (``GobPlatform.run_task``).

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import json

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok

pytestmark = pytest.mark.integration

ALARM = "simulated_adapter_in_productive"
TASK = "evaluate_fleet_alarms"


def _simulated(flow: Onboarding, zone: GobZone, adapter: str) -> None:
    beat = flow.heartbeat(zone, signal_reader={"available": True, "adapter": adapter})
    response = flow.post_heartbeat(zone, beat)
    assert response.status_code == 200, response.text  # aceptado: nada se descarta


def _raised(gob: GobPlatform, zone: GobZone) -> list[dict[str, object]]:
    return [
        event
        for event in gob.events(zone.organization_id, "fleet_alarm_raised")
        if event.get("alarm_kind") == ALARM
    ]


@pytest.mark.parametrize("adapter", ["simulated", "file"])
def test_g11_a_simulated_reader_in_a_productive_zone_raises_one_audited_alarm(
    gob: GobPlatform, adapter: str
) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    _simulated(flow, zone, adapter)
    gob.run_task(TASK, zone.organization_id)

    (event,) = _raised(gob, zone)
    assert (event["node_id"], event["zone_id"]) == (str(zone.node), str(zone.zone_id))
    alerts = [
        json.loads(bytes(row["filters"]))
        for row in gob.audit_entries(zone.organization_id, "fleet_security_alert")
    ]
    assert [alert["alert_kind"] for alert in alerts] == [ALARM]
    # El panel lo muestra: la alarma activa y el adaptador del último latido.
    alarms = ok(gob.call("GET", "/fleet/alarms", cookie=zone.admin))
    assert ALARM in json.dumps(alarms) and str(zone.node) in json.dumps(alarms)
    detail = ok(gob.call("GET", f"/fleet/nodes/{zone.node}", cookie=zone.admin))
    assert adapter in json.dumps(detail)

    # Sostenida: otro latido igual y otra evaluación no repiten la alarma (BR-GOB-81).
    gob.advance(61)
    _simulated(flow, zone, adapter)
    gob.run_task(TASK, zone.organization_id)
    assert len(_raised(gob, zone)) == 1
    assert len(gob.audit_entries(zone.organization_id, "fleet_security_alert")) == 1


def test_g11_without_a_productive_zone_the_simulated_reader_is_no_alarm(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)  # ``commissioning``: el lector simulado es legítimo para el walk-test
    _simulated(flow, zone, "simulated")
    gob.run_task(TASK, zone.organization_id)
    assert _raised(gob, zone) == []
    # Y en cuanto la zona pasa a productiva, la misma condición sí levanta la alarma.
    flow.productive(zone)
    _simulated(flow, zone, "simulated")
    gob.run_task(TASK, zone.organization_id)
    assert len(_raised(gob, zone)) == 1
