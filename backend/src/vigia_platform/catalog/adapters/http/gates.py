"""Rutas de las compuertas de una zona (SCR-05; interfaces-para-u04-u05 §3.2 y v1.5; LC-GOB-03).

- ``GET /zones/{zone_id}/gates`` (``catalog.read`` sobre la zona): la ``ZoneGateState`` con
  ``mounting {status, decided_at?, record_id?, decided_by?}``, ``usage {status, decided_at?,
  agreement_id?, decided_by?}``, ``resulting_mode`` e ``issued_at`` (nulo si la zona nunca cambió:
  entonces las dos compuertas están ``pending``).
- ``POST /zones/{zone_id}/gates/mounting/scope-record`` (``commissioning.run`` sobre la zona):
  acta de alcance ``{scope_text_es, cameras[{camera_id, framing_description_es,
  reference_marker}], blur_verification: {declared, capture_document_ref}, document_ref?}``; ya no
  exige clip (v1.5, D-2). ``201`` con el acta y el estado nuevo. Sin la declaración con captura,
  ``conflict`` con ``catalog_blur_not_verified``; zona sin nodo asignado, ``conflict`` con
  ``catalog_node_not_assigned``; encuadres que no son las cámaras de la zona o un documento que no
  coincide con su concesión, ``invalid_request``; texto que no pasa la política,
  ``catalog_free_text_rejected``.
- ``POST /zones/{zone_id}/gates/{gate}/revocation`` (``commissioning.run`` sobre la zona):
  ``{reason_es}`` obligatorio (10 a 500). ``200`` con el estado nuevo; una compuerta que no está
  ``approved``, ``conflict`` sin ``detail_code``.

Una zona inexistente, de otra organización o fuera del alcance responde ``not_found``. Con la firma
caída, ``temporarily_unavailable`` y nada escrito; con el almacén de documentos caído,
``storage_unavailable``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from vigia_contracts.models.enumerations import GateStatus, ZoneMode

from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.gates import GateConflict, GateRequestInvalid
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.documents import DocumentRequestInvalid
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateDecision, ZoneGateState
from vigia_platform.catalog.domain.scope_record import (
    MAX_CAMERAS,
    CameraFraming,
    ScopeRecordInvalid,
    ScopeRecordRequest,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import StorageUnavailable

__all__ = ["GateStateOut", "gates_router", "state_view"]

_READ: Final = PermissionKey.CATALOG_READ.value
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_SCOPE_RECORD_DETAIL_CODES: Final = (
    CatalogDetailCode.BLUR_NOT_VERIFIED.value,
    CatalogDetailCode.NODE_NOT_ASSIGNED.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)
_REVOCATION_DETAIL_CODES: Final = (CatalogDetailCode.FREE_TEXT_REJECTED.value,)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Cuerpos -----------------------------------------------------------------------------------


class CameraFramingBody(_Strict):
    camera_id: uuid.UUID
    framing_description_es: StrictStr
    reference_marker: StrictBool


class BlurVerificationBody(_Strict):
    """Parte declarada del difuminado (D-2). Los dos campos son opcionales en la forma para que
    su ausencia responda ``catalog_blur_not_verified``, no ``invalid_request``."""

    declared: StrictBool | None = None
    capture_document_ref: dict[str, Any] | None = None


class ScopeRecordBody(_Strict):
    scope_text_es: StrictStr
    cameras: list[CameraFramingBody] = Field(min_length=1, max_length=MAX_CAMERAS)
    blur_verification: BlurVerificationBody | None = None
    document_ref: dict[str, Any] | None = None


class RevocationBody(_Strict):
    reason_es: StrictStr


# --- Respuestas --------------------------------------------------------------------------------


class MountingGateOut(_Strict):
    status: GateStatus
    decided_at: str | None
    record_id: uuid.UUID | None
    decided_by: uuid.UUID | None


class UsageGateOut(_Strict):
    status: GateStatus
    decided_at: str | None
    agreement_id: uuid.UUID | None
    decided_by: uuid.UUID | None


class GateStateOut(_Strict):
    """``ZoneGateState`` (§2.4): la proyección, sin el sobre (que viaja al nodo por el latido)."""

    zone_id: uuid.UUID
    plant_id: uuid.UUID
    mounting: MountingGateOut
    usage: UsageGateOut
    resulting_mode: ZoneMode
    issued_at: str | None


class ScopeRecordOut(_Strict):
    record_id: uuid.UUID
    plant_policy_loaded_at_signing: bool
    gates: GateStateOut


def _stamp(decision: GateDecision) -> str | None:
    return None if decision.decided_at is None else format_timestamp(decision.decided_at)


def state_view(state: ZoneGateState) -> GateStateOut:
    mounting, usage = state.mounting, state.usage
    return GateStateOut(
        zone_id=state.zone_id,
        plant_id=state.plant_id,
        mounting=MountingGateOut(
            status=mounting.status,
            decided_at=_stamp(mounting),
            record_id=mounting.record_id,
            decided_by=mounting.decided_by,
        ),
        usage=UsageGateOut(
            status=usage.status,
            decided_at=_stamp(usage),
            agreement_id=usage.record_id,
            decided_by=usage.decided_by,
        ),
        resulting_mode=state.resulting_mode,
        issued_at=None if state.issued_at is None else format_timestamp(state.issued_at),
    )


def _rejected(error: CatalogRejected) -> ApiError:
    return ApiError(error.api_code, detail_code=error.detail_code.value)


def gates_router() -> APIRouter:
    router = APIRouter(tags=["compuertas"])

    @router.get(
        "/zones/{zone_id}/gates",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Estado de las compuertas de montaje y de uso de la zona y su modo resultante",
    )
    async def gate_state(
        zone_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> GateStateOut:
        no_store(response)
        state = await installed(services.gates).gate_state(request_context(request), zone_id)
        return state_view(state)

    @router.post(
        "/zones/{zone_id}/gates/mounting/scope-record",
        status_code=201,
        dependencies=[
            requires(_RUN, detail_codes=_SCOPE_RECORD_DETAIL_CODES),
            Depends(exact_query()),
        ],
        summary="Acta de alcance: aprueba la compuerta de montaje (la zona pasa a comisionamiento)",
    )
    async def scope_record(
        zone_id: uuid.UUID,
        body: ScopeRecordBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ScopeRecordOut:
        no_store(response)
        blur = body.blur_verification
        try:
            filed = await installed(services.scope_records).file_scope_record(
                request_context(request),
                zone_id,
                ScopeRecordRequest(
                    scope_text_es=body.scope_text_es,
                    cameras=tuple(
                        CameraFraming(
                            camera_id=camera.camera_id,
                            framing_description_es=camera.framing_description_es,
                            reference_marker=camera.reference_marker,
                        )
                        for camera in body.cameras
                    ),
                    blur_declared=None if blur is None else blur.declared,
                    capture_document_ref=None if blur is None else blur.capture_document_ref,
                    document_ref=body.document_ref,
                ),
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except (ScopeRecordInvalid, DocumentRequestInvalid):
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except StorageUnavailable as error:
            raise ApiError(
                ApiErrorCode.STORAGE_UNAVAILABLE, retry_after_seconds=error.retry_after_seconds
            ) from None
        return ScopeRecordOut(
            record_id=filed.record.record_id,
            plant_policy_loaded_at_signing=filed.record.plant_policy_loaded_at_signing,
            gates=state_view(filed.transition.state),
        )

    @router.post(
        "/zones/{zone_id}/gates/{gate}/revocation",
        dependencies=[
            requires(_RUN, detail_codes=_REVOCATION_DETAIL_CODES),
            Depends(exact_query()),
        ],
        summary="Revoca la compuerta de montaje o de uso con motivo; lo ya escrito permanece",
    )
    async def revocation(
        zone_id: uuid.UUID,
        gate: GateKind,
        body: RevocationBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> GateStateOut:
        no_store(response)
        try:
            transition = await installed(services.gates).revoke(
                request_context(request), zone_id, gate, body.reason_es
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except GateConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        except GateRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return state_view(transition.state)

    return router
