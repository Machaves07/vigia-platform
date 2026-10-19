"""El flujo guiado de causa raíz, puro (LC-LAZ-12; C-PLA-26; BR-LAZ-61, 62, 64 y 65).

``GuidedAnalysis`` es el borrador de ``RootCauseAnalysis`` (domain-entities §2.9) sin lo que pone
la base (identificadores, contexto objetivo, marcas). Las reglas, sin red ni base:

- **Cinco porqués en orden** (BR-LAZ-61, PR-LAZ-18): ``put_step`` escribe el paso *n* solo si
  los pasos 1 a *n - 1* ya están completos; editar uno anterior (un retroceso) lo reemplaza y
  conserva los siguientes. Un paso más allá del siguiente, o fuera de 1 a 5, es
  ``analysis_step_out_of_order``: nunca hay un paso completo con el anterior incompleto ni más
  de cinco porqués. Un paso guardado siempre está completo (su enunciado ya pasó los
  validadores).
- **Tres categorías** (BR-LAZ-62, PR-LAZ-16): ``cause_category`` acepta exactamente
  ``process``, ``engineering`` y ``organization`` (igualdad exacta, sin plegar); cualquier otro
  valor, también uno que nombre a una persona o una conducta, es ``cause_category_not_allowed``.
  Lo mismo para la categoría de un paso, de una rama del diagrama y de la causa raíz.
- **Lo que falta** (BR-LAZ-64, PR-LAZ-17): ``missing_items`` es la lista cerrada
  ``analysis_missing_item`` de lo que el registro firmado exige y el análisis no tiene, en el
  orden de la lista. ``sign`` con algo pendiente devuelve ``Pending`` (el análisis queda
  ``pending_information`` y **no** se firma); sin pendientes, exige nombre y licencia
  (``signer_license_missing``, BR-LAZ-65) y devuelve ``Signed``. Es el orden de BLM §4, paso 4.
- **Firmado es definitivo** (P4): un análisis ``signed`` no admite más pasos ni otra firma
  (``transition_not_allowed``).

``record_content`` arma el contenido ``root_cause_analysis`` de DE §5.2 **con los textos del
análisis tal cual**: lo que se firma es exactamente lo que el humano aceptó (PR-LAZ-17). Ningún
texto se reescribe aquí; la normalización NFC la hace la política de texto libre antes de
guardar el paso o el texto final.
"""

from __future__ import annotations

import dataclasses
import enum
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from vigia_platform.loop.domain.enums import (
    AnalysisMissingItem,
    CauseCategory,
    EvidenceRefKind,
    RootCauseAnalysisState,
)

__all__ = [
    "CAUSE_CATEGORIES",
    "MAX_BRANCHES",
    "MAX_EVIDENCE_REFS",
    "MAX_FACTORS",
    "MAX_WHY_STEPS",
    "QUESTIONS_ES",
    "Branch",
    "EvidenceReference",
    "Factor",
    "GuidedAnalysis",
    "Pending",
    "RootCauseCode",
    "RootCauseRejected",
    "Signed",
    "WhyStep",
    "cause_category",
    "missing_information_es",
    "missing_items",
    "put_step",
    "record_content",
    "sign",
    "steps_in_order",
]

MAX_WHY_STEPS: Final = 5
"""Cinco porqués como mucho (§2.9: ``why_steps`` de 1 a 5)."""
MAX_BRANCHES: Final = 6
"""Ramas del diagrama de causa y efecto (§2.9: 0 a 6)."""
MAX_FACTORS: Final = 16
"""Factores por rama (``CauseEffectBranch`` de ``loop.record_types``)."""
MAX_EVIDENCE_REFS: Final = 32
"""Referencias de evidencia por paso o por factor (``loop.record_types``)."""

CAUSE_CATEGORIES: Final = frozenset(category.value for category in CauseCategory)
"""``process``, ``engineering`` y ``organization``: ninguna es una persona (H-28, P7)."""

QUESTIONS_ES: Final[tuple[str, ...]] = (
    "¿Por qué ocurrió el hecho observado en la zona?",
    "¿Por qué ocurrió lo que responde el primer porqué?",
    "¿Por qué ocurrió lo que responde el segundo porqué?",
    "¿Por qué ocurrió lo que responde el tercer porqué?",
    "¿Por qué ocurrió lo que responde el cuarto porqué?",
)
"""``question_es``: el texto fijo del flujo guiado para cada paso (§2.9)."""


