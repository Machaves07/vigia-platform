"""``ZoneNodeState.coverage_ok``: la cobertura mínima de la zona vista desde el latido (BR-GOB-97).

La zona cumple su cobertura mínima cuando **todas** las cámaras de ``required_camera_ids`` están
observables **y** al menos ``required_count`` cámaras de la zona están observables en total; una
cámara ``degraded`` o ``not_observable`` cuenta como no observable (A-03, A-06). Es la definición
de ``catalog.domain.coverage`` (la misma que aplica el nodo, BR-GOB-100), sobre el catálogo
**vigente** de la zona y el estado de cada cámara que el nodo informa en el latido:

- ``observable`` o ``degraded`` (perdió solo cámaras no requeridas): ``coverage_ok``;
- ``not_observable``: no;
- sin catálogo vigente o con un catálogo sin cobertura legible: **no** (P2: la plataforma nunca
  afirma cobertura que no puede respaldar).

Módulo puro.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from vigia_contracts.models.enumerations import ObservabilityState

from vigia_platform.catalog.domain.coverage import MinimumCoverage, coverage_state

__all__ = ["CameraObservation", "zone_coverage_ok"]


@dataclass(frozen=True, slots=True)
class CameraObservation:
    """Lo que el latido dice de una cámara: su identificador y su estado de observabilidad."""

    camera_id: uuid.UUID
    observability_state: ObservabilityState


def _coverage(catalog: Mapping[str, Any] | None) -> MinimumCoverage | None:
    if catalog is None:
        return None
    try:
        return MinimumCoverage.of_catalog(catalog)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def zone_coverage_ok(
    catalog: Mapping[str, Any] | None, cameras: Iterable[CameraObservation]
) -> bool:
    """¿Cumple la zona su cobertura mínima con ``cameras``? ``catalog`` es el ``ZoneCatalog``
    vigente de la zona en su forma JSON (``None`` si no hay)."""
    coverage = _coverage(catalog)
    if coverage is None:
        return False
    observable = (
        camera.camera_id
        for camera in cameras
        if camera.observability_state is ObservabilityState.OBSERVABLE
    )
    return coverage_state(coverage, observable) is not ObservabilityState.NOT_OBSERVABLE
