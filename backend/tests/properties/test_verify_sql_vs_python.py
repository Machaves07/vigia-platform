"""PR-NUC-49: el paso en SQL del motor y el recorrido en Python coinciden (oráculo, PBT-05).

Sobre cadenas generadas en un PostgreSQL 16 migrado (expediente de planta, de organización y
auditoría, con registros con y sin ``source_key``, entradas con y sin filtros y puntos de control
firmados), **sin alteración o con una alteración de cualquier columna persistida** (también los
bytes de ``content`` o ``filters`` cambiados por una forma no canónica del mismo documento, una
marca movida por debajo del milisegundo o una fila que cambia de cadena):

- el paso 1 del motor (``ledger.chain.verify.sql_pass`` sobre ``SqlIntegrityStore.scan``, con un
  tamaño de lote generado de 1 a 10 y la comprobación final de la cabeza), y
- el recorrido de referencia en Python (``verify_support.reference_walk``: ``chain_walk`` sobre
  las filas en la forma del paquete, más los bytes persistidos, la marca en milisegundos y la
  ``source_key`` del tipo)

devuelven el mismo resultado (``intact`` o ``broken``) y la misma primera secuencia rota. Las
cadenas generadas solo llevan puntos de control bien firmados: la firma es del paso 2, que el paso
en SQL no cubre por diseño (se prueba en ``tests/integration/test_integrity_verify.py``).

Solo datos generados. Las dos lecturas son de ``vigia_app``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.properties.envelope_strategies import contents
from tests.verify_support import (
    CHECKPOINT_KEY,
    VerifyEnvironment,
    build_chain,
    chain_rows,
    mutate,
    mutations,
    normalize_steps,
    public_keys,
    reference_walk,
    steps_strategy,
    verify_environment,
)
from vigia_platform.ledger.chain.chain_walk import genesis_hash
from vigia_platform.ledger.chain.verify import VerifiedPoint, sql_pass

pytestmark = pytest.mark.integration

SHAPES = st.sampled_from(("plant", "organization", "audit"))


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[VerifyEnvironment]:
    with (
        migrated_database(postgres_endpoint, "vigia_verify_oracle") as migrated,
        verify_environment(migrated) as environment,
    ):
        yield environment


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(
    data=st.data(),
    shape=SHAPES,
    steps=steps_strategy,
    batch_size=st.integers(1, 10),
    mutated=st.booleans(),
)
def test_sql_pass_and_python_walk_agree(
    environment: VerifyEnvironment,
    data: st.DataObject,
    shape: Any,
    steps: Any,
    batch_size: int,
    mutated: bool,
) -> None:
    steps = normalize_steps(shape, steps)
    documents = [data.draw(contents) for _ in steps]
    index = data.draw(st.integers(0, len(steps) - 1), label="fila")
    mutation = data.draw(mutations(shape), label="alteración") if mutated else None
    built = environment.run(build_chain(environment.migrated, shape, steps, documents))
    if mutation is not None:
        target = built.rows[index]
        environment.run(
            mutate(
                environment.migrated,
                built.table,
                built.id_column,
                target[built.id_column],
                mutation.changes(target),
            )
        )
        event(f"{shape}: {mutation.column}")
    else:
        event(f"{shape}: sin alteración")

    rows, head = environment.run(chain_rows(environment.migrated, built))
    reference = reference_walk(built, rows, head, public_keys(CHECKPOINT_KEY))

    store, context, chain = environment.store, built.context, built.chain
    start = VerifiedPoint(0, genesis_hash(str(built.organization_id), built.plant_text))
    snapshot = environment.run(store.head(context, chain))
    found = environment.run(
        sql_pass(store, context, chain, start=start, head=snapshot, batch_size=batch_size)
    )

    assert (found is None) == (reference.status == "intact"), (mutation, found, reference)
    if found is not None:
        assert found.sequence == reference.sequence, (mutation, found, reference)
    if mutation is None:
        assert found is None
