"""Interfaz HTTP de ``ledger`` para U-05 y los verificadores (``business-logic-model.md`` §10.2;
TASK-137).

- ``records``: ``GET /ledger/records``, ``GET /ledger/records/{record_id}`` (permiso según el tipo
  consultado) y ``GET /audit/entries`` (``audit.read``).
- ``evidence``: ``POST /evidence/{evidence_id}/read-url`` (``evidence.read``).
- ``labels``: ``GET /labels`` (``labels.read``).
- ``coverage``: ``GET /zones/{zone_id}/coverage`` (tope de 31 días) y ``…/coverage/at``
  (``coverage.read``).
- ``integrity``: ``POST /integrity/verify`` (en el worker), ``GET /integrity/results`` y
  ``GET /integrity/checkpoints`` (``integrity.verify``).
- ``live_view``: ``POST /zones/{zone_id}/live-view-token`` (``live_view.open``).

Como ``identity``, los enrutadores no reciben dependencias al construirse (la especificación se
exporta sin red, NFR-NUC-52): en cada petición toman ``LedgerHttp`` de ``app.state``, que la raíz
de composición entrega en ``AppRuntime.ledger``. Sin él, ``internal_error`` (fallo cerrado).
"""

from __future__ import annotations

from fastapi import APIRouter

from vigia_platform.ledger.adapters.http.coverage import coverage_router
from vigia_platform.ledger.adapters.http.evidence import evidence_router
from vigia_platform.ledger.adapters.http.integrity import integrity_router
from vigia_platform.ledger.adapters.http.labels import labels_router
from vigia_platform.ledger.adapters.http.live_view import live_view_router
from vigia_platform.ledger.adapters.http.records import records_router
from vigia_platform.ledger.adapters.http.services import LEDGER_STATE_KEY, LedgerHttp

__all__ = ["LEDGER_STATE_KEY", "LedgerHttp", "ledger_routers"]


def ledger_routers() -> tuple[APIRouter, ...]:
    """Los enrutadores de ``ledger`` que registra ``platform_units()``."""
    return (
        records_router(),
        evidence_router(),
        labels_router(),
        coverage_router(),
        integrity_router(),
        live_view_router(),
    )
