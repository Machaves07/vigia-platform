"""Cobertura mínima de una zona (BR-GOB-97 a 100; adenda A-03 y A-06; pendiente nº 34).

La definición que nodo y plataforma aplican igual (BR-GOB-100, BR-BOR-51 de U-06):

- la zona está ``observable`` cuando **todas** las cámaras de ``required_camera_ids`` están
  observables **y** al menos ``required_count`` cámaras de la zona están observables, sin perder
  ninguna;
- ``degraded`` si conserva eso pero perdió alguna cámara no requerida;
- ``not_observable`` si pierde una requerida o baja de ``required_count``.

Una cámara ``degraded`` o ``not_observable`` cuenta como no observable: el llamador pasa solo las
observables. Un identificador observable que no es cámara de la zona no cuenta.

``unsatisfiable_reason`` es la regla del catálogo (BR-GOB-11 y 98): las requeridas están en
``cameras``, sin repetir, y ``len(required_camera_ids) ≤ required_count ≤ len(cameras)`` (con
``required_count ≥ 1``, el mínimo del contrato).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from vigia_contracts.models.enumerations import ObservabilityState

__all__ = ["MinimumCoverage", "coverage_state", "unsatisfiable_reason"]


@dataclass(frozen=True, slots=True)
class MinimumCoverage:
    """``minimum_coverage`` del catálogo junto con las cámaras de la zona a la que se aplica."""

    required_count: int
    required_camera_ids: tuple[uuid.UUID, ...]
    camera_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        if type(self.required_count) is not int:
            raise TypeError("required_count debe ser int")
        for name in ("required_camera_ids", "camera_ids"):
            values = tuple(getattr(self, name))
            if any(type(value) is not uuid.UUID for value in values):
                raise TypeError(f"{name} debe contener uuid.UUID")
            object.__setattr__(self, name, values)

    @classmethod
    def of_catalog(cls, catalog: Mapping[str, Any]) -> MinimumCoverage:
        """La cobertura de un ``ZoneCatalog`` en su forma JSON."""
        coverage = catalog["minimum_coverage"]
        return cls(
            required_count=coverage["required_count"],
            required_camera_ids=tuple(uuid.UUID(v) for v in coverage["required_camera_ids"]),
            camera_ids=tuple(uuid.UUID(c["camera_id"]) for c in catalog["cameras"]),
        )


def unsatisfiable_reason(coverage: MinimumCoverage) -> str | None:
    """Por qué la cobertura no es satisfacible con sus cámaras, o ``None`` si lo es."""
    cameras = set(coverage.camera_ids)
    required = coverage.required_camera_ids
    if not cameras:
        return "sin cámaras"
    if len(set(required)) != len(required):
        return "una cámara requerida está repetida"
    if not set(required) <= cameras:
        return "una cámara requerida no es de la zona"
    if not max(1, len(required)) <= coverage.required_count <= len(cameras):
        return "required_count fuera de [cámaras requeridas, cámaras de la zona]"
    return None


def coverage_state(
    minimum_coverage: MinimumCoverage, observable_camera_ids: Iterable[uuid.UUID]
) -> ObservabilityState:
    """Estado de la zona con las cámaras ``observable_camera_ids`` observables (BR-GOB-97)."""
    cameras = set(minimum_coverage.camera_ids)
    observable = set(observable_camera_ids) & cameras
    if not set(minimum_coverage.required_camera_ids) <= observable:
        return ObservabilityState.NOT_OBSERVABLE
    if len(observable) < minimum_coverage.required_count:
        return ObservabilityState.NOT_OBSERVABLE
    if observable == cameras:
        return ObservabilityState.OBSERVABLE
    return ObservabilityState.DEGRADED
