"""Asignación y retiro de roles (LC-NUC-05; BR-NUC-11, 14, 19, 31; PR-NUC-04, 40).

**Reglas puras** (sin base, las prueban PR-NUC-04 y los ejemplos):

- ``ROLE_ORGANIZATION_KIND``: ``provider_installer`` y ``platform_operator`` solo en la
  organización proveedora; los otros cinco, solo en organizaciones cliente (BR-NUC-11).
- ``INCOMPATIBLE_ROLES`` y ``find_conflict``: BR-NUC-14. (1) ``line_manager`` sobre una zona Z es
  incompatible con ``coordinator_sst``, ``plant_manager`` o ``administrator`` sobre cualquier
  alcance que contenga a Z; (2) ``administrator`` es incompatible con ``coordinator_sst`` y con
  ``plant_manager`` sobre alcances que se solapen. Las dos se aplican como "pareja incompatible
  sobre alcances que se solapan" (uno contiene al otro): para un ``line_manager`` de zona es
  exactamente la regla (1), y la misma relación cubre un ``line_manager`` de planta u
  organización, que el diseño no prohíbe y que de otro modo esquivaría la regla. Repetir una
  asignación vigente (mismo rol y alcance) también se rechaza. Como el solapamiento es simétrico
  y la lista son parejas, la verificación es la misma en las dos direcciones: ``asignar(a)`` y
  luego ``asignar(b)`` se rechaza si y solo si ``asignar(b)`` y luego ``asignar(a)`` se rechaza.
- ``self_assignment_allowed``: nadie se asigna un rol que no tiene ya sobre un alcance que
  contenga al nuevo (BR-NUC-19); ampliar el propio alcance es asignarse lo que no se tiene.
- ``last_administrator_violated``: la regla del último administrador (BR-NUC-31) cuenta los usuarios
  **activos** con ``administrator`` vigente de nivel organización, los que pueden invitar a otro.

**Servicio** (``RoleService``): ``assign_role`` y ``remove_role`` autorizan ``roles.manage``
sobre el alcance de la asignación y, en una transacción que bloquea la organización (las
comprobaciones y la escritura son atómicas frente a otra asignación concurrente), comprueban las
reglas,
escriben la asignación (o la marca de retiro: nunca se borra), recalculan
``second_factor_required``, auditan ``role_assigned`` o ``role_removed`` y publican
``role_assignment_changed``. Un rechazo por incompatibilidad o por asignarse a sí mismo se audita
como ``role_assignment_rejected`` (``outcome = denied``) con la asignación en conflicto, en una
transacción aparte después de revertir.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text

from vigia_platform.identity.application.common import (
    IdentityDependencies,
    IdentityRejected,
    IdentityRejection,
    ScopeRef,
    as_uuid,
    lock_organization,
    resolve_scope,
    scope_resource,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    ResourceRef,
)
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "CLIENT_ROLES",
    "INCOMPATIBLE_ROLES",
    "PROVIDER_ROLES",
    "ROLES_REQUIRING_SECOND_FACTOR",
    "Assignment",
    "AssignmentRequest",
    "Conflict",
    "RoleService",
    "active_org_administrators",
    "audit_rejection",
    "check_new_assignment",
    "find_conflict",
    "insert_assignment",
    "last_administrator_violated",
    "load_assignments",
    "refresh_second_factor_required",
    "remove_assignment",
    "role_assignable",
    "self_assignment_allowed",
]

PROVIDER_ROLES: Final = frozenset({Role.PROVIDER_INSTALLER, Role.PLATFORM_OPERATOR})
CLIENT_ROLES: Final = frozenset(Role) - PROVIDER_ROLES

ROLES_REQUIRING_SECOND_FACTOR: Final = frozenset({Role.ADMINISTRATOR, Role.PLATFORM_OPERATOR})
"""``second_factor_required`` es verdadero con una de estas asignaciones vigente (BR-NUC-21)."""

INCOMPATIBLE_ROLES: Final[frozenset[frozenset[Role]]] = frozenset(
    {
        frozenset({Role.LINE_MANAGER, Role.COORDINATOR_SST}),
        frozenset({Role.LINE_MANAGER, Role.PLANT_MANAGER}),
        frozenset({Role.LINE_MANAGER, Role.ADMINISTRATOR}),
        frozenset({Role.ADMINISTRATOR, Role.COORDINATOR_SST}),
        frozenset({Role.ADMINISTRATOR, Role.PLANT_MANAGER}),
    }
)
"""Parejas de BR-NUC-14 (simétricas)."""

_USER: Final = "user"
_ASSIGNMENT: Final = "role_assignment"


@dataclass(frozen=True, slots=True)
class AssignmentRequest:
    """Una asignación pedida: rol sobre ``(scope_level, scope_id)``."""

    role: Role
    scope_level: ScopeLevel
    scope_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Assignment:
    """Una asignación vigente con su alcance real."""

    assignment_id: uuid.UUID
    role: Role
    scope: ScopeRef


@dataclass(frozen=True, slots=True)
class Conflict:
    """La asignación vigente que impide la nueva (``duplicate`` si es la misma)."""

    assignment: Assignment
    duplicate: bool


def role_assignable(role: Role, organization_kind: str) -> bool:
    """BR-NUC-11: los roles del proveedor solo en la proveedora; los demás, solo en clientes."""
    return (role in PROVIDER_ROLES) == (organization_kind == "provider")


def find_conflict(existing: Iterable[Assignment], role: Role, scope: ScopeRef) -> Conflict | None:
    """La primera asignación vigente (por identificador) que choca con ``role`` sobre ``scope``."""
    for current in sorted(existing, key=lambda item: item.assignment_id):
        if current.role is role and current.scope == scope:
            return Conflict(current, duplicate=True)
        if frozenset({current.role, role}) in INCOMPATIBLE_ROLES and current.scope.overlaps(scope):
            return Conflict(current, duplicate=False)
    return None


def self_assignment_allowed(existing: Iterable[Assignment], role: Role, scope: ScopeRef) -> bool:
    """¿Tiene ya el actor ``role`` sobre un alcance que contiene a ``scope``? (BR-NUC-19)."""
    return any(current.role is role and current.scope.contains(scope) for current in existing)


# --- Sentencias ---------------------------------------------------------------------------------

_LOAD_ASSIGNMENTS: Final = text(
    "SELECT r.assignment_id, r.role, r.scope_level, r.scope_id,"
    " COALESCE(p.plant_id, z.plant_id) AS plant_id"
    " FROM identity.role_assignment AS r"
    " LEFT JOIN identity.plant AS p ON r.scope_level = 'plant' AND p.plant_id = r.scope_id"
    " LEFT JOIN identity.zone AS z ON r.scope_level = 'zone' AND z.zone_id = r.scope_id"
    " WHERE r.user_id = :user_id AND r.removed_at IS NULL ORDER BY r.assignment_id"
)
_LOCK_USER: Final = text(
    "SELECT user_id, status FROM identity.user_account WHERE user_id = :user_id FOR UPDATE"
)
_INSERT_ASSIGNMENT: Final = text(
    "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id, role,"
    " scope_level, scope_id, assigned_at, assigned_by) VALUES (:assignment_id,"
    " :organization_id, :user_id, :role, :scope_level, :scope_id, :assigned_at, :assigned_by)"
)
_REMOVE_ASSIGNMENT: Final = text(
    "UPDATE identity.role_assignment SET removed_at = :removed_at, removed_by = :removed_by"
    " WHERE assignment_id = :assignment_id AND removed_at IS NULL RETURNING assignment_id"
)
_ASSIGNMENT_BY_ID: Final = text(
    "SELECT r.assignment_id, r.user_id, r.role, r.scope_level, r.scope_id, r.removed_at"
    " FROM identity.role_assignment AS r WHERE r.assignment_id = :assignment_id"
)
_ACTIVE_ORG_ADMINISTRATORS: Final = text(
    "SELECT DISTINCT r.user_id FROM identity.role_assignment AS r"
    " JOIN identity.user_account AS u ON u.user_id = r.user_id"
    " WHERE r.role = 'administrator' AND r.scope_level = 'organization' AND r.removed_at IS NULL"
    " AND u.status = 'active'"
)
_SECOND_FACTOR_REQUIRED: Final = text(
    "UPDATE identity.user_account SET second_factor_required = EXISTS ("
    "SELECT FROM identity.role_assignment AS r WHERE r.user_id = :user_id"
    " AND r.removed_at IS NULL AND r.role IN ('administrator', 'platform_operator'))"
    " WHERE user_id = :user_id"
)


async def load_assignments(transaction: Transaction, user_id: uuid.UUID) -> tuple[Assignment, ...]:
    """Las asignaciones vigentes de ``user_id`` con su alcance real."""
    rows = (await transaction.execute(_LOAD_ASSIGNMENTS, {"user_id": user_id})).all()
    result: list[Assignment] = []
    for row in rows:
        level = ScopeLevel(row.scope_level)
        plant = None if level is ScopeLevel.ORGANIZATION else as_uuid(row.plant_id)
        result.append(
            Assignment(
                as_uuid(row.assignment_id),
                Role(row.role),
                ScopeRef(level, as_uuid(row.scope_id), plant),
            )
        )
    return tuple(result)


async def active_org_administrators(transaction: Transaction) -> tuple[uuid.UUID, ...]:
    rows = (await transaction.execute(_ACTIVE_ORG_ADMINISTRATORS)).all()
    return tuple(as_uuid(row.user_id) for row in rows)


async def refresh_second_factor_required(transaction: Transaction, user_id: uuid.UUID) -> None:
    """``second_factor_required`` derivado de las asignaciones vigentes (domain-entities §2.4)."""
    await transaction.execute(_SECOND_FACTOR_REQUIRED, {"user_id": user_id})


def _event(
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
    change: str,
    role: Role,
    scope: ScopeRef,
    changed_by: uuid.UUID,
    changed_at: datetime,
) -> NewEvent:
    return NewEvent(
        event_name="role_assignment_changed",
        payload={
            "user_id": str(user_id),
            "assignment_id": str(assignment_id),
            "change": change,
            "role": role.value,
            "scope_level": scope.level.value,
            "scope_id": str(scope.scope_id),
            "changed_by": str(changed_by),
            "changed_at": format_timestamp(changed_at),
        },
    )


def _scope_place(scope: ScopeRef) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Planta y zona de la entrada de auditoría de una asignación."""
    zone = scope.scope_id if scope.level is ScopeLevel.ZONE else None
    return scope.plant_id, zone


