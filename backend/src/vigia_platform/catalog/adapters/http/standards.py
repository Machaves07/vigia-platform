"""Rutas de los estándares de la zona (SCR-04; interfaces-para-u04-u05 §3.1; LC-GOB-01).

Todas con ``catalog.manage`` sobre la zona; cada una publica una versión nueva del catálogo firmado
(``201``) por el servicio único de TASK-208 y su marca de regresión la calcula el sistema
(TASK-209):

- ``POST /zones/{zone_id}/standards``: ``{family, title_es, declared_text, predicate, reason_es,
  zone_parameters?}``. La **primera** versión de una zona necesita además sus parámetros
  (``zone_parameters``: cámaras con su ``code`` legible, cobertura mínima, señales, umbrales,
  ventanas y, si se quiere, la marca unipersonal): el diseño no fija valores por defecto. Sin
  catálogo y sin ellos, ``conflict`` con ``catalog_zone_without_cameras``; con catálogo, los
  parámetros se cambian por sus rutas y ``zone_parameters`` es ``invalid_request``. Errores
  ``catalog_family_not_admitted``, ``catalog_predicate_invalid``, ``catalog_free_text_rejected``
  y ``catalog_unsatisfiable_coverage``.
- ``POST /zones/{zone_id}/standards/{standard_id}/versions``: ``{changes, reason_es}``, con
  ``changes`` ⊆ ``{title_es, declared_text, predicate}`` (al menos uno). Lo que no se pasa se
  conserva (BR-GOB-05); solo un predicado cambiado marca regresión.
- ``POST /zones/{zone_id}/standards/{standard_id}/retirement``: ``{reason_es, effective_from}``.
  ``effective_from`` no puede ser anterior a la publicación (sin retiro retroactivo, P4) ni
  programado: la versión rige desde su emisión. El último estándar de la zona no se retira
  (``conflict`` con ``catalog_last_standard_in_zone``, BR-GOB-09).

Un estándar inexistente o de otra zona responde ``not_found``, como una zona fuera del alcance.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, Field, StrictBool, StrictInt, StrictStr
from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.catalog.adapters.http.catalog import (
    CatalogVersionOut,
    Strict,
    published,
)
from vigia_platform.catalog.adapters.http.parameters import (
    MAX_SIGNALS,
    CameraBody,
    ClipWindowBody,
    CoverageValues,
    EpisodeBody,
    ThresholdValues,
    cameras_of,
)
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import (
    AGGREGATION_WINDOW_DEFAULT,
    AGGREGATION_WINDOW_MAX,
    AGGREGATION_WINDOW_MIN,
    InitialZoneParameters,
    NewStandard,
    NewStandardVersion,
    StandardDraft,
)
from vigia_platform.catalog.domain.zone_camera import MAX_CAMERAS
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.middleware import request_context

__all__ = ["standards_router"]

_MANAGE: Final = PermissionKey.CATALOG_MANAGE.value
_NEW_DETAIL_CODES: Final = (
    CatalogDetailCode.FAMILY_NOT_ADMITTED.value,
    CatalogDetailCode.PREDICATE_INVALID.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
    CatalogDetailCode.ZONE_WITHOUT_CAMERAS.value,
    CatalogDetailCode.UNSATISFIABLE_COVERAGE.value,
)
_VERSION_DETAIL_CODES: Final = (
    CatalogDetailCode.FAMILY_NOT_ADMITTED.value,
    CatalogDetailCode.PREDICATE_INVALID.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)
_RETIREMENT_DETAIL_CODES: Final = (
    CatalogDetailCode.LAST_STANDARD_IN_ZONE.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class ZoneParametersBody(Strict):
    """Los parámetros de la versión 1 de la zona (solo con la zona aún sin catálogo)."""

    cameras: list[CameraBody] = Field(min_length=1, max_length=MAX_CAMERAS)
    minimum_coverage: CoverageValues
    signals: list[dict[str, Any]] = Field(max_length=MAX_SIGNALS)
    thresholds: ThresholdValues
    clip_window: ClipWindowBody
    episode: EpisodeBody
    single_occupancy: StrictBool = False
    aggregation_window_minutes: Annotated[
        StrictInt, Field(ge=AGGREGATION_WINDOW_MIN, le=AGGREGATION_WINDOW_MAX)
    ] = AGGREGATION_WINDOW_DEFAULT


class StandardBody(Strict):
    family: PredicateFamily
    title_es: StrictStr
    declared_text: StrictStr
    predicate: dict[str, Any]
    reason_es: StrictStr
    zone_parameters: ZoneParametersBody | None = None


class StandardChanges(Strict):
    title_es: StrictStr | None = None
    declared_text: StrictStr | None = None
    predicate: dict[str, Any] | None = None


class StandardVersionBody(Strict):
    changes: StandardChanges
    reason_es: StrictStr


class RetirementBody(Strict):
    reason_es: StrictStr
    effective_from: AwareDatetime


def _initial(body: ZoneParametersBody | None) -> InitialZoneParameters | None:
    if body is None:
        return None
    return InitialZoneParameters(
        cameras=cameras_of(body.cameras),
        required_count=body.minimum_coverage.required_count,
        required_camera_ids=tuple(body.minimum_coverage.required_camera_ids),
        signals=tuple(body.signals),
        thresholds=body.thresholds.model_dump(),
        clip_window=body.clip_window.model_dump(),
        episode=body.episode.model_dump(),
        single_occupancy=body.single_occupancy,
        aggregation_window_minutes=body.aggregation_window_minutes,
    )


def standards_router() -> APIRouter:
    router = APIRouter(tags=["catálogo"])

    @router.post(
        "/zones/{zone_id}/standards",
        status_code=201,
        dependencies=[requires(_MANAGE, detail_codes=_NEW_DETAIL_CODES), Depends(exact_query())],
        summary="Declara un estándar en la zona: versión nueva del catálogo firmado",
    )
    async def new_standard(
        zone_id: uuid.UUID,
        body: StandardBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        service = installed(services.catalog)
        return await published(
            lambda: service.publish_catalog_version(
                request_context(request),
                zone_id,
                NewStandard(
                    draft=StandardDraft(
                        family=body.family,
                        title_es=body.title_es,
                        declared_text=body.declared_text,
                        predicate=body.predicate,
                    ),
                    initial=_initial(body.zone_parameters),
                ),
                body.reason_es,
            )
        )

    @router.post(
        "/zones/{zone_id}/standards/{standard_id}/versions",
        status_code=201,
        dependencies=[
            requires(_MANAGE, detail_codes=_VERSION_DETAIL_CODES),
            Depends(exact_query()),
        ],
        summary="Versión nueva de un estándar: la anterior permanece consultable",
    )
    async def new_standard_version(
        zone_id: uuid.UUID,
        standard_id: uuid.UUID,
        body: StandardVersionBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        service = installed(services.catalog)
        changes = body.changes
        return await published(
            lambda: service.publish_catalog_version(
                request_context(request),
                zone_id,
                NewStandardVersion(
                    standard_id=standard_id,
                    title_es=changes.title_es,
                    declared_text=changes.declared_text,
                    predicate=changes.predicate,
                ),
                body.reason_es,
            )
        )

    @router.post(
        "/zones/{zone_id}/standards/{standard_id}/retirement",
        status_code=201,
        dependencies=[
            requires(_MANAGE, detail_codes=_RETIREMENT_DETAIL_CODES),
            Depends(exact_query()),
        ],
        summary="Retira un estándar: versión nueva del catálogo sin él, nunca un borrado",
    )
    async def retirement(
        zone_id: uuid.UUID,
        standard_id: uuid.UUID,
        body: RetirementBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        service = installed(services.catalog)
        return await published(
            lambda: service.retire_standard(
                request_context(request),
                zone_id,
                standard_id,
                body.effective_from,
                body.reason_es,
            )
        )

    return router
