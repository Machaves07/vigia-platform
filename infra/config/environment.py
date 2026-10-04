"""Modelo de configuración de un despliegue y lectura estricta del contexto de CDK.

Un despliegue queda determinado por seis valores de contexto (``cdk.json`` fija los
valores por defecto; ``--context clave=valor`` los sustituye):

- ``environment``: ``pilot`` (permanente) o ``staging-<n>`` (efímero, ``<n>`` es el número
  de ejecución del flujo de release; infrastructure-design §2.1 y decisión D-8).
- ``instance``: ``shared`` o el identificador de una instancia dedicada
  (deployment-architecture §7, RNF-RES-17, NFR-NUC-09).
- ``first_deploy``: concede los permisos del arranque y admite servicios con 0 tareas
  (infrastructure-design §5.4 y nota U02-H-06 de §2.3).
- ``ca_rotation``: concede temporalmente los mismos permisos del arranque para rotar la
  raíz de ``vigia-node-ca`` (deployment-architecture §6.4, nota de D-8).
- ``nat_per_az``: añade la segunda traducción de direcciones ``vigia-nat-b`` (mitigación de
  R13, infrastructure-design §13; runbook de deployment-architecture §6.2).
- ``nodes_tls_mode``: ``mtls`` (autenticación mutua en ``vigia-alb-nodes``) o ``passthrough``
  (contingencia de R2: balanceador de red con paso directo y terminación en la aplicación,
  infrastructure-design §4.3).

Y uno opcional, fuera de ``cdk.json``: ``provider_organization_id``, la organización proveedora
que imprime ``vigia-admin bootstrap`` (se pasa con ``--context`` a partir del paso que la crea).

Un valor fuera de su forma cerrada detiene la síntesis con un mensaje en español: nunca
se sintetiza un despliegue con un contexto adivinado.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

# RNF-PRI-08, NFR-NUC-31: todo en una sola región.
REGION = "us-east-1"
BOOTSTRAP_QUALIFIER = "vigia"
SHARED_INSTANCE = "shared"
PILOT = "pilot"

CONTEXT_ENVIRONMENT = "environment"
CONTEXT_INSTANCE = "instance"
CONTEXT_FIRST_DEPLOY = "first_deploy"
CONTEXT_CA_ROTATION = "ca_rotation"
CONTEXT_NAT_PER_AZ = "nat_per_az"
CONTEXT_NODES_TLS_MODE = "nodes_tls_mode"
CONTEXT_PROVIDER_ORGANIZATION = "provider_organization_id"
"""Opcional (no está en ``cdk.json``): la organización proveedora que creó ``vigia-admin
bootstrap``. Con él, ``vigia-api``, ``vigia-worker`` y ``vigia-admin`` reciben
``VIGIA_PROVIDER_ORGANIZATION_ID``; sin él, no (raíz de composición, VIG-137)."""
CONTEXT_KEYS = (
    CONTEXT_ENVIRONMENT,
    CONTEXT_INSTANCE,
    CONTEXT_FIRST_DEPLOY,
    CONTEXT_CA_ROTATION,
    CONTEXT_NAT_PER_AZ,
    CONTEXT_NODES_TLS_MODE,
)

# ``staging-<n>`` con ``n`` entero positivo sin ceros a la izquierda (número de ejecución).
_STAGING = re.compile(r"^staging-([1-9][0-9]{0,8})$")
# Identificador de instancia dedicada: minúsculas, cifras y guiones; entra en nombres de
# depósitos (63 caracteres) y de pilas, así que se acota a 20 caracteres.
_INSTANCE = re.compile(r"^[a-z][a-z0-9-]{0,18}[a-z0-9]$")
_RESERVED_INSTANCES = frozenset({PILOT, SHARED_INSTANCE})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Límites de S3 e IAM y los nombres más largos que llevan el sufijo del despliegue
# (``vigia-evidence-...`` en §6.2; ``vigia-task-execution-...`` en §8).
_BUCKET_NAME_MAX = 63
_ROLE_NAME_MAX = 64
_LONGEST_BUCKET_USAGE = "evidence"
_LONGEST_ROLE = "task-execution"
# Balanceadores, grupos de destino y almacenes de confianza admiten 32 caracteres; el nombre
# más largo de ``vigia-edge`` es el del almacén ``vigia-node-trust`` (§4.3).
_LOAD_BALANCING_NAME_MAX = 32
_LONGEST_LOAD_BALANCING_NAME = "node-trust"


class ContextError(ValueError):
    """Valor de contexto de CDK ausente o fuera de su forma cerrada."""


class ObjectLockMode(StrEnum):
    """Modo de bloqueo de objetos de S3."""

    GOVERNANCE = "GOVERNANCE"
    COMPLIANCE = "COMPLIANCE"


class NodesTlsMode(StrEnum):
    """Terminación TLS de ``nodes.<dominio>`` (infrastructure-design §4.3, R2)."""

    #: Balanceador de aplicación con autenticación mutua en modo de verificación.
    MTLS = "mtls"
    #: Contingencia de R2: balanceador de red con paso directo; la aplicación termina TLS.
    PASSTHROUGH = "passthrough"


@dataclass(frozen=True)
class ObjectLock:
    """Retención por defecto del bloqueo de objetos de un depósito."""

    mode: ObjectLockMode
    days: int


@dataclass(frozen=True)
class EnvironmentConfig:
    """Configuración de un despliegue: la tabla ``pilot`` frente a ``staging-<n>`` (D-8).

    Las pilas de TASK-145 a TASK-150 leen estos valores; ninguna decide por su cuenta si
    un recurso se retiene, se bloquea o se destruye.
    """

    environment: str
    instance: str
    first_deploy: bool
    ca_rotation: bool
    ephemeral: bool

    # Depósitos vigia-evidence, vigia-archive, vigia-edge (§6.2 y nota D-8 de §2.1).
    evidence_object_lock: ObjectLock | None
    archive_object_lock: ObjectLock | None
    buckets_retained: bool
    buckets_auto_delete_objects: bool
    # En staging, vigia-data crea su propio vigia-logs-staging-<n> (nota de §2.1); en
    # pilot compartido, el registro de acceso va al vigia-logs heredado de vigia-datasets.
    access_logs_bucket_owned: bool

    # Base de datos gestionada (§6.1 y nota D-8).
    db_instance_class: str
    db_multi_az: bool
    db_deletion_protection: bool
    db_final_snapshot: bool
    db_backup_retention_days: int

    # Autoridad de nodos vigia-node-ca (§7.1 y nota D-8): permanente en pilot; en staging,
    # propia de la ejecución y destruida con el entorno (borrado programado de 7 días).
    node_ca_per_run: bool
    node_ca_pending_window_days: int

    # Zona DNS: alojada en vigia-edge en pilot; importada por atributos en staging desde
    # los parámetros SSM que publica el despliegue ``hosted_zone_source``.
    hosted_zone_owned: bool
    hosted_zone_source: str | None

    waf_enabled: bool
    include_datasets: bool

    # Servicios (§5.2 y nota U02-H-06 de §2.3; A-21).
    api_min_tasks: int
    api_max_tasks: int
    worker_min_tasks: int
    worker_max_tasks: int

    # Red (§3): una traducción de direcciones, excepción R13; ``nat_per_az`` añade la segunda.
    nat_per_az: bool = False

    # Borde (§4.3): autenticación mutua en el balanceador o contingencia de R2.
    nodes_tls_mode: NodesTlsMode = NodesTlsMode.MTLS

    region: str = REGION

    # Raíz de composición (VIG-137): la proveedora de ``vigia-admin bootstrap``, si ya existe.
    provider_organization_id: str | None = None

    def __post_init__(self) -> None:
        """Los nombres más largos del despliegue caben en los límites de sus servicios."""
        limits = (
            (self.bucket_name(_LONGEST_BUCKET_USAGE, "0" * 12), _BUCKET_NAME_MAX, "depósito"),
            (self.resource_name(_LONGEST_ROLE), _ROLE_NAME_MAX, "rol"),
            (
                self.resource_name(_LONGEST_LOAD_BALANCING_NAME),
                _LOAD_BALANCING_NAME_MAX,
                "balanceo de carga",
            ),
        )
        for name, maximum, kind in limits:
            if len(name) > maximum:
                raise ContextError(
                    f"El despliegue '{self.deployment}' produce el nombre de {kind} {name!r} "
                    f"de {len(name)} caracteres; el máximo es {maximum}. Acorta 'instance'."
                )

    @property
    def deployment(self) -> str:
        """Nombre del despliegue en rutas y sufijos: ``pilot``, ``staging-<n>``, ``<cliente>``
        o ``<cliente>-staging-<n>``."""
        if self.instance == SHARED_INSTANCE:
            return self.environment
        if self.environment == PILOT:
            return self.instance
        return f"{self.instance}-{self.environment}"

    @property
    def name_suffix(self) -> str:
        """Sufijo de los nombres físicos: vacío en ``pilot`` compartido (nombres del diseño,
        §6.2 y §8), ``-staging-<n>`` en staging y ``-<cliente>`` en una instancia dedicada."""
        if self.instance == SHARED_INSTANCE and self.environment == PILOT:
            return ""
        return f"-{self.deployment}"

    @property
    def elevated_bootstrap(self) -> bool:
        """Permisos temporales del arranque (§5.4, §7.1 y §8): ``first_deploy`` o
        ``ca_rotation``."""
        return self.first_deploy or self.ca_rotation

    @property
    def api_desired_tasks(self) -> int:
        """Tareas de ``vigia-api``: 0 solo con ``first_deploy`` (deployment-architecture §5)."""
        return 0 if self.first_deploy else self.api_min_tasks

    @property
    def worker_desired_tasks(self) -> int:
        """Tareas de ``vigia-worker``: 0 solo con ``first_deploy``."""
        return 0 if self.first_deploy else self.worker_min_tasks

    def stack_name(self, stack: str) -> str:
        """``vigia-<pila>`` con el sufijo del despliegue (barrido ``vigia-*-staging-*``, §2.1)."""
        return f"vigia-{stack}{self.name_suffix}"

    def bucket_name(self, usage: str, account: str) -> str:
        """``vigia-<uso>[-<despliegue>]-<cuenta>-us-east-1`` (§6.2 y tabla D-8)."""
        return f"vigia-{usage}{self.name_suffix}-{account}-{self.region}"

    def resource_name(self, resource: str) -> str:
        """Nombre físico de un recurso con alcance de cuenta (roles, alias): ``vigia-<recurso>``
        con el sufijo del despliegue; en ``pilot`` compartido, el nombre de §8."""
        return f"vigia-{resource}{self.name_suffix}"


def parse_environment(value: object) -> tuple[str, bool]:
    """Devuelve el entorno validado y si es efímero."""
    if not isinstance(value, str):
        raise ContextError(
            f"El contexto '{CONTEXT_ENVIRONMENT}' debe ser 'pilot' o 'staging-<n>'; "
            f"se recibió {value!r}."
        )
    if value == PILOT:
        return value, False
    if _STAGING.match(value):
        return value, True
    raise ContextError(
        f"El contexto '{CONTEXT_ENVIRONMENT}' debe ser 'pilot' o 'staging-<n>' con <n> entero "
        f"positivo sin ceros a la izquierda; se recibió {value!r}."
    )


def parse_instance(value: object) -> str:
    """Devuelve ``shared`` o el identificador validado de una instancia dedicada."""
    if value == SHARED_INSTANCE:
        return SHARED_INSTANCE
    if (
        not isinstance(value, str)
        or not _INSTANCE.match(value)
        or value in _RESERVED_INSTANCES
        or value.startswith("staging")
        or "--" in value
    ):
        raise ContextError(
            f"El contexto '{CONTEXT_INSTANCE}' debe ser 'shared' o el identificador de una "
            "instancia dedicada (2 a 20 caracteres: minúsculas, cifras y guiones simples, sin "
            f"empezar por 'staging'); se recibió {value!r}."
        )
    return value


def parse_flag(key: str, value: object) -> bool:
    """Lee un indicador booleano: ``true``/``false`` en ``cdk.json`` o en ``--context``."""
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ContextError(f"El contexto '{key}' debe ser 'true' o 'false'; se recibió {value!r}.")


def parse_nodes_tls_mode(value: object) -> NodesTlsMode:
    """Lee ``nodes_tls_mode``: ``mtls`` o ``passthrough``."""
    for mode in NodesTlsMode:
        if value == mode.value:
            return mode
    raise ContextError(
        f"El contexto '{CONTEXT_NODES_TLS_MODE}' debe ser 'mtls' o 'passthrough'; "
        f"se recibió {value!r}."
    )


def parse_provider_organization(value: object) -> str | None:
    """Lee ``provider_organization_id`` (opcional): el UUID canónico que imprime
    ``vigia-admin bootstrap``; ausente o vacío, ``None``."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _UUID.match(value):
        raise ContextError(
            f"El contexto '{CONTEXT_PROVIDER_ORGANIZATION}' debe ser el UUID en minúsculas que "
            "imprime vigia-admin bootstrap (provider_organization_id); el valor no es válido."
        )
    return value


def require(context: Mapping[str, object], key: str) -> object:
    """Valor de contexto obligatorio: ``cdk.json`` fija su valor por defecto."""
    if key not in context or context[key] is None:
        raise ContextError(
            f"Falta el contexto '{key}': cdk.json lo define por defecto; pásalo con "
            f"--context {key}=<valor> si ejecutas la aplicación fuera de la CLI de CDK."
        )
    return context[key]
