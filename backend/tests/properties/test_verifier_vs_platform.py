"""PR-NUC-19 y PR-NUC-20: el verificador de paquetes frente al recorrido de la plataforma.

- **PR-NUC-19** (oráculo): sobre paquetes generados a partir de cadenas generadas (expediente de
  planta, expediente de organización y auditoría, con puntos de control firmados por una o dos
  claves), el verificador de paquetes (``package_verifier`` sobre ``chain_walk``) y el recorrido
  de referencia de la plataforma (``tests/verifier_packages.reference_walk``, con
  ``ledger.canonical`` y ``cryptography``) coinciden: ambos ``intact`` sobre el paquete íntegro y
  ambos ``broken`` en la misma secuencia y con el mismo registro sobre cualquier mutación de un
  campo (bit alterado, valor cambiado, clave quitada o añadida), del orden de la cadena (registro
  quitado, repetido o intercambiado, cola cortada) o de una clave pública del manifiesto.
- **PR-NUC-20**: para dos puntos de control ``c1 < c2`` de una misma cadena generada,
  ``verify(paquete con c2, anterior = c1)`` pasa si y solo si el prefijo hasta ``c1`` no cambió,
  aunque quien reescribe recalcule todos los hashes y vuelva a firmar los puntos de control.
- **El archivo generado** ``tools/vigia_verify.py`` en ``python3.10 -I -S`` (sin paquetes
  instalados ni ``site``): 0 sobre un paquete íntegro (directorio y zip); 1 con un bit alterado,
  nombrando la secuencia y el registro en el resumen y en ``--out``.

Solo datos generados.
"""

from __future__ import annotations

import base64
import copy
import json
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tests.properties.envelope_strategies import contents, display_names
from tests.verifier_packages import (
    ChainBuilder,
    PackageChain,
    SigningKey,
    Verdict,
    reference_walk,
    write_package,
)
from vigia_platform.ledger.chain.package_verifier import PackageReport, verify_package
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError, canonicalize

BACKEND = Path(__file__).resolve().parents[2]
VERIFIER = BACKEND / "tools" / "vigia_verify.py"
FILE = "chains/chain.jsonl"

# --- Generadores ----------------------------------------------------------------------------------


@st.composite
def chains(draw: st.DrawFn, *, min_checkpoints: int = 0) -> tuple[ChainBuilder, list[SigningKey]]:
    """Una cadena generada con sus claves ``checkpoint`` (una o dos: rotación)."""
    kind, has_plant = draw(st.sampled_from([("ledger", True), ("ledger", False), ("audit", False)]))
    organization_id = draw(st.uuids(version=4))
    plant_id = draw(st.uuids(version=4)) if has_plant else None
    keys = [
        SigningKey.from_seed(f"checkpoint-{index}", draw(st.binary(min_size=1, max_size=8)))
        for index in range(draw(st.integers(1, 2)))
    ]
    builder = ChainBuilder(kind, organization_id, plant_id)
    steps = draw(st.lists(st.booleans(), min_size=min_checkpoints, max_size=9))
    steps += [True] * max(0, min_checkpoints - sum(steps))
    for is_checkpoint in steps:
        if is_checkpoint:
            builder.append_checkpoint(draw(st.sampled_from(keys)))
        elif kind == "audit" and draw(st.booleans()):
            builder.append(None, name=draw(display_names))
        else:
            builder.append(draw(contents), name=draw(display_names))
    return builder, keys


def _package(builder: ChainBuilder, keys: list[SigningKey] | None = None) -> PackageChain:
    return PackageChain.from_builder(builder, FILE)


def _chain_ids(builder: ChainBuilder) -> tuple[str, str | None]:
    plant = None if builder.kind == "audit" or builder.plant_id is None else str(builder.plant_id)
    return str(builder.organization_id), plant


