"""Fixtures de los perfiles de carga (TASK-231; LC-GOB-22).

Una sola plataforma por sesión (``load_target``), la del arnés de conformidad de TASK-230: base,
LocalStack y ``bootstrap``, con **dos trabajadores** de ``vigia-api`` como una tarea del piloto
(LC-GOB-20: 2 trabajadores de uvicorn por tarea): ``LoadApi`` en el proceso de la prueba (con las
métricas en memoria; ``tests.load.provision``) y un proceso ``vigia-api`` de verdad, los dos
creyendo el ``X-Forwarded-For`` del balanceador local. Los balanceadores ``aws``, ``app.`` y
``nodes.`` corren en procesos propios (``tests.load.balancer``), como el balanceador real fuera de
la tarea. Cada prueba aprovisiona su propia flota (otra organización) y lanza los nodos simulados
en un proceso aparte (``run_profile``).

La semilla sale de ``VIGIA_LOAD_SEED`` o es aleatoria; se imprime en el resumen
(``pytest_terminal_summary``), en la salida de cada prueba y en cada informe. Los informes van a
``VIGIA_LOAD_REPORT_DIR`` (el artefacto de 90 días de ``nightly``) o, sin ella, al directorio
temporal de la sesión.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from tests.conformance.conftest import API_MODULE
from tests.conformance.mtls_proxy import ServerTls, server_tls
from tests.conformance.platform_target import (
    BACKEND,
    PlatformStack,
    api_process_environment,
    platform_stack,
    wait_ready,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.load.balancer import Balancer, balancer_process
from tests.load.profiles import LoadProfile, write_sealed_dataset
from tests.load.provision import LOCAL_BALANCER, Fleet, FleetProvisioner, LoadApi, load_api
from tests.load.report import report_directory
from tests.resilience.harness import WALL, free_port
from tests.resilience.processes import process_group
from vigia_platform.shared.api.main import FORWARDED_VARIABLE

SEED_VARIABLE: Final = "VIGIA_LOAD_SEED"
MAX_SEED: Final = 2**53 - 1
API_LOG: Final = "api-carga.log"
FLUSH_SECONDS: Final = 600.0
"""Tope real para vaciar las bandejas al final de las fases (nunca decide un resultado: lo que
quede sin enviar es pérdida y falla la prueba)."""

_SEED: int | None = None


def load_seed() -> int:
    """La semilla de los perfiles de carga en esta sesión (``VIGIA_LOAD_SEED`` o aleatoria)."""
    global _SEED
    if _SEED is None:
        requested = os.environ.get(SEED_VARIABLE)
        _SEED = int(requested) if requested else secrets.randbelow(MAX_SEED + 1)
    return _SEED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _SEED is not None:
        terminalreporter.section("perfiles de carga de U-03")
        terminalreporter.write_line(f"semilla {_SEED} (reproducir: {SEED_VARIABLE}={_SEED})")


@dataclass(frozen=True)
class LoadTarget:
    """La plataforma de la sesión: dos trabajadores de ``vigia-api`` (el del proceso de la prueba,
    con las métricas en memoria, y un proceso ``vigia-api`` de verdad), como los 2 trabajadores
    de uvicorn por tarea de LC-GOB-20, detrás de los balanceadores ``app.`` y ``nodes.`` en sus
    propios procesos, con reparto por petición."""

    stack: PlatformStack
    api: LoadApi
    app: Balancer
    nodes: Balancer
    tls: ServerTls
    directory: Path

    def provision(self, profile: LoadProfile) -> Fleet:
        provisioner = FleetProvisioner(
            self.stack,
            self.api,
            self.app.url,
            self.tls.ca_file,
            self.directory,
            nodes_url=self.nodes.url,
        )
        return provisioner.fleet(profile)

    def logs(self) -> dict[str, str]:
        """Los registros de los trabajadores y de los balanceadores (``*.log`` y ``*.out``)."""
        return {
            path.name: path.read_text(encoding="utf-8", errors="replace")
            for pattern in ("*.log", "*.out")
            for path in self.directory.glob(pattern)
        }


@pytest.fixture(scope="session")
def load_target(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[LoadTarget]:
    directory = tmp_path_factory.mktemp("carga")  # fuera del árbol, nunca versionado
    tls = server_tls(directory, WALL.now())
    with (
        balancer_process(
            "aws", [localstack_endpoint.url], tls, directory, preserve_host=True
        ) as aws,
        platform_stack(
            postgres_endpoint,
            localstack_endpoint,
            directory,
            aws_url=aws.url,
            ca_bundle=tls.ca_file,
        ) as stack,
        load_api(stack.environ, free_port(), log_file=directory / API_LOG) as api,
        process_group(directory) as group,
    ):
        port = free_port()
        environ = {**api_process_environment(stack, port), FORWARDED_VARIABLE: LOCAL_BALANCER[0]}
        worker = group.start("api-b", API_MODULE, environ)
        worker_url = f"http://127.0.0.1:{port}"
        wait_ready(worker_url, alive=lambda: worker.process.poll() is None)
        backends = [api.url, worker_url]
        node_ca = directory / "vigia-node-ca.crt"
        node_ca.write_bytes(stack.node_ca_root())
        with (
            balancer_process("app", backends, tls, directory) as app,
            balancer_process("nodes", backends, tls, directory, client_ca=node_ca) as nodes,
        ):
            yield LoadTarget(stack, api, app, nodes, tls, directory)


@pytest.fixture(scope="session")
def sealed_dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_sealed_dataset(tmp_path_factory.mktemp("conjunto"), load_seed(), WALL.now())


@pytest.fixture(scope="session")
def clip_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Caché de clips sintéticos compartida por los perfiles de la sesión (PAT-REN-05)."""
    return tmp_path_factory.mktemp("clips")


@pytest.fixture(scope="session")
def load_reports(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return report_directory(tmp_path_factory.mktemp("informes-carga"))


@dataclass(frozen=True)
class DriverRun:
    code: int
    output: str
    result: dict[str, Any] | None


def run_profile(
    profile: LoadProfile,
    fleet: Fleet,
    *,
    seed: int,
    dataset: Path,
    cache: Path,
    work: Path,
    timeout: float,
) -> DriverRun:
    """Lanza ``tests.load.driver`` en un proceso aparte (sin credenciales de AWS ni de la base en
    su entorno) y espera a que termine."""
    plan = work / "plan.json"
    result_file = work / "result.json"
    outboxes = work / "bandejas"
    outboxes.mkdir()
    plan.write_text(
        json.dumps(
            {
                "profile": profile.name,
                "seed": seed,
                "fleet": str(fleet.fleet_file),
                "dataset": str(dataset),
                "cache": str(cache),
                "work": str(outboxes),
                "flush_seconds": FLUSH_SECONDS,
            }
        ),
        encoding="utf-8",
    )
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AWS_", "VIGIA_", "PG"))
    }
    environ["PYTHONPATH"] = str(BACKEND)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.load.driver",
            "--plan",
            str(plan),
            "--result",
            str(result_file),
        ],
        cwd=BACKEND,
        env=environ,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else None
    return DriverRun(completed.returncode, completed.stdout + completed.stderr, result)
