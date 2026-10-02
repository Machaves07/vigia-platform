"""``GET /zones/{zone_id}/coverage`` y ``GET /zones/{zone_id}/coverage/at`` (``coverage.read``).

``business-logic-model.md`` §7 y §10.2; BR-NUC-69 a 74; H-38.

- ``/coverage?from&to``: la línea de tiempo de ``[from, to)`` (``CoverageTimeline``,
  domain-entities §3.8): partición exacta en tramos de estado compuesto, la capa ``node_report``
  sin componer («lo que el nodo informa») y el resumen, que suma exactamente ``to - from``. Un
  hueco de comunicación aparece como su propio tramo ``not_observable`` con causa
  ``no_communication``: nunca se omite ni se presenta como observado (N-13). Ningún estado
  significa «despejada» ni «segura» (P2).
- ``/coverage/at?instant``: el compuesto de la zona en ese instante (BR-NUC-74).

El periodo tiene como mucho **31 días** (PAT-NUC-REN-04): uno más largo responde
``period_too_long`` con su mensaje en español, sin consultar ni auditar; quien necesita más pide
tramos y los concatena (PR-NUC-51). Las marcas llevan zona horaria (``Z`` o desfase) y precisión de
milisegundos; ``from`` tiene que ser anterior a ``to``. Cada consulta válida queda auditada como
``coverage_read`` (``CoveragePort``). La zona fuera del alcance de ``coverage.read`` responde
``not_found``, igual que una inexistente. Bajo concesión (el instalador del proveedor tiene
``coverage.read``) se escribe además ``provider_query`` (BR-NUC-38).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import (
    LedgerHttp,
    exact_query,
    ledger_http,
    narrowed_context,
    no_store,
    parse_instant,
    provider_access,
    request_context,
)
from vigia_platform.ledger.application.coverage import (
    CoverageInputInvalid,
    CoveragePeriod,
    CoverageZoneNotFound,
    PeriodTooLong,
)
from vigia_platform.ledger.domain.coverage import (
    CoverageInterval,
    CoverageStatus,
    CoverageSummary,
)
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["CoverageStatusOut", "CoverageTimelineOut", "coverage_router"]

Services = Annotated[LedgerHttp, Depends(ledger_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubjectOut(_Strict):
    kind: str
    camera_id: uuid.UUID | None = None
    signal_id: uuid.UUID | None = None


class IntervalOut(_Strict):
    """``CoverageInterval`` (domain-entities §3.8)."""

    starts_at: str
    ends_at: str
    duration_ms: int
    layer: str
    state: str
    causes: tuple[str, ...]
    subject: SubjectOut | None
    source_record_ids: tuple[uuid.UUID, ...]
    clock_basis: str
    clock_offset_ms: int | None = None


class SummaryOut(_Strict):
    observable_ms: int
    degraded_ms: int
    not_observable_ms: int
    no_communication_ms: int
    never_reported_ms: int
    zone_not_active_ms: int


class CoverageTimelineOut(_Strict):
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    period: dict[str, str]
    """``{from, to}`` del periodo pedido."""
    intervals: tuple[IntervalOut, ...]
    node_intervals: tuple[IntervalOut, ...]
    summary: SummaryOut


class CoverageStatusOut(_Strict):
    zone_id: uuid.UUID
    instant: str
    state: str
    causes: tuple[str, ...]
    layer: str
    clock_basis: str
    source_record_ids: tuple[uuid.UUID, ...]


def _interval(interval: CoverageInterval) -> IntervalOut:
    subject = interval.subject
    return IntervalOut(
        starts_at=format_timestamp(interval.starts_at),
        ends_at=format_timestamp(interval.ends_at),
        duration_ms=interval.duration_ms,
        layer=interval.layer.value,
        state=interval.state.value,
        causes=interval.causes,
        subject=None
        if subject is None
        else SubjectOut(
            kind=subject.kind, camera_id=subject.camera_id, signal_id=subject.signal_id
        ),
        source_record_ids=interval.source_record_ids,
        clock_basis=interval.clock_basis.value,
        clock_offset_ms=interval.clock_offset_ms,
    )


def _summary(summary: CoverageSummary) -> SummaryOut:
    return SummaryOut(
        observable_ms=summary.observable_ms,
        degraded_ms=summary.degraded_ms,
        not_observable_ms=summary.not_observable_ms,
        no_communication_ms=summary.no_communication_ms,
        never_reported_ms=summary.never_reported_ms,
        zone_not_active_ms=summary.zone_not_active_ms,
    )


def _status(zone_id: uuid.UUID, status: CoverageStatus) -> CoverageStatusOut:
    return CoverageStatusOut(
        zone_id=zone_id,
        instant=format_timestamp(status.instant),
        state=status.state.value,
        causes=status.causes,
        layer=status.layer.value,
        clock_basis=status.clock_basis.value,
        source_record_ids=status.source_record_ids,
    )


def coverage_router() -> APIRouter:
    router = APIRouter(tags=["cobertura"])
    key = PermissionKey.COVERAGE_READ

    @router.get(
        "/zones/{zone_id}/coverage",
        dependencies=[requires(key.value), Depends(exact_query("from", "to"))],
        summary="Línea de tiempo de cobertura de la zona en [from, to), tope de 31 días",
    )
    async def timeline(
        zone_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        from_: Annotated[str, Query(alias="from")],
        to: str,
    ) -> CoverageTimelineOut:
        no_store(response)
        context = request_context(request)
        period = CoveragePeriod(
            parse_instant(from_, milliseconds=True), parse_instant(to, milliseconds=True)
        )
        reader = await narrowed_context(services, context, key)
        try:
            result = await services.coverage.linea_de_tiempo(reader, zone_id, period)
        except PeriodTooLong:
            raise ApiError(ApiErrorCode.PERIOD_TOO_LONG) from None
        except CoverageInputInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except CoverageZoneNotFound:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        await provider_access(request, services, context, "read")
        return CoverageTimelineOut(
            organization_id=result.organization_id,
            plant_id=result.plant_id,
            zone_id=result.zone_id,
            period={
                "from": format_timestamp(result.period.start),
                "to": format_timestamp(result.period.end),
            },
            intervals=tuple(_interval(i) for i in result.intervals),
            node_intervals=tuple(_interval(i) for i in result.node_intervals),
            summary=_summary(result.summary),
        )

    @router.get(
        "/zones/{zone_id}/coverage/at",
        dependencies=[requires(key.value), Depends(exact_query("instant"))],
        summary="Estado compuesto de cobertura de la zona en un instante",
    )
    async def status_at(
        zone_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        instant: str,
    ) -> CoverageStatusOut:
        no_store(response)
        context = request_context(request)
        moment = parse_instant(instant, milliseconds=True)
        reader = await narrowed_context(services, context, key)
        try:
            status = await services.coverage.estado_en(reader, zone_id, moment)
        except CoverageInputInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except CoverageZoneNotFound:
            raise ApiError(ApiErrorCode.NOT_FOUND) from None
        await provider_access(request, services, context, "read")
        return _status(zone_id, status)

    return router
