"""FS-GOB-07 · Reconexión masiva de 100 nodos con una hora de cola (NFR-GOB-02, 19, 50; PR-GOB-22;
PAT-GOB-RES-01, RES-05; LC-GOB-22).

Sobre la plataforma de producción de los perfiles de carga (TASK-230 y 231: todas las unidades,
``vigia-admin bootstrap`` y la autoridad de nodos efímera de la ejecución) con la **dotación mínima
del piloto**: cuatro procesos ``vigia-api`` de verdad (2 tareas de 2 trabajadores) tras los
balanceadores ``app.`` y ``nodes.`` con mTLS (``gob_support.restartable_platform``: ningún
trabajador comparte el proceso, ni el GIL, con la prueba ni con el cliente de consola), con el
**nodo simulado del kit de U-01** (``tests.load.driver``: cliente real del contrato, bandeja SQLite,
reintentos y claves de idempotencia) en un proceso aparte y el **cliente sintético de consola**
(``tests.load.console_client``) midiendo en paralelo las rutas de NFR-GOB-03 con una sesión real.

**Inyección**: 100 nodos y 300 zonas (5 plantas) a ``speed_factor`` 60; tras un tramo en régimen,
la plataforma deja de ser alcanzable durante **una hora simulada** (los nodos encolan) y vuelve:
los 100 nodos **vacían su cola a la vez**. La semilla es la de los perfiles de carga
(``VIGIA_LOAD_SEED``, en el informe). Con ``VIGIA_LOAD_SCALE=smoke``, la misma forma reducida a 20
nodos (para ensayar el escenario en un PC compartido; ``nightly`` corre la escala completa).

**Resultado esperado** (bloqueante):

- NFR-GOB-02: aceptado = emitido, sin pérdida, sin cola muerta, sin duplicados en el expediente;
  ningún ``rate_limited`` (y ninguno por debajo del mínimo de NFR-CTR-02); todo lo encolado se
  acepta en el vaciado;
- **sin alarma de rechazos** (NFR-GOB-46): la peor proporción de rechazos permanentes en 15
  minutos no pasa del 1 %;
- ninguna ruta de consola responde con error (fuera de 2xx y no ``503``);
- **cadena íntegra** al final: la verificación completa de cada cadena de planta.

**Tendencia** (A-65; infrastructure-design §9.2): en este banco (un runner de 4 vCPU con la
plataforma, los 100 nodos y la consola en la misma máquina) **no** fallan la ejecución, se
registran en el informe y bloquean en el ``soak`` sobre ``staging`` (VIG-174), con la dotación
real del piloto:

- los transitorios por saturación de NFR-GOB-02 (``temporarily_unavailable`` y ``503``), en
  ``saturation_transients``;
- el p95 de cada ruta de personas durante la reconexión (NFR-GOB-19), en ``console``, con la ruta
  que lo incumple nombrada en ``failures``.

Solo en ``nightly`` (pesado: 100 nodos). Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint

# Fixtures de sesión de los perfiles de carga: el conjunto sellado sintético y la caché de clips.
from tests.load.conftest import (  # noqa: F401
    clip_cache,
    load_seed,
    sealed_dataset,
)
from tests.load.console_client import evaluate
from tests.load.profiles import scaled
from tests.load.report import drains_of
from tests.load.test_load_ci import (
    assert_functional,
    ledger_check,
    run_load,
    saturation_transients,
)
from tests.load.test_load_nightly import (
    PERMANENT_THRESHOLD,
    Console,
    reconnection_windows,
)
from tests.resilience.gob_support import (
    PILOT_MINIMUM_PROCESSES,
    RestartablePlatform,
    restartable_platform,
)
from tests.resilience.harness import scenario

pytestmark = [pytest.mark.integration, pytest.mark.nightly]


@pytest.fixture(scope="module")
def platform(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[RestartablePlatform]:
    directory = tmp_path_factory.mktemp("fs-gob-07")  # fuera del árbol, nunca versionado
    with restartable_platform(
        postgres_endpoint,
        localstack_endpoint,
        directory,
        processes=PILOT_MINIMUM_PROCESSES,
        restartable=False,
    ) as built:
        yield built


def test_fs_gob_07_mass_reconnection_of_a_hundred_nodes_with_an_hour_of_queue(
    platform: RestartablePlatform,
    sealed_dataset: Path,  # noqa: F811
    clip_cache: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    # ``run_load``, la consola y ``ledger_check`` usan del objetivo de carga lo que esta plataforma
    # también da: ``provision``, ``api``, ``app``, ``tls`` y ``stack``.
    load_target: Any = platform
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
            "NFR-GOB-02 (sin pérdida, sin duplicados, sin rate_limited); sin alarma de rechazos;"
            " consola sin errores; cadena íntegra. Tendencia (A-65): transitorios por saturación"
            " y p95 de las rutas de personas"
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
        overall = evaluate(console.client.samples, unpublished=console.client.unpublished)
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
            saturation_transients={"blocking": False, **saturation_transients(analysis)},
            console={"blocking": False, **verdict.to_json()},
            console_errors=list(overall.errors),
            ledger=ledger,
        )

        assert_functional(analysis, ledger, saturation_blocks=False)
        assert analysis["drains"], "hubo reconexión"
        for drain in analysis["drains"]:
            assert drain["queued"] > 0, drain
            assert drain["accepted"] == drain["queued"], drain
        assert analysis["worst_permanent_ratio_15min"] <= PERMANENT_THRESHOLD
        assert not overall.errors, overall.errors_message()
