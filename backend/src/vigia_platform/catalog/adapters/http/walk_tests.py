"""Rutas de la sesión de walk-test (SCR-06; interfaces §3.3; LC-GOB-06).

- ``POST /zones/{zone_id}/walk-tests`` (``commissioning.run`` sobre la zona):
  ``{passes_per_cell}``. ``201`` con la sesión ``in_progress`` y su matriz derivada del catálogo;
  errores ``catalog_mounting_gate_pending``, ``catalog_node_not_assigned``,
  ``catalog_walk_test_in_progress`` y ``catalog_passes_below_minimum``, en ese orden.
- ``GET /zones/{zone_id}/walk-tests/current`` (``catalog.read`` sobre la zona): la sesión
  ``in_progress``, ``reopened`` o ``incomplete`` con matriz, pasos, pases y su conteo por fila,
  horas acumuladas (total y por tipo de paso, **nunca por responsable**) y pruebas de oclusión;
  ``session: null`` si la zona no tiene ninguna.
- ``POST /walk-tests/{session_id}/steps`` (``commissioning.run``): ``{step_kind,
  responsible_user_id}``. ``201`` con el paso abierto y ``started_at`` del servidor.
- ``POST /walk-tests/{session_id}/steps/{step_id}/close`` (``commissioning.run``):
  ``{correction?: {started_at?, ended_at?, reason_es}}``. ``200`` con el paso cerrado,
  ``ended_at`` del servidor y la corrección anexa (las marcas originales siguen visibles); un paso
  ya cerrado, ``conflict``.
- ``POST /walk-tests/{session_id}/passes`` (``commissioning.run``): ``{row_id, result,
  evidence_ref?}``. ``201``; un ``row_id`` ajeno a la matriz, ``invalid_request``.
- ``POST /walk-tests/{session_id}/reopen`` (``commissioning.run``): ``{reason_es}``. ``200`` con
  la sesión ``reopened``; solo desde ``incomplete`` (si no, ``conflict``).

Sobre una sesión ``incomplete``, ``catalog_walk_test_incomplete``; sobre una ``closed``,
``conflict``. Un recurso inexistente, de otra organización o fuera del alcance responde
``not_found``. Ninguna ruta acepta un filtro, orden ni parámetro de consulta (``exact_query``).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, StrictInt, StrictStr

from vigia_platform.catalog.adapters.http.catalog import Strict
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.walk_test import (
    WalkTestConflict,
    WalkTestRequestInvalid,
    WalkTestView,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.enums import (
    PassResult,
    Posture,
    StepKind,
    WalkTestKind,
    WalkTestStatus,
)
from vigia_platform.catalog.domain.steps import CorrectionRequest, StepCorrection, WalkTestStep
from vigia_platform.catalog.domain.walk_test import (
    PassCounts,
    SessionRow,
    WalkTestPass,
    predicate_condition,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["WalkTestSessionOut", "walk_test_session_view", "walk_tests_router"]

_READ: Final = PermissionKey.CATALOG_READ.value
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_OPEN_DETAIL_CODES: Final = (
    CatalogDetailCode.MOUNTING_GATE_PENDING.value,
    CatalogDetailCode.NODE_NOT_ASSIGNED.value,
    CatalogDetailCode.WALK_TEST_IN_PROGRESS.value,
    CatalogDetailCode.PASSES_BELOW_MINIMUM.value,
)
_SESSION_DETAIL_CODES: Final = (CatalogDetailCode.WALK_TEST_INCOMPLETE.value,)
_CLOSE_DETAIL_CODES: Final = (
    CatalogDetailCode.WALK_TEST_INCOMPLETE.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)
_REOPEN_DETAIL_CODES: Final = (
    CatalogDetailCode.WALK_TEST_IN_PROGRESS.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


# --- Cuerpos -----------------------------------------------------------------------------------


class OpenWalkTestBody(Strict):
    # Sin mínimo en la forma: menos de 3 es ``catalog_passes_below_minimum`` (interfaces §3.3).
    passes_per_cell: StrictInt


class StepBody(Strict):
    step_kind: StepKind
    responsible_user_id: uuid.UUID


class CorrectionBody(Strict):
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    reason_es: StrictStr


class CloseStepBody(Strict):
    correction: CorrectionBody | None = None


class PassBody(Strict):
    row_id: uuid.UUID
    result: PassResult
    evidence_ref: uuid.UUID | None = None


class ReopenBody(Strict):
    reason_es: StrictStr


# --- Respuestas --------------------------------------------------------------------------------


class PassCountsOut(Strict):
    detected: int
    missed: int
    false_alarm: int


class MatrixRowOut(Strict):
    """Una fila de ``matrix_rows`` con el conteo de sus pases."""

    row_id: uuid.UUID
    standard_id: uuid.UUID
    standard_version: int
    predicate_conditions: list[dict[str, Any]]
    posture: Posture
    required_passes: int
    passes: PassCountsOut


class CorrectionOut(Strict):
    started_at: str | None
    ended_at: str | None
    reason_es: str
    corrected_by: uuid.UUID
    corrected_at: str


class StepOut(Strict):
    """Un paso con sus marcas del servidor y, aparte, su corrección (BR-GOB-45)."""

    step_id: uuid.UUID
    session_id: uuid.UUID
    step_kind: StepKind
    responsible_user_id: uuid.UUID
    started_at: str
    ended_at: str | None
    correction: CorrectionOut | None
    effective_duration_ms: int | None


class PassOut(Strict):
    pass_id: uuid.UUID
    session_id: uuid.UUID
    row_id: uuid.UUID
    result: PassResult
    evidence_ref: uuid.UUID | None
    recorded_by: uuid.UUID
    recorded_at: str


class StepKindDurationOut(Strict):
    """Duración de un tipo de paso, sin responsable (H-53)."""

    step_kind: StepKind
    duration_ms: int


class WalkTestSessionOut(Strict):
    """``WalkTestSession`` (DE §2.11) con lo registrado en ella."""

    session_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID
    catalog_version: int
    kind: WalkTestKind
    status: WalkTestStatus
    passes_per_cell: int
    rows: list[MatrixRowOut]
    started_at: str
    last_activity_at: str
    closed_at: str | None
    reopened_at: str | None
    reopened_by: uuid.UUID | None
    reopen_reason_es: str | None
    steps: list[StepOut]
    passes: list[PassOut]
    total_duration_ms: int
    """Suma exacta de las duraciones efectivas de los pasos cerrados (``total_hours``)."""
    steps_summary: list[StepKindDurationOut]
    occlusion_tests: list[dict[str, Any]]


class CurrentWalkTestOut(Strict):
    zone_id: uuid.UUID
    session: WalkTestSessionOut | None


def _optional_stamp(value: Any) -> str | None:
    return None if value is None else format_timestamp(value)


def _counts(counts: PassCounts) -> PassCountsOut:
    return PassCountsOut(
        detected=counts.detected, missed=counts.missed, false_alarm=counts.false_alarm
    )


def _row(row: SessionRow, counts: PassCounts) -> MatrixRowOut:
    return MatrixRowOut(
        row_id=row.row_id,
        standard_id=row.standard_id,
        standard_version=row.standard_version,
        predicate_conditions=[predicate_condition(c) for c in row.conditions],
        posture=row.posture,
        required_passes=row.required_passes,
        passes=_counts(counts),
    )


def _correction(correction: StepCorrection | None) -> CorrectionOut | None:
    if correction is None:
        return None
    return CorrectionOut(
        started_at=_optional_stamp(correction.started_at),
        ended_at=_optional_stamp(correction.ended_at),
        reason_es=correction.reason_es,
        corrected_by=correction.corrected_by,
        corrected_at=format_timestamp(correction.corrected_at),
    )


def step_view(step: WalkTestStep) -> StepOut:
    return StepOut(
        step_id=step.step_id,
        session_id=step.session_id,
        step_kind=step.step_kind,
        responsible_user_id=step.responsible_user_id,
        started_at=format_timestamp(step.started_at),
        ended_at=_optional_stamp(step.ended_at),
        correction=_correction(step.correction),
        effective_duration_ms=step.effective_duration_ms,
    )


def pass_view(recorded: WalkTestPass) -> PassOut:
    return PassOut(
        pass_id=recorded.pass_id,
        session_id=recorded.session_id,
        row_id=recorded.row_id,
        result=recorded.result,
        evidence_ref=recorded.evidence_ref,
        recorded_by=recorded.recorded_by,
        recorded_at=format_timestamp(recorded.recorded_at),
    )


def walk_test_session_view(view: WalkTestView) -> WalkTestSessionOut:
    session = view.session
    return WalkTestSessionOut(
        session_id=session.session_id,
        zone_id=session.zone_id,
        node_id=session.node_id,
        catalog_version=session.catalog_version,
        kind=session.kind,
        status=session.status,
        passes_per_cell=session.passes_per_cell,
        rows=[_row(row, view.counts[row.row_id]) for row in session.matrix_rows],
        started_at=format_timestamp(session.started_at),
        last_activity_at=format_timestamp(session.last_activity_at),
        closed_at=_optional_stamp(session.closed_at),
        reopened_at=_optional_stamp(session.reopened_at),
        reopened_by=session.reopened_by,
        reopen_reason_es=session.reopen_reason_es,
        steps=[step_view(step) for step in view.steps],
        passes=[pass_view(recorded) for recorded in view.passes],
        total_duration_ms=view.total_duration_ms,
        steps_summary=[
            StepKindDurationOut(step_kind=hours.step_kind, duration_ms=hours.duration_ms)
            for hours in view.summary
        ],
        occlusion_tests=[dict(test) for test in view.occlusion_tests],
    )


def _rejected(error: CatalogRejected) -> ApiError:
    return ApiError(error.api_code, detail_code=error.detail_code.value)


def walk_tests_router() -> APIRouter:
    router = APIRouter(tags=["comisionamiento"])

    @router.post(
        "/zones/{zone_id}/walk-tests",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_OPEN_DETAIL_CODES), Depends(exact_query())],
        summary="Abre la sesión de walk-test de la zona con la matriz derivada del catálogo",
    )
    async def open_walk_test(
        zone_id: uuid.UUID,
        body: OpenWalkTestBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> WalkTestSessionOut:
        no_store(response)
        try:
            session = await installed(services.walk_tests).open(
                request_context(request), zone_id, body.passes_per_cell
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return walk_test_session_view(WalkTestView.of(session))

    @router.get(
        "/zones/{zone_id}/walk-tests/current",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Sesión de walk-test en curso, incompleta o reabierta de la zona",
    )
    async def current_walk_test(
        zone_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> CurrentWalkTestOut:
        no_store(response)
        view = await installed(services.walk_tests).current(request_context(request), zone_id)
        return CurrentWalkTestOut(
            zone_id=zone_id, session=None if view is None else walk_test_session_view(view)
        )

    @router.post(
        "/walk-tests/{session_id}/steps",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_SESSION_DETAIL_CODES), Depends(exact_query())],
        summary="Abre un paso cronometrado por el servidor",
    )
    async def start_step(
        session_id: uuid.UUID,
        body: StepBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> StepOut:
        no_store(response)
        try:
            step = await installed(services.walk_tests).start_step(
                request_context(request), session_id, body.step_kind, body.responsible_user_id
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return step_view(step)

    @router.post(
        "/walk-tests/{session_id}/steps/{step_id}/close",
        dependencies=[requires(_RUN, detail_codes=_CLOSE_DETAIL_CODES), Depends(exact_query())],
        summary="Cierra el paso con la hora del servidor y su corrección anexa, si la hay",
    )
    async def close_step(
        session_id: uuid.UUID,
        step_id: uuid.UUID,
        body: CloseStepBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> StepOut:
        no_store(response)
        correction = body.correction
        try:
            step = await installed(services.walk_tests).close_step(
                request_context(request),
                session_id,
                step_id,
                None
                if correction is None
                else CorrectionRequest(
                    reason_es=correction.reason_es,
                    started_at=correction.started_at,
                    ended_at=correction.ended_at,
                ),
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return step_view(step)

    @router.post(
        "/walk-tests/{session_id}/passes",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_SESSION_DETAIL_CODES), Depends(exact_query())],
        summary="Registra un pase de una fila de la matriz (solo anexar)",
    )
    async def record_pass(
        session_id: uuid.UUID,
        body: PassBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> PassOut:
        no_store(response)
        try:
            recorded = await installed(services.walk_tests).record_pass(
                request_context(request), session_id, body.row_id, body.result, body.evidence_ref
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return pass_view(recorded)

    @router.post(
        "/walk-tests/{session_id}/reopen",
        dependencies=[requires(_RUN, detail_codes=_REOPEN_DETAIL_CODES), Depends(exact_query())],
        summary="Reabre con motivo una sesión incompleta por inactividad",
    )
    async def reopen_walk_test(
        session_id: uuid.UUID,
        body: ReopenBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> WalkTestSessionOut:
        no_store(response)
        try:
            view = await installed(services.walk_tests).reopen(
                request_context(request), session_id, body.reason_es
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return walk_test_session_view(view)

    return router
