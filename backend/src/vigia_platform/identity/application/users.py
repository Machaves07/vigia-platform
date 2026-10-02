"""Ciclo de vida de las cuentas (LC-NUC-05; BR-NUC-05, 30, 31, 33, 34; PR-NUC-39, 40).

``UserService`` (rutas de usuarios de TASK-136):

- ``invite_user``: autoriza ``users.manage`` sobre la organización y ``roles.manage`` sobre el
  alcance de cada asignación pedida; exige **al menos una** (BR-NUC-30) y que entre ellas no haya
  incompatibilidades (BR-NUC-14). El correo se normaliza a minúsculas y tiene que ser **nuevo en
  toda la plataforma**: si ya existe en cualquier organización se rechaza con
  ``email_unavailable`` y el mismo mensaje, sin decir dónde (BR-NUC-05; la única búsqueda fuera de
  la organización es ``identity.login_organization``, que solo devuelve un identificador). En una
  transacción crea la cuenta ``invited``, sus asignaciones (``role_assigned``,
  ``role_assignment_changed``) y la invitación (``user_invited``); después entrega el enlace por
  ``EmailSenderPort`` o lo divulga una vez (``invitation_link_disclosed``).
- ``deactivate_user``: la cuenta nunca se borra (BR-NUC-33). Cierra sus sesiones
  (``user_deactivated``), cancela sus invitaciones pendientes, marca retiradas sus asignaciones
  en esa fecha y publica ``user_deactivated``. Si con ello la organización cliente activa se
  quedara sin administrador activo de nivel organización, se rechaza con
  ``last_administrator`` y no cambia nada (BR-NUC-31, PR-NUC-40).
- ``reactivate_user``: vuelve a ``invited`` con asignaciones nuevas (al menos una) y una
  invitación nueva, y **descarta las credenciales anteriores**: el hash de la contraseña se
  sustituye por un marcador que nunca verifica, la credencial TOTP queda desactivada y la
  aceptación del aviso se pide de nuevo al activar.
- ``update_profile``: ``display_name`` y ``professional_license`` (BR-NUC-34), con la política de
  texto libre; audita ``user_profile_changed`` con los **nombres** de los campos cambiados, nunca
  sus valores. Ningún registro pasado cambia: cada uno guarda la instantánea del actor.
- ``list_users`` (``GET /users``, TASK-136): ``users.manage`` sobre la organización; las cuentas
  de la organización del contexto (la seguridad a nivel de fila no deja ver otras) con sus
  asignaciones vigentes, en páginas por ``user_id``. Nunca credenciales ni hashes.

``SecondFactorResetService.reset`` (``POST /users/{id}/second-factor/reset``, TASK-136; BR-NUC-29)
autoriza ``users.manage`` sobre la organización y delega en ``SecondFactorService.reset``, que
desactiva la credencial, cierra las sesiones del usuario y audita ``second_factor_reset``.

Todas las escrituras de una organización se serializan con el bloqueo de su fila
(``common.lock_organization``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol

from pydantic import JsonValue
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from vigia_platform.identity.adapters.session_store import end_user_sessions
from vigia_platform.identity.application.common import (
    IdentityDependencies,
    IdentityRejected,
    IdentityRejection,
    ScopeRef,
    as_uuid,
    checked_free_text,
    lock_organization,
    new_uuid4,
    resolve_scope,
    scope_resource,
    unique_violation,
)
from vigia_platform.identity.application.invitations import (
    EmailSenderRegistry,
    InvitationOutcome,
    IssuedInvitation,
    checked_link_base,
    deliver,
    issue_invitation,
)
from vigia_platform.identity.application.roles import (
    Assignment,
    AssignmentRequest,
    AssignmentView,
    active_org_administrators,
    find_conflict,
    insert_assignment,
    last_administrator_violated,
    load_assignments,
    refresh_second_factor_required,
    remove_assignment,
    role_assignable,
)
from vigia_platform.identity.auth.login import normalize_email
from vigia_platform.identity.auth.second_factor import SecondFactorNotFound
from vigia_platform.identity.auth.sessions import SessionEndReason
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, ResourceRef
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "DISCARDED_PASSWORD_HASH",
    "MAX_USERS_PAGE",
    "InviteRequest",
    "PlannedAssignment",
    "ProfileChange",
    "SecondFactorReset",
    "SecondFactorResetService",
    "UserPage",
    "UserService",
    "UserSummary",
    "create_invited_user",
    "plan_assignments",
]

DISPLAY_NAME_MAX: Final = 120
LICENSE_MAX: Final = 64
MAX_USERS_PAGE: Final = 200
"""Tamaño máximo de una página de ``list_users`` (PAT-NUC-ESC-07)."""
EMAIL_UNIQUE_CONSTRAINT: Final = "user_account_email_unique"
DISCARDED_PASSWORD_HASH: Final = "!discarded"  # noqa: S105 - marcador que nunca verifica
"""Lo que queda del hash anterior al reactivar: no es Argon2id, así que nunca verifica."""
_DISCARDED_ALGORITHM: Final = "discarded"
_USER: Final = "user"


@dataclass(frozen=True, slots=True)
class InviteRequest:
    """``POST /users``: correo, nombre, licencia opcional y al menos una asignación."""

    email: str
    display_name: str
    assignments: tuple[AssignmentRequest, ...]
    professional_license: str | None = None
    disclose_link: bool = False
    """El administrador pide ver el enlace (se muestra una sola vez y no se envía correo)."""


@dataclass(frozen=True, slots=True)
class ProfileChange:
    """``PATCH /users/{id}``: ``None`` deja el campo como está."""

    display_name: str | None = None
    professional_license: str | None = None
    clear_professional_license: bool = False


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    role: Role
    scope: ScopeRef


@dataclass(frozen=True, slots=True)
class UserSummary:
    """Una cuenta de la organización con sus asignaciones vigentes (``GET /users``)."""

    user_id: uuid.UUID
    email: str = field(repr=False)
    display_name: str = field(repr=False)
    professional_license: str | None = field(repr=False)
    status: str
    second_factor_required: bool
    second_factor_enrolled: bool
    assignments: tuple[AssignmentView, ...]


@dataclass(frozen=True, slots=True)
class UserPage:
    items: tuple[UserSummary, ...]
    next_after: uuid.UUID | None
    """El ``user_id`` desde el que sigue la página siguiente; ``None`` si no hay más."""


class SecondFactorReset(Protocol):
    """La parte de ``SecondFactorService`` que usa el restablecimiento por el administrador."""

    async def reset(self, admin_context: ScopeContext, user_id: uuid.UUID) -> int: ...


def plan_assignments(
    requested: Sequence[PlannedAssignment], organization_kind: str
) -> tuple[PlannedAssignment, ...]:
    """Las asignaciones de una cuenta nueva o reactivada, comprobadas entre sí (BR-NUC-11, 14)."""
    if not requested:
        raise IdentityRejected(IdentityRejection.ASSIGNMENT_REQUIRED, field="/assignments")
    accepted: list[Assignment] = []
    for index, planned in enumerate(requested):
        if not role_assignable(planned.role, organization_kind):
            raise IdentityRejected(
                IdentityRejection.ROLE_NOT_ASSIGNABLE, field=f"/assignments/{index}/role"
            )
        conflict = find_conflict(accepted, planned.role, planned.scope)
        if conflict is not None:
            other = next(i for i, item in enumerate(accepted) if item is conflict.assignment)
            raise IdentityRejected(
                IdentityRejection.ROLE_INCOMPATIBLE,
                field=f"/assignments/{index}",
                details=(f"/assignments/{other}",),
            )
        # Identificador provisional y ordenado: ``find_conflict`` recorre por identificador.
        accepted.append(Assignment(uuid.UUID(int=index), planned.role, planned.scope))
    return tuple(requested)


_EMAIL_ORGANIZATION: Final = text("SELECT identity.login_organization(:email) AS organization_id")
_INSERT_USER: Final = text(
    "INSERT INTO identity.user_account (user_id, organization_id, email, display_name,"
    " professional_license, status, second_factor_required, created_at)"
    " VALUES (:user_id, :organization_id, :email, :display_name, :professional_license,"
    " 'invited', false, :created_at)"
)
_LOCK_USER: Final = text(
    "SELECT user_id, status FROM identity.user_account WHERE user_id = :user_id FOR UPDATE"
)
_EMAIL_OF: Final = text("SELECT email FROM identity.user_account WHERE user_id = :user_id")
_DEACTIVATE: Final = text(
    "UPDATE identity.user_account SET status = 'deactivated', deactivated_at = :now"
    " WHERE user_id = :user_id"
)
_CANCEL_INVITATIONS: Final = text(
    "UPDATE identity.invitation SET status = 'cancelled'"
    " WHERE user_id = :user_id AND status = 'pending'"
)
_REINVITE: Final = text(
    "UPDATE identity.user_account SET status = 'invited', password_updated_at = NULL,"
    " second_factor_enrolled_at = NULL, privacy_notice_version_accepted = NULL"
    " WHERE user_id = :user_id"
)
_DISCARD_PASSWORD: Final = text(
    "UPDATE identity.password_credential SET password_hash = :hash,"
    " algorithm_version = :algorithm, updated_at = :now, breach_checked_at = NULL"
    " WHERE user_id = :user_id"
)
_DISABLE_TOTP: Final = text(
    "UPDATE identity.totp_credential SET disabled_at = :now"
    " WHERE user_id = :user_id AND disabled_at IS NULL"
)
_UPDATE_PROFILE: Final = text(
    "UPDATE identity.user_account SET display_name = :display_name,"
    " professional_license = :professional_license WHERE user_id = :user_id"
    " RETURNING user_id"
)
_PROFILE: Final = text(
    "SELECT display_name, professional_license FROM identity.user_account"
    " WHERE user_id = :user_id FOR UPDATE"
)
_USERS_PAGE: Final = text(
    "SELECT user_id, email, display_name, professional_license, status,"
    " second_factor_required, second_factor_enrolled_at IS NOT NULL AS second_factor_enrolled"
    " FROM identity.user_account"
    " WHERE CAST(:after AS uuid) IS NULL OR user_id > CAST(:after AS uuid)"
    " ORDER BY user_id LIMIT :limit"
)
_USERS_ASSIGNMENTS: Final = text(
    "SELECT assignment_id, user_id, role, scope_level, scope_id FROM identity.role_assignment"
    " WHERE removed_at IS NULL AND user_id = ANY(CAST(:user_ids AS uuid[]))"
    " ORDER BY assignment_id"
)


async def create_invited_user(
    deps: IdentityDependencies,
    transaction: Transaction,
    *,
    email: str,
    display_name: str,
    professional_license: str | None,
    assignments: Sequence[PlannedAssignment],
    now: datetime,
    user_id: uuid.UUID | None = None,
) -> IssuedInvitation:
    """La cuenta ``invited`` con sus asignaciones e invitación, en ``transaction`` (ya comprobado
    todo lo que no depende de la base). La usan ``invite_user`` y la génesis.

    ``user_id`` solo lo fija el arranque de la plataforma, donde el primer operador es a la vez
    el actor de la orden (``ScopeContexts.bootstrap_operator_context``); si no, uno nuevo.
    """
    context = transaction.context
    found = (await transaction.execute(_EMAIL_ORGANIZATION, {"email": email})).one()
    if found.organization_id is not None:
        raise IdentityRejected(IdentityRejection.EMAIL_UNAVAILABLE, field="/email")
    if user_id is None:
        user_id = new_uuid4(deps.random_bytes)
    elif type(user_id) is not uuid.UUID:
        raise TypeError("user_id debe ser uuid.UUID")
    await transaction.execute(
        _INSERT_USER,
        {
            "user_id": user_id,
            "organization_id": context.organization_id,
            "email": email,
            "display_name": display_name,
            "professional_license": professional_license,
            "created_at": now,
        },
    )
    for planned in assignments:
        await insert_assignment(deps, transaction, user_id, planned.role, planned.scope, now)
    await refresh_second_factor_required(transaction, user_id)
    return await issue_invitation(deps, transaction, user_id, now)


def checked_email(value: object) -> str:
    email = normalize_email(value)
    if email is None:
        raise IdentityRejected(IdentityRejection.INVALID_VALUE, field="/email")
    return email


def checked_license(deps: IdentityDependencies, value: object) -> str | None:
    if value is None:
        return None
    return checked_free_text(
        deps,
        value,
        entity="user_account",
        path="/professional_license",
        max_length=LICENSE_MAX,
    )


def checked_display_name(deps: IdentityDependencies, value: object) -> str:
    return checked_free_text(
        deps, value, entity="user_account", path="/display_name", max_length=DISPLAY_NAME_MAX
    )


@repository
class UserService:
    """Invitar, desactivar, reactivar y editar el perfil (``users.manage``, ``roles.manage``)."""

    def __init__(
        self, deps: IdentityDependencies, *, senders: EmailSenderRegistry, link_base: str
    ) -> None:
        self._deps = deps
        self._senders = senders
        self._link_base = checked_link_base(link_base)

    def __repr__(self) -> str:
        return "UserService()"

    async def _authorized_assignments(
        self, context: ScopeContext, requested: Sequence[AssignmentRequest]
    ) -> tuple[ScopeContext, tuple[PlannedAssignment, ...]]:
        """``users.manage`` sobre la organización y ``roles.manage`` sobre cada alcance."""
        deps = self._deps
        authorized = await deps.authorizer.authorize(
            context, PermissionKey.USERS_MANAGE, Resource.organization(context.organization_id)
        )
        if not isinstance(requested, tuple | list) or not requested:
            raise IdentityRejected(IdentityRejection.ASSIGNMENT_REQUIRED, field="/assignments")
        planned: list[PlannedAssignment] = []
        for index, item in enumerate(requested):
            if not isinstance(item, AssignmentRequest):
                raise IdentityRejected(
                    IdentityRejection.INVALID_VALUE, field=f"/assignments/{index}"
                )
            try:
                role = Role(item.role)
            except ValueError:
                raise IdentityRejected(
                    IdentityRejection.INVALID_VALUE, field=f"/assignments/{index}/role"
                ) from None
            scope = await resolve_scope(deps, context, item.scope_level, item.scope_id)
            await deps.authorizer.authorize(
                context, PermissionKey.ROLES_MANAGE, scope_resource(context.organization_id, scope)
            )
            planned.append(PlannedAssignment(role, scope))
        return authorized, tuple(planned)

    async def invite_user(self, context: ScopeContext, request: InviteRequest) -> InvitationOutcome:
        """Crea la cuenta invitada y entrega el enlace (correo o divulgación única)."""
        deps = self._deps
        if not isinstance(request, InviteRequest):
            raise TypeError("request debe ser InviteRequest")
        email = checked_email(request.email)
        display_name = checked_display_name(deps, request.display_name)
        license_ = checked_license(deps, request.professional_license)
        authorized, planned = await self._authorized_assignments(context, request.assignments)
        now = deps.clock.now()
        try:
            async with deps.database.transaction(authorized) as transaction:
                organization = await lock_organization(transaction)
                plan_assignments(planned, organization.kind)
                issued = await create_invited_user(
                    deps,
                    transaction,
                    email=email,
                    display_name=display_name,
                    professional_license=license_,
                    assignments=planned,
                    now=now,
                )
        except sa_exc.IntegrityError as error:
            # Otra invitación con el mismo correo confirmó entre la búsqueda y el alta.
            if unique_violation(error) == EMAIL_UNIQUE_CONSTRAINT:
                raise IdentityRejected(
                    IdentityRejection.EMAIL_UNAVAILABLE, field="/email"
                ) from None
            raise
        return await deliver(
            deps,
            self._senders,
            authorized,
            issued,
            email=email,
            link_base=self._link_base,
            disclose=request.disclose_link is True,
        )

    async def deactivate_user(self, context: ScopeContext, user_id: uuid.UUID) -> None:
        """Desactiva ``user_id`` (BR-NUC-33) salvo que sea el último administrador (BR-NUC-31)."""
        deps = self._deps
        if type(user_id) is not uuid.UUID:
            raise ResourceNotFound()
        authorized = await deps.authorizer.authorize(
            context, PermissionKey.USERS_MANAGE, Resource.organization(context.organization_id)
        )
        now = deps.clock.now()
        async with deps.database.transaction(authorized) as transaction:
            organization = await lock_organization(transaction)
            user = (await transaction.execute(_LOCK_USER, {"user_id": user_id})).first()
            if user is None:
                raise ResourceNotFound()
            if user.status == "deactivated":
                raise IdentityRejected(IdentityRejection.USER_STATE, field="/user_id")
            before = await active_org_administrators(transaction)
            after = tuple(admin for admin in before if admin != user_id)
            if last_administrator_violated(
                organization_kind=organization.kind,
                organization_status=organization.status,
                admins_before=before,
                admins_after=after,
            ):
                raise IdentityRejected(IdentityRejection.LAST_ADMINISTRATOR, field="/user_id")
            await transaction.execute(_DEACTIVATE, {"user_id": user_id, "now": now})
            await transaction.execute(_CANCEL_INVITATIONS, {"user_id": user_id})
            for assignment in await load_assignments(transaction, user_id):
                await remove_assignment(deps, transaction, user_id, assignment, now)
            await refresh_second_factor_required(transaction, user_id)
            await end_user_sessions(
                transaction, deps.audit, user_id, SessionEndReason.USER_DEACTIVATED, now
            )
            await deps.audit.append(
                authorized,
                AuditOperation.USER_DEACTIVATED,
                resource=ResourceRef(_USER, user_id),
                transaction=transaction,
            )
            await deps.outbox.publish(
                transaction,
                NewEvent(
                    event_name="user_deactivated",
                    payload={
                        "user_id": str(user_id),
                        "deactivated_by": str(authorized.actor.id),
                        "deactivated_at": format_timestamp(now),
                    },
                ),
            )

    async def reactivate_user(
        self,
        context: ScopeContext,
        user_id: uuid.UUID,
        assignments: Sequence[AssignmentRequest],
        *,
        disclose_link: bool = False,
    ) -> InvitationOutcome:
        """Vuelve a invitar a ``user_id`` desactivado, sin sus credenciales anteriores."""
        deps = self._deps
        if type(user_id) is not uuid.UUID:
            raise ResourceNotFound()
        authorized, planned = await self._authorized_assignments(context, assignments)
        now = deps.clock.now()
        async with deps.database.transaction(authorized) as transaction:
            organization = await lock_organization(transaction)
            plan_assignments(planned, organization.kind)
            user = (await transaction.execute(_LOCK_USER, {"user_id": user_id})).first()
            if user is None:
                raise ResourceNotFound()
            if user.status != "deactivated":
                raise IdentityRejected(IdentityRejection.USER_STATE, field="/user_id")
            email_row = (await transaction.execute(_EMAIL_OF, {"user_id": user_id})).one()
            await transaction.execute(_REINVITE, {"user_id": user_id})
            await transaction.execute(
                _DISCARD_PASSWORD,
                {
                    "user_id": user_id,
                    "hash": DISCARDED_PASSWORD_HASH,
                    "algorithm": _DISCARDED_ALGORITHM,
                    "now": now,
                },
            )
            await transaction.execute(_DISABLE_TOTP, {"user_id": user_id, "now": now})
            existing = list(await load_assignments(transaction, user_id))
            for planned_item in planned:
                conflict = find_conflict(existing, planned_item.role, planned_item.scope)
                if conflict is not None:
                    raise IdentityRejected(
                        IdentityRejection.ROLE_INCOMPATIBLE,
                        field="/assignments",
                        conflict_assignment_id=conflict.assignment.assignment_id,
                    )
                new_id = await insert_assignment(
                    deps, transaction, user_id, planned_item.role, planned_item.scope, now
                )
                existing.append(Assignment(new_id, planned_item.role, planned_item.scope))
            await refresh_second_factor_required(transaction, user_id)
            await deps.audit.append(
                authorized,
                AuditOperation.USER_REACTIVATED,
                resource=ResourceRef(_USER, user_id),
                transaction=transaction,
            )
            issued = await issue_invitation(deps, transaction, user_id, now)
        return await deliver(
            deps,
            self._senders,
            authorized,
            issued,
            email=email_row.email,
            link_base=self._link_base,
            disclose=disclose_link is True,
        )

    async def update_profile(
        self, context: ScopeContext, user_id: uuid.UUID, change: ProfileChange
    ) -> None:
        """Cambia nombre y licencia de ``user_id`` (BR-NUC-34) y lo audita sin los valores."""
        deps = self._deps
        if type(user_id) is not uuid.UUID:
            raise ResourceNotFound()
        if not isinstance(change, ProfileChange):
            raise TypeError("change debe ser ProfileChange")
        display_name = (
            None if change.display_name is None else checked_display_name(deps, change.display_name)
        )
        license_ = checked_license(deps, change.professional_license)
        authorized = await deps.authorizer.authorize(
            context, PermissionKey.USERS_MANAGE, Resource.organization(context.organization_id)
        )
        async with deps.database.transaction(authorized) as transaction:
            current = (await transaction.execute(_PROFILE, {"user_id": user_id})).first()
            if current is None:
                raise ResourceNotFound()
            new_name = current.display_name if display_name is None else display_name
            new_license = (
                None
                if change.clear_professional_license
                else (current.professional_license if license_ is None else license_)
            )
            changed = sorted(
                name
                for name, before, after in (
                    ("display_name", current.display_name, new_name),
                    ("professional_license", current.professional_license, new_license),
                )
                if before != after
            )
            if not changed:
                return
            await transaction.execute(
                _UPDATE_PROFILE,
                {
                    "user_id": user_id,
                    "display_name": new_name,
                    "professional_license": new_license,
                },
            )
            await deps.audit.append(
                authorized,
                AuditOperation.USER_PROFILE_CHANGED,
                resource=ResourceRef(_USER, as_uuid(user_id)),
                filters={"fields": list[JsonValue](changed)},
                transaction=transaction,
            )

    async def list_users(
        self, context: ScopeContext, *, after: uuid.UUID | None = None, limit: int = MAX_USERS_PAGE
    ) -> UserPage:
        """Una página de las cuentas de la organización con sus asignaciones vigentes."""
        deps = self._deps
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_USERS_PAGE
        ):
            raise ValueError(f"limit debe estar entre 1 y {MAX_USERS_PAGE}")
        if after is not None and type(after) is not uuid.UUID:
            raise TypeError("after debe ser uuid.UUID")
        authorized = await deps.authorizer.authorize(
            context, PermissionKey.USERS_MANAGE, Resource.organization(context.organization_id)
        )
        async with deps.database.transaction(authorized) as transaction:
            rows = (
                await transaction.execute(
                    _USERS_PAGE,
                    {"after": None if after is None else str(after), "limit": limit + 1},
                )
            ).all()
            more = len(rows) > limit
            rows = rows[:limit]
            user_ids = [str(row.user_id) for row in rows]
            held = (
                (await transaction.execute(_USERS_ASSIGNMENTS, {"user_ids": user_ids})).all()
                if user_ids
                else []
            )
        by_user: dict[uuid.UUID, list[AssignmentView]] = {}
        for row in held:
            owner = as_uuid(row.user_id)
            by_user.setdefault(owner, []).append(
                AssignmentView(
                    as_uuid(row.assignment_id),
                    owner,
                    Role(row.role),
                    ScopeLevel(row.scope_level),
                    as_uuid(row.scope_id),
                )
            )
        items = tuple(
            UserSummary(
                user_id=as_uuid(row.user_id),
                email=row.email,
                display_name=row.display_name,
                professional_license=row.professional_license,
                status=row.status,
                second_factor_required=bool(row.second_factor_required),
                second_factor_enrolled=bool(row.second_factor_enrolled),
                assignments=tuple(by_user.get(as_uuid(row.user_id), ())),
            )
            for row in rows
        )
        return UserPage(items, items[-1].user_id if more and items else None)


@repository
class SecondFactorResetService:
    """``POST /users/{id}/second-factor/reset``: ``users.manage`` y el restablecimiento."""

    def __init__(self, deps: IdentityDependencies, second_factor: SecondFactorReset) -> None:
        self._deps = deps
        self._second_factor = second_factor

    def __repr__(self) -> str:
        return "SecondFactorResetService()"

    async def reset(self, context: ScopeContext, user_id: uuid.UUID) -> int:
        """Restablece el segundo factor de ``user_id`` (BR-NUC-29); las sesiones cerradas.

        Un usuario de otra organización o inexistente responde ``ResourceNotFound``: la
        seguridad a nivel de fila no lo deja ver y el almacén lanza ``SecondFactorNotFound``.
        """
        if type(user_id) is not uuid.UUID:
            raise ResourceNotFound()
        authorized = await self._deps.authorizer.authorize(
            context, PermissionKey.USERS_MANAGE, Resource.organization(context.organization_id)
        )
        try:
            return await self._second_factor.reset(authorized, user_id)
        except SecondFactorNotFound:
            raise ResourceNotFound() from None
