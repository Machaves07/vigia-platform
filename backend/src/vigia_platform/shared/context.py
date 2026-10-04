"""``ScopeContext``: el contexto que activa la seguridad a nivel de fila (BR-NUC-01 a 03).

Valor inmutable que toda operación de datos recibe como parámetro obligatorio; no se persiste
(``domain-entities.md`` §4.1). Al abrir cada transacción, ``shared.db`` fija con ``SET LOCAL``
las tres variables de sesión que leen las políticas: ``vigia.organization_id``,
``vigia.actor_kind`` (el ``actor.kind``) y ``vigia.concession_id`` (vacía salvo bajo concesión).

**Sin constructores públicos** (BR-NUC-03): ``ScopeContext(...)`` sin el sello del módulo lanza
``TypeError``. Los cinco constructores (sesión válida, evento de la bandeja, iteración periódica,
orden administrativa y, desde A-51, petición de un nodo) viven en ``identity.authz.context``
(TASK-125, TASK-206) y son los únicos que usan ``_seal_scope_context``;
``tests/unit/test_scope_context.py`` falla si otro módulo de ``src/`` lo nombra.

``ContextAbsent`` es la excepción de todo repositorio o adaptador invocado sin contexto: se lanza
antes de tocar la red (BR-NUC-02).

**Registro de repositorios** (BR-NUC-02, PR-NUC-02): toda clase con operaciones de datos se
declara con el decorador ``@repository``. El decorador envuelve cada método público que recibe un
``ScopeContext`` (o, a falta de él, una ``Transaction``, que lleva el suyo) con una guarda que se
ejecuta **antes** que el cuerpo: sin contexto, avisa con ``report_context_absent`` y lanza
``ContextAbsent``, así que ninguna consulta llega a ejecutarse. El aviso lo recibe el auditor que
instala ``identity.authz.context`` (``install_context_absent_reporter``): escribe
``context_absent_attempt`` en la cadena de la organización proveedora y publica ``security_alert``.
Un método que resuelve la ausencia por sí mismo (el escritor del expediente devuelve el rechazo
``context_absent``) se marca con ``@handles_absent_context`` y avisa él mismo.
``tests/properties/test_context_absent_metaproperty.py`` recorre el registro y exige que toda
clase de ``src/`` con operaciones de datos esté en él.

Este módulo no importa FastAPI ni SQLAlchemy: ``identity.authz`` lo usa sin romper su
aislamiento (NFR-NUC-25).
"""

from __future__ import annotations

import enum
import functools
import inspect
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "Actor",
    "ActorKind",
    "ActorUnit",
    "AllowedScope",
    "ContextAbsent",
    "ContextAbsentReporter",
    "ContextOrigin",
    "GuardedOperation",
    "Role",
    "ScopeContext",
    "ScopeLevel",
    "handles_absent_context",
    "install_context_absent_reporter",
    "registered_repositories",
    "report_context_absent",
    "repository",
]

_log = get_logger("shared.context")

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
    """``context_origin``: de cuál de los cinco constructores salió el contexto (A-51)."""

    SESSION = "session"
    OUTBOX_EVENT = "outbox_event"
    PERIODIC_ITERATION = "periodic_iteration"
    ADMIN_COMMAND = "admin_command"
    NODE_REQUEST = "node_request"
    """Petición de un nodo por una ruta del contrato (``context_from_node``, solo ``node_api``)."""


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
    """Operación de datos sin ``ScopeContext`` (BR-NUC-02): no se ejecuta ninguna consulta.

    ``operation`` es el nombre de la operación del repositorio (``Clase.método``), o ``None``.
    """

    code: Final = "context_absent"

    def __init__(self, operation: str | None = None) -> None:
        super().__init__("operación de datos sin ScopeContext")
        self.operation = operation


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

    def covers(
        self,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID | None = None,
        zone_id: uuid.UUID | None = None,
    ) -> bool:
        """¿Contiene este alcance al recurso de ``organization_id``, planta y zona? (BR-NUC-12).

        Organización ⊇ planta ⊇ zona: un alcance de organización cubre todo lo de su
        organización (plantas y zonas presentes y futuras); uno de planta, esa planta y todas sus
        zonas; uno de zona, esa zona. Un recurso de organización (sin planta ni zona) solo lo
        cubre un alcance de organización. Quien llama pasa la planta **real** de la zona.
        """
        if self.scope_level is ScopeLevel.ORGANIZATION:
            return self.scope_id == organization_id
        if self.scope_level is ScopeLevel.PLANT:
            return plant_id is not None and self.scope_id == plant_id
        return zone_id is not None and self.scope_id == zone_id


_SEAL: Final = object()
"""Sello privado: solo ``_seal_scope_context`` (y con él los cinco constructores) lo pasa."""


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

    def covers(self, plant_id: uuid.UUID | None, zone_id: uuid.UUID | None = None) -> bool:
        """¿Alguna asignación de ``allowed_scopes`` contiene esa planta y zona?"""
        return any(
            scope.covers(self.organization_id, plant_id, zone_id) for scope in self.allowed_scopes
        )


