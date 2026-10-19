"""Paquetes exportados verificables (TASK-346; PR-LAZ-28): el mundo generado con criptografía real,
el paquete real escrito en disco, sus volúmenes reescritos y las mutaciones ``package_mutations``.

- ``seal_world(world)``: el mismo mundo de ``exported_packages`` con lo que el verificador
  comprueba hecho **con el código de la plataforma**, independiente de los módulos puros: el
  ``content_hash`` de cada registro sobre ``canonical_bytes_sync``, el ``record_hash`` con
  ``envelope_canonical``, el ``entry_hash`` de la auditoría con ``audit_envelope_canonical``, y
  los puntos de control firmados con ``cryptography`` (Ed25519) sobre ``signed_payload``,
  cubriendo la cabeza real de cada cadena. Dos claves: una **retirada** y otra **activa**.
- ``compose_to_directory(world, directory)``: el paquete del compositor real (TASK-318) con sus
  volúmenes escritos en ``directory`` como los descarga un tercero.
- ``rewrite_volume(data, replace)``: el volumen con el contenido de unas entradas cambiado y todo
  lo demás igual (orden, compresión, fecha y atributos); así una mutación cambia solo lo que dice.
- ``package_mutations``: alterar un bit de un artefacto (manifiesto intacto), un bit del
  manifiesto, o un bit de un registro o entrada de auditoría como una **falsificación coherente**
  (ronda 1): el atacante rehace el ``content_hash`` o el ``entry_hash`` y re-hashea el
  manifiesto, y con la firma del manifiesto la deja, la quita o firma con una clave ajena.

Solo datos generados; las claves salen de semillas fijas.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import strategies as st

from tests.export_contents_support import Composed, compose_world
from tests.properties.laz.strategies.export import (
    WORLD_ACTIVE_KEY,
    WORLD_SIGNING_SEEDS,
    ExportWorld,
    stamp,
)
from vigia_platform.accreditation.export.volumes import (
    MANIFEST_NAME,
    MANIFEST_SIGNATURE_NAME,
    ExportLocation,
)
from vigia_platform.accreditation.verifier_bundle import manifest_signature_payload
from vigia_platform.ledger.application.reader import AuditEntryView, LedgerRecordView
from vigia_platform.ledger.canonical import (
    CanonicalFormError,
    audit_envelope_canonical,
    canonical_bytes_sync,
    envelope_canonical,
)
from vigia_platform.ledger.chain.chain_walk import genesis_hash
from vigia_platform.ledger.chain.checkpoints import (
    ChainKind,
    CheckpointChain,
    CheckpointContent,
    CheckpointPublicKey,
    StoredCheckpoint,
    signed_payload,
)
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError, canonicalize
from vigia_platform.shared.signing.keys import KeyStatus

__all__ = [
    "ACTIVE_KEY",
    "RETIRED_KEY",
    "Mutation",
    "apply_mutation",
    "compose_to_directory",
    "flip_bit",
    "leaves",
    "package_mutations",
    "private_key",
    "read_volume_entries",
    "rewrite_volume",
    "seal_world",
    "sign_checkpoint",
    "volume_paths",
]

RETIRED_KEY: Final = "checkpoint-2026-01"
ACTIVE_KEY: Final = WORLD_ACTIVE_KEY
_SEEDS: Final = WORLD_SIGNING_SEEDS


def private_key(key_id: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_SEEDS[key_id])


def _public_text(key_id: str) -> str:
    raw = private_key(key_id).public_key().public_bytes_raw()
    return base64.b64encode(raw).decode("ascii")


def sign_checkpoint(
    key_id: str,
    organization_id: uuid.UUID,
    chain: CheckpointChain,
    covered_sequence: int,
    covered_hash: str,
    taken_at: str,
) -> str:
    """La firma Ed25519 (base64) de la clave ``key_id`` sobre el mensaje de U-02 (BR-NUC-53)."""
    payload = signed_payload(organization_id, chain, covered_sequence, covered_hash, taken_at)
    signature = private_key(key_id).sign(canonical_bytes_sync(payload))
    return base64.b64encode(signature).decode("ascii")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _actor_columns(actor: Any) -> dict[str, Any]:
    return {
        "actor_kind": actor.kind,
        "actor_id": actor.id,
        "actor_display_name_snapshot": actor.display_name_snapshot,
        "actor_role_in_use": actor.role_in_use,
        "actor_concession_id": actor.concession_id,
        "actor_unit": actor.unit,
    }


def _seal_records(records: list[LedgerRecordView]) -> list[LedgerRecordView]:
    sealed: list[LedgerRecordView] = []
    previous: dict[tuple[uuid.UUID, uuid.UUID | None], str] = {}
    for record in sorted(records, key=lambda item: (str(item.plant_id), item.chain_sequence)):
        chain = (record.organization_id, record.plant_id)
        before = previous.get(chain) or genesis_hash(
            str(record.organization_id), None if record.plant_id is None else str(record.plant_id)
        )
        content = canonical_bytes_sync(record.document())
        content_hash = _sha256(content)
        row = {
            **_actor_columns(record.actor),
            "chain_sequence": record.chain_sequence,
            "content_hash": content_hash,
            "correlation_id": record.correlation_id,
            "organization_id": record.organization_id,
            "plant_id": record.plant_id,
            "received_at": record.received_at,
            "record_id": record.record_id,
            "record_type": record.record_type,
            "schema_version": record.schema_version,
            "scope_node_id": record.scope_node_id,
            "scope_plant_id": record.scope_plant_id,
            "scope_zone_id": record.scope_zone_id,
        }
        record_hash = _sha256(envelope_canonical(row) + before.encode("ascii"))
        previous[chain] = record_hash
        sealed.append(
            dataclasses.replace(
                record,
                content=content,
                content_hash=content_hash,
                previous_hash=before,
                record_hash=record_hash,
            )
        )
    return sealed


def _seal_audit(
    audit: list[AuditEntryView], organization_of: Mapping[uuid.UUID | None, uuid.UUID]
) -> list[AuditEntryView]:
    sealed: list[AuditEntryView] = []
    previous: dict[uuid.UUID, str] = {}
    for entry in sorted(
        audit, key=lambda item: (str(organization_of[item.scope_plant_id]), item.chain_sequence)
    ):
        organization = organization_of[entry.scope_plant_id]
        before = previous.get(organization) or genesis_hash(str(organization), None)
        row = {
            **_actor_columns(entry.actor),
            "chain_sequence": entry.chain_sequence,
            "correlation_id": entry.correlation_id,
            "entry_id": entry.entry_id,
            "filters_hash": (
                None if entry.filters is None else _sha256(canonical_bytes_sync(entry.filters))
            ),
            "occurred_at": entry.occurred_at,
            "operation": entry.operation,
            "organization_id": organization,
            "outcome": entry.outcome,
            "resource_kind": entry.resource_kind,
            "resource_id": entry.resource_id,
            "result_count": entry.result_count,
            "scope_plant_id": entry.scope_plant_id,
            "scope_zone_id": entry.scope_zone_id,
        }
        entry_hash = _sha256(audit_envelope_canonical(row) + before.encode("ascii"))
        previous[organization] = entry_hash
        sealed.append(dataclasses.replace(entry, previous_hash=before, entry_hash=entry_hash))
    return sealed


def _head(
    items: list[tuple[int, str]], fallback_sequence: int, fallback_hash: str
) -> tuple[int, str]:
    return max(items) if items else (fallback_sequence, fallback_hash)


def seal_world(world: ExportWorld) -> ExportWorld:
    """``world`` con hashes reales y puntos de control firmados que cubren la cabeza real."""
    keys = tuple(
        dataclasses.replace(key, public_key=_public_text(key.key_id))
        for key in world.keys
        if key.key_id in _SEEDS
    )
    if [key.key_id for key in keys] != [key.key_id for key in world.keys]:
        raise ValueError("el mundo usa una clave sin semilla")
    active = next(key for key in keys if key.status is KeyStatus.ACTIVE)
    records = _seal_records(world.records)
    organization_of: dict[uuid.UUID | None, uuid.UUID] = {
        plant: organization for organization, plant in world.plants.items()
    }
    audit = _seal_audit(world.audit, organization_of)
    taken_at = stamp(world.job.generated_at)
    checkpoints: dict[uuid.UUID, list[StoredCheckpoint]] = {}
    for organization, stored in world.checkpoints.items():
        sealed: list[StoredCheckpoint] = []
        for item in stored:
            chain = item.chain
            if chain.kind is ChainKind.AUDIT:
                heads = [
                    (entry.chain_sequence, entry.entry_hash)
                    for entry in audit
                    if organization_of[entry.scope_plant_id] == organization
                ]
            else:
                heads = [
                    (record.chain_sequence, record.record_hash)
                    for record in records
                    if record.organization_id == organization and record.plant_id == chain.plant_id
                ]
            covered_sequence, covered_hash = _head(
                heads, item.content.covered_sequence, item.content.covered_hash
            )
            signature = sign_checkpoint(
                active.key_id, organization, chain, covered_sequence, covered_hash, taken_at
            )
            sealed.append(
                StoredCheckpoint(
                    chain=chain,
                    sequence=covered_sequence + 1,
                    entry_id=item.entry_id,
                    entry_hash=_sha256(item.entry_id.bytes + covered_hash.encode()),
                    content=CheckpointContent(
                        covered_sequence=covered_sequence,
                        covered_hash=covered_hash,
                        taken_at=taken_at,
                        key_id=active.key_id,
                        signature=signature,
                    ),
                )
            )
        checkpoints[organization] = sealed
    return dataclasses.replace(
        world, records=records, audit=audit, keys=keys, checkpoints=checkpoints
    )


def retired_public_key(world: ExportWorld) -> CheckpointPublicKey:
    return next(key for key in world.keys if key.status is KeyStatus.RETIRED)


# --- el paquete en disco ------------------------------------------------------------------------


def volume_paths(directory: Path) -> list[Path]:
    return sorted(directory.glob("vol-*.zip"))


def compose_to_directory(world: ExportWorld, directory: Path) -> Composed:
    """El paquete real de ``world`` (compositor de TASK-318) escrito en ``directory``."""
    composed = compose_world(world)
    job = world.job
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    directory.mkdir(parents=True, exist_ok=True)
    for volume in composed.package.stored.volumes:
        data = composed.store.latest(location.volume_key(volume.number))
        (directory / volume.name).write_bytes(data)
    return composed


def read_volume_entries(data: bytes) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(info, archive.read(info)) for info in archive.infolist()]


def rewrite_volume(data: bytes, replace: Mapping[str, bytes | None]) -> bytes:
    """El volumen con el contenido de ``replace`` en esas entradas (``None``: la quita); el
    resto, igual."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", allowZip64=False) as archive:
        for info, content in read_volume_entries(data):
            copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            copy.compress_type = info.compress_type
            copy.create_system = info.create_system
            copy.external_attr = info.external_attr
            new = replace.get(info.filename, content)
            if new is not None:
                archive.writestr(copy, new)
    return output.getvalue()


