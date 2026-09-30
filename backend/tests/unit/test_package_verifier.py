"""Bordes del verificador de paquetes: manifiesto, archivos, líneas, anclas y salida (BR-NUC-57).

Complementa PR-NUC-19 y 20 (``tests/properties/test_verifier_vs_platform.py``) con los casos
límite de la lectura del paquete, que el oráculo no genera:

- manifiesto ausente, de otra versión, con claves de más en una cadena, con rutas que salen del
  paquete (``..``, absolutas, barra inversa, enlaces simbólicos), cadenas, archivos o claves
  repetidos, lista de cadenas vacía, auditoría con planta; claves de primer nivel desconocidas
  (se toleran: U-04 añade metadatos);
- líneas en blanco y finales CRLF, líneas ilegibles (``NaN``, clave repetida, UTF-8 inválido,
  demasiado largas), enteros mayores que ``2**53`` escritos por RFC 8785;
- cadena vacía, cabeza declarada que no coincide, paquete que no empieza en la génesis;
- puntos de control anteriores: firma alterada, entrada que no es un punto de control, cadena
  ausente, dos anclas distintas en la misma secuencia, secuencia fuera del paquete;
- archivos que el manifiesto no declara (se avisan) y códigos de salida de ``main``.

Solo datos generados.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tests.verifier_packages import (
    ChainBuilder,
    PackageChain,
    SigningKey,
    jsonl,
    manifest_document,
    write_package,
)
from vigia_platform.ledger.canonical import envelope_canonical
from vigia_platform.ledger.chain import package_verifier
from vigia_platform.ledger.chain.chain_walk import Break, ChainRef, ChainWalker, genesis_hash
from vigia_platform.ledger.chain.package_verifier import (
    EXIT_BROKEN,
    EXIT_INTACT,
    EXIT_USAGE,
    main,
    render_summary,
    report_document,
    verify_package,
)

ORGANIZATION = uuid.UUID("1a1b1c1d-2e2f-4a3b-8c4d-5e5f5a5b5c5d")
PLANT = uuid.UUID("6f6e6d6c-7b7a-4988-9c9d-0e0f0a0b0c0d")
KEY = SigningKey.from_seed("checkpoint-a", b"a")
OTHER_KEY = SigningKey.from_seed("checkpoint-b", b"b")


def _noncanonical(text: str) -> str:
    """El mismo valor en base64 con bits de relleno distintos de cero en el último carácter."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    body = text.rstrip("=")
    padding = len(text) - len(body)
    last = alphabet.index(body[-1]) | ((1 << (2 * padding)) - 1)
    changed = body[:-1] + alphabet[last] + "=" * padding
    assert changed != text
    assert base64.b64decode(changed, validate=True) == base64.b64decode(text)
    return changed


def _ledger(records: int = 6, checkpoint_every: int = 3) -> ChainBuilder:
    builder = ChainBuilder("ledger", ORGANIZATION, PLANT)
    for number in range(1, records + 1):
        builder.append({"zone": f"Z-{number}", "n": number})
        if checkpoint_every and number % checkpoint_every == 0:
            builder.append_checkpoint(KEY)
    return builder


def _audit(entries: int = 4) -> ChainBuilder:
    builder = ChainBuilder("audit", ORGANIZATION, None)
    for number in range(1, entries + 1):
        builder.append({"page": number} if number % 2 else None)
    builder.append_checkpoint(KEY)
    return builder


def _package(
    tmp_path: Path, *builders: ChainBuilder, name: str = "package"
) -> tuple[Path, list[PackageChain]]:
    chains = [
        PackageChain.from_builder(builder, f"chains/{builder.kind}-{index}.jsonl")
        for index, builder in enumerate(builders or (_ledger(), _audit()))
    ]
    path = tmp_path / name
    path.mkdir()
    write_package(path, ORGANIZATION, chains, [KEY])
    return path, chains


