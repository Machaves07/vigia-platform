"""Corpus permanente de paquetes exportados hostiles (TASK-346; PAT-LAZ-SEG-02; NFR-LAZ-23; L-6).

Construidos **a mano** sobre un paquete de control mínimo e íntegro, no generados al azar: son
las entradas adversarias del escenario R6 invertido (alguien entrega a un tercero un paquete
alterado). Cada caso es un directorio con su ``vol-01.zip`` y su ``expected.json`` (estado,
motivo y posición que el verificador debe dar). Todo caso nuevo que aparezca se añade aquí como
regresión.

| Caso | Defensa | Motivo esperado |
|---|---|---|
| ``control`` | ninguna: el paquete íntegro | ``intact`` |
| ``traversal`` | rutas con «..» | ``path_traversal`` |
| ``absolute_path`` | rutas absolutas | ``absolute_path`` |
| ``compression_bomb`` | expansión de más de 100 veces | ``expansion_ratio`` |
| ``symlink`` | enlaces simbólicos | ``symlink`` |
| ``manifest_altered`` | conteos frente al contenido (oculta un hallazgo) | ``manifest_mismatch`` |
| ``artifact_bit_flip`` | hashes del manifiesto antes de extraer | ``artifact_hash_mismatch`` |
| ``forged_checkpoint`` | firma del punto de control con las claves incluidas | ``bad_signature`` |
| ``retired_key_after_retirement`` | vigencia de la clave al firmar | ``key_not_valid_at_signing`` |
| ``manifest_stale_signature`` | firma del manifiesto | ``manifest_signature_mismatch`` |
| ``manifest_resigned_by_stranger`` | firma con una clave ajena | ``bad_signature`` |
| ``manifest_signed_with_retired_key`` | clave retirada | ``key_not_valid_at_signing`` |
| ``unsigned`` | sin firma ni ``--manifest-sha256`` | ``unanchored`` (código 3) |

El control y todos los paquetes llevan ``manifest.signature.json``, la firma del SHA-256 del
manifiesto con la clave activa (decisión del dueño A2), salvo ``unsigned``.

Los hashes y las firmas salen del código de la plataforma (``ledger.canonical`` y
``cryptography``), no de los módulos puros del verificador. Las claves de firma salen de semillas
fijas: el corpus es determinista.

Uso, desde ``backend/``:

- ``uv run python tests/fixtures/synthetic/hostile_packages/build_hostile_packages.py``: escribe
  el corpus.
- ``... --check``: lo regenera en memoria y termina en 1 si algún caso versionado difiere en sus
  entradas (nombre, tipo, tamaño declarado y contenido) o en su ``expected.json``. No compara los
  bytes comprimidos, que dependen de la versión de zlib.

Solo datos sintéticos.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import stat
import sys
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vigia_platform.ledger.canonical import audit_envelope_canonical, canonical_bytes_sync
from vigia_platform.ledger.chain.chain_walk import checkpoint_message, genesis_hash

HERE: Final = Path(__file__).resolve().parent
ORGANIZATION: Final = uuid.UUID("018f0000-0000-7000-8000-00000000a001")
PLANT: Final = uuid.UUID("018f0000-0000-7000-8000-00000000b001")
ZONE: Final = uuid.UUID("018f0000-0000-7000-8000-00000000c001")
SUBJECT: Final = uuid.UUID("018f0000-0000-7000-8000-00000000d001")
ACTOR: Final = uuid.UUID("018f0000-0000-7000-8000-00000000e001")
CORRELATION: Final = uuid.UUID("018f0000-0000-7000-8000-00000000f001")
EXPORT: Final = uuid.UUID("018f0000-0000-7000-8000-0000000a0001")
RETIRED_KEY: Final = "checkpoint-2025-01"
ACTIVE_KEY: Final = "checkpoint-2026-01"
SEEDS: Final = {RETIRED_KEY: bytes(range(64, 96)), ACTIVE_KEY: bytes(range(160, 192))}
FORGER_SEED: Final = bytes(range(200, 232))
KEY_WINDOWS: Final = {
    RETIRED_KEY: ("2025-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z", "retired"),
    ACTIVE_KEY: ("2026-01-01T00:00:00.000Z", "2027-06-01T00:00:00.000Z", "active"),
}
TAKEN_AT: Final = "2026-05-01T00:00:00.000Z"
AFTER_RETIREMENT: Final = "2026-03-01T00:00:00.000Z"
GENERATED_AT: Final = "2026-05-01T00:05:00.000Z"
FINDINGS_PATH: Final = f"findings/zone-{ZONE}.json"
SIGNATURE_PATH: Final = "manifest.signature.json"
SIGNATURE_DECLARATION: Final = {
    "path": SIGNATURE_PATH,
    "algorithm": "Ed25519",
    "key_purpose": "checkpoint",
    "payload_kind": "vigia.export.manifest",
}
DATE_TIME: Final = (1980, 1, 1, 0, 0, 0)
STATES: Final = (
    "received",
    "in_review",
    "discarded",
    "open",
    "action_assigned",
    "overdue",
    "closed",
    "unclosable",
    "suspended_by_reclassification",
)
BOMB_BYTES: Final = 16 * 1024 * 1024
"""16 MiB de ceros: se comprimen a unos 16 KB (más de 1000 veces)."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(document: Any) -> bytes:
    return canonical_bytes_sync(document)


