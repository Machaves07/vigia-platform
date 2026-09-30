"""Recorrido de una cadena: enlaces, hashes y puntos de control (LC-NUC-18, BR-NUC-46, 56, 57).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10. Lo comparten el
verificador de paquetes (``tools/vigia_verify.py``, generado por ``tools/build_verifier.py``) y el
paso 2 del motor de verificación de la plataforma (``ledger.chain.verify``, TASK-118), de modo que
plataforma y verificador aplican el mismo algoritmo (PAT-NUC-MAN-08).

Las entradas son registros del expediente o entradas de auditoría en la **forma del paquete**
(``docs/package-format.md``): diccionarios JSON con los valores textuales exactos de la base
(UUID en minúsculas, marcas ``AAAA-MM-DDTHH:MM:SS.mmmZ``) y el contenido como documento JSON.

Por cada entrada, en este orden (business-logic-model §5; el primer fallo fija el motivo):

1. forma: claves exactas y tipos (``malformed``);
2. secuencia: la anterior más uno (``sequence_gap``);
3. cadena: organización y planta de la cadena (``wrong_chain``);
4. enlace: ``previous_hash`` igual al hash del registro anterior (``previous_hash_mismatch``);
5. contenido: ``content_hash = SHA-256(RFC 8785(content))`` (``content_not_canonical``,
   ``content_hash_mismatch``); en auditoría, ``filters`` y ``filters_hash`` (nulos a la vez);
6. registro: ``record_hash = SHA-256(RFC 8785(sobre) ‖ previous_hash)`` (``record_hash_mismatch``);
7. ancla: si un punto de control de un paquete anterior cubre esta secuencia, el hash coincide
   (``previous_checkpoint_mismatch``): si difiere, alguien reescribió el prefijo;
8. punto de control (``record_type = checkpoint`` o ``operation = checkpoint``): forma
   (``checkpoint_malformed``), cobertura exacta del registro anterior (``checkpoint_coverage``),
   clave conocida (``unknown_key``) y firma Ed25519 sobre el canónico de
   ``{covered_hash, covered_sequence, kind, organization_id, plant_id, taken_at}``
   (``bad_signature``).

La secuencia rota es siempre la primera esperada que falla (la anterior más uno) y se nombra
además el identificador de la entrada encontrada. Al terminar, la cabeza declarada (última
secuencia y último hash) debe coincidir con la recalculada (``head_mismatch``) y toda ancla debe
haberse comprobado (``previous_checkpoint_missing``: el paquete no llega a esa secuencia).

Sin E/S ni hora del sistema.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from vigia_platform.ledger.chain.pure_ed25519 import ed25519_verify
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError, canonicalize

__all__ = [
    "CHAIN_KINDS",
    "Break",
    "ChainRef",
    "ChainResult",
    "ChainWalker",
    "CheckpointSeen",
    "checkpoint_message",
    "genesis_hash",
]

CHAIN_KINDS: Final = ("ledger", "audit")
"""``ledger``: expediente (por organización y planta, o de organización); ``audit``: auditoría."""

CHECKPOINT: Final = "checkpoint"

_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_SIGNATURE: Final = re.compile(r"[A-Za-z0-9+/]{86}==")

_ACTOR_KEYS: Final = frozenset(
    {"concession_id", "display_name_snapshot", "id", "kind", "role_in_use", "unit"}
)
_RECORD_SCOPE_KEYS: Final = frozenset({"node_id", "plant_id", "zone_id"})
_AUDIT_SCOPE_KEYS: Final = frozenset({"plant_id", "zone_id"})
_RESOURCE_KEYS: Final = frozenset({"id", "kind"})
_CHECKPOINT_KEYS: Final = frozenset(
    {"covered_hash", "covered_sequence", "key_id", "signature", "taken_at"}
)
_RECORD_KEYS: Final = frozenset(
    {
        "actor",
        "chain_sequence",
        "content",
        "content_hash",
        "correlation_id",
        "organization_id",
        "plant_id",
        "previous_hash",
        "received_at",
        "record_hash",
        "record_id",
        "record_type",
        "schema_version",
        "scope",
    }
)
_AUDIT_KEYS: Final = frozenset(
    {
        "actor",
        "chain_sequence",
        "correlation_id",
        "entry_hash",
        "entry_id",
        "filters",
        "filters_hash",
        "occurred_at",
        "operation",
        "organization_id",
        "outcome",
        "previous_hash",
        "resource_ref",
        "result_count",
        "scope",
    }
)

MESSAGES: Final = {
    "malformed": "la entrada no tiene la forma de un registro de la cadena",
    "sequence_gap": "la secuencia no sigue a la anterior (falta, sobra o se repite un registro)",
    "wrong_chain": "la entrada es de otra organización o planta",
    "previous_hash_mismatch": "el enlace con el registro anterior no coincide",
    "content_not_canonical": "el contenido no tiene forma canónica RFC 8785",
    "content_hash_mismatch": "el hash del contenido no coincide",
    "record_hash_mismatch": "el hash del registro no coincide",
    "previous_checkpoint_mismatch": (
        "el registro no coincide con el punto de control del paquete anterior: "
        "el prefijo de la cadena cambió"
    ),
    "checkpoint_malformed": "el punto de control no tiene la forma esperada",
    "checkpoint_coverage": "el punto de control no cubre exactamente el registro anterior",
    "unknown_key": "la clave del punto de control no está entre las claves públicas del paquete",
    "bad_signature": "la firma del punto de control no verifica",
    "head_mismatch": "la cabeza declarada de la cadena no coincide con la recalculada",
    "previous_checkpoint_missing": (
        "el paquete no contiene la secuencia del punto de control anterior: "
        "no se puede comprobar el prefijo"
    ),
}
"""Motivo de rotura → explicación en español."""


def _fullmatch(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional(value: object, check: re.Pattern[str]) -> bool:
    return value is None or _fullmatch(check, value)


def _optional_text(value: object) -> bool:
    return value is None or isinstance(value, str)


def genesis_hash(organization_id: str, plant_id: str | None) -> str:
    """``SHA-256("vigia:genesis:" + organization_id + ":" + (plant_id | "organization"))``."""
    tail = "organization" if plant_id is None else plant_id
    return hashlib.sha256(f"vigia:genesis:{organization_id}:{tail}".encode()).hexdigest()


def checkpoint_message(
    kind: str,
    organization_id: str,
    plant_id: str | None,
    covered_sequence: int,
    covered_hash: str,
    taken_at: str,
) -> bytes:
    """Bytes que firma la clave ``checkpoint`` (domain-entities §3.4, BR-NUC-53)."""
    return canonicalize(
        {
            "covered_hash": covered_hash,
            "covered_sequence": covered_sequence,
            "kind": kind,
            "organization_id": organization_id,
            "plant_id": plant_id,
            "taken_at": taken_at,
        }
    )


@dataclass(frozen=True)
class ChainRef:
    """Una cadena: expediente de planta, expediente de organización o auditoría."""

    kind: str
    organization_id: str
    plant_id: str | None

    def describe(self) -> str:
        """Nombre de la cadena en español."""
        if self.kind == "audit":
            return f"auditoría de la organización {self.organization_id}"
        if self.plant_id is None:
            return f"expediente de la organización {self.organization_id}"
        return f"expediente de la planta {self.plant_id}"


@dataclass(frozen=True)
class Break:
    """Primer fallo de una cadena: secuencia esperada, entrada encontrada y motivo."""

    sequence: int | None
    entry_id: str | None
    reason: str
    detail: str = ""

    def message(self) -> str:
        """Explicación en español."""
        text = MESSAGES.get(self.reason, self.reason)
        return f"{text} ({self.detail})" if self.detail else text


@dataclass(frozen=True)
class CheckpointSeen:
    """Un punto de control verificado dentro de la cadena."""

    sequence: int
    entry_id: str
    entry_hash: str
    covered_sequence: int
    covered_hash: str
    key_id: str
    taken_at: str


@dataclass(frozen=True)
class ChainResult:
    """Resultado del recorrido de una cadena."""

    chain: ChainRef
    status: str
    first_sequence: int | None
    last_sequence: int
    last_hash: str
    entries: int
    checkpoints: tuple[CheckpointSeen, ...]
    anchors_matched: tuple[int, ...]
    broken: Break | None

    @property
    def intact(self) -> bool:
        return self.status == "intact"


def _entry_id(chain: ChainRef, entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    value = entry.get("entry_id" if chain.kind == "audit" else "record_id")
    return value if _fullmatch(_UUID, value) else None


def _actor_ok(actor: object) -> bool:
    return (
        isinstance(actor, dict)
        and actor.keys() == _ACTOR_KEYS
        and _optional(actor["concession_id"], _UUID)
        and isinstance(actor["display_name_snapshot"], str)
        and _fullmatch(_UUID, actor["id"])
        and isinstance(actor["kind"], str)
        and _optional_text(actor["role_in_use"])
        and isinstance(actor["unit"], str)
    )


def _record_ok(entry: dict[str, object]) -> bool:
    if entry.keys() != _RECORD_KEYS:
        return False
    scope = entry["scope"]
    return (
        _fullmatch(_UUID, entry["record_id"])
        and _fullmatch(_UUID, entry["organization_id"])
        and _optional(entry["plant_id"], _UUID)
        and _is_integer(entry["chain_sequence"])
        and isinstance(entry["record_type"], str)
        and _is_integer(entry["schema_version"])
        and _actor_ok(entry["actor"])
        and isinstance(scope, dict)
        and scope.keys() == _RECORD_SCOPE_KEYS
        and all(_optional(value, _UUID) for value in scope.values())
        and _fullmatch(_UUID, entry["correlation_id"])
        and _fullmatch(_TIMESTAMP, entry["received_at"])
        and _fullmatch(_HEX64, entry["content_hash"])
        and _fullmatch(_HEX64, entry["previous_hash"])
        and _fullmatch(_HEX64, entry["record_hash"])
    )


def _audit_ok(entry: dict[str, object]) -> bool:
    if entry.keys() != _AUDIT_KEYS:
        return False
    scope = entry["scope"]
    resource = entry["resource_ref"]
    return (
        _fullmatch(_UUID, entry["entry_id"])
        and _fullmatch(_UUID, entry["organization_id"])
        and _is_integer(entry["chain_sequence"])
        and _actor_ok(entry["actor"])
        and isinstance(entry["operation"], str)
        and isinstance(scope, dict)
        and scope.keys() == _AUDIT_SCOPE_KEYS
        and all(_optional(value, _UUID) for value in scope.values())
        and (
            resource is None
            or (
                isinstance(resource, dict)
                and resource.keys() == _RESOURCE_KEYS
                and _optional(resource["id"], _UUID)
                and _optional_text(resource["kind"])
            )
        )
        and _optional(entry["filters_hash"], _HEX64)
        and (entry["result_count"] is None or _is_integer(entry["result_count"]))
        and isinstance(entry["outcome"], str)
        and _fullmatch(_UUID, entry["correlation_id"])
        and _fullmatch(_TIMESTAMP, entry["occurred_at"])
        and _fullmatch(_HEX64, entry["previous_hash"])
        and _fullmatch(_HEX64, entry["entry_hash"])
    )


def _record_envelope(entry: dict[str, object]) -> dict[str, object]:
    return {
        "actor": entry["actor"],
        "chain_sequence": entry["chain_sequence"],
        "content_hash": entry["content_hash"],
        "correlation_id": entry["correlation_id"],
        "organization_id": entry["organization_id"],
        "plant_id": entry["plant_id"],
        "received_at": entry["received_at"],
        "record_id": entry["record_id"],
        "record_type": entry["record_type"],
        "schema_version": entry["schema_version"],
        "scope": entry["scope"],
    }


def _audit_envelope(entry: dict[str, object]) -> dict[str, object]:
    return {
        "actor": entry["actor"],
        "chain_sequence": entry["chain_sequence"],
        "correlation_id": entry["correlation_id"],
        "entry_id": entry["entry_id"],
        "filters_hash": entry["filters_hash"],
        "occurred_at": entry["occurred_at"],
        "operation": entry["operation"],
        "organization_id": entry["organization_id"],
        "outcome": entry["outcome"],
        "resource_ref": entry["resource_ref"],
        "result_count": entry["result_count"],
        "scope": entry["scope"],
    }


def _sha256_canonical(value: object) -> str | None:
    """SHA-256 hexadecimal del canónico de ``value``, o ``None`` si no tiene forma canónica."""
    try:
        return hashlib.sha256(canonicalize(value)).hexdigest()
    except CanonicalizationError:
        return None


@dataclass
class ChainWalker:
    """Recorre una cadena entrada a entrada, en orden de secuencia.

    ``public_keys``: clave pública Ed25519 (32 bytes) por ``key_id`` de propósito ``checkpoint``.
    ``start_sequence`` y ``start_hash``: punto de partida; por omisión, la génesis (secuencia 0 y
    el hash de génesis de la cadena). ``anchors``: ``record_hash`` esperado por secuencia, tomado
    de puntos de control de paquetes anteriores (BR-NUC-57).
    """

    chain: ChainRef
    public_keys: Mapping[str, bytes]
    start_sequence: int = 0
    start_hash: str | None = None
    anchors: Mapping[int, str] = field(default_factory=dict)
    _last_sequence: int = field(init=False)
    _last_hash: str = field(init=False)
    _entries: int = field(init=False, default=0)
    _checkpoints: list[CheckpointSeen] = field(init=False, default_factory=list)
    _anchors_matched: list[int] = field(init=False, default_factory=list)
    _last_entry_id: str | None = field(init=False, default=None)
    _broken: Break | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.chain.kind not in CHAIN_KINDS:
            raise ValueError(f"tipo de cadena desconocido: {self.chain.kind!r}")
        self._last_sequence = self.start_sequence
        self._last_hash = (
            self.start_hash
            if self.start_hash is not None
            else genesis_hash(self.chain.organization_id, self.chain.plant_id)
        )
        expected = self.anchors.get(self.start_sequence)
        if expected is not None:
            if expected != self._last_hash:
                self._broken = Break(self.start_sequence + 1, None, "previous_checkpoint_mismatch")
            else:
                self._anchors_matched.append(self.start_sequence)

    @property
    def broken(self) -> Break | None:
        return self._broken

    def feed(self, entry: object) -> Break | None:
        """Comprueba la entrada siguiente; devuelve el fallo si la cadena se rompe aquí.

        Tras el primer fallo la cadena queda rota y las entradas siguientes se ignoran.
        """
        if self._broken is not None:
            return self._broken
        self._broken = self._check(entry)
        return self._broken

    def _fail(self, entry: object, reason: str, detail: str = "") -> Break:
        return Break(self._last_sequence + 1, _entry_id(self.chain, entry), reason, detail)

    def _check(self, entry: object) -> Break | None:
        audit = self.chain.kind == "audit"
        if not isinstance(entry, dict):
            return self._fail(entry, "malformed")
        hash_key = "entry_hash" if audit else "record_hash"
        sequence, entry_hash = entry.get("chain_sequence"), entry.get(hash_key)
        well_formed = _audit_ok(entry) if audit else _record_ok(entry)
        if not (well_formed and isinstance(sequence, int) and isinstance(entry_hash, str)):
            return self._fail(entry, "malformed")
        if sequence != self._last_sequence + 1:
            return self._fail(entry, "sequence_gap", f"se encontró la secuencia {sequence}")
        if entry["organization_id"] != self.chain.organization_id or (
            not audit and entry["plant_id"] != self.chain.plant_id
        ):
            return self._fail(entry, "wrong_chain")
        if entry["previous_hash"] != self._last_hash:
            return self._fail(entry, "previous_hash_mismatch")

        if audit:
            content, declared = entry["filters"], entry["filters_hash"]
        else:
            content, declared = entry["content"], entry["content_hash"]
        if audit and content is None:
            if declared is not None:
                return self._fail(entry, "content_hash_mismatch")
        else:
            computed = _sha256_canonical(content)
            if computed is None:
                return self._fail(entry, "content_not_canonical")
            if computed != declared:
                return self._fail(entry, "content_hash_mismatch")

        envelope = _audit_envelope(entry) if audit else _record_envelope(entry)
        try:
            envelope_bytes = canonicalize(envelope)
        except CanonicalizationError:
            return self._fail(entry, "malformed")
        recomputed = hashlib.sha256(envelope_bytes + self._last_hash.encode("ascii")).hexdigest()
        if recomputed != entry_hash:
            return self._fail(entry, "record_hash_mismatch")

        expected = self.anchors.get(sequence)
        if expected is not None:
            if expected != entry_hash:
                return self._fail(entry, "previous_checkpoint_mismatch")
            self._anchors_matched.append(sequence)

        kind_field = entry["operation"] if audit else entry["record_type"]
        if kind_field == CHECKPOINT:
            failure = self._check_checkpoint(entry, content, sequence, entry_hash)
            if failure is not None:
                return failure

        self._last_sequence = sequence
        self._last_hash = entry_hash
        self._last_entry_id = _entry_id(self.chain, entry)
        self._entries += 1
        return None

    def _check_checkpoint(
        self, entry: dict[str, object], content: object, sequence: int, entry_hash: str
    ) -> Break | None:
        if not isinstance(content, dict) or content.keys() != _CHECKPOINT_KEYS:
            return self._fail(entry, "checkpoint_malformed")
        covered_sequence = content["covered_sequence"]
        covered_hash = content["covered_hash"]
        taken_at = content["taken_at"]
        key_id = content["key_id"]
        signature_text = content["signature"]
        if not (
            _is_integer(covered_sequence)
            and isinstance(covered_sequence, int)
            and isinstance(covered_hash, str)
            and _fullmatch(_HEX64, covered_hash)
            and isinstance(taken_at, str)
            and _fullmatch(_TIMESTAMP, taken_at)
            and isinstance(key_id, str)
            and isinstance(signature_text, str)
            and _fullmatch(_SIGNATURE, signature_text)
        ):
            return self._fail(entry, "checkpoint_malformed")
        if covered_sequence != sequence - 1 or covered_hash != self._last_hash:
            return self._fail(entry, "checkpoint_coverage")
        public_key = self.public_keys.get(key_id)
        if public_key is None:
            return self._fail(entry, "unknown_key", f"key_id {key_id!r}")
        try:
            signature = base64.b64decode(signature_text, validate=True)
        except (binascii.Error, ValueError):
            return self._fail(entry, "checkpoint_malformed")
        if base64.b64encode(signature).decode("ascii") != signature_text:
            # Base64 no canónico: los bits de relleno del último carácter no son cero. Otro texto
            # que decodifica a la misma firma no es la firma escrita (PR-NUC-21).
            return self._fail(entry, "checkpoint_malformed", "firma en base64 no canónico")
        message = checkpoint_message(
            self.chain.kind,
            self.chain.organization_id,
            self.chain.plant_id,
            covered_sequence,
            covered_hash,
            taken_at,
        )
        if not ed25519_verify(public_key, message, signature):
            return self._fail(entry, "bad_signature", f"key_id {key_id!r}")
        self._checkpoints.append(
            CheckpointSeen(
                sequence=sequence,
                entry_id=_entry_id(self.chain, entry) or "",
                entry_hash=entry_hash,
                covered_sequence=covered_sequence,
                covered_hash=covered_hash,
                key_id=key_id,
                taken_at=taken_at,
            )
        )
        return None

    def finish(self, declared_head: tuple[int, str] | None = None) -> ChainResult:
        """Cierra el recorrido; con ``declared_head``, exige que la cabeza coincida."""
        if self._broken is None and declared_head is not None:
            declared_sequence, declared_hash = declared_head
            if declared_sequence > self._last_sequence:
                self._broken = Break(
                    self._last_sequence + 1,
                    None,
                    "head_mismatch",
                    f"la cabeza declarada llega a la secuencia {declared_sequence}",
                )
            elif declared_sequence < self._last_sequence:
                self._broken = Break(
                    declared_sequence + 1,
                    None,
                    "head_mismatch",
                    f"la cabeza declarada termina en la secuencia {declared_sequence}",
                )
            elif declared_hash != self._last_hash:
                self._broken = Break(self._last_sequence, self._last_entry_id, "head_mismatch")
        if self._broken is None:
            missing = sorted(set(self.anchors) - set(self._anchors_matched))
            if missing:
                self._broken = Break(missing[0], None, "previous_checkpoint_missing")
        return ChainResult(
            chain=self.chain,
            status="intact" if self._broken is None else "broken",
            first_sequence=self.start_sequence + 1 if self._entries else None,
            last_sequence=self._last_sequence,
            last_hash=self._last_hash,
            entries=self._entries,
            checkpoints=tuple(self._checkpoints),
            anchors_matched=tuple(self._anchors_matched),
            broken=self._broken,
        )
