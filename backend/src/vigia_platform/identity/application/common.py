"""Piezas comunes de los servicios de ``identity.hierarchy`` (LC-NUC-05; TASK-126).

- ``IdentityDependencies``: lo que todos los servicios reciben (base, escritor del expediente,
  auditoría, bandeja, autorización, política de texto libre, reloj y generador aleatorio).
- ``IdentityRejected``: el rechazo de una operación de identidad con un ``code`` de lista cerrada
  (``IdentityRejection``), el ``api_code`` con que responde la interfaz y, si aplica, el puntero
  del campo. El mensaje es genérico y nunca repite el valor recibido ni revela datos de otra
  organización (BR-NUC-05, BR-NUC-09).
- ``ScopeRef``: un alcance (organización, planta o zona) con la planta **real** de la zona, como
  lo cargó la base; ``contains`` y ``overlaps`` son la jerarquía organización ⊇ planta ⊇ zona.
- Sentencias compartidas: el bloqueo de la organización con que se serializan los cambios de
  usuarios y roles de una organización (incompatibilidades atómicas en las dos direcciones y la
  regla del último administrador) y la carga de alcances bajo la seguridad a nivel de fila.

Ningún módulo de ``identity.application`` lee la hora del sistema: usan el ``Clock`` inyectado.
"""

from __future__ import annotations