async def insert_assignment(
    deps: IdentityDependencies,
    transaction: Transaction,
    user_id: uuid.UUID,
    role: Role,
    scope: ScopeRef,
    now: datetime,
) -> uuid.UUID:
    """Escribe una asignación ya comprobada, la audita y publica el cambio (sin recalcular el
    segundo factor: lo hace quien llama una vez)."""
    context = transaction.context
    assignment_id = uuid7(deps.clock, deps.random_bytes)
    await transaction.execute(
        _INSERT_ASSIGNMENT,
        {
            "assignment_id": assignment_id,
            "organization_id": context.organization_id,
            "user_id": user_id,
            "role": role.value,
            "scope_level": scope.level.value,
            "scope_id": scope.scope_id,
            "assigned_at": now,
            "assigned_by": context.actor.id,
        },
    )
    plant_id, zone_id = _scope_place(scope)
    await deps.audit.append(
        context,
        AuditOperation.ROLE_ASSIGNED,
        plant_id=plant_id,
        zone_id=zone_id,
        resource=ResourceRef(_ASSIGNMENT, assignment_id),
        filters={"user_id": str(user_id), "role": role.value, "scope_level": scope.level.value},
        transaction=transaction,
    )
    await deps.outbox.publish(
        transaction,
        _event(user_id, assignment_id, "assigned", role, scope, context.actor.id, now),
    )
    return assignment_id


