"""Interfaz HTTP de ``fleet`` para U-05 (SCR-07; interfaces-para-u04-u05 §3.4 y «Versión 1.5»).

- ``nodes``: declaración (con reemplazo), zonas, códigos de alta, intentos, revocación y baja de
  un nodo (TASK-218).
- ``commissioning_clips``: ``GET /zones/{zone_id}/commissioning-clips`` (``catalog.read``), los
  clips de verificación de la zona para el selector del pase (nº 32, TASK-222).
- ``inventory``: ``GET /fleet/nodes`` y ``GET /fleet/nodes/{node_id}`` (``fleet.read``), el
  inventario con sus avisos y la historia de latidos (TASK-224).
- ``fleet_thresholds``: ``GET``/``PUT /plants/{plant_id}/fleet-thresholds`` (``fleet.read`` /
  ``fleet.manage``), los umbrales del panel por planta (TASK-224).
- ``fleet_alarms``: ``GET /fleet/alarms`` (``fleet.read``), las alarmas de flota por transición,
  activas y recientes (TASK-225).

Los enrutadores no reciben dependencias al construirse (la especificación se exporta sin red,
NFR-NUC-52): en cada petición toman los servicios de ``FleetHttp`` en ``app.state``, que la unidad
``fleet`` deja con ``PlatformUnit.api_state`` (A-52). Sin ellos, ``internal_error``.
"""

from __future__ import annotations

from fastapi import APIRouter

from vigia_platform.fleet.adapters.http.commissioning_clips import commissioning_clips_router
from vigia_platform.fleet.adapters.http.fleet_alarms import alarms_router
from vigia_platform.fleet.adapters.http.fleet_thresholds import fleet_thresholds_router
from vigia_platform.fleet.adapters.http.inventory import inventory_router
from vigia_platform.fleet.adapters.http.nodes import nodes_router
from vigia_platform.fleet.adapters.http.services import FLEET_STATE_KEY, FleetHttp

__all__ = ["FLEET_STATE_KEY", "FleetHttp", "fleet_routers"]


def fleet_routers() -> tuple[APIRouter, ...]:
    """Los enrutadores de ``fleet`` que registra ``platform_units()``."""
    return (
        nodes_router(),
        commissioning_clips_router(),
        inventory_router(),
        fleet_thresholds_router(),
        alarms_router(),
    )
