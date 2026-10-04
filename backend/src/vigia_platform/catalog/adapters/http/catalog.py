"""Rutas del catálogo de la zona (SCR-04; interfaces-para-u04-u05 §3.1; LC-GOB-01).

- ``GET /zones/{zone_id}/catalog`` (``catalog.read``): la versión vigente con su ``ZoneCatalog``,
  motivo, autor, fecha, ``changed_fields``, ``single_occupancy`` y ``aggregation_window_minutes``.
  Una zona sin catálogo responde ``not_found``.
- ``GET /zones/{zone_id}/catalog/versions`` (``catalog.read``): el historial, la más reciente
  primero, en páginas de hasta 200 (``after`` es el ``next_after`` de la página anterior).
- ``GET /zones/{zone_id}/catalog/versions/{catalog_version}`` (``catalog.read``): la versión
  completa con su sobre firmado **tal como se guardó** (``stored_envelope``: nunca vuelve a
  firmar ni canonicaliza, H-42).
- ``PUT /zones/{zone_id}/catalog/single-occupancy`` (``catalog.manage``):
  ``{single_occupancy, aggregation_window_minutes (15 a 480, 60 por defecto), reason_es}``.
  ``200`` con la versión nueva; no marca regresión (BR-GOB-10).

No existe ``GET .../catalog/versions/{from}/diff/{to}`` (A-54): U-05 compara versiones con la
ruta de cada una. Una zona inexistente, de otra organización o fuera del alcance responde
``not_found``; con la firma caída, ``temporarily_unavailable`` y nada escrito. Los cuerpos son
cerrados (``extra = forbid``): ningún campo puede fijar ni suprimir la marca de regresión (G-10).

``published`` traduce los errores de toda publicación; la usan también ``standards`` y
``parameters``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.publication import (
    MAX_CATALOG_VERSION,
    MAX_PAGE_SIZE,
    CatalogPublicationFailed,
    CatalogRequestInvalid,
)
from vigia_platform.catalog.application.regression import RegressionWriteFailed
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import (
    AGGREGATION_WINDOW_DEFAULT,
    AGGREGATION_WINDOW_MAX,
    AGGREGATION_WINDOW_MIN,
    CatalogRuleViolated,
    SetSingleOccupancy,
    ZoneCatalogVersion,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "PUBLICATION_DETAIL_CODES",
    "CatalogVersionOut",
    "Strict",
    "catalog_router",
    "published",
    "version_view",
]

_READ: Final = PermissionKey.CATALOG_READ.value
_MANAGE: Final = PermissionKey.CATALOG_MANAGE.value
_CURSOR: Final = re.compile(r"[1-9][0-9]{0,9}")
PUBLICATION_DETAIL_CODES: Final = (
    CatalogDetailCode.UNSATISFIABLE_COVERAGE.value,
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
)
"""Lo que puede rechazar cualquier publicación de parámetros (BR-GOB-11, texto libre)."""

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Cuerpos -----------------------------------------------------------------------------------


class SingleOccupancyBody(Strict):
    single_occupancy: StrictBool
    aggregation_window_minutes: Annotated[
        StrictInt, Field(ge=AGGREGATION_WINDOW_MIN, le=AGGREGATION_WINDOW_MAX)
    ] = AGGREGATION_WINDOW_DEFAULT
    reason_es: StrictStr


# --- Respuestas --------------------------------------------------------------------------------


class CatalogVersionSummary(Strict):
    """Una entrada del historial: qué cambió, por qué, quién y cuándo (H-44)."""

    zone_id: uuid.UUID
    catalog_version: int
    issued_at: str
    issued_by: uuid.UUID
    role_in_use: Role
    reason_es: str
    changed_fields: tuple[CatalogChangedField, ...]
    superseded_at: str | None


class CatalogVersionOut(CatalogVersionSummary):
    """La versión con su ``ZoneCatalog`` y los atributos de plataforma que no viajan al nodo."""

    single_occupancy: bool
    aggregation_window_minutes: int
    catalog: dict[str, Any]
    """El ``ZoneCatalog`` firmado (la carga del sobre)."""


class CatalogVersionDetail(CatalogVersionOut):
    envelope: dict[str, Any]
    """El ``SignedEnvelope<ZoneCatalog>`` tal como se guardó al emitirlo."""


class CatalogHistoryOut(Strict):
    versions: tuple[CatalogVersionSummary, ...]
    next_after: str | None
    """Pásalo como ``after`` para la página siguiente; ``null`` si no hay más."""


def _summary_fields(version: ZoneCatalogVersion) -> dict[str, Any]:
    return {
        "zone_id": version.zone_id,
        "catalog_version": version.catalog_version,
        "issued_at": format_timestamp(version.issued_at),
        "issued_by": version.issued_by,
        "role_in_use": version.role_in_use,
        "reason_es": version.reason_es,
        "changed_fields": version.changed_fields,
        "superseded_at": (
            None if version.superseded_at is None else format_timestamp(version.superseded_at)
        ),
    }


def version_view(version: ZoneCatalogVersion) -> CatalogVersionOut:
    return CatalogVersionOut(
        **_summary_fields(version),
        single_occupancy=version.single_occupancy,
        aggregation_window_minutes=version.aggregation_window_minutes,
        catalog=dict(version.payload),
    )


async def published(
    publication: Callable[[], Awaitable[ZoneCatalogVersion]],
) -> CatalogVersionOut:
    """La versión nueva, o el error de la persona: ``detail_code`` del catálogo bajo su ``code``
    (``conflict`` o ``invalid_request``), ``invalid_request`` sin él, o el del expediente."""
    try:
        version = await publication()
    except CatalogRejected as error:
        raise ApiError(error.api_code, detail_code=error.detail_code.value) from None
    except (CatalogRequestInvalid, CatalogRuleViolated):
        raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
    except (CatalogPublicationFailed, RegressionWriteFailed) as error:
        raise from_ledger_rejection(error.rejection) from None
    return version_view(version)


def _cursor(value: str | None) -> int | None:
    if value is None:
        return None
    if _CURSOR.fullmatch(value) is None or int(value) > MAX_CATALOG_VERSION:
        raise ApiError(ApiErrorCode.INVALID_REQUEST)
    return int(value)


CatalogVersionNumber = Annotated[int, Path(ge=1, le=MAX_CATALOG_VERSION)]


def catalog_router() -> APIRouter:
    router = APIRouter(tags=["catálogo"])

    @router.get(
        "/zones/{zone_id}/catalog",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Catálogo vigente de la zona con su motivo, autor y fecha",
    )
    async def current_catalog(
        zone_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> CatalogVersionOut:
        no_store(response)
        version = await installed(services.catalog).catalog_version(
            request_context(request), zone_id
        )
        return version_view(version)

    @router.get(
        "/zones/{zone_id}/catalog/versions",
        dependencies=[requires(_READ), Depends(exact_query("after", "limit"))],
        summary="Historial de versiones del catálogo de la zona, la más reciente primero",
    )
    async def catalog_history(
        zone_id: uuid.UUID,
        request: Request,
        response: Response,
        services: Services,
        after: Annotated[str | None, Query(max_length=10)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = MAX_PAGE_SIZE,
    ) -> CatalogHistoryOut:
        no_store(response)
        page = await installed(services.catalog).catalog_history(
            request_context(request), zone_id, before=_cursor(after), limit=limit
        )
        return CatalogHistoryOut(
            versions=tuple(CatalogVersionSummary(**_summary_fields(v)) for v in page.items),
            next_after=None if page.next_before is None else str(page.next_before),
        )

    @router.get(
        "/zones/{zone_id}/catalog/versions/{catalog_version}",
        dependencies=[requires(_READ), Depends(exact_query())],
        summary="Versión del catálogo con su sobre firmado tal como se emitió",
    )
    async def catalog_version(
        zone_id: uuid.UUID,
        catalog_version: CatalogVersionNumber,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionDetail:
        no_store(response)
        version = await installed(services.catalog).catalog_version(
            request_context(request), zone_id, catalog_version
        )
        return CatalogVersionDetail(
            **version_view(version).model_dump(), envelope=dict(version.envelope)
        )

    @router.put(
        "/zones/{zone_id}/catalog/single-occupancy",
        dependencies=[
            requires(_MANAGE, detail_codes=(CatalogDetailCode.FREE_TEXT_REJECTED.value,)),
            Depends(exact_query()),
        ],
        summary="Marca unipersonal y ventana de agregación de la zona (no marca regresión)",
    )
    async def single_occupancy(
        zone_id: uuid.UUID,
        body: SingleOccupancyBody,
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
                SetSingleOccupancy(
                    single_occupancy=body.single_occupancy,
                    aggregation_window_minutes=body.aggregation_window_minutes,
                ),
                body.reason_es,
            )
        )

    return router