def _manifest(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    return document


def _write_manifest(path: Path, document: dict[str, Any]) -> None:
    (path / "manifest.json").write_text(json.dumps(document), encoding="utf-8")


def _error(path: Path) -> str | None:
    report = verify_package(path)
    assert not report.intact
    return None if report.package_error is None else report.package_error.reason


# --- Manifiesto y archivos ------------------------------------------------------------------------


def test_intact_package_and_unknown_top_level_keys(tmp_path: Path) -> None:
    path, _ = _package(tmp_path)
    document = _manifest(path)
    document["exported_at"] = "2026-09-29T00:00:00.000Z"
    document["verifier_sha256"] = "0" * 64
    _write_manifest(path, document)
    report = verify_package(path)
    assert report.intact, render_summary(report)
    assert report_document(report)["status"] == "intact"


def test_missing_manifest(tmp_path: Path) -> None:
    path, _ = _package(tmp_path)
    (path / "manifest.json").unlink()
    assert _error(path) == "package_unreadable"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda d: d.update(format="otro"), "unsupported_format"),
        (lambda d: d.update(format_version=2), "unsupported_format"),
        (lambda d: d.update(format_version=True), "unsupported_format"),
        (lambda d: d.update(format_version="1"), "unsupported_format"),
        (lambda d: d.update(organization_id="ORG"), "manifest_invalid"),
        (lambda d: d.update(organization_id=str(ORGANIZATION).upper()), "manifest_invalid"),
        (lambda d: d.update(chains=[]), "manifest_invalid"),
        (lambda d: d.update(chains={}), "manifest_invalid"),
        (lambda d: d["chains"][0].update(extra=1), "manifest_invalid"),
        (lambda d: d["chains"][0].pop("last_hash"), "manifest_invalid"),
        (lambda d: d["chains"][0].update(kind="other"), "manifest_invalid"),
        (lambda d: d["chains"][1].update(plant_id=str(PLANT)), "manifest_invalid"),
        (lambda d: d["chains"][0].update(first_sequence=0), "manifest_invalid"),
        (lambda d: d["chains"][0].update(first_sequence=True), "manifest_invalid"),
        (lambda d: d["chains"][0].update(last_sequence=-1), "manifest_invalid"),
        (lambda d: d["chains"][0].update(last_hash="A" * 64), "manifest_invalid"),
        (lambda d: d["chains"][0].update(last_hash="a" * 64 + "\n"), "manifest_invalid"),
        (
            lambda d: d["chains"].append(dict(d["chains"][0], file="chains/x.jsonl")),
            "manifest_invalid",
        ),
        (lambda d: d["chains"][1].update(file=d["chains"][0]["file"]), "manifest_invalid"),
        (lambda d: d["chains"][0].update(file="../fuera.jsonl"), "manifest_invalid"),
        (lambda d: d["chains"][0].update(file="/etc/passwd"), "manifest_invalid"),
        (lambda d: d["chains"][0].update(file="chains\\ledger.jsonl"), "manifest_invalid"),
        (lambda d: d["chains"][0].update(file="chains/.oculto"), "manifest_invalid"),
        (lambda d: d["chains"][0].update(file="manifest.json"), "manifest_invalid"),
        (lambda d: d["chains"][0].update(file="chains/ledger.jsonl\n"), "manifest_invalid"),
        (lambda d: d["checkpoint_keys"].append(dict(d["checkpoint_keys"][0])), "manifest_invalid"),
        (lambda d: d["checkpoint_keys"][0].update(public_key="x"), "manifest_invalid"),
        (
            lambda d: d["checkpoint_keys"][0].update(
                public_key=_noncanonical(d["checkpoint_keys"][0]["public_key"])
            ),
            "manifest_invalid",
        ),
        (lambda d: d["checkpoint_keys"][0].update(key_id=""), "manifest_invalid"),
        (lambda d: d["checkpoint_keys"][0].update(extra=1), "manifest_invalid"),
        (lambda d: d.pop("checkpoint_keys"), "manifest_invalid"),
        (lambda d: d["chains"][0].update(file="chains/no-existe.jsonl"), "package_unreadable"),
    ],
)
def test_invalid_manifests(tmp_path: Path, change: Any, reason: str) -> None:
    path, _ = _package(tmp_path)
    document = _manifest(path)
    change(document)
    _write_manifest(path, document)
    assert _error(path) == reason


@pytest.mark.parametrize("data", [b"", b"[]", b"NaN", b'{"format":1,"format":2}', b"\xff\xfe"])
def test_unreadable_manifest(tmp_path: Path, data: bytes) -> None:
    path, _ = _package(tmp_path)
    (path / "manifest.json").write_bytes(data)
    assert _error(path) in {"manifest_invalid", "unsupported_format"}


@pytest.mark.skipif(os.name == "nt", reason="enlaces simbólicos")
def test_symlink_outside_the_package_is_not_followed(tmp_path: Path) -> None:
    path, chains = _package(tmp_path)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes((path / chains[0].file).read_bytes())
    (path / chains[0].file).unlink()
    (path / chains[0].file).symlink_to(outside)
    assert _error(path) == "package_unreadable"


