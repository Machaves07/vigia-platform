"""Variables de proceso de la zona (SCR-04; A-129, TASK-594; LC-GOB-01; BR-GOB-11 y 52).

``PUT /zones/{zone_id}/process-context`` con ``catalog.manage`` sobre la zona y ``reason_es``
obligatorio (10 a 500): ``{before_ms, variables[], reason_es}`` con la forma de
``ProcessContextDeclaration`` de v1.6.0. Responde ``200`` con la **versión nueva del catálogo
firmado**, como las otras cinco rutas de parámetros, con su registro ``catalog_version_published``
(autor, motivo y hora) y ``changed_fields = [process_context]``. ``variables: []`` retira el
bloque: la versión nueva sale sin él.

Las reglas son las del modelo del contrato (regla 7 del catálogo): una variable de estado cita una
señal declarada en la misma versión, ``variable_id`` y ``code`` no se repiten y la escala de una
analógica no es plana. Si alguna falla, ``invalid_request`` y ninguna versión nueva. La
descripción de cada variable pasa por la política de texto libre (``catalog_free_text_rejected``).

**Regresión (Q7 de A-129, precisa BR-GOB-52):** las variables son contexto y nunca entran en un
predicado ni en el aviso (BR-BOR-15), así que cambiarlas no deja la regresión de la zona pendiente:
la matriz afectada queda vacía. Cambiar las señales sí la deja, como siempre.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, StrictInt, StrictStr
from vigia_contracts.models.zone_catalog import ProcessVariableDeclaration

from vigia_platform.catalog.adapters.http.catalog import CatalogVersionOut, Strict, published
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import SetProcessContext
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.middleware import request_context

__all__ = ["MAX_BEFORE_MS", "MAX_PROCESS_VARIABLES", "ProcessContextBody", "process_context_router"]

_MANAGE: Final = PermissionKey.CATALOG_MANAGE.value
_DETAIL_CODES: Final = (
    CatalogDetailCode.FREE_TEXT_REJECTED.value,
    CatalogDetailCode.ZONE_WITHOUT_CAMERAS.value,
)
MAX_PROCESS_VARIABLES: Final = 12
"""``ProcessContextDeclaration.variables``: de 1 a 12 (0 en el cuerpo retira el bloque)."""
MAX_BEFORE_MS: Final = 120_000
"""``ProcessContextDeclaration.before_ms``: de 0 a 120 000 ms (30 000 en Pope, A-129)."""

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class ProcessContextBody(Strict):
    before_ms: Annotated[StrictInt, Field(ge=0, le=MAX_BEFORE_MS)]
    variables: list[ProcessVariableDeclaration] = Field(max_length=MAX_PROCESS_VARIABLES)
    """``ProcessVariableDeclaration`` del contrato; vacía, la versión nueva sale sin el bloque."""
    reason_es: StrictStr


def process_context_router() -> APIRouter:
    router = APIRouter(tags=["catálogo"])

    @router.put(
        "/zones/{zone_id}/process-context",
        dependencies=[requires(_MANAGE, detail_codes=_DETAIL_CODES), Depends(exact_query())],
        summary="Variables de proceso de la zona: versión nueva del catálogo sin regresión",
    )
    async def process_context(
        zone_id: uuid.UUID,
        body: ProcessContextBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> CatalogVersionOut:
        no_store(response)
        change = SetProcessContext(
            before_ms=body.before_ms,
            variables=tuple(variable.to_json_value() for variable in body.variables),
        )
        service = installed(services.catalog)
        return await published(
            lambda: service.publish_catalog_version(
                request_context(request), zone_id, change, body.reason_es
            )
        )

    return router
