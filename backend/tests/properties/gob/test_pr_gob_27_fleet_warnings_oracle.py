"""PR-GOB-27 (TASK-224; LC-GOB-15; PAT-GOB-ESC-01 y su nota), contra PostgreSQL 16 real.

Oráculo (perfil ``ci``, semilla fija y semilla de la sesión registradas por ``tests/conftest.py``):
para estados de inventario y umbrales de planta generados, los ``warnings`` que devuelve ``GET
/fleet/nodes`` coinciden **fila a fila** con ``fleet_warnings.evaluate``, el evaluador de referencia
de las **ocho** clases. Después, un ``PUT`` de umbrales nuevos cambia la respuesta siguiente sin
ninguna escritura intermedia en el inventario: solo cambian la fila de umbrales, su auditoría y el
``provider_query`` de la petición bajo concesión.

Cada ejemplo reescribe (como superusuario) el estado de cuatro nodos de una planta: uno con una zona
``productive``, uno con una zona ``commissioning`` y dos sin zona productiva; estado del nodo,
baja, edad del último latido (en el borde de 5 veces el intervalo), intervalo configurado, cola y
antigüedad del más viejo, desviación de reloj, aviso de retiro, adaptador, credencial vigente (en
el borde de 15 días), cámaras del último latido y de uno anterior, y clips huérfanos dentro y fuera
de la ventana, del día y de verificación. Los desplazamientos se generan en los bordes de cada
umbral. Solo datos generados (NFR-CTR-43); reloj simulado al milisegundo desde la hora de la base.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fleet_http_support import FleetStack, fleet_stack
from tests.fleet_inventory_support import InventoryWorld, NodeState, fingerprint
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.domain.fleet_thresholds import FleetThresholds
from vigia_platform.fleet.domain.fleet_warnings import evaluate
from vigia_platform.shared.signing.keys import to_millisecond

pytestmark = pytest.mark.integration

MINUTE_MS: Final = 60_000
DAY_MS: Final = 86_400_000
PENDING_THRESHOLDS: Final = (1, 50, 100, 200)
AGE_THRESHOLDS: Final = (1, 30, 120)
DRIFT_THRESHOLDS: Final = (1, 5_000, 9_000)
UNCHANGED_BY_A_THRESHOLD: Final = (
    "fleet.node_inventory",
    "fleet.camera_inventory",
    "fleet.zone_node_state",
    "fleet.heartbeat_history",
    "fleet.node_fleet_record",
    "fleet.node_credential",
    "fleet.clip_upload_grant",
    "fleet.fleet_alarm",
    "identity.node_identity",
    "identity.zone_node_assignment",
)


def _near(values: tuple[int, ...]) -> st.SearchStrategy[int]:
    """Cada valor y sus vecinos (los bordes), con algo de ruido alrededor."""
    borders = sorted({v + d for v in values for d in (-1, 0, 1) if v + d >= 0})
    return st.one_of(st.sampled_from(borders), st.integers(0, 2 * max(values)))


@st.composite
def thresholds(draw: st.DrawFn, plant: Any) -> FleetThresholds | None:
    if draw(st.booleans()):
        return None  # sin fila: valores por defecto
    return FleetThresholds(
        plant_id=plant,
        queue_pending_threshold=draw(st.sampled_from(PENDING_THRESHOLDS)),
        queue_age_threshold_minutes=draw(st.sampled_from(AGE_THRESHOLDS)),
        clock_drift_threshold_ms=draw(st.sampled_from(DRIFT_THRESHOLDS)),
        updated_by=plant,
        updated_at=datetime.fromisoformat("2026-10-01T00:00:00+00:00"),
    )


@st.composite
def camera(draw: st.DrawFn) -> tuple[float, float]:
    declared = float(draw(st.integers(1, 60)))
    measured = draw(st.sampled_from((declared, declared - 0.5, declared + 0.5, 0.0, declared * 2)))
    return max(measured, 0.0), declared


@st.composite
def node_states(draw: st.DrawFn) -> NodeState:
    interval = draw(st.sampled_from((None, 15, 60, 600)))
    mute_ms = 5 * (interval or 60) * 1000
    heard = draw(
        st.one_of(
            st.none(),
            st.sampled_from((mute_ms - 1, mute_ms, mute_ms + 1)),
            st.integers(0, 3 * mute_ms),
        )
    )
    certificate = 15 * DAY_MS
    return NodeState(
        status=draw(
            st.sampled_from(
                ("enrolled", "enrolled", "declared", "re_enrollment_pending", "revoked")
            )
        ),
        decommissioned=draw(st.integers(0, 9)) == 0,
        heartbeat_age_ms=heard,
        interval_seconds=interval,
        pending=draw(_near(PENDING_THRESHOLDS)),
        oldest_pending_age_ms=draw(
            st.one_of(
                st.none(),
                st.sampled_from(
                    tuple(m * MINUTE_MS + d for m in (*AGE_THRESHOLDS, 30) for d in (-1, 0, 1))
                ),
                st.integers(0, 240 * MINUTE_MS),
            )
        ),
        offset_ms=draw(_near(DRIFT_THRESHOLDS)) * draw(st.sampled_from((1, -1))),
        retires_at=draw(st.sampled_from((None, None, "2027-03-01T00:00:00.000Z"))),
        adapter=draw(st.sampled_from(("modbus_rtu", "simulated", "file"))),
        certificate_in_ms=draw(
            st.one_of(
                st.none(),
                st.sampled_from((certificate - 1, certificate, certificate + 1, -DAY_MS)),
                st.integers(-30 * DAY_MS, 400 * DAY_MS),
            )
        ),
        cameras=tuple(draw(st.lists(camera(), min_size=1, max_size=8))),
        stale_cameras=tuple(draw(st.lists(st.just((0.0, 10.0)), max_size=2))),
        orphan_clips=draw(st.sampled_from((0, 1, 5, 6, 7, 50, 51))),
        orphan_clips_outside=draw(st.sampled_from((0, 60))),
        day_clips=draw(st.sampled_from((0, 20, 120))),
        verification_orphans=draw(st.sampled_from((0, 3))),
    )


class Layout:
    """La planta del oráculo: cuatro nodos y si alguna de sus zonas es ``productive``."""

    def __init__(self, world: InventoryWorld) -> None:
        self.world = world
        self.plant = world.plants[0]
        productive, commissioning, _ = world.zones(self.plant)
        world.set_gate(productive, mounting="approved", usage="approved")
        world.set_gate(commissioning, mounting="approved", usage="pending")
        self.productive = {
            world.declare(self.plant, [productive]): True,
            world.declare(self.plant, [commissioning]): False,
            world.declare(self.plant, []): False,
            world.declare(self.plant, []): False,
        }


@pytest.fixture(scope="module")
def layout(postgres_endpoint: PostgresEndpoint) -> Iterator[Layout]:
    with fleet_stack(postgres_endpoint, "pr_gob_27") as stack:
        clock = stack.authz.sessions.clock
        clock.set(to_millisecond(clock.now()))
        yield Layout(InventoryWorld.build(stack))


def _warnings(world: InventoryWorld, plant: Any) -> dict[str, list[str]]:
    nodes = world.list_all(world.admin, {"plant_id": str(plant)})
    return {node["node_id"]: node["warnings"] for node in nodes}


def _expected(
    layout: Layout,
    states: dict[Any, NodeState],
    written_at: datetime,
    limits: FleetThresholds,
    now: datetime,
) -> dict[str, list[str]]:
    return {
        str(node): [
            kind.value
            for kind in evaluate(state.inputs(written_at, layout.productive[node]), limits, now)
        ]
        for node, state in states.items()
    }


def _stack(layout: Layout) -> FleetStack:
    return layout.world.stack


@settings(suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(data=st.data())
def test_pr_gob_27_warnings_match_the_reference_row_by_row(
    layout: Layout, data: st.DataObject
) -> None:
    world, plant = layout.world, layout.plant
    states = {
        node: data.draw(node_states(), label=f"node {i}")
        for i, node in enumerate(layout.productive)
    }
    first = data.draw(thresholds(plant), label="umbrales")
    second = data.draw(thresholds(plant).filter(lambda t: t is not None), label="umbrales nuevos")
    assert second is not None
    written_at = world.next_now()
    world.write_states(states, written_at)
    world.write_thresholds(plant, first)
    limits = first if first is not None else FleetThresholds(plant_id=plant)

    seen = _warnings(world, plant)
    assert world.now() == written_at
    assert seen == _expected(layout, states, written_at, limits, written_at)

    # Cambiar un umbral cambia la respuesta siguiente sin ninguna escritura intermedia.
    before = fingerprint(_stack(layout), world.organization)
    response = _stack(layout).send(
        world.installer,
        "PUT",
        f"/plants/{plant}/fleet-thresholds",
        {
            "queue_pending_threshold": second.queue_pending_threshold,
            "queue_age_threshold_minutes": second.queue_age_threshold_minutes,
            "clock_drift_threshold_ms": second.clock_drift_threshold_ms,
        },
    )
    assert response.status_code == 200, response.text
    after = fingerprint(_stack(layout), world.organization)
    assert all(before[table] == after[table] for table in UNCHANGED_BY_A_THRESHOLD)
    again = _warnings(world, plant)
    assert again == _expected(layout, states, written_at, second, world.now())
    assert world.now() == written_at + timedelta(seconds=2)