def test_zip_package_and_missing_member(tmp_path: Path) -> None:
    ledger = _ledger()
    chain = PackageChain.from_builder(ledger, "chains/ledger.jsonl")
    archive = write_package(tmp_path / "p.zip", ORGANIZATION, [chain], [KEY], as_zip=True)
    assert verify_package(archive).intact
    broken = tmp_path / "q.zip"
    with zipfile.ZipFile(broken, "w") as target:
        target.writestr(
            "manifest.json", json.dumps(manifest_document(ORGANIZATION, [chain], [KEY]))
        )
    assert _error(broken) == "package_unreadable"


def test_regular_file_is_not_a_package(tmp_path: Path) -> None:
    path = tmp_path / "package.txt"
    path.write_text("hola", encoding="utf-8")
    assert _error(path) == "package_unreadable"


def test_unlisted_files_are_reported(tmp_path: Path) -> None:
    path, _ = _package(tmp_path)
    (path / "chains" / "oculta.jsonl").write_text("{}\n", encoding="utf-8")
    report = verify_package(path)
    assert report.intact
    assert report.unlisted_files == ["chains/oculta.jsonl"]
    assert "chains/oculta.jsonl" in render_summary(report)


# --- Líneas ---------------------------------------------------------------------------------------


def _rewrite_line(path: Path, file: str, index: int, data: bytes) -> None:
    lines = (path / file).read_bytes().split(b"\n")
    lines[index] = data
    (path / file).write_bytes(b"\n".join(lines))


def test_blank_lines_and_crlf_are_accepted(tmp_path: Path) -> None:
    path, chains = _package(tmp_path)
    target = path / chains[0].file
    target.write_bytes(b"\r\n\r\n" + target.read_bytes().replace(b"\n", b"\r\n\r\n"))
    assert verify_package(path).intact


@pytest.mark.parametrize(
    "mangle",
    [
        lambda line: line.replace(b'"n":2', b'"n":NaN'),
        lambda line: line.replace(b'"n":2', b'"n":2,"n":2'),
        lambda line: line.replace(b'"Z-2"', b'"Z-\xff"'),
        lambda line: line.replace(b'"n":2', b'"n":' + b"1" * 5000),
        lambda line: line.replace(b'"n":2', b'"n":' + b"[" * 50_000),
        lambda line: line[:-5],
    ],
    ids=["nan", "duplicate-key", "utf8", "digits", "deep", "cut"],
)
def test_unreadable_line_breaks_at_its_sequence_and_names_the_record(
    tmp_path: Path, mangle: Any
) -> None:
    ledger = _ledger()
    path, chains = _package(tmp_path, ledger)
    line = (path / chains[0].file).read_bytes().split(b"\n")[1]
    _rewrite_line(path, chains[0].file, 1, mangle(line))
    report = verify_package(path)
    failure = report.chains[0].result.broken
    assert failure is not None
    assert (failure.sequence, failure.reason) == (2, "malformed")
    assert failure.entry_id == str(ledger.rows[1]["record_id"])
    assert report.chains[0].broken_line == 2


def test_line_longer_than_the_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = _package(tmp_path)
    monkeypatch.setattr(package_verifier, "MAX_LINE_BYTES", 256)
    failure = verify_package(path).chains[0].result.broken
    assert failure is not None and failure.reason == "malformed" and failure.sequence == 1


def test_large_integers_written_by_rfc8785_are_read_as_doubles(tmp_path: Path) -> None:
    builder = ChainBuilder("ledger", ORGANIZATION, PLANT)
    builder.append({"big": 1.2345678901234567e19, "tiny": 5e-324, "neg": -1e21})
    path, chains = _package(tmp_path, builder)
    target = path / chains[0].file
    # Como lo escribiría una exportación que copia los bytes canónicos de la columna content.
    data = target.read_bytes().replace(b"1.2345678901234567e+19", b"12345678901234567000")
    assert b'"big":12345678901234567000' in data
    target.write_bytes(data)
    assert verify_package(path).intact


# --- Cabeza, génesis y cadenas vacías -----------------------------------------------------------


def test_empty_chain_is_intact_only_if_the_head_is_empty(tmp_path: Path) -> None:
    empty = ChainBuilder("ledger", ORGANIZATION, PLANT)
    path, _ = _package(tmp_path, empty)
    report = verify_package(path)
    assert report.intact
    assert "sin registros" in render_summary(report)
    document = _manifest(path)
    document["chains"][0]["last_sequence"] = 1
    _write_manifest(path, document)
    failure = verify_package(path).chains[0].result.broken
    assert failure is not None and (failure.sequence, failure.reason) == (1, "head_mismatch")


