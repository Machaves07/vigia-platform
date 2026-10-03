"""Pilas de U-02 en su orden de registro y despliegue (infrastructure-design §2.3).

``register_stacks`` las instancia en el orden ``foundation``, ``data``, ``edge``,
``observability``, ``compute`` y ``datasets``, con las dependencias de la tabla de §2.3, para que
``cdk deploy --all`` siga el orden del primer despliegue (deployment-architecture §5).
``vigia-datasets`` solo existe en ``pilot`` de la instancia compartida (D-8).

Única diferencia con §2.3 (TASK-149, nota de VIG-48): ``vigia-observability`` va antes que
``vigia-compute`` y no al revés. Crea los grupos ``/vigia/<despliegue>/*`` que las tareas de
``vigia-compute`` importan por nombre, y ``awslogs`` no arranca una tarea sin su grupo:
``vigia-migrate`` (paso 6 del primer despliegue) fallaría si la pila llegara en el paso 9.
``cdk deploy vigia-compute`` despliega antes su dependencia.
"""

from __future__ import annotations

from collections.abc import Mapping

from constructs import Construct

from config import EnvironmentConfig
from stacks.base import VigiaStack
from stacks.compute import ComputeStack
from stacks.data import DataStack
from stacks.datasets import DatasetsStack
from stacks.edge import EdgeStack
from stacks.foundation import FoundationStack
from stacks.observability import ObservabilityStack

__all__ = ["DEPENDENCIES", "STACK_ORDER", "VigiaStack", "register_stacks"]

STACK_ORDER: tuple[type[VigiaStack], ...] = (
    FoundationStack,
    DataStack,
    EdgeStack,
    ObservabilityStack,
    ComputeStack,
    DatasetsStack,
)

# Columna "Depende de" de la tabla de §2.3, con ``vigia-observability`` antes de ``vigia-compute``.
DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    FoundationStack.key: (),
    DataStack.key: (FoundationStack.key,),
    EdgeStack.key: (FoundationStack.key, DataStack.key),
    ObservabilityStack.key: (FoundationStack.key, DataStack.key, EdgeStack.key),
    ComputeStack.key: (
        FoundationStack.key,
        DataStack.key,
        EdgeStack.key,
        ObservabilityStack.key,
    ),
    DatasetsStack.key: (),
}


def register_stacks(
    scope: Construct, config: EnvironmentConfig, *, tags: Mapping[str, str]
) -> dict[str, VigiaStack]:
    """Instancia las pilas del despliegue, con las etiquetas globales, y devuelve cada una por
    su nombre corto."""
    registered: dict[str, VigiaStack] = {}
    for stack_type in STACK_ORDER:
        if stack_type is DatasetsStack and not config.include_datasets:
            continue
        stack = stack_type(scope, config, tags=tags)
        for dependency in DEPENDENCIES[stack_type.key]:
            stack.add_stack_dependency(registered[dependency])
        registered[stack_type.key] = stack
    return registered
