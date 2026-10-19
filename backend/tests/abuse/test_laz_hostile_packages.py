"""Corpus de paquetes hostiles contra el verificador empaquetado (TASK-346; PAT-LAZ-SEG-02;
NFR-LAZ-23; escenario L-6 y R6 invertido).

- El corpus (``tests/fixtures/synthetic/hostile_packages``) tiene los siete paquetes hostiles de
  TASK-346 (más la ruta absoluta y un control íntegro) y es reproducible con su generador.
- **El archivo que viaja en el paquete** (``resources/verifier/vigia_verify.py``), ejecutado en un
  proceso aparte con ``-I -S`` (sin ``site-packages``) desde un directorio de trabajo vacío y con
  ``--extract``, rechaza cada paquete hostil con su motivo y su posición, con código 1 y **sin
  extraer nada**: el directorio de trabajo queda vacío. El control sale ``intact`` (código 0) y se
  extrae con los hashes del manifiesto.
- **Cada defensa es la que rechaza su caso**: con la defensa anulada (``..`` y rutas absolutas,
  expansión, enlace simbólico, hashes previos) el caso deja de rechazarse por ese motivo; sin los
  hashes previos, el artefacto alterado se daría por íntegro y **se extraería**.

Se compara por código de salida y por los campos estructurados del resultado, nunca por el texto
(VIG-194). Solo datos sintéticos.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from tests.fixtures.synthetic.hostile_packages import build_hostile_packages
from vigia_platform.accreditation import export_verifier
from vigia_platform.accreditation.export_verifier import verify_export
from vigia_platform.accreditation.verifier_bundle import VERIFIER_FILE

CORPUS: Final = Path(build_hostile_packages.__file__).resolve().parent
REQUIRED: Final = frozenset(
    {
        "traversal",
        "compression_bomb",
        "symlink",
        "manifest_altered",
        "artifact_bit_flip",
        "forged_checkpoint",
        "retired_key_after_retirement",
    }
)
CASES: Final = sorted(path.parent.name for path in CORPUS.glob("*/expected.json"))


def _expected(case: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((CORPUS / case / "expected.json").read_text("utf-8"))
    return document


def _run(case: str, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path, Any]:
    work = tmp_path / "trabajo"
    work.mkdir()
    result = tmp_path / "resultado.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(VERIFIER_FILE),
            str(CORPUS / case),
            "--extract",
            "extraido",
            "--out",
            str(result),
        ],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed, work, json.loads(result.read_text("utf-8"))


def test_the_corpus_has_the_seven_hostile_packages_and_is_reproducible() -> None:
    assert set(CASES) >= REQUIRED | {"control"}
    assert build_hostile_packages.main(["--check"]) == 0


ANCHOR_CASES: Final = frozenset(
    {
        "manifest_stale_signature",
        "manifest_resigned_by_stranger",
        "manifest_signed_with_retired_key",
        "unsigned",
    }
)


def test_the_corpus_has_the_anchor_cases() -> None:
    assert set(CASES) >= ANCHOR_CASES


@pytest.mark.parametrize("case", sorted(set(CASES) - {"control", "unsigned"}))
def test_each_hostile_package_is_rejected_with_its_reason_and_nothing_is_extracted(
    case: str, tmp_path: Path
) -> None:
    expected = _expected(case)
    completed, work, document = _run(case, tmp_path)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert document["status"] == "broken"
    broken = document["broken"]
    assert broken is not None
    assert {key: broken[key] for key in ("reason", "volume", "artifact", "pointer")} == {
        key: expected[key] for key in ("reason", "volume", "artifact", "pointer")
    }
    assert isinstance(broken["offset"], int)
    assert document["extracted_to"] is None
    assert list(work.iterdir()) == [], "el verificador extrajo algo de un paquete roto"


def test_the_control_package_is_intact_and_extracted_with_the_manifest_hashes(
    tmp_path: Path,
) -> None:
    completed, work, document = _run("control", tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert document["status"] == "intact"
    assert document["broken"] is None
    extracted = work / "extraido"
    manifest = json.loads((extracted / "manifest.json").read_text("utf-8"))
    assert document["artifacts_verified"] == len(manifest["artifacts"])
    assert document["checkpoints_verified"] == len(manifest["checkpoints"]) == 2
    for artifact in manifest["artifacts"]:
        data = (extracted / artifact["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
    files = sorted(p.relative_to(extracted).as_posix() for p in extracted.rglob("*") if p.is_file())
    assert files == sorted(
        ["manifest.json", "manifest.signature.json", *(a["path"] for a in manifest["artifacts"])]
    )
    assert document["anchored_by"] == ["manifest_signature"]


def test_an_unsigned_package_is_unanchored_never_intact_and_nothing_is_extracted(
    tmp_path: Path,
) -> None:
    completed, work, document = _run("unsigned", tmp_path)
    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert (document["status"], document["broken"], document["anchored_by"]) == (
        "unanchored",
        None,
        [],
    )
    assert document["extracted_to"] is None
    assert list(work.iterdir()) == []


def _forged(tmp_path: Path, forgery: str, *, keep_signature: bool) -> Path:
    package = build_hostile_packages.control_package()
    original = hashlib.sha256(build_hostile_packages._canonical(package.manifest)).hexdigest()
    if keep_signature:
        package.freeze_signature()
    else:
        package.unsign()
    build_hostile_packages.FORGERIES[forgery](package)
    directory = tmp_path / forgery
    directory.mkdir(parents=True)
    (directory / "vol-01.zip").write_bytes(package.volume())
    (directory / "registered.sha256").write_text(original)
    return directory


@pytest.mark.parametrize("forgery", sorted(build_hostile_packages.FORGERIES))
def test_the_reviewer_forgeries_are_broken_with_the_signature_and_unanchored_without_it(
    forgery: str, tmp_path: Path
) -> None:
    """Las tres sondas del revisor (ronda 1): contenido del registro 1 con su ``content_hash``
    recalculado, ``received_at`` del registro 2, y un hallazgo abierto cerrado con los conteos
    ajustados; todas con el manifiesto re-hasheado. Antes respondían ``intact``."""
    signed = verify_export(_forged(tmp_path / "firmado", forgery, keep_signature=True))
    assert signed.failure is not None
    assert (signed.failure.reason, signed.failure.position.artifact) == (
        "manifest_signature_mismatch",
        "manifest.signature.json",
    )
    unsigned_directory = _forged(tmp_path / "sin_firma", forgery, keep_signature=False)
    unsigned = verify_export(unsigned_directory)
    assert (unsigned.status, unsigned.failure) == ("unanchored", None)
    # Sin firma, el SHA-256 que registró la plataforma (export_produced) también la delata.
    registered = (unsigned_directory / "registered.sha256").read_text()
    with_sha = verify_export(unsigned_directory, manifest_sha256=registered)
    assert with_sha.failure is not None
    assert with_sha.failure.reason == "manifest_hash_mismatch"


@pytest.mark.parametrize(
    ("case", "guard"),
    [
        ("traversal", "check_entry_name"),
        ("absolute_path", "check_entry_name"),
        ("compression_bomb", "check_entry_expansion"),
        ("symlink", "check_entry_type"),
        ("artifact_bit_flip", "check_artifact_hash"),
    ],
)
def test_each_defense_is_the_one_that_rejects_its_case(
    case: str, guard: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Con la defensa anulada, el caso ya no se rechaza por su motivo: la prueba depende de ella."""
    expected = _expected(case)
    with_guard = verify_export(CORPUS / case)
    assert with_guard.failure is not None
    assert with_guard.failure.reason == expected["reason"]
    monkeypatch.setattr(export_verifier, guard, lambda *args, **kwargs: None)
    destination = tmp_path / "extraido"
    without = verify_export(CORPUS / case, extract_to=destination)
    assert without.failure is None or without.failure.reason != expected["reason"]
    if guard == "check_artifact_hash":
        # Sin los hashes previos, el artefacto alterado llega a escribirse: solo lo para la
        # comprobación de respaldo durante la extracción, que borra lo escrito.
        assert without.failure is not None
        assert without.failure.reason == "extract_failed"
        assert not destination.exists()
