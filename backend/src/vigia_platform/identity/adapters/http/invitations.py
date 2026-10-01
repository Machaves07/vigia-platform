"""Activación de la cuenta con el enlace de invitación (§2 y §10.2; BR-NUC-30, 32; NFR-NUC-29).

``POST /invitations/{token}/accept`` es pública (lista cerrada) y tiene dos pasos, que distingue
``step`` en el cuerpo:

- ``begin``: valida el enlace y devuelve el aviso de tratamiento vigente (versión y texto) y, si el
  rol exige segundo factor, la inscripción (QR, URI y códigos de recuperación, **una vez**).
- ``complete``: contraseña, versión del aviso aceptada y, si el rol lo exige, el primer TOTP. Activa
  la cuenta; la persona inicia sesión después con ``POST /auth/login``.

Un token inexistente, mal formado, usado, vencido o cancelado, de una cuenta que no está invitada o
de una organización suspendida responde **igual**: ``not_found`` (BR-NUC-30). El token viaja en la
ruta; los registros solo llevan la plantilla (``/invitations/{token}/accept``), nunca el valor.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.identity.adapters.http.services import (
    IdentityHttp,
    identity_error,
    identity_http,
    no_store,
)
from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.identity.application.privacy_notice import PrivacyNotice, current_notice
from vigia_platform.identity.auth.second_factor import AlreadyEnrolled, SecondFactorNotFound
from vigia_platform.shared.api.declarations import UnauthenticatedRoute, unauthenticated
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode

__all__ = ["invitations_router"]

_PASSWORD_MAX_CHARS: Final = 1024
"""La contraseña la juzga la política (BR-NUC-20), no el esquema."""

Services = Annotated[IdentityHttp, Depends(identity_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BeginActivation(_Strict):
    step: Literal["begin"]


class CompleteActivation(_Strict):
    step: Literal["complete"]
    password: StrictStr = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)
    notice_version: StrictStr = Field(min_length=1, max_length=32)
    second_factor_code: StrictStr | None = Field(default=None, min_length=1, max_length=16)


ActivationRequest = Annotated[BeginActivation | CompleteActivation, Field(discriminator="step")]


class NoticeView(_Strict):
    version: str
    text: str
    pending_legal_text: bool
    """El texto vigente es el marcador pendiente del abogado (P4)."""


class ActivationEnrollment(_Strict):
    provisioning_uri: str
    qr_svg: str
    recovery_codes: tuple[str, ...]


class ActivationResponse(_Strict):
    status: Literal["pending", "activated"]
    second_factor_required: bool | None = None
    enrollment: ActivationEnrollment | None = None
    notice: NoticeView | None = None


def _notice(notice: PrivacyNotice) -> NoticeView:
    return NoticeView(
        version=notice.version, text=notice.text, pending_legal_text=notice.pending_legal_text
    )


def invitations_router() -> APIRouter:
    router = APIRouter(tags=["invitación"])

    @router.post(
        "/invitations/{token}/accept",
        dependencies=[unauthenticated(UnauthenticatedRoute.INVITATION_ACCEPT)],
        summary="Activación de la cuenta con el enlace de invitación",
        response_model_exclude_none=True,
    )
    async def accept(
        token: str, body: ActivationRequest, response: Response, services: Services
    ) -> ActivationResponse:
        no_store(response)
        try:
            if isinstance(body, CompleteActivation):
                await services.invitations.accept_invitation(
                    token, body.password, body.notice_version, body.second_factor_code
                )
                return ActivationResponse(status="activated")
            start = await services.invitations.begin_activation(token)
        except IdentityRejected as error:
            raise identity_error(error) from None
        except AlreadyEnrolled:
            # El enlace es válido y el segundo factor ya quedó confirmado en un intento anterior
            # que no terminó: falta solo ``complete`` (sin código).
            return ActivationResponse(
                status="pending", second_factor_required=True, notice=_notice(current_notice())
            )
        except SecondFactorNotFound:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        enrollment = start.enrollment
        return ActivationResponse(
            status="pending",
            second_factor_required=start.second_factor_required,
            enrollment=None
            if enrollment is None
            else ActivationEnrollment(
                provisioning_uri=enrollment.provisioning_uri,
                qr_svg=enrollment.qr_svg,
                recovery_codes=enrollment.recovery_codes,
            ),
            notice=_notice(start.notice),
        )

    return router
