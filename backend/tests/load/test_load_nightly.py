"""Perfil ``nightly`` y cliente sintético de consola (NFR-GOB-06, 02, 18, 19, 50; TASK-231).

``test_nightly_profile…`` (marca ``nightly``: solo con ``--hypothesis-profile=nightly``): 100 nodos
y 300 zonas a ``speed_factor`` 60 durante 30 minutos, después la **reconexión masiva** (una hora de
cola que los 100 nodos vacían a la vez) y **4 horas** de indisponibilidad seguidas de la
reconexión (NFR-GOB-18). Con ``VIGIA_LOAD_SCALE=smoke``, el mismo perfil reducido (20 nodos, a lo
sumo 3 minutos) para ensayar el arnés en local. En paralelo, el cliente sintético de consola
recorre las rutas de NFR-GOB-03 con una sesión real. Bloquea:

- lo funcional de ``ci``: aceptados = emitidos, sin cola muerta, sin duplicados en el expediente,
  cada cadena de planta íntegra y en orden de recepción (BR-GOB-91), cero ``rate_limited`` por
  debajo del mínimo de NFR-CTR-02 y cero ``temporarily_unavailable`` (NFR-GOB-02);
- cada vaciado de cola completo: todo lo encolado durante la indisponibilidad se aceptó;
- la peor proporción de rechazos permanentes en 15 minutos no pasa del 1 % (NFR-GOB-50 frente a
  la alarma de NFR-GOB-46);
- el **cliente sintético** (NFR-GOB-19): en las ventanas de reconexión, cada ruta cumple su p95 o
  la prueba falla nombrándola.

Las escrituras por segundo (≥ 100 agregadas y ≥ 20 por cadena de planta, NFR-GOB-02) y las
latencias de NFR-GOB-01 son **tendencia** aquí y van al informe (infrastructure-design §9.2): solo
bloquean en el ``soak``.

``test_the_console_client_fails_naming…`` (marca ``integration``, en el check de integración):
contra una flota de dos nodos, una latencia de 250 ms inyectada en una ruta de consola hace que
el veredicto falle **nombrando esa ruta**; sin la latencia, la ruta pasa.
"""

from __future__ import annotations

import datetime as dt
import ssl
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.load.conftest import LoadTarget, load_seed
from tests.load.console_client import (
    MIN_SAMPLES,
    ConsoleClient,
    ConsoleTargets,
    delayed,
    evaluate,
)
from tests.load.profiles import ABSOLUTE_TARGETS, LoadProfile, nightly_profile
from tests.load.provision import Fleet
from tests.load.report import Drain, drains_of
from tests.load.test_load_ci import assert_functional, finish_report, ledger_check, run_load

pytestmark = pytest.mark.integration

RECONNECTION_WINDOW: Final = dt.timedelta(seconds=60)
"""Ventana mínima de medición de la consola tras cada reconexión (más, si el vaciado tarda más)."""
PERMANENT_THRESHOLD: Final = 0.01
INJECTED_ROUTE: Final = "GET /zones/{zone_id}/gates"
INJECTED_MS: Final = 250.0


class Console:
    """El cliente sintético ligado a la flota cuando arrancan los nodos."""

    def __init__(self, target: LoadTarget, seed: int) -> None:
        self.target = target
        self.seed = seed
        self.client: ConsoleClient | None = None

    def start(self, fleet: Fleet) -> None:
        self.client = console_client(self.target, fleet, self.seed)
        self.client.start()

    def stop(self) -> None:
        if self.client is not None:
            self.client.stop()


def console_client(
    target: LoadTarget, fleet: Fleet, seed: int, transport: httpx.BaseTransport | None = None
) -> ConsoleClient:
    return ConsoleClient(
        target.app.url,
        target.tls.ca_file,
        fleet.console,
        ConsoleTargets(
            tuple(node.node_id for node in fleet.nodes), fleet.zone_ids, fleet.plant_ids
        ),
        seed,
        transport=transport,
    )


