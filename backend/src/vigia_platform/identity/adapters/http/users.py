"""Rutas de usuarios y roles (``business-logic-model.md`` §10.2; BR-NUC-05, 11, 14, 19, 29 a 34).

Con ``users.manage`` (``administrator`` de su organización y ``platform_operator`` en la
proveedora):

- ``GET /users``: las cuentas de la organización con sus asignaciones vigentes, en páginas por
  ``user_id`` (``after``, ``limit`` ≤ 200). Nunca credenciales ni hashes.
- ``POST /users``: invita (``201``) con al menos una asignación (BR-NUC-30). El enlace se envía por
  correo o, sin correo o a petición (``disclose_link``), va en la respuesta **una sola vez**
  (BR-NUC-32). Un correo que ya tiene cuenta en cualquier organización responde ``conflict`` sin
  decir dónde (BR-NUC-05).
- ``POST /users/{user_id}/deactivate`` y ``POST /users/{user_id}/reactivate`` (BR-NUC-31, 33): la
  última cuenta administradora activa no se desactiva (``conflict``); la reactivación pide
  asignaciones nuevas y emite otra invitación.
- ``PATCH /users/{user_id}``: ``display_name`` y ``professional_license`` (BR-NUC-34).
- ``POST /users/{user_id}/second-factor/reset``: restablece el segundo factor y cierra sus sesiones
  (BR-NUC-29).

Con ``roles.manage`` (BR-NUC-19; la ruta exige la clave y el servicio la autoriza sobre el alcance
de la asignación):

- ``POST /users/{user_id}/roles``: asigna (``201``). Una incompatibilidad (BR-NUC-14) o asignarse
  a sí mismo un rol que no se tiene responde ``conflict`` y queda auditada como
  ``role_assignment_rejected`` con el identificador de la asignación en conflicto
  (``conflict_assignment_id``). El cuerpo de error es el genérico y cerrado de la plataforma
  (``ApiErrorBody``): la asignación en conflicto se nombra en la auditoría y la persona que
  administra la ve en ``GET /users``.
- ``DELETE /users/{user_id}/roles/{assignment_id}``: retira (``204``); nunca se borra.

Un usuario, una asignación, una planta o una zona de otra organización (o inexistentes) responden
``not_found`` igual (BR-NUC-09).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

from vigia_platform.identity.adapters.http.services import (
    IdentityHttp,
    identity_error,
    identity_http,
    installed,
    no_store,
)
from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.identity.application.invitations import InvitationDelivery, InvitationOutcome
from vigia_platform.identity.application.roles import AssignmentRequest
from vigia_platform.identity.application.roles import AssignmentView as AssignedRole
from vigia_platform.identity.application.users import (
    MAX_USERS_PAGE,
    InviteRequest,
    ProfileChange,
    UserSummary,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["users_router"]

MAX_ASSIGNMENTS: Final = 32
"""Asignaciones por invitación o reactivación ``[objetivo propio]``."""

Services = Annotated[IdentityHttp, Depends(identity_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AssignmentBody(_Strict):
    role: Role
    scope_level: ScopeLevel
    scope_id: uuid.UUID

    def request(self) -> AssignmentRequest:
        return AssignmentRequest(self.role, self.scope_level, self.scope_id)


class InviteBody(_Strict):
    email: StrictStr = Field(min_length=3, max_length=254)
    display_name: StrictStr = Field(min_length=1, max_length=120)
    professional_license: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    assignments: tuple[AssignmentBody, ...] = Field(min_length=1, max_length=MAX_ASSIGNMENTS)
    disclose_link: StrictBool = False


class ReactivateBody(_Strict):
    assignments: tuple[AssignmentBody, ...] = Field(min_length=1, max_length=MAX_ASSIGNMENTS)
    disclose_link: StrictBool = False


class ProfileBody(_Strict):
    display_name: StrictStr | None = Field(default=None, min_length=1, max_length=120)
    professional_license: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    clear_professional_license: StrictBool = False


class UserAssignmentView(_Strict):
    assignment_id: uuid.UUID
    role: Role
    scope_level: ScopeLevel
    scope_id: uuid.UUID


class ManagedUserView(_Strict):
    user_id: uuid.UUID
    email: str
    display_name: str
    professional_license: str | None
    status: str
    second_factor_required: bool
    second_factor_enrolled: bool
    assignments: tuple[UserAssignmentView, ...]


class UsersPage(_Strict):
    users: tuple[ManagedUserView, ...]
    next_after: uuid.UUID | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


class InvitationView(_Strict):
    user_id: uuid.UUID
    invitation_id: uuid.UUID
    expires_at: str
    delivery: InvitationDelivery
    link: str | None
    """Solo con ``link_disclosed``: se muestra esta única vez (BR-NUC-32)."""


class DeactivatedView(_Strict):
    user_id: uuid.UUID
    status: Literal["deactivated"]


class SecondFactorResetView(_Strict):
    user_id: uuid.UUID
    sessions_closed: int


class AssignedView(_Strict):
    assignment_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    scope_level: ScopeLevel
    scope_id: uuid.UUID


def _assignment(view: AssignedRole) -> UserAssignmentView:
    return UserAssignmentView(
        assignment_id=view.assignment_id,
        role=view.role,
        scope_level=view.scope_level,
        scope_id=view.scope_id,
    )


def _user(summary: UserSummary) -> ManagedUserView:
    return ManagedUserView(
        user_id=summary.user_id,
        email=summary.email,
        display_name=summary.display_name,
        professional_license=summary.professional_license,
        status=summary.status,
        second_factor_required=summary.second_factor_required,
        second_factor_enrolled=summary.second_factor_enrolled,
        assignments=tuple(_assignment(item) for item in summary.assignments),
    )


def _invitation(outcome: InvitationOutcome) -> InvitationView:
    return InvitationView(
        user_id=outcome.user_id,
        invitation_id=outcome.invitation_id,
        expires_at=format_timestamp(outcome.expires_at),
        delivery=outcome.delivery,
        link=outcome.link,
    )


_USERS_MANAGE: Final = PermissionKey.USERS_MANAGE.value
_ROLES_MANAGE: Final = PermissionKey.ROLES_MANAGE.value


def users_router() -> APIRouter:
    router = APIRouter(tags=["usuarios y roles"])

    @router.get(
        "/users",
        dependencies=[requires(_USERS_MANAGE)],
        summary="Cuentas de la organización con sus asignaciones vigentes",
    )
    async def list_users(
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[uuid.UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_USERS_PAGE)] = MAX_USERS_PAGE,
    ) -> UsersPage:
        no_store(response)
        page = await installed(services.users).list_users(
            request_context(request), after=after, limit=limit
        )
        return UsersPage(
            users=tuple(_user(item) for item in page.items), next_after=page.next_after
        )

    @router.post(
        "/users",
        status_code=201,
        dependencies=[requires(_USERS_MANAGE)],
        summary="Invitar a una persona con al menos una asignación",
    )
    async def invite(
        body: InviteBody, request: Request, response: Response, services: Services
    ) -> InvitationView:
        no_store(response)
        try:
            outcome = await installed(services.users).invite_user(
                request_context(request),
                InviteRequest(
                    email=body.email,
                    display_name=body.display_name,
                    professional_license=body.professional_license,
                    assignments=tuple(item.request() for item in body.assignments),
                    disclose_link=body.disclose_link,
                ),
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        return _invitation(outcome)

    @router.post(
        "/users/{user_id}/deactivate",
        dependencies=[requires(_USERS_MANAGE)],
        summary="Desactivar una cuenta (nunca se borra)",
    )
    async def deactivate(
        user_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> DeactivatedView:
        no_store(response)
        try:
            await installed(services.users).deactivate_user(request_context(request), user_id)
        except IdentityRejected as error:
            raise identity_error(error) from None
        return DeactivatedView(user_id=user_id, status="deactivated")

    @router.post(
        "/users/{user_id}/reactivate",
        dependencies=[requires(_USERS_MANAGE)],
        summary="Reactivar una cuenta con asignaciones nuevas y otra invitación",
    )
    async def reactivate(
        user_id: uuid.UUID,
        body: ReactivateBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> InvitationView:
        no_store(response)
        try:
            outcome = await installed(services.users).reactivate_user(
                request_context(request),
                user_id,
                tuple(item.request() for item in body.assignments),
                disclose_link=body.disclose_link,
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        return _invitation(outcome)

    @router.patch(
        "/users/{user_id}",
        status_code=204,
        dependencies=[requires(_USERS_MANAGE)],
        summary="Editar el nombre y la licencia profesional",
    )
    async def update_profile(
        user_id: uuid.UUID, body: ProfileBody, request: Request, services: Services
    ) -> Response:
        try:
            await installed(services.users).update_profile(
                request_context(request),
                user_id,
                ProfileChange(
                    display_name=body.display_name,
                    professional_license=body.professional_license,
                    clear_professional_license=body.clear_professional_license,
                ),
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        response = Response(status_code=204)
        no_store(response)
        return response

    @router.post(
        "/users/{user_id}/second-factor/reset",
        dependencies=[requires(_USERS_MANAGE)],
        summary="Restablecer el segundo factor de una persona",
    )
    async def reset_second_factor(
        user_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> SecondFactorResetView:
        no_store(response)
        closed = await installed(services.second_factor_reset).reset(
            request_context(request), user_id
        )
        return SecondFactorResetView(user_id=user_id, sessions_closed=closed)

    @router.post(
        "/users/{user_id}/roles",
        status_code=201,
        dependencies=[requires(_ROLES_MANAGE)],
        summary="Asignar un rol sobre un alcance",
    )
    async def assign_role(
        user_id: uuid.UUID,
        body: AssignmentBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> AssignedView:
        no_store(response)
        try:
            view = await installed(services.roles).assign_role(
                request_context(request), user_id, body.request()
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        return AssignedView(
            assignment_id=view.assignment_id,
            user_id=view.user_id,
            role=view.role,
            scope_level=view.scope_level,
            scope_id=view.scope_id,
        )

    @router.delete(
        "/users/{user_id}/roles/{assignment_id}",
        status_code=204,
        dependencies=[requires(_ROLES_MANAGE)],
        summary="Retirar una asignación (queda con su fecha)",
    )
    async def remove_role(
        user_id: uuid.UUID, assignment_id: uuid.UUID, request: Request, services: Services
    ) -> Response:
        try:
            await installed(services.roles).remove_role(
                request_context(request), user_id, assignment_id
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        response = Response(status_code=204)
        no_store(response)
        return response

    return router
