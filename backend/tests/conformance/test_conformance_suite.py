"""``vigia-conformance run`` contra las rutas reales de ``node_api`` (NFR-GOB-61 y 15; TASK-230).

La orden del kit de U-01 fijado en ``uv.lock``, como subproceso y por URL, con el perfil de la
sesión (``ci`` en el check «backend (pruebas de integración)», ``nightly`` en el trabajo
``conformance`` de ``nightly.yml``): ``--target`` es la base de la ingesta del balanceador
``nodes.`` (mTLS con la raíz efímera de ``vigia-node-ca``) y ``--enrollment-url``, la del alta en
``app.``. Detrás, **dos procesos** ``vigia-api`` con la raíz de composición de producción y reparto
alterno por petición (NFR-GOB-15): la suite entera, también las secuencias de latido y de ingesta
con la misma clave de idempotencia, pasa sin afinidad de instancia.

- Código de salida 0 obligatorio; la semilla se imprime y está en el ``ConformanceReport``, que
  queda en el directorio de informes (artefacto ``informe-conformidad``).
- El código de alta del aprovisionamiento no aparece en la salida de la orden, en el informe ni
  en el JSON de ``--provision`` (PR-GOB-31).

Lo que el kit omite contra una plataforma por URL sale ``skipped`` con su motivo, nunca
``passed`` (``enrollment.code_single_use`` y ``code_expired`` necesitan códigos de alta a demanda
para un nodo ya dado de alta, que la plataforma no emite: BLM §3.4; las de ``CONTROL``, el reloj
de la plataforma simulada; las de ``FLOOD``, permiso para saturar).
"""

from __future__ import annotations

import json
import shutil
from typing import Final

import pytest

from tests.conformance.conftest import (
    BalancedTarget,
    conformance_profile,
    conformance_seed,
)
from tests.conformance.platform_target import conformance_command, run_conformance

pytestmark = pytest.mark.integration

TIMEOUT_SECONDS: Final = {"ci": 1_800.0, "nightly": 12_600.0}
"""Tope de la orden (nunca decide el resultado): 30 min en ``ci`` y 3,5 h en ``nightly``."""


def test_the_kit_passes_against_two_processes_behind_the_local_balancer(
    balanced_target: BalancedTarget, report_directory: object, tmp_path: object
) -> None:
    from pathlib import Path

    profile, seed = conformance_profile(), conformance_seed()
    print(f"vigia-conformance perfil {profile} semilla {seed}")
    report_file = Path(str(tmp_path)) / "conformance-report.json"
    command = conformance_command(
        nodes_url=balanced_target.nodes.url,
        app_url=balanced_target.app.url,
        provision_file=balanced_target.provisioned.provision_file,
        profile=profile,
        seed=seed,
        report_file=report_file,
    )
    run = run_conformance(command, report_file, timeout=TIMEOUT_SECONDS[profile])
    destination = Path(str(report_directory))
    (destination / f"conformance-{profile}-{seed}.txt").write_text(run.output, encoding="utf-8")
    if report_file.is_file():
        shutil.copy(report_file, destination / f"conformance-{profile}-{seed}.json")
    print(run.output)

    assert run.code == 0, run.output
    assert run.report is not None
    assert run.report["seed"] == seed
    assert run.report["passed"] is True
    assert f"Semilla {seed}" in run.output
    results = {check["check_id"]: check["result"] for check in run.report["checks"]}
    assert "failed" not in results.values()
    # Las diez rutas, con los dos procesos (reparto por petición).
    served = balanced_target.nodes.served("/api/nodes/")
    assert {exchange.backend for exchange in served} == {0, 1}
    provision = balanced_target.provisioned.provision_file.read_text(encoding="utf-8")
    assert "enrollment_code" not in json.dumps(run.report)
    assert "enrollment_code" not in provision
