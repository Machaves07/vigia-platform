"""Rutas de la política de firmantes y del acuerdo de uso (SCR-05; interfaces §3.2; LC-GOB-04).

- ``PUT /plants/{plant_id}/signatory-policy`` (``commissioning.run`` sobre la planta):
  ``{required_roles: [role], minimum}``. ``200`` con la política; ``minimum < 3``,
  ``catalog_fewer_than_three``; sin ``copasst``, ``catalog_workers_representation_missing``.
- ``GET /plants/{plant_id}/signatory-policy`` (``catalog.read`` sobre la planta): la política, con
  ``configured = false`` en una planta sin ella.
- ``POST /zones/{zone_id}/use-agreements`` (``commissioning.run`` sobre la zona):
  ``{signatories[{role, user_id}], document_ref?, replaces_agreement_id?}``. ``201`` con el acuerdo
  en ``pending_signatures``; errores ``catalog_signatory_role_not_in_policy``,
  ``catalog_signatory_user_role_mismatch``, ``catalog_workers_representation_missing``,
  ``catalog_fewer_than_three`` y ``catalog_agreement_reused_from_other_zone``.
- ``POST /use-agreements/{agreement_id}/confirmations`` (sin cuerpo): **se declara con
  ``transparency.read``**, que tienen todos los roles firmantes; el servicio exige además
  ``agreements.sign`` a los firmantes de gestión (decisión del redactor de TASK-212). ``201`` con la
  confirmación del usuario de la sesión (``200`` si ya había confirmado); quien no es firmante
  esperado, ``catalog_signatory_not_expected``; bajo concesión de proveedor, ``not_found``.
- ``POST /use-agreements/{agreement_id}/approval`` (``commissioning.run`` sobre la zona del
  acuerdo): ``200`` con el acuerdo y las compuertas; la primera guarda de BR-GOB-29 que falta:
  ``catalog_mounting_gate_pending``, ``catalog_commissioning_record_missing``,
  ``catalog_signatures_incomplete`` o ``catalog_plant_policy_missing``. Aprobar otra vez devuelve
  el estado actual; un acuerdo ``superseded`` o ``revoked``, ``conflict`` sin ``detail_code``.

Un recurso inexistente, de otra organización o fuera del alcance responde ``not_found``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from vigia_platform.catalog.adapters.http.gates import GateStateOut, state_view
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.agreements import (
    AgreementConflict,
    AgreementRequest,
    AgreementRequestInvalid,
)
from vigia_platform.catalog.application.signatory_policy import SignatoryPolicyInvalid
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.agreements import (
    MAX_SIGNATORIES,
    WORKERS_ROLE,
    AgreementConfirmation,
    SignatoryPolicy,
    UseAgreement,
)
from vigia_platform.catalog.domain.documents import DocumentRequestInvalid
from vigia_platform.catalog.domain.enums import AgreementStatus, ConfirmationOrigin
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StorageUnavailable

__all__ = ["AgreementOut", "agreement_view", "agreements_router"]

_READ: Final = PermissionKey.CATALOG_READ.value
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_TRANSPARENCY: Final = PermissionKey.TRANSPARENCY_READ.value
_POLICY_DETAIL_CODES: Final = (
    CatalogDetailCode.FEWER_THAN_THREE.value,
    CatalogDetailCode.WORKERS_REPRESENTATION_MISSING.value,
)
_CREATE_DETAIL_CODES: Final = (
    CatalogDetailCode.SIGNATORY_ROLE_NOT_IN_POLICY.value,
    CatalogDetailCode.SIGNATORY_USER_ROLE_MISMATCH.value,
    CatalogDetailCode.WORKERS_REPRESENTATION_MISSING.value,
    CatalogDetailCode.FEWER_THAN_THREE.value,
    CatalogDetailCode.AGREEMENT_REUSED_FROM_OTHER_ZONE.value,
)
_CONFIRM_DETAIL_CODES: Final = (CatalogDetailCode.SIGNATORY_NOT_EXPECTED.value,)
_APPROVAL_DETAIL_CODES: Final = (
    CatalogDetailCode.MOUNTING_GATE_PENDING.value,
    CatalogDetailCode.COMMISSIONING_RECORD_MISSING.value,
    CatalogDetailCode.SIGNATURES_INCOMPLETE.value,
    CatalogDetailCode.PLANT_POLICY_MISSING.value,
)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Cuerpos -----------------------------------------------------------------------------------


class SignatoryPolicyBody(_Strict):
    required_roles: list[Role] = Field(max_length=len(Role))
    minimum: StrictInt


class SignatoryBody(_Strict):
    role: Role
    user_id: uuid.UUID


class UseAgreementBody(_Strict):
    # Sin mínimo en la forma: menos de los exigidos es ``catalog_fewer_than_three``.
    signatories: list[SignatoryBody] = Field(max_length=MAX_SIGNATORIES)
    document_ref: dict[str, Any] | None = None
    replaces_agreement_id: uuid.UUID | None = None


# --- Respuestas --------------------------------------------------------------------------------


class SignatoryPolicyOut(_Strict):
    """``PlantSignatoryPolicy`` (DE §2.7); ``configured = false`` en una planta sin ella."""

    plant_id: uuid.UUID
    configured: bool
    required_roles: list[Role] | None = None
    minimum: int | None = None
    workers_role: Role = WORKERS_ROLE
    updated_by: uuid.UUID | None = None
    updated_at: str | None = None


class SignatoryOut(_Strict):
    role: Role
    user_id: uuid.UUID
    display_name: str | None
    confirmed_at: str | None


class AgreementOut(_Strict):
    """``UseAgreement`` (DE §2.8; la forma de ``current_agreement`` de interfaces §1.2)."""

    agreement_id: uuid.UUID
    zone_id: uuid.UUID
    status: AgreementStatus
    signatories: list[SignatoryOut]
    document_sha256: str | None
    replaces_agreement_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: str
    approved_at: str | None
    approved_by: uuid.UUID | None


class ConfirmationOut(_Strict):
    agreement_id: uuid.UUID
    user_id: uuid.UUID
    role_in_use: Role
    confirmed_at: str
    origin: ConfirmationOrigin


class ApprovalOut(_Strict):
    agreement: AgreementOut
    gates: GateStateOut


def _policy_view(plant_id: uuid.UUID, policy: SignatoryPolicy | None) -> SignatoryPolicyOut:
    if policy is None:
        return SignatoryPolicyOut(plant_id=plant_id, configured=False)
    return SignatoryPolicyOut(
        plant_id=plant_id,
        configured=True,
        required_roles=list(policy.required_roles),
        minimum=policy.minimum,
        updated_by=policy.updated_by,
        updated_at=format_timestamp(policy.updated_at),
    )


def agreement_view(
    agreement: UseAgreement, confirmations: tuple[AgreementConfirmation, ...] = ()
) -> AgreementOut:
    confirmed = {c.user_id: c.confirmed_at for c in confirmations}
    approved_at = agreement.approved_at
    return AgreementOut(
        agreement_id=agreement.agreement_id,
        zone_id=agreement.zone_id,
        status=agreement.status,
        signatories=[
            SignatoryOut(
                role=s.role,
                user_id=s.user_id,
                display_name=s.display_name,
                confirmed_at=(
                    None if s.user_id not in confirmed else format_timestamp(confirmed[s.user_id])
                ),
            )
            for s in agreement.signatories
        ],
        document_sha256=None if agreement.document_ref is None else agreement.document_ref.sha256,
        replaces_agreement_id=agreement.replaces_agreement_id,
        created_by=agreement.created_by,
        created_at=format_timestamp(agreement.created_at),
        approved_at=None if approved_at is None else format_timestamp(approved_at),
        approved_by=agreement.approved_by,
    )


def _rejected(error: CatalogRejected) -> ApiError:
    return ApiError(error.api_code, detail_code=error.detail_code.value)


def agreements_router() -> APIRouter:
    router = APIRouter(tags=["acuerdos"])

    @router.put(
        "/plants/{plant_id}/signatory-policy",
        dependencies=[requires(_RUN, detail_codes=_POLICY_DETAIL_CODES), Depends(exact_query())],
        summary="Fija la política de firmantes del acuerdo de uso de la planta",
    )
    async def put_signatory_policy(
        plant_id: uuid.UUID,
        body: SignatoryPolicyBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> SignatoryPolicyOut:
        no_store(response)
        try:
            policy = await installed(services.signatory_policies).put_policy(
                request_context(request), plant_id, body.required_roles, body.minimum
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except SignatoryPolicyInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return _policy_view(plant_id, policy)

    @router.get(
        "/plants/{plant_id}/signatory-policy",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Política de firmantes de la planta (configured = false si no hay)",
    )
    async def signatory_policy(
        plant_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> SignatoryPolicyOut:
        no_store(response)
        policy = await installed(services.signatory_policies).policy(
            request_context(request), plant_id
        )
        return _policy_view(plant_id, policy)

    @router.post(
        "/zones/{zone_id}/use-agreements",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_CREATE_DETAIL_CODES), Depends(exact_query())],
        summary="Registra el acuerdo de uso de la zona con sus firmantes esperados",
    )
    async def create_agreement(
        zone_id: uuid.UUID,
        body: UseAgreementBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> AgreementOut:
        no_store(response)
        try:
            agreement = await installed(services.agreements).create(
                request_context(request),
                zone_id,
                AgreementRequest(
                    signatories=[(s.role, s.user_id) for s in body.signatories],
                    document_ref=body.document_ref,
                    replaces_agreement_id=body.replaces_agreement_id,
                ),
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except (AgreementRequestInvalid, DocumentRequestInvalid):
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except AgreementConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        except StorageUnavailable as error:
            raise ApiError(
                ApiErrorCode.STORAGE_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds
            ) from None
        return agreement_view(agreement)

    @router.post(
        "/use-agreements/{agreement_id}/confirmations",
        status_code=201,
        dependencies=[
            requires(_TRANSPARENCY, detail_codes=_CONFIRM_DETAIL_CODES),
            Depends(exact_query()),
        ],
        summary="Confirma la firma del usuario de la sesión en el acuerdo de uso",
    )
    async def confirm_agreement(
        agreement_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> ConfirmationOut:
        no_store(response)
        try:
            result = await installed(services.agreements).confirm(
                request_context(request), agreement_id
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except AgreementConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        if not result.created:
            response.status_code = 200
        confirmation = result.confirmation
        return ConfirmationOut(
            agreement_id=confirmation.agreement_id,
            user_id=confirmation.user_id,
            role_in_use=confirmation.role_in_use,
            confirmed_at=format_timestamp(confirmation.confirmed_at),
            origin=confirmation.origin,
        )

    @router.post(
        "/use-agreements/{agreement_id}/approval",
        dependencies=[requires(_RUN, detail_codes=_APPROVAL_DETAIL_CODES), Depends(exact_query())],
        summary="Aprueba el acuerdo de uso: la compuerta de uso queda aprobada",
    )
    async def approve_agreement(
        agreement_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> ApprovalOut:
        no_store(response)
        try:
            approval = await installed(services.agreements).approve(
                request_context(request), agreement_id
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except AgreementConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return ApprovalOut(
            agreement=agreement_view(approval.agreement, approval.confirmations),
            gates=state_view(approval.state),
        )

    return router
