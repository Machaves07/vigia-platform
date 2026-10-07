"""G-15 · Corrección abusiva del cronómetro del walk-test (business-rules §12; gate 10; P7).

**Qué intenta**: inflar o reducir las horas de comisionamiento de una zona para mover el gate 10
(reescribiendo las marcas, corrigiendo sin motivo o una y otra vez) o sacar de las horas una
medida por persona.

**Qué lo detiene**:

- BR-GOB-44: el cronómetro lo lleva el servidor: ``started_at`` y ``ended_at`` son marcas de la
  plataforma; un cuerpo que intenta fijarlas fuera de la corrección es ``invalid_request``;
- BR-GOB-45: la corrección se **anexa** con ``reason_es``, ``corrected_by`` y ``corrected_at`` y
  nunca sustituye las marcas originales, que siguen visibles en la vista y en el registro
  ``commissioning_step``; sin motivo, o incoherente, se rechaza y no escribe nada; un paso se
  cierra (y se corrige) una sola vez;
- BR-GOB-46: no hay agregación ni orden por persona: el resumen va por ``step_kind`` y la ruta
  rechaza filtrar u ordenar por responsable;
- BR-GOB-47: el total es exactamente la suma de las duraciones efectivas de los pasos cerrados.

El ejemplo de H-53 (``tests/examples/test_h53_commissioning_hours.py``, TASK-214) fija el caso
feliz; aquí, los intentos de abuso sobre la aplicación completa.

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.gob_platform_support import GobPlatform, GobZone, Onboarding, ok, stamp

pytestmark = pytest.mark.integration

REASON = "Se olvidó abrir el paso al llegar a la celda de la prensa"
PERSON_KEYS = ("responsible_user_id", "user_id", "corrected_by", "reopened_by")


def _ms(start: str, end: str) -> int:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)) // timedelta(
        milliseconds=1
    )


def _session(flow: Onboarding, zone: GobZone) -> str:
    opened = ok(
        flow.as_installer(
            zone, "POST", f"/zones/{zone.zone_id}/walk-tests", {"passes_per_cell": 3}
        ),
        201,
    )
    session_id: str = opened["session_id"]
    return session_id


def _step(flow: Onboarding, zone: GobZone, session_id: str, kind: str) -> dict[str, Any]:
    body: dict[str, Any] = ok(
        flow.as_installer(
            zone,
            "POST",
            f"/walk-tests/{session_id}/steps",
            {"step_kind": kind, "responsible_user_id": str(zone.installer_id)},
        ),
        201,
    )
    return body


def _close(flow: Onboarding, zone: GobZone, session_id: str, step_id: str, body: Any) -> Any:
    return flow.as_installer(zone, "POST", f"/walk-tests/{session_id}/steps/{step_id}/close", body)


def test_g15_marks_are_the_servers_and_a_correction_is_appended_once_with_reason(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    session_id = _session(flow, zone)
    step = _step(flow, zone, session_id, "physical_setup")
    gob.advance(600)
    step_id = step["step_id"]
    inflated = stamp(datetime.fromisoformat(step["started_at"]) - timedelta(hours=6))

    # Fijar las marcas a mano, corregir sin motivo o con una marca imposible: nada se escribe.
    for body in (
        {"started_at": inflated},
        {"correction": {"started_at": inflated}},
        {"correction": {"started_at": inflated, "reason_es": ""}},
        {"correction": {"started_at": "2020-01-01T00:00:00.000Z", "reason_es": REASON}},
    ):
        refused = _close(flow, zone, session_id, step_id, body)
        assert refused.status_code == 400, (body, refused.text)
        assert refused.json()["code"] == "invalid_request"
    assert gob.records(zone.organization_id, "commissioning_step") == []

    closed = ok(
        _close(
            flow,
            zone,
            session_id,
            step_id,
            {"correction": {"started_at": inflated, "reason_es": REASON}},
        )
    )
    assert closed["started_at"] == step["started_at"]  # la marca del servidor sigue
    correction = closed["correction"]
    assert (correction["started_at"], correction["reason_es"]) == (inflated, REASON)
    assert correction["corrected_by"] == str(zone.installer_id)
    assert correction["corrected_at"] == closed["ended_at"]
    # Una sola vez: no se vuelve a cerrar ni a corregir.
    again = _close(
        flow,
        zone,
        session_id,
        step_id,
        {"correction": {"started_at": step["started_at"], "reason_es": REASON}},
    )
    assert (again.status_code, again.json()["code"]) == (409, "conflict")
    (record,) = gob.contents(zone.organization_id, "commissioning_step")
    assert (record["started_at"], record["ended_at"]) == (step["started_at"], closed["ended_at"])
    assert record["correction"]["started_at"] == inflated


def test_g15_hours_are_the_sum_of_steps_and_never_by_person(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)
    session_id = _session(flow, zone)
    closed = []
    for index, kind in enumerate(("physical_setup", "framing")):
        step = _step(flow, zone, session_id, kind)
        gob.advance(60 * (index + 1))
        body: dict[str, Any] = {}
        if index == 1:
            earlier = datetime.fromisoformat(step["started_at"]) - timedelta(minutes=5)
            body = {"correction": {"started_at": stamp(earlier), "reason_es": REASON}}
        closed.append(ok(_close(flow, zone, session_id, step["step_id"], body)))
    current = ok(flow.as_installer(zone, "GET", f"/zones/{zone.zone_id}/walk-tests/current"))
    view = current["session"]
    effective = [
        _ms(
            (step["correction"] or {}).get("started_at") or step["started_at"],
            (step["correction"] or {}).get("ended_at") or step["ended_at"],
        )
        for step in closed
    ]
    assert view["total_duration_ms"] == sum(effective)  # BR-GOB-47
    assert [entry["step_kind"] for entry in view["steps_summary"]] == ["physical_setup", "framing"]
    # Los agregados de horas no nombran a nadie: el responsable solo está en cada paso.
    for key in PERSON_KEYS:
        assert all(key not in entry for entry in view["steps_summary"])
    assert not isinstance(view["total_duration_ms"], dict)
    for query in ({"responsible_user_id": str(zone.installer_id)}, {"order_by": "responsible"}):
        refused = gob.call(
            "GET",
            f"/zones/{zone.zone_id}/walk-tests/current",
            cookie=zone.installer,
            concession=zone.concession,
            params=query,
        )
        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")