def _seal_scope_context(
    *,
    organization_id: uuid.UUID,
    actor: Actor,
    origin: ContextOrigin,
    allowed_scopes: Iterable[AllowedScope],
    correlation_id: uuid.UUID,
    session_id_hash: str | None = None,
) -> ScopeContext:
    """Crea un ``ScopeContext`` validado. **Privado**: solo lo llaman los cinco constructores.

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


# --- Registro de repositorios y aviso de ContextAbsent (BR-NUC-02, PR-NUC-02) --------------------

type ContextAbsentReporter = Callable[[str], None]
"""Recibe ``Clase.método`` de cada operación invocada sin contexto; nunca debe bloquear."""


class _ReporterSlot:
    reporter: ContextAbsentReporter | None = None


def install_context_absent_reporter(
    reporter: ContextAbsentReporter | None,
) -> ContextAbsentReporter | None:
    """Instala el auditor de los intentos sin contexto y devuelve el anterior."""
    previous = _ReporterSlot.reporter
    _ReporterSlot.reporter = reporter
    return previous


def report_context_absent(operation: str) -> None:
    """Avisa de un intento sin contexto (``operation`` = ``Clase.método``). Nunca lanza."""
    _log.warning("operación de datos sin contexto rechazada")
    reporter = _ReporterSlot.reporter
    if reporter is None:
        return
    try:
        reporter(operation)
    except Exception:
        _log.exception("no se pudo registrar el intento sin contexto")


_HANDLES_ABSENCE: Final = "__vigia_handles_absent_context__"
_MISSING: Final = object()


def handles_absent_context[F: Callable[..., Any]](function: F) -> F:
    """Marca una operación que resuelve la ausencia de contexto por sí misma (y avisa)."""
    setattr(function, _HANDLES_ABSENCE, True)
    return function


class _GuardKind(enum.Enum):
    CONTEXT = "context"
    TRANSACTION = "transaction"


@dataclass(frozen=True, slots=True)
class _Guarded:
    name: str
    index: int | None
    default: object
    kind: _GuardKind


@dataclass(frozen=True, slots=True)
class GuardedOperation:
    """Una operación de un repositorio registrado y el parámetro que lleva el contexto."""

    repository: type[Any]
    name: str
    parameters: tuple[str, ...]
    handles_absence: bool

    @property
    def operation(self) -> str:
        return f"{self.repository.__qualname__}.{self.name}"


_REPOSITORIES: Final[dict[type[Any], tuple[GuardedOperation, ...]]] = {}


def _guarded_parameters(function: Callable[..., Any]) -> tuple[_Guarded, ...]:
    """Los parámetros ``ScopeContext``; sin ninguno, las ``Transaction`` obligatorias."""
    parameters = list(inspect.signature(function).parameters.values())
    found: list[_Guarded] = []
    transactions: list[_Guarded] = []
    for index, parameter in enumerate(parameters):
        annotation = str(parameter.annotation)
        position = (
            None if parameter.kind in (parameter.KEYWORD_ONLY, parameter.VAR_KEYWORD) else index
        )
        default = _MISSING if parameter.default is parameter.empty else parameter.default
        if "ScopeContext" in annotation:
            found.append(_Guarded(parameter.name, position, default, _GuardKind.CONTEXT))
        elif re.fullmatch(r"(\w+\.)?Transaction", annotation.strip("'\"")):
            transactions.append(_Guarded(parameter.name, position, default, _GuardKind.TRANSACTION))
    return tuple(found or transactions)


def _has_context(value: object, kind: _GuardKind) -> bool:
    if kind is _GuardKind.CONTEXT:
        return isinstance(value, ScopeContext)
    try:
        return isinstance(getattr(value, "context", None), ScopeContext)
    except Exception:
        return False


def _check(
    guarded: tuple[_Guarded, ...],
    operation: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    for parameter in guarded:
        if parameter.name in kwargs:
            value = kwargs[parameter.name]
        elif parameter.index is not None and parameter.index < len(args):
            value = args[parameter.index]
        else:
            value = parameter.default
        if not _has_context(value, parameter.kind):
            report_context_absent(operation)
            raise ContextAbsent(operation)


def _guard(
    function: Callable[..., Any], operation: str, guarded: tuple[_Guarded, ...]
) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(function):

        @functools.wraps(function)
        async def guarded_async(*args: Any, **kwargs: Any) -> Any:
            _check(guarded, operation, args, kwargs)
            return await function(*args, **kwargs)

        return guarded_async

    @functools.wraps(function)
    def guarded_sync(*args: Any, **kwargs: Any) -> Any:
        _check(guarded, operation, args, kwargs)
        return function(*args, **kwargs)

    return guarded_sync


def repository[C: type[Any]](cls: C) -> C:
    """Registra ``cls`` como repositorio y pone la guarda de contexto en sus operaciones.

    Operación = método público cuya firma recibe un ``ScopeContext`` (o una ``Transaction``).
    La guarda corre antes que el cuerpo: sin contexto, ``report_context_absent`` y
    ``ContextAbsent``. Una clase sin operaciones no es un repositorio (``TypeError``).
    """
    operations: list[GuardedOperation] = []
    for name, attribute in list(vars(cls).items()):
        if name.startswith("_"):
            continue
        wrapper: type[staticmethod[Any, Any]] | type[classmethod[Any, Any, Any]] | None = None
        function: object = attribute
        if isinstance(attribute, staticmethod | classmethod):
            wrapper = type(attribute)
            function = attribute.__func__
        if not inspect.isfunction(function):
            continue
        guarded = _guarded_parameters(function)
        if not guarded:
            continue
        handles = bool(getattr(function, _HANDLES_ABSENCE, False))
        operation = GuardedOperation(cls, name, tuple(p.name for p in guarded), handles)
        operations.append(operation)
        if handles:
            continue
        replacement: object = _guard(function, operation.operation, guarded)
        if wrapper is not None:
            replacement = wrapper(replacement)  # type: ignore[arg-type]
        setattr(cls, name, replacement)
    if not operations:
        raise TypeError(f"{cls.__qualname__} no tiene operaciones de datos con contexto")
    _REPOSITORIES[cls] = tuple(operations)
    return cls


def registered_repositories() -> dict[type[Any], tuple[GuardedOperation, ...]]:
    """Copia del registro: cada repositorio con sus operaciones guardadas."""
    return dict(_REPOSITORIES)
