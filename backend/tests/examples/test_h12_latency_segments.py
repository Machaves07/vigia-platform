"""Prueba de ejemplo de H-12 (TASK-216; NFR-GOB-70, PAT-GOB-REN-08): la latencia del acta.

De extremo a extremo sobre la aplicación real y PostgreSQL 16 real
(``tests/close_record_support.py``), con peticiones HTTP como las hará U-05: la consola envía las
muestras de exposición con el reloj del navegador y el instalador cierra el acta con el p95 que
lee del ``LocalStatus`` del nodo y las líneas base del nodo.

- Los **cuatro tramos se guardan por separado**, cada uno con su reloj (``measured_by``): el del
  instalador (tramo 1), la plataforma (tramos 2 y 3b, sobre los clips de verificación) y el
  navegador (tramo 3a, sobre las muestras).
- **Ningún cálculo resta marcas de relojes distintos**: dos zonas con las mismas duraciones, una
  con el navegador siete horas atrasado y el nodo tres días adelantado (sus líneas base) y otra
  con los relojes al revés, dan exactamente los mismos tramos.
- La **suma** de las medianas medidas está marcada como **orientativa**.
- **Sin muestras de U-05**, el tramo 3a dice «no medido»: nulo y en ``not_measured``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.close_record_support import CloseWorld, Zone, close_world
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

SHOWN_MS: Final = (90, 110, 130, 150, 170)
"""Duraciones de pintado (``displayed_at - fetched_at``) de las cinco muestras."""


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[CloseWorld]:
    with close_world(postgres_endpoint, "h12_examples") as world:
        yield world


def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _close_with_clocks(
    world: CloseWorld, browser: timedelta, node: timedelta, samples: bool = True
) -> dict[str, Any]:
    """Una zona lista cuyas muestras van con el navegador desplazado ``browser`` y cuyas líneas
    base van con el nodo desplazado ``node``; devuelve el acta cerrada por HTTP."""
    zone: Zone = world.zone()
    world.passes(zone)
    world.occlusions_ok(zone)
    world.clips(zone, 80, served=True)  # tramo 2: 400 ms; tramo 3b: 150 ms (reloj de plataforma)
    if samples:
        passes = world.fetch(
            "SELECT pass_id FROM catalog.walk_test_pass WHERE session_id = $1 ORDER BY recorded_at",
            zone.session.session_id,
        )
        browser_now = zone.session.started_at + browser
        for row, shown in zip(passes, SHOWN_MS, strict=False):
            _ok(
                world.request(
                    "POST",
                    f"/walk-tests/{zone.session.session_id}/exposure-samples",
                    zone.mounted,
                    {
                        "pass_id": str(row["pass_id"]),
                        "fetched_at": format_timestamp(browser_now),
                        "displayed_at": format_timestamp(
                            browser_now + timedelta(milliseconds=shown)
                        ),
                    },
                ),
                201,
            )
    body = world.body(zone, beacon=210)
    for baseline in body["installer_measurements"]["baselines"]:
        baseline["captured_at"] = format_timestamp(zone.session.started_at + node)
    world.advance()
    closed = _ok(
        world.request("POST", f"/walk-tests/{zone.session.session_id}/close", zone.mounted, body)
    )
    # Las líneas base llevan el reloj del nodo tal cual: se registran, nunca se restan.
    sent = [b["captured_at"] for b in body["installer_measurements"]["baselines"]]
    assert [b["captured_at"] for b in closed["installer_measurements"]["baselines"]] == sent
    # La lectura del acta dice exactamente lo mismo que el cierre.
    path = f"/commissioning-records/{closed['commissioning_record_id']}"
    assert _ok(world.request("GET", path, zone.mounted)) == closed
    return closed


def test_h12_the_four_tranches_are_kept_apart_and_never_subtract_two_clocks(
    world: CloseWorld,
) -> None:
    behind = _close_with_clocks(world, browser=-timedelta(hours=7), node=timedelta(days=3))
    ahead = _close_with_clocks(world, browser=timedelta(days=3), node=-timedelta(hours=7))

    latency = behind["latency"]
    assert latency == {**ahead["latency"]}  # ningún desfase de reloj cambia una cifra
    assert latency["node_tranche"] == {
        "median_ms": None,
        "p95_ms": 210,
        "max_ms": None,
        "repetitions": None,
        "measured_by": "installer",
    }
    assert latency["platform_tranche"] == {
        "median_ms": 400,
        "p95_ms": 400,
        "max_ms": 400,
        "repetitions": 80,
        "measured_by": "platform",
    }
    assert latency["exposure_tranche"] == {
        "median_ms": 130,
        "p95_ms": 170,
        "max_ms": 170,
        "repetitions": len(SHOWN_MS),
        "measured_by": "browser",
    }
    assert latency["served_tranche"] == {
        "median_ms": 150,
        "p95_ms": 150,
        "max_ms": 150,
        "repetitions": 80,
        "measured_by": "platform",
    }
    assert latency["not_measured"] == []
    # La suma de las medianas medidas, marcada como orientativa (el tramo 1 no trae mediana).
    assert latency["indicative"] is True
    assert latency["indicative_sum_median_ms"] == 400 + 130 + 150


def test_h12_without_u05_samples_the_exposure_tranche_says_not_measured(
    world: CloseWorld,
) -> None:
    record = _close_with_clocks(world, browser=timedelta(0), node=timedelta(0), samples=False)

    latency = record["latency"]
    assert latency["exposure_tranche"] is None
    assert latency["not_measured"] == ["exposure_tranche"]
    assert latency["indicative"] is True
    assert latency["indicative_sum_median_ms"] == 400 + 150
