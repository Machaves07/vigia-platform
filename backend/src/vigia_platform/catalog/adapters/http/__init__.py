"""Interfaz HTTP de ``catalog`` para U-05 (``interfaces-para-u04-u05.md`` §3.2).

- ``documents``: ``POST /documents`` (``commissioning.run``), concesión de subida de un documento
  firmado de planta (LC-GOB-05, VIG-143).

Como ``identity`` y ``ledger``, los enrutadores no reciben dependencias al construirse (la
especificación se exporta sin red, NFR-NUC-52): en cada petición toman su servicio de
``app.state``, que la unidad ``catalog`` deja con ``api_state``. Sin él, ``internal_error``.
"""

from __future__ import annotations

from fastapi import APIRouter

from vigia_platform.catalog.adapters.http.documents import (
    CATALOG_DOCUMENTS_STATE_KEY,
    documents_router,
)

__all__ = ["CATALOG_DOCUMENTS_STATE_KEY", "catalog_routers"]


def catalog_routers() -> tuple[APIRouter, ...]:
    """Los enrutadores de ``catalog`` que registra ``platform_units()``."""
    return (documents_router(),)