@pytest.mark.parametrize(
    ("field", "value", "sequence"),
    [("last_sequence", 12, 9), ("last_sequence", 7, 8), ("last_hash", "0" * 64, 8)],
)
def test_declared_head_must_match(tmp_path: Path, field: str, value: object, sequence: int) -> None:
    path, _ = _package(tmp_path, _ledger())
    document = _manifest(path)
    document["chains"][0][field] = value
    _write_manifest(path, document)
    failure = verify_package(path).chains[0].result.broken
    assert failure is not None
    assert (failure.sequence, failure.reason) == (sequence, "head_mismatch")


def test_package_that_does_not_start_at_genesis(tmp_path: Path) -> None:
    full = _ledger(records=6)
    _package(tmp_path, full)
    entries = full.entries()[4:]
    tail = PackageChain(
        "ledger", str(PLANT), "chains/tail.jsonl", entries, 5, full.last_sequence, full.last_hash
    )
    partial = tmp_path / "partial"
    partial.mkdir()
    write_package(partial, ORGANIZATION, [tail], [KEY])
    report = verify_package(partial)
    assert report.intact, render_summary(report)
    assert report_document(report)["chains"][0]["from_genesis"] is False  # type: ignore[index]
    assert "no empieza" not in render_summary(report)
    assert "empieza en la secuencia 5" in render_summary(report)
    # Con el punto de control de la secuencia 4 como ancla, el enlace con el prefijo se comprueba.
    anchor = tmp_path / "c.json"
    anchor.write_text(json.dumps(full.entries()[3]), encoding="utf-8")
    assert verify_package(partial, [anchor]).intact
    entries[0]["previous_hash"] = "f" * 64
    write_package(partial, ORGANIZATION, [tail], [KEY])
    anchored = verify_package(partial, [anchor])
    assert not anchored.intact
    failure = anchored.chains[0].result.broken
    assert failure is not None and (failure.sequence, failure.reason) == (
        5,
        "previous_checkpoint_mismatch",
    )


def test_entry_of_another_organization(tmp_path: Path) -> None:
    other = ChainBuilder("ledger", uuid.UUID(int=7), PLANT)
    other.append({"zone": "Z-1"})
    ledger = _ledger(records=2, checkpoint_every=0)
    ledger.rows[1] = other.rows[0] | {"chain_sequence": 2}
    path, _ = _package(tmp_path, ledger)
    failure = verify_package(path).chains[0].result.broken
    assert failure is not None and (failure.sequence, failure.reason) == (2, "wrong_chain")


# --- Puntos de control anteriores ---------------------------------------------------------------


def _checkpoint_entry(builder: ChainBuilder) -> dict[str, Any]:
    return next(e for e in builder.entries() if e.get("record_type") == "checkpoint")


def test_previous_checkpoint_with_altered_signature_is_invalid(tmp_path: Path) -> None:
    ledger = _ledger()
    path, _ = _package(tmp_path, ledger)
    entry = _checkpoint_entry(ledger)
    signature = entry["content"]["signature"]
    entry["content"]["signature"] = ("B" if signature[0] == "A" else "A") + signature[1:]
    anchor = tmp_path / "c.json"
    anchor.write_text(json.dumps(entry), encoding="utf-8")
    report = verify_package(path, [anchor])
    assert not report.intact
    assert report.previous[0].status == "invalid"
    assert "NO COINCIDE" in render_summary(report)


@pytest.mark.parametrize(
    "document",
    [
        {"record_type": "zone_created"},
        [],
        "texto",
        {"record_type": "checkpoint"},
        {
            "record_type": "checkpoint",
            "organization_id": str(ORGANIZATION),
            "chain_sequence": 0,
            "previous_hash": "0" * 64,
        },
    ],
)
def test_previous_checkpoint_that_is_not_one(tmp_path: Path, document: object) -> None:
    path, _ = _package(tmp_path)
    anchor = tmp_path / "c.json"
    anchor.write_text(json.dumps(document), encoding="utf-8")
    report = verify_package(path, [anchor])
    assert not report.intact
    assert [previous.status for previous in report.previous] == ["invalid"]


