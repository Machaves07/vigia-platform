"""Rutas de la jerarquía (``business-logic-model.md`` §2 y §10.2; BR-NUC-07, 08, 12).

- ``GET /hierarchy`` (``hierarchy.read``, los siete roles): la organización con las plantas, zonas
  y nodos **al alcance de la sesión** (una asignación de zona ve su zona dentro de su planta; una
  de planta, la planta con todas sus zonas). Bajo concesión, el alcance concedido.
- ``POST /plants`` (``hierarchy.manage`` sobre la organización): planta con ``country``,
  ``data_region`` (inmutable, BR-NUC-07) y ``timezone``; escribe la génesis de su cadena
  (``plant_created``). Responde ``201``.
- ``POST /plants/{plant_id}/zones`` (``hierarchy.manage`` sobre la planta): zona nueva con
  ``zone_created``. Responde ``201``. Una planta de otra organización responde ``not_found``.

Un código repetido responde ``conflict``; un nombre que no pasa la política de texto libre, un
país, región o zona horaria inválidos, ``invalid_request``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.identity.adapters.http.services import (
    IdentityHttp,
    identity_error,
    identity_http,
    installed,
    no_store,
)
from vigia_platform.identity.application import hierarchy as model
from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.middleware import request_context

__all__ = ["hierarchy_router"]

Services = Annotated[IdentityHttp, Depends(identity_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlantBody(_Strict):
    code: StrictStr = Field(min_length=2, max_length=32)
    name: StrictStr = Field(min_length=1, max_length=120)
    country: StrictStr = Field(min_length=2, max_length=2)
    data_region: StrictStr = Field(min_length=1, max_length=32)
    timezone: StrictStr = Field(min_length=3, max_length=64)


class ZoneBody(_Strict):
    code: StrictStr = Field(min_length=2, max_length=32)
    name: StrictStr = Field(min_length=1, max_length=120)


class ZoneView(_Strict):
    zone_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    name: str
    node_id: uuid.UUID | None


class PlantView(_Strict):
    plant_id: uuid.UUID
    code: str
    name: str
    country: str
    data_region: str
    timezone: str
    status: str
    zones: tuple[ZoneView, ...]


class NodeView(_Strict):
    node_id: uuid.UUID
    plant_id: uuid.UUID
    code: str
    status: str
    live_view_local_url: str | None


class HierarchyOrganizationView(_Strict):
    organization_id: uuid.UUID
    code: str
    name: str
    kind: str
    status: str


class HierarchyView(_Strict):
    organization: HierarchyOrganizationView
    plants: tuple[PlantView, ...]
    nodes: tuple[NodeView, ...]


def _zone(zone: model.ZoneView) -> ZoneView:
    return ZoneView(
        zone_id=zone.zone_id,
        plant_id=zone.plant_id,
        code=zone.code,
        name=zone.name,
        node_id=zone.node_id,
    )


def _plant(plant: model.PlantView) -> PlantView:
    return PlantView(
        plant_id=plant.plant_id,
        code=plant.code,
        name=plant.name,
        country=plant.country,
        data_region=plant.data_region,
        timezone=plant.timezone,
        status=plant.status,
        zones=tuple(_zone(zone) for zone in plant.zones),
    )


_READ: Final = PermissionKey.HIERARCHY_READ.value
_MANAGE: Final = PermissionKey.HIERARCHY_MANAGE.value


def hierarchy_router() -> APIRouter:
    router = APIRouter(tags=["jerarquía"])

    @router.get(
        "/hierarchy",
        dependencies=[requires(_READ)],
        summary="Organización, plantas, zonas y nodos al alcance de la sesión",
    )
    async def hierarchy(request: Request, response: Response, services: Services) -> HierarchyView:
        no_store(response)
        view = await installed(services.hierarchy).hierarchy(request_context(request))
        organization = view.organization
        return HierarchyView(
            organization=HierarchyOrganizationView(
                organization_id=organization.organization_id,
                code=organization.code,
                name=organization.name,
                kind=organization.kind,
                status=organization.status,
            ),
            plants=tuple(_plant(plant) for plant in view.plants),
            nodes=tuple(
                NodeView(
                    node_id=node.node_id,
                    plant_id=node.plant_id,
                    code=node.code,
                    status=node.status,
                    live_view_local_url=node.live_view_local_url,
                )
                for node in view.nodes
            ),
        )

    @router.post(
        "/plants",
        status_code=201,
        dependencies=[requires(_MANAGE)],
        summary="Crear una planta con la génesis de su cadena",
    )
    async def create_plant(
        body: PlantBody, request: Request, response: Response, services: Services
    ) -> PlantView:
        no_store(response)
        try:
            plant = await installed(services.hierarchy).create_plant(
                request_context(request),
                model.PlantSpec(
                    code=body.code,
                    name=body.name,
                    country=body.country,
                    data_region=body.data_region,
                    timezone=body.timezone,
                ),
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        return _plant(plant)

    @router.post(
        "/plants/{plant_id}/zones",
        status_code=201,
        dependencies=[requires(_MANAGE)],
        summary="Crear una zona de la planta",
    )
    async def create_zone(
        plant_id: uuid.UUID,
        body: ZoneBody,
        request: Request,
        response: Response,
        services: Services,
    ) -> ZoneView:
        no_store(response)
        try:
            zone = await installed(services.hierarchy).create_zone(
                request_context(request), plant_id, model.ZoneSpec(code=body.code, name=body.name)
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        return _zone(zone)

    return router