def _parse(stamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def reconnection_windows(
    result: dict[str, Any], drains: Sequence[Drain]
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Desde que la plataforma vuelve hasta ``RECONNECTION_WINDOW`` o el fin del vaciado."""
    windows = []
    unreachable = [phase for phase in result["phases"] if phase["unreachable"]]
    for phase, drain in zip(unreachable, drains, strict=True):
        start = _parse(phase["end"])
        length = max(RECONNECTION_WINDOW, dt.timedelta(seconds=drain.seconds or 0.0))
        windows.append((start, start + length))
    return windows


def trend(analysis: dict[str, Any]) -> dict[str, Any]:
    """NFR-GOB-02 frente a sus objetivos absolutos: tendencia fuera del ``soak``."""
    targets = ABSOLUTE_TARGETS["NFR-GOB-02"]
    drains = analysis["drains"]
    aggregate = max((d["peak_writes_per_second"] or 0.0 for d in drains), default=None)
    plant = max((d["peak_plant_chain_writes_per_second"] or 0.0 for d in drains), default=None)
    return {
        "blocking": False,
        "NFR-GOB-02": {
            "aggregate_writes_per_second": aggregate,
            "aggregate_target": targets["aggregate_writes_per_second"],
            "plant_chain_writes_per_second": plant,
            "plant_chain_target": targets["plant_chain_writes_per_second"],
        },
        "NFR-GOB-01": analysis["node_latency"],
    }


@pytest.mark.nightly
def test_nightly_profile_mass_reconnection_and_four_hour_outage_keep_console_and_chain(
    load_target: LoadTarget,
    sealed_dataset: Path,
    clip_cache: Path,
    load_reports: Path,
    tmp_path: Path,
) -> None:
    profile = nightly_profile()
    console = Console(load_target, load_seed())
    fleet, result, analysis, output = run_load(
        load_target,
        profile,
        dataset=sealed_dataset,
        cache=clip_cache,
        work=tmp_path,
        console=console,
    )
    assert console.client is not None
    ledger = ledger_check(load_target, fleet, set(result["journal"]["emitted"]))
    windows = reconnection_windows(result, drains_of(result))
    verdict = evaluate(
        console.client.samples, windows=windows, unpublished=console.client.unpublished
    )
    overall = evaluate(console.client.samples, unpublished=console.client.unpublished)
    document = {
        "seed": load_seed(),
        "profile": profile.describe(),
        "target": "local: plataforma real del arnés de TASK-230 (vigia-api en proceso)",
        "started_at": result["started_at"],
        "finished_at": result["finished_at"],
        "results": analysis,
        "ledger": ledger,
        "console": {
            "blocking": True,
            "windows": [[start.isoformat(), end.isoformat()] for start, end in windows],
            "reconnection": verdict.to_json(),
            "whole_run": overall.to_json(),
        },
        "trend": trend(dict(analysis)),
    }
    finish_report(load_target, fleet, profile, load_reports, document, output)

    assert_functional(analysis, ledger)
    for drain in analysis["drains"]:
        assert drain["queued"] > 0, drain
        assert drain["accepted"] == drain["queued"], drain
    assert analysis["worst_permanent_ratio_15min"] <= PERMANENT_THRESHOLD
    assert verdict.passed, verdict.message()


def test_the_console_client_fails_naming_a_route_with_injected_latency(
    load_target: LoadTarget,
) -> None:
    fleet = load_target.provision(
        LoadProfile(
            "consola",
            nodes=2,
            zones_per_node=1,
            nodes_per_plant=2,
            speed_factor=60.0,
            steady_minutes=0.0,
        )
    )
    context = ssl.create_default_context(cafile=str(load_target.tls.ca_file))

    slow = console_client(
        load_target,
        fleet,
        load_seed(),
        delayed(httpx.HTTPTransport(verify=context), INJECTED_ROUTE, INJECTED_MS, time.sleep),
    )
    slow.run(rounds=MIN_SAMPLES)
    injected = evaluate(slow.samples, unpublished=slow.unpublished)
    print(injected.to_json())
    assert not injected.passed
    assert any(f.startswith(f"{INJECTED_ROUTE}: p95 ") for f in injected.failures), injected
    assert INJECTED_ROUTE in injected.message()

    control = console_client(load_target, fleet, load_seed(), httpx.HTTPTransport(verify=context))
    control.run(rounds=MIN_SAMPLES)
    clean = evaluate(control.samples, unpublished=control.unpublished)
    print(clean.to_json())
    assert not any(f.startswith(INJECTED_ROUTE) for f in clean.failures), clean.failures
