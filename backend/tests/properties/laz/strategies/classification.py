"""Generador ``classification_sequences`` de C-PLA-23 (PR-LAZ-05 y PR-LAZ-06; TASK-308).

Una secuencia de pasos sobre **un** sujeto que un coordinador podría pedir, en cualquier orden:

- ``ClassifyStep``: una clasificación o reclasificación con salida, categoría de **su** lista
  (DE §1), un motivo de una de cuatro clases (``valid``, ``missing``, ``short`` o ``blocked``) y
  ``reinforced`` (verdadero casi siempre: una reclasificación sin él es un caso a probar, no la
  norma);
- ``LifecycleStep``: las operaciones del ciclo de vida que llevan el sujeto adonde la
  reclasificación tiene sus filas propias (acción asignada para T15 y T16, cierre para T19,
  declaración de incerrable para T20). Quien ejecuta la secuencia solo las aplica si el estado lo
  permite (§4.1); si no, las salta.

Es **el** generador de secuencias de clasificación de U-04: lo usan PR-LAZ-05 (historial) y
PR-LAZ-06 (etiquetas). ``classification_sequence`` del kit de U-02 (``vigia_contracts``) genera
presentaciones del nodo para la plataforma simulada (PR-20, PR-NUC-24), no decisiones humanas, y
sigue siendo el de esas propiedades.

La generación depende solo de lo dibujado (retro 14): ni marcas de tiempo ni estado de la base.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from hypothesis import strategies as st

from vigia_platform.loop.application.classification import REASONS_BY_OUTCOME
from vigia_platform.loop.domain.enums import ClassificationOutcome

__all__ = [
    "BLOCKED_TEXT",
    "LEAD_INS",
    "SHORT_TEXT",
    "VALID_TEXTS",
    "ClassifyStep",
    "LifecycleStep",
    "ReasonKind",
    "Step",
    "classification_sequences",
    "classify_steps",
    "reason_text",
]

type ReasonKind = Literal["valid", "missing", "short", "blocked"]
type LifecycleOperation = Literal["assign_action", "close", "declare_unclosable"]

VALID_TEXTS: Final = (
    "Coexistencia observada entre la carretilla y el peatón en el pasillo",
    "Revisado el clip completo con mantenimiento: la intervención constaba",
    "Reflejo del sol en la lámina metálica del acceso norte de la zona",
    "Limpieza programada de la estación con la línea detenida y bloqueada",
)
"""Motivos neutros de 20 a 1000 caracteres que pasan la política y los validadores."""
SHORT_TEXT: Final = "Motivo muy corto aquí"[:19]
"""19 caracteres: justo por debajo del mínimo."""
BLOCKED_TEXT: Final = "La zona quedó garantizada sin ningún riesgo tras la revisión del clip"
"""Contiene ``garantizar`` (lista inicial, BLM §14.2)."""


@dataclass(frozen=True)
class ClassifyStep:
    outcome: ClassificationOutcome
    reason_category: str
    reason: ReasonKind
    text_index: int
    reinforced: bool


@dataclass(frozen=True)
class LifecycleStep:
    operation: LifecycleOperation


type Step = ClassifyStep | LifecycleStep


def reason_text(step: ClassifyStep) -> str | None:
    """El texto del motivo de ``step`` (``None`` si falta)."""
    match step.reason:
        case "missing":
            return None
        case "short":
            return SHORT_TEXT
        case "blocked":
            return BLOCKED_TEXT
    return VALID_TEXTS[step.text_index % len(VALID_TEXTS)]


@st.composite
def classify_steps(draw: st.DrawFn, *, valid_only: bool = False) -> ClassifyStep:
    """Una clasificación con categoría de la lista de su salida."""
    outcome = draw(st.sampled_from(list(ClassificationOutcome)))
    category = draw(st.sampled_from(sorted(REASONS_BY_OUTCOME[outcome])))
    kinds: list[ReasonKind] = ["valid"] if valid_only else ["valid", "missing", "short", "blocked"]
    reason: ReasonKind = draw(
        st.sampled_from(kinds) if valid_only else st.sampled_from(["valid"] * 5 + kinds[1:])
    )
    reinforced = True if valid_only else draw(st.sampled_from([True, True, True, True, False]))
    return ClassifyStep(
        outcome, category, reason, draw(st.integers(0, len(VALID_TEXTS) - 1)), reinforced
    )


_LIFECYCLE: Final[tuple[LifecycleOperation, ...]] = ("assign_action", "close", "declare_unclosable")

_CONFIRM: Final = ClassifyStep(
    ClassificationOutcome.CONFIRMED, "real_coexistence", "valid", 0, True
)
LEAD_INS: Final[dict[str, tuple[Step, ...]]] = {
    "received": (),
    "open": (_CONFIRM,),
    "action_assigned": (_CONFIRM, LifecycleStep("assign_action")),
    "closed": (_CONFIRM, LifecycleStep("assign_action"), LifecycleStep("close")),
    "unclosable": (_CONFIRM, LifecycleStep("declare_unclosable")),
}
"""Entradas que llevan el sujeto al estado desde el que la reclasificación tiene sus filas
propias (T15 y T16 desde el lazo abierto, T19 desde el cierre, T20 desde la declaración): sin
ellas, un ejemplo aleatorio casi nunca llega a cerrar antes de reclasificar."""


@st.composite
def classification_sequences(
    draw: st.DrawFn, *, min_size: int = 1, max_size: int = 8, valid_only: bool = False
) -> list[Step]:
    """Una entrada de ``LEAD_INS`` y después pasos de clasificación (dos de cada tres)
    intercalados con operaciones del ciclo de vida.

    ``valid_only``: solo clasificaciones con motivo válido y reforzado, sin entrada ni ciclo de
    vida (las etiquetas de PR-LAZ-06)."""
    size = draw(st.integers(min_size, max_size))
    steps: list[Step] = [] if valid_only else list(LEAD_INS[draw(st.sampled_from(list(LEAD_INS)))])
    for _ in range(size):
        if not valid_only and draw(st.integers(0, 2)) == 0:
            steps.append(LifecycleStep(draw(st.sampled_from(_LIFECYCLE))))
        else:
            steps.append(draw(classify_steps(valid_only=valid_only)))
    return steps
