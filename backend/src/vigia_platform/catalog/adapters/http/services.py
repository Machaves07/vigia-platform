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
from vigia_platform.catalog.application.agreements import AgreementService
from vigia_platform.catalog.application.documents import DocumentService
from vigia_platform.catalog.application.gates import GateService
from vigia_platform.catalog.application.occlusion import OcclusionService
from vigia_platform.catalog.application.plant_policy import PlantPolicyService
from vigia_platform.catalog.application.publication import CatalogPublicationService
from vigia_platform.catalog.application.regression import RegressionService
from vigia_platform.catalog.application.scope_record import ScopeRecordService
from vigia_platform.catalog.application.signatory_policy import SignatoryPolicyService
from vigia_platform.catalog.application.transparency import TransparencyService
from vigia_platform.catalog.application.walk_test import WalkTestService
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger

__all__ = ["CATALOG_STATE_KEY", "CatalogHttp", "catalog_http", "installed"]

CATALOG_STATE_KEY: Final = "vigia_catalog_http"

_log = get_logger("catalog.http")


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogHttp:
    """Los servicios que usan las rutas de ``catalog``."""

    admissions: AdmissionService | None = None
    documents: DocumentService | None = None
    """``POST /documents`` (LC-GOB-05, VIG-143)."""
    gates: GateService | None = None
    """``GET /zones/{zone_id}/gates`` y la revocación (LC-GOB-03, VIG-146)."""
    scope_records: ScopeRecordService | None = None
    """Acta de alcance de la compuerta de montaje (LC-GOB-03, VIG-146)."""
    plant_policies: PlantPolicyService | None = None
    """``POST`` y ``GET /plants/{plant_id}/policy`` (LC-GOB-03, VIG-146)."""
    catalog: CatalogPublicationService | None = None
    """Catálogo de la zona, estándares y parámetros (LC-GOB-01, VIG-148)."""
    regression: RegressionService | None = None
    """``GET /zones/{zone_id}/regression`` y la recaptura del encuadre (LC-GOB-09, VIG-148)."""
    signatory_policies: SignatoryPolicyService | None = None
    """``PUT`` y ``GET /plants/{plant_id}/signatory-policy`` (LC-GOB-04, VIG-149)."""
    agreements: AgreementService | None = None
    """Acuerdo de uso: alta, confirmación y aprobación (LC-GOB-04, VIG-149)."""
    transparency: TransparencyService | None = None
    """``GET /zones/{zone_id}/transparency`` (LC-GOB-04, VIG-149)."""
    walk_tests: WalkTestService | None = None
    """Sesión de walk-test, pasos, pases y reapertura (LC-GOB-06, VIG-150)."""
    occlusions: OcclusionService | None = None
    """Prueba de oclusión y su reevaluación perezosa (LC-GOB-07, VIG-154)."""


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
