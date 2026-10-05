"""Prueba de ejemplo de H-53 (TASK-214; SCR-06): las horas miden la zona, nunca a la persona.

De extremo a extremo sobre la aplicación real y PostgreSQL 16 real (``tests/walk_test_support.py``),
solo con peticiones HTTP, como las hará U-05: el instalador abre la sesión, cronometra tres pasos
de dos responsables distintos (él mismo y un coordinador de la planta), corrige uno con motivo y
consulta la sesión en curso.

- Las horas acumuladas son la suma exacta de las duraciones efectivas de los pasos cerrados, y el
  resumen las agrupa por tipo de paso (BR-GOB-46, 47).
- Ningún agregado lleva un responsable: ni el total ni el resumen nombran a una persona, y la
  única forma de ver quién hizo un paso es el propio paso.
- Ninguna ruta acepta filtrar ni ordenar por responsable: un parámetro de consulta se rechaza.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.walk_test_support import CORRECTION, WalkTestWorld, walk_test_world
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

MINUTE: Final = 60
PERSON_KEYS: Final = ("responsible_user_id", "user_id", "corrected_by", "reopened_by")


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[WalkTestWorld]:
    with walk_test_world(postgres_endpoint, "h53_examples") as world:
        yield world


def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _ms(start: str, end: str) -> int:
    delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    return delta // timedelta(milliseconds=1)


def test_h53_hours_add_up_by_step_kind_and_never_by_person(world: WalkTestWorld) -> None:
    mounted = world.mounted(count=2)
    session = _ok(
        world.request("POST", f"/zones/{mounted.zone}/walk-tests", mounted, {"passes_per_cell": 3}),
        201,
    )
    session_id = session["session_id"]
    installer = str(mounted.installer.actor.id)
    coordinator = world.a.g.member(
        mounted.site, Role.COORDINATOR_SST, ScopeLevel.ZONE, mounted.zone
    )
    responsibles = (installer, installer, str(coordinator.actor.id))
    kinds = ("physical_setup", "framing", "framing")
    closed: list[dict[str, Any]] = []
    for index, (kind, responsible) in enumerate(zip(kinds, responsibles, strict=True)):
        step = _ok(
            world.request(
                "POST",
                f"/walk-tests/{session_id}/steps",
                mounted,
                {"step_kind": kind, "responsible_user_id": responsible},
            ),
            201,
        )
        world.advance((index + 1) * MINUTE)  # por debajo de la inactividad de la sesión de usuario
        body: dict[str, Any] = {}
        if index == 1:  # se olvidó abrirlo: cinco minutos antes de la marca del servidor
            started = datetime.fromisoformat(step["started_at"]) - timedelta(minutes=5)
            body = {
                "correction": {"started_at": format_timestamp(started), "reason_es": CORRECTION}
            }
        closed.append(
            _ok(
                world.request(
                    "POST", f"/walk-tests/{session_id}/steps/{step['step_id']}/close", mounted, body
                )
            )
        )
    # Un paso abierto todavía no suma nada.
    _ok(
        world.request(
            "POST",
            f"/walk-tests/{session_id}/steps",
            mounted,
            {"step_kind": "latency_measurement", "responsible_user_id": installer},
        ),
        201,
    )

    current = _ok(world.request("GET", f"/zones/{mounted.zone}/walk-tests/current", mounted))
    view = current["session"]
    effective = [
        _ms(
            (step["correction"] or {}).get("started_at") or step["started_at"],
            (step["correction"] or {}).get("ended_at") or step["ended_at"],
        )
        for step in closed
    ]
    server = [_ms(step["started_at"], step["ended_at"]) for step in closed]
    # Reloj del servidor: cada paso dura lo que se avanzó más el segundo de la petición.
    assert server == [61_000, 121_000, 181_000]
    assert effective == [server[0], server[1] + 5 * 60_000, server[2]]
    assert view["total_duration_ms"] == sum(effective)
    assert view["steps_summary"] == [
        {"step_kind": "physical_setup", "duration_ms": effective[0]},
        {"step_kind": "framing", "duration_ms": effective[1] + effective[2]},
    ]
    # Las marcas del servidor siguen visibles junto a la corrección (G-15).
    corrected = next(step for step in view["steps"] if step["correction"] is not None)
    assert corrected["started_at"] == closed[1]["started_at"]
    assert corrected["correction"]["started_at"] != corrected["started_at"]
    # Ningún agregado nombra a una persona: el responsable solo aparece en cada paso.
    for key in PERSON_KEYS:
        assert all(key not in entry for entry in view["steps_summary"])
    assert {step["responsible_user_id"] for step in view["steps"]} == set(responsibles)
    # Y ninguna ruta acepta filtrar ni ordenar por responsable.
    for params in (f"?responsible_user_id={installer}", "?order_by=responsible_user_id"):
        refused = world.request("GET", f"/zones/{mounted.zone}/walk-tests/current{params}", mounted)
        assert (refused.status_code, refused.json()["code"]) == (400, "invalid_request")
    assert uuid.UUID(session_id) == uuid.UUID(view["session_id"])
