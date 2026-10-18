"""Aplicación CDK de U-02 (infrastructure-design §2.3, TASK-144).

Lee el contexto (``environment``, ``instance``, ``first_deploy``, ``ca_rotation``,
``nat_per_az``, ``nodes_tls_mode``; valores por defecto en ``cdk.json``; y el opcional
``provider_organization_id``), registra las pilas del despliegue en su orden y aplica las
etiquetas globales ``project=vigia``, ``unit=U-02``, ``managed_by=cdk`` y
``environment=<entorno>``. Calificador de arranque ``vigia`` (``cdk bootstrap --qualifier
vigia``); la cuenta no se fija, así que ``cdk synth`` no necesita credenciales.

Una pila de otra unidad se añade aquí, después de ``register_stacks``, y lee lo que
necesita de U-02 por el contrato de ``stacks/outputs.py`` (pendiente nº 38). La primera es
``vigia-loop`` de U-04 (TASK-331): séptima pila de la cuenta y sexta desplegable por entorno,
con el mismo contexto, la etiqueta ``unit=U-04`` y la última en el orden de despliegue.
"""

from __future__ import annotations

from collections.abc import Mapping

from aws_cdk import App, Tags
from constructs import Construct

from config import CONTEXT_KEYS, CONTEXT_PROVIDER_ORGANIZATION, EnvironmentConfig, load_config
from stacks import VigiaStack, register_stacks
from stacks.compute import ComputeStack
from stacks.data import DataStack
from stacks.edge import EdgeStack
from stacks.foundation import FoundationStack
from stacks.loop import LoopStack

UNIT = "U-02"
# ``vigia-loop`` va al final: depende de las cuatro pilas de U-02 que importa (U-04
# deployment-architecture §3). La dependencia ordena el despliegue y no cambia ninguna plantilla.
LOOP_DEPENDENCIES = (FoundationStack.key, DataStack.key, EdgeStack.key, ComputeStack.key)


def global_tags(config: EnvironmentConfig) -> dict[str, str]:
    """Etiquetas de todo recurso de U-02 (infrastructure-design, «Cómo leer este documento»)."""
    return {
        "project": "vigia",
        "unit": UNIT,
        "managed_by": "cdk",
        "environment": config.environment,
    }


def read_config(app: App) -> EnvironmentConfig:
    """Configuración del despliegue a partir del contexto de la aplicación."""
    keys = (*CONTEXT_KEYS, CONTEXT_PROVIDER_ORGANIZATION)
    return load_config({key: app.node.try_get_context(key) for key in keys})


def apply_tags(scope: Construct, config: EnvironmentConfig) -> None:
    """Etiqueta los recursos de todas las pilas del árbol, también las de otras unidades."""
    for key, value in global_tags(config).items():
        Tags.of(scope).add(key, value)


def register_loop(
    app: Construct, config: EnvironmentConfig, registered: Mapping[str, VigiaStack]
) -> LoopStack:
    """Añade ``vigia-loop`` (U-04) después de las pilas de U-02."""
    loop = LoopStack(app, config, tags={**global_tags(config), "unit": LoopStack.unit})
    for dependency in LOOP_DEPENDENCIES:
        loop.add_stack_dependency(registered[dependency])
    return loop


def build(app: App | None = None) -> App:
    """Construye la aplicación con las pilas de U-02 y la de U-04 para el contexto recibido."""
    app = app or App()
    config = read_config(app)
    registered = register_stacks(app, config, tags=global_tags(config))
    register_loop(app, config, registered)
    apply_tags(app, config)
    return app


if __name__ == "__main__":
    build().synth()