@pytest.mark.parametrize("content", [b"no es json", b""])
def test_previous_checkpoint_file_unreadable(tmp_path: Path, content: bytes) -> None:
    path, _ = _package(tmp_path)
    anchor = tmp_path / "c.json"
    anchor.write_bytes(content)
    report = verify_package(path, [anchor])
    assert report.package_error is None
    assert not report.intact
    assert report.previous[0].status == "invalid"


def test_previous_package_without_checkpoints(tmp_path: Path) -> None:
    earlier, _ = _package(tmp_path, _ledger(records=2, checkpoint_every=0), name="earlier")
    current, _ = _package(tmp_path, _ledger(records=2, checkpoint_every=0), name="current")
    report = verify_package(current, [earlier])
    assert not report.intact
    assert report.previous[0].detail == "no contiene ningún punto de control"


def test_previous_checkpoint_of_a_missing_chain(tmp_path: Path) -> None:
    audit = _audit()
    path, _ = _package(tmp_path, _ledger())
    anchor = tmp_path / "c.json"
    anchor.write_text(
        json.dumps(next(e for e in audit.entries() if e["operation"] == "checkpoint")),
        encoding="utf-8",
    )
    report = verify_package(path, [anchor])
    assert not report.intact
    assert report.previous[0].status == "chain_missing"


def test_previous_checkpoint_beyond_the_package(tmp_path: Path) -> None:
    longer = _ledger(records=9)
    shorter = _ledger(records=3)
    path, _ = _package(tmp_path, shorter)
    anchor = tmp_path / "c.json"
    anchor.write_text(
        json.dumps([e for e in longer.entries() if e["record_type"] == "checkpoint"][-1]),
        encoding="utf-8",
    )
    report = verify_package(path, [anchor])
    failure = report.chains[0].result.broken
    assert failure is not None and failure.reason == "previous_checkpoint_missing"


def test_two_conflicting_previous_checkpoints(tmp_path: Path) -> None:
    ledger = _ledger()
    path, _ = _package(tmp_path, ledger)
    rewritten = ChainBuilder("ledger", ORGANIZATION, PLANT)
    for number in range(1, 4):
        rewritten.append({"zone": f"Z-{number}", "n": -number})
    rewritten.append_checkpoint(KEY)
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps(_checkpoint_entry(ledger)), encoding="utf-8")
    second.write_text(json.dumps(_checkpoint_entry(rewritten)), encoding="utf-8")
    report = verify_package(path, [first, second])
    assert not report.intact
    assert [previous.status for previous in report.previous] == ["matched", "invalid"]


def test_previous_package_as_anchor(tmp_path: Path) -> None:
    earlier_ledger = _ledger(records=3)
    earlier, _ = _package(tmp_path, earlier_ledger, _audit(), name="earlier")
    # El paquete actual continúa las mismas cadenas.
    current_ledger = _ledger(records=6)
    current, _ = _package(tmp_path, current_ledger, _audit(), name="current")
    report = verify_package(current, [earlier])
    assert report.intact, render_summary(report)
    assert sorted(p.status for p in report.previous) == ["matched", "matched"]


# --- chain_walk directo ---------------------------------------------------------------------------


def test_walker_rejects_unknown_chain_kind() -> None:
    with pytest.raises(ValueError):
        ChainWalker(ChainRef("otra", str(ORGANIZATION), None), {})


def test_walker_stays_broken() -> None:
    walker = ChainWalker(ChainRef("ledger", str(ORGANIZATION), str(PLANT)), {})
    first = walker.feed({"not": "an entry"})
    assert isinstance(first, Break) and first.sequence == 1
    assert walker.feed(_ledger().entries()[0]) is first
    assert walker.finish().status == "broken"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda c: c.update(covered_sequence=True), "checkpoint_malformed"),
        (lambda c: c.update(covered_sequence=c["covered_sequence"] - 1), "checkpoint_coverage"),
        (lambda c: c.update(covered_hash="0" * 64), "checkpoint_coverage"),
        (lambda c: c.update(taken_at="2026-09-29T00:00:00Z"), "checkpoint_malformed"),
        (
            lambda c: c.update(
                signature=("B" if c["signature"][0] == "A" else "A") + c["signature"][1:]
            ),
            None,
        ),
        (lambda c: c.update(signature=c["signature"][:-3] + "A=="), None),
        (lambda c: c.update(signature=_noncanonical(c["signature"])), "checkpoint_malformed"),
        (lambda c: c.update(signature="*" * 86 + "=="), "checkpoint_malformed"),
        (lambda c: c.update(signature=c["signature"][4:]), "checkpoint_malformed"),
        (lambda c: c.update(signature=c["signature"][:-2]), "checkpoint_malformed"),
        (lambda c: c.update(signature="AAAA" + c["signature"]), "checkpoint_malformed"),
        (lambda c: c.update(key_id="checkpoint-z"), "unknown_key"),
        (lambda c: c.update(extra=1), "checkpoint_malformed"),
        (lambda c: c.pop("key_id"), "checkpoint_malformed"),
    ],
)
def test_checkpoint_content_rules(change: Any, reason: str | None) -> None:
    """El punto de control se recalcula (hashes coherentes) salvo el campo alterado."""
    builder = ChainBuilder("ledger", ORGANIZATION, PLANT)
    builder.append({"zone": "Z-1"})
    row = builder.append_checkpoint(KEY)
    content = json.loads(row["content"])
    builder.rows.pop()
    change(content)
    builder.append(content, record_type="checkpoint", checkpoint=True)
    walker = ChainWalker(
        ChainRef("ledger", str(ORGANIZATION), str(PLANT)), {KEY.key_id: KEY.public_bytes}
    )
    failures = [walker.feed(entry) for entry in builder.entries()]
    assert failures[0] is None
    failure = failures[1]
    assert failure is not None and failure.sequence == 2
    assert failure.reason == (reason or "bad_signature")


