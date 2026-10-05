"""Generadores de U-03 para la matriz y los pasos del walk-test (tech-stack-decisions §2.6).

``walk_test_matrices`` es el generador de matrices derivadas de PR-GOB-10, 13 y 14.

``walk_test_matrices()`` da un ``WalkTestMatrix``: un ``ZoneCatalog`` canónico compuesto por
``plan_publication`` (la versión 1 de ``catalog_versions`` y hasta tres estándares más), su matriz
derivada (``derive_matrix``: 4 filas por estándar) y un subconjunto **no vacío** de filas
pendientes, con bordes: una fila, todas las de un estándar, todas las de la matriz. Sirve a la
parte de marca de PR-GOB-14 (TASK-209) y a PR-GOB-13 (TASK-214).

``walk_test_step_sequences()`` (TASK-214, PR-GOB-10) da una ``StepSequence``: pasos de la lista
cerrada con responsables de un grupo pequeño (varios pasos por persona), abiertos o cerrados por
``close_step`` con el reloj del servidor, y con correcciones generadas (solo inicio, solo fin o
las dos, hacia atrás o hacia delante dentro de lo que admite BR-GOB-45). Las marcas salen de
desplazamientos dibujados sobre ``T0``, nunca de la hora real. Solo datos generados (NFR-CTR-43).
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
from vigia_platform.catalog.domain.enums import StepKind
from vigia_platform.catalog.domain.matrix import MatrixRow, derive_matrix
from vigia_platform.catalog.domain.standard import DeclaredBy
from vigia_platform.catalog.domain.steps import CorrectionRequest, WalkTestStep, close_step

__all__ = [
    "RESPONSIBLES",
    "StepSequence",
    "WalkTestMatrix",
    "state_of",
    "walk_test_matrices",
    "walk_test_step_sequences",
]

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


# --- Pasos cronometrados (PR-GOB-10) -----------------------------------------------------------

RESPONSIBLES: Final = tuple(uuid.UUID(int=100 + index, version=4) for index in range(3))
"""Tres responsables: cada secuencia reparte varios pasos entre ellos."""
CORRECTOR: Final = uuid.UUID(int=200, version=4)
CORRECTION_REASON: Final = "Se olvidó abrir el paso al llegar a la celda"
_SESSION: Final = uuid.UUID(int=300, version=4)
_ORGANIZATION: Final = uuid.UUID(int=301, version=4)
_PLANT: Final = uuid.UUID(int=302, version=4)
_MS: Final = timedelta(milliseconds=1)
_DAY_MS: Final = 24 * 3600 * 1000


@dataclass(frozen=True)
class StepSequence:
    opened: tuple[WalkTestStep, ...]
    """Cada paso tal como se abrió (marca del servidor, sin cierre)."""
    steps: tuple[WalkTestStep, ...]
    """Los mismos pasos tras ``close_step`` (los abiertos, sin tocar)."""
    closed_at: tuple[datetime | None, ...]
    """Instante del servidor con el que se cerró cada paso (``None`` si sigue abierto)."""


@st.composite
def _corrections(draw: st.DrawFn, started: datetime, ended: datetime) -> CorrectionRequest | None:
    """Una corrección válida de ``[started, ended]`` o ninguna."""
    shape = draw(st.sampled_from(("none", "start", "end", "both")))
    if shape == "none":
        return None
    span = (ended - started) // _MS
    new_start: datetime | None = None
    new_end: datetime | None = None
    if shape in ("start", "both"):
        # Hacia atrás (se olvidó abrirlo) hasta un día, o hacia delante sin pasar el fin.
        new_start = started + _MS * draw(st.integers(-_DAY_MS, span))
    if shape in ("end", "both"):
        floor = new_start if new_start is not None else started
        new_end = floor + _MS * draw(st.integers(0, (ended - floor) // _MS))
    return CorrectionRequest(CORRECTION_REASON, started_at=new_start, ended_at=new_end)


@st.composite
def walk_test_step_sequences(draw: st.DrawFn, max_steps: int = 12) -> StepSequence:
    opened: list[WalkTestStep] = []
    steps: list[WalkTestStep] = []
    closed_at: list[datetime | None] = []
    for index in range(draw(st.integers(0, max_steps))):
        started = T0 + _MS * draw(st.integers(0, 30 * _DAY_MS))
        step = WalkTestStep(
            step_id=uuid.UUID(int=1000 + index, version=4),
            organization_id=_ORGANIZATION,
            plant_id=_PLANT,
            session_id=_SESSION,
            step_kind=draw(st.sampled_from(tuple(StepKind))),
            responsible_user_id=draw(st.sampled_from(RESPONSIBLES)),
            started_at=started,
        )
        opened.append(step)
        if not draw(st.booleans()):
            steps.append(step)
            closed_at.append(None)
            continue
        at = started + _MS * draw(st.integers(0, 12 * 3600 * 1000))
        correction = draw(_corrections(started, at))
        steps.append(close_step(step, at=at, corrected_by=CORRECTOR, correction=correction))
        closed_at.append(at)
    return StepSequence(tuple(opened), tuple(steps), tuple(closed_at))
