"""Interfaz HTTP de ``catalog`` para U-05 (SCR-04 y SCR-05; interfaces-para-u04-u05 §3).

- ``admissions``: prueba de admisión de tres preguntas (``catalog.manage``, ``catalog.read``).
- ``documents``: ``POST /documents`` (``commissioning.run``), concesión de subida de un documento
  firmado de planta (LC-GOB-05, VIG-143).
- ``gates``: estado de las compuertas (``catalog.read``), acta de alcance y revocación
  (``commissioning.run``) de una zona (LC-GOB-03, VIG-146).
- ``plant_policy``: política de hallazgos incerrables de la planta (``commissioning.run`` y
  ``catalog.read``; LC-GOB-03, VIG-146).

Los enrutadores no reciben dependencias al construirse (la especificación se exporta sin red,
NFR-NUC-52): en cada petición toman los servicios de ``CatalogHttp`` en ``app.state``, que la
unidad ``catalog`` deja con ``PlatformUnit.api_state`` (A-52). Sin ellos, ``internal_error``.
"""

from __future__ import annotations

from fastapi import APIRouter

from vigia_platform.catalog.adapters.http.admissions import admissions_router
from vigia_platform.catalog.adapters.http.documents import documents_router
from vigia_platform.catalog.adapters.http.gates import gates_router
from vigia_platform.catalog.adapters.http.plant_policy import plant_policy_router
from vigia_platform.catalog.adapters.http.services import CATALOG_STATE_KEY, CatalogHttp

__all__ = ["CATALOG_STATE_KEY", "CatalogHttp", "catalog_routers"]


def catalog_routers() -> tuple[APIRouter, ...]:
    """Los enrutadores de ``catalog`` que registra ``platform_units()``."""
    return (admissions_router(), documents_router(), gates_router(), plant_policy_router())
