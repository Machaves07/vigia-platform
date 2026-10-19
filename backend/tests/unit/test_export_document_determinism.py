"""Determinismo del documento portátil (LC-LAZ-23; NFR-LAZ-18 y 54; PR-LAZ-49; VIG-236).

Dos generaciones del mismo ``ManifestInputs`` producen el mismo ``document_sha256``: en el
mismo proceso, en un proceso hijo distinto y con otra zona horaria del proceso. Ninguna prueba
aquí depende de la hora real: ``sample_manifest_inputs`` no la usa (ver su módulo).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

from tests.export_document_support import sample_manifest_inputs
from vigia_platform.accreditation.export.document import generate_document

BACKEND: Final = Path(__file__).resolve().parents[2]


def test_two_generations_in_the_same_process_are_byte_identical() -> None:
    inputs = sample_manifest_inputs()
    first = generate_document(inputs)
    second = generate_document(inputs)
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_a_new_manifest_inputs_instance_with_the_same_values_matches() -> None:
    """El mismo alcance, construido de nuevo: no hay estado oculto entre llamadas."""
    first = generate_document(sample_manifest_inputs())
    second = generate_document(sample_manifest_inputs())
    assert first == second


def _child_digest(*, timezone: str, hash_seed: str) -> str:
    environment = {**os.environ, "TZ": timezone, "PYTHONHASHSEED": hash_seed}
    result = subprocess.run(
        [sys.executable, "-m", "tests.export_document_child"],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_two_different_processes_with_two_different_timezones_match() -> None:
    reference = hashlib.sha256(generate_document(sample_manifest_inputs())).hexdigest()
    utc = _child_digest(timezone="UTC", hash_seed="0")
    bogota = _child_digest(timezone="America/Bogota", hash_seed="42")
    auckland = _child_digest(timezone="Pacific/Auckland", hash_seed="random")
    assert reference == utc == bogota == auckland
