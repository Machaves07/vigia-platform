"""``POST /fleet/target-versions`` (TASK-226; interfaces §3.4 y «Versión 1.5»; BR-GOB-101 y 102).

``fleet.manage`` sobre la planta. Cuerpo ``{plant_id, node_ids[] | group, target_version,
maintenance_window {from, to}}``:

- ``plant_id`` es la **planta del alcance**, sobre la que se autoriza: la interfaz no la nombra y
  ``group`` no puede resolverse sin ella (decisión declarada en TASK-226);
- ``node_ids`` (1 a 100, sin repetir) **o** ``group = "plant"``: todos los nodos no revocados ni
  dados de baja de la planta; la publicación guarda la lista resuelta;
- ``target_version``: ``MAJOR.MINOR.PATCH`` en minúsculas, dentro de la ventana de compatibilidad
  del contrato en el instante de publicar; si no, ``invalid_request`` con ``detail_code =
  fleet_version_outside_contract_window`` y no se publica nada;
- ``maintenance_window``: ``to > from``, informativa en el piloto (D-5).

``201`` con la publicación. Planta o nodo inexistente, de otra organización, de otra planta o fuera
del alcance: ``not_found`` sin publicar nada. Cuerpo mal formado, ``node_ids`` y ``group`` a la vez
(o ninguno), nodos repetidos, más de 100 o ventana vacía: ``invalid_request``. ``Cache-Control:
no-store``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr

from vigia_platform.fleet.adapters.http.services import FleetHttp, fleet_http, installed
from vigia_platform.fleet.application.common import FleetRejected, FleetWriteFailed
from vigia_platform.fleet.detail_codes import FleetDetailCode
from vigia_platform.fleet.domain.fleet_versions import (
    MAX_TARGET_NODES,
    NodeGroup,
    TargetVersionInvalid,
    TargetVersionPublication,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import body_limit, requires
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, from_ledger_rejection
from vigia_platform.shared.api.middleware import request_context
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["BODY_LIMIT_BYTES", "target_versions_router"]

BODY_LIMIT_BYTES: Final = 16_384
"""Cuerpo máximo `[objetivo propio]`: 100 nodos ocupan unos 4 KB."""
_MANAGE: Final = PermissionKey.FLEET_MANAGE.value
_CODES: Final = (FleetDetailCode.VERSION_OUTSIDE_CONTRACT_WINDOW.value,)

Services = Annotated[FleetHttp, Depends(fleet_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MaintenanceWindowBody(_Strict):
    """``{from, to}``: ``from`` es palabra reservada de Python, de ahí el alias."""

    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime


class TargetVersionBody(_Strict):
    plant_id: uuid.UUID
    node_ids: tuple[uuid.UUID, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_TARGET_NODES
    )
    group: NodeGroup | None = None
    target_version: StrictStr = Field(max_length=64)
    maintenance_window: MaintenanceWindowBody


class MaintenanceWindowView(_Strict):
    from_: str = Field(serialization_alias="from")
    to: str


class TargetVersionPublicationView(_Strict):
    publication_id: uuid.UUID
    plant_id: uuid.UUID
    target_version: str
    node_ids: tuple[uuid.UUID, ...]
    maintenance_window: MaintenanceWindowView
    published_by: uuid.UUID
    published_at: str
    ledger_record_id: uuid.UUID


def _view(publication: TargetVersionPublication) -> TargetVersionPublicationView:
    return TargetVersionPublicationView(
        publication_id=publication.publication_id,
        plant_id=publication.plant_id,
        target_version=publication.target_version,
        node_ids=publication.node_ids,
        maintenance_window=MaintenanceWindowView(
            **{
                "from_": format_timestamp(publication.window.starts_at),
                "to": format_timestamp(publication.window.ends_at),
            }
        ),
        published_by=publication.published_by,
        published_at=format_timestamp(publication.published_at),
        ledger_record_id=publication.ledger_record_id,
    )


def target_versions_router() -> APIRouter:
    router = APIRouter(tags=["flota"])

    @router.post(
        "/fleet/target-versions",
        status_code=201,
        dependencies=[requires(_MANAGE, detail_codes=_CODES), body_limit(BODY_LIMIT_BYTES)],
        response_model_by_alias=True,
        summary="Publica la versión objetivo de nodos de una planta (ventana informativa)",
    )
    async def publish_target_version(
        body: TargetVersionBody, request: Request, response: Response, services: Services
    ) -> TargetVersionPublicationView:
        response.headers["Cache-Control"] = "no-store"
        service = installed(services.target_versions)
        try:
            publication = await service.publish(
                request_context(request),
                body.plant_id,
                node_ids=body.node_ids,
                group=body.group,
                target_version=body.target_version,
                window_from=body.maintenance_window.from_,
                window_to=body.maintenance_window.to,
            )
        except TargetVersionInvalid:
            raise ApiError(ApiErrorCode.INVALID_REQUEST) from None
        except FleetRejected as error:
            raise ApiError(error.api_code, detail_code=error.detail_code.value) from None
        except FleetWriteFailed as error:
            raise from_ledger_rejection(error.rejection) from None
        return _view(publication)

    return router
