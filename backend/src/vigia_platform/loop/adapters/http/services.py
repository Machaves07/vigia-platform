"""Servicios de las rutas de ``loop`` (``app.state``) y su construcción por la raíz.

``LoopHttp`` lo deja la unidad ``loop`` en ``app.state`` (``API_STATE`` de
``loop.registration``, A-52), construido con la infraestructura común (``UnitServices``). Las rutas
lo toman en cada petición: sin él, ``internal_error`` (fallo cerrado, nunca deja pasar). Los
servicios son opcionales para que cada grupo de rutas se pueda montar por separado; cada tarea de
U-04 que añade rutas añade aquí su servicio.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastapi import Request

from vigia_platform.loop.application.actions import ActionService
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.evidence import EvidenceService
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.config.service import LoopConfigurationService
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger

if TYPE_CHECKING:
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "LOOP_STATE_KEY",
    "LoopHttp",
    "build_loop_http",
    "installed",
    "loop_http",
    "loop_http_state",
]

LOOP_STATE_KEY: Final = "vigia_loop_http"

_log = get_logger("loop.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopHttp:
    """Los servicios que usan las rutas de ``loop``."""

    configuration: LoopConfigurationService | None = None
    """``GET`` y ``PUT /loop/plants/{plant_id}/configuration`` (LC-LAZ-19, VIG-205)."""
    classification: ClassificationService | None = None
    """``POST /loop/findings/{finding_state_id}/classifications`` (LC-LAZ-07, VIG-212)."""
    actions: ActionService | None = None
    """``POST /loop/findings/{finding_state_id}/actions`` (LC-LAZ-09, VIG-223)."""
    evidence: EvidenceService | None = None
    """``POST /loop/actions/{action_id}/evidence`` (LC-LAZ-09, VIG-223); los adjuntos los
    verifica ``ClosureAttachmentPort`` de ``app.state`` (TASK-310)."""


def installed[T](service: T | None) -> T:
    """El servicio de la ruta; sin él, ``internal_error`` (nunca deja pasar)."""
    if service is None:
        _log.error("la ruta del lazo no tiene su servicio instalado")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return service


def loop_http(request: Request) -> LoopHttp:
    """Los servicios de la aplicación; sin ellos, ``internal_error``."""
    services = getattr(request.app.state, LOOP_STATE_KEY, None)
    if not isinstance(services, LoopHttp):
        _log.error("las rutas del lazo no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services


def build_loop_http(services: UnitServices) -> LoopHttp:
    """``LoopHttp`` sobre la infraestructura común de la raíz."""
    return LoopHttp(
        configuration=LoopConfigurationService(
            repository=PostgresLoopConfigurationRepository(services.database),
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            free_text=services.free_text,
            clock=services.clock,
        ),
        classification=ClassificationService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
        ),
        actions=ActionService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
        ),
        evidence=EvidenceService(
            database=services.database,
            writer=services.writer,
            authorizer=services.authorizer,
            clock=services.clock,
        ),
    )


def loop_http_state(services: UnitServices) -> Mapping[str, object]:
    """La entrada de ``app.state`` de la unidad (``API_STATE``)."""
    return {LOOP_STATE_KEY: build_loop_http(services)}
