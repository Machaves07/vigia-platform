"""``WalkTestRegression`` y el cálculo puro de la marca (DE §2.16 y su nota; BR-GOB-51 a 56).

La regresión es una **marca sin bloqueo operativo**: dice que los compromisos del acta del
walk-test dejan de estar vigentes en la zona hasta que una reejecución cubra las filas afectadas
(TASK-216). La zona sigue ``productive``, las compuertas no se tocan y la ingesta continúa
(BR-GOB-53). Una fila por zona: ``current`` (acta vigente, o zona que nunca se marcó) o
``pending``.

**La marca la calcula el sistema** a partir de ``changed_fields`` y de la matriz derivada del
catálogo (``derive_matrix`` y su ``row_id`` determinista), nunca a partir de un dato del cliente
(BR-GOB-52, 56). ``publication_rows`` aplica la tabla de TASK-209:

=============================================================  ==============================
Cambio                                                          Filas afectadas
=============================================================  ==============================
estándar nuevo                                                  las filas de ese estándar
versión de estándar con el predicado cambiado                   las filas de ese estándar
versión de estándar que solo cambia ``title_es`` o el texto     ninguna: no marca
``cameras`` o ``minimum_coverage`` (BR-GOB-52)                  ``all``
``thresholds``, ``signals``, ``clip_window`` o ``episode``      ``all`` (nota del redactor)
retiro de un estándar                                           ``all`` (nota del redactor)
``single_occupancy``                                            ninguna: no marca
la primera versión de la zona                                   ninguna: aún no hay acta
=============================================================  ==============================

El encuadre no es campo del catálogo: lo marca el instalador al recapturar la línea base
(``framing_recaptured``, nota U03-H-14) y un cambio de ``model_version`` lo marca el latido
(``model_version_change``); los dos con la matriz completa.

**Unión** (``merged``): si la zona ya estaba ``pending``, las filas se unen (``all`` absorbe), la
causa pasa a la de la última marca y ``marked_at`` conserva el primer instante del periodo.

**Arrastre** (``carried_forward``): el ``row_id`` incluye la versión del estándar, así que una
versión nueva cambia los identificadores de sus filas aunque no marque. Las filas pendientes se
llevan a la matriz de la versión nueva por ``(standard_id, posture)``: así la reejecución, que
deriva su matriz del catálogo vigente, puede cubrirlas. Una fila sin equivalente (su estándar ya
no está) deja la marca en ``all``: nunca se pierde una fila pendiente.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from vigia_platform.catalog.domain.enums import (
    CatalogChangedField,
    RegressionCause,
    RegressionState,
)
from vigia_platform.catalog.domain.matrix import derive_matrix
from vigia_platform.catalog.domain.predicates import canonical_conditions

__all__ = [
    "ALL_ROWS",
    "MAX_AFFECTED_ROWS",
    "ZONE_WIDE_FIELDS",
    "AffectedRows",
    "RegressionMark",
    "WalkTestRegression",
    "carried_forward",
    "merged",
    "predicate_changed",
    "publication_rows",
]

ALL_ROWS: Final = "all"
"""La matriz completa: absorbe cualquier lista de filas."""
MAX_AFFECTED_ROWS: Final = 1024
"""Tope de filas de una marca (``MAX_MATRIX_ROWS`` de los registros); más, y la marca es ``all``."""
ZONE_WIDE_FIELDS: Final = frozenset(
    {
        CatalogChangedField.CAMERAS,
        CatalogChangedField.MINIMUM_COVERAGE,
        CatalogChangedField.SIGNALS,
        CatalogChangedField.THRESHOLDS,
        CatalogChangedField.CLIP_WINDOW,
        CatalogChangedField.EPISODE,
    }
)
"""Parámetros de toda la zona: su cambio afecta a la matriz completa."""

AffectedRows = tuple[uuid.UUID, ...] | Literal["all"]
"""Filas afectadas: ``row_id`` ordenados y sin repetir, o ``"all"``."""


def _rows(value: object) -> AffectedRows:
    if value == ALL_ROWS:
        return ALL_ROWS
    if not isinstance(value, tuple | list | frozenset | set):
        raise TypeError("affected_row_ids es una lista de row_id o «all»")
    rows = frozenset(value)
    if not rows or any(type(row) is not uuid.UUID for row in rows):
        raise ValueError("affected_row_ids lleva de 1 a 1024 row_id")
    if len(rows) > MAX_AFFECTED_ROWS:
        return ALL_ROWS
    return tuple(sorted(rows))


@dataclass(frozen=True, slots=True, kw_only=True)
class RegressionMark:
    """Una marca: su causa, sus filas, la versión que la disparó y su instante."""

    cause: RegressionCause
    affected_row_ids: AffectedRows
    marked_at: datetime
    catalog_version: int | None = None
    model_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cause", RegressionCause(self.cause))
        object.__setattr__(self, "affected_row_ids", _rows(self.affected_row_ids))
        if self.marked_at.utcoffset() is None:
            raise ValueError("marked_at debe llevar zona horaria")
        if self.cause is RegressionCause.CATALOG_CHANGE and self.catalog_version is None:
            raise ValueError("catalog_change nombra la versión del catálogo que la disparó")
        if self.cause is RegressionCause.MODEL_VERSION_CHANGE and self.model_version is None:
            raise ValueError("model_version_change nombra la versión del modelo que la disparó")
        if self.cause is not RegressionCause.CATALOG_CHANGE and self.affected_row_ids != ALL_ROWS:
            raise ValueError("el encuadre y el modelo afectan a la matriz completa (BR-GOB-52)")


@dataclass(frozen=True, slots=True, kw_only=True)
class WalkTestRegression:
    """La fila de la zona (DE §2.16); ``ledger_record_id`` es nulo solo si nunca se marcó."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    state: RegressionState
    marked_at: datetime | None = None
    cause: RegressionCause | None = None
    catalog_version: int | None = None
    model_version: str | None = None
    affected_row_ids: AffectedRows | None = None
    cleared_at: datetime | None = None
    cleared_by_session_id: uuid.UUID | None = None
    ledger_record_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        for name in ("organization_id", "plant_id", "zone_id"):
            if type(getattr(self, name)) is not uuid.UUID:
                raise TypeError(f"{name} debe ser uuid.UUID")
        object.__setattr__(self, "state", RegressionState(self.state))
        if self.cause is not None:
            object.__setattr__(self, "cause", RegressionCause(self.cause))
        if self.affected_row_ids is not None:
            object.__setattr__(self, "affected_row_ids", _rows(self.affected_row_ids))
        if self.pending and (
            self.marked_at is None or self.cause is None or self.affected_row_ids is None
        ):
            raise ValueError("una regresión pendiente lleva marked_at, cause y affected_row_ids")
        if (self.cleared_at is None) != (self.cleared_by_session_id is None):
            raise ValueError("cleared_at y cleared_by_session_id van juntos")

    @property
    def pending(self) -> bool:
        return self.state is RegressionState.PENDING

    @classmethod
    def initial(
        cls, organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID
    ) -> WalkTestRegression:
        """La zona que nunca se marcó: ``current`` sin causa ni filas."""
        return cls(
            organization_id=organization_id,
            plant_id=plant_id,
            zone_id=zone_id,
            state=RegressionState.CURRENT,
        )


