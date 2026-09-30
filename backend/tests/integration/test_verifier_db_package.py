"""El verificador de paquetes sobre filas reales encadenadas por el disparador (TASK-116).

PR-NUC-19 con la base: en un PostgreSQL 16 migrado, ``vigia_app`` escribe registros en una cadena
de planta y en una de organización, y entradas de auditoría; el disparador ``vigia_chain_link``
fija secuencias, marcas y hashes. Se anexa un punto de control firmado a la cadena de
organización (``record_type = checkpoint``) y a la de auditoría (``operation = checkpoint``, con
el contenido en ``filters``). Las filas leídas de vuelta, pasadas a la forma del paquete
(``docs/package-format.md``) con la cabeza de ``ledger.chain_head``, verifican ``intact`` con el
archivo generado ``tools/vigia_verify.py`` en ``python3.10 -I -S``; un bit alterado lo rompe en
esa secuencia. Demuestra que la forma del paquete son exactamente las columnas persistidas.

Solo datos generados.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import (
    DatabaseLoop,
    MigratedDatabase,
    audit_values,
    insert_audit,
    insert_record,
    migrated_database,
    record_values,
    register_record_types,
    set_organization,
)
from tests.verifier_packages import (
    PackageChain,
    SigningKey,
    canonical_timestamp,
    row_to_entry,
    signed_message,
    write_package,
)
from vigia_platform.ledger.chain.package_verifier import verify_package

pytestmark = pytest.mark.integration

VERIFIER = Path(__file__).resolve().parents[2] / "tools" / "vigia_verify.py"
KEY = SigningKey.from_seed("checkpoint-db", b"base sintetica")


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_verifier") as migrated:
        yield migrated


@pytest.fixture(scope="module")
def loop() -> Iterator[DatabaseLoop]:
    runner = DatabaseLoop()
    yield runner
    runner.close()


def _checkpoint(
    kind: str, organization_id: uuid.UUID, last: Any, hash_column: str
) -> dict[str, Any]:
    taken_at = canonical_timestamp(last["received_at" if kind == "ledger" else "occurred_at"])
    covered_sequence, covered_hash = last["chain_sequence"], last[hash_column]
    message = signed_message(
        kind, str(organization_id), None, covered_sequence, covered_hash, taken_at
    )
    return {
        "covered_sequence": covered_sequence,
        "covered_hash": covered_hash,
        "taken_at": taken_at,
        "key_id": KEY.key_id,
        "signature": KEY.sign(message),
    }


async def _write_chains(database: MigratedDatabase) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    owner = await database.connect()
    try:
        await register_record_types(owner)
        await owner.execute(
            "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
            " content_schema) VALUES ('checkpoint', 'U-02', 'organization', 1, '{}')"
            " ON CONFLICT (record_type) DO NOTHING"
        )
    finally:
        await owner.close()
    organization_id, plant_id = uuid.uuid4(), uuid.uuid4()
    app = await database.connect("vigia_app")
    try:
        async with app.transaction():
            await set_organization(app, organization_id)
            for number in range(1, 6):
                content = {"zone": f"Z-{number:02d}", "n": number, "nota": "Señal ámbar \u2028"}
                await insert_record(app, record_values(organization_id, plant_id, content=content))
            last = None
            for number in range(1, 4):
                last = await insert_record(
                    app, record_values(organization_id, None, content={"plant": number})
                )
            checkpoint = _checkpoint("ledger", organization_id, last, "record_hash")
            await insert_record(
                app,
                record_values(organization_id, None, record_type="checkpoint", content=checkpoint),
            )
            await insert_record(app, record_values(organization_id, None, content={"after": 1}))
            for number in range(1, 4):
                last = await insert_audit(
                    app,
                    audit_values(organization_id, filters=None if number == 2 else {"p": number}),
                )
            values = audit_values(
                organization_id, filters=_checkpoint("audit", organization_id, last, "entry_hash")
            )
            values["operation"] = "checkpoint"
            await insert_audit(app, values)
        async with app.transaction():
            await set_organization(app, organization_id)
            rows = {
                "plant": await app.fetch(
                    "SELECT * FROM ledger.ledger_record WHERE plant_id = $1"
                    " ORDER BY chain_sequence",
                    plant_id,
                ),
                "organization": await app.fetch(
                    "SELECT * FROM ledger.ledger_record WHERE plant_id IS NULL"
                    " ORDER BY chain_sequence"
                ),
                "audit": await app.fetch(
                    "SELECT * FROM shared.audit_entry ORDER BY chain_sequence"
                ),
                "heads": await app.fetch("SELECT * FROM ledger.chain_head"),
            }
    finally:
        await app.close()
    return organization_id, plant_id, rows


def _chain(
    kind: str, plant_id: uuid.UUID | None, rows: list[Any], heads: list[Any], file: str
) -> PackageChain:
    head = next(
        h
        for h in heads
        if h["kind"] == kind and h["plant_id"] == (None if kind == "audit" else plant_id)
    )
    return PackageChain(
        kind,
        None if plant_id is None else str(plant_id),
        file,
        [row_to_entry(kind, row) for row in rows],
        1,
        head["last_sequence"],
        head["last_hash"],
    )


def _run(package: Path) -> subprocess.CompletedProcess[str]:
    python = shutil.which("python3.10") or sys.executable
    return subprocess.run(
        [python, "-I", "-S", str(VERIFIER), str(package)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_package_from_trigger_rows_verifies(
    database: MigratedDatabase, loop: DatabaseLoop, tmp_path: Path
) -> None:
    organization_id, plant_id, rows = loop.run(_write_chains(database))
    chains = [
        _chain("ledger", plant_id, rows["plant"], rows["heads"], "chains/ledger-plant.jsonl"),
        _chain(
            "ledger", None, rows["organization"], rows["heads"], "chains/ledger-organization.jsonl"
        ),
        _chain("audit", None, rows["audit"], rows["heads"], "chains/audit.jsonl"),
    ]
    assert [len(chain.entries) for chain in chains] == [5, 5, 4]
    package = tmp_path / "package"
    package.mkdir()
    write_package(package, organization_id, chains, [KEY])

    report = verify_package(package)
    assert report.intact, report
    assert [len(chain.result.checkpoints) for chain in report.chains] == [0, 1, 1]
    completed = _run(package)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    # Un bit alterado en el contenido del registro 3 de la planta.
    target = package / "chains" / "ledger-plant.jsonl"
    lines = target.read_text(encoding="utf-8").split("\n")
    entry = json.loads(lines[2])
    entry["content"]["n"] ^= 1
    lines[2] = json.dumps(entry, ensure_ascii=False)
    target.write_text("\n".join(lines), encoding="utf-8")
    completed = _run(package)
    assert completed.returncode == 1
    assert "ROTA en la secuencia 3" in completed.stdout
    assert entry["record_id"] in completed.stdout
