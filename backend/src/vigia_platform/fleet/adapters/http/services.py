"""Servicios de las rutas de ``fleet`` (``app.state``) y su construcción por la raíz.

``FleetHttp`` lo deja la unidad ``fleet`` en ``app.state`` (``PlatformUnit.api_state``, A-52),
construido con la infraestructura común (``UnitServices``). Las rutas lo toman en cada petición:
sin él, ``internal_error`` (fallo cerrado, nunca deja pasar).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Request

from vigia_platform.fleet.application.clip_confirmation import CommissioningClips
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["FLEET_STATE_KEY", "FleetHttp", "fleet_http", "installed"]

FLEET_STATE_KEY: Final = "vigia_fleet_http"

_log = get_logger("fleet.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class FleetHttp:
    """Los servicios que usan las rutas de ``fleet``."""

    commissioning_clips: CommissioningClips | None = None
    """``GET /zones/{zone_id}/commissioning-clips`` (TASK-222)."""


def installed[T](service: T | None) -> T:
    """El servicio de la ruta; sin él, ``internal_error`` (nunca deja pasar)."""
    if service is None:
        _log.error("la ruta de la flota no tiene su servicio instalado")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


def fleet_http(request: Request) -> FleetHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error``."""
    services = getattr(request.app.state, FLEET_STATE_KEY, None)
    if not isinstance(services, FleetHttp):
        _log.error("las rutas de la flota no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services