class RootCauseCode(enum.StrEnum):
    """Por qué el dominio rechaza (la aplicación lo traduce a su ``detail_code``)."""

    ANALYSIS_STEP_OUT_OF_ORDER = "analysis_step_out_of_order"
    CAUSE_CATEGORY_NOT_ALLOWED = "cause_category_not_allowed"
    SIGNER_LICENSE_MISSING = "signer_license_missing"
    TRANSITION_NOT_ALLOWED = "transition_not_allowed"


class RootCauseRejected(Exception):
    """Un rechazo del flujo guiado, con su código."""

    def __init__(self, code: RootCauseCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """Una referencia a un hecho de la zona (un registro, un clip, una métrica o cobertura)."""

    kind: EvidenceRefKind
    id: uuid.UUID

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind.value, "id": str(self.id)}


@dataclass(frozen=True, slots=True)
class WhyStep:
    """Un porqué completo: su enunciado ya pasó la política de texto libre."""

    step_index: int
    statement: str
    evidence_refs: tuple[EvidenceReference, ...] = ()
    category: CauseCategory | None = None

    @property
    def question_es(self) -> str:
        return QUESTIONS_ES[self.step_index - 1]

    def to_json(self) -> dict[str, Any]:
        """La forma del paso en el registro (sin ``question_es``, que es fijo)."""
        step: dict[str, Any] = {
            "step_index": self.step_index,
            "statement": self.statement,
            "evidence_refs": [ref.to_json() for ref in self.evidence_refs],
        }
        if self.category is not None:
            step["category"] = self.category.value
        return step


@dataclass(frozen=True, slots=True)
class Factor:
    text: str
    evidence_refs: tuple[EvidenceReference, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"text": self.text, "evidence_refs": [ref.to_json() for ref in self.evidence_refs]}


@dataclass(frozen=True, slots=True)
class Branch:
    """Una rama del diagrama de causa y efecto: una de las tres categorías, nunca una persona."""

    category: CauseCategory
    factors: tuple[Factor, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "branch_category": self.category.value,
            "factors": [factor.to_json() for factor in self.factors],
        }


@dataclass(frozen=True, slots=True)
class GuidedAnalysis:
    """El borrador del análisis: estado, porqués, diagrama y el texto final propuesto."""

    state: RootCauseAnalysisState = RootCauseAnalysisState.DRAFT
    steps: tuple[WhyStep, ...] = ()
    branches: tuple[Branch, ...] = ()
    root_cause_category: CauseCategory | None = None
    root_cause_statement: str | None = None
    conclusion: str | None = None


@dataclass(frozen=True, slots=True)
class Pending:
    """La firma no procede: el análisis queda ``pending_information`` con lo que falta."""

    analysis: GuidedAnalysis
    missing: tuple[AnalysisMissingItem, ...]


@dataclass(frozen=True, slots=True)
class Signed:
    """La firma procede: el análisis ``signed`` que se escribe en el expediente."""

    analysis: GuidedAnalysis


def cause_category(value: object) -> CauseCategory:
    """Una de las tres categorías, por igualdad exacta; si no, ``cause_category_not_allowed``."""
    if isinstance(value, str) and value in CAUSE_CATEGORIES:
        return CauseCategory(value)
    raise RootCauseRejected(RootCauseCode.CAUSE_CATEGORY_NOT_ALLOWED)


def steps_in_order(steps: Sequence[WhyStep]) -> bool:
    """El invariante de PR-LAZ-18: pasos 1, 2, …, *k* sin huecos y *k* ≤ 5."""
    return len(steps) <= MAX_WHY_STEPS and [step.step_index for step in steps] == list(
        range(1, len(steps) + 1)
    )


def put_step(analysis: GuidedAnalysis, step: WhyStep) -> GuidedAnalysis:
    """Escribe el paso ``step.step_index``: el siguiente, o uno ya completo (edición)."""
    if analysis.state is RootCauseAnalysisState.SIGNED:
        raise RootCauseRejected(RootCauseCode.TRANSITION_NOT_ALLOWED)
    index = step.step_index
    if type(index) is not int or not 1 <= index <= MAX_WHY_STEPS:
        raise RootCauseRejected(RootCauseCode.ANALYSIS_STEP_OUT_OF_ORDER)
    if index > len(analysis.steps) + 1:
        raise RootCauseRejected(RootCauseCode.ANALYSIS_STEP_OUT_OF_ORDER)
    steps = list(analysis.steps)
    if index == len(steps) + 1:
        steps.append(step)
    else:
        steps[index - 1] = step
    return dataclasses.replace(analysis, steps=tuple(steps))