import enum
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from pydantic import BaseModel
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.writer import (
    LedgerDatabase,
    LedgerRejection,
    Receipt,
    RecordScope,
    violated_constraint,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, ScopeLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import OutboxPort

__all__ = [
    "UNIQUE_VIOLATION",
    "IdentityDependencies",
    "IdentityRejected",
    "IdentityRejection",
    "LedgerWriter",
    "ScopeRef",
    "as_uuid",
    "checked_free_text",
    "lock_organization",
    "new_uuid4",
    "resolve_scope",
    "scope_resource",
    "unique_violation",
    "write_record",
]

UNIQUE_VIOLATION: Final = "23505"


class IdentityRejection(enum.StrEnum):
    """Por qué se rechaza una operación de identidad (lista cerrada)."""

    INVALID_VALUE = "invalid_value"
    """Un campo no cumple su forma (código, país, zona horaria, correo, longitud)."""
    FREE_TEXT_REJECTED = "free_text_rejected"
    """Un nombre no pasa la política de texto libre."""
    CODE_TAKEN = "code_taken"
    """El ``code`` ya existe en su ámbito (organización o planta)."""
    EMAIL_UNAVAILABLE = "email_unavailable"
    """El correo ya tiene cuenta en la plataforma; nunca dice en qué organización (BR-NUC-05)."""
    ASSIGNMENT_REQUIRED = "assignment_required"
    """Una invitación o reactivación sin ninguna asignación (BR-NUC-30)."""
    ROLE_NOT_ASSIGNABLE = "role_not_assignable"
    """El rol no se asigna en esta clase de organización (BR-NUC-11)."""
    ROLE_INCOMPATIBLE = "role_incompatible"
    """Choca con una asignación vigente (BR-NUC-14) o la repite."""
    SELF_ASSIGNMENT = "self_assignment"
    """Nadie se asigna un rol que no tiene ya sobre ese alcance (BR-NUC-19)."""
    LAST_ADMINISTRATOR = "last_administrator"
    """La organización se quedaría sin administrador activo (BR-NUC-31)."""
    USER_STATE = "user_state"
    """El estado de la cuenta no admite la operación (p. ej. desactivar una ya desactivada)."""
    ALREADY_REMOVED = "already_removed"
    """La asignación ya estaba retirada."""
    NODE_PLANT_MISMATCH = "node_plant_mismatch"
    """El nodo y la zona no son de la misma planta (BR-NUC-08)."""
    ZONE_HAS_NODE = "zone_has_node"
    """La zona ya tiene un nodo vigente (BR-NUC-08)."""
    ZONE_WITHOUT_NODE = "zone_without_node"
    """La zona no tiene nodo vigente que retirar."""
    INVITATION_INVALID = "invitation_invalid"
    """Token inexistente, usado, vencido o cancelado: el mismo rechazo para todos."""
    PASSWORD_REJECTED = "password_rejected"  # noqa: S105 - código, no un secreto
    """La contraseña no cumple la política (BR-NUC-20)."""
    SECOND_FACTOR_REQUIRED = "second_factor_required"
    """El rol exige segundo factor y no se inscribió o el código no es válido (BR-NUC-21, 32)."""
    PRIVACY_NOTICE_OUTDATED = "privacy_notice_outdated"
    """Se aceptó una versión del aviso que no es la vigente (NFR-NUC-29)."""


_API_CODES: Final[Mapping[IdentityRejection, str]] = {
    IdentityRejection.INVALID_VALUE: "invalid_request",
    IdentityRejection.FREE_TEXT_REJECTED: "invalid_request",
    IdentityRejection.CODE_TAKEN: "conflict",
    IdentityRejection.EMAIL_UNAVAILABLE: "conflict",
    IdentityRejection.ASSIGNMENT_REQUIRED: "invalid_request",
    IdentityRejection.ROLE_NOT_ASSIGNABLE: "invalid_request",
    IdentityRejection.ROLE_INCOMPATIBLE: "conflict",
    IdentityRejection.SELF_ASSIGNMENT: "conflict",
    IdentityRejection.LAST_ADMINISTRATOR: "conflict",
    IdentityRejection.USER_STATE: "conflict",
    IdentityRejection.ALREADY_REMOVED: "conflict",
    IdentityRejection.NODE_PLANT_MISMATCH: "invalid_request",
    IdentityRejection.ZONE_HAS_NODE: "conflict",
    IdentityRejection.ZONE_WITHOUT_NODE: "zone_without_node",
    IdentityRejection.INVITATION_INVALID: "not_found",
    IdentityRejection.PASSWORD_REJECTED: "invalid_request",
    IdentityRejection.SECOND_FACTOR_REQUIRED: "second_factor_required",
    IdentityRejection.PRIVACY_NOTICE_OUTDATED: "invalid_request",
}

_MESSAGES: Final[Mapping[IdentityRejection, str]] = {
    IdentityRejection.INVALID_VALUE: "Un campo no tiene la forma esperada.",
    IdentityRejection.FREE_TEXT_REJECTED: "Un nombre no cumple la política de texto libre.",
    IdentityRejection.CODE_TAKEN: "Ya existe un elemento con ese código.",
    IdentityRejection.EMAIL_UNAVAILABLE: "Ese correo no se puede invitar.",
    IdentityRejection.ASSIGNMENT_REQUIRED: "La cuenta necesita al menos una asignación de rol.",
    IdentityRejection.ROLE_NOT_ASSIGNABLE: "Ese rol no se asigna en esta organización.",
    IdentityRejection.ROLE_INCOMPATIBLE: (
        "La asignación es incompatible con otra asignación vigente del usuario."
    ),
    IdentityRejection.SELF_ASSIGNMENT: "Nadie puede asignarse un rol que no tiene.",
    IdentityRejection.LAST_ADMINISTRATOR: (
        "La organización debe conservar al menos un administrador activo."
    ),
    IdentityRejection.USER_STATE: "El estado de la cuenta no admite esta operación.",
    IdentityRejection.ALREADY_REMOVED: "La asignación ya estaba retirada.",
    IdentityRejection.NODE_PLANT_MISMATCH: "El nodo y la zona no son de la misma planta.",
    IdentityRejection.ZONE_HAS_NODE: "La zona ya tiene un nodo asignado.",
    IdentityRejection.ZONE_WITHOUT_NODE: "La zona no tiene un nodo asignado.",
    IdentityRejection.INVITATION_INVALID: "La invitación no es válida.",
    IdentityRejection.PASSWORD_REJECTED: "La contraseña no cumple la política.",
    IdentityRejection.SECOND_FACTOR_REQUIRED: "Hay que inscribir el segundo factor.",
    IdentityRejection.PRIVACY_NOTICE_OUTDATED: "Hay que aceptar la versión vigente del aviso.",
}


class IdentityRejected(Exception):
    """Rechazo de una operación de identidad: ``code`` cerrado, sin datos del valor recibido.

    ``conflict_assignment_id`` nombra la asignación en conflicto (BR-NUC-14); ``field`` es el
    puntero del campo que falla; ``details`` son códigos cerrados (p. ej. los motivos de la
    política de contraseñas).
    """

    def __init__(
        self,
        code: IdentityRejection,
        *,
        field: str | None = None,
        conflict_assignment_id: uuid.UUID | None = None,
        details: tuple[str, ...] = (),
    ) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.field = field
        self.conflict_assignment_id = conflict_assignment_id
        self.details = details

    @property
    def api_code(self) -> str:
        """El ``api_error_code`` con que responde la interfaz."""
        return _API_CODES[self.code]

    @property
    def message_es(self) -> str:
        return _MESSAGES[self.code]


class LedgerWriter(Protocol):
    """La parte de ``EscritorExpediente`` que usan estos servicios."""

    async def write(
        self,
        context: ScopeContext | None,
        record_type: str,
        content: Mapping[str, Any] | BaseModel,
        *,
        scope: RecordScope | None = ...,
        transaction: Transaction | None = ...,
    ) -> Receipt | LedgerRejection: ...


@dataclass(frozen=True)
class IdentityDependencies:
    """Lo que reciben los servicios de ``identity.application``."""

    database: LedgerDatabase
    writer: LedgerWriter
    audit: AuditWriter
    outbox: OutboxPort
    authorizer: Authorizer
    free_text: FreeTextPolicyRegistry
    clock: Clock
    provider_organization_id: uuid.UUID
    random_bytes: Callable[[int], bytes] = field(default=os.urandom, repr=False)

    def __post_init__(self) -> None:
        if type(self.provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")


class LedgerWriteFailed(Exception):
    """El expediente rechazó un registro de identidad: la operación se revierte entera."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"registro rechazado: {rejection.code.value}")
        self.rejection = rejection


async def write_record(
    deps: IdentityDependencies,
    context: ScopeContext,
    transaction: Transaction,
    record_type: str,
    content: Mapping[str, Any],
    *,
    scope: RecordScope | None = None,
) -> Receipt:
    """Escribe ``record_type`` en la transacción de la operación; un rechazo la revierte.

    Un nombre que no pasa la política de texto libre sale como ``IdentityRejected``; cualquier
    otro rechazo es un defecto (el contenido lo compone el servicio) y sale como
    ``LedgerWriteFailed``.
    """
    result = await deps.writer.write(
        context, record_type, dict(content), scope=scope, transaction=transaction
    )
    if isinstance(result, LedgerRejection):
        if result.code.value == "free_text_rejected":
            raise IdentityRejected(IdentityRejection.FREE_TEXT_REJECTED, field=result.field)
        raise LedgerWriteFailed(result)
    return result


def new_uuid4(random_bytes: Callable[[int], bytes]) -> uuid.UUID:
    """UUID v4 con el generador inyectado (organización, planta, zona, nodo, usuario)."""
    return uuid.UUID(bytes=random_bytes(16), version=4)


def as_uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def checked_free_text(
    deps: IdentityDependencies, value: object, *, entity: str, path: str, max_length: int
) -> str:
    """``value`` en NFC si pasa la política de texto libre (nombres de §2, ≤ ``max_length``)."""
    if not isinstance(value, str):
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field=path)
    try:
        return deps.free_text.apply(value, FreeTextField(entity, path, 1, max_length))
    except FreeTextRejected:
        raise IdentityRejected(IdentityRejection.FREE_TEXT_REJECTED, field=path) from None


def unique_violation(error: sa_exc.IntegrityError) -> str | None:
    """Nombre de la restricción de unicidad que violó ``error``, o ``None``."""
    return violated_constraint(error, UNIQUE_VIOLATION)


@dataclass(frozen=True, slots=True)
class ScopeRef:
    """Un alcance con su lugar real: ``plant_id`` es la planta (o la de la zona); nulo en la
    organización."""

    level: ScopeLevel
    scope_id: uuid.UUID
    plant_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.level, ScopeLevel):
            raise TypeError("level debe ser ScopeLevel")
        if type(self.scope_id) is not uuid.UUID:
            raise TypeError("scope_id debe ser uuid.UUID")
        if (self.plant_id is None) != (self.level is ScopeLevel.ORGANIZATION):
            raise ValueError("planta y zona llevan su planta; la organización, ninguna")
        if self.level is ScopeLevel.PLANT and self.plant_id != self.scope_id:
            raise ValueError("el alcance de planta es su propia planta")

    def contains(self, other: ScopeRef) -> bool:
        """Organización ⊇ planta ⊇ zona (ambos de la misma organización)."""
        if self.level is ScopeLevel.ORGANIZATION:
            return True
        if self.level is ScopeLevel.PLANT:
            return other.plant_id == self.scope_id
        return other.level is ScopeLevel.ZONE and other.scope_id == self.scope_id

    def overlaps(self, other: ScopeRef) -> bool:
        """Dos alcances se solapan si uno contiene al otro (simétrica)."""
        return self.contains(other) or other.contains(self)


def scope_resource(organization_id: uuid.UUID, scope: ScopeRef) -> Resource:
    """El recurso de ``authorize`` para un alcance."""
    if scope.level is ScopeLevel.ORGANIZATION:
        return Resource.organization(organization_id)
    if scope.level is ScopeLevel.PLANT or scope.plant_id is None:
        return Resource.plant(organization_id, scope.scope_id)
    return Resource.zone(organization_id, scope.plant_id, scope.scope_id)


_PLANT_OF: Final = text(
    "SELECT plant_id FROM identity.plant WHERE organization_id = :organization_id"
    " AND plant_id = :scope_id"
)
_ZONE_PLANT: Final = text(
    "SELECT plant_id FROM identity.zone WHERE organization_id = :organization_id"
    " AND zone_id = :scope_id"
)
_LOCK_ORGANIZATION: Final = text(
    "SELECT kind, status FROM identity.organization WHERE organization_id = :organization_id"
    " FOR NO KEY UPDATE"
)


async def resolve_scope(
    deps: IdentityDependencies, context: ScopeContext, level: object, scope_id: object
) -> ScopeRef:
    """El alcance ``(level, scope_id)`` de la organización del contexto con su planta real.

    Una planta o zona inexistente o de otra organización (la seguridad a nivel de fila no la deja
    ver) responde ``ResourceNotFound``, igual que un recurso fuera de alcance (BR-NUC-09).
    """
    try:
        scope_level = ScopeLevel(str(level)) if isinstance(level, str) else ScopeLevel("")
    except ValueError:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/scope_level") from None
    if type(scope_id) is not uuid.UUID:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/scope_id")
    if scope_level is ScopeLevel.ORGANIZATION:
        if scope_id != context.organization_id:
            raise ResourceNotFound()
        return ScopeRef(ScopeLevel.ORGANIZATION, scope_id)
    statement = _PLANT_OF if scope_level is ScopeLevel.PLANT else _ZONE_PLANT
    rows = await deps.database.read(
        context, statement, {"organization_id": context.organization_id, "scope_id": scope_id}
    )
    if not rows:
        raise ResourceNotFound()
    return ScopeRef(scope_level, scope_id, as_uuid(rows[0].plant_id))


@dataclass(frozen=True, slots=True)
class OrganizationLock:
    kind: str
    status: str


async def lock_organization(transaction: Transaction) -> OrganizationLock:
    """Bloquea la fila de la organización de la transacción (``FOR NO KEY UPDATE``).

    Serializa los cambios de usuarios y roles de una organización: dos asignaciones
    concurrentes del mismo usuario no pasan las dos la comprobación de incompatibilidades, y dos
    desactivaciones concurrentes no dejan entre las dos la organización sin administrador. No
    bloquea las inserciones que solo la referencian (``FOR KEY SHARE``).
    """
    row = (
        await transaction.execute(
            _LOCK_ORGANIZATION, {"organization_id": transaction.context.organization_id}
        )
    ).one_or_none()
    if row is None:
        raise ResourceNotFound()
    return OrganizationLock(kind=row.kind, status=row.status)
