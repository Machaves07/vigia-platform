"""Banco del motor de verificación: 50 000 registros a ≥ 5 000 registros por segundo (NFR-NUC-01).

``[objetivo propio]`` de NFR-NUC-01 y criterio de TASK-118: una cadena íntegra de 50 000 registros
se verifica a 5 000 registros por segundo o más en el banco local. La cadena es de expediente de
planta, escrita por ``vigia_app`` y encadenada por el disparador, con contenido del tamaño de un
hallazgo y un punto de control firmado cada 1 000 registros (50 firmas Ed25519 que verificar).

Se mide ``IntegrityService.verify`` de punta a punta, con la auditoría del resultado incluida, en
los dos modos programados sobre la cadena entera (5 lotes de 10 000):

- ``incremental`` sin resultado anterior (parte de la génesis: el peor caso de la diaria), con la
  muestra canónica del 1 % y al menos 100 por lote;
- ``full`` (la mensual): forma canónica del 100 %.

El criterio de NFR-NUC-01 es el de la incremental; la completa se exige igual. Imprime las cifras
(registros, segundos y registros por segundo) para el PR. Solo corre con
``--hypothesis-profile=nightly``. Solo datos generados.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest
from vigia_contracts.canonical import canonicalize

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import insert_batch, record_values, set_organization
from tests.verify_support import (
    BuiltChain,
    VerifyEnvironment,
    insert_entry,
    verify_environment,
)
from tests.writer_support import ZONE_TYPE
from vigia_platform.ledger.chain.verify import IntegrityStatus, VerificationMode

pytestmark = [pytest.mark.nightly, pytest.mark.integration]

RECORDS = 50_000
CHECKPOINT_EVERY = 1_000
TARGET_PER_SECOND = 5_000


def _content(block: int) -> dict[str, object]:
    return {
        "finding_id": str(uuid.UUID(int=block)),
        "zone_id": "0b8e7c6d-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
        "standard_id": f"STD-{block % 37:03d}",
        "episode": {"started_at": "2026-09-01T00:00:00.000Z", "duration_ms": 1000 + block},
        "confidence": (block % 1000) / 1000,
        "classes": ["person", "restricted_zone"],
        "clips": [{"sha256": f"{block:064x}", "size_bytes": 1_000_000 + block}],
        "anonymized": True,
    }


async def _write(environment: VerifyEnvironment, built: BuiltChain) -> None:
    connection = await environment.migrated.connect("vigia_app")
    try:
        written = 0
        block = 0
        while written < RECORDS:
            block += 1
            async with connection.transaction():
                await set_organization(connection, built.organization_id)
                plain = min(CHECKPOINT_EVERY - 1, RECORDS - written)
                values = record_values(
                    built.organization_id,
                    built.plant_id,
                    record_type=ZONE_TYPE,
                    content=_content(block),
                )
                values["content"] = canonicalize(_content(block))
                await insert_batch(
                    connection,
                    "ledger.ledger_record",
                    values,
                    rows=plain,
                    overrides={"record_id": "gen_random_uuid()"},
                )
                written += plain
                if written < RECORDS:
                    built.rows[:] = [
                        await connection.fetchrow(
                            "SELECT * FROM ledger.ledger_record WHERE plant_id = $1"
                            " ORDER BY chain_sequence DESC LIMIT 1",
                            built.plant_id,
                        )
                    ]
                    await insert_entry(connection, built, "checkpoint")
                    written += 1
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[VerifyEnvironment]:
    with (
        migrated_database(postgres_endpoint, "vigia_verify_bench") as migrated,
        verify_environment(migrated) as environment,
    ):
        yield environment


def test_verify_50k_records_at_5000_per_second(
    environment: VerifyEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    built = BuiltChain("plant", uuid.uuid4(), uuid.uuid4())
    environment.run(_write(environment, built))
    service = environment.service()
    rates: dict[str, float] = {}
    for mode in (VerificationMode.INCREMENTAL, VerificationMode.FULL):
        started = time.perf_counter()  # noqa: TID251 - el banco mide tiempo real a propósito.
        result = environment.run(service.verify(built.context, built.chain, mode))
        elapsed = time.perf_counter() - started  # noqa: TID251 - el banco mide tiempo real.
        assert result.status is IntegrityStatus.INTACT, result
        assert result.to_sequence == RECORDS
        assert result.checkpoints_checked == RECORDS // CHECKPOINT_EVERY
        expected_canonical = RECORDS if mode is VerificationMode.FULL else 5 * 100
        assert result.canonical_checked == expected_canonical
        rates[mode.value] = RECORDS / elapsed
        with capsys.disabled():
            print(
                f"\nbanco verify {mode.value}: {RECORDS} registros,"
                f" {result.checkpoints_checked} puntos de control,"
                f" {result.canonical_checked} canónicos, {elapsed:.2f} s"
                f" ({rates[mode.value]:,.0f} registros/s; objetivo {TARGET_PER_SECOND:,})"
            )
    assert rates["incremental"] >= TARGET_PER_SECOND
    assert rates["full"] >= TARGET_PER_SECOND
