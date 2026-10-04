"""Matriz del walk-test derivada del catálogo (respuesta 9; BR-GOB-36; nota de TASK-208).

Con la gramática v1 un ``Predicate`` es una conjunción ``all_of``: cada estándar vigente aporta
**una** combinación de condiciones, que se cruza con las cuatro posturas (``standing``,
``crouched``, ``partially_occluded``, ``slow_movement``). La matriz tiene 4 filas por estándar;
con el máximo del contrato (32 estándares) son 128, la «matriz máxima» de NFR-GOB-05.

El ``row_id`` es **determinista**: UUID v5 de ``{standard_id, standard_version, condiciones
canónicas, postura}``. La misma fila tiene el mismo identificador en cualquier versión del
catálogo y en la sesión de reejecución, y cambia si cambian la versión del estándar o sus
condiciones; así se comparan las filas afectadas por un cambio (TASK-209) con las que cubre una
sesión (TASK-214, PR-GOB-13).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from vigia_platform.catalog.domain.enums import Posture
from vigia_platform.catalog.domain.predicates import canonical_conditions

__all__ = ["MATRIX_ROW_NAMESPACE", "POSTURES", "MatrixRow", "derive_matrix", "matrix_row_id"]

MATRIX_ROW_NAMESPACE: Final = uuid.UUID("6f1c2d0e-8a4b-5c7d-9e21-3b5a7c9d1e40")
"""Espacio de nombres de los ``row_id`` (constante: cambiarla cambiaría todas las filas)."""
POSTURES: Final = (
    Posture.STANDING,
    Posture.CROUCHED,
    Posture.PARTIALLY_OCCLUDED,
    Posture.SLOW_MOVEMENT,
)
"""Las cuatro posturas, en el orden en que aparecen las filas de cada estándar."""


@dataclass(frozen=True, slots=True)
class MatrixRow:
    """Una fila: un estándar en una versión, su combinación de condiciones y una postura."""

    row_id: uuid.UUID
    standard_id: uuid.UUID
    standard_version: int
    conditions: tuple[tuple[str, str], ...]
    posture: Posture


def matrix_row_id(
    standard_id: uuid.UUID,
    standard_version: int,
    conditions: tuple[tuple[str, str], ...],
    posture: Posture,
) -> uuid.UUID:
    """El ``row_id`` determinista de la fila (UUID v5 sobre una forma canónica)."""
    name = json.dumps(
        {
            "standard_id": str(standard_id),
            "standard_version": standard_version,
            "conditions": [list(condition) for condition in sorted(conditions)],
            "posture": Posture(posture).value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(MATRIX_ROW_NAMESPACE, name)


def derive_matrix(catalog: Mapping[str, Any]) -> tuple[MatrixRow, ...]:
    """Las filas de la matriz del ``ZoneCatalog`` (forma JSON), ordenadas por estándar y postura."""
    rows: list[MatrixRow] = []
    standards = sorted(catalog["standards"], key=lambda s: (s["standard_id"], s["version"]))
    for standard in standards:
        standard_id = uuid.UUID(standard["standard_id"])
        version = int(standard["version"])
        conditions = canonical_conditions(standard["predicate"])
        for posture in POSTURES:
            rows.append(
                MatrixRow(
                    row_id=matrix_row_id(standard_id, version, conditions, posture),
                    standard_id=standard_id,
                    standard_version=version,
                    conditions=conditions,
                    posture=posture,
                )
            )
    return tuple(rows)
