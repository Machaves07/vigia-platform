"""Servicios de las rutas de ``catalog`` (``app.state``) y su construcción por la raíz.

``CatalogHttp`` lo deja la unidad ``catalog`` en ``app.state`` (``PlatformUnit.api_state``,
A-52), construido con la infraestructura común (``UnitServices``). Las rutas lo toman en cada
petición: sin él, ``internal_error`` (fallo cerrado, nunca deja pasar). Los servicios son
opcionales para que cada grupo de rutas se pueda montar por separado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Request

from vigia_platform.catalog.application.admission import AdmissionService
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["CATALOG_STATE_KEY", "CatalogHttp", "catalog_http", "installed"]

CATALOG_STATE_KEY: Final = "vigia_catalog_http"

_log = get_logger("catalog.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogHttp:
    """Los servicios que usan las rutas de ``catalog``."""

    admissions: AdmissionService | None = None


def installed[T](service: T | None) -> T:
    """El servicio de la ruta; sin él, ``internal_error`` (nunca deja pasar)."""
    if service is None:
        _log.error("la ruta del catálogo no tiene su servicio instalado")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


def catalog_http(request: Request) -> CatalogHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error``."""
    services = getattr(request.app.state, CATALOG_STATE_KEY, None)
    if not isinstance(services, CatalogHttp):
        _log.error("las rutas del catálogo no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services
