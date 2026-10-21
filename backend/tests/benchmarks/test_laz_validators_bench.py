"""NFR-LAZ-04: banco de los dos validadores de texto de U-04 sobre 1 000 caracteres (TASK-340).

``laz_validators_1000_chars`` mide la validación de un texto **permitido** de 1 000 caracteres
(el peor caso: ningún término ni patrón corta la búsqueda) por ``FreeTextPolicyRegistry.apply``
con los **dos validadores encadenados** en el orden de ``loop.registration``:
``blocked_vocabulary`` con la lista de una organización ya en la caché por proceso y
``person_attribution`` con su vocabulario declarado ya en la suya. Cubre la política base de U-02,
la forma canónica, el plegado de A-45, la búsqueda de lemas y patrones, los patrones de §14.5 y de
A-45 punto 3, la forma con mayúsculas y el patrón de nombre propio. Sin red ni base: las dos
cachés se cargan antes de medir (PAT-LAZ-REN-02). El texto cita equipos con mayúsculas, cifras,
fechas y un equipo con marca declarada, para que el patrón de nombre propio recorra pares.

El objetivo, **p95 ≤ 20 ms**, es ``[objetivo propio]`` (NFR-LAZ-04): el informe dice si se cumple
y nunca decide el resultado. Falla si la mediana empeora más de un 50 % frente
a la base del mismo runner y perfil; una base ausente o incompatible exige
actualización explícita (TASK-330), nunca una comparación entre equipos distintos.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Final

from tests.benchmarks.conftest import Measure
from tests.benchmarks.laz_support import measured
from tests.vocabulary_support import FIELD, MemoryStore, customer_term
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry, FreeTextPreparation, RowReader
from vigia_platform.loop.validators.attribution import (
    PERSON_ATTRIBUTION,
    PersonAttributionValidator,
)
from vigia_platform.loop.validators.cache import DeclaredNamesCache, VocabularyCache
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY,
    BlockedVocabularyValidator,
    VocabularyList,
)
from vigia_platform.shared.clock import SimulatedClock

TEXT_CHARS: Final = 1_000
OBJECTIVE_MS: Final = 20.0
SENTENCE: Final = (
    "Revisión de la guarda de la Prensa Norte y de la barrera de la Prensa Schuler Kompakt"
    " durante el turno de mantenimiento del 2026-10-18; la señal del sensor y la baliza quedaron"
    " verificadas con el acta de calibración PRO-001 y un costo de 1.500.000 pesos. "
)


def _text() -> str:
    text = (SENTENCE * (TEXT_CHARS // len(SENTENCE) + 1))[:TEXT_CHARS]
    return text.rstrip() + "x" * (TEXT_CHARS - len(text.rstrip()))


class _Names:
    async def names(self, organization_id: uuid.UUID, rows: RowReader) -> tuple[str, ...]:
        return ("Prensa Schuler Kompakt", "Planta Principal", "Zona de Prensas")


def test_laz_validators_1000_chars(measure: Measure) -> None:
    """Fila 5. [objetivo propio] p95 ≤20 ms.

    Composición: política base y forma canónica de U-02 + blocked_vocabulary
    y person_attribution con cachés calientes; sin red ni base.
    Tramos: total → both_validators (1 000 caracteres permitidos, peor caso).
    """
    organization = uuid.uuid4()
    vocabulary = VocabularyList(
        version=2, terms=(customer_term("montacargas"), customer_term("umbral"))
    )
    vocabulary_cache = VocabularyCache(MemoryStore({organization: vocabulary}))
    names_cache = DeclaredNamesCache(_Names())

    async def rows(*_: object) -> list[object]:
        raise AssertionError("el banco no lee la base")

    preparation = FreeTextPreparation(
        organization, rows, SimulatedClock(datetime(2026, 10, 18, tzinfo=UTC))
    )
    asyncio.run(vocabulary_cache.refresh(preparation))
    asyncio.run(names_cache.refresh(preparation))
    registry = FreeTextPolicyRegistry()
    registry.register(BLOCKED_VOCABULARY, BlockedVocabularyValidator(vocabulary_cache.compiled_for))
    registry.register(PERSON_ATTRIBUTION, PersonAttributionValidator(names_cache.names_for))
    registry.seal()
    text = _text()
    assert len(text) == TEXT_CHARS
    assert registry.apply(text, FIELD, organization_id=organization) == text  # permitido

    result = measured(
        measure,
        "laz_validators_1000_chars",
        OBJECTIVE_MS,
        lambda trace: trace.call(
            "both_validators", lambda: registry.apply(text, FIELD, organization_id=organization)
        ),
        details={"chars": TEXT_CHARS, "validators": [BLOCKED_VOCABULARY, PERSON_ATTRIBUTION]},
    )
    assert result.samples == 100