def flip_bit(data: bytes, bit: int) -> bytes:
    changed = bytearray(data)
    changed[bit // 8] ^= 1 << (bit % 8)
    return bytes(changed)


def leaves(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    """Cada valor de texto, entero o booleano con su puntero JSON."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from leaves(item, f"{pointer}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, f"{pointer}/{index}")
    elif isinstance(value, bool | int | str):
        yield pointer, value


def _one_bit(value: Any, position: int) -> Any:
    """El mismo valor con un bit cambiado (sin salir de su tipo JSON)."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value ^ 1
    text = str(value)
    if not text:
        return "a"
    index = position % len(text)
    return text[:index] + chr(ord(text[index]) ^ 1) + text[index + 1 :]


def _set(document: Any, pointer: str, value: Any) -> None:
    parts = pointer.strip("/").split("/")
    target = document
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    last = parts[-1]
    if isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value


@dataclass(frozen=True)
class Mutation:
    """Una alteración del paquete: qué se toca, dónde y qué debe responder el verificador."""

    kind: str
    """``artifact_bit``, ``manifest_bit`` o ``record_bit``."""
    path: str
    bit: int = 0
    pointer: str = ""
    """En ``record_bit``: el valor alterado (``/findings/i/records/j/...`` o ``/entries/k/...``)."""
    record: str = ""
    """En ``record_bit``: el puntero del registro o de la entrada que contiene el valor."""
    signature: str = "keep"
    """En ``record_bit``, qué hace el atacante con la firma del manifiesto: ``keep`` (la deja),
    ``drop`` (la quita con su declaración) o ``stranger`` (firma con una clave ajena bajo el
    ``key_id`` de la activa)."""


def _record_targets(composed: Composed) -> list[tuple[str, str, str]]:
    """(artefacto, puntero del registro, puntero del valor) que un tercero puede comprobar."""
    targets: list[tuple[str, str, str]] = []
    for path in sorted(composed.artifacts):
        if path.startswith("findings/"):
            document = composed.json(path)
            for index, finding in enumerate(document["findings"]):
                for position, record in enumerate(finding["records"]):
                    base = f"/findings/{index}/records/{position}"
                    if "content" not in record:
                        continue
                    targets.append((path, base, f"{base}/content_hash"))
                    for pointer, _ in leaves(record["content"], f"{base}/content"):
                        targets.append((path, base, pointer))
        elif path == "audit/audit-log.json":
            for index, entry in enumerate(composed.json(path)["entries"]):
                base = f"/entries/{index}"
                for pointer, _ in leaves(entry, base):
                    targets.append((path, base, pointer))
    return targets


@st.composite
def package_mutations(draw: st.DrawFn, composed: Composed) -> Mutation:
    """Una alteración de un bit del paquete ``composed`` (PR-LAZ-28)."""
    kinds = ["artifact_bit", "manifest_bit"]
    targets = _record_targets(composed)
    if targets:
        kinds.append("record_bit")
    kind = draw(st.sampled_from(kinds))
    if kind == "artifact_bit":
        path = draw(st.sampled_from(sorted(p for p, data in composed.artifacts.items() if data)))
        size = len(composed.artifacts[path])
        return Mutation(kind, path, draw(st.integers(0, 8 * size - 1)))
    if kind == "manifest_bit":
        size = len(canonical_bytes_sync(composed.manifest))
        return Mutation(kind, MANIFEST_NAME, draw(st.integers(0, 8 * size - 1)))
    path, record, pointer = draw(st.sampled_from(targets))
    signature = draw(st.sampled_from(("keep", "drop", "stranger")))
    return Mutation(kind, path, draw(st.integers(0, 64)), pointer, record, signature)


def _audit_entry_hash(entry: Mapping[str, Any], organization_id: str) -> str:
    """El ``entry_hash`` que recalcula quien conoce el sobre de U-02 (el atacante también)."""
    filters = entry["filters"]
    envelope = {
        "actor": entry["actor"],
        "chain_sequence": entry["chain_sequence"],
        "correlation_id": entry["correlation_id"],
        "entry_id": entry["entry_id"],
        "filters_hash": None if filters is None else _sha256(canonicalize(filters)),
        "occurred_at": entry["occurred_at"],
        "operation": entry["operation"],
        "organization_id": organization_id,
        "outcome": entry["outcome"],
        "resource_ref": (
            None
            if entry["resource_kind"] is None and entry["resource_id"] is None
            else {"id": entry["resource_id"], "kind": entry["resource_kind"]}
        ),
        "result_count": entry["result_count"],
        "scope": {"plant_id": entry["scope_plant_id"], "zone_id": entry["scope_zone_id"]},
    }
    return _sha256(canonicalize(envelope) + str(entry["previous_hash"]).encode())


def _recompute(document: Any, mutation: Mutation, organization_id: str) -> None:
    """Falsificación coherente: rehace el hash propio del registro o de la entrada alterados.

    Si el bit alterado es el propio hash (``content_hash`` o ``entry_hash``) no se rehace: rehacerlo
    devolvería el valor original y el paquete quedaría idéntico (ronda 2, contraejemplo de
    ``/entries/0/entry_hash``).
    """
    if mutation.pointer.endswith(("/content_hash", "/entry_hash")):
        return
    target = document
    for part in mutation.record.strip("/").split("/"):
        target = target[int(part)] if isinstance(target, list) else target[part]
    try:
        if mutation.record.startswith("/entries/"):
            target["entry_hash"] = _audit_entry_hash(target, organization_id)
        else:
            target["content_hash"] = _sha256(canonical_bytes_sync(target["content"]))
    except (CanonicalizationError, CanonicalFormError, TypeError, ValueError):
        pass  # un valor sin forma canónica: el atacante no puede rehacer el hash


def _stranger_signature(manifest: Mapping[str, Any], data: bytes) -> bytes:
    stranger = Ed25519PrivateKey.from_private_bytes(bytes(range(200, 232)))
    payload = manifest_signature_payload(manifest, data)
    signature = base64.b64encode(stranger.sign(canonical_bytes_sync(payload))).decode("ascii")
    return canonical_bytes_sync({"payload": payload, "key_id": ACTIVE_KEY, "signature": signature})


def apply_mutation(composed: Composed, directory: Path, mutation: Mutation) -> None:
    """Escribe en ``directory`` el paquete alterado por ``mutation``."""
    manifest_bytes = canonical_bytes_sync(composed.manifest)
    changes: dict[str, bytes | None] = {}
    if mutation.kind == "artifact_bit":
        changes[mutation.path] = flip_bit(composed.artifacts[mutation.path], mutation.bit)
    elif mutation.kind == "manifest_bit":
        changes[MANIFEST_NAME] = flip_bit(manifest_bytes, mutation.bit)
    else:
        document = composed.json(mutation.path)
        current = document
        for part in mutation.pointer.strip("/").split("/"):
            current = current[int(part)] if isinstance(current, list) else current[part]
        _set(document, mutation.pointer, _one_bit(current, mutation.bit))
        organization_id = composed.manifest["organization"]["organization_id"]
        _recompute(document, mutation, organization_id)
        data = canonical_bytes_sync(document)
        changes[mutation.path] = data
        # Quien altera el paquete re-hashea el manifiesto: los hashes previos ya no lo delatan.
        manifest = json.loads(manifest_bytes)
        for artifact in manifest["artifacts"]:
            if artifact["path"] == mutation.path:
                artifact["sha256"] = _sha256(data)
                artifact["size_bytes"] = len(data)
        if mutation.signature == "drop":
            manifest.pop("manifest_signature", None)
            changes[MANIFEST_SIGNATURE_NAME] = None
        new_manifest = canonical_bytes_sync(manifest)
        changes[MANIFEST_NAME] = new_manifest
        if mutation.signature == "stranger":
            changes[MANIFEST_SIGNATURE_NAME] = _stranger_signature(manifest, new_manifest)
    for volume in volume_paths(directory):
        data = volume.read_bytes()
        names = {info.filename for info, _ in read_volume_entries(data)}
        mine = {name: content for name, content in changes.items() if name in names}
        if mine:
            volume.write_bytes(rewrite_volume(data, mine))