async def remove_assignment(
    deps: IdentityDependencies,
    transaction: Transaction,
    user_id: uuid.UUID,
    assignment: Assignment,
    now: datetime,
) -> bool:
    """Marca retirada una asignación vigente (nunca se borra), la audita y publica el cambio."""
    context = transaction.context
    removed = (
        await transaction.execute(
            _REMOVE_ASSIGNMENT,
            {
                "assignment_id": assignment.assignment_id,
                "removed_at": now,
                "removed_by": context.actor.id,
            },
        )
    ).first()
    if removed is None:
        return False
    plant_id, zone_id = _scope_place(assignment.scope)
    await deps.audit.append(
        context,
        AuditOperation.ROLE_REMOVED,
        plant_id=plant_id,
        zone_id=zone_id,
        resource=ResourceRef(_ASSIGNMENT, assignment.assignment_id),
        filters={
            "user_id": str(user_id),
            "role": assignment.role.value,
            "scope_level": assignment.scope.level.value,
        },
        transaction=transaction,
    )
    await deps.outbox.publish(
        transaction,
        _event(
            user_id,
            assignment.assignment_id,
            "removed",
            assignment.role,
            assignment.scope,
            context.actor.id,
            now,
        ),
    )
    return True


async def audit_rejection(
    deps: IdentityDependencies,
    context: ScopeContext,
    user_id: uuid.UUID,
    role: Role,
    scope: ScopeRef,
    rejection: IdentityRejected,
) -> None:
    """``role_assignment_rejected`` con la asignación en conflicto, en su propia transacción."""
    filters: dict[str, str] = {
        "user_id": str(user_id),
        "role": role.value,
        "scope_level": scope.level.value,
        "reason": rejection.code.value,
    }
    if rejection.conflict_assignment_id is not None:
        filters["conflict_assignment_id"] = str(rejection.conflict_assignment_id)
    plant_id, zone_id = _scope_place(scope)
    await deps.audit.append(
        context,
        AuditOperation.ROLE_ASSIGNMENT_REJECTED,
        outcome=AuditOutcome.DENIED,
        plant_id=plant_id,
        zone_id=zone_id,
        resource=ResourceRef(_USER, user_id),
        filters=filters,
    )


