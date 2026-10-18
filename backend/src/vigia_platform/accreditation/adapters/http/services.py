"""Servicios de las rutas de ``accreditation`` (``app.state``) (TASK-317; A-52).

``AccreditationHttp`` lo deja la unidad ``accreditation`` en ``app.state`` (``API_STATE`` de
``accreditation.registration``), construido con la infraestructura común (``UnitServices``). Las
rutas lo toman en cada petición: sin él, ``internal_error`` (fallo cerrado, nunca deja pasar).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Request

from vigia_platform.accreditation.export.download import ExportDownloads
from vigia_platform.accreditation.export.request import ExportRequests
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["ACCREDITATION_STATE_KEY", "AccreditationHttp", "accreditation_http", "installed"]

ACCREDITATION_STATE_KEY: Final = "vigia_accreditation_http"

_log = get_logger("accreditation.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class AccreditationHttp:
    """Los servicios que usan las rutas de ``accreditation``."""

    exports: ExportRequests | None = None
    """``POST /accreditation/exports`` y ``GET /accreditation/exports/{export_id}`` (TASK-317)."""
    downloads: ExportDownloads | None = None
    """``GET /accreditation/exports/{export_id}/download`` y ``POST …/download-url`` (TASK-345);
    sin depósito de evidencias, ``None`` y las dos responden ``internal_error``."""


def installed[T](service: T | None) -> T:
    """El servicio de la ruta; sin él, ``internal_error`` (nunca deja pasar)."""
    if service is None:
        _log.error("la ruta de la acreditación no tiene su servicio instalado")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


def accreditation_http(request: Request) -> AccreditationHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error``."""
    services = getattr(request.app.state, ACCREDITATION_STATE_KEY, None)
    if not isinstance(services, AccreditationHttp):
        _log.error("las rutas de la acreditación no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services
