"""PR-NUC-14: alterar cualquier columna persistida da ``broken`` exactamente en esa secuencia.

Sobre cadenas generadas en un PostgreSQL 16 migrado (expediente de planta, expediente de
organización y auditoría; registros con y sin ``source_key``, entradas con y sin filtros y puntos
de control firmados), escritas por ``vigia_app`` y encadenadas por el disparador: una columna
persistida de una fila, alterada directamente con un rol privilegiado (``verify_support.mutate``),
hace que ``IntegrityService.verify`` en modo ``full`` responda ``broken`` en la secuencia de esa
fila. El tamaño de lote se genera (de 1 a 10) para que la rotura caiga en cualquier posición del
lote y en sus bordes (el ``LAG`` que cruza lotes). Sin alteración, la misma cadena es ``intact``.

Columnas: todas las de ``LedgerRecord`` y ``AuditEntry`` (``LEDGER_COLUMNS``, ``AUDIT_COLUMNS``).
``content_json`` y ``filters_json`` son columnas generadas (se alteran alterando los bytes, también
por una forma no canónica del mismo documento). ``chain_sequence`` se altera hacia arriba; hacia
abajo, la fila alterada ocupa un hueco anterior y la rotura se nombra en esa posición con el
identificador de la fila alterada (``test_downward_sequence_names_the_mutated_row``).

Solo datos generados. El motor lee como ``vigia_app``, nunca como superusuario.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.properties.envelope_strategies import contents
from tests.verify_support import (
    VerifyEnvironment,
    build_chain,
    mutate,
    mutations,
    normalize_steps,
    steps_strategy,
    verify_environment,
)
from vigia_platform.ledger.chain.verify import IntegrityStatus, VerificationMode

pytestmark = pytest.mark.integration

SHAPES = st.sampled_from(("plant", "organization", "audit"))


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[VerifyEnvironment]:
    with (
        migrated_database(postgres_endpoint, "vigia_verify_mutations") as migrated,
        verify_environment(migrated) as environment,
    ):
        yield environment


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(data=st.data(), shape=SHAPES, steps=steps_strategy, batch_size=st.integers(1, 10))
def test_any_column_mutation_breaks_at_that_sequence(
    environment: VerifyEnvironment, data: st.DataObject, shape: Any, steps: Any, batch_size: int
) -> None:
    steps = normalize_steps(shape, steps)
    documents = [data.draw(contents) for _ in steps]
    index = data.draw(st.integers(0, len(steps) - 1), label="fila")
    mutation = data.draw(mutations(shape), label="alteración")
    built = environment.run(build_chain(environment.migrated, shape, steps, documents))
    service = environment.service(batch_size=batch_size)

    intact = environment.run(service.verify(built.context, built.chain, VerificationMode.FULL))
    assert intact.status is IntegrityStatus.INTACT, intact
    assert intact.to_sequence == len(steps)

    target = built.rows[index]
    changes = mutation.changes(target)
    event(f"{shape}: {mutation.column}")
    environment.run(
        mutate(environment.migrated, built.table, built.id_column, target[built.id_column], changes)
    )

    result = environment.run(service.verify(built.context, built.chain, VerificationMode.FULL))
    assert result.status is IntegrityStatus.BROKEN, (changes, result)
    assert result.broken_sequence == target["chain_sequence"], (changes, result)


def test_downward_sequence_names_the_mutated_row(environment: VerifyEnvironment) -> None:
    """``chain_sequence`` bajada de 5 a 2: la fila 5 aparece dos posiciones antes y se nombra."""
    built = environment.run(build_chain(environment.migrated, "plant", ["record"] * 6))
    target = built.rows[4]
    environment.run(
        mutate(
            environment.migrated,
            built.table,
            built.id_column,
            target["record_id"],
            {"chain_sequence": 2},
        )
    )
    result = environment.run(
        environment.service().verify(built.context, built.chain, VerificationMode.FULL)
    )
    # Las dos filas con secuencia 2 se ordenan por identificador. Si la alterada va primero, rompe
    # el enlace en la posición 2; si va segunda, la secuencia en la posición 3. En los dos casos la
    # rotura lleva el identificador de la fila alterada.
    assert result.status is IntegrityStatus.BROKEN
    assert result.broken_sequence in (2, 3)
    assert result.broken_entry_id == uuid.UUID(str(target["record_id"]))
