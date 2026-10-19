"""NFR-LAZ-48 · corpus R5 contra los dos validadores de U-04 (TASK-340; L-5; BR-LAZ-84).

**Qué intenta** (escenario L-5, R5): que la plataforma afirme que alguien tuvo la culpa o que
guarde su identidad: un cargo con verbo, un cargo como causa, «error humano», un nombre, un
documento, un correo, un teléfono o una placa, también con homoglifos, espacios no ASCII, ancho
completo o invisibles, o dictado por una instrucción incrustada en el texto de un estándar.

**Qué lo detiene**: la política de texto libre de U-02 (base: invisibles y marcado) con los dos
validadores de U-04 en el orden de ``loop.registration`` (``blocked_vocabulary`` y
``person_attribution``), sobre **todo** texto persistido.

El corpus está en ``r5_corpus/*.yaml``: cada archivo lleva su ``version`` y su descripción, y
cada caso su identificador, el texto, el veredicto esperado (``reject`` o ``accept``), el código
del rechazo (``FreeTextRejected.reason``), el patrón de ``person_attribution`` si aplica y el
**motivo**. Los casos ``accept`` son los límites que un falso positivo cruzaría (fallo cerrado:
bloquearía un motivo legítimo); ``declared`` es el vocabulario declarado de la organización del
caso. Todo caso nuevo se queda como regresión permanente; TASK-316 repite el corpus contra la
salida del asistente.

Se comparan códigos y patrones, nunca subcadenas (VIG-194). Solo datos generados.
"""

from __future__ import annotations

import functools
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.attribution_support import both_policy
from vigia_platform.ledger.free_text import FreeTextField, FreeTextRejected
from vigia_platform.loop.validators.attribution import (
    AttributionPattern,
    PersonAttributionRejected,
)

CORPUS: Final = Path(__file__).resolve().parent / "r5_corpus"
FIELD: Final = FreeTextField("root_cause_analysis", "/root_cause_statement", 1, 2000)
"""Una ruta de prosa de U-04 (el enunciado de la causa raíz)."""
MIN_REJECTED: Final = 30
"""NFR-LAZ-48: al menos 30 casos trampa ``[estimación propia]``."""
_ID: Final = re.compile(r"R5-[A-Z]+-\d{2}")
_CODES: Final = frozenset(
    {
        "person_attribution_detected",
        "blocked_vocabulary_term",
        "invisible_character",
        "markup",
    }
)


@dataclass(frozen=True)
class Case:
    source: str
    version: int
    id: str
    text: str
    expected: str
    code: str | None
    pattern: AttributionPattern | None
    reason: str
    declared: tuple[str, ...]


def _case(source: str, version: int, raw: dict[str, Any]) -> Case:
    pattern = raw.get("pattern")
    return Case(
        source=source,
        version=version,
        id=raw["id"],
        text=raw["text"],
        expected=raw["expected"],
        code=raw.get("code"),
        pattern=None if pattern is None else AttributionPattern(pattern),
        reason=raw["reason"],
        declared=tuple(raw.get("declared", ())),
    )


@functools.cache
def corpus() -> tuple[Case, ...]:
    cases: list[Case] = []
    for path in sorted(CORPUS.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(document) == {"version", "description", "cases"}, path.name
        version = document["version"]
        assert isinstance(version, int) and version >= 1, path.name
        assert document["description"].strip(), path.name
        cases.extend(_case(path.name, version, raw) for raw in document["cases"])
    return tuple(cases)


def test_the_corpus_is_versioned_complete_and_well_formed() -> None:
    cases = corpus()
    assert {case.source for case in cases} >= {
        "attribution.yaml",
        "contact_data.yaml",
        "standard_injection.yaml",
        "must_pass.yaml",
    }
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))
    rejected = [case for case in cases if case.expected == "reject"]
    assert len(rejected) >= MIN_REJECTED
    for case in cases:
        assert _ID.fullmatch(case.id), case.id
        assert case.expected in {"reject", "accept"}, case.id
        assert case.reason.strip(), case.id
        assert case.text.strip(), case.id
        if case.expected == "reject":
            assert case.code in _CODES, case.id
        else:
            assert case.code is None and case.pattern is None, case.id
        if case.pattern is not None:
            assert case.code == "person_attribution_detected", case.id
    # Todos los patrones de person_attribution tienen al menos un caso trampa.
    assert {case.pattern for case in rejected if case.pattern} == set(AttributionPattern)


@pytest.mark.parametrize("case", corpus(), ids=[case.id for case in corpus()])
def test_every_case_of_the_corpus_gets_its_verdict(case: Case) -> None:
    policy = both_policy(case.declared)
    organization = uuid.uuid4()
    if case.expected == "accept":
        assert policy.apply(case.text, FIELD, organization_id=organization)
        return
    with pytest.raises(FreeTextRejected) as raised:
        policy.apply(case.text, FIELD, organization_id=organization)
    assert raised.value.reason == case.code
    if case.pattern is not None:
        assert isinstance(raised.value, PersonAttributionRejected)
        assert raised.value.verdict.pattern is case.pattern
