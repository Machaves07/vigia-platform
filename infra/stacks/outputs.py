"""Contrato de salidas entre pilas (pendiente aditivo nº 38, infrastructure-design §2.3).

Una pila no lee a otra por ``Fn::ImportValue`` (que ata sus ciclos de vida: la
exportadora ya no puede cambiar ni borrar el valor), ni por búsquedas contra AWS al
sintetizar (``from_lookup``, que necesita credenciales). Lee por dos vías estables:

- **``roleName`` fijos**: los roles de tarea de §8 tienen nombre físico fijo,
  ``vigia-<rol>`` con el sufijo del despliegue (``vigia-worker-task`` en ``pilot``,
  ``vigia-worker-task-staging-<n>`` en ``staging-<n>``). Se importan por nombre.
- **Parámetros SSM ``/vigia/<despliegue>/<recurso>``**: la pila que crea el recurso publica
  su identificador con :func:`publish`; la que lo consume lo lee con :func:`import_value`,
  que CloudFormation resuelve al desplegar (parámetro de tipo
  ``AWS::SSM::Parameter::Value<String>``), nunca al sintetizar.

Con esto U-04 añade ``stacks/loop.py`` a ``app.py`` e importa el rol de tarea del worker,
``sg-worker`` y la zona DNS sin editar ninguna pila de U-02. ``staging-<n>`` importa por la
misma vía la zona que aloja ``pilot`` (D-8).
"""

from __future__ import annotations

import re
from enum import StrEnum

from aws_cdk import Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from config import EnvironmentConfig

# Límite de IAM para ``RoleName``.
_ROLE_NAME_MAX = 64
_PARAMETER = re.compile(r"^/vigia/[a-z0-9-]+/[a-z0-9-]+$")


class Output(StrEnum):
    """Recursos del contrato: el valor es el último segmento del parámetro SSM."""

    WORKER_TASK_ROLE_ARN = "worker-task-role-arn"
    WORKER_SECURITY_GROUP_ID = "sg-worker-id"
    HOSTED_ZONE_ID = "zone-id"
    HOSTED_ZONE_NAME = "zone-name"


class TaskRole(StrEnum):
    """Roles de tarea de ``vigia-compute`` con ``roleName`` fijo (§8)."""

    TASK_EXECUTION = "task-execution"
    API_TASK = "api-task"
    WORKER_TASK = "worker-task"
    MIGRATE_TASK = "migrate-task"
    ADMIN_TASK = "admin-task"


_DESCRIPTIONS: dict[Output, str] = {
    Output.WORKER_TASK_ROLE_ARN: "ARN del rol de tarea vigia-worker-task (salidas nº 38)",
    Output.WORKER_SECURITY_GROUP_ID: "Identificador del grupo sg-worker (salidas nº 38)",
    Output.HOSTED_ZONE_ID: "Identificador de la zona DNS vigia-zone (salidas nº 38)",
    Output.HOSTED_ZONE_NAME: "Nombre de la zona DNS vigia-zone (salidas nº 38)",
}


def parameter_name(deployment: str, output: Output) -> str:
    """``/vigia/<despliegue>/<recurso>``."""
    name = f"/vigia/{deployment}/{output.value}"
    if not _PARAMETER.match(name):
        raise ValueError(f"Nombre de parámetro fuera del contrato: {name!r}")
    return name


def role_name(config: EnvironmentConfig, role: TaskRole) -> str:
    """``roleName`` fijo de un rol de tarea en el despliegue."""
    name = config.resource_name(role.value)
    if len(name) > _ROLE_NAME_MAX:
        raise ValueError(f"El nombre de rol {name!r} supera {_ROLE_NAME_MAX} caracteres")
    return name


def publish(
    stack: Stack, config: EnvironmentConfig, output: Output, value: str
) -> ssm.StringParameter:
    """Publica el identificador de un recurso del contrato. Solo lo llama la pila que lo crea."""
    return ssm.StringParameter(
        stack,
        f"ContractOutput-{output.value}",
        parameter_name=parameter_name(config.deployment, output),
        string_value=value,
        description=_DESCRIPTIONS[output],
        tier=ssm.ParameterTier.STANDARD,
    )


def import_value(
    scope: Construct, config: EnvironmentConfig, output: Output, *, deployment: str | None = None
) -> str:
    """Lee un valor del contrato; CloudFormation lo resuelve al desplegar.

    ``deployment`` elige otro despliegue de origen: ``staging-<n>`` lee la zona de ``pilot``.
    """
    return ssm.StringParameter.value_for_string_parameter(
        scope, parameter_name(deployment or config.deployment, output)
    )


def import_worker_task_role(
    scope: Construct, construct_id: str, config: EnvironmentConfig, *, mutable: bool = False
) -> iam.IRole:
    """Rol ``vigia-worker-task`` por su nombre fijo. ``mutable=True`` solo para adjuntar una
    política gestionada aditiva (U-04, ``vigia-loop-worker``)."""
    return iam.Role.from_role_name(
        scope, construct_id, role_name(config, TaskRole.WORKER_TASK), mutable=mutable
    )


def import_worker_security_group(
    scope: Construct, construct_id: str, config: EnvironmentConfig
) -> ec2.ISecurityGroup:
    """Grupo ``sg-worker`` por su identificador publicado, sin permitir añadirle reglas: la
    regla que lo cita vive en el grupo del consumidor (U-04 §3.2)."""
    return ec2.SecurityGroup.from_security_group_id(
        scope,
        construct_id,
        import_value(scope, config, Output.WORKER_SECURITY_GROUP_ID),
        mutable=False,
    )


def import_hosted_zone(
    scope: Construct, construct_id: str, config: EnvironmentConfig
) -> route53.IHostedZone:
    """Zona DNS por atributos: la del propio despliegue o, en ``staging-<n>``, la del
    despliegue permanente que la aloja (D-8)."""
    source = config.deployment if config.hosted_zone_owned else config.hosted_zone_source
    return route53.HostedZone.from_hosted_zone_attributes(
        scope,
        construct_id,
        hosted_zone_id=import_value(scope, config, Output.HOSTED_ZONE_ID, deployment=source),
        zone_name=import_value(scope, config, Output.HOSTED_ZONE_NAME, deployment=source),
    )
