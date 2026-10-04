"""Ruta de la vista de transparencia del COPASST (SCR-16; interfaces §3.5; LC-GOB-04).

``GET /zones/{zone_id}/transparency`` (``transparency.read`` sobre la zona):
``{zone_id, declared_scope: {scope_text_es, cameras[], minimum_coverage}, standards[{standard_id,
version, title_es, declared_text}], current_agreement, gates: {mounting, usage, resulting_mode},
pending_confirmation_for_me}``. ``current_agreement`` tiene la forma de interfaces §1.2 (o nulo) y
``pending_confirmation_for_me`` es el acuerdo que espera la firma del usuario de la sesión (o nulo):
la única acción desde esta vista es esa confirmación (``POST
/use-agreements/{agreement_id}/confirmations``). Una zona inexistente, de otra organización o
fuera del alcance responde ``not_found``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from vigia_contracts.models.enumerations import ZoneMode

from vigia_platform.catalog.adapters.http.agreements import AgreementOut, agreement_view
from vigia_platform.catalog.adapters.http.gates import (
    MountingGateOut,
    UsageGateOut,
    state_view,
)
from vigia_platform.catalog.adapters.http.services import CatalogHttp, catalog_http, installed
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.adapters.http.services import exact_query, no_store
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.middleware import request_context

__all__ = ["TransparencyOut", "transparency_router"]

_TRANSPARENCY: Final = PermissionKey.TRANSPARENCY_READ.value

Services = Annotated[CatalogHttp, Depends(catalog_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeclaredScopeOut(_Strict):
    scope_text_es: str | None
    cameras: list[dict[str, Any]]
    minimum_coverage: dict[str, Any] | None


class StandardOut(_Strict):
    standard_id: uuid.UUID
    version: int
    title_es: str
    declared_text: str


class GatesOut(_Strict):
    mounting: MountingGateOut
    usage: UsageGateOut
    resulting_mode: ZoneMode


class TransparencyOut(_Strict):
    zone_id: uuid.UUID
    declared_scope: DeclaredScopeOut
    standards: list[StandardOut]
    current_agreement: AgreementOut | None
    gates: GatesOut
    pending_confirmation_for_me: uuid.UUID | None


def transparency_router() -> APIRouter:
    router = APIRouter(tags=["transparencia"])

    @router.get(
        "/zones/{zone_id}/transparency",
        dependencies=[requires(_TRANSPARENCY), Depends(exact_query())],
        summary="Vista de transparencia: alcance declarado, estándares, acuerdo y compuertas",
    )
    async def transparency(
        zone_id: uuid.UUID, request: Request, response: Response, services: Services
    ) -> TransparencyOut:
        no_store(response)
        view = await installed(services.transparency).view(request_context(request), zone_id)
        gates = state_view(view.gates)
        current = view.current_agreement
        return TransparencyOut(
            zone_id=view.zone_id,
            declared_scope=DeclaredScopeOut(
                scope_text_es=view.scope_text_es,
                cameras=[dict(camera) for camera in view.cameras],
                minimum_coverage=(
                    None if view.minimum_coverage is None else dict(view.minimum_coverage)
                ),
            ),
            standards=[StandardOut.model_validate(dict(s)) for s in view.standards],
            current_agreement=None
            if current is None
            else agreement_view(current, view.confirmations),
            gates=GatesOut(
                mounting=gates.mounting, usage=gates.usage, resulting_mode=gates.resulting_mode
            ),
            pending_confirmation_for_me=view.pending_confirmation_for_me,
        )

    return router
