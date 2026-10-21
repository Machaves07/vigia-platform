"""Bordes del verificador del paquete exportado (TASK-346; LC-LAZ-25; PAT-LAZ-SEG-02; NFR-LAZ-23).

Sobre el paquete de control del corpus (``build_hostile_packages.control_package``), alterado
caso a caso, y sobre las comprobaciones de cada entrada con ``ZipInfo`` construidos a mano:

- rutas: ``..`` (también con barra inversa), absolutas (``/``, ``\\``, letra de unidad), vacías,
  ocultas, con espacio, no ASCII, de 255 y 256 caracteres y con un NUL escondido;
- tipos: enlace simbólico, directorio, FIFO, archivo regular y modo DOS;
- expansión: exactamente 100 veces pasa, una más no; exactamente 2 GB pasa, uno más no; métodos
  no admitidos y entradas cifradas;
- límite de entradas por volumen y total, ZIP64 y varios discos en el registro final;
- volúmenes: huecos, enlaces simbólicos, ruta de ``vol-02.zip``, nombre libre de un solo volumen;
- manifiesto: falta, versión, JSON inválido, SHA-256 registrado, entradas que sobran, faltan, se
  repiten o cambian de tamaño;
- contenido: claves, puntos de control (clave desconocida, vigencia en sus bordes semiabiertos,
  cobertura), enlaces, ``content_hash``, ``entry_hash``, declaraciones del manifiesto;
- el ancla del manifiesto: firma (forma, declaración, clave, vigencia) y `unanchored` sin ella;
- rutas que chocan al extraer (prefijo de directorio y mayúsculas) y fallos de escritura;
- la línea de órdenes: códigos 0, 1, 2 y 3 y la extracción;
- las listas cerradas del módulo puro frente a las de la plataforma.

Se compara por motivo y posición estructurada, nunca por el texto (VIG-194). Solo datos
sintéticos.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import stat
import struct
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.fixtures.synthetic.hostile_packages.build_hostile_packages import (
    ACTIVE_KEY,
    RETIRED_KEY,
    SEEDS,
    Entry,
    Package,
    _checkpoint,
    close_open_finding,
    control_package,
)
from vigia_platform.accreditation.export_verifier import (
    CHECKPOINT_CONTENT_KEYS,
    KEY_STATUSES,
    OPEN_LIFECYCLE_STATES,
    ExportBroken,
    ExportLimits,
    _end_of_directory_entries,
    _Entry,
    check_entry_expansion,
    check_entry_name,
    check_entry_type,
    export_main,
    verify_export,
)
from vigia_platform.accreditation.manifest.build import OPEN_STATES
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.ledger.chain.checkpoints import CheckpointContent
from vigia_platform.shared.signing.keys import KeyStatus

FINDINGS = "findings/zone-018f0000-0000-7000-8000-00000000c001.json"
AUDIT = "audit/audit-log.json"
CHECKPOINTS = "checkpoints/checkpoints.json"
KEYS = "keys/checkpoint-public-keys.json"


def _write(package: Package, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "vol-01.zip").write_bytes(package.volume())
    return directory


def _reason(directory: Path, **kwargs: Any) -> str | None:
    report = verify_export(directory, **kwargs)
    return None if report.failure is None else report.failure.reason


def _entry(name: str, **attributes: Any) -> _Entry:
    info = zipfile.ZipInfo(name)
    info.header_offset = 0
    for key, value in attributes.items():
        setattr(info, key, value)
    return _Entry("vol-01.zip", info)


# --- cada entrada -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("findings/zone-a.json", None),
        ("a" * 255, None),
        ("a" * 256, "path_not_allowed"),
        ("a/../b", "path_traversal"),
        ("..", "path_traversal"),
        ("..\\evil", "path_traversal"),
        ("a\\..\\b", "path_traversal"),
        ("/etc/passwd", "absolute_path"),
        ("\\windows\\evil", "absolute_path"),
        ("C:/evil", "absolute_path"),
        ("c:evil", "absolute_path"),
        ("a//b", "path_not_allowed"),
        ("a/", "path_not_allowed"),
        ("", "path_not_allowed"),
        (".hidden", "path_not_allowed"),
        ("a b", "path_not_allowed"),
        ("zoné.json", "path_not_allowed"),
        ("a\\b", "path_not_allowed"),
    ],
)
def test_entry_names(name: str, reason: str | None) -> None:
    entry = _entry("placeholder")
    entry.info.filename = name
    entry.info.orig_filename = name
    if reason is None:
        check_entry_name(entry)
        return
    with pytest.raises(ExportBroken) as raised:
        check_entry_name(entry)
    assert raised.value.failure.reason == reason
    assert raised.value.failure.position.artifact == name


def test_a_hidden_nul_in_the_stored_name_is_rejected() -> None:
    # zipfile corta el nombre en el NUL: el nombre original lo delata.
    entry = _entry("seguro.json")
    entry.info.orig_filename = "seguro.json\x00../../evil"
    with pytest.raises(ExportBroken) as raised:
        check_entry_name(entry)
    assert raised.value.failure.reason == "path_traversal"


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        (stat.S_IFLNK | 0o777, "symlink"),
        (stat.S_IFDIR | 0o755, "special_file"),
        (stat.S_IFIFO | 0o644, "special_file"),
        (stat.S_IFCHR | 0o644, "special_file"),
        (stat.S_IFREG | 0o644, None),
        (0, None),
    ],
)
def test_entry_types(mode: int, reason: str | None) -> None:
    entry = _entry("a.json", external_attr=mode << 16)
    if reason is None:
        check_entry_type(entry)
        return
    with pytest.raises(ExportBroken) as raised:
        check_entry_type(entry)
    assert raised.value.failure.reason == reason


LIMITS = ExportLimits(max_entry_bytes=2 * 1000**3)


@pytest.mark.parametrize(
    ("method", "size", "compressed", "flags", "reason"),
    [
        (zipfile.ZIP_DEFLATED, 100 * 1000, 1000, 0, None),
        (zipfile.ZIP_DEFLATED, 100 * 1000 + 1, 1000, 0, "expansion_ratio"),
        (zipfile.ZIP_DEFLATED, 0, 0, 0, None),
        (zipfile.ZIP_DEFLATED, 1, 0, 0, "expansion_ratio"),
        (zipfile.ZIP_STORED, 2 * 1000**3, 2 * 1000**3, 0, None),
        (zipfile.ZIP_STORED, 2 * 1000**3 + 1, 2 * 1000**3 + 1, 0, "entry_too_large"),
        (zipfile.ZIP_STORED, 10, 9, 0, "expansion_ratio"),
        (zipfile.ZIP_BZIP2, 10, 10, 0, "compression_not_allowed"),
        (zipfile.ZIP_LZMA, 10, 10, 0, "compression_not_allowed"),
        (zipfile.ZIP_STORED, 10, 10, 0x1, "compression_not_allowed"),
    ],
)
def test_entry_expansion_borders(
    method: int, size: int, compressed: int, flags: int, reason: str | None
) -> None:
    entry = _entry(
        "a.json",
        compress_type=method,
        file_size=size,
        compress_size=compressed,
        flag_bits=flags,
    )
    if reason is None:
        check_entry_expansion(entry, LIMITS)
        return
    with pytest.raises(ExportBroken) as raised:
        check_entry_expansion(entry, LIMITS)
    assert raised.value.failure.reason == reason


def _end_record(entries: int, *, disk: int = 0, size: int = 46, offset: int = 0) -> bytes:
    return b"PK\x05\x06" + struct.pack("<4H2LH", disk, 0, entries, entries, size, offset, 0)


@pytest.mark.parametrize(
    ("tail", "result"),
    [
        (_end_record(3), 3),
        (_end_record(0xFFFF), "zip64_not_allowed"),
        (_end_record(3, disk=1), "zip64_not_allowed"),
        (_end_record(3, size=0xFFFFFFFF), "zip64_not_allowed"),
        (_end_record(3, offset=0xFFFFFFFF), "zip64_not_allowed"),
        (b"PK\x06\x07" + bytes(16) + _end_record(3), "zip64_not_allowed"),
        (b"no es un zip", "package_unreadable"),
    ],
)
def test_end_of_directory_record(tail: bytes, result: int | str) -> None:
    handle = io.BytesIO(b"x" * 64 + tail)
    if isinstance(result, int):
        assert _end_of_directory_entries(handle, "vol-01.zip") == result
        return
    with pytest.raises(ExportBroken) as raised:
        _end_of_directory_entries(handle, "vol-01.zip")
    assert raised.value.failure.reason == result


def test_entry_limits_per_volume_and_in_total(tmp_path: Path) -> None:
    directory = _write(control_package(), tmp_path / "p")
    entries = 7  # cinco artefactos, el manifiesto y su firma
    assert _reason(directory, limits=ExportLimits(max_entries=entries)) is None
    assert _reason(directory, limits=ExportLimits(max_entries=entries - 1)) == "too_many_entries"
    assert _reason(directory, limits=ExportLimits(max_volume_entries=entries)) is None
    limits = ExportLimits(max_volume_entries=entries - 1)
    assert _reason(directory, limits=limits) == "too_many_entries"


# --- volúmenes ----------------------------------------------------------------------------------


def test_the_control_package_is_intact_from_the_directory_and_from_vol_01(tmp_path: Path) -> None:
    directory = _write(control_package(), tmp_path / "p")
    assert _reason(directory) is None
    assert _reason(directory / "vol-01.zip") is None
    single = tmp_path / "paquete.zip"
    single.write_bytes((directory / "vol-01.zip").read_bytes())
    assert _reason(single) is None


def test_volume_discovery_rejects_gaps_symlinks_and_later_volumes(tmp_path: Path) -> None:
    directory = _write(control_package(), tmp_path / "p")
    empty = tmp_path / "vacio"
    empty.mkdir()
    assert _reason(empty) == "package_unreadable"
    (directory / "vol-03.zip").write_bytes(b"")
    assert _reason(directory) == "package_unreadable"
    (directory / "vol-03.zip").unlink()
    (directory / "vol-02.zip").write_bytes((directory / "vol-01.zip").read_bytes())
    assert _reason(directory / "vol-02.zip") == "package_unreadable"
    (directory / "vol-02.zip").unlink()
    (directory / "vol-02.zip").symlink_to(directory / "vol-01.zip")
    assert _reason(directory) == "symlink"
    (directory / "vol-02.zip").unlink()
    link = tmp_path / "enlace"
    link.symlink_to(directory)
    assert _reason(link) == "symlink"
    assert _reason(tmp_path / "no-existe") == "package_unreadable"
    (directory / "vol-01.zip").write_bytes(b"no es un zip")
    assert _reason(directory) == "package_unreadable"


# --- manifiesto y hashes previos ----------------------------------------------------------------


def _changed(change: Callable[[Package], None]) -> Package:
    package = control_package()
    change(package)
    return package


def _set_manifest(key: str, value: Any) -> Callable[[Package], None]:
    def change(package: Package) -> None:
        package.manifest[key] = value

    return change


@pytest.mark.parametrize(
    ("change", "reason", "artifact"),
    [
        (
            lambda p: p.entries.append(Entry("sobra.txt", b"x")),
            "unlisted_entry",
            "sobra.txt",
        ),
        (
            lambda p: p.entries.pop(0),
            "artifact_missing",
            FINDINGS,
        ),
        (
            lambda p: p.manifest["artifacts"][0].update(size_bytes=1),
            "artifact_size_mismatch",
            FINDINGS,
        ),
        (
            lambda p: p.manifest["artifacts"][0].update(volume=2),
            "artifact_missing",
            FINDINGS,
        ),
        (
            lambda p: p.manifest["artifacts"][0].update(sha256="0" * 64),
            "artifact_hash_mismatch",
            FINDINGS,
        ),
        (_set_manifest("manifest_version", 0), "manifest_invalid", "manifest.json"),
        (_set_manifest("manifest_version", 3), "manifest_invalid", "manifest.json"),
        (_set_manifest("manifest_version", True), "manifest_invalid", "manifest.json"),
        (_set_manifest("artifacts", {}), "manifest_invalid", "manifest.json"),
        (_set_manifest("verifier_version", None), "manifest_invalid", "manifest.json"),
        (_set_manifest("organization", {"organization_id": "X"}), "manifest_invalid", None),
        (
            lambda p: p.manifest["artifacts"].append(dict(p.manifest["artifacts"][0])),
            "manifest_invalid",
            "manifest.json",
        ),
        (
            lambda p: p.manifest["artifacts"][0].update(path="../fuera.json"),
            "manifest_invalid",
            "manifest.json",
        ),
    ],
)
def test_manifest_and_hashes_before_extraction(
    change: Callable[[Package], None], reason: str, artifact: str | None, tmp_path: Path
) -> None:
    directory = _write(_changed(change), tmp_path / "p")
    report = verify_export(directory)
    assert report.failure is not None
    assert report.failure.reason == reason
    if artifact is not None:
        assert report.failure.position.artifact == artifact


def test_manifest_missing_unreadable_and_duplicated_entries(tmp_path: Path) -> None:
    package = control_package()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for entry in package.entries:
            archive.writestr(entry.name, entry.data)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "vol-01.zip").write_bytes(output.getvalue())
    assert _reason(tmp_path / "a") == "manifest_missing"

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", b'{"manifest_version": NaN}')
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "vol-01.zip").write_bytes(output.getvalue())
    assert _reason(tmp_path / "b") == "manifest_invalid"

    package.entries.append(Entry(package.entries[0].name, package.entries[0].data))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile avisa del nombre repetido
        directory = _write(package, tmp_path / "c")
    assert _reason(directory) == "duplicate_entry"


def test_registered_manifest_sha256(tmp_path: Path) -> None:
    directory = _write(control_package(), tmp_path / "p")
    digest = verify_export(directory).manifest_sha256
    assert digest is not None
    assert _reason(directory, manifest_sha256=digest) is None
    assert _reason(directory, manifest_sha256="0" * 64) == "manifest_hash_mismatch"


# --- contenido ----------------------------------------------------------------------------------


def _replace_checkpoint(package: Package, checkpoint: dict[str, Any]) -> None:
    checkpoints = package.documents[CHECKPOINTS]["checkpoints"]
    checkpoints[0] = checkpoint
    package.manifest["checkpoints"] = checkpoints
    package.set_document(CHECKPOINTS, {"checkpoints": checkpoints})


def _signed(key_id: str, taken_at: str) -> Callable[[Package], None]:
    def change(package: Package) -> None:
        original = package.documents[CHECKPOINTS]["checkpoints"][0]
        checkpoint = _checkpoint(
            "ledger",
            2,
            original["content"]["covered_hash"],
            seed=SEEDS[key_id],
            key_id=key_id,
            taken_at=taken_at,
        )
        _replace_checkpoint(package, checkpoint)

    return change


@pytest.mark.parametrize(
    ("key_id", "taken_at", "reason"),
    [
        (RETIRED_KEY, "2025-01-01T00:00:00.000Z", None),
        (RETIRED_KEY, "2025-12-31T23:59:59.999Z", None),
        (RETIRED_KEY, "2026-01-01T00:00:00.000Z", "key_not_valid_at_signing"),
        (RETIRED_KEY, "2024-12-31T23:59:59.999Z", "key_not_valid_at_signing"),
        (ACTIVE_KEY, "2026-01-01T00:00:00.000Z", None),
        (ACTIVE_KEY, "2025-12-31T23:59:59.999Z", "key_not_valid_at_signing"),
        (ACTIVE_KEY, "2027-06-01T00:00:00.000Z", "key_not_valid_at_signing"),
    ],
)
def test_key_validity_is_half_open(
    key_id: str, taken_at: str, reason: str | None, tmp_path: Path
) -> None:
    directory = _write(_changed(_signed(key_id, taken_at)), tmp_path / "p")
    assert _reason(directory) == reason


def _document(path: str, change: Callable[[Any], None], manifest_key: str = "") -> Any:
    def apply(package: Package) -> None:
        document = json.loads(json.dumps(package.documents[path]))
        change(document)
        if manifest_key:
            package.manifest[manifest_key] = document[next(iter(document))]
        package.set_document(path, document)

    return apply


def _raw(path: str, old: bytes, new: bytes) -> Callable[[Package], None]:
    """Cambia los bytes del artefacto a mano (JSON legible sin forma canónica) y re-hashea."""

    def apply(package: Package) -> None:
        entry = next(item for item in package.entries if item.name == path)
        assert entry.data.count(old) == 1
        entry.data = entry.data.replace(old, new)
        for artifact in package.manifest["artifacts"]:
            if artifact["path"] == path:
                artifact["sha256"] = hashlib.sha256(entry.data).hexdigest()
                artifact["size_bytes"] = len(entry.data)

    return apply


def _record(index: int, **values: Any) -> Callable[[Any], None]:
    def change(document: Any) -> None:
        document["findings"][0]["records"][index].update(values)

    return change


@pytest.mark.parametrize(
    ("change", "reason", "artifact", "pointer"),
    [
        (
            _document(FINDINGS, _record(1, content={"outcome": "dismissed", "n": 2})),
            "content_hash_mismatch",
            FINDINGS,
            "/findings/0/records/1/content_hash",
        ),
        (
            _document(FINDINGS, _record(1, previous_hash="1" * 64)),
            "previous_hash_mismatch",
            FINDINGS,
            "/findings/0/records/1/previous_hash",
        ),
        (
            _document(FINDINGS, _record(1, record_hash="2" * 64)),
            "checkpoint_mismatch",
            FINDINGS,
            "/findings/0/records/1/record_hash",
        ),
        (
            _document(FINDINGS, _record(1, chain_sequence=1)),
            "record_conflict",
            FINDINGS,
            "/findings/0/records/1",
        ),
        (
            _raw(FINDINGS, b'"n":1}', b'"n":"\\ud800"}'),
            "content_not_canonical",
            FINDINGS,
            None,
        ),
        (
            _document(AUDIT, lambda d: d["entries"][1].update(result_count=99)),
            "entry_hash_mismatch",
            AUDIT,
            "/entries/1/entry_hash",
        ),
        (
            _document(AUDIT, lambda d: d["entries"][1].update(extra=1)),
            "artifact_invalid",
            AUDIT,
            "/entries/1",
        ),
        (
            _document(
                CHECKPOINTS,
                lambda d: d["checkpoints"][0]["content"].update(key_id="otra"),
                "checkpoints",
            ),
            "unknown_key",
            CHECKPOINTS,
            "/checkpoints/0",
        ),
        (
            _document(CHECKPOINTS, lambda d: d["checkpoints"][0].update(sequence=9), "checkpoints"),
            "checkpoint_invalid",
            CHECKPOINTS,
            "/checkpoints/0",
        ),
        (
            _document(
                CHECKPOINTS,
                lambda d: d["checkpoints"].append(d["checkpoints"][0]),
                "checkpoints",
            ),
            "checkpoint_invalid",
            CHECKPOINTS,
            "/checkpoints/2",
        ),
        (
            _document(CHECKPOINTS, lambda d: d["checkpoints"].pop()),
            "manifest_mismatch",
            "manifest.json",
            "/checkpoints",
        ),
        (
            _document(
                KEYS, lambda d: d["keys"][0].update(status="revoked"), "checkpoint_public_keys"
            ),
            "key_invalid",
            KEYS,
            "/keys/0",
        ),
        (
            _document(
                KEYS,
                lambda d: d["keys"][0].update(valid_until=d["keys"][0]["valid_from"]),
                "checkpoint_public_keys",
            ),
            "key_invalid",
            KEYS,
            "/keys/0",
        ),
        (
            _document(KEYS, lambda d: d["keys"].pop(0)),
            "manifest_mismatch",
            "manifest.json",
            "/checkpoint_public_keys",
        ),
        (
            _set_manifest("evidence_flagged_count", 1),
            "manifest_mismatch",
            "manifest.json",
            "/evidence_flagged_count",
        ),
        (
            _set_manifest("verifier_version", "export-verifier.1"),
            "manifest_mismatch",
            "manifest.json",
            "/verifier_version",
        ),
        (
            _set_manifest(
                "clips",
                [{"included": True, "path": "clips/x.mp4", "sha256": "0" * 64, "size_bytes": 1}],
            ),
            "manifest_mismatch",
            "manifest.json",
            "/clips/0",
        ),
        (
            _set_manifest("absent_entries", []),
            "manifest_mismatch",
            "manifest.json",
            "/absent_entries",
        ),
        (
            _set_manifest("zones", []),
            "manifest_mismatch",
            "manifest.json",
            "/zones",
        ),
    ],
)
def test_content_checks_with_the_manifest_rehashed(
    change: Callable[[Package], None],
    reason: str,
    artifact: str,
    pointer: str | None,
    tmp_path: Path,
) -> None:
    directory = _write(_changed(change), tmp_path / "p")
    report = verify_export(directory)
    assert report.failure is not None, "la alteración no se detectó"
    assert report.failure.reason == reason
    assert report.failure.position.artifact == artifact
    if pointer is not None:
        assert report.failure.position.pointer == pointer


def test_hostile_json_never_escapes_as_an_exception(tmp_path: Path) -> None:
    deep = "[" * 100_000 + "]" * 100_000
    for index, text in enumerate([deep, '{"entries": [1e999]}', '{"entries": [1, 1, ']):
        package = control_package()
        entry = next(item for item in package.entries if item.name == AUDIT)
        entry.data = text.encode()
        for artifact in package.manifest["artifacts"]:
            if artifact["path"] == AUDIT:
                artifact["sha256"] = hashlib.sha256(entry.data).hexdigest()
                artifact["size_bytes"] = len(entry.data)
        assert _reason(_write(package, tmp_path / str(index))) == "artifact_invalid"


# --- línea de órdenes ---------------------------------------------------------------------------


def test_command_line_exit_codes_and_extraction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _write(control_package(), tmp_path / "p")
    out = tmp_path / "r.json"
    assert export_main([str(directory), "--out", str(out), "--extract", str(tmp_path / "x")]) == 0
    document = json.loads(out.read_text("utf-8"))
    assert (document["status"], document["broken"]) == ("intact", None)
    assert (tmp_path / "x" / "manifest.json").is_file()
    assert (tmp_path / "x" / FINDINGS).is_file()

    empty = tmp_path / "vacio"
    empty.mkdir()
    assert export_main([str(directory), "--extract", str(empty)]) == 0
    assert (empty / AUDIT).is_file()

    assert export_main([str(tmp_path / "no-existe")]) == 2
    assert export_main([str(directory), "--manifest-sha256", "ABC"]) == 2
    assert export_main([str(directory), "--extract", str(tmp_path / "x")]) == 2
    assert export_main([str(directory), "--extract", str(out)]) == 2

    broken = _write(_changed(_set_manifest("finding_count", 7)), tmp_path / "b")
    target = tmp_path / "nada"
    assert export_main([str(broken), "--out", str(out), "--extract", str(target)]) == 1
    document = json.loads(out.read_text("utf-8"))
    assert document["broken"]["reason"] == "manifest_mismatch"
    assert document["broken"]["pointer"] == "/finding_count"
    assert not target.exists()
    capsys.readouterr()


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        export_main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == "vigia_verify export-verifier.2\n"


# --- listas cerradas frente a la plataforma ------------------------------------------------------


def test_closed_lists_match_the_platform() -> None:
    assert set(OPEN_LIFECYCLE_STATES) == {state.value for state in OPEN_STATES}
    assert set(KEY_STATUSES) == {status.value for status in KeyStatus}
    content = CheckpointContent(
        covered_sequence=1, covered_hash="0" * 64, taken_at="x", key_id="k", signature="s"
    )
    assert set(content.to_json()) == CHECKPOINT_CONTENT_KEYS


# --- ancla del manifiesto (ronda 1: decisión del dueño A2 y B1) --------------------------------


def test_without_signature_or_registered_sha_the_package_is_never_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = control_package()
    package.unsign()
    directory = _write(package, tmp_path / "p")
    report = verify_export(directory, extract_to=tmp_path / "x")
    assert (report.status, report.failure, report.anchored_by) == ("unanchored", None, [])
    assert not report.intact
    assert not (tmp_path / "x").exists()
    out = tmp_path / "r.json"
    assert export_main([str(directory), "--out", str(out), "--extract", str(tmp_path / "y")]) == 3
    document = json.loads(out.read_text("utf-8"))
    assert (document["status"], document["anchored_by"], document["extracted_to"]) == (
        "unanchored",
        [],
        None,
    )
    assert not (tmp_path / "y").exists()
    # Con el SHA-256 registrado por la plataforma, el mismo paquete sí queda anclado.
    digest = report.manifest_sha256
    assert digest is not None
    anchored = verify_export(directory, manifest_sha256=digest)
    assert (anchored.status, anchored.anchored_by) == ("intact", ["manifest_sha256"])
    capsys.readouterr()


def _declaration(value: Any) -> Callable[[Package], None]:
    def change(package: Package) -> None:
        package.manifest["manifest_signature"] = value

    return change


@pytest.mark.parametrize(
    ("change", "reason", "artifact"),
    [
        (lambda p: p.manifest.update(generated_at="2026-05-02T00:00:00.000Z"), None, None),
        (_declaration(None), "manifest_signature_invalid", "manifest.json"),
        (_declaration({"path": "otra.json"}), "manifest_signature_invalid", "manifest.json"),
        (
            lambda p: setattr(p, "frozen_signature", b'{"payload": {}}'),
            "manifest_signature_invalid",
            "manifest.signature.json",
        ),
        (
            lambda p: setattr(p, "frozen_signature", b"no es json"),
            "manifest_signature_invalid",
            "manifest.signature.json",
        ),
        (
            lambda p: setattr(p, "signer", (SEEDS[ACTIVE_KEY], "otra-clave")),
            "unknown_key",
            "manifest.signature.json",
        ),
    ],
)
def test_manifest_signature_shapes(
    change: Callable[[Package], None], reason: str | None, artifact: str | None, tmp_path: Path
) -> None:
    directory = _write(_changed(change), tmp_path / "p")
    report = verify_export(directory)
    if reason is None:
        assert (report.status, report.anchored_by) == ("intact", ["manifest_signature"])
        return
    assert report.failure is not None
    assert (report.failure.reason, report.failure.position.artifact) == (reason, artifact)


def test_a_declared_signature_that_is_missing_breaks_the_package(tmp_path: Path) -> None:
    package = control_package()
    package.signer = None  # sin firma, pero el manifiesto la sigue declarando
    report = verify_export(_write(package, tmp_path / "p"))
    assert report.failure is not None
    assert report.failure.reason == "manifest_signature_invalid"


@pytest.mark.parametrize(
    ("generated_at", "reason"),
    [
        ("2026-01-01T00:00:00.000Z", None),
        ("2025-12-31T23:59:59.999Z", "key_not_valid_at_signing"),
        ("2027-06-01T00:00:00.000Z", "key_not_valid_at_signing"),
    ],
)
def test_signature_key_validity_is_half_open(
    generated_at: str, reason: str | None, tmp_path: Path
) -> None:
    directory = _write(
        _changed(lambda p: p.manifest.update(generated_at=generated_at)), tmp_path / "p"
    )
    assert _reason(directory) == reason


# --- rutas que chocan al extraer y fallos de escritura -------------------------------------------


@pytest.mark.parametrize(
    ("names", "reason"),
    [
        (("x", "x/y"), "path_not_allowed"),
        (("x/y", "x"), "path_not_allowed"),
        (("Ab", "ab"), "duplicate_entry"),
        (("dir/Ab.json", "DIR/ab.json"), "duplicate_entry"),
        (("Dir/a", "dir"), "path_not_allowed"),
    ],
)
def test_paths_that_collide_on_extraction_are_rejected(
    names: tuple[str, str], reason: str, tmp_path: Path
) -> None:
    package = control_package()
    for name in names:
        package.entries.append(Entry(name, b"x", kind="coverage"))
        package.manifest["artifacts"].append(
            {
                "path": name,
                "kind": "coverage",
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "size_bytes": 1,
                "volume": 1,
            }
        )
    out = tmp_path / "r.json"
    destination = tmp_path / "x"
    code = export_main(
        [str(_write(package, tmp_path / "p")), "--out", str(out), "--extract", str(destination)]
    )
    assert code == 1
    broken = json.loads(out.read_text("utf-8"))["broken"]
    assert (broken["reason"], broken["artifact"]) == (reason, names[1])
    assert not destination.exists()


def test_an_os_error_while_extracting_is_extract_failed_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _write(control_package(), tmp_path / "p")
    real_open = Path.open

    def failing_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "xb" and self.name.endswith(".json") and "audit" in self.parts:
            raise OSError(28, "No queda espacio en el dispositivo")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    destination = tmp_path / "x"
    report = verify_export(directory, extract_to=destination)
    assert report.failure is not None
    assert (report.failure.reason, report.failure.position.artifact) == ("extract_failed", AUDIT)
    assert not destination.exists()


# --- ronda 2: campos del mensaje firmado, bytes canónicos y qué clave ancló ---------------------


def _signed_payload(package: Package, **changes: str) -> bytes:
    """Una firma válida de la clave activa sobre el SHA correcto con otros campos cambiados."""
    manifest = canonical_bytes_sync(package.manifest)
    payload = {
        "kind": "vigia.export.manifest",
        "export_id": package.manifest["export_id"],
        "organization_id": package.manifest["organization"]["organization_id"],
        "plant_id": package.manifest["plant"]["plant_id"],
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "generated_at": package.manifest["generated_at"],
        **changes,
    }
    private = Ed25519PrivateKey.from_private_bytes(SEEDS[ACTIVE_KEY])
    signature = base64.b64encode(private.sign(canonical_bytes_sync(payload))).decode("ascii")
    return canonical_bytes_sync({"payload": payload, "key_id": ACTIVE_KEY, "signature": signature})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("export_id", "018f0000-0000-7000-8000-0000000a0002"),
        ("organization_id", "018f0000-0000-7000-8000-00000000a002"),
        ("plant_id", "018f0000-0000-7000-8000-00000000b002"),
        ("generated_at", "2026-05-02T00:05:00.000Z"),
    ],
)
def test_a_valid_signature_for_another_export_is_a_mismatch(
    field_name: str, value: str, tmp_path: Path
) -> None:
    """Con el SHA del manifiesto correcto y la firma válida, otro campo del mensaje (otra
    exportación, organización, planta o fecha) no ancla este paquete."""
    package = control_package()
    package.frozen_signature = _signed_payload(package, **{field_name: value})
    report = verify_export(_write(package, tmp_path / "p"))
    assert report.failure is not None
    assert (report.failure.reason, report.failure.position.artifact) == (
        "manifest_signature_mismatch",
        "manifest.signature.json",
    )
    assert report.failure.detail.startswith(field_name)


def test_the_same_signature_in_canonical_form_anchors_the_package(tmp_path: Path) -> None:
    package = control_package()
    package.frozen_signature = _signed_payload(package)
    assert verify_export(_write(package, tmp_path / "p")).status == "intact"


@pytest.mark.parametrize(
    "transform",
    [lambda data: data + b" ", lambda data: b" " + data, lambda data: data.replace(b":", b": ")],
    ids=["trailing-space", "leading-space", "spaced"],
)
def test_a_non_canonical_signature_file_is_invalid(transform: Any, tmp_path: Path) -> None:
    package = control_package()
    package.frozen_signature = transform(_signed_payload(package))
    report = verify_export(_write(package, tmp_path / "p"))
    assert report.failure is not None
    assert report.failure.reason == "manifest_signature_invalid"


def test_the_report_names_the_key_that_anchored_the_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _write(control_package(), tmp_path / "p")
    out = tmp_path / "r.json"
    assert export_main([str(directory), "--out", str(out)]) == 0
    expected = {
        "key_id": ACTIVE_KEY,
        "public_key_sha256": hashlib.sha256(
            Ed25519PrivateKey.from_private_bytes(SEEDS[ACTIVE_KEY]).public_key().public_bytes_raw()
        ).hexdigest(),
    }
    assert json.loads(out.read_text("utf-8"))["signing_key"] == expected
    assert verify_export(directory).signing_key == expected
    unsigned = control_package()
    unsigned.unsign()
    assert verify_export(_write(unsigned, tmp_path / "u")).signing_key is None
    capsys.readouterr()


def test_a_key_added_to_the_included_set_is_named_so_a_third_party_can_reject_it(
    tmp_path: Path,
) -> None:
    """Límite del modelo de confianza decidido (claves incluidas): quien añade su clave al
    conjunto, conserva las auténticas, cierra un hallazgo y firma obtiene ``intact``. El informe
    dice qué clave firmó, para compararla con ``/.well-known/vigia-checkpoint-keys``."""
    attacker_seed, attacker_id = bytes(range(210, 242)), "checkpoint-2026-99"
    package = control_package()
    raw = Ed25519PrivateKey.from_private_bytes(attacker_seed).public_key().public_bytes_raw()
    keys = [
        *package.documents[KEYS]["keys"],
        {
            "key_id": attacker_id,
            "algorithm": "Ed25519",
            "public_key": base64.b64encode(raw).decode("ascii"),
            "status": "active",
            "valid_from": "2026-01-01T00:00:00.000Z",
            "valid_until": "2027-06-01T00:00:00.000Z",
        },
    ]
    package.set_document(KEYS, {"keys": keys})
    package.manifest["checkpoint_public_keys"] = keys
    close_open_finding(package)
    package.signer = (attacker_seed, attacker_id)
    directory = _write(package, tmp_path / "p")
    report = verify_export(directory)
    assert report.status == "intact"
    assert report.signing_key == {
        "key_id": attacker_id,
        "public_key_sha256": hashlib.sha256(raw).hexdigest(),
    }
    # Con el SHA-256 que registró la plataforma, la falsificación cae.
    original = hashlib.sha256(canonical_bytes_sync(control_package().manifest)).hexdigest()
    anchored = verify_export(directory, manifest_sha256=original)
    assert anchored.failure is not None
    assert anchored.failure.reason == "manifest_hash_mismatch"
