"""L-6: SEG-02, hashes antes de extraer. Corpus íntegro en test_laz_hostile_packages.

Mutación: omitir la comparación de SHA-256 en ``export_verifier.check_artifact_hash``
y regenerar el verificador empaquetado que realmente ejecuta la prueba.
"""

from pathlib import Path

from tests.abuse.test_laz_hostile_packages import _run


def test_laz_l06_a_modified_artifact_is_rejected_before_extracting(tmp_path: Path) -> None:
    completed, work, document = _run("artifact_bit_flip", tmp_path)
    assert completed.returncode == 1
    assert document["status"] == "broken"
    assert document["broken"]["reason"] == "artifact_hash_mismatch"
    assert document["extracted_to"] is None
    assert list(work.iterdir()) == []
