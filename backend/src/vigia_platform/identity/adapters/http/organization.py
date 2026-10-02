"""Configuración de la organización (``business-logic-model.md`` §10.2; domain-entities §2.1).

Con ``organization.settings`` sobre la organización (``administrator`` y ``plant_manager`` de
nivel organización):

- ``GET /organization/settings``: código, nombre, tipo, estado y los topes de concesión
  (``concession_max_days`` 1 a 90 y ``concession_default_days`` 1 a ``concession_max_days``).
- ``PATCH /organization/settings``: cambia el nombre y los topes; valen para las concesiones
  nuevas (BR-NUC-35). Un resultado con ``concession_default_days > concession_max_days`` responde
  ``invalid_request`` y no cambia nada. Queda auditado como ``organization_settings_changed``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from vigia_platform.identity.adapters.http.services import (
    IdentityHttp,
    identity_error,
    identity_http,
    installed,
    no_store,
)
from vigia_platform.identity.application.common import IdentityRejected
from vigia_platform.identity.application.organization import (
    MAX_CONCESSION_DAYS,
    OrganizationSettings,
    SettingsChange,
)
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.declarations import requires
from vigia_platform.shared.api.middleware import request_context

__all__ = ["organization_router"]

Services = Annotated[IdentityHttp, Depends(identity_http)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SettingsBody(_Strict):
    name: StrictStr | None = Field(default=None, min_length=1, max_length=120)
    concession_max_days: StrictInt | None = Field(default=None, ge=1, le=MAX_CONCESSION_DAYS)
    concession_default_days: StrictInt | None = Field(default=None, ge=1, le=MAX_CONCESSION_DAYS)


class SettingsView(_Strict):
    organization_id: uuid.UUID
    code: str
    name: str
    kind: str
    status: str
    concession_max_days: int
    concession_default_days: int


def _view(settings: OrganizationSettings) -> SettingsView:
    return SettingsView(
        organization_id=settings.organization_id,
        code=settings.code,
        name=settings.name,
        kind=settings.kind,
        status=settings.status,
        concession_max_days=settings.concession_max_days,
        concession_default_days=settings.concession_default_days,
    )


_SETTINGS: Final = PermissionKey.ORGANIZATION_SETTINGS.value


def organization_router() -> APIRouter:
    router = APIRouter(tags=["organización"])

    @router.get(
        "/organization/settings",
        dependencies=[requires(_SETTINGS)],
        summary="Datos y topes de concesión de la organización",
    )
    async def read_settings(
        request: Request, response: Response, services: Services
    ) -> SettingsView:
        no_store(response)
        settings = await installed(services.organization).settings(request_context(request))
        return _view(settings)

    @router.patch(
        "/organization/settings",
        dependencies=[requires(_SETTINGS)],
        summary="Cambiar el nombre y los topes de concesión",
    )
    async def update_settings(
        body: SettingsBody, request: Request, response: Response, services: Services
    ) -> SettingsView:
        no_store(response)
        try:
            settings = await installed(services.organization).update(
                request_context(request),
                SettingsChange(
                    name=body.name,
                    concession_max_days=body.concession_max_days,
                    concession_default_days=body.concession_default_days,
                ),
            )
        except IdentityRejected as error:
            raise identity_error(error) from None
        return _view(settings)

    return router
