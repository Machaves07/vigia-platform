"""Generadores de la muestra mensual (PR-LAZ-30; BR-LAZ-105; TASK-343).

Poblaciones de ``SampleCandidate`` con pocas familias y categorías de motivo, para que la
estratificación se ejerza de verdad (varios elementos por estrato y varios estratos), y los
parámetros de ``draw_sample`` (porcentaje, mínimo, máximo y semilla).

Solo datos generados.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import strategies as st

from vigia_platform.accreditation.monthly.sample import MAX_SEED, SampleCandidate

__all__ = [
    "FAMILIES",
    "REASON_CATEGORIES",
    "SampleParameters",
    "candidates",
    "populations",
    "sample_parameters",
]

FAMILIES: tuple[str, ...] = ("real_coexistence", "guard_bypassed", "procedure_gap")
REASON_CATEGORIES: tuple[str, ...] = ("human_error", "equipment_failure", "process_deviation")


@st.composite
def _candidate(draw: st.DrawFn) -> SampleCandidate:
    return SampleCandidate(
        classification_id=draw(st.uuids()),
        subject_id=draw(st.uuids()),
        family=draw(st.sampled_from(FAMILIES)),
        reason_category=draw(st.sampled_from(REASON_CATEGORIES)),
    )


def candidates() -> st.SearchStrategy[SampleCandidate]:
    """Un candidato suelto, con familia y categoría de un vocabulario pequeño y cerrado."""
    return _candidate()


def populations(max_size: int = 300) -> st.SearchStrategy[list[SampleCandidate]]:
    """Una población completa, acotada para que la propiedad corra en tiempo razonable."""
    return st.lists(_candidate(), max_size=max_size)


@dataclass(frozen=True)
class SampleParameters:
    """Los parámetros de ``draw_sample`` ajenos a la población."""

    sample_percent: int
    sample_min: int
    sample_max: int
    seed: int


@st.composite
def sample_parameters(draw: st.DrawFn) -> SampleParameters:
    """``sample_min`` se dibuja primero y ``sample_max`` nunca queda por debajo de él."""
    sample_percent = draw(st.integers(1, 100))
    sample_min = draw(st.integers(0, 50))
    extra = draw(st.integers(0, 50))
    seed = draw(st.integers(0, MAX_SEED))
    return SampleParameters(
        sample_percent=sample_percent,
        sample_min=sample_min,
        sample_max=sample_min + extra,
        seed=seed,
    )
