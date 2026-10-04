"""Generadores de U-03 para la matriz del walk-test (tech-stack-decisions §2.6).

``walk_test_matrices`` es el generador de matrices derivadas de PR-GOB-10, 13 y 14.

``walk_test_matrices()`` da un ``WalkTestMatrix``: un ``ZoneCatalog`` canónico compuesto por
``plan_publication`` (la versión 1 de ``catalog_versions`` y hasta tres estándares más), su matriz
derivada (``derive_matrix``: 4 filas por estándar) y un subconjunto **no vacío** de filas
pendientes, con bordes: una fila, todas las de un estándar, todas las de la matriz.

TASK-214 lo amplía con pases, oclusiones y pasos cronometrados (PR-GOB-10 y 13); aquí sirve a la
parte de marca de PR-GOB-14 (TASK-209). Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from hypothesis import strategies as st

from tests.properties.gob.strategies.catalog import catalog_versions, drafts
from vigia_platform.catalog.domain.catalog_version import (
    CatalogRuleViolated,
    CatalogState,
    NewStandard,
    PublicationPlan,
    ZoneRef,
    plan_publication,
)
from vigia_platform.catalog.domain.matrix import MatrixRow, derive_matrix
from vigia_platform.catalog.domain.standard import DeclaredBy

__all__ = ["WalkTestMatrix", "state_of", "walk_test_matrices"]

T0: Final = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
DECLARED_BY: Final = DeclaredBy(
    uuid.UUID(int=11, version=4), "Coordinación SST sintética", "administrator"
)
REASON: Final = "Cambio sintético del catálogo de la zona"


@dataclass(frozen=True)
class WalkTestMatrix:
    zone: ZoneRef
    catalog: dict[str, Any]
    rows: tuple[MatrixRow, ...]
    pending: frozenset[uuid.UUID]
    """Filas pendientes de una reejecución (subconjunto no vacío de ``rows``)."""


def state_of(plan: PublicationPlan) -> CatalogState:
    return CatalogState(
        catalog=plan.catalog,
        single_occupancy=plan.single_occupancy,
        aggregation_window_minutes=plan.aggregation_window_minutes,
    )


@st.composite
def walk_test_matrices(draw: st.DrawFn, max_standards: int = 4) -> WalkTestMatrix:
    scenario = draw(catalog_versions(max_changes=0))
    plan = plan_publication(
        None,
        scenario.first,
        zone=scenario.zone,
        issued_at=T0,
        declared_by=DECLARED_BY,
        reason_es=REASON,
        new_standard_id=uuid.UUID(int=1, version=4),
    )
    for index in range(draw(st.integers(0, max_standards - 1))):
        try:
            plan = plan_publication(
                state_of(plan),
                NewStandard(draft=draw(drafts())),
                zone=scenario.zone,
                issued_at=T0 + timedelta(seconds=index + 1),
                declared_by=DECLARED_BY,
                reason_es=REASON,
                new_standard_id=uuid.UUID(int=index + 2, version=4),
            )
        except CatalogRuleViolated:
            break
    rows = derive_matrix(plan.catalog)
    shape = draw(st.sampled_from(("one", "standard", "all", "any")))
    if shape == "one":
        pending = frozenset({draw(st.sampled_from(rows)).row_id})
    elif shape == "standard":
        standard_id = draw(st.sampled_from(rows)).standard_id
        pending = frozenset(row.row_id for row in rows if row.standard_id == standard_id)
    elif shape == "all":
        pending = frozenset(row.row_id for row in rows)
    else:
        chosen = draw(st.lists(st.sampled_from(rows), min_size=1, unique_by=lambda r: r.row_id))
        pending = frozenset(row.row_id for row in chosen)
    return WalkTestMatrix(scenario.zone, plan.catalog, rows, pending)