def pure_verdict(
    builder: ChainBuilder,
    chain: PackageChain,
    keys: list[SigningKey],
    *,
    manifest_keys: list[dict[str, str]] | None = None,
    previous: list[Path] | None = None,
    as_zip: bool = False,
) -> tuple[Verdict, PackageReport]:
    """Veredicto del verificador de paquetes sobre el paquete escrito en disco."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ("package.zip" if as_zip else "package")
        if not as_zip:
            path.mkdir()
        manifest = None
        if manifest_keys is not None:
            manifest = {
                "format": "vigia-package",
                "format_version": 1,
                "organization_id": str(builder.organization_id),
                "chains": [chain.manifest()],
                "checkpoint_keys": manifest_keys,
            }
        write_package(
            path, builder.organization_id, [chain], keys, as_zip=as_zip, manifest=manifest
        )
        report = verify_package(path, previous or [])
    result = report.chains[0].result if report.chains else None
    if report.intact:
        return Verdict("intact", None, None), report
    assert result is not None and result.broken is not None, report
    return Verdict("broken", result.broken.sequence, result.broken.entry_id), report


def platform_verdict(
    builder: ChainBuilder,
    chain: PackageChain,
    public_keys: dict[str, bytes],
    anchors: dict[int, str] | None = None,
) -> Verdict:
    organization_id, plant_id = _chain_ids(builder)
    return reference_walk(
        builder.kind,
        organization_id,
        plant_id,
        chain.entries,
        public_keys,
        declared_head=(chain.last_sequence, chain.last_hash),
        first_sequence=chain.first_sequence,
        anchors=anchors,
    )


def _keys(keys: list[SigningKey]) -> dict[str, bytes]:
    return {key.key_id: key.public_bytes for key in keys}


# --- PR-NUC-19: íntegro ---------------------------------------------------------------------------


@given(chains(), st.booleans())
def test_intact_packages_agree(
    generated: tuple[ChainBuilder, list[SigningKey]], as_zip: bool
) -> None:
    builder, keys = generated
    chain = _package(builder, keys)
    pure, report = pure_verdict(builder, chain, keys, as_zip=as_zip)
    assert pure == Verdict("intact", None, None), report
    assert platform_verdict(builder, chain, _keys(keys)) == pure
    checkpoints = sum(
        1
        for row in builder.rows
        if row.get("record_type") == "checkpoint" or row.get("operation") == "checkpoint"
    )
    assert len(report.chains[0].result.checkpoints) == checkpoints


# --- PR-NUC-19: mutaciones ------------------------------------------------------------------------


def _paths(value: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Rutas a todos los nodos debajo de ``value`` (sin la raíz)."""
    found: list[tuple[Any, ...]] = []
    items: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = list(enumerate(value))
    for key, child in items:
        found.append((*prefix, key))
        found.extend(_paths(child, (*prefix, key)))
    return found


def _get(value: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        value = value[key]
    return value


def _flip_text(text: str, data: st.DataObject) -> str:
    index = data.draw(st.integers(0, len(text) - 1))
    bit = data.draw(st.integers(0, 15))
    code = ord(text[index]) ^ (1 << bit)
    return text[:index] + chr(code) + text[index + 1 :]


_replacements = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**60), 2**60),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
    st.just([]),
    st.just({}),
)


def mutate_entry(entry: dict[str, Any], data: st.DataObject) -> dict[str, Any]:
    """Una mutación de un campo de la entrada (a cualquier profundidad)."""
    mutated = copy.deepcopy(entry)
    path = data.draw(st.sampled_from(_paths(mutated)))
    parent = _get(mutated, path[:-1])
    key, old = path[-1], _get(mutated, path)
    operation = data.draw(st.sampled_from(["flip", "hex", "replace", "delete", "add"]))
    hex_positions = (
        [i for i, c in enumerate(old) if c in "0123456789abcdef"] if isinstance(old, str) else []
    )
    if operation == "hex" and hex_positions:
        # Cambio que conserva la forma (un hash o un UUID siguen siendo válidos): solo lo delata
        # el hash que lo cubre o el enlace que lo compara.
        position = data.draw(st.sampled_from(hex_positions))
        replacement = data.draw(
            st.sampled_from([c for c in "0123456789abcdef" if c != old[position]])
        )
        parent[key] = old[:position] + replacement + old[position + 1 :]
    elif operation in {"flip", "hex"} and isinstance(old, str) and old:
        parent[key] = _flip_text(old, data)
    elif operation in {"flip", "hex"} and isinstance(old, int) and not isinstance(old, bool):
        parent[key] = old ^ (1 << data.draw(st.integers(0, 40)))
    elif operation == "delete" and isinstance(parent, dict):
        del parent[key]
    elif operation == "add" and isinstance(old, dict):
        old[data.draw(st.text(max_size=8))] = data.draw(_replacements)
    elif operation == "add" and isinstance(parent, dict):
        parent[data.draw(st.text(min_size=1, max_size=8))] = data.draw(_replacements)
    else:
        parent[key] = data.draw(_replacements)
    return mutated


