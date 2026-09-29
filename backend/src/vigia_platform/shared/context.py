"""``ScopeContext``: el contexto que activa la seguridad a nivel de fila (BR-NUC-01 a 03).

Valor inmutable que toda operación de datos recibe como parámetro obligatorio; no se persiste
(``domain-entities.md`` §4.1). Al abrir cada transacción, ``shared.db`` fija con ``SET LOCAL``
las tres variables de sesión que leen las políticas: ``vigia.organization_id``,
``vigia.actor_kind`` (el ``actor.kind``) y ``vigia.concession_id`` (vacía salvo bajo concesión).

**Sin constructores públicos** (BR-NUC-03): ``ScopeContext(...)`` sin el sello del módulo lanza
``TypeError``. Los cuatro constructores (sesión válida, evento de la bandeja, iteración periódica
y orden administrativa) viven en ``identity.authz.context`` (TASK-125) y son los únicos que usan
``_seal_scope_context``; ``tests/unit/test_scope_context.py`` falla si otro módulo de ``src/`` lo
nombra.

``ContextAbsent`` es la excepción de todo repositorio o adaptador invocado sin contexto: se lanza
antes de tocar la red (BR-NUC-02). La auditoría del intento la añade TASK-125.

Este módulo no importa FastAPI ni SQLAlchemy: ``identity.authz`` lo usa sin romper su
aislamiento (NFR-NUC-25).
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "Actor",
    "ActorKind",
    "ActorUnit",
    "AllowedScope",
    "ContextAbsent",
    "ContextOrigin",
    "Role",
    "ScopeContext",
    "ScopeLevel",
]

DISPLAY_NAME_MAX_CHARS: Final = 120
"""Longitud máxima de ``Actor.display_name_snapshot`` (``domain-entities.md`` §3.2)."""

_SESSION_ID_HASH: Final = re.compile(r"[0-9a-f]{64}")
"""SHA-256 en hexadecimal en minúsculas: nunca el identificador de sesión en claro."""


class ActorKind(enum.StrEnum):
    """``actor_kind``: quién actúa. Su valor es el de ``vigia.actor_kind``."""

    USER = "user"
    PROVIDER_USER = "provider_user"
    NODE = "node"
    SYSTEM = "system"
    OPERATOR = "operator"


class ContextOrigin(enum.StrEnum):
    """``context_origin``: de cuál de los cuatro constructores salió el contexto."""

    SESSION = "session"
    OUTBOX_EVENT = "outbox_event"
    PERIODIC_ITERATION = "periodic_iteration"
    ADMIN_COMMAND = "admin_command"


class ScopeLevel(enum.StrEnum):
    ORGANIZATION = "organization"
    PLANT = "plant"
    ZONE = "zone"


class Role(enum.StrEnum):
    COORDINATOR_SST = "coordinator_sst"
    LINE_MANAGER = "line_manager"
    PLANT_MANAGER = "plant_manager"
    ADMINISTRATOR = "administrator"
    PROVIDER_INSTALLER = "provider_installer"
    COPASST = "copasst"
    PLATFORM_OPERATOR = "platform_operator"


class ActorUnit(enum.StrEnum):
    """Unidad que escribe en nombre del actor (``Actor.unit``)."""

    U02 = "U-02"
    U03 = "U-03"
    U04 = "U-04"


class ContextAbsent(Exception):
    """Operación de datos sin ``ScopeContext`` (BR-NUC-02): no se ejecuta ninguna consulta."""

    code: Final = "context_absent"

    def __init__(self) -> None:
        super().__init__("operación de datos sin ScopeContext")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _require_uuid(value: object, name: str) -> None:
    if type(value) is not uuid.UUID:
        raise TypeError(f"{name} debe ser uuid.UUID")


def _require_enum[E: enum.Enum](value: object, kind: type[E], name: str) -> None:
    if not isinstance(value, kind):
        raise TypeError(f"{name} debe ser {kind.__name__}")


@dataclass(frozen=True, slots=True)
class Actor:
    """Quién actúa (``domain-entities.md`` §3.2); la instantánea se toma al construir."""

    kind: ActorKind
    id: uuid.UUID
    """``node_id`` del certificado para ``node``; UUID fijo por proceso para ``system``."""
    display_name_snapshot: str
    unit: ActorUnit
    role_in_use: Role | None = None
    concession_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, ActorKind, "kind")
        _require_uuid(self.id, "id")
        if type(self.display_name_snapshot) is not str:
            raise TypeError("display_name_snapshot debe ser str")
        _require(
            0 < len(self.display_name_snapshot) <= DISPLAY_NAME_MAX_CHARS,
            f"display_name_snapshot debe tener de 1 a {DISPLAY_NAME_MAX_CHARS} caracteres",
        )
        _require_enum(self.unit, ActorUnit, "unit")
        if self.role_in_use is not None:
            _require_enum(self.role_in_use, Role, "role_in_use")
        if self.concession_id is not None:
            _require_uuid(self.concession_id, "concession_id")
        _require(
            (self.concession_id is not None) == (self.kind is ActorKind.PROVIDER_USER),
            "concession_id va si y solo si el actor es del proveedor bajo concesión",
        )


@dataclass(frozen=True, slots=True)
class AllowedScope:
    """Una asignación vigente ``{scope_level, scope_id, role}`` o el alcance de la concesión."""

    scope_level: ScopeLevel
    scope_id: uuid.UUID
    role: Role

    def __post_init__(self) -> None:
        _require_enum(self.scope_level, ScopeLevel, "scope_level")
        _require_uuid(self.scope_id, "scope_id")
        _require_enum(self.role, Role, "role")


_SEAL: Final = object()
"""Sello privado: solo ``_seal_scope_context`` (y con él los cuatro constructores) lo pasa."""


@dataclass(frozen=True, slots=True)
class ScopeContext:
    """Contexto de alcance inmutable (``domain-entities.md`` §4.1). No tiene constructor público."""

    organization_id: uuid.UUID
    """La organización cuyas filas son visibles (bajo concesión, la del cliente)."""
    actor: Actor
    origin: ContextOrigin
    allowed_scopes: tuple[AllowedScope, ...]
    concession_id: uuid.UUID | None
    """Solo si ``origin = session`` y el actor es del proveedor."""
    correlation_id: uuid.UUID
    """UUID v7."""
    session_id_hash: str | None
    """SHA-256 en hexadecimal del identificador de sesión; solo si ``origin = session``."""
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError(
                "ScopeContext no tiene constructor público: usa los constructores de "
                "identity.authz.context (BR-NUC-03)"
            )
        _require_uuid(self.organization_id, "organization_id")
        if not isinstance(self.actor, Actor):
            raise TypeError("actor debe ser Actor")
        _require_enum(self.origin, ContextOrigin, "origin")
        if type(self.allowed_scopes) is not tuple or not all(
            isinstance(scope, AllowedScope) for scope in self.allowed_scopes
        ):
            raise TypeError("allowed_scopes debe ser una tupla de AllowedScope")
        _require_uuid(self.correlation_id, "correlation_id")
        _require(self.correlation_id.version == 7, "correlation_id debe ser un UUID v7")
        if self.concession_id is not None:
            _require_uuid(self.concession_id, "concession_id")
        _require(
            self.concession_id == self.actor.concession_id,
            "concession_id debe coincidir con la del actor",
        )
        _require(
            self.concession_id is None or self.origin is ContextOrigin.SESSION,
            "concession_id solo va con origin = session",
        )
        if self.session_id_hash is not None:
            if type(self.session_id_hash) is not str:
                raise TypeError("session_id_hash debe ser str")
            _require(
                _SESSION_ID_HASH.fullmatch(self.session_id_hash) is not None,
                "session_id_hash debe ser un SHA-256 en hexadecimal en minúsculas",
            )
        _require(
            (self.session_id_hash is not None) == (self.origin is ContextOrigin.SESSION),
            "session_id_hash va si y solo si origin = session",
        )
        # El sello se consume: ``dataclasses.replace`` sobre un contexto ya creado no lo copia,
        # así que no sirve para fabricar otro con distinta organización o actor.
        object.__setattr__(self, "_seal", None)


def _seal_scope_context(
    *,
    organization_id: uuid.UUID,
    actor: Actor,
    origin: ContextOrigin,
    allowed_scopes: Iterable[AllowedScope],
    correlation_id: uuid.UUID,
    session_id_hash: str | None = None,
) -> ScopeContext:
    """Crea un ``ScopeContext`` validado. **Privado**: solo lo llaman los cuatro constructores.

    ``concession_id`` sale del actor, así que nunca puede diferir de él.
    """
    if not isinstance(actor, Actor):
        raise TypeError("actor debe ser Actor")
    return ScopeContext(
        organization_id=organization_id,
        actor=actor,
        origin=origin,
        allowed_scopes=tuple(allowed_scopes),
        concession_id=actor.concession_id,
        correlation_id=correlation_id,
        session_id_hash=session_id_hash,
        _seal=_SEAL,
    )
