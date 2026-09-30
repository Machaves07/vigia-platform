"""Paquetes de prueba del verificador y recorrido de referencia de la plataforma (TASK-116).

Lo comparten ``tests/properties/test_verifier_vs_platform.py`` (PR-NUC-19, PR-NUC-20),
``tests/unit/test_package_verifier.py``, ``tests/benchmarks/test_verifier_100k.py`` y
``tests/integration/test_verifier_db_package.py``.

- **Construcción con el código de la plataforma**, independiente de los módulos puros: los
  hashes salen de ``ledger.canonical`` (``envelope_canonical``, ``audit_envelope_canonical`` y
  ``canonical_bytes_sync``, que usan ``rfc8785`` a través de ``vigia_contracts``) y las firmas de
  los puntos de control, de ``cryptography``. Las filas tienen la forma de las columnas de la base
  (UUID como ``uuid.UUID``, marcas con zona) y ``row_to_entry`` las pasa a la forma del paquete
  (``docs/package-format.md``).
- **Recorrido de referencia** (``reference_walk``): el pseudocódigo de business-logic-model §5
  escrito con ese mismo código de la plataforma. Lee cada entrada de vuelta a columnas de forma
  estricta (el texto de un UUID o de una marca debe ser exactamente el de la base) y devuelve
  ``(estado, secuencia, identificador)``. Es el oráculo de PR-NUC-19 frente a ``chain_walk``.

Solo datos generados; las claves de firma se generan en cada corrida a partir de semillas.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from vigia_contracts.canonical import canonicalize

from vigia_platform.ledger.canonical import (
    CanonicalFormError,
    audit_envelope_canonical,
    canonical_bytes_sync,
    envelope_canonical,
)

FORMAT = "vigia-package"
FORMAT_VERSION = 1
CHECKPOINT = "checkpoint"

RECORD_KEYS = frozenset(
    {
        "record_id",
        "organization_id",
        "plant_id",
        "chain_sequence",
        "record_type",
        "schema_version",
        "actor",
        "scope",
        "correlation_id",
        "received_at",
        "content",
        "content_hash",
        "previous_hash",
        "record_hash",
    }
)
AUDIT_KEYS = frozenset(
    {
        "entry_id",
        "organization_id",
        "chain_sequence",
        "actor",
        "operation",
        "scope",
        "resource_ref",
        "filters",
        "filters_hash",
        "result_count",
        "outcome",
        "correlation_id",
        "occurred_at",
        "previous_hash",
        "entry_hash",
    }
)
ACTOR_KEYS = frozenset(
    {"kind", "id", "display_name_snapshot", "role_in_use", "concession_id", "unit"}
)


# --- Claves -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    """Clave ``checkpoint`` de prueba, generada desde una semilla."""

    key_id: str
    private: Ed25519PrivateKey

    @classmethod
    def from_seed(cls, key_id: str, seed: bytes) -> SigningKey:
        return cls(key_id, Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed).digest()))

    @property
    def public_bytes(self) -> bytes:
        return self.private.public_key().public_bytes_raw()

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_bytes).decode("ascii")

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self.private.sign(message)).decode("ascii")


def manifest_keys(keys: Sequence[SigningKey]) -> list[dict[str, str]]:
    return [{"key_id": key.key_id, "public_key": key.public_b64} for key in keys]


# --- Construcción de cadenas con el código de la plataforma -------------------------------------


def canonical_timestamp(value: datetime) -> str:
    """Texto de una marca como lo escribe la base: UTC, milisegundos y ``Z``."""
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def genesis(organization_id: uuid.UUID, plant_id: uuid.UUID | None) -> str:
    tail = "organization" if plant_id is None else str(plant_id)
    return hashlib.sha256(f"vigia:genesis:{organization_id}:{tail}".encode()).hexdigest()


def signed_message(
    kind: str,
    organization_id: str,
    plant_id: str | None,
    covered_sequence: int,
    covered_hash: str,
    taken_at: str,
) -> bytes:
    """El canónico que firma la clave ``checkpoint``, con la canonicalización de U-01."""
    return canonicalize(
        {
            "kind": kind,
            "organization_id": organization_id,
            "plant_id": plant_id,
            "covered_sequence": covered_sequence,
            "covered_hash": covered_hash,
            "taken_at": taken_at,
        }
    )


@dataclass
class ChainBuilder:
    """Cadena en construcción, fila a fila, como la encadenaría el disparador."""

    kind: str
    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    start: datetime = datetime(2026, 9, 1, tzinfo=UTC)
    rows: list[dict[str, Any]] = field(default_factory=list)
    first_sequence: int = 1
    start_hash: str | None = None

    @property
    def last_sequence(self) -> int:
        return self.first_sequence - 1 + len(self.rows)

    @property
    def last_hash(self) -> str:
        if self.rows:
            return str(self.rows[-1]["record_hash" if self.kind == "ledger" else "entry_hash"])
        if self.start_hash is not None:
            return self.start_hash
        return genesis(self.organization_id, None if self.kind == "audit" else self.plant_id)

    def _moment(self) -> datetime:
        return self.start + timedelta(milliseconds=37 * (len(self.rows) + 1))

    def append(
        self,
        content: Mapping[str, Any] | None,
        *,
        name: str = "Coordinación SST",
        record_type: str = "zone_created",
        operation: str = "ledger_read",
        checkpoint: bool = False,
    ) -> dict[str, Any]:
        sequence = self.last_sequence + 1
        previous = self.last_hash
        common: dict[str, Any] = {
            "organization_id": self.organization_id,
            "chain_sequence": sequence,
            "actor_kind": "user",
            "actor_id": uuid.uuid5(self.organization_id, f"actor-{sequence}"),
            "actor_display_name_snapshot": name,
            "actor_role_in_use": "coordinator_sst",
            "actor_concession_id": None,
            "actor_unit": "U-02",
            "correlation_id": uuid.uuid5(self.organization_id, f"correlation-{sequence}"),
            "previous_hash": previous,
        }
        document = None if content is None else dict(content)
        if self.kind == "ledger":
            data = canonical_bytes_sync(document or {})
            row = {
                **common,
                "record_id": uuid.uuid5(self.organization_id, f"{self.plant_id}-{sequence}"),
                "plant_id": self.plant_id,
                "record_type": CHECKPOINT if checkpoint else record_type,
                "schema_version": 1,
                "scope_plant_id": self.plant_id,
                "scope_zone_id": None,
                "scope_node_id": None,
                "received_at": self._moment(),
                "content": data,
                "content_hash": hashlib.sha256(data).hexdigest(),
            }
            row["record_hash"] = hashlib.sha256(
                envelope_canonical(row) + previous.encode("ascii")
            ).hexdigest()
        else:
            data_or_none = None if document is None else canonical_bytes_sync(document)
            row = {
                **common,
                "entry_id": uuid.uuid5(self.organization_id, f"audit-{sequence}"),
                "operation": CHECKPOINT if checkpoint else operation,
                "scope_plant_id": None,
                "scope_zone_id": None,
                "resource_kind": None,
                "resource_id": None,
                "filters": data_or_none,
                "filters_hash": (
                    None if data_or_none is None else hashlib.sha256(data_or_none).hexdigest()
                ),
                "result_count": 3,
                "outcome": "success",
                "occurred_at": self._moment(),
            }
            row["entry_hash"] = hashlib.sha256(
                audit_envelope_canonical(row) + previous.encode("ascii")
            ).hexdigest()
        self.rows.append(row)
        return row

    def append_checkpoint(self, key: SigningKey) -> dict[str, Any]:
        covered_sequence, covered_hash = self.last_sequence, self.last_hash
        taken_at = canonical_timestamp(self._moment())
        plant = None if self.kind == "audit" or self.plant_id is None else str(self.plant_id)
        message = signed_message(
            self.kind, str(self.organization_id), plant, covered_sequence, covered_hash, taken_at
        )
        content = {
            "covered_sequence": covered_sequence,
            "covered_hash": covered_hash,
            "taken_at": taken_at,
            "key_id": key.key_id,
            "signature": key.sign(message),
        }
        return self.append(content, record_type=CHECKPOINT, checkpoint=True)

    def entries(self) -> list[dict[str, Any]]:
        return [row_to_entry(self.kind, row) for row in self.rows]

    def manifest_chain(self, file: str) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "plant_id": None
            if self.plant_id is None or self.kind == "audit"
            else str(self.plant_id),
            "file": file,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "last_hash": self.last_hash,
        }


def _text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _content_document(data: bytes | None) -> Any:
    return None if data is None else json.loads(data, parse_int=_parse_int)


def _parse_int(text: str) -> int | float:
    value = int(text)
    return value if abs(value) <= 2**53 - 1 else float(text)


def row_to_entry(kind: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Columnas persistidas (una fila de la base o de ``ChainBuilder``) → entrada del paquete."""
    actor = {
        "kind": row["actor_kind"],
        "id": _text(row["actor_id"]),
        "display_name_snapshot": row["actor_display_name_snapshot"],
        "role_in_use": row["actor_role_in_use"],
        "concession_id": _text(row["actor_concession_id"]),
        "unit": row["actor_unit"],
    }
    if kind == "ledger":
        return {
            "record_id": _text(row["record_id"]),
            "organization_id": _text(row["organization_id"]),
            "plant_id": _text(row["plant_id"]),
            "chain_sequence": row["chain_sequence"],
            "record_type": row["record_type"],
            "schema_version": row["schema_version"],
            "actor": actor,
            "scope": {
                "plant_id": _text(row["scope_plant_id"]),
                "zone_id": _text(row["scope_zone_id"]),
                "node_id": _text(row["scope_node_id"]),
            },
            "correlation_id": _text(row["correlation_id"]),
            "received_at": canonical_timestamp(row["received_at"]),
            "content": _content_document(bytes(row["content"])),
            "content_hash": row["content_hash"],
            "previous_hash": row["previous_hash"],
            "record_hash": row["record_hash"],
        }
    resource = (
        None
        if row["resource_kind"] is None and row["resource_id"] is None
        else {"kind": row["resource_kind"], "id": _text(row["resource_id"])}
    )
    filters = row["filters"]
    return {
        "entry_id": _text(row["entry_id"]),
        "organization_id": _text(row["organization_id"]),
        "chain_sequence": row["chain_sequence"],
        "actor": actor,
        "operation": row["operation"],
        "scope": {"plant_id": _text(row["scope_plant_id"]), "zone_id": _text(row["scope_zone_id"])},
        "resource_ref": resource,
        "filters": _content_document(None if filters is None else bytes(filters)),
        "filters_hash": row["filters_hash"],
        "result_count": row["result_count"],
        "outcome": row["outcome"],
        "correlation_id": _text(row["correlation_id"]),
        "occurred_at": canonical_timestamp(row["occurred_at"]),
        "previous_hash": row["previous_hash"],
        "entry_hash": row["entry_hash"],
    }


