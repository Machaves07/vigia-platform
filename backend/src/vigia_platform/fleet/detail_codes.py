"""``detail_code`` de la flota (pendiente nº 33; interfaces §4, BLM §4.1).

Cada error de negocio con nombre de las rutas de personas de la flota (declaración del nodo,
códigos de alta, revocación, baja y versión objetivo) viaja como ``ApiError.detail_code`` con el
prefijo ``fleet_``; ``code`` sigue siendo ``conflict`` o ``invalid_request`` de U-02
(``FLEET_API_ERROR_CODES``). El mensaje en español es la etiqueta de ``fleet_detail_code`` en
``labels.platform.es.json``. Las rutas del nodo usan solo los ``rejection_code`` del contrato.

Los avisos del inventario (``node_mute``, ``version_retiring``, ``queue_over_threshold``,
``clock_drift``, ``certificate_expiring``, ``simulated_adapter_in_productive``,
``fleet_version_pending``, ``update_reverted``) no son errores y no están aquí: son
``fleet_alarm_kind`` o estados del inventario.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Final

from vigia_platform.shared.api.errors import ApiErrorCode, DetailCodeRegistry

__all__ = [
    "FLEET_API_ERROR_CODES",
    "FLEET_DETAIL_CODE_LABEL_BINDINGS",
    "FleetDetailCode",
    "register_fleet_detail_codes",
]


class FleetDetailCode(enum.StrEnum):
    """Lista cerrada de ``detail_code`` del módulo ``fleet``."""

    NODE_NOT_DECLARED = "fleet_node_not_declared"
    NODE_UNREGISTERED = "fleet_node_unregistered"
    NODE_NOT_REVOKED = "fleet_node_not_revoked"
    ZONE_ALREADY_SERVED = "fleet_zone_already_served"
    ZONE_IN_OTHER_PLANT = "fleet_zone_in_other_plant"
    CODE_IN_USE = "fleet_code_in_use"
    REPLACED_NODE_NOT_FOUND = "fleet_replaced_node_not_found"
    VERSION_OUTSIDE_CONTRACT_WINDOW = "fleet_version_outside_contract_window"
    FREE_TEXT_REJECTED = "fleet_free_text_rejected"


FLEET_API_ERROR_CODES: Final[Mapping[FleetDetailCode, ApiErrorCode]] = {
    FleetDetailCode.NODE_NOT_DECLARED: ApiErrorCode.CONFLICT,
    FleetDetailCode.NODE_UNREGISTERED: ApiErrorCode.CONFLICT,
    FleetDetailCode.NODE_NOT_REVOKED: ApiErrorCode.CONFLICT,
    FleetDetailCode.ZONE_ALREADY_SERVED: ApiErrorCode.CONFLICT,
    FleetDetailCode.ZONE_IN_OTHER_PLANT: ApiErrorCode.INVALID_REQUEST,
    FleetDetailCode.CODE_IN_USE: ApiErrorCode.CONFLICT,
    FleetDetailCode.REPLACED_NODE_NOT_FOUND: ApiErrorCode.INVALID_REQUEST,
    FleetDetailCode.VERSION_OUTSIDE_CONTRACT_WINDOW: ApiErrorCode.INVALID_REQUEST,
    FleetDetailCode.FREE_TEXT_REJECTED: ApiErrorCode.INVALID_REQUEST,
}
"""``api_error_code`` de U-02 bajo el que viaja cada ``detail_code`` (BLM §4.1).

``fleet_replaced_node_not_found`` es ``invalid_request`` y no ``not_found``: el pendiente nº 33
solo admite ``conflict`` o ``invalid_request``, y el nodo nombrado está en el cuerpo, no en la
ruta (un recurso de la ruta fuera de alcance sigue siendo ``not_found``, BR-NUC-09)."""

FLEET_DETAIL_CODE_LABEL_BINDINGS: Final[Mapping[str, type[enum.Enum]]] = {
    "fleet_detail_code": FleetDetailCode,
}
"""Mensaje en español de cada ``detail_code``: el arranque falla si falta uno (NFR-GOB-67)."""


def register_fleet_detail_codes(registry: DetailCodeRegistry) -> None:
    """Registra los ``detail_code`` de la flota; ``ApiStartupError`` si alguno no cumple."""
    registry.register(code.value for code in FleetDetailCode)