def check_new_assignment(
    *,
    organization_kind: str,
    existing: Sequence[Assignment],
    actor_assignments: Sequence[Assignment] | None,
    role: Role,
    scope: ScopeRef,
) -> None:
    """Las reglas puras de una asignación nueva; ``IdentityRejected`` con la primera que falla.

    ``actor_assignments`` son las del actor cuando se asigna a sí mismo (``None`` si no).
    """
    if not role_assignable(role, organization_kind):
        raise IdentityRejected(IdentityRejection.ROLE_NOT_ASSIGNABLE, field="/role")
    if actor_assignments is not None and not self_assignment_allowed(
        actor_assignments, role, scope
    ):
        raise IdentityRejected(IdentityRejection.SELF_ASSIGNMENT, field="/role")
    conflict = find_conflict(existing, role, scope)
    if conflict is not None:
        raise IdentityRejected(
            IdentityRejection.ROLE_INCOMPATIBLE,
            field="/role",
            conflict_assignment_id=conflict.assignment.assignment_id,
        )


def last_administrator_violated(
    *,
    organization_kind: str,
    organization_status: str,
    admins_before: Sequence[uuid.UUID],
    admins_after: Sequence[uuid.UUID],
) -> bool:
    """BR-NUC-31: una organización cliente activa no pasa de tener administrador activo a no
    tenerlo."""
    return (
        organization_kind == "client"
        and organization_status == "active"
        and len(set(admins_before)) > 0
        and len(set(admins_after)) == 0
    )


# --- Servicio -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentView:
    assignment_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    scope_level: ScopeLevel
    scope_id: uuid.UUID