def _canonical_or_none(value: Any) -> bytes | None:
    try:
        return canonicalize(value)
    except CanonicalizationError:
        return None


@given(chains(), st.data())
def test_field_mutations_break_both_at_the_same_sequence(
    generated: tuple[ChainBuilder, list[SigningKey]], data: st.DataObject
) -> None:
    builder, keys = generated
    assume(builder.rows)
    chain = _package(builder, keys)
    index = data.draw(st.integers(0, len(chain.entries) - 1))
    original = chain.entries[index]
    mutated = mutate_entry(original, data)
    # Toda diferencia de valor (no de escritura: 1 frente a 1.0) está cubierta por algún hash.
    assume(_canonical_or_none(mutated) != _canonical_or_none(original))
    chain.entries[index] = mutated
    pure, report = pure_verdict(builder, chain, keys)
    platform = platform_verdict(builder, chain, _keys(keys))
    assert pure.status == "broken", report
    assert pure.sequence == index + 1
    assert pure == platform


ChainMutation = Callable[[list[Any], int], list[Any]]
CHAIN_MUTATIONS: dict[str, ChainMutation] = {
    "drop": lambda entries, i: entries[:i] + entries[i + 1 :],
    "duplicate": lambda entries, i: [*entries[: i + 1], entries[i], *entries[i + 1 :]],
    "swap": lambda entries, i: (
        [*entries[:i], entries[i + 1], entries[i], *entries[i + 2 :]]
        if i + 1 < len(entries)
        else [*entries[:i], entries[i], entries[i]]
    ),
    "truncate": lambda entries, i: entries[:i],
    "reverse_tail": lambda entries, i: entries[:i] + entries[i:][::-1],
}


@given(chains(), st.sampled_from(sorted(CHAIN_MUTATIONS)), st.data())
def test_order_mutations_break_both_at_the_same_sequence(
    generated: tuple[ChainBuilder, list[SigningKey]], name: str, data: st.DataObject
) -> None:
    builder, keys = generated
    assume(builder.rows)
    chain = _package(builder, keys)
    index = data.draw(st.integers(0, len(chain.entries) - 1))
    mutated = CHAIN_MUTATIONS[name](chain.entries, index)
    assume(mutated != chain.entries)
    chain.entries = mutated
    pure, report = pure_verdict(builder, chain, keys)
    assert pure.status == "broken", report
    assert pure == platform_verdict(builder, chain, _keys(keys))


