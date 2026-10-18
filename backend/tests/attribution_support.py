"""Apoyo de las pruebas del validador ``person_attribution`` (TASK-340).

- ``both_policy``: una ``FreeTextPolicyRegistry`` sellada con los dos validadores de U-04 en el
  orden de ``loop.registration`` (``blocked_vocabulary`` con la lista inicial y
  ``person_attribution`` con el vocabulario declarado que se le pase), sin caché ni base;
- ``loop_writer``: el ``EscritorExpediente`` real de ``writer_environment`` con la política que
  registra U-04 al arrancar (``loop_free_text``: los dos validadores con sus cachés y
  preparadores sobre PostgreSQL).

Solo datos generados.
"""

from __future__ import annotations

from collections.abc import Iterable

from tests.writer_support import WriterEnvironment
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.loop.registration import loop_free_text
from vigia_platform.loop.validators.attribution import (
    PERSON_ATTRIBUTION,
    PersonAttributionValidator,
    declared_names,
)
from vigia_platform.loop.validators.vocabulary import (
    BLOCKED_VOCABULARY,
    BlockedVocabularyValidator,
    initial_vocabulary,
)

__all__ = ["both_policy", "loop_writer"]


def both_policy(declared: Iterable[str] = ()) -> FreeTextPolicyRegistry:
    """La política base más los dos validadores de U-04, sellada."""
    names = declared_names(declared)
    registry = FreeTextPolicyRegistry()
    registry.register(
        BLOCKED_VOCABULARY, BlockedVocabularyValidator(lambda _organization: initial_vocabulary())
    )
    registry.register(PERSON_ATTRIBUTION, PersonAttributionValidator(lambda _organization: names))
    registry.seal()
    return registry


def loop_writer(environment: WriterEnvironment) -> EscritorExpediente:
    """El escritor del entorno con la política de texto libre que registra U-04."""
    registry = FreeTextPolicyRegistry()
    loop_free_text(registry)
    registry.seal()
    return EscritorExpediente(
        database=environment.database,
        registry=environment.registry,
        free_text=registry,
        evidence=EvidenceVerifier(environment.storage, environment.clock),
        outbox=environment.outbox,
        clock=environment.clock,
    )