@repository
class RoleService:
    """``assign_role`` y ``remove_role`` (rutas ``POST/DELETE /users/{id}/roles``, TASK-136)."""

    def __init__(self, deps: IdentityDependencies) -> None:
        self._deps = deps

    async def assign_role(
        self,
        context: ScopeContext,
        user_id: uuid.UUID,
        request: AssignmentRequest,
    ) -> AssignmentView:
        """Asigna ``request`` a ``user_id``; ``IdentityRejected`` o ``ResourceNotFound``."""
        deps = self._deps
        if type(user_id) is not uuid.UUID:
            raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/user_id")
        try:
            role = Role(request.role)
        except ValueError:
            raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/role") from None
        scope = await resolve_scope(deps, context, request.scope_level, request.scope_id)
        authorized = await deps.authorizer.authorize(
            context, PermissionKey.ROLES_MANAGE, scope_resource(context.organization_id, scope)
        )
        now = deps.clock.now()
        try:
            async with deps.database.transaction(authorized) as transaction:
                organization = await lock_organization(transaction)
                user = (await transaction.execute(_LOCK_USER, {"user_id": user_id})).first()
                if user is None:
                    raise ResourceNotFound()
                if user.status == "deactivated":
                    raise IdentityRejected(IdentityRejection.USER_STATE, field="/user_id")
                existing = await load_assignments(transaction, user_id)
                actor_assignments = existing if authorized.actor.id == user_id else None
                check_new_assignment(
                    organization_kind=organization.kind,
                    existing=existing,
                    actor_assignments=actor_assignments,
                    role=role,
                    scope=scope,
                )
                assignment_id = await insert_assignment(
                    deps, transaction, user_id, role, scope, now
                )
                await refresh_second_factor_required(transaction, user_id)
        except IdentityRejected as rejected:
            if rejected.code in (
                IdentityRejection.ROLE_INCOMPATIBLE,
                IdentityRejection.SELF_ASSIGNMENT,
                IdentityRejection.ROLE_NOT_ASSIGNABLE,
            ):
                await audit_rejection(deps, authorized, user_id, role, scope, rejected)
            raise
        return AssignmentView(assignment_id, user_id, role, scope.level, scope.scope_id)

    async def remove_role(
        self, context: ScopeContext, user_id: uuid.UUID, assignment_id: uuid.UUID
    ) -> None:
        """Retira la asignación ``assignment_id`` de ``user_id`` (queda con su fecha)."""
        deps = self._deps
        if type(user_id) is not uuid.UUID or type(assignment_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await deps.database.read(
            context, _ASSIGNMENT_BY_ID, {"assignment_id": assignment_id}
        )
        if not rows or as_uuid(rows[0].user_id) != user_id:
            raise ResourceNotFound()
        row = rows[0]
        scope = await resolve_scope(deps, context, row.scope_level, as_uuid(row.scope_id))
        authorized = await deps.authorizer.authorize(
            context, PermissionKey.ROLES_MANAGE, scope_resource(context.organization_id, scope)
        )
        now = deps.clock.now()
        async with deps.database.transaction(authorized) as transaction:
            organization = await lock_organization(transaction)
            existing = await load_assignments(transaction, user_id)
            target = next((a for a in existing if a.assignment_id == assignment_id), None)
            if target is None:
                raise IdentityRejected(IdentityRejection.ALREADY_REMOVED, field="/assignment_id")
            before = await active_org_administrators(transaction)
            after = _administrators_after_removal(before, user_id, existing, target)
            if last_administrator_violated(
                organization_kind=organization.kind,
                organization_status=organization.status,
                admins_before=before,
                admins_after=after,
            ):
                raise IdentityRejected(IdentityRejection.LAST_ADMINISTRATOR, field="/assignment_id")
            await remove_assignment(deps, transaction, user_id, target, now)
            await refresh_second_factor_required(transaction, user_id)


def _administrators_after_removal(
    before: Sequence[uuid.UUID],
    user_id: uuid.UUID,
    existing: Sequence[Assignment],
    target: Assignment,
) -> tuple[uuid.UUID, ...]:
    """Los administradores activos de organización que quedan si se retira ``target``."""
    still_admin = any(
        a.assignment_id != target.assignment_id
        and a.role is Role.ADMINISTRATOR
        and a.scope.level is ScopeLevel.ORGANIZATION
        for a in existing
    )
    if user_id in before and not still_admin:
        return tuple(admin for admin in before if admin != user_id)
    return tuple(before)