@given(chains(min_checkpoints=1), st.integers(0, 255), st.sampled_from(["flip", "drop", "rename"]))
def test_manifest_key_mutations_break_both(
    generated: tuple[ChainBuilder, list[SigningKey]], bit: int, operation: str
) -> None:
    builder, keys = generated
    chain = _package(builder, keys)
    first_checkpoint = next(
        row
        for row in builder.rows
        if row.get("record_type") == "checkpoint" or row.get("operation") == "checkpoint"
    )
    data = first_checkpoint["content" if builder.kind == "ledger" else "filters"]
    public_keys = _keys(keys)
    target = next(key for key in keys if key.key_id == json.loads(data)["key_id"])
    raw = bytearray(target.public_bytes)
    if operation == "flip":
        raw[bit // 8 % 32] ^= 1 << bit % 8
        public_keys[target.key_id] = bytes(raw)
    elif operation == "drop":
        del public_keys[target.key_id]
    else:
        public_keys[target.key_id + "-x"] = public_keys.pop(target.key_id)
    manifest_keys = [
        {"key_id": key_id, "public_key": base64.b64encode(value).decode()}
        for key_id, value in public_keys.items()
    ]
    pure, report = pure_verdict(builder, chain, keys, manifest_keys=manifest_keys)
    platform = platform_verdict(builder, chain, public_keys)
    assert pure == platform
    # El primer punto de control firmado con esa clave rompe la cadena (una clave alterada
    # verifica por azar solo con probabilidad despreciable).
    assert pure.status == "broken", report
    assert pure.sequence == first_checkpoint["chain_sequence"]


# --- PR-NUC-20: prefijo con un punto de control anterior -----------------------------------------


def _rebuild(
    builder: ChainBuilder, keys: list[SigningKey], change_at: int | None, new_content: Any
) -> ChainBuilder:
    """Reescribe la cadena como lo haría quien controla la base y la clave: todo recalculado."""
    rebuilt = ChainBuilder(builder.kind, builder.organization_id, builder.plant_id, builder.start)
    by_id = {key.key_id: key for key in keys}
    for position, row in enumerate(builder.rows):
        data = row["content"] if builder.kind == "ledger" else row["filters"]
        is_checkpoint = (
            row.get("record_type") == "checkpoint" or row.get("operation") == "checkpoint"
        )
        if is_checkpoint:
            rebuilt.append_checkpoint(by_id[json.loads(data)["key_id"]])
            continue
        content = None if data is None else json.loads(data)
        if position == change_at:
            content = new_content
        rebuilt.append(content, name=row["actor_display_name_snapshot"])
    return rebuilt


@given(chains(min_checkpoints=2), st.data())
def test_previous_checkpoint_passes_iff_prefix_unchanged(
    generated: tuple[ChainBuilder, list[SigningKey]], data: st.DataObject
) -> None:
    builder, keys = generated
    checkpoints = [
        position
        for position, row in enumerate(builder.rows)
        if row.get("record_type") == "checkpoint" or row.get("operation") == "checkpoint"
    ]
    c1 = data.draw(st.sampled_from(checkpoints[:-1]))
    previous_entry = builder.entries()[c1]
    ordinary = [p for p in range(len(builder.rows)) if p not in checkpoints]
    change_at = data.draw(st.one_of(st.none(), st.sampled_from(ordinary))) if ordinary else None
    new_content = {"rewritten": data.draw(st.integers(0, 2**20))}
    current = _rebuild(builder, keys, change_at, new_content)
    chain = _package(current, keys)
    with tempfile.TemporaryDirectory() as directory:
        anchor = Path(directory) / "c1.json"
        anchor.write_text(json.dumps(previous_entry), encoding="utf-8")
        pure, report = pure_verdict(current, chain, keys, previous=[anchor])
    prefix_unchanged = change_at is None or change_at > c1
    # El paquete reescrito es internamente íntegro: solo el punto de control anterior lo delata.
    alone, _ = pure_verdict(current, chain, keys)
    assert alone.status == "intact"
    assert (pure.status == "intact") is prefix_unchanged, report
    if not prefix_unchanged:
        assert pure.sequence == c1 + 1
        assert report.previous[0].status == "not_matched"
    anchors = {
        c1 + 1: str(previous_entry["record_hash" if builder.kind == "ledger" else "entry_hash"])
    }
    assert platform_verdict(current, chain, _keys(keys), anchors) == pure


# --- El archivo generado en Python 3.10 sin dependencias -----------------------------------------


def _python310() -> str:
    found = shutil.which("python3.10")
    if found is None:
        pytest.skip("python3.10 no está instalado en este equipo")
    return found


def _run_verifier(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    """``python3.10 -I -S tools/vigia_verify.py ...``: sin site-packages ni variables de Python."""
    return subprocess.run(
        [_python310(), "-I", "-S", str(VERIFIER), *map(str, arguments)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _example_package(tmp_path: Path, *, as_zip: bool = False) -> tuple[Path, ChainBuilder]:
    organization_id = uuid.UUID("5d0f1c9e-7b1a-4f7e-9d7c-2b6f0a9e1c01")
    plant_id = uuid.UUID("0b8e7c6d-5a4f-4e3d-8c2b-1a0f9e8d7c6b")
    key = SigningKey.from_seed("checkpoint-2026-09", b"semilla sintetica")
    ledger = ChainBuilder("ledger", organization_id, plant_id)
    audit = ChainBuilder("audit", organization_id, None)
    for number in range(1, 13):
        ledger.append({"zone": f"Z-{number:02d}", "n": number, "nota": "Señal ámbar"})
        audit.append({"page": number} if number % 2 else None)
        if number % 5 == 0:
            ledger.append_checkpoint(key)
            audit.append_checkpoint(key)
    chains_ = [
        PackageChain.from_builder(ledger, "chains/ledger-plant.jsonl"),
        PackageChain.from_builder(audit, "chains/audit.jsonl"),
    ]
    path = tmp_path / ("package.zip" if as_zip else "package")
    if not as_zip:
        path.mkdir()
    write_package(path, organization_id, chains_, [key], as_zip=as_zip)
    return path, ledger


@pytest.mark.parametrize("as_zip", [False, True], ids=["directory", "zip"])
def test_generated_verifier_python310_intact(tmp_path: Path, as_zip: bool) -> None:
    package, _ = _example_package(tmp_path, as_zip=as_zip)
    out = tmp_path / "resultado.json"
    completed = _run_verifier(package, "--out", out)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Resultado: ÍNTEGRO" in completed.stdout
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "intact"
    assert [chain["status"] for chain in result["chains"]] == ["intact", "intact"]
    assert [chain["checkpoints_verified"] for chain in result["chains"]] == [2, 2]


def test_generated_verifier_python310_bit_flip_names_sequence_and_record(tmp_path: Path) -> None:
    package, ledger = _example_package(tmp_path)
    target = package / "chains" / "ledger-plant.jsonl"
    lines = target.read_bytes().split(b"\n")
    # Registro 7: un bit del primer dígito hexadecimal de su content_hash ('0'-'9' o 'a'-'f').
    line = bytearray(lines[6])
    position = line.index(b'"content_hash":"') + len(b'"content_hash":"')
    line[position] ^= 0x01
    lines[6] = bytes(line)
    target.write_bytes(b"\n".join(lines))
    record_id = str(ledger.rows[6]["record_id"])
    out = tmp_path / "resultado.json"
    completed = _run_verifier(package, "--out", out)
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "ROTA en la secuencia 7" in completed.stdout
    assert record_id in completed.stdout
    assert "Resultado: ROTO" in completed.stdout
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["status"] == "broken"
    assert result["first_broken"]["sequence"] == 7
    assert result["first_broken"]["entry_id"] == record_id
    assert result["first_broken"]["line"] == 7


def test_generated_verifier_python310_unreadable_line_still_names_the_record(
    tmp_path: Path,
) -> None:
    package, ledger = _example_package(tmp_path)
    target = package / "chains" / "ledger-plant.jsonl"
    lines = target.read_bytes().split(b"\n")
    lines[2] = lines[2].replace(b'"chain_sequence":3', b'"chain_sequence":3,,', 1)
    target.write_bytes(b"\n".join(lines))
    completed = _run_verifier(package)
    assert completed.returncode == 1
    assert "ROTA en la secuencia 3" in completed.stdout
    assert str(ledger.rows[2]["record_id"]) in completed.stdout


def test_generated_verifier_python310_previous_checkpoint(tmp_path: Path) -> None:
    package, ledger = _example_package(tmp_path)
    checkpoint = next(entry for entry in ledger.entries() if entry["record_type"] == "checkpoint")
    anchor = tmp_path / "anterior.json"
    anchor.write_text(json.dumps(checkpoint), encoding="utf-8")
    completed = _run_verifier(package, "--previous-checkpoint", anchor)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "coincide; el prefijo no cambió" in completed.stdout
    # El paquete anterior entero también sirve de ancla.
    completed = _run_verifier(package, "--previous-checkpoint", package)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    # Un punto de control anterior que no está en el paquete: no se da por bueno.
    moved = dict(checkpoint, chain_sequence=checkpoint["chain_sequence"] + 100)
    anchor.write_text(json.dumps(moved), encoding="utf-8")
    completed = _run_verifier(package, "--previous-checkpoint", anchor)
    assert completed.returncode == 1


@pytest.mark.parametrize(
    "arguments",
    [(), ("--no-such-option",), ("/no/existe/paquete",)],
    ids=["no-package", "unknown-option", "missing-path"],
)
def test_generated_verifier_python310_usage_errors(arguments: tuple[str, ...]) -> None:
    assert _run_verifier(*arguments).returncode == 2


def test_generated_verifier_python310_help_in_spanish() -> None:
    completed = _run_verifier("--help")
    assert completed.returncode == 0
    assert completed.stdout.startswith("uso: vigia_verify.py")
    assert "--previous-checkpoint" in completed.stdout
    assert "Código de salida" in completed.stdout
