"""Rutas de la política de hallazgos incerrables de la planta (SCR-05; interfaces §1.2 y §3.2).

- ``POST /plants/{plant_id}/policy`` (``commissioning.run`` sobre la planta): ``{version,
  signed_at, signed_by_display_name (≤ 120), legal_opinion_reference (≤ 120), criteria_summary_es
  (≤ 2 000), document_ref}``. El ``document_ref`` es obligatorio, de ``kind = plant_policy``, y se
  verifica contra su concesión y los metadatos del objeto. La versión la fija el servidor como la
  anterior más uno: un ``version`` distinto responde ``conflict`` (sin ``detail_code``) y no
  escribe nada. ``201`` con la política cargada; la anterior permanece.
- ``GET /plants/{plant_id}/policy`` (``catalog.read`` sobre la planta): ``{loaded, policy_id?,
  version?, signed_at?, signed_by_display_name?, legal_opinion_reference?, document_sha256?,
  criteria_summary_es?}``; ``loaded = false`` en una planta sin política.

Una planta inexistente, de otra organización o fuera del alcance responde ``not_found``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictInt, StrictStr

from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.plant_policy import (
    PolicyRequestInvalid,
    PolicyVersionConflict,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.documents import DocumentRequestInvalid
from vigia_platform.catalog.domain.plant_policy import PlantPolicy, PlantPolicyRequest
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StorageUnavailable

__all__ = ["PlantPolicyOut", "plant_policy_router"]

_READ: Final = PermissionKey.CATALOG_READ.value
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_POST_DETAIL_CODES: Final = (CatalogDetailCode.FREE_TEXT_REJECTED.value,)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlantPolicyBody(_Strict):
    version: StrictInt
    signed_at: AwareDatetime
    signed_by_display_name: StrictStr
    legal_opinion_reference: StrictStr
    criteria_summary_es: StrictStr
    document_ref: dict[str, Any]


class PlantPolicyOut(_Strict):
    """``PlantPolicy`` de ``GateQueryPort.plant_policy`` (interfaces §1.2)."""

    loaded: bool
    policy_id: uuid.UUID | None = None
    version: int | None = None
    signed_at: str | None = None
    signed_by_display_name: str | None = None
    legal_opinion_reference: str | None = None
    document_sha256: str | None = None
    criteria_summary_es: str | None = None


def policy_view(policy: PlantPolicy | None) -> PlantPolicyOut:
    if policy is None:
        return PlantPolicyOut(loaded=False)
    return PlantPolicyOut(
        loaded=True,
        policy_id=policy.policy_id,
        version=policy.version,
        signed_at=format_timestamp(policy.signed_at),
        signed_by_display_name=policy.signed_by_display_name,
        legal_opinion_reference=policy.legal_opinion_reference,
        document_sha256=policy.document_ref.sha256,
        criteria_summary_es=policy.criteria_summary_es,
    )


def plant_policy_router() -> APIRouter:
    router = APIRouter(tags=["compuertas"])

    @router.post(
        "/plants/{plant_id}/policy",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_POST_DETAIL_CODES), Depends(exact_query())],
        summary="Carga la versión siguiente de la política de hallazgos incerrables de la planta",
    )
    async def sign_policy(
        plant_id: uuid.UUID,
        body: PlantPolicyBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> PlantPolicyOut:
        no_store(response)
        try:
            policy = await installed(services.plant_policies).sign_policy(
                request_context(request),
                plant_id,
                PlantPolicyRequest(
                    version=body.version,
                    signed_at=body.signed_at,
                    signed_by_display_name=body.signed_by_display_name,
                    legal_opinion_reference=body.legal_opinion_reference,
                    criteria_summary_es=body.criteria_summary_es,
                    document_ref=body.document_ref,
                ),
            )
        except CatalogRejected as error:
            raise ApiError(error.api_code, detail_code=error.detail_code.value) from None
        except (PolicyRequestInvalid, DocumentRequestInvalid):
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except PolicyVersionConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        except StorageUnavailable as error:
            raise ApiError(
                ApiErrorCode.STORAGE_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds
            ) from None
        return policy_view(policy)

    @router.get(
        "/plants/{plant_id}/policy",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Política de hallazgos incerrables vigente de la planta (loaded = false si no hay)",
    )
    async def plant_policy(
        plant_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> PlantPolicyOut:
        no_store(response)
        policy = await installed(services.plant_policies).policy(request_context(request), plant_id)
        return policy_view(policy)

    return router
