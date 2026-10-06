"""Servicios de las rutas de ``fleet`` (``app.state``) (TASK-218, 222, 224 y 225; A-52).

``FleetHttp`` lo deja la unidad ``fleet`` en ``app.state`` (``PlatformUnit.api_state``),
construido con la infraestructura común (``UnitServices``). Las rutas lo toman en cada petición:
sin él, ``internal_error`` (fallo cerrado, nunca deja pasar).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fastapi import Request

from vigia_platform.fleet.application.clip_confirmation import CommissioningClips
from vigia_platform.fleet.application.enrollment_codes import EnrollmentCodeService
from vigia_platform.fleet.application.fleet_alarms import FleetAlarms
from vigia_platform.fleet.application.fleet_thresholds import FleetThresholdsService
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.fleet.application.node_declaration import NodeDeclarationService
from vigia_platform.fleet.application.node_revocation import NodeRevocationService
from vigia_platform.fleet.application.target_versions import TargetVersionService
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["FLEET_STATE_KEY", "FleetHttp", "fleet_http", "installed"]

FLEET_STATE_KEY: Final = "vigia_fleet_http"

_log = get_logger("fleet.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class FleetHttp:
    """Los servicios que usan las rutas de ``fleet``."""

    declarations: NodeDeclarationService | None = None
    enrollment_codes: EnrollmentCodeService | None = None
    revocations: NodeRevocationService | None = None
    commissioning_clips: CommissioningClips | None = None
    """``GET /zones/{zone_id}/commissioning-clips`` (TASK-222)."""
    inventory: FleetInventory | None = None
    """``GET /fleet/nodes`` y ``GET /fleet/nodes/{node_id}`` (TASK-224)."""
    thresholds: FleetThresholdsService | None = None
    """``GET`` y ``PUT /plants/{plant_id}/fleet-thresholds`` (TASK-224)."""
    target_versions: TargetVersionService | None = None
    """``POST /fleet/target-versions`` (TASK-226)."""
    alarms: FleetAlarms | None = None
    """``GET /fleet/alarms`` (TASK-225)."""


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
