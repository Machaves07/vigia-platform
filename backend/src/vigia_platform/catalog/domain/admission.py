"""Prueba de admisión de tres preguntas por (planta, familia) (LC-GOB-02; BR-GOB-13 a 18; H-43).

Es la regla que impide que el producto se convierta en vigilancia de personas: una familia del
contrato (``predicate_family``) entra en una planta solo si las tres respuestas son afirmativas:

- ``standard``: existe un estándar de seguridad declarado por escrito y versionado;
- ``remedy``: el hallazgo se cierra con una acción de ingeniería o de proceso sobre la zona;
- ``subject``: el dato pierde sentido al desagregarlo por persona.

``decide_admission`` es la función pura de la decisión (PAT-GOB-MAN): sin base ni hora del
sistema. ``admitted`` si y solo si las tres valen ``true``; si no, ``rejected`` con
``failed_criterion`` = el **primer** criterio falso en el orden ``standard``, ``remedy``,
``subject`` (decisión del redactor de TASK-207: el diseño nombra un solo criterio sin fijar el
orden). La propiedad PR-GOB-11 la recorre entera.

No existe otra forma de admitir: ni clave de permiso, ni parámetro, ni ruta que salte las tres
respuestas, y ninguna familia de productividad cabe en la lista cerrada del contrato
(BR-GOB-17, 18; NFR-GOB-41). La plataforma **habilita** familias existentes, nunca las crea.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from vigia_contracts.models.enumerations import PredicateFamily

from vigia_platform.catalog.domain.enums import AdmissionCriterion, AdmissionResult
from vigia_platform.shared.context import Role

__all__ = [
    "CRITERIA_ORDER",
    "MAX_JUSTIFICATION_CHARS",
    "AdmissionAnswers",
    "AdmissionDecision",
    "FamilyAdmission",
    "decide_admission",
]

MAX_JUSTIFICATION_CHARS: Final = 2000
"""``justification_es`` ≤ 2 000 caracteres (domain-entities §2.3)."""

CRITERIA_ORDER: Final = (
    AdmissionCriterion.STANDARD,
    AdmissionCriterion.REMEDY,
    AdmissionCriterion.SUBJECT,
)
"""Orden en que se elige ``failed_criterion`` cuando fallan varios."""


@dataclass(frozen=True, slots=True)
class AdmissionAnswers:
    """Las tres respuestas booleanas de la prueba (BR-GOB-13)."""

    standard: bool
    remedy: bool
    subject: bool

    def __post_init__(self) -> None:
        for name in ("standard", "remedy", "subject"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} debe ser bool")

    def answer(self, criterion: AdmissionCriterion) -> bool:
        """La respuesta al criterio ``criterion``."""
        value: bool = getattr(self, AdmissionCriterion(criterion).value)
        return value

    def as_dict(self) -> dict[str, bool]:
        return {"standard": self.standard, "remedy": self.remedy, "subject": self.subject}


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Resultado de la prueba: ``failed_criterion`` existe si y solo si es ``rejected``."""

    result: AdmissionResult
    failed_criterion: AdmissionCriterion | None = None

    def __post_init__(self) -> None:
        if (self.result is AdmissionResult.REJECTED) != (self.failed_criterion is not None):
            raise ValueError("failed_criterion acompaña a un rechazo y solo a un rechazo")

    @property
    def admitted(self) -> bool:
        return self.result is AdmissionResult.ADMITTED


def decide_admission(answers: AdmissionAnswers) -> AdmissionDecision:
    """``admitted`` si y solo si las tres respuestas son afirmativas (BR-GOB-13, PR-GOB-11)."""
    if not isinstance(answers, AdmissionAnswers):
        raise TypeError("answers debe ser AdmissionAnswers")
    for criterion in CRITERIA_ORDER:
        if not answers.answer(criterion):
            return AdmissionDecision(AdmissionResult.REJECTED, criterion)
    return AdmissionDecision(AdmissionResult.ADMITTED)


@dataclass(frozen=True, slots=True, kw_only=True)
class FamilyAdmission:
    """Una evaluación de la prueba (domain-entities §2.3 ⛓🔒): aprobada o rechazada, nunca se
    reescribe ni se borra (BR-GOB-15, P4)."""

    admission_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    family: PredicateFamily
    answers: AdmissionAnswers
    justification_es: str | None
    result: AdmissionResult
    failed_criterion: AdmissionCriterion | None
    evaluated_by: uuid.UUID
    role_in_use: Role
    evaluated_at: datetime
    ledger_record_id: uuid.UUID

    def __post_init__(self) -> None:
        # Las mismas restricciones que la tabla (``gob_0017``): el resultado sale de las
        # respuestas y el criterio fallido es una respuesta negativa.
        AdmissionDecision(self.result, self.failed_criterion)
        if self.result is not decide_admission(self.answers).result:
            raise ValueError("result es admitted si y solo si las tres respuestas son afirmativas")
        if self.failed_criterion is not None and self.answers.answer(self.failed_criterion):
            raise ValueError("failed_criterion debe nombrar una respuesta negativa")
        if self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at debe llevar zona horaria")

    @property
    def admitted(self) -> bool:
        return self.result is AdmissionResult.ADMITTED