def _key(seed: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def _sign(seed: bytes, kind: str, plant: str | None, sequence: int, covered: str, at: str) -> str:
    message = checkpoint_message(kind, str(ORGANIZATION), plant, sequence, covered, at)
    return base64.b64encode(_key(seed).sign(message)).decode("ascii")


# --- el paquete de control ----------------------------------------------------------------------


@dataclass
class Entry:
    """Una entrada del volumen: nombre, bytes y lo que la hace hostil."""

    name: str
    data: bytes
    deflate: bool = False
    mode: int = stat.S_IFREG | 0o644
    kind: str = ""


@dataclass
class Package:
    entries: list[Entry] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, Any] = field(default_factory=dict)
    signer: tuple[bytes, str] | None = (SEEDS[ACTIVE_KEY], ACTIVE_KEY)
    """Semilla y ``key_id`` que firman el SHA-256 del manifiesto; ``None``: sin firma."""
    frozen_signature: bytes | None = None
    """Una firma ya hecha (la de antes de alterar el manifiesto), en vez de firmar de nuevo."""

    def freeze_signature(self) -> None:
        """Guarda la firma del manifiesto actual: lo que se altere después ya no la cuadra."""
        self.frozen_signature = self.signature(_canonical(self.manifest))

    def unsign(self) -> None:
        """Quita la firma y su declaración: el paquete queda sin ancla."""
        self.signer, self.frozen_signature = None, None
        self.manifest.pop("manifest_signature", None)

    def signature(self, manifest: bytes) -> bytes | None:
        if self.frozen_signature is not None:
            return self.frozen_signature
        if self.signer is None:
            return None
        seed, key_id = self.signer
        payload = {
            "kind": "vigia.export.manifest",
            "export_id": self.manifest["export_id"],
            "organization_id": self.manifest["organization"]["organization_id"],
            "plant_id": self.manifest["plant"]["plant_id"],
            "manifest_sha256": _sha256(manifest),
            "generated_at": self.manifest["generated_at"],
        }
        signature = base64.b64encode(_key(seed).sign(_canonical(payload))).decode("ascii")
        return _canonical({"payload": payload, "key_id": key_id, "signature": signature})

    def set_document(self, name: str, document: Any) -> None:
        """Sustituye un artefacto JSON y re-hashea el manifiesto (como haría quien lo altera)."""
        self.documents[name] = document
        data = _canonical(document)
        for entry in self.entries:
            if entry.name == name:
                entry.data = data
        for artifact in self.manifest["artifacts"]:
            if artifact["path"] == name:
                artifact["sha256"] = _sha256(data)
                artifact["size_bytes"] = len(data)

    def volume(self) -> bytes:
        output = io.BytesIO()
        manifest = _canonical(self.manifest)
        tail = [Entry("manifest.json", manifest, True)]
        signature = self.signature(manifest)
        if signature is not None:
            tail.append(Entry("manifest.signature.json", signature, True))
        with zipfile.ZipFile(output, mode="w", allowZip64=False) as archive:
            for entry in [*self.entries, *tail]:
                info = zipfile.ZipInfo(entry.name, date_time=DATE_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED if entry.deflate else zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = entry.mode << 16
                archive.writestr(info, entry.data)
        return output.getvalue()


def _record(sequence: int, previous: str, content: dict[str, Any]) -> dict[str, Any]:
    content_hash = _sha256(_canonical(content))
    return {
        "record_id": str(uuid.UUID(int=0x018F0000_0000_7000_8000_000000000100 + sequence)),
        "record_type": "finding_received" if sequence == 1 else "finding_classified",
        "schema_version": 1,
        "chain_sequence": sequence,
        "received_at": f"2026-04-0{sequence}T10:00:00.000Z",
        "content_hash": content_hash,
        "previous_hash": previous,
        # El sobre completo no viaja en el paquete: un hash del contenido y del enlace basta.
        "record_hash": _sha256((content_hash + previous).encode("ascii")),
        "evidence_ids": [],
        "content": content,
    }


def _audit_entry(sequence: int, previous: str) -> dict[str, Any]:
    entry_id = uuid.UUID(int=0x018F0000_0000_7000_8000_000000000200 + sequence)
    filters = {"zone_id": str(ZONE)}
    occurred = datetime(2026, 4, sequence, 12, 0, tzinfo=UTC)
    row = {
        "actor_kind": "user",
        "actor_id": ACTOR,
        "actor_display_name_snapshot": "Coordinación SST sintética",
        "actor_role_in_use": "coordinator_sst",
        "actor_concession_id": None,
        "actor_unit": "u02",
        "chain_sequence": sequence,
        "correlation_id": CORRELATION,
        "entry_id": entry_id,
        "filters_hash": _sha256(_canonical(filters)),
        "occurred_at": occurred,
        "operation": "ledger_read",
        "organization_id": ORGANIZATION,
        "outcome": "success",
        "resource_kind": None,
        "resource_id": None,
        "result_count": sequence,
        "scope_plant_id": PLANT,
        "scope_zone_id": ZONE,
    }
    entry_hash = _sha256(audit_envelope_canonical(row) + previous.encode("ascii"))
    return {
        "entry_id": str(entry_id),
        "chain_sequence": sequence,
        "actor": {
            "kind": "user",
            "id": str(ACTOR),
            "display_name_snapshot": "Coordinación SST sintética",
            "role_in_use": "coordinator_sst",
            "concession_id": None,
            "unit": "u02",
        },
        "operation": "ledger_read",
        "scope_plant_id": str(PLANT),
        "scope_zone_id": str(ZONE),
        "resource_kind": None,
        "resource_id": None,
        "filters": filters,
        "result_count": sequence,
        "outcome": "success",
        "correlation_id": str(CORRELATION),
        "occurred_at": "2026-04-0" + str(sequence) + "T12:00:00.000Z",
        "previous_hash": previous,
        "entry_hash": entry_hash,
    }


def _checkpoint(
    kind: str, sequence: int, covered: str, *, seed: bytes, key_id: str, taken_at: str
) -> dict[str, Any]:
    plant = str(PLANT) if kind == "ledger" else None
    return {
        "kind": kind,
        "plant_id": plant,
        "entry_id": str(uuid.UUID(int=0x018F0000_0000_7000_8000_000000000300 + sequence)),
        "sequence": sequence + 1,
        "entry_hash": _sha256(f"{kind}:{sequence}:{covered}".encode()),
        "content": {
            "covered_sequence": sequence,
            "covered_hash": covered,
            "taken_at": taken_at,
            "key_id": key_id,
            "signature": _sign(seed, kind, plant, sequence, covered, taken_at),
        },
    }


def _public_keys() -> list[dict[str, Any]]:
    keys = []
    for key_id in (RETIRED_KEY, ACTIVE_KEY):
        valid_from, valid_until, status = KEY_WINDOWS[key_id]
        raw = _key(SEEDS[key_id]).public_key().public_bytes_raw()
        keys.append(
            {
                "key_id": key_id,
                "algorithm": "Ed25519",
                "public_key": base64.b64encode(raw).decode("ascii"),
                "status": status,
                "valid_from": valid_from,
                "valid_until": valid_until,
            }
        )
    return keys


def control_package() -> Package:
    """Un paquete mínimo e íntegro: una zona, un hallazgo con dos registros y dos de auditoría."""
    first = _record(
        1, genesis_hash(str(ORGANIZATION), str(PLANT)), {"episode": "sintético", "n": 1}
    )
    second = _record(2, first["record_hash"], {"outcome": "confirmed", "n": 2})
    audit_one = _audit_entry(1, genesis_hash(str(ORGANIZATION), None))
    audit_two = _audit_entry(2, audit_one["entry_hash"])
    checkpoints = [
        _checkpoint(
            "ledger",
            2,
            second["record_hash"],
            seed=SEEDS[ACTIVE_KEY],
            key_id=ACTIVE_KEY,
            taken_at=TAKEN_AT,
        ),
        _checkpoint(
            "audit",
            2,
            audit_two["entry_hash"],
            seed=SEEDS[ACTIVE_KEY],
            key_id=ACTIVE_KEY,
            taken_at=TAKEN_AT,
        ),
    ]
    keys = _public_keys()
    findings_path = f"findings/zone-{ZONE}.json"
    coverage_path = f"coverage/zone-{ZONE}.json"
    documents: dict[str, Any] = {
        findings_path: {
            "zone_id": str(ZONE),
            "single_occupancy": False,
            "aggregation_window_minutes": 60,
            "findings": [
                {
                    "subject_kind": "episode",
                    "subject_id": str(SUBJECT),
                    "zone_id": str(ZONE),
                    "lifecycle_state": "open",
                    "evidence_flagged": False,
                    "records": [first, second],
                }
            ],
        },
        coverage_path: {
            "zone_id": str(ZONE),
            "intervals": [
                {"state": "observable", "from": "2026-04-01T00:00:00.000Z"},
                {"state": "not_observable", "from": "2026-04-15T00:00:00.000Z"},
            ],
        },
        "audit/audit-log.json": {"entries": [audit_one, audit_two]},
        "checkpoints/checkpoints.json": {"checkpoints": checkpoints},
        "keys/checkpoint-public-keys.json": {"keys": keys},
    }
    kinds = {
        findings_path: "findings_json",
        coverage_path: "coverage",
        "audit/audit-log.json": "audit",
        "checkpoints/checkpoints.json": "checkpoint",
        "keys/checkpoint-public-keys.json": "public_key",
    }
    package = Package(documents=documents)
    for name, document in documents.items():
        package.entries.append(Entry(name, _canonical(document), kind=kinds[name]))
    counts = dict.fromkeys(STATES, 0)
    counts["open"] = 1
    package.manifest = {
        "manifest_version": 1,
        "export_id": str(EXPORT),
        "package_form": "without_clips",
        "organization": {"organization_id": str(ORGANIZATION), "code": "org-s", "name": "Org"},
        "plant": {"plant_id": str(PLANT), "code": "pl-s", "name": "Planta", "timezone": "UTC"},
        "zones": [{"zone_id": str(ZONE), "code": "z-s", "name": "Zona sintética"}],
        "finding_count": 1,
        "open_finding_count": 1,
        "closed_finding_count": 0,
        "unclosable_count": 0,
        "discarded_count": 0,
        "suspended_count": 0,
        "evidence_flagged_count": 0,
        "state_counts": counts,
        "clips": [],
        "attachments": [],
        "artifacts": [
            {
                "path": entry.name,
                "kind": entry.kind,
                "sha256": _sha256(entry.data),
                "size_bytes": len(entry.data),
                "volume": 1,
            }
            for entry in package.entries
        ],
        "absent_entries": [
            {"kind": "document", "reason_es": "ausente en el corpus"},
            {"kind": "verifier", "reason_es": "ausente en el corpus"},
        ],
        "checkpoints": checkpoints,
        "checkpoint_public_keys": keys,
        "verifier_version": "absent",
        "generated_at": GENERATED_AT,
        "manifest_signature": dict(SIGNATURE_DECLARATION),
    }
    return package


def close_open_finding(package: Package) -> None:
    """Cierra el hallazgo abierto y ajusta los conteos y el hash: una falsificación coherente."""
    findings = json.loads(json.dumps(package.documents[FINDINGS_PATH]))
    findings["findings"][0]["lifecycle_state"] = "closed"
    package.set_document(FINDINGS_PATH, findings)
    package.manifest["state_counts"].update(open=0, closed=1)
    package.manifest.update(open_finding_count=0, closed_finding_count=1)


def rewrite_record_content(package: Package) -> None:
    """Cambia el contenido del registro 1 y recalcula su ``content_hash``."""
    findings = json.loads(json.dumps(package.documents[FINDINGS_PATH]))
    record = findings["findings"][0]["records"][0]
    record["content"] = {"episode": "reescrito", "n": 1}
    record["content_hash"] = _sha256(_canonical(record["content"]))
    package.set_document(FINDINGS_PATH, findings)


def move_received_at(package: Package) -> None:
    """Cambia el ``received_at`` del registro 2 (fuera de su contenido)."""
    findings = json.loads(json.dumps(package.documents[FINDINGS_PATH]))
    findings["findings"][0]["records"][1]["received_at"] = "2026-04-09T10:00:00.000Z"
    package.set_document(FINDINGS_PATH, findings)


FORGERIES: Final = {
    "record_content": rewrite_record_content,
    "received_at": move_received_at,
    "closed_finding": close_open_finding,
}
"""Las tres sondas del revisor (ronda 1): falsificaciones coherentes que solo el ancla delata."""


# --- los casos ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    name: str
    build: Callable[[], Package]
    expected: dict[str, Any]


def _broken(reason: str, artifact: str, pointer: str | None = None) -> dict[str, Any]:
    return {
        "status": "broken",
        "reason": reason,
        "volume": "vol-01.zip",
        "artifact": artifact,
        "pointer": pointer,
    }


def _with_entry(entry: Entry) -> Callable[[], Package]:
    def build() -> Package:
        package = control_package()
        package.entries.insert(0, entry)
        return package

    return build


def _manifest_altered() -> Package:
    # Oculta el hallazgo abierto: el manifiesto dice cero, el archivo de hallazgos lo lleva.
    package = control_package()
    package.manifest["state_counts"]["open"] = 0
    package.manifest["finding_count"] = 0
    package.manifest["open_finding_count"] = 0
    return package


def _artifact_bit_flip() -> Package:
    # Un bit de la línea de tiempo («observable» → «obsfrvable»): el manifiesto no se toca.
    package = control_package()
    entry = next(item for item in package.entries if item.name.startswith("coverage/"))
    position = entry.data.index(b"observable") + 3
    changed = bytearray(entry.data)
    changed[position] ^= 0x01
    entry.data = bytes(changed)
    return package


def _replace_ledger_checkpoint(checkpoint: dict[str, Any]) -> Package:
    package = control_package()
    checkpoints = package.documents["checkpoints/checkpoints.json"]["checkpoints"]
    checkpoints[0] = checkpoint
    package.manifest["checkpoints"] = checkpoints
    package.set_document("checkpoints/checkpoints.json", {"checkpoints": checkpoints})
    return package


def _forged_checkpoint() -> Package:
    # Otro hash cubierto, firmado con una clave ajena pero con el key_id de la clave activa.
    package = control_package()
    original = package.documents["checkpoints/checkpoints.json"]["checkpoints"][0]
    forged = _checkpoint(
        "ledger",
        2,
        _sha256(b"prefijo reescrito"),
        seed=FORGER_SEED,
        key_id=ACTIVE_KEY,
        taken_at=original["content"]["taken_at"],
    )
    forged["entry_id"] = original["entry_id"]
    return _replace_ledger_checkpoint(forged)


def _retired_key_after_retirement() -> Package:
    # Firma válida de la clave retirada, pero con una marca posterior a su retiro.
    package = control_package()
    original = package.documents["checkpoints/checkpoints.json"]["checkpoints"][0]
    late = _checkpoint(
        "ledger",
        2,
        original["content"]["covered_hash"],
        seed=SEEDS[RETIRED_KEY],
        key_id=RETIRED_KEY,
        taken_at=AFTER_RETIREMENT,
    )
    late["entry_id"] = original["entry_id"]
    return _replace_ledger_checkpoint(late)


def _stale_signature() -> Package:
    # La firma de la plataforma se conserva, pero el hallazgo abierto se cierra después.
    package = control_package()
    package.freeze_signature()
    close_open_finding(package)
    return package


def _resigned_by_stranger() -> Package:
    # El mismo cierre, firmado con una clave ajena bajo el key_id de la clave activa.
    package = control_package()
    close_open_finding(package)
    package.signer = (FORGER_SEED, ACTIVE_KEY)
    return package


def _signed_with_retired_key() -> Package:
    # Firma válida de la clave retirada sobre un manifiesto generado después de su retiro.
    package = control_package()
    package.signer = (SEEDS[RETIRED_KEY], RETIRED_KEY)
    return package


def _unsigned() -> Package:
    package = control_package()
    package.unsign()
    return package


CASES: Final = (
    Case("control", control_package, {"status": "intact", "reason": None}),
    Case(
        "traversal",
        _with_entry(Entry("../../evil.txt", b"fuera del directorio\n")),
        _broken("path_traversal", "../../evil.txt"),
    ),
    Case(
        "absolute_path",
        _with_entry(Entry("/etc/vigia-evil", b"ruta absoluta\n")),
        _broken("absolute_path", "/etc/vigia-evil"),
    ),
    Case(
        "compression_bomb",
        _with_entry(Entry("bomb.bin", bytes(BOMB_BYTES), deflate=True)),
        _broken("expansion_ratio", "bomb.bin"),
    ),
    Case(
        "symlink",
        _with_entry(Entry("link-to-passwd", b"../../../../etc/passwd", mode=stat.S_IFLNK | 0o777)),
        _broken("symlink", "link-to-passwd"),
    ),
    Case(
        "manifest_altered",
        _manifest_altered,
        _broken("manifest_mismatch", "manifest.json", "/state_counts/open"),
    ),
    Case(
        "artifact_bit_flip",
        _artifact_bit_flip,
        _broken("artifact_hash_mismatch", f"coverage/zone-{ZONE}.json", "/artifacts/1"),
    ),
    Case(
        "forged_checkpoint",
        _forged_checkpoint,
        _broken("bad_signature", "checkpoints/checkpoints.json", "/checkpoints/0"),
    ),
    Case(
        "retired_key_after_retirement",
        _retired_key_after_retirement,
        _broken("key_not_valid_at_signing", "checkpoints/checkpoints.json", "/checkpoints/0"),
    ),
    Case(
        "manifest_stale_signature",
        _stale_signature,
        _broken("manifest_signature_mismatch", SIGNATURE_PATH),
    ),
    Case(
        "manifest_resigned_by_stranger",
        _resigned_by_stranger,
        _broken("bad_signature", SIGNATURE_PATH),
    ),
    Case(
        "manifest_signed_with_retired_key",
        _signed_with_retired_key,
        _broken("key_not_valid_at_signing", SIGNATURE_PATH),
    ),
    Case("unsigned", _unsigned, {"status": "unanchored", "reason": None}),
)
"""El control, los siete paquetes hostiles de TASK-346, la ruta absoluta y los cuatro del ancla
del manifiesto (ronda 1: decisión del dueño A2 y B1). Salvo en estos cuatro, el generador vuelve a
firmar el manifiesto con la clave activa después de alterarlo: así cada caso prueba **su**
defensa, la de debajo de la firma, y no la firma."""


def _expected_text(case: Case) -> str:
    return json.dumps(case.expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def describe(volume: bytes) -> list[tuple[str, int, int, int, str]]:
    """Lo que el corpus fija de un volumen: nombre, método, modo, tamaño y SHA-256 del contenido."""
    with zipfile.ZipFile(io.BytesIO(volume)) as archive:
        return [
            (
                info.orig_filename,
                info.compress_type,
                info.external_attr >> 16,
                info.file_size,
                _sha256(archive.read(info)),
            )
            for info in archive.infolist()
        ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corpus de paquetes exportados hostiles.")
    parser.add_argument(
        "--check", action="store_true", help="no escribe: compara con lo versionado"
    )
    parser.add_argument("--output", type=Path, default=HERE, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    output: Path = arguments.output
    status = 0
    for case in CASES:
        volume = case.build().volume()
        directory = output / case.name
        if arguments.check:
            stored = directory / "vol-01.zip"
            expected = directory / "expected.json"
            same = (
                stored.is_file()
                and expected.is_file()
                and describe(stored.read_bytes()) == describe(volume)
                and expected.read_text(encoding="utf-8") == _expected_text(case)
            )
            if not same:
                print(
                    f"build_hostile_packages: {case.name} difiere de lo generado", file=sys.stderr
                )
                status = 1
            continue
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "vol-01.zip").write_bytes(volume)
        (directory / "expected.json").write_text(_expected_text(case), encoding="utf-8")
        print(f"build_hostile_packages: escrito {case.name} ({len(volume)} bytes)")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
