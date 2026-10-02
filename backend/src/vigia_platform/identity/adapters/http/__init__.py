"""Interfaz HTTP de ``identity`` para U-05 (``business-logic-model.md`` §10.2; TASK-135 y 136).

- ``auth``: inicio de sesión en dos pasos, inscripción del segundo factor con la sesión pendiente,
  cierre de sesión, sesiones propias, cerrar las demás y cambio de contraseña.
- ``invitations``: activación de la cuenta con el enlace de invitación.
- ``me``: ``GET /me`` y la aceptación del aviso de tratamiento.
- ``users``: usuarios y roles (``users.manage``, ``roles.manage``).
- ``hierarchy``: jerarquía (``hierarchy.read``, ``hierarchy.manage``).
- ``organization``: configuración de la organización (``organization.settings``).
- ``concessions``: concesiones del lado cliente y del lado proveedor (``concessions.*``).

Los enrutadores no reciben dependencias al construirse (la especificación se exporta sin red,
NFR-NUC-52): en cada petición toman los servicios de ``IdentityHttp`` en ``app.state``, que la raíz
de composición entrega en ``AppRuntime.identity``. Sin ellos, ``internal_error`` (fallo cerrado).
"""

from __future__ import annotations

from fastapi import APIRouter

from vigia_platform.identity.adapters.http.auth import auth_router
from vigia_platform.identity.adapters.http.concessions import concessions_router
from vigia_platform.identity.adapters.http.hierarchy import hierarchy_router
from vigia_platform.identity.adapters.http.invitations import invitations_router
from vigia_platform.identity.adapters.http.me import me_router
from vigia_platform.identity.adapters.http.organization import organization_router
from vigia_platform.identity.adapters.http.services import IDENTITY_STATE_KEY, IdentityHttp
from vigia_platform.identity.adapters.http.users import users_router

__all__ = ["IDENTITY_STATE_KEY", "IdentityHttp", "identity_routers"]


def identity_routers() -> tuple[APIRouter, ...]:
    """Los enrutadores de ``identity`` que registra ``platform_units()``."""
    return (
        auth_router(),
        invitations_router(),
        me_router(),
        users_router(),
        hierarchy_router(),
        organization_router(),
        concessions_router(),
    )
