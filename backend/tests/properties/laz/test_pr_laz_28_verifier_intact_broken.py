"""PR-LAZ-28: el verificador responde ``intact`` sobre el paquete real y ``broken`` con la posición
sobre cualquier alteración de un bit (TASK-346; LC-LAZ-25; BR-LAZ-90 y 91; NFR-LAZ-53; se apoya en
PR-NUC-19).

**Propiedad** (``exported_packages`` con criptografía real, ``seal_world``): el paquete del
compositor real (TASK-318) leído de sus volúmenes tiene cada artefacto del manifiesto con su hash
(oráculo: los bytes de cada entrada ZIP) y el verificador responde ``intact``, con todos los
artefactos hasheados, los tres puntos de control firmados verificados y la versión del
verificador declarada igual a la embebida.

**Mutaciones** (``package_mutations``), cada una sobre una copia del paquete en disco:

- un bit de cualquier artefacto, manifiesto intacto: ``artifact_hash_mismatch`` en **ese**
  artefacto, su volumen y el desplazamiento de su cabecera;
- un bit del manifiesto, con su SHA-256 registrado (``--manifest-sha256``):
  ``manifest_hash_mismatch``;
- un bit de un registro con contenido o de una entrada de auditoría **re-hasheando el manifiesto**
  (quien altera el paquete): el verificador lo detecta por el ``content_hash`` o el
  ``entry_hash`` recalculado (o por la forma, si el bit rompe un identificador) y señala ese
  registro con su puntero JSON.

Se compara por el código de motivo y los campos estructurados de la posición, nunca por el texto
(VIG-194). Solo datos generados.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.properties.laz.strategies.export import ExportWorld, exported_packages
from tests.verifier_bundle_support import (
    apply_mutation,
    compose_to_directory,
    package_mutations,
    seal_world,
    volume_paths,
)
from vigia_platform.accreditation.export_verifier import verify_export
from vigia_platform.accreditation.verifier_bundle import VERIFIER_PATH, VERIFIER_VERSION

_SETTINGS: Any = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])

RECORD_REASONS = frozenset(
    {"content_hash_mismatch", "entry_hash_mismatch", "artifact_invalid", "content_not_canonical"}
)


def _entries(directory: Path) -> dict[str, tuple[str, int, bytes]]:
    """Cada entrada de los volúmenes: (volumen, desplazamiento de su cabecera, bytes)."""
    found: dict[str, tuple[str, int, bytes]] = {}
    for volume in volume_paths(directory):
        with zipfile.ZipFile(io.BytesIO(volume.read_bytes())) as archive:
            for info in archive.infolist():
                found[info.filename] = (volume.name, info.header_offset, archive.read(info))
    return found


@_SETTINGS
@given(world=exported_packages(), data=st.data())
def test_pr_laz_28_intact_package_and_every_one_bit_mutation_is_broken_with_its_position(
    world: ExportWorld, data: st.DataObject
) -> None:
    sealed = seal_world(world)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        original = root / "original"
        composed = compose_to_directory(sealed, original)
        manifest = composed.manifest

        # Oráculo: cada artefacto del manifiesto existe en su volumen con su tamaño y su hash.
        entries = _entries(original)
        assert set(entries) == {"manifest.json", *(a["path"] for a in manifest["artifacts"])}
        for artifact in manifest["artifacts"]:
            volume, _, content = entries[artifact["path"]]
            assert volume == f"vol-{artifact['volume']:02d}.zip"
            assert len(content) == artifact["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        assert manifest["verifier_version"] == VERIFIER_VERSION
        assert any(a["path"] == VERIFIER_PATH for a in manifest["artifacts"])

        report = verify_export(original, manifest_sha256=composed.package.manifest_sha256)
        assert report.failure is None, report.failure
        assert report.status == "intact"
        assert report.artifacts_verified == len(manifest["artifacts"])
        assert report.checkpoints_verified == len(manifest["checkpoints"]) == 3
        assert report.declared_verifier_version == VERIFIER_VERSION
        assert report.manifest_sha256 == composed.package.manifest_sha256

        mutation = data.draw(package_mutations(composed), label="mutation")
        altered = root / "altered"
        shutil.copytree(original, altered)
        apply_mutation(composed, altered, mutation)
        broken = verify_export(altered, manifest_sha256=composed.package.manifest_sha256)
        failure = broken.failure
        assert failure is not None, f"{mutation} no se detectó"
        position = failure.position

        if mutation.kind == "artifact_bit":
            volume, offset, _ = _entries(altered)[mutation.path]
            assert failure.reason == "artifact_hash_mismatch"
            assert (position.volume, position.artifact, position.offset) == (
                volume,
                mutation.path,
                offset,
            )
        elif mutation.kind == "manifest_bit":
            assert failure.reason == "manifest_hash_mismatch"
            assert (position.volume, position.artifact) == ("vol-01.zip", "manifest.json")
        else:
            # Sin el SHA-256 registrado, el manifiesto re-hasheado no delata nada: el registro sí.
            unanchored = verify_export(altered).failure
            assert unanchored is not None
            assert unanchored.reason in RECORD_REASONS, unanchored
            assert unanchored.position.artifact == mutation.path
            assert unanchored.position.pointer is not None
            assert unanchored.position.pointer.startswith(mutation.record), (
                unanchored.position.pointer,
                mutation,
            )
            # Con el SHA-256 registrado, el manifiesto alterado cae antes que nada.
            assert failure.reason == "manifest_hash_mismatch"
