"""Fixtures de sesión de la suite de conformidad contra la plataforma (TASK-230).

Una sola pila por sesión (``platform_target``): base, LocalStack, ``bootstrap``, ``vigia-api`` en
el proceso de la prueba (con el exportador de métricas en memoria) detrás de su balanceador local,
y el aprovisionamiento. ``balanced_target`` añade **dos procesos** ``vigia-api`` de verdad sobre la
misma base detrás de otro balanceador con reparto alterno por petición (NFR-GOB-15).

La semilla del kit sale de ``VIGIA_CONFORMANCE_SEED`` o es aleatoria; se imprime en el resumen
(``pytest_terminal_summary``) y va en cada informe. Los informes y la comparación de métricas van
a ``VIGIA_CONFORMANCE_REPORT_DIR`` (el artefacto ``informe-conformidad`` de ``nightly.yml``) o, sin
ella, al directorio temporal de la sesión.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from hypothesis import settings

from tests.conformance.mtls_proxy import MtlsProxy, ServerTls, mtls_proxy, server_tls
from tests.conformance.platform_target import (
    InProcessApi,
    PlatformStack,
    Provisioned,
    Provisioner,
    api_process_environment,
    in_process_api,
    platform_stack,
    wait_ready,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.resilience.harness import WALL, free_port
from tests.resilience.processes import ProcessGroup, process_group

SEED_VARIABLE: Final = "VIGIA_CONFORMANCE_SEED"
REPORT_DIR_VARIABLE: Final = "VIGIA_CONFORMANCE_REPORT_DIR"
API_MODULE: Final = "vigia_platform.shared.api.main"
"""``vigia-api`` (``pyproject.toml``): el punto de entrada de la imagen, como módulo."""
MAX_SEED: Final = 2**53 - 1

_SEED: int | None = None


def conformance_seed() -> int:
    """La semilla del kit en esta sesión (``VIGIA_CONFORMANCE_SEED`` o aleatoria)."""
    global _SEED
    if _SEED is None:
        requested = os.environ.get(SEED_VARIABLE)
        _SEED = int(requested) if requested else secrets.randbelow(MAX_SEED + 1)
    return _SEED


def conformance_profile() -> str:
    """El perfil del kit es el de Hypothesis de la sesión: ``ci`` o ``nightly``."""
    return "nightly" if settings.get_current_profile_name() == "nightly" else "ci"


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _SEED is not None:
        terminalreporter.section("conformidad de U-01 contra la plataforma")
        terminalreporter.write_line(
            f"perfil {conformance_profile()} semilla {_SEED} (reproducir: {SEED_VARIABLE}={_SEED})"
        )


@dataclass(frozen=True)
class Target:
    """Un objetivo de la suite: los dos balanceadores y lo aprovisionado."""

    nodes: MtlsProxy
    app: MtlsProxy
    provisioned: Provisioned


@dataclass(frozen=True)
class PlatformTarget(Target):
    stack: PlatformStack
    api: InProcessApi
    tls: ServerTls
    directory: Path


@pytest.fixture(scope="session")
def report_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    configured = os.environ.get(REPORT_DIR_VARIABLE)
    directory = Path(configured) if configured else tmp_path_factory.mktemp("informes")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture(scope="session")
def platform_target(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PlatformTarget]:
    directory = tmp_path_factory.mktemp("conformidad")  # fuera del árbol, nunca versionado
    tls = server_tls(directory, WALL.now())
    with (
        mtls_proxy("aws", [localstack_endpoint.url], tls, preserve_host=True) as aws,
        platform_stack(
            postgres_endpoint,
            localstack_endpoint,
            directory,
            aws_url=aws.url,
            ca_bundle=tls.ca_file,
        ) as stack,
        in_process_api(stack.environ, free_port()) as api,
    ):
        node_ca = directory / "vigia-node-ca.crt"
        node_ca.write_bytes(stack.node_ca_root())
        with (
            mtls_proxy("app", [api.url], tls) as app,
            mtls_proxy("nodes", [api.url], tls, client_ca=node_ca) as nodes,
        ):
            provisioned = Provisioner(stack, api, app.url, tls.ca_file, directory).provision()
            provisioned.wait_until_ready()
            yield PlatformTarget(nodes, app, provisioned, stack, api, tls, directory)


@dataclass(frozen=True)
class BalancedTarget(Target):
    group: ProcessGroup
    backends: tuple[str, ...]


@pytest.fixture(scope="session")
def balanced_target(platform_target: PlatformTarget) -> Iterator[BalancedTarget]:
    """Dos procesos ``vigia-api`` sobre la misma base tras un balanceador local (NFR-GOB-15)."""
    stack = platform_target.stack
    with process_group(platform_target.directory) as group:
        backends = []
        for name in ("api-a", "api-b"):
            port = free_port()
            spawned = group.start(name, API_MODULE, api_process_environment(stack, port))
            url = f"http://127.0.0.1:{port}"
            wait_ready(url, alive=lambda spawned=spawned: spawned.process.poll() is None)
            backends.append(url)
        node_ca = platform_target.provisioned.node_ca_file
        with (
            mtls_proxy("app", backends, platform_target.tls) as app,
            mtls_proxy("nodes", backends, platform_target.tls, client_ca=node_ca) as nodes,
        ):
            yield BalancedTarget(nodes, app, platform_target.provisioned, group, tuple(backends))
