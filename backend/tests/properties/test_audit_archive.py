"""PR-NUC-56: ida y vuelta del archivo de auditoría y bit alterado (TASK-131, LC-NUC-33).

PR-NUC-56: «para cualquier partición de auditoría generada, ``restore(export(p)) = p`` en recuento,
hashes, cadena y puntos de control; con un bit alterado en el archivo, la partición sigue adjunta y
no se escribe ``audit_partition_archived``» (PAT-NUC-MAN-03).

Aquí, sin base ni almacén, el formato y la verificación de ``shared.archive.audit_archive``:

- **Ida y vuelta**: particiones generadas (de una a tres organizaciones, cada una con un tramo de
  su cadena que empieza en la génesis o a mitad, con puntos de control firmados intercalados,
  ``filters`` con texto difícil, números y anidamiento, recurso y alcance opcionales) →
  ``build_archive`` → ``read_archive`` → ``verify_archive`` pasa, y ``restore_rows`` devuelve
  **exactamente** las columnas de cada fila (hashes incluidos), con los mismos puntos de control.
- **Bit alterado**: cualquier bit de cualquier byte del archivo hace fallar ``verify_download``
  (lo que la tarea hace antes de desprender). Y aunque quien altera recalcule el ZIP entero (el
  SHA-256 ya no sirve), un bit alterado en las cadenas se detecta o deja exactamente las mismas
  filas: nunca da por bueno un archivo con otras filas.
- **Archivos hostiles**: bytes arbitrarios, miembros de más o de menos, rutas que salen del
  directorio y miembros por encima del tope terminan en ``ArchiveVerificationFailed``, nunca en
  otra excepción.
- **Restauración de solo lectura** con las claves del propio archivo y el verificador incluido
  (``vigia_verify.py`` real) que da cada paquete por íntegro.

Solo datos generados; las claves se generan en cada corrida a partir de semillas.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import sys
import uuid
import warnings
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tests.properties.envelope_strategies import display_names, texts
from tests.verifier_packages import (
    ChainBuilder,
    SigningKey,
    canonical_timestamp,
    genesis,
    signed_message,
)
from vigia_platform.ledger.canonical import audit_envelope_canonical, canonical_bytes_sync
from vigia_platform.ledger.chain.checkpoints import CheckpointPublicKey
from vigia_platform.shared.archive import audit_archive
from vigia_platform.shared.archive.audit_archive import (
    ArchiveFailure,
    ArchiveVerificationFailed,
    AuditPartition,
    PartitionSnapshot,
    build_archive,
    column_values,
    extract_archive,
    read_archive,
    restore_audit_partition,
    restore_rows,
    verify_archive,
    verify_download,
)
from vigia_platform.shared.signing.keys import KeyStatus

BACKEND = Path(__file__).resolve().parents[2]
VERIFIER = (BACKEND / "tools" / "vigia_verify.py").read_bytes()
SIGNING_KEYS = (
    SigningKey.from_seed("checkpoint-2026-01", b"archive-key-1"),
    SigningKey.from_seed("checkpoint-2026-07", b"archive-key-2"),
)
PUBLIC_KEYS = tuple(
    CheckpointPublicKey(
        key_id=key.key_id,
        public_key=key.public_b64,
        status=KeyStatus.ACTIVE if index else KeyStatus.RETIRED,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2027, 1, 1, tzinfo=UTC),
    )
    for index, key in enumerate(SIGNING_KEYS)
)
EXPORTED_AT = datetime(2028, 11, 2, 3, 0, tzinfo=UTC)
ROLES = (None, "coordinator_sst", "plant_manager", "administrator", "platform_operator")
OPERATIONS = ("ledger_read", "audit_read", "login_succeeded", "export_requested")
ACTOR_KINDS = ("user", "provider_user", "system", "operator")

_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1),
    st.floats(allow_nan=False, allow_infinity=False),
    texts(max_size=24),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(texts(max_size=12), children, max_size=3),
    ),
    max_leaves=8,
)
_filters = st.one_of(st.none(), st.dictionaries(texts(max_size=16), _json_values, max_size=4))
_optional_uuid = st.one_of(st.none(), st.uuids())


@dataclass
class Generated:
    snapshot: PartitionSnapshot
    checkpoints: dict[uuid.UUID, int]


def _row(
    draw: st.DrawFn,
    organization_id: uuid.UUID,
    sequence: int,
    previous: str,
    moment: datetime,
    filters_document: Mapping[str, Any] | None,
    operation: str,
) -> dict[str, Any]:
    filters = None if filters_document is None else canonical_bytes_sync(dict(filters_document))
    resource = draw(
        st.one_of(st.none(), st.tuples(st.sampled_from(("ledger_record", "user")), st.uuids()))
    )
    kind = draw(st.sampled_from(ACTOR_KINDS))
    row: dict[str, Any] = {
        "entry_id": draw(st.uuids()),
        "organization_id": organization_id,
        "chain_sequence": sequence,
        "actor_kind": kind,
        "actor_id": draw(st.uuids()),
        "actor_display_name_snapshot": draw(display_names),
        "actor_role_in_use": draw(st.sampled_from(ROLES)),
        "actor_concession_id": draw(_optional_uuid) if kind == "provider_user" else None,
        "actor_unit": draw(st.sampled_from(("U-02", "U-03", "U-04"))),
        "operation": operation,
        "scope_plant_id": draw(_optional_uuid),
        "scope_zone_id": draw(_optional_uuid),
        "resource_kind": None if resource is None else resource[0],
        "resource_id": None if resource is None else resource[1],
        "filters": filters,
        "filters_hash": None if filters is None else hashlib.sha256(filters).hexdigest(),
        "result_count": draw(st.one_of(st.none(), st.integers(min_value=0, max_value=2**31 - 1))),
        "outcome": draw(st.sampled_from(("success", "denied", "error"))),
        "correlation_id": draw(st.uuids()),
        "occurred_at": moment,
        "previous_hash": previous,
    }
    row["entry_hash"] = hashlib.sha256(
        audit_envelope_canonical(row) + previous.encode("ascii")
    ).hexdigest()
    return row


@st.composite
def partitions(draw: st.DrawFn) -> Generated:
    month = date(draw(st.integers(min_value=2026, max_value=2030)), draw(st.integers(1, 12)), 1)
    partition = AuditPartition(month)
    start = datetime(month.year, month.month, 1, tzinfo=UTC)
    organizations = sorted(draw(st.lists(st.uuids(), min_size=1, max_size=3, unique=True)))
    rows: dict[uuid.UUID, list[dict[str, Any]]] = {}
    checkpoints: dict[uuid.UUID, int] = {}
    for organization_id in organizations:
        from_genesis = draw(st.booleans())
        sequence = 1 if from_genesis else draw(st.integers(min_value=2, max_value=2**40))
        previous = (
            genesis(organization_id, None)
            if from_genesis
            else draw(st.binary(min_size=32, max_size=32)).hex()
        )
        moment = start + timedelta(milliseconds=draw(st.integers(0, 10**6)))
        organization_rows: list[dict[str, Any]] = []
        count = 0
        for _ in range(draw(st.integers(min_value=1, max_value=6))):
            if draw(st.integers(0, 3)) == 0:
                key = draw(st.sampled_from(SIGNING_KEYS))
                taken_at = canonical_timestamp(moment)
                message = signed_message(
                    "audit", str(organization_id), None, sequence - 1, previous, taken_at
                )
                filters: Mapping[str, Any] | None = {
                    "covered_sequence": sequence - 1,
                    "covered_hash": previous,
                    "taken_at": taken_at,
                    "key_id": key.key_id,
                    "signature": key.sign(message),
                }
                operation = "checkpoint"
                count += 1
            else:
                filters = draw(_filters)
                operation = draw(st.sampled_from(OPERATIONS))
            row = _row(draw, organization_id, sequence, previous, moment, filters, operation)
            organization_rows.append(row)
            previous = row["entry_hash"]
            sequence += 1
            moment += timedelta(milliseconds=draw(st.integers(0, 5000)))
        rows[organization_id] = organization_rows
        checkpoints[organization_id] = count
    return Generated(PartitionSnapshot(partition, rows), checkpoints)


def fixed_partition() -> Generated:
    """Una partición fija para los ejemplos: una cadena desde la génesis y otra a mitad."""
    month = date(2026, 9, 1)
    start = datetime(2026, 9, 1, tzinfo=UTC)
    first = uuid.UUID("0a0a0a0a-0000-4000-8000-0000000000a1")
    second = uuid.UUID("0a0a0a0a-0000-4000-8000-0000000000a2")
    from_genesis = ChainBuilder("audit", first, None, start=start)
    from_genesis.append({"zona": "Prensas \u001f «2»"})
    from_genesis.append_checkpoint(SIGNING_KEYS[0])
    from_genesis.append(None)
    middle = ChainBuilder(
        "audit", second, None, start=start, first_sequence=41, start_hash="ab" * 32
    )
    middle.append_checkpoint(SIGNING_KEYS[1])
    middle.append({"n": 12345678901234, "x": [1.5, None, True]})
    rows = {first: from_genesis.rows, second: middle.rows}
    return Generated(PartitionSnapshot(AuditPartition(month), rows), {first: 1, second: 1})


def expected_rows(snapshot: PartitionSnapshot) -> list[dict[str, Any]]:
    return [
        column_values(row)
        for organization_id in sorted(snapshot.rows)
        for row in snapshot.rows[organization_id]
    ]


def archive_of(generated: Generated) -> bytes:
    return build_archive(generated.snapshot, PUBLIC_KEYS, VERIFIER, EXPORTED_AT)


def rebuild(data: bytes, change: Callable[[str, bytes], bytes]) -> bytes:
    """El mismo ZIP con cada miembro pasado por ``change`` (quien altera y recalcula)."""
    source = zipfile.ZipFile(io.BytesIO(data))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info.filename, change(info.filename, source.read(info)))
    return output.getvalue()


# --- Ida y vuelta --------------------------------------------------------------------------------


@given(partitions())
def test_pr_nuc_56_restore_of_export_is_the_partition(generated: Generated) -> None:
    snapshot = generated.snapshot
    data = archive_of(generated)
    contents = verify_download(
        data, hashlib.sha256(data).hexdigest(), snapshot, PUBLIC_KEYS, VERIFIER
    )
    # Recuento, hashes y cadena: cada fila vuelve con todas sus columnas, en orden.
    assert list(restore_rows(contents)) == expected_rows(snapshot)
    assert contents.manifest["entry_count"] == snapshot.entry_count
    for segment in contents.segments:
        rows = snapshot.rows[segment.organization_id]
        assert segment.entries == len(rows)
        assert segment.first_sequence == rows[0]["chain_sequence"]
        assert segment.last_hash == rows[-1]["entry_hash"]
        # Puntos de control: los mismos, y el último declarado es el último de la partición.
        assert segment.checkpoints == generated.checkpoints[segment.organization_id]
        checkpoint_rows = [row for row in rows if row["operation"] == "checkpoint"]
        if checkpoint_rows:
            assert segment.last_checkpoint is not None
            assert segment.last_checkpoint["entry_hash"] == checkpoint_rows[-1]["entry_hash"]
        else:
            assert segment.last_checkpoint is None


# --- Bit alterado --------------------------------------------------------------------------------


@given(partitions(), st.data())
def test_pr_nuc_56_any_flipped_bit_fails_the_verification(
    generated: Generated, data: st.DataObject
) -> None:
    archive = archive_of(generated)
    position = data.draw(st.integers(min_value=0, max_value=len(archive) - 1))
    bit = data.draw(st.integers(min_value=0, max_value=7))
    flipped = bytearray(archive)
    flipped[position] ^= 1 << bit
    with pytest.raises(ArchiveVerificationFailed) as raised:
        verify_download(
            bytes(flipped),
            hashlib.sha256(archive).hexdigest(),
            generated.snapshot,
            PUBLIC_KEYS,
            VERIFIER,
        )
    assert raised.value.reason is ArchiveFailure.DIGEST_MISMATCH


@given(partitions(), st.data())
def test_pr_nuc_56_a_flipped_bit_in_the_chains_never_passes_with_other_rows(
    generated: Generated, data: st.DataObject
) -> None:
    """Sin la defensa del SHA-256 (ZIP recalculado), un bit alterado en una cadena se detecta;
    solo pasa si las filas restauradas son exactamente las mismas (p. ej. ``\\u001f`` y
    ``\\u001F`` en un escape JSON)."""
    archive = archive_of(generated)
    organization_id = data.draw(st.sampled_from(sorted(generated.snapshot.rows)))
    member = f"organizations/{organization_id}/chains/audit.jsonl"
    original = zipfile.ZipFile(io.BytesIO(archive)).read(member)
    position = data.draw(st.integers(min_value=0, max_value=len(original) - 1))
    bit = data.draw(st.integers(min_value=0, max_value=7))

    def change(name: str, content: bytes) -> bytes:
        if name != member:
            return content
        altered = bytearray(content)
        altered[position] ^= 1 << bit
        return bytes(altered)

    tampered = rebuild(archive, change)
    try:
        contents = read_archive(tampered)
        verify_archive(contents, generated.snapshot, PUBLIC_KEYS, VERIFIER)
    except ArchiveVerificationFailed:
        return
    assert list(restore_rows(contents)) == expected_rows(generated.snapshot)


@given(partitions(), st.data())
def test_a_snapshot_that_differs_from_the_archive_fails(
    generated: Generated, data: st.DataObject
) -> None:
    """La verificación es contra la base: otra fila, una de menos o otro verificador no pasan."""
    archive = archive_of(generated)
    contents = read_archive(archive)
    organization_id = data.draw(st.sampled_from(sorted(generated.snapshot.rows)))
    rows = generated.snapshot.rows[organization_id]
    index = data.draw(st.integers(min_value=0, max_value=len(rows) - 1))
    changed = dict(rows[index])
    changed["result_count"] = (changed["result_count"] or 0) + 1
    other = {
        **generated.snapshot.rows,
        organization_id: [*rows[:index], changed, *rows[index + 1 :]],
    }
    with pytest.raises(ArchiveVerificationFailed) as raised:
        verify_archive(
            contents, PartitionSnapshot(generated.snapshot.partition, other), PUBLIC_KEYS, VERIFIER
        )
    assert raised.value.reason is ArchiveFailure.ENTRY_MISMATCH
    shorter = {**generated.snapshot.rows, organization_id: rows[:-1]}
    shorter = {key: value for key, value in shorter.items() if value}
    with pytest.raises(ArchiveVerificationFailed):
        verify_archive(
            contents,
            PartitionSnapshot(generated.snapshot.partition, shorter),
            PUBLIC_KEYS,
            VERIFIER,
        )
    with pytest.raises(ArchiveVerificationFailed) as raised:
        verify_archive(contents, generated.snapshot, PUBLIC_KEYS, VERIFIER + b"\n")
    assert raised.value.reason is ArchiveFailure.VERIFIER_MISMATCH
    with pytest.raises(ArchiveVerificationFailed) as raised:
        verify_archive(contents, generated.snapshot, PUBLIC_KEYS[1:], VERIFIER)
    assert raised.value.reason is ArchiveFailure.CHECKPOINT_MISMATCH


@given(partitions(), st.data())
def test_a_partition_already_broken_in_the_database_is_not_exported(
    generated: Generated, data: st.DataObject
) -> None:
    organization_id = data.draw(st.sampled_from(sorted(generated.snapshot.rows)))
    rows = generated.snapshot.rows[organization_id]
    index = data.draw(st.integers(min_value=0, max_value=len(rows) - 1))
    broken = dict(rows[index])
    column = data.draw(st.sampled_from(("entry_hash", "previous_hash", "filters_hash")))
    assume(broken[column] is not None)
    broken[column] = "0" * 64 if broken[column] != "0" * 64 else "1" * 64
    other = {
        **generated.snapshot.rows,
        organization_id: [*rows[:index], broken, *rows[index + 1 :]],
    }
    with pytest.raises(ArchiveVerificationFailed) as raised:
        build_archive(
            PartitionSnapshot(generated.snapshot.partition, other),
            PUBLIC_KEYS,
            VERIFIER,
            EXPORTED_AT,
        )
    assert raised.value.reason is ArchiveFailure.SOURCE_BROKEN


# --- Archivos hostiles ---------------------------------------------------------------------------


@given(st.binary(max_size=4096))
def test_arbitrary_bytes_only_raise_verification_failed(data: bytes) -> None:
    with pytest.raises(ArchiveVerificationFailed):
        read_archive(data)


@pytest.mark.parametrize(
    "change",
    ["extra", "missing", "escape", "duplicate", "not_json", "deep"],
)
def test_hostile_archives_are_rejected(change: str) -> None:
    generated = fixed_partition()
    archive = archive_of(generated)
    source = zipfile.ZipFile(io.BytesIO(archive))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info)
            if change == "missing" and info.filename.endswith("audit.jsonl"):
                continue
            if change == "not_json" and info.filename == "archive.json":
                content = b"{nope"
            if change == "deep" and info.filename == "archive.json":
                content = b"[" * 100_000 + b"]" * 100_000
            target.writestr(info.filename, content)
        if change == "extra":
            target.writestr("organizations/notes.txt", b"x")
        if change == "escape":
            target.writestr("../../etc/vigia.txt", b"x")
        if change == "duplicate":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)  # el nombre repetido es el ataque
                target.writestr("vigia_verify.py", b"print('otro')")
    with pytest.raises(ArchiveVerificationFailed) as raised:
        read_archive(output.getvalue())
    assert raised.value.reason in (ArchiveFailure.UNREADABLE, ArchiveFailure.FORMAT_MISMATCH)


def test_a_member_above_the_cap_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = archive_of(fixed_partition())
    monkeypatch.setattr(audit_archive, "MAX_MEMBER_BYTES", 16)
    with pytest.raises(ArchiveVerificationFailed) as raised:
        read_archive(archive)
    assert raised.value.reason is ArchiveFailure.UNREADABLE


# --- Restauración de solo lectura ----------------------------------------------------------------


class MemoryStorage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.writes = 0

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        return self.objects[key]


@given(partitions())
def test_read_only_restore_verifies_with_the_archive_keys(generated: Generated) -> None:
    archive = archive_of(generated)
    key = generated.snapshot.partition.object_key
    storage = MemoryStorage({key: archive})
    restored = asyncio.run(
        restore_audit_partition(storage, key, hashlib.sha256(archive).hexdigest())  # type: ignore[arg-type]
    )
    assert list(restored.rows) == expected_rows(generated.snapshot)
    with pytest.raises(ArchiveVerificationFailed) as raised:
        asyncio.run(restore_audit_partition(storage, key, "0" * 64))  # type: ignore[arg-type]
    assert raised.value.reason is ArchiveFailure.DIGEST_MISMATCH


def test_read_only_restore_rejects_a_consistently_rewritten_archive() -> None:
    """Un archivo reescrito con su SHA-256 recalculado (y registrado) no se restaura: la cadena
    no cuadra con sus propios hashes."""
    generated = fixed_partition()

    def change(name: str, content: bytes) -> bytes:
        if not name.endswith("audit.jsonl"):
            return content
        return content.replace(b'"result_count":3', b'"result_count":4', 1)

    tampered = rebuild(archive_of(generated), change)
    key = generated.snapshot.partition.object_key
    storage = MemoryStorage({key: tampered})
    with pytest.raises(ArchiveVerificationFailed) as raised:
        asyncio.run(
            restore_audit_partition(storage, key, hashlib.sha256(tampered).hexdigest())  # type: ignore[arg-type]
        )
    assert raised.value.reason is ArchiveFailure.CHAIN_BROKEN


def test_extracted_packages_pass_the_included_verifier(tmp_path: Path) -> None:
    """El ``vigia_verify.py`` incluido da por íntegro el paquete de cada organización."""
    generated = fixed_partition()
    archive = archive_of(generated)
    directory = tmp_path / "restaurado"
    written = extract_archive(archive, directory)
    assert all(path.is_relative_to(directory) for path in written)
    verifier = directory / "vigia_verify.py"
    assert verifier.read_bytes() == VERIFIER
    for organization_id in generated.snapshot.rows:
        package = directory / "organizations" / str(organization_id)
        result = subprocess.run(
            [sys.executable, str(verifier), str(package)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    with pytest.raises(FileExistsError):
        extract_archive(archive, directory)