def missing_items(analysis: GuidedAnalysis) -> tuple[AnalysisMissingItem, ...]:
    """Lo que el registro firmado exige y el análisis no tiene, en el orden de la lista."""
    present = {
        AnalysisMissingItem.WHY_STEPS: bool(analysis.steps),
        AnalysisMissingItem.ROOT_CAUSE_CATEGORY: analysis.root_cause_category is not None,
        AnalysisMissingItem.ROOT_CAUSE_STATEMENT: analysis.root_cause_statement is not None,
        AnalysisMissingItem.CONCLUSION: analysis.conclusion is not None,
    }
    return tuple(item for item in AnalysisMissingItem if not present[item])


def missing_information_es(
    missing: Sequence[AnalysisMissingItem], label: Callable[[AnalysisMissingItem], str]
) -> str:
    """``missing_information``: la enumeración en español de lo que falta (con sus etiquetas)."""
    if not missing:
        raise ValueError("no falta nada: un análisis completo no tiene missing_information")
    return "Falta para firmar: " + "; ".join(label(item) for item in missing) + "."


def sign(
    analysis: GuidedAnalysis,
    *,
    branches: Sequence[Branch],
    root_cause_category: CauseCategory | None,
    root_cause_statement: str | None,
    conclusion: str | None,
    signer_name: str | None,
    professional_license: str | None,
) -> Pending | Signed:
    """Firma con el texto final que el humano acepta, o lo deja pendiente con lo que falta."""
    if analysis.state is RootCauseAnalysisState.SIGNED:
        raise RootCauseRejected(RootCauseCode.TRANSITION_NOT_ALLOWED)
    if len(branches) > MAX_BRANCHES:  # pragma: no cover - el esquema de la ruta ya lo acota
        raise ValueError("más ramas de las que admite el diagrama")
    candidate = dataclasses.replace(
        analysis,
        branches=tuple(branches),
        root_cause_category=root_cause_category,
        root_cause_statement=root_cause_statement,
        conclusion=conclusion,
    )
    missing = missing_items(candidate)
    if missing:
        pending = dataclasses.replace(candidate, state=RootCauseAnalysisState.PENDING_INFORMATION)
        return Pending(pending, missing)
    if not (signer_name or "").strip() or not (professional_license or "").strip():
        raise RootCauseRejected(RootCauseCode.SIGNER_LICENSE_MISSING)
    return Signed(dataclasses.replace(candidate, state=RootCauseAnalysisState.SIGNED))


def record_content(
    analysis: GuidedAnalysis,
    *,
    analysis_id: uuid.UUID,
    subject_kind: str,
    subject_id: uuid.UUID,
    anchor_record_id: uuid.UUID,
    signer: Mapping[str, str],
    signed_at: str,
) -> dict[str, Any]:
    """El contenido ``root_cause_analysis`` (DE §5.2) de un análisis firmado, texto por texto."""
    if analysis.state is not RootCauseAnalysisState.SIGNED or missing_items(analysis):
        raise ValueError("solo un análisis firmado y completo tiene registro")
    if analysis.root_cause_category is None:  # pragma: no cover - missing_items lo descarta
        raise ValueError("sin categoría de la causa raíz")
    return {
        "analysis_id": str(analysis_id),
        "subject_kind": subject_kind,
        "subject_id": str(subject_id),
        "anchor_record_id": str(anchor_record_id),
        "why_steps": [step.to_json() for step in analysis.steps],
        "cause_effect_branches": [branch.to_json() for branch in analysis.branches],
        "root_cause_category": analysis.root_cause_category.value,
        "root_cause_statement": analysis.root_cause_statement,
        "conclusion": analysis.conclusion,
        "state": RootCauseAnalysisState.SIGNED.value,
        "assistant": {"used": False},
        "signer": dict(signer),
        "signed_at": signed_at,
    }
