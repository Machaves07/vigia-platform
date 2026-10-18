"""Lista inicial de vocabulario bloqueado, derivada de los documentos (BLM §14.1 a 14.4; BR-LAZ-82).

Es la versión 1 de la lista de **cada** organización (``origin = derived``) y el **mínimo** que
ninguna versión posterior retira (BR-LAZ-86): el cliente solo añade, o retira lo que él añadió.
Mientras una organización no tenga fila en ``loop.blocked_vocabulary_list``, se valida con esta
lista y ``list_version = 1``. **U-05 y U-06** la consumen para validar sus textos fijos (D-9;
BR-APP-27, NFR-BOR-59): aquí solo se declara.

Cada término es un **lema** (cubre sus flexiones, ``lexicon``) o un **patrón** (``re`` de la
biblioteca estándar) escrito sobre la **forma de validación** (``folding.validation_form``: sin
tildes, en minúsculas, un espacio entre palabras). El validador envuelve cada patrón entre límites
de palabra. Rige la lista de §14 tal como está escrita (hipótesis U04-H-12, BR-LAZ-86: el mínimo
irreductible lo confirma el dueño con el coordinador SST del piloto), con tres precisiones para
que caiga exactamente lo que la columna «Qué cae» de §14 dice que cae:

- el lema ``garantia`` acompaña a ``garantizar``: §14.2 dice que «garantía de detección» cae con
  ``garantizar``, y un lematizador no une el sustantivo al verbo;
- ``mejora(ra)? la seguridad en`` admite un «un» antes de la cifra («mejorará la seguridad en un
  30 %», el ejemplo de §14.3);
- los dos casos que §14.2 dice que **no** caen («95 % de cobertura observable medida» y «cero
  falsos negativos en N pases del acta», hechos medidos) quedan fuera con una anticipación
  negativa en su patrón.

Los ``term_id`` son UUID v5 estables (espacio ``INITIAL_TERM_NAMESPACE`` y ``forma:valor``): la
siembra de la versión 1 en la base (TASK-350) usa los mismos, así que un veredicto con la lista en
memoria y otro con la lista leída de la base nombran el mismo término. Ninguna sugerencia contiene
un término de la lista (``tests/unit/test_laz_folding.py`` lo comprueba).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from vigia_platform.loop.domain.enums import BlockedTermCategory, BlockedTermForm

__all__ = [
    "INITIAL_LIST_VERSION",
    "INITIAL_TERMS",
    "INITIAL_TERM_NAMESPACE",
    "InitialTerm",
]

INITIAL_LIST_VERSION: Final = 1
"""La lista derivada es la versión 1 de la lista de cada organización (BLM §14)."""

INITIAL_TERM_NAMESPACE: Final = uuid.UUID("0f4d3c1e-6a0b-5f1e-9c7a-3b2d1e0f4a5c")
"""Espacio de nombres de los ``term_id`` de la lista inicial."""

_GAP: Final = r"[\W_]*"
"""Entre dos palabras: cualquier separador o ninguno."""


@dataclass(frozen=True)
class InitialTerm:
    """Un término de la lista derivada."""

    category: BlockedTermCategory
    form: BlockedTermForm
    value: str
    suggestion_es: str

    @property
    def term_id(self) -> uuid.UUID:
        return uuid.uuid5(INITIAL_TERM_NAMESPACE, f"{self.form.value}:{self.value}")


def _words(*words: str) -> str:
    return _GAP.join(words)


_ABSENCE: Final = BlockedTermCategory.ABSENCE_CLAIM
_PERCENT: Final = BlockedTermCategory.PERCENTAGE_GUARANTEE
_REDUCTION: Final = BlockedTermCategory.REDUCTION_PROMISE
_INTENT: Final = BlockedTermCategory.INTENT_OR_BLAME_ATTRIBUTION
_LEMMA: Final = BlockedTermForm.LEMMA
_PATTERN: Final = BlockedTermForm.PATTERN

_SAY_OBSERVED: Final = (
    "Describe lo que el sistema observó y en qué periodo; no afirmes lo que no se observó."
)
_SAY_MEASURED: Final = "Cita la cifra medida, con su periodo y su fuente, en lugar de prometerla."
_SAY_FACTS: Final = (
    "Describe el hecho observable y la condición del proceso, sin juzgar a nadie ni su intención."
)

INITIAL_TERMS: Final[tuple[InitialTerm, ...]] = (
    # §14.1 Afirmaciones de ausencia (P2)
    InitialTerm(_ABSENCE, _LEMMA, "vacio", _SAY_OBSERVED),
    InitialTerm(_ABSENCE, _LEMMA, "despejado", _SAY_OBSERVED),
    InitialTerm(_ABSENCE, _LEMMA, "ausencia", _SAY_OBSERVED),
    InitialTerm(_ABSENCE, _PATTERN, _words("no", "hay", "(?:nadie|personas|gente)"), _SAY_OBSERVED),
    InitialTerm(
        _ABSENCE, _PATTERN, _words("sin", "(?:personas|gente|nadie|presencia)"), _SAY_OBSERVED
    ),
    InitialTerm(
        _ABSENCE,
        _PATTERN,
        _words("no", "(?:ocurrio|paso|sucedio)", "nada"),
        _SAY_OBSERVED,
    ),
    InitialTerm(
        _ABSENCE,
        _PATTERN,
        _words("zona", "(?:esta|quedo|permanece|se" + _GAP + "encuentra)", "segura"),
        _SAY_OBSERVED,
    ),
    InitialTerm(
        _ABSENCE, _PATTERN, _words("libre", "de", "(?:personas|riesgo|peligro)"), _SAY_OBSERVED
    ),
    InitialTerm(
        _ABSENCE, _PATTERN, _words("cero", "(?:presencias?|personas|cruces)"), _SAY_OBSERVED
    ),
    InitialTerm(
        _ABSENCE, _PATTERN, _words("nadie", "(?:entro|ingreso|estuvo|paso)"), _SAY_OBSERVED
    ),
    # §14.2 Garantías de porcentaje (P6)
    InitialTerm(_PERCENT, _LEMMA, "garantizar", _SAY_MEASURED),
    InitialTerm(_PERCENT, _LEMMA, "garantia", _SAY_MEASURED),
    InitialTerm(_PERCENT, _LEMMA, "infalible", _SAY_MEASURED),
    InitialTerm(
        _PERCENT,
        _PATTERN,
        r"[0-9]+(?:[.,][0-9]+)?"
        + _GAP
        + "%"
        + _GAP
        + _words(
            "de", "(?:deteccion|efectividad|precision|cobertura(?!" + _GAP + "observable)|exito)"
        ),
        _SAY_MEASURED,
    ),
    InitialTerm(
        _PERCENT,
        _PATTERN,
        _words("(?:precision|efectividad|deteccion)", "del", "[0-9]+"),
        _SAY_MEASURED,
    ),
    InitialTerm(
        _PERCENT,
        _PATTERN,
        _words("100", "%", "(?:seguro|efectivo|fiable|confiable)"),
        _SAY_MEASURED,
    ),
    InitialTerm(
        _PERCENT,
        _PATTERN,
        _words(
            "cero",
            "(?:falsos"
            + _GAP
            + "negativos(?!"
            + _GAP
            + "en"
            + _GAP
            + "[0-9]+"
            + _GAP
            + "pases)|accidentes|incidentes|errores)",
        ),
        _SAY_MEASURED,
    ),
    InitialTerm(
        _PERCENT,
        _PATTERN,
        _words("(?:detecta|detectamos)", "(?:todo|todos" + _GAP + "los|siempre)"),
        _SAY_MEASURED,
    ),
    InitialTerm(_PERCENT, _PATTERN, _words("nunca", "falla"), _SAY_MEASURED),
    # §14.3 Promesas de reducción (P6)
    InitialTerm(
        _REDUCTION,
        _PATTERN,
        "reduc(?:e|ira|iremos|cion)"
        + _GAP
        + "(?:(?:de|del|los|las)"
        + _GAP
        + ")?"
        + "(?:accidentes|incidentes|lesiones|riesgo)",
        _SAY_MEASURED,
    ),
    InitialTerm(
        _REDUCTION,
        _PATTERN,
        _words("reduccion", "del", r"[0-9]+(?:[.,][0-9]+)?") + _GAP + "%",
        _SAY_MEASURED,
    ),
    InitialTerm(
        _REDUCTION,
        _PATTERN,
        _words("(?:evitara|prevendra|impedira)", "(?:accidentes|lesiones|incidentes)"),
        _SAY_MEASURED,
    ),
    InitialTerm(
        _REDUCTION, _PATTERN, _words("elimina(?:ra)?", "el", "(?:riesgo|peligro)"), _SAY_MEASURED
    ),
    InitialTerm(_REDUCTION, _LEMMA, "disminuira", _SAY_MEASURED),
    InitialTerm(
        _REDUCTION,
        _PATTERN,
        _words("mejora(?:ra)?", "la", "seguridad", "en", "(?:un" + _GAP + ")?[0-9]+"),
        _SAY_MEASURED,
    ),
    # §14.4 Atribución de intención o de culpa (P10, RNF-OBS-03)
    InitialTerm(_INTENT, _LEMMA, "sabotaje", _SAY_FACTS),
    InitialTerm(_INTENT, _PATTERN, _words("manipulacion", "deliberada"), _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "saboteo", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "intencional", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "deliberado", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "adrede", _SAY_FACTS),
    InitialTerm(_INTENT, _PATTERN, _words("a", "proposito"), _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "negligencia", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "negligente", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "imprudencia", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "imprudente", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "temerario", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "descuido", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "descuidado", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "culpa", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "culpable", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "culpar", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "irresponsabilidad", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "irresponsable", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "desobediencia", _SAY_FACTS),
    InitialTerm(_INTENT, _LEMMA, "indisciplina", _SAY_FACTS),
    InitialTerm(_INTENT, _PATTERN, _words("mala", "fe"), _SAY_FACTS),
    InitialTerm(_INTENT, _PATTERN, _words("omision", "voluntaria"), _SAY_FACTS),
    InitialTerm(
        _INTENT,
        _PATTERN,
        _words("no", "(?:quiso|le" + _GAP + "importo|hizo" + _GAP + "caso)"),
        _SAY_FACTS,
    ),
    InitialTerm(
        _INTENT,
        _PATTERN,
        _words("por", "(?:culpa|descuido|error|negligencia)", "(?:del|de" + _GAP + "la)"),
        _SAY_FACTS,
    ),
)
"""Los términos de §14.1 a §14.4, en el orden del documento."""