def _rechain(builder: ChainBuilder) -> None:
    """Recalcula ``previous_hash`` y ``record_hash`` de toda la cadena, como quien la reescribe."""
    previous = genesis_hash(str(ORGANIZATION), str(PLANT))
    for row in builder.rows:
        row["previous_hash"] = previous
        row["record_hash"] = hashlib.sha256(
            envelope_canonical(row) + previous.encode("ascii")
        ).hexdigest()
        previous = row["record_hash"]


def _walk(builder: ChainBuilder) -> Break | None:
    walker = ChainWalker(
        ChainRef("ledger", str(ORGANIZATION), str(PLANT)), {KEY.key_id: KEY.public_bytes}
    )
    for entry in builder.entries():
        failure = walker.feed(entry)
        if failure is not None:
            return failure
    return None


def test_rechained_sequence_skip_is_a_gap() -> None:
    """Una secuencia saltada con todos los hashes recalculados solo la delata la secuencia."""
    builder = _ledger(records=4, checkpoint_every=0)
    for row in builder.rows[2:]:
        row["chain_sequence"] += 1
    _rechain(builder)
    failure = _walk(builder)
    assert failure is not None
    assert (failure.sequence, failure.reason) == (3, "sequence_gap")
    assert failure.entry_id == str(builder.rows[2]["record_id"])


def test_previous_hash_changed_to_another_valid_hash() -> None:
    builder = _ledger(records=3, checkpoint_every=0)
    original = builder.rows[1]["previous_hash"]
    builder.rows[1]["previous_hash"] = ("1" if original[0] == "0" else "0") + original[1:]
    failure = _walk(builder)
    assert failure is not None
    assert (failure.sequence, failure.reason) == (2, "previous_hash_mismatch")


def test_genesis_hash_matches_the_trigger_formula() -> None:
    assert (
        genesis_hash(str(ORGANIZATION), None) == ChainBuilder("audit", ORGANIZATION, None).last_hash
    )
    assert (
        genesis_hash(str(ORGANIZATION), str(PLANT))
        == ChainBuilder("ledger", ORGANIZATION, PLANT).last_hash
    )


# --- main -----------------------------------------------------------------------------------------


def test_main_exit_codes_and_result_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path, chains = _package(tmp_path)
    out = tmp_path / "resultado.json"
    assert main([str(path), "--out", str(out)]) == EXIT_INTACT
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "intact"
    assert "ÍNTEGRO" in capsys.readouterr().out
    _rewrite_line(path, chains[0].file, 0, (path / chains[0].file).read_bytes().split(b"\n")[1])
    assert main([str(path), "--out", str(out)]) == EXIT_BROKEN
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "broken" and result["first_broken"]["sequence"] == 1
    assert main([str(tmp_path / "no-existe")]) == EXIT_USAGE
    assert main([str(path), "--out", str(tmp_path / "no-existe" / "r.json")]) == EXIT_USAGE
    with pytest.raises(SystemExit) as raised:
        main(["--opcion-desconocida"])
    assert raised.value.code == EXIT_USAGE


def test_jsonl_helper_round_trip() -> None:
    entries = _ledger(records=2).entries()
    assert [json.loads(line) for line in jsonl(entries).splitlines()] == entries
