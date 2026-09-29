"""Aplicación CDK de U-02 (infrastructure-design §2.3, TASK-144).

Lee el contexto (``environment``, ``instance``, ``first_deploy``, ``ca_rotation``; valores
por defecto en ``cdk.json``), registra las pilas del despliegue en su orden y aplica las
etiquetas globales ``project=vigia``, ``unit=U-02``, ``managed_by=cdk`` y
``environment=<entorno>``. Calificador de arranque ``vigia`` (``cdk bootstrap --qualifier
vigia``); la cuenta no se fija, así que ``cdk synth`` no necesita credenciales.

Una pila de otra unidad se añade aquí, después de ``register_stacks``, y lee lo que
necesita de U-02 por el contrato de ``stacks/outputs.py`` (pendiente nº 38).
"""

from __future__ import annotations

from aws_cdk import App, Tags
from constructs import Construct

from config import CONTEXT_KEYS, EnvironmentConfig, load_config
from stacks import register_stacks

UNIT = "U-02"


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
    return load_config({key: app.node.try_get_context(key) for key in CONTEXT_KEYS})


def apply_tags(scope: Construct, config: EnvironmentConfig) -> None:
    """Etiqueta los recursos de todas las pilas del árbol, también las de otras unidades."""
    for key, value in global_tags(config).items():
        Tags.of(scope).add(key, value)


def build(app: App | None = None) -> App:
    """Construye la aplicación con las pilas de U-02 para el contexto recibido."""
    app = app or App()
    config = read_config(app)
    register_stacks(app, config, tags=global_tags(config))
    apply_tags(app, config)
    return app


if __name__ == "__main__":
    build().synth()
