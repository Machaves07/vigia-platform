"""Síntesis de la aplicación por despliegue y pilas de prueba para las reglas.

La síntesis usa el mismo contexto que ``cdk synth`` (``cdk.json``) con los valores que
cada prueba sustituye, y no necesita credenciales: la cuenta queda sin resolver.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack
from aws_cdk.assertions import Template

import app as cdk_app
from config import EnvironmentConfig

INFRA = Path(__file__).resolve().parents[1]
CDK_JSON = INFRA / "cdk.json"
PROBE_STACK = "vigia-probe"


def cdk_settings() -> dict[str, Any]:
    settings: dict[str, Any] = json.loads(CDK_JSON.read_text(encoding="utf-8"))
    return settings


def default_context() -> dict[str, Any]:
    context: dict[str, Any] = cdk_settings()["context"]
    return context


@dataclass(frozen=True)
class Synthesized:
    """Resultado de sintetizar la aplicación para un contexto."""

    config: EnvironmentConfig
    stack_names: tuple[str, ...]
    templates: Mapping[str, dict[str, Any]]
    regions: Mapping[str, str]
    tags: Mapping[str, Mapping[str, str]]
    dependencies: Mapping[str, tuple[str, ...]]
    missing_context: tuple[Any, ...]


def synthesize(
    extend: Callable[[App, EnvironmentConfig], None] | None = None, **context: Any
) -> Synthesized:
    """Sintetiza la aplicación con ``cdk.json`` y los valores de ``context``.

    ``extend`` añade pilas o recursos después de ``app.build``, como haría otra unidad.
    """
    # Como la CLI: con el informe de versión, cada pila lleva ``CDKMetadata`` y las pilas
    # aún vacías (TASK-145 a 150) pasan la validación por defecto de CDK.
    application = cdk_app.build(
        App(context={**default_context(), **context}, analytics_reporting=True)
    )
    config = cdk_app.read_config(application)
    if extend:
        extend(application, config)
    assembly = application.synth()
    stack_names = tuple(
        child.stack_name for child in application.node.children if isinstance(child, Stack)
    )
    artifacts = {artifact.stack_name: artifact for artifact in assembly.stacks}
    # ``missing`` lista las búsquedas contra AWS pendientes (``from_lookup``).
    manifest = json.loads(Path(assembly.directory, "manifest.json").read_text(encoding="utf-8"))
    return Synthesized(
        config=config,
        stack_names=stack_names,
        templates={name: artifacts[name].template for name in stack_names},
        regions={name: artifacts[name].environment.region for name in stack_names},
        tags={name: dict(artifacts[name].tags) for name in stack_names},
        dependencies={
            name: tuple(d.id for d in artifacts[name].dependencies if not d.id.endswith(".assets"))
            for name in stack_names
        },
        missing_context=tuple(manifest.get("missing", ())),
    )


# Despliegues sobre los que se aplican todas las reglas.
DEPLOYMENTS: Mapping[str, Mapping[str, Any]] = {
    "pilot": {},
    "pilot-first-deploy": {"first_deploy": "true"},
    "pilot-ca-rotation": {"ca_rotation": "true"},
    "staging-7": {"environment": "staging-7"},
    "dedicated-acme": {"instance": "acme"},
}


@pytest.fixture(scope="session", params=sorted(DEPLOYMENTS))
def deployment(request: pytest.FixtureRequest) -> Synthesized:
    """Cada despliegue de :data:`DEPLOYMENTS`, sintetizado una vez por sesión."""
    return _synthesized(str(request.param))


_CACHE: dict[str, Synthesized] = {}


def _synthesized(name: str) -> Synthesized:
    if name not in _CACHE:
        _CACHE[name] = synthesize(**DEPLOYMENTS[name])
    return _CACHE[name]


@pytest.fixture(
    scope="session", params=sorted(n for n in DEPLOYMENTS if not n.startswith("staging-"))
)
def permanent_deployment(request: pytest.FixtureRequest) -> Synthesized:
    """Los despliegues permanentes (``pilot`` compartido y dedicado)."""
    return _synthesized(str(request.param))


@pytest.fixture(scope="session")
def pilot() -> Synthesized:
    return _synthesized("pilot")


@pytest.fixture(scope="session")
def staging() -> Synthesized:
    return _synthesized("staging-7")


def probe(build: Callable[[Stack], object], *, region: str = "us-east-1") -> dict[str, Any]:
    """Plantilla de una pila de prueba construida con ``build`` (casos negativos)."""
    stack = Stack(App(), PROBE_STACK, env=Environment(region=region))
    build(stack)
    return dict(Template.from_stack(stack).to_json())
