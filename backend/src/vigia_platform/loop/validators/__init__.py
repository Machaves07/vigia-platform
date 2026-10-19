"""Validadores de texto de U-04 (LC-LAZ-16; BR-LAZ-77 a 86; RF-PLA-15).

Corren en ``FreeTextPolicyRegistry`` de U-02 sobre **toda** ruta de texto libre de cualquier tipo
de registro, también los de U-03, **antes del hash** (BR-LAZ-78): un texto rechazado nunca se
persiste (BR-LAZ-80).

- ``folding``: la forma de validación común (A-45: ``casefold`` y confusables de UTS #39 sobre la
  forma canónica de U-02);
- ``vocabulary``: el validador ``blocked_vocabulary`` (lemas y patrones, D-9);
- ``initial_list``: la lista derivada de BLM §14.1 a §14.4 (versión 1, ``origin = derived``);
- ``lexicon``: lemas y flexiones de la tabla propia ``resources/vocabulary/inflections.es.json``;
- ``attribution``: el validador ``person_attribution`` (BLM §14.5 y A-45 punto 3: cargos,
  culpa, documentos, nombres propios, correos, teléfonos y placas), TASK-340;
- ``cache``: la lista compilada y el vocabulario declarado de cada organización en memoria del
  proceso (PAT-LAZ-REN-02).

El registro está en ``loop.registration`` (``FREE_TEXT_REGISTRARS``). **Módulo crítico**: se importa
sin FastAPI ni SQLAlchemy.
"""

from vigia_platform.loop.validators.attribution import (
    PERSON_ATTRIBUTION,
    PersonAttributionRejected,
    PersonAttributionValidator,
    PersonAttributionVerdict,
)
from vigia_platform.loop.validators.cache import (
    TTL_SECONDS,
    DeclaredNamesCache,
    ListHead,
    VocabularyCache,
)
from vigia_platform.loop.validators.folding import validation_form
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY,
    BlockedVocabularyRejected,
    BlockedVocabularyValidator,
    BlockedVocabularyVerdict,
    CompiledVocabulary,
)

__all__ = [
    "BLOCKED_VOCABULARY",
    "PERSON_ATTRIBUTION",
    "TTL_SECONDS",
    "BlockedVocabularyRejected",
    "BlockedVocabularyValidator",
    "BlockedVocabularyVerdict",
    "CompiledVocabulary",
    "DeclaredNamesCache",
    "ListHead",
    "PersonAttributionRejected",
    "PersonAttributionValidator",
    "PersonAttributionVerdict",
    "VocabularyCache",
    "validation_form",
]
