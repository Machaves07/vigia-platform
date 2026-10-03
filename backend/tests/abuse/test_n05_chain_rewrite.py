"""N-5 · Reescritura de la cadena por quien controla la base (R6; business-rules §14).

**Qué intenta**: quien controla la base altera un registro y **recalcula** toda la cadena desde
ahí (``content_hash``, ``record_hash`` de cada sucesor y la cabeza), para que los enlaces vuelvan a
cuadrar.

**Qué lo detiene** (BR-NUC-53 a BR-NUC-57):

- BR-NUC-53: puntos de control firmados con la clave ``checkpoint`` que cubren el último registro
  (``covered_sequence``, ``covered_hash``): quien no tiene la clave privada no los rehace;
- BR-NUC-54 y BR-NUC-57: el cliente conserva sus paquetes y el verificador sin red acepta el punto
  de control de un paquete anterior para comprobar que el prefijo no cambió;
- BR-NUC-55: la clave pública ``checkpoint`` se publica sin autenticación y ninguna se retira
  jamás de la publicación (tampoco tras rotar);
- BR-NUC-56: la verificación en la plataforma comprueba cada enlace **y** cada punto de control.

Incluye el seguimiento 4 de la revisión de VIG-86: la rotación y su auditoría van en la misma
transacción (VIG-88, ``LedgerRotationRecorder``); si la auditoría falla, la clave no queda.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from vigia_contracts.canonical import canonicalize

from tests.ledger_database import chain_hash, record_envelope
from tests.platform_support import HOUR, T0, Platform, code_of
from tests.verifier_packages import PackageChain, row_to_entry, write_package
from tests.verify_support import mutate
from tests.writer_support import unit_context
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.ledger.chain.checkpoints import CheckpointChain
from vigia_platform.ledger.chain.package_verifier import verify_package
from vigia_platform.ledger.chain.verify import VerificationMode
from vigia_platform.shared.context import ActorKind, ActorUnit, Role

pytestmark = pytest.mark.integration

PACKAGE_KEYS = (
    "record_id", "organization_id", "plant_id", "chain_sequence", "record_type",
    "schema_version", "actor", "scope", "correlation_id", "received_at", "content",
    "content_hash", "previous_hash", "record_hash",
)  # fmt: skip


def _rows(platform: Platform, organization_id: uuid.UUID, plant_id: uuid.UUID) -> list[Any]:
    return platform.fetch(
        "SELECT * FROM ledger.ledger_record WHERE organization_id = $1 AND plant_id = $2"
        " ORDER BY chain_sequence",
        organization_id,
        plant_id,
    )


def _rewrite(
    platform: Platform,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    sequence: int,
    *,
    forge_checkpoint: bool,
) -> None:
    """El ataque: cambia el registro ``sequence`` y recalcula la cadena desde él hasta la cabeza.

    Con ``forge_checkpoint``, además reescribe el ``covered_hash`` de los puntos de control
    posteriores para que «cuadren» con la cadena nueva (sin la clave privada no puede firmarlos).
    """
    migrated = platform.authz.sessions.migrated
    previous: str | None = None
    rewritten: dict[int, str] = {}
    for row in _rows(platform, organization_id, plant_id):
        current = dict(row)
        number = current["chain_sequence"]
        if number < sequence:
            previous = current["record_hash"]
            rewritten[number] = current["record_hash"]
            continue
        content = json.loads(bytes(current["content"]))
        if number == sequence:
            content["resulting_mode"] = "commissioning"
        if forge_checkpoint and current["record_type"] == "checkpoint":
            content["covered_hash"] = rewritten[content["covered_sequence"]]
        current["content"] = canonicalize(content)
        current["content_hash"] = hashlib.sha256(current["content"]).hexdigest()
        assert previous is not None
        current["previous_hash"] = previous
        current["record_hash"] = chain_hash(record_envelope(current), previous)
        platform.run(
            mutate(
                migrated,
                "ledger.ledger_record",
                "record_id",
                current["record_id"],
                {
                    name: current[name]
                    for name in ("content", "content_hash", "previous_hash", "record_hash")
                },
            )
        )
        previous = current["record_hash"]
        rewritten[number] = previous
    platform.execute(
        "UPDATE ledger.chain_head SET last_hash = $1 WHERE organization_id = $2"
        " AND kind = 'ledger' AND plant_id = $3",
        previous,
        organization_id,
        plant_id,
    )


def _links_hold(rows: list[Any], organization_id: uuid.UUID, plant_id: uuid.UUID) -> bool:
    """Lo que vería quien solo comprueba los enlaces: con la reescritura, todo cuadra."""
    from tests.ledger_database import genesis_hash

    previous = genesis_hash(organization_id, plant_id)
    for row in rows:
        if hashlib.sha256(bytes(row["content"])).hexdigest() != row["content_hash"]:
            return False
        if row["previous_hash"] != previous:
            return False
        if chain_hash(record_envelope(row), previous) != row["record_hash"]:
            return False
        previous = row["record_hash"]
    return True


@pytest.mark.parametrize("forge_checkpoint", [False, True], ids=["enlaces", "y-punto-de-control"])
def test_n05_a_recomputed_chain_fails_against_the_signed_checkpoint(
    platform: Platform, forge_checkpoint: bool
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    organization_id = site.organization_id
    for i in range(3):
        platform.gate(site, plant_id, zone_id, T0 + i * HOUR)
    checkpoint = platform.checkpoint(organization_id, plant_id)
    platform.gate(site, plant_id, zone_id, T0 + 3 * HOUR)
    _rewrite(platform, organization_id, plant_id, 2, forge_checkpoint=forge_checkpoint)
    rows = _rows(platform, organization_id, plant_id)
    assert _links_hold(rows, organization_id, plant_id)  # el ataque es «perfecto» en los enlaces
    context = unit_context(organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)
    result = platform.run(
        platform.verifier.verify(context, CheckpointChain.plant(plant_id), VerificationMode.FULL)
    )
    assert not result.intact, result
    # El punto de control firmado (secuencia 4) ya no cuadra con lo que dice cubrir.
    assert result.broken_sequence == checkpoint_sequence(rows, checkpoint.entry_id)
    assert platform.events(organization_id, "integrity_compromised")


def checkpoint_sequence(rows: list[Any], entry_id: uuid.UUID) -> int:
    (row,) = [row for row in rows if row["record_id"] == entry_id]
    sequence: int = row["chain_sequence"]
    return sequence


def test_n05_the_clients_previous_package_detects_the_rewritten_prefix(
    platform: Platform, tmp_path: Path
) -> None:
    site = platform.site()
    ((plant_id, zone_id),) = site.zones()
    organization_id = site.organization_id
    for i in range(3):
        platform.gate(site, plant_id, zone_id, T0 + i * HOUR)
    anchor = platform.checkpoint(organization_id, plant_id)
    # El cliente guarda su paquete del mes: el punto de control (su ancla) tal como lo leyó.
    _, coordinator = platform.person(organization_id, Role.COORDINATOR_SST)
    record = platform.call("GET", f"/ledger/records/{anchor.entry_id}", cookie=coordinator).json()
    previous = tmp_path / "anterior.json"
    previous.write_text(json.dumps({k: record[k] for k in PACKAGE_KEYS}), encoding="utf-8")
    # Más registros, un punto de control nuevo… y la reescritura, que rehace hasta los puntos de
    # control posteriores con hashes nuevos (sin poder firmarlos).
    platform.gate(site, plant_id, zone_id, T0 + 3 * HOUR)
    _rewrite(platform, organization_id, plant_id, 2, forge_checkpoint=True)
    rows = _rows(platform, organization_id, plant_id)
    (head,) = platform.fetch(
        "SELECT * FROM ledger.chain_head WHERE organization_id = $1 AND plant_id = $2",
        organization_id,
        plant_id,
    )
    keys = platform.call("GET", "/.well-known/vigia-checkpoint-keys").json()["keys"]
    chain = PackageChain(
        "ledger",
        str(plant_id),
        "chains/ledger-plant.jsonl",
        [row_to_entry("ledger", row) for row in rows],
        1,
        head["last_sequence"],
        head["last_hash"],
    )
    manifest = {
        "format": "vigia-package",
        "format_version": 1,
        "organization_id": str(organization_id),
        "chains": [chain.manifest()],
        "checkpoint_keys": [{"key_id": k["key_id"], "public_key": k["public_key"]} for k in keys],
    }
    package = tmp_path / "paquete"
    package.mkdir()
    write_package(package, organization_id, [chain], [], manifest=manifest)
    # Sin red, sin acceso al sistema y sin secretos del proveedor: la reescritura no pasa.
    report = verify_package(package, [previous])
    assert not report.intact, report


def test_n05_checkpoint_keys_are_never_withdrawn_after_rotation(platform: Platform) -> None:
    def published() -> dict[str, str]:
        response = platform.call("GET", "/.well-known/vigia-checkpoint-keys")
        assert response.status_code == 200, response.text
        return {key["key_id"]: key["public_key"] for key in response.json()["keys"]}

    before = published()
    operator = platform.operator()
    for _ in range(2):
        rotated = platform.call("POST", "/platform/keys/checkpoint/rotate", cookie=operator)
        assert rotated.status_code == 200, rotated.text
    after = published()
    # Las claves anteriores siguen publicadas, con el mismo material público (BR-NUC-55).
    assert before.items() <= after.items()
    assert len(after) == len(before) + 2
    assert all(len(base64.b64decode(value)) == 32 for value in after.values())


def test_n05_a_rotation_never_stays_without_its_audit_entry(
    platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = platform.authz.sessions.audit
    original = audit.append

    async def failing(context: Any, operation: Any, *args: Any, **kwargs: Any) -> Any:
        if operation == AuditOperation.KEY_ROTATED:
            raise ConnectionError("la base se cae entre la rotación y su auditoría (sintético)")
        return await original(context, operation, *args, **kwargs)

    active = {key.key_id for key in platform.signing.public_keys(_checkpoint_purpose())}
    audited = len(platform.audit_entries(platform.provider, "key_rotated"))
    monkeypatch.setattr(audit, "append", failing)
    response = platform.call("POST", "/platform/keys/checkpoint/rotate", cookie=platform.operator())
    monkeypatch.undo()
    assert response.status_code >= 500 and code_of(response) == "internal_error"
    # Lo persistido, no la memoria del proceso: otro proceso que relea no debe ver la clave.
    assert platform.run(platform.signing.refresh())
    rotated = {key.key_id for key in platform.signing.public_keys(_checkpoint_purpose())} - active
    unaudited = len(platform.audit_entries(platform.provider, "key_rotated")) == audited
    # El invariante que se espera: o la rotación no ocurre, o queda auditada.
    assert not (rotated and unaudited), f"rotación sin auditar: {sorted(rotated)}"
    # Y una rotación que sí ocurre deja exactamente una entrada (la de su transacción).
    response = platform.call("POST", "/platform/keys/checkpoint/rotate", cookie=platform.operator())
    assert response.status_code == 200, response.text
    assert len(platform.audit_entries(platform.provider, "key_rotated")) == audited + 1


def _checkpoint_purpose() -> Any:
    from vigia_platform.shared.signing.keys import SigningPurpose

    return SigningPurpose.CHECKPOINT
