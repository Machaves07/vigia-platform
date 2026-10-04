"""Rutas de la prueba de admisión (SCR-04; interfaces-para-u04-u05 §3.1; LC-GOB-02).

- ``POST /plants/{plant_id}/admissions`` (``catalog.manage`` sobre la planta): cuerpo
  ``{family, answers: {standard, remedy, subject}, justification_es?}``. La admitida responde
  ``201`` con la evaluación. La rechazada **queda confirmada** (fila y ``standard_admission_test``
  con su ``failed_criterion``) y después responde ``invalid_request`` con ``detail_code``
  ``catalog_admission_rejected``: el cuerpo de ``ApiError`` es cerrado, así que el criterio
  fallido se consulta en el expediente y en el ``GET`` (decisión de TASK-207). Si la familia ya
  está admitida en la planta, ``conflict`` con ``catalog_family_already_admitted`` y nada se
  escribe. Una ``family`` fuera de la lista cerrada del contrato no pasa la validación del cuerpo
  (``invalid_request``, sin escribir); una justificación que no pasa la política,
  ``catalog_free_text_rejected``.
- ``GET /plants/{plant_id}/admissions`` (``catalog.read`` sobre la planta): evaluaciones con
  familia, respuestas, autor (``evaluated_by`` y ``role_in_use``), fecha, resultado y
  ``failed_criterion``, la más reciente primero, en páginas de hasta 200 (``after`` opaco).

Una planta inexistente, de otra organización o fuera del alcance responde ``not_found``.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr
from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.adapters.postgres.admission_repository import AdmissionCursor
from vigia_platform.catalog.application.admission import (
    MAX_PAGE_SIZE,
    AdmissionRequest,
    AdmissionWriteFailed,
    CatalogRejected,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.admission import AdmissionAnswers, FamilyAdmission
from vigia_platform.catalog.domain.enums import AdmissionCriterion, AdmissionResult
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["admissions_router", "decode_cursor", "encode_cursor"]

MAX_CURSOR_CHARS: Final = 128
_CURSOR: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MANAGE: Final = PermissionKey.CATALOG_MANAGE.value
_READ: Final = PermissionKey.CATALOG_READ.value
_POST_DETAIL_CODES: Final = (
    CatalogDetailCode.ADMISSION_REJECTED.value,
    CatalogDetailCode.FAMILY_ALREADY_ADMITTED.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnswersBody(_Strict):
    """Las tres preguntas (BR-GOB-13): estándar escrito, remedio de ingeniería o de proceso, y
    dato que pierde sentido al desagregarlo por persona."""

    standard: StrictBool
    remedy: StrictBool
    subject: StrictBool


class AdmissionBody(_Strict):
    family: PredicateFamily
    answers: AnswersBody
    justification_es: StrictStr | None = None


class AdmissionView(_Strict):
    """Una evaluación de la prueba, admitida o rechazada."""

    admission_id: uuid.UUID
    plant_id: uuid.UUID
    family: PredicateFamily
    answers: AnswersBody
    justification_es: str | None
    result: AdmissionResult
    failed_criterion: AdmissionCriterion | None
    evaluated_by: uuid.UUID
    role_in_use: Role
    evaluated_at: str
    ledger_record_id: uuid.UUID


class AdmissionsPage(_Strict):
    admissions: tuple[AdmissionView, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


def encode_cursor(cursor: AdmissionCursor) -> str:
    """Cursor opaco: la marca de la evaluación con microsegundos y su identificador."""
    raw = f"{cursor.evaluated_at.astimezone(UTC).isoformat()}|{cursor.admission_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> AdmissionCursor:
    """El cursor de ``encode_cursor``; cualquier otra cosa, ``invalid_request``."""
    if _CURSOR.fullmatch(value) is None:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        moment, _, identifier = raw.partition("|")
        return AdmissionCursor(datetime.fromisoformat(moment), uuid.UUID(identifier))
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None


def _view(admission: FamilyAdmission) -> AdmissionView:
    return AdmissionView(
        admission_id=admission.admission_id,
        plant_id=admission.plant_id,
        family=admission.family,
        answers=AnswersBody(**admission.answers.as_dict()),
        justification_es=admission.justification_es,
        result=admission.result,
        failed_criterion=admission.failed_criterion,
        evaluated_by=admission.evaluated_by,
        role_in_use=admission.role_in_use,
        evaluated_at=format_timestamp(admission.evaluated_at),
        ledger_record_id=admission.ledger_record_id,
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def admissions_router() -> APIRouter:
    router = APIRouter(tags=["catálogo"])

    @router.post(
        "/plants/{plant_id}/admissions",
        status_code=201,
        dependencies=[requires(_MANAGE, detail_codes=_POST_DETAIL_CODES)],
        summary="Prueba de admisión de tres preguntas de una familia en la planta",
    )
    async def evaluate(
        plant_id: uuid.UUID,
        body: AdmissionBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> AdmissionView:
        _no_store(response)
        service = installed(services.admissions)
        try:
            admission = await service.evaluate(
                request_context(request),
                plant_id,
                AdmissionRequest(
                    family=body.family,
                    answers=AdmissionAnswers(**body.answers.model_dump()),
                    justification_es=body.justification_es,
                ),
            )
        except CatalogRejected as error:
            raise ApiError(error.api_code, detail_code=error.detail_code.value) from None
        except AdmissionWriteFailed as error:
            raise from_ledger_rejection(error.rejection) from None
        if not admission.admitted:
            # Ya confirmada (BR-GOB-15): el rechazo se responde después de registrarlo.
            raise ApiError(
                ApiErrorCode.INVALID_REQUEST,
                detail_code=CatalogDetailCode.ADMISSION_REJECTED.value,
            )
        return _view(admission)

    @router.get(
        "/plants/{plant_id}/admissions",
        dependencies=[requires(_READ)],
        summary="Evaluaciones de la prueba de admisión de la planta, la más reciente primero",
    )
    async def list_admissions(
        plant_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=MAX_CURSOR_CHARS)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
    ) -> AdmissionsPage:
        _no_store(response)
        cursor = None if after is None else decode_cursor(after)
        page = await installed(services.admissions).list_admissions(
            request_context(request), plant_id, after=cursor, limit=limit
        )
        return AdmissionsPage(
            admissions=tuple(_view(item) for item in page.items),
            next_after=None if page.next_cursor is None else encode_cursor(page.next_cursor),
        )

    return router
