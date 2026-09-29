"""Configuración por entorno de la aplicación CDK (infrastructure-design §2.1 y §2.3, D-8)."""

from __future__ import annotations

from collections.abc import Mapping

from config.environment import (
    BOOTSTRAP_QUALIFIER,
    CONTEXT_CA_ROTATION,
    CONTEXT_ENVIRONMENT,
    CONTEXT_FIRST_DEPLOY,
    CONTEXT_INSTANCE,
    CONTEXT_KEYS,
    CONTEXT_NAT_PER_AZ,
    CONTEXT_NODES_TLS_MODE,
    REGION,
    ContextError,
    EnvironmentConfig,
    NodesTlsMode,
    ObjectLock,
    ObjectLockMode,
    parse_environment,
    parse_flag,
    parse_instance,
    parse_nodes_tls_mode,
    require,
)
from config.pilot import pilot_config
from config.staging import staging_config

__all__ = [
    "BOOTSTRAP_QUALIFIER",
    "CONTEXT_KEYS",
    "REGION",
    "ContextError",
    "EnvironmentConfig",
    "NodesTlsMode",
    "ObjectLock",
    "ObjectLockMode",
    "load_config",
]


def load_config(context: Mapping[str, object]) -> EnvironmentConfig:
    """Construye la configuración a partir de los seis valores de contexto obligatorios."""
    environment, ephemeral = parse_environment(require(context, CONTEXT_ENVIRONMENT))
    instance = parse_instance(require(context, CONTEXT_INSTANCE))
    first_deploy = parse_flag(CONTEXT_FIRST_DEPLOY, require(context, CONTEXT_FIRST_DEPLOY))
    ca_rotation = parse_flag(CONTEXT_CA_ROTATION, require(context, CONTEXT_CA_ROTATION))
    nat_per_az = parse_flag(CONTEXT_NAT_PER_AZ, require(context, CONTEXT_NAT_PER_AZ))
    nodes_tls_mode = parse_nodes_tls_mode(require(context, CONTEXT_NODES_TLS_MODE))
    if ephemeral:
        return staging_config(
            environment,
            instance=instance,
            first_deploy=first_deploy,
            ca_rotation=ca_rotation,
            nat_per_az=nat_per_az,
            nodes_tls_mode=nodes_tls_mode,
        )
    return pilot_config(
        instance=instance,
        first_deploy=first_deploy,
        ca_rotation=ca_rotation,
        nat_per_az=nat_per_az,
        nodes_tls_mode=nodes_tls_mode,
    )
