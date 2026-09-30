"""Banco del verificador de paquetes: 100 000 registros en menos de 60 s (NFR-NUC-53).

``[objetivo propio]`` del diseño: ``python vigia_verify.py`` verifica un paquete de 100 000
registros en menos de 60 s en el runner x86-64. El paquete es sintético y realista: una cadena de
expediente de planta con contenido del tamaño de un hallazgo y un punto de control firmado en cada
posición múltiplo de 1 000 (99 firmas Ed25519 que verificar), más su cadena de auditoría. Se mide el
archivo generado ``tools/vigia_verify.py`` tal como lo ejecuta un cliente: ``python3.10 -I -S`` (sin
paquetes instalados) o, si no hay 3.10, el intérprete de la prueba con ``-I -S``.

Solo corre con ``--hypothesis-profile=nightly``. Solo datos generados.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tests.verifier_packages import ChainBuilder, PackageChain, SigningKey, write_package

pytestmark = pytest.mark.nightly

BACKEND = Path(__file__).resolve().parents[2]
VERIFIER = BACKEND / "tools" / "vigia_verify.py"
RECORDS = 100_000
CHECKPOINT_EVERY = 1_000
LIMIT_SECONDS = 60.0


def _content(number: int) -> dict[str, object]:
    return {
        "finding_id": str(uuid.UUID(int=number)),
        "zone_id": "0b8e7c6d-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
        "standard_id": f"STD-{number % 37:03d}",
        "episode": {"started_at": "2026-09-01T00:00:00.000Z", "duration_ms": 1000 + number % 5000},
        "confidence": (number % 1000) / 1000,
        "classes": ["person", "restricted_zone"],
        "clips": [{"sha256": f"{number:064x}", "size_bytes": 1_000_000 + number}],
        "anonymized": True,
    }


def _package(directory: Path) -> tuple[Path, int]:
    organization_id = uuid.UUID("5d0f1c9e-7b1a-4f7e-9d7c-2b6f0a9e1c01")
    plant_id = uuid.UUID("0b8e7c6d-5a4f-4e3d-8c2b-1a0f9e8d7c6b")
    key = SigningKey.from_seed("checkpoint-2026-09", b"banco sintetico")
    ledger = ChainBuilder("ledger", organization_id, plant_id)
    audit = ChainBuilder("audit", organization_id, None)
    while len(ledger.rows) + len(audit.rows) < RECORDS:
        number = len(ledger.rows) + 1
        if number % CHECKPOINT_EVERY == 0:
            ledger.append_checkpoint(key)
        else:
            ledger.append(_content(number))
        if number % 100 == 0:
            audit.append({"filters": {"zone_id": str(plant_id), "page": number // 100}})
    chains = [
        PackageChain.from_builder(ledger, "chains/ledger-plant.jsonl"),
        PackageChain.from_builder(audit, "chains/audit.jsonl"),
    ]
    path = directory / "package"
    path.mkdir()
    write_package(path, organization_id, chains, [key])
    return path, sum(1 for row in ledger.rows if row["record_type"] == "checkpoint")


def test_verifier_100k_records_under_60_seconds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package, expected_checkpoints = _package(tmp_path)
    out = tmp_path / "resultado.json"
    python = shutil.which("python3.10") or sys.executable
    started = time.perf_counter()  # noqa: TID251 - el banco mide tiempo real a propósito.
    completed = subprocess.run(
        [python, "-I", "-S", str(VERIFIER), str(package), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )
    elapsed = time.perf_counter() - started  # noqa: TID251 - el banco mide tiempo real a propósito.
    result = json.loads(out.read_text(encoding="utf-8"))
    entries = sum(chain["entries"] for chain in result["chains"])
    checkpoints = sum(chain["checkpoints_verified"] for chain in result["chains"])
    with capsys.disabled():
        print(
            f"\nbanco vigia_verify ({Path(python).name}): {entries} registros, "
            f"{checkpoints} puntos de control, {elapsed:.1f} s "
            f"({entries / elapsed:,.0f} registros/s; tope {LIMIT_SECONDS:.0f} s)"
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert result["status"] == "intact"
    assert entries == RECORDS
    assert checkpoints == expected_checkpoints >= RECORDS // CHECKPOINT_EVERY - 1
    assert elapsed < LIMIT_SECONDS
