"""NFR-LAZ-04: banco de los validadores de texto de U-04 sobre 1 000 caracteres (TASK-303).

``laz_validators_1000_chars`` mide la validación de un texto **permitido** de 1 000 caracteres
(el peor caso: ningún término corta la búsqueda) por ``FreeTextPolicyRegistry.apply`` con
``blocked_vocabulary`` y la lista de una organización ya en la caché por proceso: la política base
de U-02, la forma canónica, el plegado de A-45 y la búsqueda de lemas y patrones. Sin red ni base:
la caché ya se cargó antes de medir (PAT-LAZ-REN-02). El validador ``person_attribution``
(TASK-340) se añadirá a la misma medida.

El objetivo, **p95 ≤ 20 ms**, es ``[objetivo propio]`` (NFR-LAZ-04): el informe dice si se cumple
y nunca decide el resultado. En el perfil ``ci`` el banco es **informativo** (no compara con la
base: el equipo del CI no es el de la medición); en ``nightly`` falla si la mediana empeora más
de un 50 % frente a ``baseline.json`` (factor por defecto, como U-02).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Final

from hypothesis import settings

from tests.benchmarks.conftest import Measure
from tests.conftest import NIGHTLY_PROFILE
from tests.vocabulary_support import FIELD, MemoryStore, customer_term
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry, FreeTextPreparation
from vigia_platform.loop.validators.cache import VocabularyCache
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY,
    BlockedVocabularyValidator,
    VocabularyList,
)
from vigia_platform.shared.clock import SimulatedClock

TEXT_CHARS: Final = 1_000
OBJECTIVE_MS: Final = 20.0
SENTENCE: Final = (
    "Revisión de la guarda norte y de la barrera de la prensa durante el turno de mantenimiento; "
    "la señal del sensor y la baliza quedaron verificadas con el acta de calibración. "
)


def _text() -> str:
    text = (SENTENCE * (TEXT_CHARS // len(SENTENCE) + 1))[:TEXT_CHARS]
    return text.rstrip() + "x" * (TEXT_CHARS - len(text.rstrip()))


def test_laz_validators_1000_chars(measure: Measure) -> None:
    organization = uuid.uuid4()
    vocabulary = VocabularyList(
        version=2, terms=(customer_term("montacargas"), customer_term("umbral"))
    )
    cache = VocabularyCache(MemoryStore({organization: vocabulary}))

    async def rows(*_: object) -> list[object]:
        raise AssertionError("el banco no lee la base")

    asyncio.run(
        cache.refresh(
            FreeTextPreparation(
                organization, rows, SimulatedClock(datetime(2026, 10, 18, tzinfo=UTC))
            )
        )
    )
    registry = FreeTextPolicyRegistry()
    registry.register(BLOCKED_VOCABULARY, BlockedVocabularyValidator(cache.compiled_for))
    registry.seal()
    text = _text()
    assert len(text) == TEXT_CHARS
    assert registry.apply(text, FIELD, organization_id=organization) == text  # permitido

    result = measure(
        "laz_validators_1000_chars",
        "Validadores de texto de U-04 (blocked_vocabulary) sobre 1 000 caracteres",
        lambda: registry.apply(text, FIELD, organization_id=organization),
        objective_ms=OBJECTIVE_MS,
        details={"chars": TEXT_CHARS, "validators": [BLOCKED_VOCABULARY]},
        informative=settings.get_current_profile_name() != NIGHTLY_PROFILE,
    )
    assert result.samples == 100
