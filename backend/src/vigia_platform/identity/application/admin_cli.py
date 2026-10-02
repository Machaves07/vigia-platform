"""Orden administrativa ``vigia-admin`` (LC-NUC-07; BR-NUC-06, 10; NFR-NUC-09, 54; PAT-NUC-ESC-06).

Las operaciones sin interfaz del operador del proveedor. Llama a los **mismos servicios de
aplicación** que la interfaz (``OrganizationGenesis``, ``SigningService``, ``DeadLetterReplay``,
``PartitionMaintenance``…) con un contexto de orden administrativa:

- ``bootstrap``: la organización proveedora (BR-NUC-06), su primer ``platform_operator`` por
  invitación, una clave Ed25519 inicial por propósito (``key_set`` primero: firma el primer
  conjunto) y la raíz de ``vigia-node-ca`` (10 años, firmada con ``kms:Sign``; sin material
  privado fuera de KMS), publicada **solo** en ``vigia-edge/ca/root.pem`` (la lista de revocación
  la publica el worker, D-7). El actor es el operador que nace en la orden
  (``ScopeContexts.bootstrap_operator_context``). Si algo falla después de crear la proveedora,
  ``bootstrap --resume`` completa lo que falta: reinvita al operador si sigue invitado, crea las
  claves que no existan y, con ``--publish-root``, vuelve a publicar la raíz.
- ``create-organization``: organización cliente, primera planta y primer administrador por
  invitación (``platform.organizations.create``), con confirmación explícita.
- ``rotate-key <purpose>``, ``replay-dead-letter <event> <consumer>``,
  ``create-partitions --until AAAA-MM`` y ``record-restore-drill --result ok|failed``: con el
  contexto de ``context_from_operator`` (``--operator``: un ``platform_operator`` activo).
- ``rotate-node-ca --new-key-id``: solo con ``ca_rotation=true`` en la síntesis (que concede los
  permisos); publica la raíz vigente y la nueva en un paquete (D-6).
- ``restore-audit-partition``: descarga, verifica y extrae un archivo de auditoría en un
  directorio nuevo; solo lectura (runbook 6.5).

**Secretos** (NFR-NUC-17, 54): el enlace de una invitación **nunca** sale por la salida ni por los
registros: se escribe en el secreto de un solo uso ``vigia/<entorno>/bootstrap/invitation``, que
el dueño lee con su identidad y borra tras activar la cuenta. La salida es un objeto JSON de una
línea con identificadores (y nunca una cadena con ``://``); los registros van a la salida de
errores con mensajes constantes. ``--dry-run`` valida y muestra lo que haría sin escribir nada:
ni la base, ni secretos, ni objetos (solo lecturas, como la del operador o la clave KMS).

**Composición**: como ``vigia-worker``, la orden pide sus dependencias (``AdminRuntime``) al
constructor que nombra ``VIGIA_ADMIN_RUNTIME`` (``vigia_platform.<módulo>:<función>``,
asíncrono, recibe la configuración y el identificador de la organización proveedora). La
configuración se lee una vez del entorno con un modelo estricto (``AdminConfig``).

Códigos de salida: 0 hecho; 2 uso incorrecto; 3 sin confirmación; 4 rechazo de la operación;
5 dependencia no disponible (reintentable); 1 configuración o error inesperado.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import enum
import importlib
import json
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final, NoReturn, Protocol, TextIO

from pydantic import BaseModel, ConfigDict, Field

from vigia_platform.identity.application.common import (
    IdentityRejected,
    new_uuid4,
)
from vigia_platform.identity.application.hierarchy import (
    FirstOperator,
    GenesisRequest,
    GenesisResult,
    PlantSpec,
    ProviderAlreadyExists,
    ProviderGenesisRequest,
    ProviderGenesisResult,
)
from vigia_platform.identity.application.invitations import InvitationOutcome
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.context import ContextUnavailable
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditReceipt
from vigia_platform.shared.archive.audit_archive import (
    ArchiveVerificationFailed,
    extract_archive,
    restored_from_bytes,
)
from vigia_platform.shared.archive.partitions import (
    MAX_MONTHS_PER_CALL,
    PartitionReport,
    add_months,
    month_of,
)
from vigia_platform.shared.archive.restore_drill import DrillResult
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction, TransientDatabaseError
from vigia_platform.shared.node_ca import ROOT_CERTIFICATE_KEY, NodeCaError, PublishedRoot
from vigia_platform.shared.observability.logging import configure_logging, get_logger
from vigia_platform.shared.outbox.registries import REGISTRY_NAME
from vigia_platform.shared.outbox.replay import ReplayReceipt
from vigia_platform.shared.secrets import SecretsUnavailable
from vigia_platform.shared.signing.keys import (
    KeySetPublicationRecord,
    KeyStateConflict,
    SigningKeyRecord,
    SigningPurpose,
    active_key,
    format_timestamp,
)
from vigia_platform.shared.signing.service import (
    RotationResult,
    SigningKeyUnavailable,
    SigningNotReady,
    SigningStartupError,
    SigningStateError,
)
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "BOOTSTRAP_KEY_ORDER",
    "RUNTIME_VARIABLE",
    "AdminConfig",
    "AdminRuntime",
    "ExitCode",
    "build_parser",
    "main",
    "run",
]

_log = get_logger("identity.admin")

RUNTIME_VARIABLE: Final = "VIGIA_ADMIN_RUNTIME"
_RUNTIME_REFERENCE: Final = re.compile(r"vigia_platform(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*")
"""Solo un constructor del propio paquete: la variable no puede nombrar cualquier función."""
_ENVIRONMENT: Final = r"^(local|test|pilot|staging-[1-9][0-9]{0,2})$"
_SECRET_NAME: Final = r"^[A-Za-z0-9][A-Za-z0-9/_+=.@-]{0,511}$"  # noqa: S105 - patrón del nombre
_KMS_KEY_ID: Final = r"^[A-Za-z0-9][A-Za-z0-9:/_-]{0,2047}$"
_BUCKET: Final = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
_OBJECT_KEY: Final = r"^[A-Za-z0-9][A-Za-z0-9!_.*'()/=-]{0,511}$"
_LINK_BASE: Final = r"^https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$"
_MONTH: Final = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")

BOOTSTRAP_KEY_ORDER: Final = (
    SigningPurpose.KEY_SET,
    SigningPurpose.CATALOG,
    SigningPurpose.GATE,
    SigningPurpose.LIVE_VIEW_TOKEN,
    SigningPurpose.CHECKPOINT,
)
"""``key_set`` primero: su clave firma el primer conjunto que publica cada propósito del nodo."""


class ExitCode(enum.IntEnum):
    OK = 0
    FAILURE = 1
    USAGE = 2
    NOT_CONFIRMED = 3
    REJECTED = 4
    UNAVAILABLE = 5


# --- Configuración ------------------------------------------------------------------------------


class AdminConfig(BaseModel):
    """Configuración de ``vigia-admin``: modelo estricto, inmutable, leído una vez."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    environment: str = Field(pattern=_ENVIRONMENT)
    provider_organization_id: uuid.UUID | None = None
    """``VIGIA_PROVIDER_ORGANIZATION_ID``: la que imprimió ``bootstrap``; la exigen las demás."""
    invitation_secret: str = Field(pattern=_SECRET_NAME)
    """``VIGIA_BOOTSTRAP_INVITATION_SECRET``; por omisión,
    ``vigia/<entorno>/bootstrap/invitation``."""
    link_base: str | None = Field(default=None, pattern=_LINK_BASE)
    """``VIGIA_PUBLIC_ORIGIN`` (``https://app.<dominio>``): base del enlace de invitación."""
    node_ca_key_id: str | None = Field(default=None, pattern=_KMS_KEY_ID)
    """``VIGIA_NODE_CA_KEY_ARN``: solo con ``first_deploy=true`` o ``ca_rotation=true``."""
    edge_bucket: str | None = Field(default=None, pattern=_BUCKET)
    root_certificate_key: str = Field(default=ROOT_CERTIFICATE_KEY, pattern=_OBJECT_KEY)
    archive_bucket: str | None = Field(default=None, pattern=_BUCKET)
    """``VIGIA_ARCHIVE_BUCKET``: el de ``restore-audit-partition`` (runbook 6.5)."""

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> AdminConfig:
        """``ValueError`` (de Pydantic) si falta una variable obligatoria o no es válida."""
        environment = environ.get("VIGIA_ENVIRONMENT", "")
        values: dict[str, Any] = {
            "environment": environment,
            "invitation_secret": environ.get(
                "VIGIA_BOOTSTRAP_INVITATION_SECRET", f"vigia/{environment}/bootstrap/invitation"
            ),
        }
        provider = environ.get("VIGIA_PROVIDER_ORGANIZATION_ID")
        if provider:
            values["provider_organization_id"] = uuid.UUID(provider)
        for name, variable in (
            ("link_base", "VIGIA_PUBLIC_ORIGIN"),
            ("node_ca_key_id", "VIGIA_NODE_CA_KEY_ARN"),
            ("edge_bucket", "VIGIA_EDGE_BUCKET"),
            ("root_certificate_key", "VIGIA_ROOT_CERTIFICATE_KEY"),
            ("archive_bucket", "VIGIA_ARCHIVE_BUCKET"),
        ):
            value = environ.get(variable)
            if value:
                values[name] = value
        return cls(**values)


