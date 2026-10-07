"""FS-GOB-07 · Reconexión masiva de 100 nodos con una hora de cola (NFR-GOB-02, 19, 50; PR-GOB-22;
PAT-GOB-RES-01, RES-05; LC-GOB-22).

Sobre la plataforma de los perfiles de carga de TASK-231 (``tests/load``: la de producción con
todas las unidades, ``vigia-admin bootstrap`` y la autoridad de nodos efímera de la ejecución, dos
trabajadores de ``vigia-api`` tras los balanceadores ``app.`` y ``nodes.`` con mTLS), con el **nodo
simulado del kit de U-01** (``tests.load.driver``: cliente real del contrato, bandeja SQLite,
reintentos y claves de idempotencia) en un proceso aparte y el **cliente sintético de consola**
(``tests.load.console_client``) midiendo en paralelo las rutas de NFR-GOB-03 con una sesión real.

**Inyección**: 100 nodos y 300 zonas (5 plantas) a ``speed_factor`` 60; tras un tramo en régimen,
la plataforma deja de ser alcanzable durante **una hora simulada** (los nodos encolan) y vuelve:
los 100 nodos **vacían su cola a la vez**. La semilla es la de los perfiles de carga
(``VIGIA_LOAD_SEED``, en el informe). Con ``VIGIA_LOAD_SCALE=smoke``, la misma forma reducida a 20
nodos (para ensayar el escenario en un PC compartido; ``nightly`` corre la escala completa).

**Resultado esperado**:

- NFR-GOB-02: aceptado = emitido, sin pérdida, sin cola muerta, sin duplicados en el expediente;
  ningún ``rate_limited`` (y ninguno por debajo del mínimo de NFR-CTR-02) ni ``temporarily_
  unavailable`` por saturación; todo lo encolado se acepta en el vaciado;
- **sin alarma de rechazos** (NFR-GOB-46): la peor proporción de rechazos permanentes en 15
  minutos no pasa del 1 %;
- **las rutas de personas siguen dentro de su p95** durante la reconexión (NFR-GOB-19): si alguna
  no, el escenario falla nombrándola;
- **cadena íntegra** al final: la verificación completa de cada cadena de planta.

Solo en ``nightly`` (pesado: 100 nodos). Solo datos generados.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Las fixtures de sesión de los perfiles de carga (plataforma, conjunto sellado sintético y caché de
# clips): la misma plataforma que ``tests/load``.
from tests.load.conftest import (  # noqa: F401
    LoadTarget,
    clip_cache,
    load_seed,
    load_target,
    sealed_dataset,
)
from tests.load.console_client import evaluate
from tests.load.profiles import scaled
from tests.load.report import drains_of
from tests.load.test_load_ci import assert_functional, ledger_check, run_load
from tests.load.test_load_nightly import (
    PERMANENT_THRESHOLD,
    Console,
    reconnection_windows,
)
from tests.resilience.harness import scenario

pytestmark = [pytest.mark.integration, pytest.mark.nightly]


def test_fs_gob_07_mass_reconnection_of_a_hundred_nodes_with_an_hour_of_queue(
    load_target: LoadTarget,  # noqa: F811
    sealed_dataset: Path,  # noqa: F811
    clip_cache: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    profile = scaled("fs-gob-07")
    with scenario(
        "FS-GOB-07",
        title="Reconexión masiva de 100 nodos con una hora de cola",
        injection=(
            f"{profile.nodes} nodos simulados del kit a speed_factor {profile.speed_factor:g}"
            f" vaciando {profile.queue_hours:g} h de cola a la vez, con el cliente sintético de"
            " consola midiendo"
        ),
        expected=(
            "NFR-GOB-02 (sin pérdida, sin duplicados, sin rate_limited ni saturación); rutas de"
            " personas dentro de su p95; sin alarma de rechazos; cadena íntegra"
        ),
    ) as run:
        console = Console(load_target, load_seed())
        fleet, result, analysis, _ = run_load(
            load_target,
            profile,
            dataset=sealed_dataset,
            cache=clip_cache,
            work=tmp_path,
            console=console,
        )
        assert console.client is not None
        ledger = ledger_check(load_target, fleet, set(result["journal"]["emitted"]))
        drains = drains_of(result)
        windows = reconnection_windows(result, drains)
        verdict = evaluate(
            console.client.samples, windows=windows, unpublished=console.client.unpublished
        )
        run.observe(
            load_seed=load_seed(),
            profile=profile.describe(),
            emitted=analysis["emitted"],
            accepted=analysis["accepted"],
            lost=analysis["lost"],
            rate_limited_by_operation=analysis["rate_limited_by_operation"],
            rate_limited_below_minimum=analysis["rate_limited_below_minimum"],
            drains=analysis["drains"],
            worst_permanent_ratio_15min=analysis["worst_permanent_ratio_15min"],
            console=verdict.to_json(),
            ledger=ledger,
        )

        assert_functional(analysis, ledger)
        assert analysis["drains"], "hubo reconexión"
        for drain in analysis["drains"]:
            assert drain["queued"] > 0, drain
            assert drain["accepted"] == drain["queued"], drain
        assert analysis["worst_permanent_ratio_15min"] <= PERMANENT_THRESHOLD
        assert verdict.passed, verdict.message()
