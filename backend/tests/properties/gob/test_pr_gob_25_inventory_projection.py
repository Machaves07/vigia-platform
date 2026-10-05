"""PR-GOB-25 (TASK-223; BR-GOB-71, 72; DE §3.5 a §3.8), contra PostgreSQL 16 real.

``RuleBasedStateMachine`` (perfil ``ci``, semilla fija y semilla de la sesión registradas por
``tests/conftest.py``) sobre la ruta real ``POST heartbeats``: cada ejemplo es un nodo nuevo con
dos zonas y tres cámaras por zona. Las reglas generan latidos **con huecos**, **duplicados** (un
``heartbeat_id`` ya aceptado con otro contenido) y **reloj desviado**: el ``sent_at`` y la
desviación del reloj del nodo arbitrarios, y el reloj de la plataforma que retrocede (otra
instancia con el reloj algo atrasado).

Invariantes, leídos de la base después de cada paso:

- la proyección (``NodeInventory``, ``CameraInventory`` y ``ZoneNodeState``) es la del **último
  latido aceptado** (el último ``heartbeat_id`` nuevo), campo a campo;
- un ``heartbeat_id`` repetido no la altera (ni la historia);
- ``last_heartbeat_at`` nunca retrocede: es el máximo de las recepciones de la plataforma, sea cual
  sea el reloj del nodo;
- ``HeartbeatHistory`` tiene una fila por ``heartbeat_id`` aceptado.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final

import pytest
from hypothesis import seed as hypothesis_seed
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from tests.conftest import _seeds_for_profile
from tests.heartbeat_support import HeartbeatStack, NodeSite, heartbeat_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.domain.heartbeat import parse_instant
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

pytestmark = pytest.mark.integration

STEPS: Final = 8
STATES: Final = ("observable", "degraded", "not_observable")
MODES: Final = ("no_capture", "commissioning", "productive")
MODELS: Final = ("modelo-1.0", "modelo-2.0")
SOFTWARE: Final = ("1.4.0", "1.4.1", "1.5.0")


@st.composite
def readings(draw: st.DrawFn, cameras: int, zones: int) -> dict[str, Any]:
    """Lo que varía de un latido a otro: estados, modos, cola, reloj y versiones."""
    return {
        "camera_states": draw(
            st.lists(st.sampled_from(STATES), min_size=cameras, max_size=cameras)
        ),
        "connected": draw(st.lists(st.booleans(), min_size=cameras, max_size=cameras)),
        "fps": draw(
            st.lists(
                st.floats(min_value=0, max_value=60, allow_nan=False, allow_infinity=False),
                min_size=cameras,
                max_size=cameras,
            )
        ),
        "modes": draw(st.lists(st.sampled_from(MODES), min_size=zones, max_size=zones)),
        "zone_states": draw(st.lists(st.sampled_from(STATES), min_size=zones, max_size=zones)),
        "episodes": draw(st.lists(st.integers(0, 50), min_size=zones, max_size=zones)),
        "catalog_versions": draw(st.lists(st.integers(1, 9), min_size=zones, max_size=zones)),
        "valid_hours": draw(st.lists(st.integers(-48, 168), min_size=zones, max_size=zones)),
        "pending": draw(st.integers(0, 10_000)),
        "offset_ms": draw(st.integers(-900_000_000, 900_000_000)),
        "synchronized": draw(st.booleans()),
        "sent_skew_seconds": draw(st.integers(-86_400, 86_400)),
        "uptime": draw(st.integers(0, 10_000_000)),
        "model": draw(st.sampled_from(MODELS)),
        "software": draw(st.sampled_from(SOFTWARE)),
    }


@dataclass
class Model:
    accepted: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``heartbeat_id`` aceptado → cuerpo enviado."""
    last: dict[str, Any] | None = None
    last_heartbeat_at: dt.datetime | None = None