# --- Cálculo -----------------------------------------------------------------------------------


def _predicate_key(standard: Mapping[str, Any]) -> tuple[Any, ...]:
    predicate = standard["predicate"]
    return canonical_conditions(predicate), int(predicate["min_duration_ms"])


def predicate_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """¿Cambió el predicado del estándar? (condiciones sin orden y duración mínima)."""
    return _predicate_key(before) != _predicate_key(after)


def _standards(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(standard["standard_id"]): standard for standard in catalog["standards"]}


def publication_rows(
    previous: Mapping[str, Any] | None,
    new: Mapping[str, Any],
    changed_fields: Iterable[CatalogChangedField],
) -> AffectedRows | None:
    """Las filas que la versión ``new`` (sobre ``previous``) deja afectadas; ``None`` si no marca.

    ``previous`` y ``new`` son ``ZoneCatalog`` en forma JSON. Solo cuenta lo que dicen los dos
    catálogos y ``changed_fields``: el autor del cambio no puede ocultar ni fijar la marca.
    """
    if previous is None:
        # Versión 1: no hay acta que deje de regir; el walk-test inicial cubre toda la matriz.
        return None
    fields = {CatalogChangedField(field) for field in changed_fields}
    if fields & ZONE_WIDE_FIELDS:
        return ALL_ROWS
    if CatalogChangedField.STANDARDS not in fields:
        return None  # solo la marca unipersonal: no toca la matriz
    before, after = _standards(previous), _standards(new)
    if set(before) - set(after):
        return ALL_ROWS  # retiro de un estándar
    touched = {
        standard_id
        for standard_id, standard in after.items()
        if standard_id not in before or predicate_changed(before[standard_id], standard)
    }
    if not touched:
        return None  # solo cambiaron textos
    rows = tuple(row.row_id for row in derive_matrix(new) if str(row.standard_id) in touched)
    return _rows(rows)


