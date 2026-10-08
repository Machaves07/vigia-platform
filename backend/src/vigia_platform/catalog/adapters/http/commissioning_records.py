"""Rutas del acta de comisionamiento y de la reejecución (SCR-06; interfaces §3.3 y v1.5).

- ``POST /zones/{zone_id}/walk-tests/regression-rerun`` (``commissioning.run`` sobre la zona):
  ``{passes_per_cell}``. ``201`` con la sesión ``kind = regression_rerun`` y solo las filas
  afectadas (o todas); la regresión que no está ``pending`` es ``conflict`` sin ``detail_code``;
  después, los errores de la apertura (``catalog_mounting_gate_pending``,
  ``catalog_node_not_assigned``, ``catalog_walk_test_in_progress``,
  ``catalog_passes_below_minimum``). La zona sigue ``productive``.
- ``POST /walk-tests/{session_id}/close`` (``commissioning.run``): ``{signatures[{user_id}],
  false_alarm_acceptance?: {reason_es}, installer_measurements: {beacon_latency_ms_p95,
  baselines[{camera_id, zone_id, captured_at}]}}``. ``200`` con el acta estructurada; la primera
  guarda que falla: ``catalog_steps_still_open``, ``catalog_matrix_incomplete``,
  ``catalog_false_negative_present``, ``catalog_redundancy_not_verified``,
  ``catalog_false_alarm_rate_above_threshold``, ``catalog_latency_not_measured`` o
  ``catalog_blur_not_verified``; el almacén caído, ``storage_unavailable`` (transitorio); una
  sesión ya cerrada, ``conflict``.
- ``POST /walk-tests/{session_id}/exposure-samples`` (``commissioning.run``): ``{pass_id,
  fetched_at, displayed_at}`` con el reloj del navegador. ``201`` con la muestra nueva y ``200``
  con la primera si el pase ya tenía una (sin efecto); un pase ajeno a la sesión,
  ``catalog_pass_not_found``.
- ``GET /commissioning-records/{record_id}`` (``catalog.read`` sobre la zona del acta): el acta
  estructurada completa, nunca un responsable (H-53).
- ``GET /commissioning-records/{record_id}/document`` (``catalog.read`` sobre la zona del acta,
  TASK-217): el documento legible en ``application/pdf``, generado a demanda desde **la misma
  vista** que la ruta anterior y nunca almacenado (BR-GOB-50, NFR-GOB-69; LC-GOB-08). La lectura
  es la de ``CloseRecordService.record`` (alcance de zona y auditoría ``catalog_read`` bajo
  concesión, A-56). Si la generación no termina en 10 s, ``temporarily_unavailable`` con
  ``retry_after_seconds`` y sin cuerpo parcial (PAT-GOB-REN-06).

Un recurso inexistente, de otra organización o fuera del alcance responde ``not_found``. Ninguna
ruta acepta un filtro, orden ni parámetro de consulta (``exact_query``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, Field, StrictInt, StrictStr

from vigia_platform.catalog.adapters.http.catalog import Strict
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.adapters.http.walk_tests import (
    StepKindDurationOut,
    WalkTestSessionOut,
    walk_test_session_view,
)
from vigia_platform.catalog.adapters.postgres.commissioning_record_repository import (
    StoredRecord,
)
from vigia_platform.catalog.adapters.rendering import (
    DOCUMENT_RETRY_AFTER_SECONDS,
    DocumentTimedOut,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.close_record import Baseline, CloseRequest
from vigia_platform.catalog.application.walk_test import (
    WalkTestConflict,
    WalkTestRequestInvalid,
    WalkTestView,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.commissioning_record import CommissioningRecord
from vigia_platform.catalog.domain.enums import OcclusionVerification, WalkTestKind
from vigia_platform.catalog.domain.latency import ExposureSample
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "PDF_MEDIA_TYPE",
    "CommissioningRecordOut",
    "commissioning_records_router",
    "record_view",
]

_READ: Final = PermissionKey.CATALOG_READ.value
PDF_MEDIA_TYPE: Final = "application/pdf"
_RUN: Final = PermissionKey.COMMISSIONING_RUN.value
_RERUN_DETAIL_CODES: Final = (
    CatalogDetailCode.MOUNTING_GATE_PENDING.value,
    CatalogDetailCode.NODE_NOT_ASSIGNED.value,
    CatalogDetailCode.WALK_TEST_IN_PROGRESS.value,
    CatalogDetailCode.PASSES_BELOW_MINIMUM.value,
)
_CLOSE_DETAIL_CODES: Final = (
    CatalogDetailCode.STEPS_STILL_OPEN.value,
    CatalogDetailCode.MATRIX_INCOMPLETE.value,
    CatalogDetailCode.FALSE_NEGATIVE_PRESENT.value,
    CatalogDetailCode.REDUNDANCY_NOT_VERIFIED.value,
    CatalogDetailCode.FALSE_ALARM_RATE_ABOVE_THRESHOLD.value,
    CatalogDetailCode.LATENCY_NOT_MEASURED.value,
    CatalogDetailCode.BLUR_NOT_VERIFIED.value,
    CatalogDetailCode.WALK_TEST_INCOMPLETE.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)
_EXPOSURE_DETAIL_CODES: Final = (
    CatalogDetailCode.PASS_NOT_FOUND.value,
    CatalogDetailCode.WALK_TEST_INCOMPLETE.value,
)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


# --- Cuerpos -----------------------------------------------------------------------------------


class RerunBody(Strict):
    # Sin mínimo en la forma: menos de 3 es ``catalog_passes_below_minimum`` (interfaces §3.3).
    passes_per_cell: StrictInt


class SignatureBody(Strict):
    user_id: uuid.UUID


class FalseAlarmAcceptanceBody(Strict):
    reason_es: StrictStr


class BaselineBody(Strict):
    camera_id: uuid.UUID
    zone_id: uuid.UUID
    captured_at: AwareDatetime


class InstallerMeasurementsBody(Strict):
    beacon_latency_ms_p95: StrictInt
    baselines: Annotated[list[BaselineBody], Field(max_length=8, default_factory=list)]


class CloseBody(Strict):
    signatures: Annotated[list[SignatureBody], Field(min_length=1, max_length=32)]
    false_alarm_acceptance: FalseAlarmAcceptanceBody | None = None
    installer_measurements: InstallerMeasurementsBody


class ExposureSampleBody(Strict):
    pass_id: uuid.UUID
    fetched_at: AwareDatetime
    displayed_at: AwareDatetime


# --- Respuestas --------------------------------------------------------------------------------


class MatrixResultOut(Strict):
    row_id: uuid.UUID
    detected: int
    missed: int
    false_alarms: int
    unverifiable_evidence_refs: list[uuid.UUID] = Field(default_factory=list)
    """``evidence_ref`` de los pases que no resuelven a un clip de verificación de la zona."""


class FalseAlarmAcceptanceOut(Strict):
    reason_es: str
    accepted_by: uuid.UUID
    accepted_at: str


class TrancheOut(Strict):
    median_ms: int | None = None
    p95_ms: int
    max_ms: int | None = None
    repetitions: int | None = None
    measured_by: str


class LatencyOut(Strict):
    """Los cuatro tramos por separado, cada uno con su reloj; nulo es «no medido»."""

    node_tranche: TrancheOut | None = None
    platform_tranche: TrancheOut | None = None
    exposure_tranche: TrancheOut | None = None
    served_tranche: TrancheOut | None = None
    not_measured: list[str] = Field(default_factory=list)
    indicative_sum_median_ms: int | None = None
    indicative_sum_p95_ms: int
    indicative: bool = True
    """Las sumas son orientativas: nunca una cifra única prometida (P6)."""
    repetitions_counted: int | None = None


class CameraMeasuredOut(Strict):
    camera_id: uuid.UUID
    measured_fps: float | None
    declared_min_fps: float | None


class OcclusionSummaryOut(Strict):
    camera_id: uuid.UUID
    verification: OcclusionVerification
    test_id: uuid.UUID | None = None


class BaselineOut(Strict):
    camera_id: uuid.UUID
    zone_id: uuid.UUID
    captured_at: str


class InstallerMeasurementsOut(Strict):
    beacon_latency_ms_p95: int
    measured_by: str = "installer"
    baselines: list[BaselineOut] = Field(default_factory=list)


class SignatureOut(Strict):
    user_id: uuid.UUID
    role_in_use: str
    signed_at: str


class CommissioningRecordOut(Strict):
    """``CommissioningRecord`` (DE §2.15 con sus notas): el acta estructurada, sin responsable."""

    commissioning_record_id: uuid.UUID
    session_id: uuid.UUID
    zone_id: uuid.UUID
    catalog_version: int
    kind: WalkTestKind
    passes_per_cell: int
    matrix_results: list[MatrixResultOut]
    false_negatives_total: int
    false_alarm_rate_observed: float
    false_alarm_threshold: float
    false_alarm_acceptance: FalseAlarmAcceptanceOut | None
    latency: LatencyOut
    cameras_measured: list[CameraMeasuredOut]
    occlusion_summary: list[OcclusionSummaryOut]
    total_duration_ms: int
    """Suma exacta de las duraciones de los pasos (``total_hours`` en milisegundos)."""
    steps_summary: list[StepKindDurationOut]
    installer_measurements: InstallerMeasurementsOut
    signatures: list[SignatureOut]
    closed_at: str


class ExposureSampleOut(Strict):
    sample_id: uuid.UUID
    session_id: uuid.UUID
    pass_id: uuid.UUID
    fetched_at: str
    displayed_at: str
    recorded_at: str


def record_view(document: Mapping[str, Any]) -> CommissioningRecordOut:
    """La respuesta del acta a partir de su forma guardada (la de ``commissioning_record``)."""
    steps = [StepKindDurationOut.model_validate(step) for step in document["steps_summary"]]
    return CommissioningRecordOut.model_validate(
        {**document, "total_duration_ms": sum(step.duration_ms for step in steps)}
    )


def _stored(record: StoredRecord) -> dict[str, Any]:
    return {
        "commissioning_record_id": record.commissioning_record_id,
        "session_id": record.session_id,
        "zone_id": record.zone_id,
        "catalog_version": record.catalog_version,
        "kind": record.kind,
        "passes_per_cell": record.passes_per_cell,
        "matrix_results": record.matrix_results,
        "false_negatives_total": record.false_negatives_total,
        "false_alarm_rate_observed": record.false_alarm_rate_observed,
        "false_alarm_threshold": record.false_alarm_threshold,
        "false_alarm_acceptance": record.false_alarm_acceptance,
        "latency": record.latency,
        "cameras_measured": record.cameras_measured,
        "occlusion_summary": record.occlusion_summary,
        "steps_summary": record.steps_summary,
        "installer_measurements": record.installer_measurements,
        "signatures": record.signatures,
        "closed_at": format_timestamp(record.closed_at),
    }


def _closed(record: CommissioningRecord) -> dict[str, Any]:
    content = record.record_content()
    content["false_alarm_acceptance"] = content.get("false_alarm_acceptance")
    for key in ("regression_cleared",):
        content.pop(key, None)
    return content


def _sample_view(sample: ExposureSample) -> ExposureSampleOut:
    return ExposureSampleOut(
        sample_id=sample.sample_id,
        session_id=sample.session_id,
        pass_id=sample.pass_id,
        fetched_at=format_timestamp(sample.fetched_at),
        displayed_at=format_timestamp(sample.displayed_at),
        recorded_at=format_timestamp(sample.recorded_at),
    )


def _rejected(error: CatalogRejected) -> ApiError:
    return ApiError(error.api_code, detail_code=error.detail_code.value)


def commissioning_records_router() -> APIRouter:
    router = APIRouter(tags=["comisionamiento"])

    @router.post(
        "/zones/{zone_id}/walk-tests/regression-rerun",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_RERUN_DETAIL_CODES), Depends(exact_query())],
        summary="Abre la reejecución por regresión con solo las filas afectadas",
    )
    async def open_regression_rerun(
        zone_id: uuid.UUID,
        body: RerunBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> WalkTestSessionOut:
        no_store(response)
        try:
            session = await installed(services.regression_reruns).open(
                request_context(request), zone_id, body.passes_per_cell
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return walk_test_session_view(WalkTestView.of(session))

    @router.post(
        "/walk-tests/{session_id}/close",
        dependencies=[requires(_RUN, detail_codes=_CLOSE_DETAIL_CODES), Depends(exact_query())],
        summary="Cierra el acta de comisionamiento con sus siete guardas",
    )
    async def close_walk_test(
        session_id: uuid.UUID,
        body: CloseBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CommissioningRecordOut:
        no_store(response)
        measurements = body.installer_measurements
        acceptance = body.false_alarm_acceptance
        try:
            record = await installed(services.records).close(
                request_context(request),
                session_id,
                CloseRequest(
                    signatures=[signature.user_id for signature in body.signatures],
                    beacon_latency_ms_p95=measurements.beacon_latency_ms_p95,
                    baselines=[
                        Baseline(b.camera_id, b.zone_id, b.captured_at)
                        for b in measurements.baselines
                    ],
                    false_alarm_reason_es=None if acceptance is None else acceptance.reason_es,
                ),
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        return record_view(_closed(record))

    @router.post(
        "/walk-tests/{session_id}/exposure-samples",
        status_code=201,
        dependencies=[requires(_RUN, detail_codes=_EXPOSURE_DETAIL_CODES), Depends(exact_query())],
        summary="Registra la muestra de exposición de un pase mostrado (reloj del navegador)",
    )
    async def record_exposure_sample(
        session_id: uuid.UUID,
        body: ExposureSampleBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ExposureSampleOut:
        no_store(response)
        try:
            recorded = await installed(services.exposures).record(
                request_context(request),
                session_id,
                body.pass_id,
                body.fetched_at,
                body.displayed_at,
            )
        except CatalogRejected as error:
            raise _rejected(error) from None
        except WalkTestRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except WalkTestConflict:
            raise ApiError(ApiErrorCode.CONFLICT) from None
        if not recorded.created:
            response.status_code = 200  # la primera muestra del pase, sin efecto
        return _sample_view(recorded.sample)

    @router.get(
        "/commissioning-records/{record_id}",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Acta de comisionamiento estructurada (sin responsable)",
    )
    async def commissioning_record(
        record_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> CommissioningRecordOut:
        no_store(response)
        stored = await installed(services.records).record(request_context(request), record_id)
        return record_view(_stored(stored))

    @router.get(
        "/commissioning-records/{record_id}/document",
        dependencies=[requires(_READ), Depends(exact_query())],
        response_class=Response,
        responses={
            200: {
                "description": "Documento legible del acta (PDF generado a demanda, no almacenado)",
                "content": {PDF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
            }
        },
        summary="Documento legible del acta de comisionamiento (PDF a demanda)",
    )
    async def commissioning_record_document(
        record_id: uuid.UUID, request: Request, services: Services
    ) -> Response:
        renderer = installed(services.record_documents)
        stored = await installed(services.records).record(request_context(request), record_id)
        view = record_view(_stored(stored)).model_dump(mode="json")
        try:
            document = await renderer.render(view)
        except DocumentTimedOut:
            raise ApiError(
                ApiErrorCode.TEMPORARILY_UNAVAILABLE,
                retry_after_seconds=DOCUMENT_RETRY_AFTER_SECONDS,
            ) from None
        return Response(
            content=document,
            media_type=PDF_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="acta-{record_id}.pdf"',
            },
        )

    return router