class InventoryProjection(RuleBasedStateMachine):
    stack: ClassVar[HeartbeatStack]

    def __init__(self) -> None:
        super().__init__()
        self.model = Model()
        self.site: NodeSite | None = None

    @initialize()
    def enroll(self) -> None:
        self.stack.tick()
        self.site = self.stack.site(zones=2, cameras=3)

    def _body(self, values: dict[str, Any]) -> dict[str, Any]:
        stack, site = self.stack, self.site
        assert site is not None
        body = stack.body(
            site,
            sent_at=format_timestamp(
                stack.now() + dt.timedelta(seconds=values["sent_skew_seconds"])
            ),
            node_clock={
                "synchronized": values["synchronized"],
                "offset_ms": values["offset_ms"],
                "source": "ntp_local",
            },
            software_version=values["software"],
            model_version=values["model"],
            uptime_seconds=values["uptime"],
            local_queue={"pending": values["pending"], "dead_letter": [], "retained_sent": 0},
        )
        for camera, state, connected, fps in zip(
            body["cameras"],
            values["camera_states"],
            values["connected"],
            values["fps"],
            strict=True,
        ):
            camera.update(observability_state=state, connected=connected, measured_fps=fps)
        for zone, mode, state, episodes, version, hours in zip(
            body["zones"],
            values["modes"],
            values["zone_states"],
            values["episodes"],
            values["catalog_versions"],
            values["valid_hours"],
            strict=True,
        ):
            zone.update(
                mode=mode,
                observability_state=state,
                open_episodes=episodes,
                catalog_version=version,
                gate_state_valid_until=format_timestamp(stack.now() + dt.timedelta(hours=hours)),
            )
        return body

    def _post(self, body: dict[str, Any]) -> None:
        assert self.site is not None
        response = self.stack.post(self.site, body)
        assert response.status_code == 200, response.text

    # --- Reglas -------------------------------------------------------------------------------

    @rule(gap=st.integers(1, 7_200), values=readings(cameras=6, zones=2))
    def a_new_heartbeat_after_a_gap(self, gap: int, values: dict[str, Any]) -> None:
        self.stack.tick(gap)
        body = self._body(values)
        received = to_millisecond(self.stack.now())
        self._post(body)
        model = self.model
        model.accepted[body["heartbeat_id"]] = body
        model.last = body
        previous = model.last_heartbeat_at
        model.last_heartbeat_at = received if previous is None else max(previous, received)

    @precondition(lambda self: bool(self.model.accepted))
    @rule(
        pick=st.integers(0, 64),
        gap=st.integers(0, 600),
        values=readings(cameras=6, zones=2),
    )
    def a_repeated_heartbeat_id_with_other_content(
        self, pick: int, gap: int, values: dict[str, Any]
    ) -> None:
        identifiers = sorted(self.model.accepted)
        heartbeat_id = identifiers[pick % len(identifiers)]
        self.stack.tick(gap)
        body = {**self._body(values), "heartbeat_id": heartbeat_id}
        self._post(body)  # se ignora: el modelo no cambia

    @rule(seconds=st.integers(1, 30))
    def the_platform_clock_lags(self, seconds: int) -> None:
        # Otra instancia con el reloj atrasado: la recepción puede ser anterior a la última.
        clock = self.stack.authz.sessions.clock
        clock.set(clock.now() - dt.timedelta(seconds=seconds))

    # --- Invariantes --------------------------------------------------------------------------

    @invariant()
    def the_projection_is_the_last_accepted_heartbeat(self) -> None:
        last, site = self.model.last, self.site
        if last is None or site is None:
            return
        inventory = self.stack.inventory(site.node_id)
        assert inventory is not None
        assert inventory["software_version"] == last["software_version"]
        assert inventory["model_version"] == last["model_version"]
        assert inventory["contract_version"] == last["contract_version"]
        assert inventory["uptime_seconds"] == last["uptime_seconds"]
        assert inventory["local_queue"] == last["local_queue"]
        clock = last["node_clock"]
        assert inventory["clock"] == {
            "synchronized": clock["synchronized"],
            "offset_ms": clock["offset_ms"],
        }
        assert inventory["communication_state"] == "reachable"
        cameras = {str(row["camera_id"]): row for row in self.stack.cameras(site.node_id)}
        assert set(cameras) == {camera["camera_id"] for camera in last["cameras"]}
        for camera in last["cameras"]:
            row = cameras[camera["camera_id"]]
            assert row["observability_state"] == camera["observability_state"]
            assert row["connected"] is camera["connected"]
            assert row["measured_fps"] == camera["measured_fps"]
        zones = {str(row["zone_id"]): row for row in self.stack.zones(site.node_id)}
        assert set(zones) == {zone["zone_id"] for zone in last["zones"]}
        for zone in last["zones"]:
            row = zones[zone["zone_id"]]
            assert row["mode"] == zone["mode"]
            assert row["observability_state"] == zone["observability_state"]
            assert row["catalog_version_in_node"] == zone["catalog_version"]
            assert row["open_episodes"] == zone["open_episodes"]
            assert row["gate_state_valid_until"] == parse_instant(zone["gate_state_valid_until"])

    @invariant()
    def last_heartbeat_at_never_goes_back(self) -> None:
        if self.site is None or self.model.last_heartbeat_at is None:
            return
        inventory = self.stack.inventory(self.site.node_id)
        assert inventory is not None
        assert inventory["last_heartbeat_at"] == self.model.last_heartbeat_at

    @invariant()
    def one_history_row_per_accepted_heartbeat_id(self) -> None:
        if self.site is None:
            return
        rows = self.stack.history(self.site.node_id)
        assert sorted(str(row["heartbeat_id"]) for row in rows) == sorted(self.model.accepted)


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[HeartbeatStack]:
    with heartbeat_stack(postgres_endpoint, "pr_gob_25") as built:
        yield built


def test_pr_gob_25_the_projection_is_the_last_accepted_heartbeat(stack: HeartbeatStack) -> None:
    InventoryProjection.stack = stack
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(InventoryProjection)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS))
