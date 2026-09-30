"""Proyección de ``Label`` desde su registro fuente (LC-NUC-16 parte 1; BR-NUC-67).

Cuando el tipo declara ``label_rule`` (``classification`` y ``review_resolution`` de U-04), cada
escritura proyecta **exactamente una** etiqueta en la misma transacción que el registro
(BR-NUC-50). Para que la transacción siga siendo corta (PAT-NUC-RES-08), el escritor calcula aquí
la etiqueta **antes** de abrirla:

- ``project(rule, document)`` toma de las rutas de la regla ``subject_record_id``, ``family``,
  ``outcome``, ``reason_category`` y ``labeled_by`` (U-04 §5.3). Un valor ausente o con otra forma
  lanza ``LabelRuleViolation`` con su puntero: el escritor lo traduce a ``content_invalid``;
- ``evidence_ids`` se resuelve **por referencia**: las evidencias ya registradas del registro
  sujeto (``resolve_evidence_ids``), nunca copias de su contenido;
- dentro de la transacción, ``insert_label`` solo inserta la fila con el ``record_id`` y el
  ``received_at`` del registro fuente (``labeled_at``).

La etiqueta nunca lleva texto libre: ``registry`` rechaza al arrancar una regla que apunte a
texto libre, y aquí ``family`` y ``reason_category`` se exigen ``snake_case`` y ``outcome`` de la
lista cerrada, como la tabla.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text

from vigia_platform.ledger.content_paths import Located, locate
from vigia_platform.ledger.registry import LabelRule
from vigia_platform.shared.db import Transaction

__all__ = [
    "LABEL_OUTCOMES",
    "LabelProjection",
    "LabelRuleViolation",
    "evidence_ids_statement",
    "insert_label",
    "project",
]

LABEL_OUTCOMES: Final = frozenset(
    {"confirmed", "authorized_operation", "false_positive", "review_confirmed", "review_discarded"}
)
"""``outcome`` de ``Label`` (domain-entities §3.7), la lista de la restricción de la tabla."""

_SNAKE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_INSERT_LABEL: Final = text(
    "INSERT INTO ledger.label (label_id, organization_id, plant_id, zone_id, source_record_id,"
    " subject_record_id, family, outcome, reason_category, evidence_ids, labeled_at, labeled_by)"
    " VALUES (:label_id, :organization_id, :plant_id, :zone_id, :source_record_id,"
    " :subject_record_id, :family, :outcome, :reason_category, CAST(:evidence_ids AS uuid[]),"
    " :labeled_at, CAST(:labeled_by AS jsonb))"
)

_EVIDENCE_OF_RECORD: Final = text(
    "SELECT evidence_id FROM ledger.evidence"
    " WHERE organization_id = :organization_id AND record_id = :record_id"
    " ORDER BY verified_at, evidence_id"
)


def evidence_ids_statement() -> Any:
    """Consulta de las evidencias del registro sujeto (parámetros ``organization_id`` y
    ``record_id``); el escritor la ejecuta en una lectura antes de abrir la transacción."""
    return _EVIDENCE_OF_RECORD


class LabelRuleViolation(ValueError):
    """El contenido no da lo que la regla de etiqueta necesita; ``pointer`` es la ruta."""

    def __init__(self, pointer: str, detail: str) -> None:
        super().__init__(f"{pointer or '/'}: {detail}")
        self.pointer = pointer


@dataclass(frozen=True, slots=True)
class LabelProjection:
    """Lo que la etiqueta toma del contenido; el resto lo pone el escritor."""

    subject_record_id: uuid.UUID
    family: str
    outcome: str
    reason_category: str
    labeled_by: Mapping[str, Any]


def _single(document: Any, path: str) -> Located:
    found = locate(document, path)
    if len(found) != 1:
        raise LabelRuleViolation(path, "la regla de etiqueta exige exactamente un valor")
    return found[0]


def _code(document: Any, path: str, allowed: frozenset[str] | None = None) -> str:
    located = _single(document, path)
    value = located.value
    if not isinstance(value, str) or not _SNAKE.fullmatch(value):
        raise LabelRuleViolation(located.pointer, "se esperaba un código snake_case")
    if allowed is not None and value not in allowed:
        raise LabelRuleViolation(located.pointer, "valor fuera de la lista cerrada")
    return value


def project(rule: LabelRule, document: Any) -> LabelProjection:
    """Valores de la etiqueta según ``rule``; ``LabelRuleViolation`` si falta o no encaja uno."""
    subject = _single(document, rule.subject_record_path)
    try:
        subject_id = uuid.UUID(subject.value) if isinstance(subject.value, str) else None
    except ValueError:
        subject_id = None
    if subject_id is None:
        raise LabelRuleViolation(subject.pointer, "se esperaba el UUID del registro sujeto")
    labeled_by = _single(document, rule.labeled_by_path)
    if not isinstance(labeled_by.value, Mapping):
        raise LabelRuleViolation(labeled_by.pointer, "se esperaba la instantánea del firmante")
    return LabelProjection(
        subject_record_id=subject_id,
        family=_code(document, rule.family_path),
        outcome=_code(document, rule.outcome_path, LABEL_OUTCOMES),
        reason_category=_code(document, rule.reason_category_path),
        labeled_by=dict(labeled_by.value),
    )


async def insert_label(
    transaction: Transaction,
    projection: LabelProjection,
    *,
    label_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    source_record_id: uuid.UUID,
    labeled_at: datetime,
    evidence_ids: Sequence[uuid.UUID],
) -> None:
    """Inserta la etiqueta en la transacción del registro fuente; no confirma."""
    await transaction.execute(
        _INSERT_LABEL,
        {
            "label_id": label_id,
            "organization_id": transaction.context.organization_id,
            "plant_id": plant_id,
            "zone_id": zone_id,
            "source_record_id": source_record_id,
            "subject_record_id": projection.subject_record_id,
            "family": projection.family,
            "outcome": projection.outcome,
            "reason_category": projection.reason_category,
            "evidence_ids": list(evidence_ids),
            "labeled_at": labeled_at,
            "labeled_by": json.dumps(projection.labeled_by, ensure_ascii=False, allow_nan=False),
        },
    )
