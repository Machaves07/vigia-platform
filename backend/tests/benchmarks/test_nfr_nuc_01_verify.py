"""Banco de la verificación por lotes de NFR-NUC-01 (TASK-142, VIG-91).

Sobre PostgreSQL 16 en contenedor: una cadena de expediente de planta de **un lote** (10 000
registros, el tamaño de lote de ``IntegrityService``) escrita por ``vigia_app`` y encadenada por el
disparador, con contenido del tamaño de un hallazgo y un punto de control firmado cada 1 000
registros. Cada ronda verifica la cadena entera en modo ``full`` (forma canónica del 100 %, las 10
firmas y la auditoría del resultado): el modo ``incremental`` partiría del resultado anterior y no
verificaría nada en la segunda ronda. El objetivo de NFR-NUC-01 (≥ 5 000 registros por segundo)
equivale a 2 000 ms por lote; ``test_verify_throughput.py`` lo exige además sobre 50 000.

Solo datos generados. Solo corre con ``--hypothesis-profile=nightly``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import cast

import pytest
from pydantic import JsonValue
from vigia_contracts.canonical import canonicalize

from tests.benchmarks.conftest import Measure
from tests.benchmarks.test_verify_throughput import _content
from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import insert_batch, record_values, set_organization
from tests.verify_support import BuiltChain, VerifyEnvironment, insert_entry, verify_environment
from tests.writer_support import ZONE_TYPE
from vigia_platform.ledger.chain.verify import IntegrityResult, IntegrityStatus, VerificationMode

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

RECORDS = 10_000
CHECKPOINT_EVERY = 1_000
TARGET_PER_SECOND = 5_000


async def _write(environment: VerifyEnvironment, built: BuiltChain) -> None:
    connection = await environment.migrated.connect("vigia_app")
    try:
        for block in range(1, RECORDS // CHECKPOINT_EVERY + 1):
            async with connection.transaction():
                await set_organization(connection, built.organization_id)
                values = record_values(built.organization_id, built.plant_id, record_type=ZONE_TYPE)
                values["content"] = canonicalize(cast(JsonValue, _content(block)))
                await insert_batch(
                    connection,
                    "ledger.ledger_record",
                    values,
                    rows=CHECKPOINT_EVERY - 1,
                    overrides={"record_id": "gen_random_uuid()"},
                )
                built.rows[:] = [
                    await connection.fetchrow(
                        "SELECT * FROM ledger.ledger_record WHERE plant_id = $1"
                        " ORDER BY chain_sequence DESC LIMIT 1",
                        built.plant_id,
                    )
                ]
                await insert_entry(connection, built, "checkpoint")
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[VerifyEnvironment]:
    with (
        migrated_database(postgres_endpoint, "bench_verify_batch") as migrated,
        verify_environment(migrated) as environment,
    ):
        yield environment


def test_verify_one_batch(environment: VerifyEnvironment, measure: Measure) -> None:
    built = BuiltChain("plant", uuid.uuid4(), uuid.uuid4())
    environment.run(_write(environment, built))
    service = environment.service()

    def target() -> None:
        result: IntegrityResult = environment.run(
            service.verify(built.context, built.chain, VerificationMode.FULL)
        )
        assert result.status is IntegrityStatus.INTACT, result
        assert result.to_sequence == RECORDS

    result = measure(
        "ledger_verify_batch",
        "Verificación de un lote de 10 000 registros (modo full)",
        target,
        objective_ms=RECORDS / TARGET_PER_SECOND * 1000,
        details={"records": RECORDS, "checkpoints": RECORDS // CHECKPOINT_EVERY},
    )
    result.details["records_per_second_median"] = round(RECORDS / (result.median / 1000))
