"""PR-LAZ-28: el verificador responde ``intact`` sobre el paquete real y ``broken`` con la posición
sobre cualquier alteración de un bit (TASK-346; LC-LAZ-25; BR-LAZ-90 y 91; NFR-LAZ-53; se apoya en
PR-NUC-19).

**Propiedad** (``exported_packages`` con criptografía real, ``seal_world``): el paquete del
compositor real (TASK-318) leído de sus volúmenes tiene cada artefacto del manifiesto con su hash
(oráculo: los bytes de cada entrada ZIP) y el verificador responde ``intact``, con todos los
artefactos hasheados, los tres puntos de control firmados verificados, la versión del
verificador declarada igual a la embebida y el paquete **anclado por la firma de su manifiesto**
(solo con lo que viaja en él, como un tercero sin acceso a la plataforma).

**Mutaciones** (``package_mutations``), cada una sobre una copia del paquete en disco y
verificada por un tercero (solo la firma) y con el SHA-256 registrado (``--manifest-sha256``):

- un bit de cualquier artefacto, manifiesto intacto: ``artifact_hash_mismatch`` en **ese**
  artefacto, su volumen y el desplazamiento de su cabecera;
- un bit del manifiesto: roto para el tercero; ``manifest_hash_mismatch`` con el SHA registrado;
- una **falsificación coherente** de un registro o de una entrada de auditoría (ronda 1, decisión
  del dueño A2 y B1): el atacante cambia un bit, rehace el ``content_hash`` o el ``entry_hash`` y
  re-hashea el manifiesto. Si deja la firma, ``manifest_signature_mismatch``; si firma con una
  clave ajena bajo el ``key_id`` activo, ``bad_signature``; si la quita, nunca ``intact``
  (``unanchored``); con el SHA registrado, ``manifest_hash_mismatch``.

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

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests.properties.laz.strategies.export import ExportWorld, exported_packages
from tests.verifier_bundle_support import (
    Mutation,
    apply_mutation,
    compose_to_directory,
    package_mutations,
    seal_world,
    volume_paths,
)
from vigia_platform.accreditation.export_verifier import verify_export
from vigia_platform.accreditation.verifier_bundle import (
    MANIFEST_SIGNATURE_DECLARATION,
    VERIFIER_PATH,
    VERIFIER_VERSION,
)

_SETTINGS: Any = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


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
        registered = composed.package.manifest_sha256

        # Oráculo: cada artefacto del manifiesto existe en su volumen con su tamaño y su hash.
        entries = _entries(original)
        assert set(entries) == {
            "manifest.json",
            "manifest.signature.json",
            *(a["path"] for a in manifest["artifacts"]),
        }
        assert entries["manifest.signature.json"][0] == "vol-01.zip"
        for artifact in manifest["artifacts"]:
            volume, _, content = entries[artifact["path"]]
            assert volume == f"vol-{artifact['volume']:02d}.zip"
            assert len(content) == artifact["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        assert manifest["verifier_version"] == VERIFIER_VERSION
        assert any(a["path"] == VERIFIER_PATH for a in manifest["artifacts"])
        assert manifest["manifest_signature"] == MANIFEST_SIGNATURE_DECLARATION

        # Un tercero sin acceso a la plataforma: solo la firma que viaja en el paquete.
        report = verify_export(original)
        assert report.failure is None, report.failure
        assert (report.status, report.anchored_by) == ("intact", ["manifest_signature"])
        assert report.artifacts_verified == len(manifest["artifacts"])
        assert report.checkpoints_verified == len(manifest["checkpoints"]) == 3
        assert report.declared_verifier_version == VERIFIER_VERSION
        assert report.manifest_sha256 == registered
        both = verify_export(original, manifest_sha256=registered)
        assert (both.status, both.anchored_by) == (
            "intact",
            ["manifest_sha256", "manifest_signature"],
        )

        mutation = data.draw(package_mutations(composed), label="mutation")
        altered = root / "altered"
        shutil.copytree(original, altered)
        apply_mutation(composed, altered, mutation)
        third_party = verify_export(altered)
        assert not third_party.intact, f"{mutation} no se detectó"
        failure = third_party.failure

        if mutation.kind == "artifact_bit":
            volume, offset, _ = _entries(altered)[mutation.path]
            assert failure is not None
            assert failure.reason == "artifact_hash_mismatch"
            assert (
                failure.position.volume,
                failure.position.artifact,
                failure.position.offset,
            ) == (volume, mutation.path, offset)
        elif mutation.kind == "manifest_bit":
            # Cualquier bit del manifiesto lo rompe: forma, firma o los hashes que declara.
            assert failure is not None, third_party.status
        elif mutation.signature == "keep":
            # Falsificación coherente (hash propio rehecho, manifiesto re-hasheado): la firma de la
            # plataforma ya no corresponde al manifiesto.
            assert failure is not None
            assert (failure.reason, failure.position.artifact) == (
                "manifest_signature_mismatch",
                "manifest.signature.json",
            )
        elif mutation.signature == "stranger":
            assert failure is not None
            assert (failure.reason, failure.position.artifact) == (
                "bad_signature",
                "manifest.signature.json",
            )
        else:
            # Sin firma: nunca «íntegro» (decisión B1).
            assert third_party.status in ("unanchored", "broken")

        # Con el SHA-256 que registró la plataforma, toda alteración del manifiesto cae primero.
        anchored = verify_export(altered, manifest_sha256=registered)
        assert anchored.failure is not None
        if mutation.kind != "artifact_bit":
            assert (anchored.failure.reason, anchored.failure.position.artifact) == (
                "manifest_hash_mismatch",
                "manifest.json",
            )


_REGRESSION: Any = settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


@_REGRESSION
@given(world=exported_packages())
def test_pr_laz_28_regression_a_flipped_entry_hash_is_never_restored(world: ExportWorld) -> None:
    """Contraejemplo de la ronda 2: ``Mutation(record_bit, audit/audit-log.json,
    pointer=/entries/0/entry_hash, signature=keep)``. El atacante simulado rehacía el
    ``entry_hash`` que acababa de alterar, el paquete quedaba idéntico y salía ``intact``. Ahora el
    bit alterado se conserva y cada forma de tratar la firma tiene su veredicto fijo."""
    sealed = seal_world(world)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        original = root / "original"
        composed = compose_to_directory(sealed, original)
        assume(composed.json("audit/audit-log.json")["entries"])
        expected = {"keep": "manifest_signature_mismatch", "stranger": "bad_signature"}
        for signature in ("keep", "drop", "stranger"):
            mutation = Mutation(
                "record_bit",
                "audit/audit-log.json",
                0,
                "/entries/0/entry_hash",
                "/entries/0",
                signature,
            )
            altered = root / signature
            shutil.copytree(original, altered)
            apply_mutation(composed, altered, mutation)
            assert _entries(altered) != _entries(original), "la mutación no cambió el paquete"
            report = verify_export(altered)
            assert not report.intact, mutation
            if signature == "drop":
                # Sin firma, el entry_hash alterado no cuadra con su sobre.
                assert report.failure is not None
                assert report.failure.reason in ("entry_hash_mismatch", "artifact_invalid")
                assert report.failure.position.pointer is not None
                assert report.failure.position.pointer.startswith("/entries/0")
            else:
                assert report.failure is not None
                assert report.failure.reason == expected[signature]
