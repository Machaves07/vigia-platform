"""Las dos rutas de la configuración del lazo (SUP-U05-38 y 39; LC-LAZ §6.10; LC-LAZ-19).

- ``GET /loop/plants/{plant_id}/configuration`` (``hierarchy.read`` sobre la planta): ``200`` con
  la configuración vigente (plazos, umbrales, calendario laboral y zona horaria); la primera
  lectura crea los valores por defecto con su registro de origen.
- ``PUT /loop/plants/{plant_id}/configuration`` (``organization.settings`` sobre la planta): los
  parámetros que cambian y ``reason`` (10 a 500 caracteres). ``200`` con la configuración que queda
  (la versión siguiente, o la misma si nada cambia). Sin motivo,
  ``loop_configuration_change_reason_required``; un parámetro fuera de su rango o que rompe una
  regla entre parámetros, ``loop_deadline_out_of_range``; un valor que no es un entero, un nombre
  desconocido, ``timezone`` o un calendario mal formado, ``invalid_request`` (esquema estricto).
  Los dos códigos de negocio viajan como ``invalid_request`` con su ``detail_code`` (400, A-68).

Una planta inexistente, de otra organización, fuera del alcance o pedida bajo concesión de
proveedor responde ``not_found``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.loop.adapters.http.services import LoopHttp, installed, loop_http
from vigia_platform.loop.calendar import MAX_NON_WORKING_DATES
from vigia_platform.loop.config.parameters import (
    INTEGER_PARAMETERS,
    ConfigurationRejected,
    PlantLoopConfiguration,
)
from vigia_platform.loop.config.service import ConfigurationRequestInvalid
from vigia_platform.loop.detail_codes import HTTP_STATUS_BY_DETAIL_CODE, LoopDetailCode
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "BusinessCalendarBody",
    "ConfigurationChangeBody",
    "ConfigurationOut",
    "configuration_router",
]

_READ: Final = PermissionKey.HIERARCHY_READ.value
_SETTINGS: Final = PermissionKey.ORGANIZATION_SETTINGS.value
_PUT_DETAIL_CODES: Final = (
    LoopDetailCode.DEADLINE_OUT_OF_RANGE.value,
    LoopDetailCode.CONFIGURATION_CHANGE_REASON_REQUIRED.value,
)
PATH: Final = "/loop/plants/{plant_id}/configuration"

Services = Annotated[LoopHttp, Depends(loop_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class BusinessCalendarBody(_Strict):
    working_weekdays: list[StrictInt] = Field(min_length=1, max_length=7)
    """Días laborables ISO 8601: 1 = lunes … 7 = domingo."""
    non_working_dates: list[StrictStr] = Field(max_length=MAX_NON_WORKING_DATES)
    """Fechas no laborables declaradas ``AAAA-MM-DD`` (a lo sumo 40 por año)."""


class ConfigurationChangeBody(_Strict):
    """Solo los parámetros que cambian, y el motivo."""

    classification_deadline_business_days: StrictInt | None = None
    review_deadline_business_days: StrictInt | None = None
    action_deadline_default_days: StrictInt | None = None
    action_deadline_min_days: StrictInt | None = None
    action_deadline_max_days: StrictInt | None = None
    manager_highlight_overdue_days: StrictInt | None = None
    unclosable_review_default_months: StrictInt | None = None
    unclosable_review_max_months: StrictInt | None = None
    monthly_package_generation_day: StrictInt | None = None
    sample_percent: StrictInt | None = None
    sample_min: StrictInt | None = None
    sample_max: StrictInt | None = None
    adoption_silence_days: StrictInt | None = None
    exposure_coverage_threshold_percent: StrictInt | None = None
    email_grouping_hours: StrictInt | None = None
    export_max_days: StrictInt | None = None
    export_max_zones: StrictInt | None = None
    single_occupancy_default_window_minutes: StrictInt | None = None
    reminder_interval_hours: StrictInt | None = None
    business_calendar: BusinessCalendarBody | None = None
    reason: StrictStr | None = None
    """Motivo del cambio, de 10 a 500 caracteres (obligatorio)."""


class BusinessCalendarOut(BaseModel):
    working_weekdays: list[int]
    non_working_dates: list[str]


class ChangedByOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    role_in_use: str


class ConfigurationOut(BaseModel):
    plant_id: uuid.UUID
    classification_deadline_business_days: int
    review_deadline_business_days: int
    action_deadline_default_days: int
    action_deadline_min_days: int
    action_deadline_max_days: int
    manager_highlight_overdue_days: int
    unclosable_review_default_months: int
    unclosable_review_max_months: int
    monthly_package_generation_day: int
    sample_percent: int
    sample_min: int
    sample_max: int
    adoption_silence_days: int
    exposure_coverage_threshold_percent: int
    email_grouping_hours: int
    export_max_days: int
    export_max_zones: int
    single_occupancy_default_window_minutes: int
    reminder_interval_hours: int
    business_calendar: BusinessCalendarOut
    timezone: str
    version: int
    changed_at: str
    changed_by: ChangedByOut
    change_reason: str


def view(configuration: PlantLoopConfiguration) -> ConfigurationOut:
    changed_by = configuration.changed_by
    return ConfigurationOut(
        plant_id=configuration.plant_id,
        **configuration.settings.integers,
        business_calendar=BusinessCalendarOut(**configuration.business_calendar.document()),
        timezone=configuration.timezone,
        version=configuration.version,
        changed_at=format_timestamp(configuration.changed_at),
        changed_by=ChangedByOut(
            user_id=uuid.UUID(str(changed_by["user_id"])),
            display_name=str(changed_by["display_name"]),
            role_in_use=str(changed_by["role_in_use"]),
        ),
        change_reason=configuration.change_reason,
    )


def _integers(body: ConfigurationChangeBody) -> dict[str, Any]:
    named = body.model_fields_set & set(INTEGER_PARAMETERS)
    values = {name: getattr(body, name) for name in named}
    if any(value is None for value in values.values()):
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return values


def configuration_router() -> APIRouter:
    router = APIRouter(tags=["lazo"])

    @router.get(
        PATH,
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Configuración del lazo de la planta: plazos, umbrales, calendario y zona",
    )
    async def configuration(
        plant_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> ConfigurationOut:
        no_store(response)
        found = await installed(services.configuration).configuration(
            request_context(request), plant_id
        )
        return view(found)

    @router.put(
        PATH,
        dependencies=[
            requires(_SETTINGS, detail_codes=_PUT_DETAIL_CODES),
            Depends(exact_query()),
        ],
        summary="Cambia la configuración del lazo de la planta con motivo (versión nueva)",
    )
    async def change_configuration(
        plant_id: uuid.UUID,
        body: ConfigurationChangeBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ConfigurationOut:
        no_store(response)
        if "business_calendar" in body.model_fields_set and body.business_calendar is None:
            raise ApiError(ApiErrorCode.INVALID_REQUEST)
        calendar = None if body.business_calendar is None else body.business_calendar.model_dump()
        try:
            changed = await installed(services.configuration).change(
                request_context(request),
                plant_id,
                integers=_integers(body),
                business_calendar=calendar,
                reason=body.reason,
            )
        except ConfigurationRejected as rejected:
            code, _status = HTTP_STATUS_BY_DETAIL_CODE[rejected.detail_code]
            raise ApiError(code, detail_code=rejected.detail_code.value) from None
        except ConfigurationRequestInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        return view(changed)

    return router