# --- Dependencias --------------------------------------------------------------------------------


class AdminDatabase(Protocol):
    """``shared.db.Database``."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...

    async def dispose(self) -> None: ...


class AdminContexts(Protocol):
    """``identity.authz.ScopeContexts``."""

    def provider_audit_context(self) -> ScopeContext: ...

    def bootstrap_operator_context(
        self, operator_id: uuid.UUID, display_name: str
    ) -> ScopeContext: ...

    async def context_from_operator(
        self, operator_id: uuid.UUID, *, correlation_id: uuid.UUID | None = None
    ) -> ScopeContext: ...


class AdminAuthorizer(Protocol):
    async def authorize(
        self, context: ScopeContext, key: PermissionKey, resource: Resource
    ) -> ScopeContext: ...


class AdminGenesis(Protocol):
    """``OrganizationGenesis``."""

    def check(self, request: GenesisRequest) -> GenesisRequest: ...

    def check_provider(self, request: ProviderGenesisRequest) -> ProviderGenesisRequest: ...

    async def create_client_organization(
        self, operator_context: ScopeContext, request: GenesisRequest
    ) -> GenesisResult: ...

    async def create_provider_organization(
        self, bootstrap_context: ScopeContext, request: ProviderGenesisRequest
    ) -> ProviderGenesisResult: ...

    async def first_operator(self, provider_context: ScopeContext) -> FirstOperator | None: ...

    async def reissue_operator_invitation(
        self, bootstrap_context: ScopeContext, operator: FirstOperator
    ) -> InvitationOutcome | None: ...


class AdminSigning(Protocol):
    """``SigningService``."""

    @property
    def ready(self) -> bool: ...

    async def start(self, *, required: Iterable[SigningPurpose] = ...) -> None: ...

    def all_keys(self) -> tuple[SigningKeyRecord, ...]: ...

    def current_publication(self) -> KeySetPublicationRecord | None: ...

    async def rotate(self, purpose: SigningPurpose, *, context: ScopeContext) -> RotationResult: ...


class AdminReplay(Protocol):
    """``DeadLetterReplay``."""

    async def replay(
        self, context: ScopeContext, event_id: uuid.UUID, consumer_name: str
    ) -> ReplayReceipt: ...


class AdminPartitions(Protocol):
    """``PartitionMaintenance``."""

    async def create(
        self, transaction: Transaction, *, until: date | None = None
    ) -> PartitionReport: ...


class AdminDrills(Protocol):
    """``RestoreDrills``."""

    async def record(self, operator_context: ScopeContext, result: DrillResult) -> AuditReceipt: ...


class OneTimeSecrets(Protocol):
    """``SecretsManagerAdapter.put_one_time``."""

    async def put_one_time(self, name: str, value: str) -> str: ...


class RootPublisher(Protocol):
    """``NodeCaPublisher``."""

    async def check_key(self, key_id: str) -> None: ...

    async def publish_root(self, key_id: str, *, now: Any) -> PublishedRoot: ...

    async def rotate_root(self, new_key_id: str, *, now: Any) -> PublishedRoot: ...


class ArchiveReader(Protocol):
    """``StoragePort.get_object`` sobre ``vigia-archive``."""

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminRuntime:
    """Dependencias ya construidas de ``vigia-admin`` (las crea la raíz de composición)."""

    clock: Clock
    database: AdminDatabase
    contexts: AdminContexts
    authorizer: AdminAuthorizer
    genesis: AdminGenesis
    signing: AdminSigning
    replay: AdminReplay
    partitions: AdminPartitions
    drills: AdminDrills
    secrets: OneTimeSecrets
    node_ca: RootPublisher | None = None
    """Con ``VIGIA_NODE_CA_KEY_ARN`` y ``VIGIA_EDGE_BUCKET`` (arranque o sustitución de raíz)."""
    archive: ArchiveReader | None = None
    """Con ``VIGIA_ARCHIVE_BUCKET`` (``restore-audit-partition``)."""
    registries: tuple[Callable[[], Awaitable[None]], ...] = ()
    """Sincronizadores de los registros (tipos de registro y de evento): solo antes de escribir,
    nunca con ``--dry-run``."""
    random_bytes: Callable[[int], bytes] = field(default=os.urandom, repr=False)


type RuntimeBuilder = Callable[[AdminConfig, uuid.UUID], Awaitable[AdminRuntime]]


def resolve_runtime_builder(reference: str | None) -> RuntimeBuilder:
    """El constructor de ``VIGIA_ADMIN_RUNTIME`` (``vigia_platform.<módulo>:<función>``)."""
    if reference is None or not _RUNTIME_REFERENCE.fullmatch(reference):
        raise ValueError(f"{RUNTIME_VARIABLE} debe ser «vigia_platform.<módulo>:<función>»")
    module_name, function_name = reference.split(":")
    builder = getattr(importlib.import_module(module_name), function_name, None)
    if not callable(builder):
        raise ValueError(f"{RUNTIME_VARIABLE} no nombra una función")
    resolved: RuntimeBuilder = builder
    return resolved


# --- Errores y salida ----------------------------------------------------------------------------


class AdminError(Exception):
    """Rechazo propio de la orden: ``code`` cerrado y mensaje en español sin datos."""

    def __init__(self, code: str, message: str, exit_code: ExitCode = ExitCode.REJECTED) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(slots=True)
class Streams:
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO


def _reject_links(value: object) -> None:
    """La salida solo lleva identificadores: ninguna cadena con ``://`` (enlaces) ni larga."""
    if isinstance(value, str):
        if "://" in value or "#" in value or len(value) > 256:
            raise AdminError(
                "output_rejected", "la salida contendría algo que no es un identificador"
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_links(key)
            _reject_links(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_links(item)
    elif value is not None and not isinstance(value, bool | int):
        raise AdminError("output_rejected", "la salida contendría algo que no es un identificador")


def _emit(streams: Streams, payload: Mapping[str, object]) -> None:
    _reject_links(payload)
    streams.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
    streams.stdout.flush()


def _fail(streams: Streams, code: str, message: str, field_path: str | None = None) -> None:
    error: dict[str, str] = {"error": code, "mensaje": message}
    if field_path is not None:
        error["campo"] = field_path
    streams.stderr.write(json.dumps(error, sort_keys=True, ensure_ascii=False) + "\n")
    streams.stderr.flush()


def _confirm(args: argparse.Namespace, streams: Streams, expected: str, what: str) -> None:
    """Confirmación explícita: ``--yes`` o escribir ``expected`` (NFR-NUC-54)."""
    if args.yes:
        return
    streams.stderr.write(f"Vas a {what}. Escribe «{expected}» para confirmar: ")
    streams.stderr.flush()
    answer = streams.stdin.readline().strip()
    if answer != expected:
        raise AdminError(
            "not_confirmed",
            "operación no confirmada: no se escribió nada (usa --yes en una tarea sin consola)",
            ExitCode.NOT_CONFIRMED,
        )


# --- Analizador de argumentos ------------------------------------------------------------------

_ARGPARSE_ES: Final = (
    (re.compile(r"the following arguments are required: (.*)"),
     r"faltan argumentos obligatorios: \1"),
    (re.compile(r"argument (.*?): invalid choice: (.*?) \(choose from (.*)\)"),
     r"argumento \1: opción no válida: \2 (elige entre \3)"),
    (re.compile(r"argument (.*?): invalid (\S+) value: (.*)"),
     r"argumento \1: valor no válido: \3"),
    (re.compile(r"unrecognized arguments: (.*)"), r"argumentos no reconocidos: \1"),
    (re.compile(r"argument (.*?): expected one argument"), r"argumento \1: falta su valor"),
    (re.compile(r"argument (.*?): not allowed with argument (.*)"),
     r"argumento \1: no se puede usar con \2"),
)  # fmt: skip


class _SpanishFormatter(argparse.RawDescriptionHelpFormatter):
    def add_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[argparse._MutuallyExclusiveGroup],
        prefix: str | None = None,
    ) -> None:
        super().add_usage(usage, actions, groups, "uso: " if prefix is None else prefix)


class _Parser(argparse.ArgumentParser):
    """``argparse`` con la ayuda y los errores en español (NFR-NUC-54)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _SpanishFormatter)
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self._positionals.title = "argumentos"
        self._optionals.title = "opciones"
        self.add_argument(
            "-h", "--help", action="help", default=argparse.SUPPRESS,
            help="muestra esta ayuda y termina",
        )  # fmt: skip

    def error(self, message: str) -> NoReturn:
        for pattern, replacement in _ARGPARSE_ES:
            message = pattern.sub(replacement, message)
        self.print_usage(sys.stderr)
        self.exit(ExitCode.USAGE, f"{self.prog}: error: {message}\n")


def _uuid_argument(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("no es un UUID") from None


def _month_argument(value: str) -> date:
    match = _MONTH.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("debe ser un mes AAAA-MM")
    return date(int(match.group(1)), int(match.group(2)), 1)


def _add_common(parser: argparse.ArgumentParser, *, confirm: bool = False) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="valida y muestra lo que haría, sin escribir nada",
    )
    if confirm:
        parser.add_argument(
            "--yes",
            action="store_true",
            help="confirma sin preguntar (tarea puntual sin consola)",
        )


def _add_operator(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--operator",
        required=True,
        type=_uuid_argument,
        metavar="UUID",
        help="identificador del platform_operator activo que ejecuta la orden",
    )


def build_parser() -> argparse.ArgumentParser:
    """El analizador de ``vigia-admin`` con la ayuda en español."""
    parser = _Parser(
        prog="vigia-admin",
        description=(
            "Órdenes administrativas de la plataforma Vigía (LC-NUC-07).\n"
            "La salida es una línea JSON con identificadores; nunca enlaces ni contraseñas.\n"
            "El enlace de una invitación se deja en el secreto de un solo uso\n"
            "vigia/<entorno>/bootstrap/invitation."
        ),
        epilog=(
            "Configuración por el entorno: VIGIA_ENVIRONMENT, VIGIA_ADMIN_RUNTIME,\n"
            "VIGIA_PROVIDER_ORGANIZATION_ID, VIGIA_PUBLIC_ORIGIN, VIGIA_NODE_CA_KEY_ARN,\n"
            "VIGIA_EDGE_BUCKET, VIGIA_ARCHIVE_BUCKET y VIGIA_BOOTSTRAP_INVITATION_SECRET.\n"
            "Salida: 0 hecho, 2 uso, 3 sin confirmar, 4 rechazo, 5 dependencia no disponible."
        ),
    )
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="ORDEN", title="órdenes", parser_class=_Parser
    )

    bootstrap = commands.add_parser(
        "bootstrap",
        help="arranque: organización proveedora, primer operador, claves y raíz de nodos",
        description=(
            "Crea la organización proveedora y su primer platform_operator por invitación,\n"
            "una clave Ed25519 por propósito y la raíz de vigia-node-ca (10 años, firmada\n"
            "con kms:Sign), publicada solo en ca/root.pem. El enlace va al secreto de un\n"
            "solo uso, nunca a la salida."
        ),
    )
    bootstrap.add_argument("--organization-code", metavar="CÓDIGO", help="código de la proveedora")
    bootstrap.add_argument("--organization-name", metavar="NOMBRE", help="nombre de la proveedora")
    bootstrap.add_argument("--operator-email", metavar="CORREO", help="correo del primer operador")
    bootstrap.add_argument("--operator-name", metavar="NOMBRE", help="nombre del primer operador")
    bootstrap.add_argument(
        "--resume",
        action="store_true",
        help="completa un arranque interrumpido (VIGIA_PROVIDER_ORGANIZATION_ID)",
    )
    bootstrap.add_argument(
        "--publish-root",
        action="store_true",
        help="con --resume: vuelve a emitir y publicar ca/root.pem",
    )
    _add_common(bootstrap, confirm=True)

    create = commands.add_parser(
        "create-organization",
        help="alta de una organización cliente, su primera planta y su administrador",
        description=(
            "Crea una organización cliente con su primera planta y su primer administrador\n"
            "por invitación. Pide confirmación (escribir el código) salvo con --yes."
        ),
    )
    _add_operator(create)
    create.add_argument("--code", required=True, metavar="CÓDIGO", help="código de la organización")
    create.add_argument("--name", required=True, metavar="NOMBRE", help="nombre de la organización")
    create.add_argument("--plant-code", required=True, metavar="CÓDIGO", help="código de la planta")
    create.add_argument("--plant-name", required=True, metavar="NOMBRE", help="nombre de la planta")
    create.add_argument("--country", required=True, metavar="PAÍS", help="ISO 3166-1 alfa-2")
    create.add_argument(
        "--data-region", required=True, metavar="REGIÓN", help="región de datos de la planta"
    )
    create.add_argument("--timezone", required=True, metavar="ZONA", help="zona horaria IANA")
    create.add_argument(
        "--admin-email", required=True, metavar="CORREO", help="correo del administrador"
    )
    create.add_argument(
        "--admin-name", required=True, metavar="NOMBRE", help="nombre del administrador"
    )
    create.add_argument("--admin-license", metavar="MATRÍCULA", help="matrícula profesional")
    create.add_argument(
        "--concession-max-days", type=int, default=30, metavar="DÍAS",
        help="tope de días de una concesión (1 a 90; 30 por omisión)",
    )  # fmt: skip
    create.add_argument(
        "--concession-default-days", type=int, default=7, metavar="DÍAS",
        help="días por omisión de una concesión (7 por omisión)",
    )  # fmt: skip
    _add_common(create, confirm=True)

    rotate = commands.add_parser(
        "rotate-key",
        help="rota la clave de firma de un propósito",
        description="Crea la versión nueva de la clave del propósito y publica el conjunto.",
    )
    rotate.add_argument(
        "purpose",
        choices=[purpose.value for purpose in SigningPurpose],
        metavar="PROPÓSITO",
        help="catalog, gate, live_view_token, key_set o checkpoint",
    )
    _add_operator(rotate)
    _add_common(rotate)

    root = commands.add_parser(
        "rotate-node-ca",
        help="sustituye la raíz de vigia-node-ca (solo con ca_rotation=true)",
        description=(
            "Firma una raíz nueva con la clave KMS nueva y publica ca/root.pem como paquete\n"
            "de dos raíces: la vigente y la nueva (D-6)."
        ),
    )
    root.add_argument(
        "--new-key-id", required=True, metavar="ARN", help="clave KMS ECC_NIST_P256 nueva"
    )
    _add_common(root, confirm=True)

    replay = commands.add_parser(
        "replay-dead-letter",
        help="reentrega un evento de la cola muerta a un consumidor",
        description="Devuelve la entrega a pendiente con el mismo event_id (BR-NUC-82).",
    )
    replay.add_argument("event_id", type=_uuid_argument, metavar="EVENTO", help="event_id")
    replay.add_argument("consumer", metavar="CONSUMIDOR", help="nombre del consumidor")
    _add_operator(replay)
    _add_common(replay)

    partitions = commands.add_parser(
        "create-partitions",
        help="crea por adelantado las particiones mensuales",
        description="Crea las particiones del mes en curso hasta el mes indicado (incluido).",
    )
    partitions.add_argument(
        "--until", required=True, type=_month_argument, metavar="AAAA-MM", help="último mes"
    )
    _add_operator(partitions)
    _add_common(partitions)

    restore = commands.add_parser(
        "restore-audit-partition",
        help="descarga, verifica y extrae un archivo de auditoría (solo lectura)",
        description=(
            "Descarga el archivo de vigia-archive, comprueba su SHA-256 y sus cadenas y lo\n"
            "extrae en un directorio nuevo. No escribe en la base (runbook 6.5)."
        ),
    )
    restore.add_argument("object_key", metavar="OBJETO", help="clave del archivo en vigia-archive")
    restore.add_argument(
        "--sha256", required=True, metavar="HEX", help="archive_sha256 del registro archivado"
    )
    restore.add_argument(
        "--output", required=True, type=Path, metavar="DIRECTORIO", help="directorio nuevo"
    )
    _add_common(restore)

    drill = commands.add_parser(
        "record-restore-drill",
        help="registra el resultado del ensayo de restauración",
        description="Escribe restore_drill_recorded al terminar el runbook 6.1.",
    )
    drill.add_argument(
        "--result",
        required=True,
        choices=[result.value for result in DrillResult],
        metavar="RESULTADO",
        help="ok o failed",
    )
    _add_operator(drill)
    _add_common(drill)
    return parser


# --- Órdenes ------------------------------------------------------------------------------------


def _require(value: str | None, code: str, message: str) -> str:
    if value is None:
        raise AdminError(code, message, ExitCode.FAILURE)
    return value


def _require_provider(config: AdminConfig) -> uuid.UUID:
    if config.provider_organization_id is None:
        raise AdminError(
            "provider_unknown",
            "falta VIGIA_PROVIDER_ORGANIZATION_ID (la imprime bootstrap)",
            ExitCode.FAILURE,
        )
    return config.provider_organization_id


def _require_node_ca(runtime: AdminRuntime) -> RootPublisher:
    if runtime.node_ca is None:
        raise AdminError(
            "node_ca_unavailable",
            "sin VIGIA_NODE_CA_KEY_ARN y VIGIA_EDGE_BUCKET (first_deploy o ca_rotation)",
            ExitCode.FAILURE,
        )
    return runtime.node_ca


async def _synchronize(runtime: AdminRuntime) -> None:
    for synchronize in runtime.registries:
        await synchronize()


async def _write_invitation(
    config: AdminConfig,
    runtime: AdminRuntime,
    organization_id: uuid.UUID,
    invitation: InvitationOutcome,
) -> str | None:
    """El enlace divulgado va al secreto de un solo uso; devuelve el nombre del secreto."""
    link = invitation.link
    if link is None:  # entregado por correo: no hay enlace que guardar
        return None
    value = json.dumps(
        {
            "organization_id": str(organization_id),
            "user_id": str(invitation.user_id),
            "invitation_id": str(invitation.invitation_id),
            "expires_at": format_timestamp(invitation.expires_at),
            "link": link,
        },
        sort_keys=True,
    )
    await runtime.secrets.put_one_time(config.invitation_secret, value)
    _log.info("enlace de invitación guardado en el secreto de un solo uso")
    return config.invitation_secret


def _invitation_output(invitation: InvitationOutcome, secret: str | None) -> dict[str, object]:
    return {
        "invitation_id": str(invitation.invitation_id),
        "invitation_expires_at": format_timestamp(invitation.expires_at),
        "invitation_delivery": invitation.delivery.value,
        "invitation_secret": secret,
    }


async def _initial_keys(runtime: AdminRuntime, context: ScopeContext) -> dict[str, str]:
    """Una clave activa por propósito: crea solo las que falten (``key_set`` primero)."""
    signing = runtime.signing
    if not signing.ready:
        await signing.start(required=())
    created: dict[str, str] = {}
    for purpose in BOOTSTRAP_KEY_ORDER:
        current = active_key(signing.all_keys(), purpose)
        if current is not None:
            created[purpose.value] = current.key_id
            continue
        result = await signing.rotate(purpose, context=context)
        created[purpose.value] = result.new_key.key_id
    return created


def _root_output(published: PublishedRoot) -> dict[str, object]:
    return {
        "root_object_key": published.object_key,
        "root_version_id": published.version_id,
        "root_sha256": published.fingerprints[-1],
        "root_bundle_sha256": list(published.fingerprints),
        "root_not_after": format_timestamp(published.root.not_valid_after_utc),
    }


async def _bootstrap(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    if args.resume:
        await _bootstrap_resume(args, config, runtime, streams)
        return
    if args.publish_root:
        raise AdminError("invalid_arguments", "--publish-root solo vale con --resume")
    missing = [
        name
        for name, value in (
            ("--organization-code", args.organization_code),
            ("--organization-name", args.organization_name),
            ("--operator-email", args.operator_email),
            ("--operator-name", args.operator_name),
        )
        if not value
    ]
    if missing:
        raise AdminError(
            "invalid_arguments", "faltan argumentos de bootstrap: " + ", ".join(missing)
        )
    _require(config.link_base, "link_base_missing", "falta VIGIA_PUBLIC_ORIGIN")
    key_id = _require(config.node_ca_key_id, "node_ca_unavailable", "falta VIGIA_NODE_CA_KEY_ARN")
    node_ca = _require_node_ca(runtime)
    request = runtime.genesis.check_provider(
        ProviderGenesisRequest(
            code=args.organization_code,
            name=args.organization_name,
            operator_email=args.operator_email,
            operator_display_name=args.operator_name,
        )
    )
    await node_ca.check_key(key_id)  # solo lectura: una clave que no es P-256 no crea nada
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "bootstrap",
                "dry_run": True,
                "provider_organization_code": request.code,
                "would_create": [
                    "provider_organization",
                    "platform_operator_invitation",
                    *(f"signing_key:{purpose.value}" for purpose in BOOTSTRAP_KEY_ORDER),
                    "node_ca_root",
                ],
                "invitation_secret": config.invitation_secret,
                "root_object_key": config.root_certificate_key,
            },
        )
        return
    _confirm(args, streams, request.code, "crear la organización proveedora " + request.code)
    await _synchronize(runtime)
    operator_id = new_uuid4(runtime.random_bytes)
    context = runtime.contexts.bootstrap_operator_context(
        operator_id, request.operator_display_name
    )
    genesis = await runtime.genesis.create_provider_organization(context, request)
    _log.info("organización proveedora creada")
    secret = await _write_invitation(config, runtime, genesis.organization_id, genesis.invitation)
    keys = await _initial_keys(runtime, context)
    published = await node_ca.publish_root(key_id, now=runtime.clock.now())
    publication = runtime.signing.current_publication()
    _emit(
        streams,
        {
            "command": "bootstrap",
            "dry_run": False,
            "provider_organization_id": str(genesis.organization_id),
            "operator_user_id": str(genesis.operator_user_id),
            **_invitation_output(genesis.invitation, secret),
            "signing_keys": keys,
            "key_set_publication_id": None
            if publication is None
            else str(publication.publication_id),
            **_root_output(published),
        },
    )


async def _bootstrap_resume(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    provider_id = _require_provider(config)
    operator = await runtime.genesis.first_operator(runtime.contexts.provider_audit_context())
    if operator is None:
        raise AdminError("provider_incomplete", "la proveedora no tiene platform_operator")
    key_id: str | None = None
    if args.publish_root:
        key_id = _require(
            config.node_ca_key_id, "node_ca_unavailable", "falta VIGIA_NODE_CA_KEY_ARN"
        )
        await _require_node_ca(runtime).check_key(key_id)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "bootstrap",
                "dry_run": True,
                "resume": True,
                "provider_organization_id": str(provider_id),
                "operator_user_id": str(operator.user_id),
                "operator_status": operator.status,
                "would_publish_root": key_id is not None,
            },
        )
        return
    _confirm(args, streams, "resume", "completar el arranque de la plataforma")
    await _synchronize(runtime)
    context = runtime.contexts.bootstrap_operator_context(operator.user_id, operator.display_name)
    output: dict[str, object] = {
        "command": "bootstrap",
        "dry_run": False,
        "resume": True,
        "provider_organization_id": str(provider_id),
        "operator_user_id": str(operator.user_id),
    }
    if operator.status == "invited":
        _require(config.link_base, "link_base_missing", "falta VIGIA_PUBLIC_ORIGIN")
        invitation = await runtime.genesis.reissue_operator_invitation(context, operator)
        if invitation is not None:
            secret = await _write_invitation(config, runtime, provider_id, invitation)
            output |= _invitation_output(invitation, secret)
    output["signing_keys"] = await _initial_keys(runtime, context)
    if key_id is not None:
        output |= _root_output(
            await _require_node_ca(runtime).publish_root(key_id, now=runtime.clock.now())
        )
    _emit(streams, output)


async def _operator_context(args: argparse.Namespace, runtime: AdminRuntime) -> ScopeContext:
    """La orden administrativa de ``--operator`` (solo lee: un operador activo de la proveedora)."""
    return await runtime.contexts.context_from_operator(args.operator)


async def _create_organization(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    _require(config.link_base, "link_base_missing", "falta VIGIA_PUBLIC_ORIGIN")
    request = runtime.genesis.check(
        GenesisRequest(
            code=args.code,
            name=args.name,
            plant=PlantSpec(
                code=args.plant_code,
                name=args.plant_name,
                country=args.country,
                data_region=args.data_region,
                timezone=args.timezone,
            ),
            administrator_email=args.admin_email,
            administrator_display_name=args.admin_name,
            administrator_professional_license=args.admin_license,
            concession_max_days=args.concession_max_days,
            concession_default_days=args.concession_default_days,
            disclose_link=True,
        )
    )
    operator = await _operator_context(args, runtime)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "create-organization",
                "dry_run": True,
                "operator_user_id": str(operator.actor.id),
                "organization_code": request.code,
                "plant_code": request.plant.code,
                "data_region": request.plant.data_region,
                "would_create": [
                    "client_organization",
                    "plant",
                    "administrator_invitation",
                ],
                "invitation_secret": config.invitation_secret,
            },
        )
        return
    _confirm(args, streams, request.code, "crear la organización cliente " + request.code)
    await _synchronize(runtime)
    result = await runtime.genesis.create_client_organization(operator, request)
    _log.info("organización cliente creada")
    secret = await _write_invitation(config, runtime, result.organization_id, result.invitation)
    _emit(
        streams,
        {
            "command": "create-organization",
            "dry_run": False,
            "organization_id": str(result.organization_id),
            "plant_id": str(result.plant_id),
            "administrator_user_id": str(result.administrator_user_id),
            **_invitation_output(result.invitation, secret),
        },
    )


async def _rotate_key(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    provider_id = _require_provider(config)
    purpose = SigningPurpose(args.purpose)
    operator = await _operator_context(args, runtime)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "rotate-key",
                "dry_run": True,
                "operator_user_id": str(operator.actor.id),
                "purpose": purpose.value,
                "would_create": ["signing_key", "key_rotated"],
            },
        )
        return
    await _synchronize(runtime)
    authorized = await runtime.authorizer.authorize(
        operator, PermissionKey.PLATFORM_KEYS_ROTATE, Resource.organization(provider_id)
    )
    if not runtime.signing.ready:
        await runtime.signing.start()
    result = await runtime.signing.rotate(purpose, context=authorized)
    _emit(
        streams,
        {
            "command": "rotate-key",
            "dry_run": False,
            "purpose": purpose.value,
            "key_id": result.new_key.key_id,
            "previous_key_id": result.previous_key_id,
            "valid_until": format_timestamp(result.new_key.valid_until),
            "key_set_publication_id": None
            if result.publication is None
            else str(result.publication.publication_id),
        },
    )


async def _rotate_node_ca(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    node_ca = _require_node_ca(runtime)
    new_key_id = args.new_key_id
    if not isinstance(new_key_id, str) or re.fullmatch(_KMS_KEY_ID, new_key_id) is None:
        raise AdminError("invalid_value", "--new-key-id no es un identificador de clave KMS")
    if new_key_id == config.node_ca_key_id:
        raise AdminError("invalid_value", "la clave nueva debe ser distinta de la vigente")
    await node_ca.check_key(new_key_id)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "rotate-node-ca",
                "dry_run": True,
                "root_object_key": config.root_certificate_key,
                "would_publish": ["current_root", "new_root"],
            },
        )
        return
    _confirm(args, streams, "rotate-node-ca", "publicar una raíz nueva de vigia-node-ca")
    published = await node_ca.rotate_root(new_key_id, now=runtime.clock.now())
    _emit(
        streams,
        {"command": "rotate-node-ca", "dry_run": False, **_root_output(published)},
    )


async def _replay_dead_letter(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    _require_provider(config)
    consumer = args.consumer
    if not REGISTRY_NAME.fullmatch(consumer):
        raise AdminError("invalid_value", "el consumidor no es un nombre registrado válido")
    operator = await _operator_context(args, runtime)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "replay-dead-letter",
                "dry_run": True,
                "operator_user_id": str(operator.actor.id),
                "event_id": str(args.event_id),
                "consumer": consumer,
            },
        )
        return
    receipt = await runtime.replay.replay(operator, args.event_id, consumer)
    _emit(
        streams,
        {
            "command": "replay-dead-letter",
            "dry_run": False,
            "event_id": str(receipt.event_id),
            "consumer": receipt.consumer_name,
            "organization_id": str(receipt.organization_id),
            "status": "pending",
        },
    )


async def _create_partitions(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    _require_provider(config)
    until: date = args.until
    first = month_of(runtime.clock.now())
    if until < first:
        raise AdminError("invalid_value", "--until no puede ser anterior al mes en curso")
    if add_months(first, MAX_MONTHS_PER_CALL - 1) < until:
        raise AdminError("invalid_value", f"a lo sumo {MAX_MONTHS_PER_CALL} meses por llamada")
    operator = await _operator_context(args, runtime)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "create-partitions",
                "dry_run": True,
                "operator_user_id": str(operator.actor.id),
                "first_month": first.isoformat()[:7],
                "last_month": until.isoformat()[:7],
            },
        )
        return
    async with runtime.database.transaction(operator) as transaction:
        report = await runtime.partitions.create(transaction, until=until)
    _emit(
        streams,
        {
            "command": "create-partitions",
            "dry_run": False,
            "first_month": report.first_month.isoformat()[:7],
            "last_month": report.last_month.isoformat()[:7],
            "created": [result.partition for result in report.created],
            "blocked": [result.partition for result in report.blocked],
        },
    )
    if report.blocked:
        raise AdminError(
            "partitions_blocked",
            "hay meses sin crear: la partición por defecto ya tiene filas de esos meses",
        )


async def _restore_audit_partition(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    object_key: str = args.object_key
    expected: str = args.sha256
    output: Path = args.output
    if re.fullmatch(_OBJECT_KEY, object_key) is None:
        raise AdminError("invalid_value", "la clave del objeto no es válida")
    if _SHA256.fullmatch(expected) is None:
        raise AdminError("invalid_value", "--sha256 debe ser un SHA-256 hexadecimal en minúsculas")
    if output.exists():
        raise AdminError("invalid_value", "el directorio de salida ya existe")
    if not output.parent.is_dir():
        raise AdminError("invalid_value", "el directorio padre de la salida no existe")
    if runtime.archive is None:
        raise AdminError("archive_unavailable", "falta VIGIA_ARCHIVE_BUCKET", ExitCode.FAILURE)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "restore-audit-partition",
                "dry_run": True,
                "object_key": object_key,
                "sha256": expected,
            },
        )
        return
    data = await runtime.archive.get_object(object_key)
    restored = restored_from_bytes(data, expected)
    files = extract_archive(data, output)
    partition = restored.contents.partition
    _emit(
        streams,
        {
            "command": "restore-audit-partition",
            "dry_run": False,
            "object_key": object_key,
            "sha256": expected,
            "partition": None if partition is None else partition.qualified_name,
            "entry_count": len(restored.rows),
            "organizations": [
                str(segment.organization_id) for segment in restored.contents.segments
            ],
            "files": len(files),
        },
    )


async def _record_restore_drill(
    args: argparse.Namespace, config: AdminConfig, runtime: AdminRuntime, streams: Streams
) -> None:
    _require_provider(config)
    result = DrillResult(args.result)
    operator = await _operator_context(args, runtime)
    if args.dry_run:
        _emit(
            streams,
            {
                "command": "record-restore-drill",
                "dry_run": True,
                "operator_user_id": str(operator.actor.id),
                "result": result.value,
            },
        )
        return
    receipt = await runtime.drills.record(operator, result)
    _emit(
        streams,
        {
            "command": "record-restore-drill",
            "dry_run": False,
            "result": result.value,
            "audit_entry_id": str(receipt.entry_id),
            "chain_sequence": receipt.chain_sequence,
        },
    )


type Command = Callable[[argparse.Namespace, AdminConfig, AdminRuntime, Streams], Awaitable[None]]

COMMANDS: Final[Mapping[str, Command]] = {
    "bootstrap": _bootstrap,
    "create-organization": _create_organization,
    "rotate-key": _rotate_key,
    "rotate-node-ca": _rotate_node_ca,
    "replay-dead-letter": _replay_dead_letter,
    "create-partitions": _create_partitions,
    "restore-audit-partition": _restore_audit_partition,
    "record-restore-drill": _record_restore_drill,
}


# --- Ejecución ----------------------------------------------------------------------------------


def _provider_for(
    args: argparse.Namespace, config: AdminConfig, random_bytes: Callable[[int], bytes]
) -> uuid.UUID:
    """La proveedora: nueva en ``bootstrap`` (sin ``--resume``); si no, la configurada."""
    if args.command == "bootstrap" and not args.resume:
        return new_uuid4(random_bytes)
    return _require_provider(config)


async def execute(
    args: argparse.Namespace,
    config: AdminConfig,
    builder: RuntimeBuilder,
    streams: Streams,
    *,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> int:
    """Construye las dependencias, ejecuta la orden y traduce los fallos a códigos de salida."""
    runtime: AdminRuntime | None = None
    try:
        provider_id = _provider_for(args, config, random_bytes)
        runtime = await builder(config, provider_id)
        await COMMANDS[args.command](args, config, runtime, streams)
        return ExitCode.OK
    except AdminError as error:
        _fail(streams, error.code, str(error))
        return error.exit_code
    except IdentityRejected as error:
        _fail(streams, error.code.value, error.message_es, error.field)
        return ExitCode.REJECTED
    except ProviderAlreadyExists:
        _fail(streams, "provider_exists", "la organización proveedora ya existe: usa --resume")
        return ExitCode.REJECTED
    except ContextUnavailable:
        _fail(streams, "operator_invalid", "--operator no es un platform_operator activo")
        return ExitCode.REJECTED
    except ResourceNotFound:
        _fail(streams, "not_found", "no existe o no está al alcance")
        return ExitCode.REJECTED
    except (KeyStateConflict, SigningKeyUnavailable):
        _fail(streams, "conflict", "las claves cambiaron o falta la key_set vigente; reintenta")
        return ExitCode.REJECTED
    except NodeCaError as error:
        _fail(streams, "node_ca_invalid", str(error))
        return ExitCode.REJECTED
    except ArchiveVerificationFailed as error:
        _fail(streams, "archive_invalid", "el archivo no verifica: " + error.reason.value)
        return ExitCode.REJECTED
    except (TransientDatabaseError, SecretsUnavailable, StorageUnavailable):
        _fail(streams, "temporarily_unavailable", "dependencia no disponible: reintenta")
        return ExitCode.UNAVAILABLE
    except (SigningStartupError, SigningStateError, SigningNotReady):
        _fail(streams, "signing_unavailable", "las claves de firma no se pudieron cargar")
        return ExitCode.UNAVAILABLE
    except Exception:
        _log.exception("vigia-admin terminó con un error inesperado")
        _fail(streams, "internal_error", "error inesperado (detalle en los registros)")
        return ExitCode.FAILURE
    finally:
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.database.dispose()


def run(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    builder: RuntimeBuilder | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> int:
    """``vigia-admin`` con ``argv`` y ``environ`` explícitos (las pruebas inyectan ``builder``)."""
    streams = Streams(
        stdin if stdin is not None else sys.stdin,
        stdout if stdout is not None else sys.stdout,
        stderr if stderr is not None else sys.stderr,
    )
    try:
        args = build_parser().parse_args(list(argv))
    except SystemExit as exit_:
        return int(exit_.code) if isinstance(exit_.code, int) else ExitCode.USAGE
    try:
        config = AdminConfig.from_environ(environ)
        chosen = (
            builder
            if builder is not None
            else resolve_runtime_builder(environ.get(RUNTIME_VARIABLE))
        )
    except Exception:
        _log.error("configuración de vigia-admin no válida: revisa las variables VIGIA_*")
        _fail(streams, "config_invalid", "configuración no válida (variables VIGIA_*)")
        return ExitCode.FAILURE
    return asyncio.run(execute(args, config, chosen, streams, random_bytes=random_bytes))


def main(argv: Sequence[str] | None = None) -> int:
    """``vigia-admin``: registros a la salida de errores, resultado a la salida estándar."""
    configure_logging(stream=sys.stderr)
    return run(sys.argv[1:] if argv is None else argv, environ=os.environ)


if __name__ == "__main__":  # pragma: no cover - punto de entrada
    sys.exit(main())
