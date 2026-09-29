"""Pilas de U-02 en su orden de registro y despliegue (infrastructure-design §2.3).

``register_stacks`` las instancia en el orden ``foundation``, ``data``, ``edge``, ``compute``,
``observability`` y ``datasets``, con las dependencias de la tabla de §2.3, para que
``cdk deploy --all`` siga el orden del primer despliegue (deployment-architecture §5).
``vigia-datasets`` solo existe en ``pilot`` de la instancia compartida (D-8).
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
    ComputeStack,
    ObservabilityStack,
    DatasetsStack,
)

# Columna "Depende de" de la tabla de §2.3.
DEPENDENCIES: Mapping[str, tuple[str, ...]] = {
    FoundationStack.key: (),
    DataStack.key: (FoundationStack.key,),
    EdgeStack.key: (FoundationStack.key, DataStack.key),
    ComputeStack.key: (FoundationStack.key, DataStack.key, EdgeStack.key),
    ObservabilityStack.key: (ComputeStack.key,),
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
