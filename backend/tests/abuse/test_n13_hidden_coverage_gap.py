"""N-13 · Ocultar un hueco de cobertura (H-11, H-38; P2, P10; business-rules §14).

**Qué intenta**: presentar como observado un periodo en que nadie observaba: un nodo que dice
``observable`` mientras la plataforma no sabe nada de él, una zona sin nodo o sin modo
productivo, o un tramo que simplemente no aparece en la línea de tiempo.

**Qué lo detiene** (BR-NUC-69 a BR-NUC-72):

- BR-NUC-69: dos capas; la de comunicación la deriva la **plataforma** de sus propios registros, y
  ``mute`` empieza en el último latido, no cuando se declaró;
- BR-NUC-70: la composición manda sobre lo que el nodo dice: sin nodo o sin informes,
  ``never_reported``; sin modo productivo, ``zone_not_active``; sin comunicación,
  ``no_communication``; lo que el nodo informa se conserva aparte (``node_intervals``);
- BR-NUC-71: partición exacta del periodo, sin tramos omitidos, y ningún estado «despejada»;
- BR-NUC-72: ``observable_ms`` cuenta solo instantes con comunicación, modo productivo y estado
  ``observable``; la suma del resumen es exactamente la longitud del periodo.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime
from typing import Any

import pytest

from tests.platform_support import HOUR, T0, Platform, stamp
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

FORBIDDEN_WORDS = ("clear", "safe", "despejad", "segur", "no ocurrió")


def _timeline(platform: Platform, site: Any, zone_id: uuid.UUID, hours: int = 4) -> dict[str, Any]:
    _, cookie = platform.person(site.organization_id, Role.COORDINATOR_SST)
    response = platform.call(
        "GET",
        f"/zones/{zone_id}/coverage",
        cookie=cookie,
        params={"from": stamp(T0), "to": stamp(T0 + hours * HOUR)},
    )
    assert response.status_code == 200, response.text
    lowered = response.text.lower()
    assert not [word for word in FORBIDDEN_WORDS if word in lowered]
    body: dict[str, Any] = response.json()
    _assert_partition(body, T0, T0 + hours * HOUR)
    return body


def _assert_partition(body: dict[str, Any], start: datetime, end: datetime) -> None:
    """Intervalos ordenados, contiguos, sin solape, que cubren todo ``[start, end)``."""
    intervals = body["intervals"]
    assert intervals, "una línea de tiempo vacía omite el periodo entero"
    assert intervals[0]["starts_at"] == stamp(start)
    assert intervals[-1]["ends_at"] == stamp(end)
    for before, after in itertools.pairwise(intervals):
        assert before["ends_at"] == after["starts_at"], (before, after)
    total = int((end - start).total_seconds() * 1000)
    assert sum(i["duration_ms"] for i in intervals) == total
    assert sum(body["summary"].values()) == total


def _states(body: dict[str, Any]) -> list[tuple[str, str, str, list[str]]]:
    return [(i["starts_at"], i["ends_at"], i["state"], i["causes"]) for i in body["intervals"]]


def test_n13_a_node_that_says_observable_while_mute_does_not_count_as_observed(
    platform: Platform,
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    node_id = platform.node(site, plant_id, zone_id)
    platform.gate(site, plant_id, zone_id, T0 - HOUR)
    platform.communication(site, plant_id, node_id, "reachable", T0 - HOUR)
    # El nodo declara ``observable`` desde antes del periodo y nunca dice otra cosa…
    platform.observability(site, plant_id, zone_id, node_id, T0 - HOUR)
    # …pero su último latido llegó a T0 + 1 h; la plataforma lo declaró mudo a T0 + 2 h.
    platform.communication(site, plant_id, node_id, "mute", T0 + 2 * HOUR, T0 + HOUR)
    platform.communication(site, plant_id, node_id, "reachable", T0 + 3 * HOUR)
    body = _timeline(platform, site, zone_id)
    assert _states(body) == [
        (stamp(T0), stamp(T0 + HOUR), "observable", []),
        (stamp(T0 + HOUR), stamp(T0 + 3 * HOUR), "not_observable", ["no_communication"]),
        (stamp(T0 + 3 * HOUR), stamp(T0 + 4 * HOUR), "observable", []),
    ]
    assert body["intervals"][1]["layer"] == "platform_communication"
    assert body["intervals"][1]["clock_basis"] == "platform"
    assert body["summary"]["observable_ms"] == 2 * 3_600_000
    assert body["summary"]["no_communication_ms"] == 2 * 3_600_000
    # Lo que el nodo dijo se conserva aparte: no se borra, pero no cambia el estado compuesto.
    assert any(i["state"] == "observable" for i in body["node_intervals"])


def test_n13_a_zone_without_node_or_reports_is_never_reported(platform: Platform) -> None:
    site = platform.site(plants=1, zones_per_plant=2)
    (plant_id, silent), (_, unassigned) = site.zones()
    platform.node(site, plant_id, silent)  # asignado, nunca informó
    platform.gate(site, plant_id, silent, T0 - HOUR)
    platform.gate(site, plant_id, unassigned, T0 - HOUR)
    for zone_id in (silent, unassigned):
        body = _timeline(platform, site, zone_id)
        assert _states(body) == [
            (stamp(T0), stamp(T0 + 4 * HOUR), "not_observable", ["never_reported"])
        ]
        assert body["summary"]["observable_ms"] == 0
        assert body["summary"]["never_reported_ms"] == 4 * 3_600_000


def test_n13_a_zone_outside_productive_mode_is_not_active_whatever_the_node_says(
    platform: Platform,
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    node_id = platform.node(site, plant_id, zone_id)
    platform.communication(site, plant_id, node_id, "reachable", T0 - HOUR)
    platform.observability(site, plant_id, zone_id, node_id, T0 - HOUR)
    # Sin ``gate_state_changed`` a modo productivo: la compuerta de uso no está aprobada (P9).
    body = _timeline(platform, site, zone_id)
    assert _states(body) == [
        (stamp(T0), stamp(T0 + 4 * HOUR), "not_observable", ["zone_not_active"])
    ]
    assert body["summary"]["observable_ms"] == 0