def carried_forward(
    affected: AffectedRows, previous: Mapping[str, Any], new: Mapping[str, Any]
) -> AffectedRows:
    """Las filas pendientes de la matriz de ``previous`` llevadas a la de ``new``.

    Cada fila se busca por ``(standard_id, posture)``; si alguna no tiene equivalente, ``all``.
    """
    if affected == ALL_ROWS:
        return ALL_ROWS
    old = {row.row_id: (row.standard_id, row.posture) for row in derive_matrix(previous)}
    now = {(row.standard_id, row.posture): row.row_id for row in derive_matrix(new)}
    carried: list[uuid.UUID] = []
    for row_id in affected:
        key = old.get(row_id)
        if key is None:
            # Ya era de la matriz vigente (otra marca en la misma versión) o de una anterior.
            if row_id in now.values():
                carried.append(row_id)
                continue
            return ALL_ROWS
        if key not in now:
            return ALL_ROWS
        carried.append(now[key])
    return _rows(carried)


def merged(current: WalkTestRegression, mark: RegressionMark) -> WalkTestRegression:
    """La fila tras ``mark``: un periodo nuevo si estaba ``current``; si no, la unión.

    ``ledger_record_id`` lo fija quien escribe el registro de la marca.
    """
    if not current.pending:
        return WalkTestRegression(
            organization_id=current.organization_id,
            plant_id=current.plant_id,
            zone_id=current.zone_id,
            state=RegressionState.PENDING,
            marked_at=mark.marked_at,
            cause=mark.cause,
            catalog_version=mark.catalog_version,
            model_version=mark.model_version,
            affected_row_ids=mark.affected_row_ids,
            ledger_record_id=current.ledger_record_id,
        )
    before = current.affected_row_ids
    if before == ALL_ROWS or mark.affected_row_ids == ALL_ROWS or before is None:
        rows: AffectedRows = ALL_ROWS
    else:
        rows = _rows((*before, *mark.affected_row_ids))
    return WalkTestRegression(
        organization_id=current.organization_id,
        plant_id=current.plant_id,
        zone_id=current.zone_id,
        state=RegressionState.PENDING,
        marked_at=current.marked_at,
        cause=mark.cause,
        catalog_version=(
            mark.catalog_version if mark.catalog_version is not None else current.catalog_version
        ),
        model_version=(
            mark.model_version if mark.model_version is not None else current.model_version
        ),
        affected_row_ids=rows,
        ledger_record_id=current.ledger_record_id,
    )