# --- Escritura del paquete ----------------------------------------------------------------------


@dataclass
class PackageChain:
    kind: str
    plant_id: str | None
    file: str
    entries: list[Any]
    first_sequence: int
    last_sequence: int
    last_hash: str

    @classmethod
    def from_builder(cls, builder: ChainBuilder, file: str) -> PackageChain:
        declared = builder.manifest_chain(file)
        return cls(
            builder.kind,
            declared["plant_id"],
            file,
            builder.entries(),
            builder.first_sequence,
            builder.last_sequence,
            builder.last_hash,
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "plant_id": self.plant_id,
            "file": self.file,
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "last_hash": self.last_hash,
        }


def jsonl(entries: Sequence[Any]) -> bytes:
    return b"".join(
        json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for entry in entries
    )


def manifest_document(
    organization_id: uuid.UUID | str, chains: Sequence[PackageChain], keys: Sequence[SigningKey]
) -> dict[str, Any]:
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "organization_id": str(organization_id),
        "chains": [chain.manifest() for chain in chains],
        "checkpoint_keys": manifest_keys(keys),
    }


def write_package(
    path: Path,
    organization_id: uuid.UUID | str,
    chains: Sequence[PackageChain],
    keys: Sequence[SigningKey],
    *,
    as_zip: bool = False,
    manifest: Mapping[str, Any] | None = None,
) -> Path:
    """Escribe el paquete en el directorio ``path`` (o en el zip ``path``)."""
    document = (
        dict(manifest) if manifest is not None else manifest_document(organization_id, chains, keys)
    )
    files = {"manifest.json": json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")}
    files.update({chain.file: jsonl(chain.entries) for chain in chains})
    if as_zip:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return path
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return path


# --- Recorrido de referencia de la plataforma ---------------------------------------------------


class _Malformed(Exception):
    pass


def _uuid_column(value: Any, *, optional: bool = False) -> uuid.UUID | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise _Malformed
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise _Malformed from None
    if str(parsed) != value:
        raise _Malformed
    return parsed


def _timestamp_column(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _Malformed
    try:
        parsed = datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC)
    except ValueError:
        raise _Malformed from None
    if canonical_timestamp(parsed) != value:
        raise _Malformed
    return parsed


def _hex(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise _Malformed
    return value


def _dict(value: Any, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _Malformed
    return value


def _integer(value: Any, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Malformed
    return value


def _actor_columns(entry: Mapping[str, Any]) -> dict[str, Any]:
    actor = _dict(entry["actor"], ACTOR_KEYS)
    return {
        "actor_kind": actor["kind"],
        "actor_id": _uuid_column(actor["id"]),
        "actor_display_name_snapshot": actor["display_name_snapshot"],
        "actor_role_in_use": actor["role_in_use"],
        "actor_concession_id": _uuid_column(actor["concession_id"], optional=True),
        "actor_unit": actor["unit"],
    }


def _record_columns(entry: Mapping[str, Any]) -> dict[str, Any]:
    _dict(entry, RECORD_KEYS)
    scope = _dict(entry["scope"], frozenset({"plant_id", "zone_id", "node_id"}))
    return {
        **_actor_columns(entry),
        "record_id": _uuid_column(entry["record_id"]),
        "organization_id": _uuid_column(entry["organization_id"]),
        "plant_id": _uuid_column(entry["plant_id"], optional=True),
        "chain_sequence": _integer(entry["chain_sequence"]),
        "record_type": entry["record_type"],
        "schema_version": _integer(entry["schema_version"]),
        "scope_plant_id": _uuid_column(scope["plant_id"], optional=True),
        "scope_zone_id": _uuid_column(scope["zone_id"], optional=True),
        "scope_node_id": _uuid_column(scope["node_id"], optional=True),
        "correlation_id": _uuid_column(entry["correlation_id"]),
        "received_at": _timestamp_column(entry["received_at"]),
        "content_hash": _hex(entry["content_hash"]),
        "previous_hash": _hex(entry["previous_hash"]),
        "record_hash": _hex(entry["record_hash"]),
    }


def _audit_columns(entry: Mapping[str, Any]) -> dict[str, Any]:
    _dict(entry, AUDIT_KEYS)
    scope = _dict(entry["scope"], frozenset({"plant_id", "zone_id"}))
    resource = entry["resource_ref"]
    if resource is not None:
        resource = _dict(resource, frozenset({"kind", "id"}))
    return {
        **_actor_columns(entry),
        "entry_id": _uuid_column(entry["entry_id"]),
        "organization_id": _uuid_column(entry["organization_id"]),
        "chain_sequence": _integer(entry["chain_sequence"]),
        "operation": entry["operation"],
        "scope_plant_id": _uuid_column(scope["plant_id"], optional=True),
        "scope_zone_id": _uuid_column(scope["zone_id"], optional=True),
        "resource_kind": None if resource is None else resource["kind"],
        "resource_id": None if resource is None else _uuid_column(resource["id"], optional=True),
        "filters_hash": _hex(entry["filters_hash"], optional=True),
        "result_count": _integer(entry["result_count"], optional=True),
        "outcome": entry["outcome"],
        "correlation_id": _uuid_column(entry["correlation_id"]),
        "occurred_at": _timestamp_column(entry["occurred_at"]),
        "previous_hash": _hex(entry["previous_hash"]),
        "entry_hash": _hex(entry["entry_hash"]),
    }


def _checkpoint_ok(
    kind: str,
    organization_id: str,
    plant_id: str | None,
    content: Any,
    sequence: int,
    previous: str,
    public_keys: Mapping[str, Ed25519PublicKey],
) -> bool:
    if not isinstance(content, dict) or set(content) != {
        "covered_sequence",
        "covered_hash",
        "taken_at",
        "key_id",
        "signature",
    }:
        return False
    if content["covered_sequence"] != sequence - 1 or content["covered_hash"] != previous:
        return False
    try:
        _integer(content["covered_sequence"])
        _hex(content["covered_hash"])
        _timestamp_column(content["taken_at"])
    except _Malformed:
        return False
    key = public_keys.get(content["key_id"]) if isinstance(content["key_id"], str) else None
    signature_text = content["signature"]
    if key is None or not isinstance(signature_text, str) or len(signature_text) != 88:
        return False
    try:
        signature = base64.b64decode(signature_text, validate=True)
        if base64.b64encode(signature).decode("ascii") != signature_text:
            return False  # base64 no canónico: no es la firma escrita
        key.verify(
            signature,
            signed_message(
                kind,
                organization_id,
                plant_id,
                content["covered_sequence"],
                content["covered_hash"],
                content["taken_at"],
            ),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


@dataclass(frozen=True)
class Verdict:
    status: str
    sequence: int | None
    entry_id: str | None


def reference_walk(
    kind: str,
    organization_id: str,
    plant_id: str | None,
    entries: Sequence[Any],
    public_keys: Mapping[str, bytes],
    *,
    declared_head: tuple[int, str],
    first_sequence: int = 1,
    anchors: Mapping[int, str] | None = None,
) -> Verdict:
    """Pseudocódigo de business-logic-model §5 con el código de la plataforma."""
    keys = {key_id: Ed25519PublicKey.from_public_bytes(raw) for key_id, raw in public_keys.items()}
    anchors = dict(anchors or {})
    id_key = "entry_id" if kind == "audit" else "record_id"
    if first_sequence == 1:
        previous = genesis(
            uuid.UUID(organization_id), None if plant_id is None else uuid.UUID(plant_id)
        )
    else:
        first = entries[0] if entries else None
        candidate = first.get("previous_hash") if isinstance(first, dict) else None
        previous = (
            candidate if isinstance(candidate, str) else genesis(uuid.UUID(organization_id), None)
        )
    sequence = first_sequence - 1
    matched: set[int] = set()
    if sequence in anchors:
        if anchors[sequence] != previous:
            return Verdict("broken", sequence + 1, None)
        matched.add(sequence)
    last_id: str | None = None
    for entry in entries:
        expected = sequence + 1
        raw_id = entry.get(id_key) if isinstance(entry, dict) else None
        found_id = raw_id if isinstance(raw_id, str) and _is_uuid_text(raw_id) else None
        broken = Verdict("broken", expected, found_id)
        try:
            columns = _audit_columns(entry) if kind == "audit" else _record_columns(entry)
        except (_Malformed, KeyError, TypeError):
            return broken
        if columns["chain_sequence"] != expected:
            return broken
        if str(columns["organization_id"]) != organization_id:
            return broken
        if kind == "ledger" and _text(columns["plant_id"]) != plant_id:
            return broken
        if columns["previous_hash"] != previous:
            return broken
        content = entry["filters"] if kind == "audit" else entry["content"]
        try:
            if kind == "audit" and content is None:
                content_hash = None
            else:
                content_hash = hashlib.sha256(canonical_bytes_sync(content)).hexdigest()
            if content_hash != columns["filters_hash" if kind == "audit" else "content_hash"]:
                return broken
            envelope = (
                audit_envelope_canonical(columns)
                if kind == "audit"
                else envelope_canonical(columns)
            )
        except CanonicalFormError:
            return broken
        own_hash = columns["entry_hash" if kind == "audit" else "record_hash"]
        if hashlib.sha256(envelope + previous.encode("ascii")).hexdigest() != own_hash:
            return broken
        if expected in anchors:
            if anchors[expected] != own_hash:
                return broken
            matched.add(expected)
        if (columns["operation"] if kind == "audit" else columns["record_type"]) == CHECKPOINT:
            chain_plant = None if kind == "audit" else plant_id
            if not _checkpoint_ok(
                kind, organization_id, chain_plant, content, expected, previous, keys
            ):
                return broken
        previous, sequence, last_id = own_hash, expected, found_id
    declared_sequence, declared_hash = declared_head
    if declared_sequence > sequence:
        return Verdict("broken", sequence + 1, None)
    if declared_sequence < sequence:
        return Verdict("broken", declared_sequence + 1, None)
    if declared_hash != previous:
        return Verdict("broken", sequence, last_id)
    missing = sorted(set(anchors) - matched)
    if missing:
        return Verdict("broken", missing[0], None)
    return Verdict("intact", None, None)


def _is_uuid_text(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
