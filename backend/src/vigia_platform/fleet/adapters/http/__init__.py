"""Interfaz HTTP de ``fleet`` para U-05 (interfaces-para-u04-u05 «Versión 1.5»).

- ``commissioning_clips``: ``GET /zones/{zone_id}/commissioning-clips`` (``catalog.read``), los
  clips de verificación de la zona para el selector del pase (nº 32, TASK-222).

Los enrutadores no reciben dependencias al construirse (la especificación se exporta sin red,
NFR-NUC-52): en cada petición toman sus servicios de ``FleetHttp`` en ``app.state``, que la unidad
``fleet`` deja con ``PlatformUnit.api_state`` (A-52). Sin ellos, ``internal_error``.
"""

from __future__ import annotations

from fastapi import APIRouter

from vigia_platform.fleet.adapters.http.commissioning_clips import commissioning_clips_router
from vigia_platform.fleet.adapters.http.services import FLEET_STATE_KEY, FleetHttp

__all__ = ["FLEET_STATE_KEY", "FleetHttp", "fleet_routers"]


def fleet_routers() -> tuple[APIRouter, ...]:
    """Los enrutadores de ``fleet`` que registra ``platform_units()``."""
    return (commissioning_clips_router(),)
