"""Puntos de control diarios firmados por cadena (LC-NUC-14; BR-NUC-53 a 55).

Un punto de control es el ancla que el cliente conserva: un registro ``checkpoint`` del expediente
(o una entrada de auditoría ``checkpoint``, con el contenido en ``filters``) que cubre al registro
anterior de su cadena. Su contenido, exactamente estas claves (``domain-entities.md`` §3.4)::

    {covered_sequence, covered_hash, taken_at, key_id, signature}

``covered_sequence`` y ``covered_hash`` son la secuencia y el hash (``record_hash`` o
``entry_hash``) de la cabeza de la cadena; ``taken_at`` la marca del reloj inyectado en UTC con
milisegundos; ``signature`` la firma Ed25519 (base64 estándar) de la clave ``checkpoint`` activa
sobre el canónico RFC 8785 de ``{covered_hash, covered_sequence, kind, organization_id, plant_id,
taken_at}`` (``kind`` ``ledger`` o ``audit``; ``plant_id`` nulo en la cadena de organización y en
la de auditoría). Es el mismo mensaje que comprueba ``chain_walk.checkpoint_message``, así que el
verificador de paquetes y el motor de la plataforma lo aceptan sin más (``docs/package-format.md``).

``CheckpointService`` implementa ``CheckpointPort`` (business-logic-model §10.1):

- ``write_checkpoints_now(context, chains)``: por cada cadena pedida (todas las de la organización
  si ``chains`` es ``None``) lee la cabeza, firma y anexa el punto de control, y publica
  ``checkpoint_written`` en la misma transacción. **Idempotente por cabeza**: si el último
  registro de la cadena ya es un punto de control, no escribe otro (``already_current``); una
  cadena sin registros no recibe ninguno (``empty_chain``: no hay nada que cubrir). Si otra
  escritura se adelanta entre la lectura de la cabeza y el ``INSERT``, el disparador lo rechaza
  (``CheckpointCoverageConflict``, restricción ``ledger_record_checkpoint_coverage`` de
  ``nuc_0005``) y se relee, se vuelve a firmar y se reintenta, hasta ``max_attempts``. Lo invoca
  la tarea diaria ``write_checkpoints`` (00:00 UTC, una invocación por organización) y U-04 antes
  de cada exportación o paquete mensual.
- ``latest_checkpoints(context, chains)``: el último punto de control de cada cadena, para
  incrustarlo en la exportación (BR-NUC-54).
- ``checkpoint_public_keys()``: todas las claves públicas de propósito ``checkpoint``, activas,
  en solapamiento **y retiradas**: ninguna se retira jamás de la publicación (BR-NUC-55). Nunca
  lleva la referencia al secreto.

El registro ``checkpoint`` es de U-02: el contexto con el que se escribe tiene que ser de un actor
de U-02. ``writer_context`` traduce el contexto del llamador (p. ej. el de U-04) al contexto de
sistema de U-02 de la misma organización; sin él, un contexto de otra unidad se rechaza con
``CheckpointContextRejected`` antes de tocar la base.

Módulo crítico aislado (NFR-NUC-25): no importa FastAPI ni SQLAlchemy. La base, el escritor del
expediente, la auditoría y la bandeja llegan por el puerto ``CheckpointStore``
(``ledger.adapters.checkpoint_store``); la firma por ``CheckpointSigner`` (``SigningService``). No
lee la hora del sistema.
"""

from __future__ import annotations

import base64
import binascii
import enum
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_platform.ledger.chain.chain_walk import checkpoint_message
from vigia_platform.ledger.chain.pure_ed25519 import ed25519_verify
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import (
    KeyStatus,
    PlatformSignedEnvelope,
    SigningKeyRecord,
    SigningPurpose,
    format_timestamp,
)

__all__ = [
    "CHECKPOINT",
    "DEFAULT_MAX_ATTEMPTS",
    "MAX_SIGNABLE_SEQUENCE",
    "ChainHeadState",
    "ChainKind",
    "CheckpointChain",
    "CheckpointContent",
    "CheckpointContextRejected",
    "CheckpointCoverageConflict",
    "CheckpointOutcome",
    "CheckpointPublicKey",
    "CheckpointResult",
    "CheckpointService",
    "CheckpointSigner",
    "CheckpointStore",
    "CheckpointWriteFailed",
    "StoredCheckpoint",
    "event_payload",
    "signed_payload",
    "verify_checkpoint",
]

CHECKPOINT: Final = "checkpoint"
"""``record_type`` del expediente y ``operation`` de la auditoría."""

DEFAULT_MAX_ATTEMPTS: Final = 3
"""Intentos por cadena ante ``CheckpointCoverageConflict`` ``[objetivo propio]``."""

_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_SIGNATURE: Final = re.compile(r"[A-Za-z0-9+/]{86}==")
_KEY_ID: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
_MAX_SEQUENCE: Final = 2**63 - 1
MAX_SIGNABLE_SEQUENCE: Final = 2**53 - 1
"""Mayor secuencia que puede cubrir un punto de control: el mensaje firmado es RFC 8785 (I-JSON)
y ningún entero mayor tiene forma canónica, ni en U-01 ni en el verificador de paquetes."""

_log = get_logger("ledger.chain.checkpoints")


# --- Errores ------------------------------------------------------------------------------------


class CheckpointCoverageConflict(Exception):
    """El disparador rechazó el punto de control: la cabeza avanzó mientras se firmaba."""

    def __init__(self) -> None:
        super().__init__("la cabeza de la cadena avanzó; el punto de control no la cubre")


class CheckpointContextRejected(Exception):
    """El contexto no es de un actor de U-02 y no hay ``writer_context`` que lo traduzca."""

    code: Final = "context_absent"

    def __init__(self) -> None:
        super().__init__("el punto de control solo lo escribe U-02")


class CheckpointWriteFailed(Exception):
    """Alguna cadena quedó sin su punto de control; ``results`` lleva las que sí se atendieron."""

    def __init__(
        self,
        results: tuple[CheckpointResult, ...],
        failures: tuple[tuple[CheckpointChain, BaseException], ...],
    ) -> None:
        chains = ", ".join(chain.describe() for chain, _ in failures)
        super().__init__(f"sin punto de control en: {chains}")
        self.results = results
        self.failures = failures


# --- Valores ------------------------------------------------------------------------------------


class ChainKind(enum.StrEnum):
    """``checkpoint_kind``: expediente o auditoría."""

    LEDGER = "ledger"
    AUDIT = "audit"


@dataclass(frozen=True, slots=True)
class CheckpointChain:
    """Una cadena de la organización del contexto: expediente de planta o de organización, o
    auditoría (``plant_id`` siempre nulo)."""

    kind: ChainKind
    plant_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ChainKind):
            raise TypeError("kind debe ser ChainKind")
        if self.plant_id is not None and type(self.plant_id) is not uuid.UUID:
            raise TypeError("plant_id debe ser uuid.UUID")
        if self.kind is ChainKind.AUDIT and self.plant_id is not None:
            raise ValueError("la cadena de auditoría es de organización: sin plant_id")

    @classmethod
    def plant(cls, plant_id: uuid.UUID) -> CheckpointChain:
        return cls(ChainKind.LEDGER, plant_id)

    @classmethod
    def organization(cls) -> CheckpointChain:
        return cls(ChainKind.LEDGER, None)

    @classmethod
    def audit(cls) -> CheckpointChain:
        return cls(ChainKind.AUDIT, None)

    def describe(self) -> str:
        if self.kind is ChainKind.AUDIT:
            return "auditoría"
        return "expediente de organización" if self.plant_id is None else "expediente de planta"

    def sort_key(self) -> tuple[str, str]:
        return (self.kind.value, "" if self.plant_id is None else str(self.plant_id))


@dataclass(frozen=True, slots=True)
class ChainHeadState:
    """``ChainHead`` de una cadena y si su último registro ya es un punto de control."""

    chain: CheckpointChain
    last_sequence: int
    last_hash: str
    last_is_checkpoint: bool


@dataclass(frozen=True, slots=True)
class CheckpointContent:
    """El contenido del punto de control (``domain-entities.md`` §3.4)."""

    covered_sequence: int
    covered_hash: str
    taken_at: str
    key_id: str
    signature: str

    def to_json(self) -> dict[str, Any]:
        return {
            "covered_sequence": self.covered_sequence,
            "covered_hash": self.covered_hash,
            "taken_at": self.taken_at,
            "key_id": self.key_id,
            "signature": self.signature,
        }

    @classmethod
    def from_json(cls, document: object) -> CheckpointContent:
        """Lectura estricta de un contenido persistido; ``ValueError`` si no tiene la forma."""
        if not isinstance(document, dict) or set(document) != {
            "covered_sequence",
            "covered_hash",
            "taken_at",
            "key_id",
            "signature",
        }:
            raise ValueError("el punto de control no tiene las cinco claves")
        sequence = document["covered_sequence"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or not 0 <= sequence <= _MAX_SEQUENCE
        ):
            raise ValueError("covered_sequence no es una secuencia")
        texts = (
            (document["covered_hash"], _HEX64),
            (document["taken_at"], _TIMESTAMP),
            (document["key_id"], _KEY_ID),
            (document["signature"], _SIGNATURE),
        )
        for value, pattern in texts:
            if not isinstance(value, str) or pattern.fullmatch(value) is None:
                raise ValueError("el punto de control no tiene la forma esperada")
        return cls(
            covered_sequence=sequence,
            covered_hash=document["covered_hash"],
            taken_at=document["taken_at"],
            key_id=document["key_id"],
            signature=document["signature"],
        )


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    """Un punto de control tal como está en su cadena."""

    chain: CheckpointChain
    sequence: int
    entry_id: uuid.UUID
    """``record_id`` o ``entry_id``."""
    entry_hash: str
    """``record_hash`` o ``entry_hash``."""
    content: CheckpointContent


class CheckpointOutcome(enum.StrEnum):
    WRITTEN = "written"
    ALREADY_CURRENT = "already_current"
    """El último registro de la cadena ya era un punto de control: no se escribe otro."""
    EMPTY_CHAIN = "empty_chain"
    """La cadena no tiene registros: no hay nada que cubrir."""


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    chain: CheckpointChain
    outcome: CheckpointOutcome
    checkpoint: StoredCheckpoint | None
    """El escrito, o el que ya cubría la cabeza; ``None`` en una cadena vacía."""


@dataclass(frozen=True, slots=True)
class CheckpointPublicKey:
    """Clave pública ``checkpoint`` publicada: nunca la referencia al secreto."""

    key_id: str
    public_key: str
    """32 bytes en base64 estándar."""
    status: KeyStatus
    valid_from: datetime
    valid_until: datetime

    @classmethod
    def of(cls, record: SigningKeyRecord) -> CheckpointPublicKey:
        return cls(
            key_id=record.key_id,
            public_key=record.public_key,
            status=record.status,
            valid_from=record.valid_from,
            valid_until=record.valid_until,
        )

    def public_key_bytes(self) -> bytes:
        return base64.b64decode(self.public_key, validate=True)

    def to_json(self) -> dict[str, str]:
        return {
            "key_id": self.key_id,
            "algorithm": "Ed25519",
            "public_key": self.public_key,
            "status": self.status.value,
            "valid_from": format_timestamp(self.valid_from),
            "valid_until": format_timestamp(self.valid_until),
        }


# --- Mensaje firmado y verificación ------------------------------------------------------------


def signed_payload(
    organization_id: uuid.UUID,
    chain: CheckpointChain,
    covered_sequence: int,
    covered_hash: str,
    taken_at: str,
) -> dict[str, Any]:
    """Lo que firma la clave ``checkpoint``: sus bytes son el canónico RFC 8785 de este objeto."""
    return {
        "covered_hash": covered_hash,
        "covered_sequence": covered_sequence,
        "kind": chain.kind.value,
        "organization_id": str(organization_id),
        "plant_id": None if chain.plant_id is None else str(chain.plant_id),
        "taken_at": taken_at,
    }


def verify_checkpoint(
    organization_id: uuid.UUID,
    chain: CheckpointChain,
    content: CheckpointContent,
    public_keys: Mapping[str, bytes],
) -> bool:
    """La firma del punto de control verifica con la clave publicada de su ``key_id`` (PR-NUC-21).

    Usa el verificador puro del paquete (``chain_walk`` y ``pure_ed25519``): el mismo algoritmo
    que el cliente ejecuta sobre su copia.
    """
    public_key = public_keys.get(content.key_id)
    if public_key is None:
        return False
    try:
        signature = base64.b64decode(content.signature, validate=True)
    except (binascii.Error, ValueError):
        return False
    if base64.b64encode(signature).decode("ascii") != content.signature:
        # Base64 no canónico: los 2 bits de relleno del último carácter no son cero. Otro texto
        # que decodifica a la misma firma no es la firma publicada (PR-NUC-21).
        return False
    try:
        message = checkpoint_message(
            chain.kind.value,
            str(organization_id),
            None if chain.plant_id is None else str(chain.plant_id),
            content.covered_sequence,
            content.covered_hash,
            content.taken_at,
        )
    except CanonicalizationError:
        # Sin forma canónica (p. ej. una secuencia fuera del rango de I-JSON) no hay firma válida.
        return False
    return ed25519_verify(public_key, message, signature)


def event_payload(chain: CheckpointChain, content: CheckpointContent) -> dict[str, Any]:
    """Carga de ``checkpoint_written`` (``CheckpointWritten`` de ``u02_events``)."""
    return {
        "chain_kind": chain.kind.value,
        "covered_sequence": content.covered_sequence,
        "covered_hash": content.covered_hash,
        "taken_at": content.taken_at,
    }


# --- Puertos ------------------------------------------------------------------------------------


class CheckpointSigner(Protocol):
    """La parte de ``SigningPort`` que usa el servicio (``SigningService``)."""

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any: ...

    def public_keys(self, purpose: SigningPurpose) -> tuple[SigningKeyRecord, ...]: ...


class CheckpointStore(Protocol):
    """Cadenas, cabezas y escritura del punto de control (``ledger.adapters.checkpoint_store``).

    Todo con el contexto recibido: la seguridad a nivel de fila limita a su organización.
    """

    async def heads(self, context: ScopeContext) -> Sequence[ChainHeadState]:
        """Todas las cadenas con cabeza de la organización del contexto."""
        ...

    async def head(self, context: ScopeContext, chain: CheckpointChain) -> ChainHeadState | None:
        """La cabeza de ``chain``, o ``None`` si la cadena aún no existe."""
        ...

    async def latest(
        self, context: ScopeContext, chain: CheckpointChain
    ) -> StoredCheckpoint | None:
        """El punto de control de mayor secuencia de ``chain``."""
        ...

    async def append(
        self, context: ScopeContext, chain: CheckpointChain, content: CheckpointContent
    ) -> StoredCheckpoint:
        """Anexa el punto de control y publica ``checkpoint_written`` en la misma transacción.

        ``CheckpointCoverageConflict`` si la cabeza ya no es la que ``content`` cubre; nada queda
        escrito.
        """
        ...


def _require_u02(context: ScopeContext) -> ScopeContext:
    if context.actor.unit is not ActorUnit.U02:
        raise CheckpointContextRejected()
    return context


# --- Servicio -----------------------------------------------------------------------------------


@repository
class CheckpointService:
    """``CheckpointPort`` (LC-NUC-14)."""

    def __init__(
        self,
        *,
        store: CheckpointStore,
        signer: CheckpointSigner,
        clock: Clock,
        writer_context: Callable[[ScopeContext], ScopeContext] = _require_u02,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts debe ser un entero positivo")
        self._store = store
        self._signer = signer
        self._clock = clock
        self._writer_context = writer_context
        self._max_attempts = max_attempts

    async def write_checkpoints_now(
        self, context: ScopeContext, chains: Sequence[CheckpointChain] | None = None
    ) -> tuple[CheckpointResult, ...]:
        """Un punto de control en cada cadena (todas las de la organización si ``chains`` es
        ``None``), salvo en las que ya lo tienen como último registro.

        Atiende todas las cadenas aunque alguna falle; si alguna quedó sin punto de control,
        lanza ``CheckpointWriteFailed`` al final con lo hecho y lo que falló.
        """
        writing = self._context(context)
        targets = await self._targets(writing, chains)
        results: list[CheckpointResult] = []
        failures: list[tuple[CheckpointChain, BaseException]] = []
        for chain in targets:
            try:
                results.append(await self._write_one(writing, chain))
            except Exception as error:  # se informa al final, cadena por cadena
                _log.error(
                    "punto de control no escrito",
                    chain_kind=chain.kind.value,
                    error_type=type(error).__name__,
                )
                failures.append((chain, error))
        if failures:
            raise CheckpointWriteFailed(tuple(results), tuple(failures))
        return tuple(results)

    async def latest_checkpoints(
        self, context: ScopeContext, chains: Sequence[CheckpointChain] | None = None
    ) -> tuple[StoredCheckpoint, ...]:
        """El último punto de control de cada cadena que tiene alguno."""
        targets = await self._targets(context, chains)
        found: list[StoredCheckpoint] = []
        for chain in targets:
            checkpoint = await self._store.latest(context, chain)
            if checkpoint is not None:
                found.append(checkpoint)
        return tuple(found)

    def checkpoint_public_keys(self) -> tuple[CheckpointPublicKey, ...]:
        """Todas las claves ``checkpoint``, también las retiradas (BR-NUC-55), por ``key_id``."""
        records = self._signer.public_keys(SigningPurpose.CHECKPOINT)
        keys = [
            CheckpointPublicKey.of(record)
            for record in records
            if record.purpose is SigningPurpose.CHECKPOINT
        ]
        return tuple(sorted(keys, key=lambda key: key.key_id))

    # --- interno -----------------------------------------------------------------------------

    def _context(self, context: ScopeContext) -> ScopeContext:
        if not isinstance(context, ScopeContext):
            raise CheckpointContextRejected()
        writing = self._writer_context(context)
        if (
            not isinstance(writing, ScopeContext)
            or writing.organization_id != context.organization_id
        ):
            raise CheckpointContextRejected()
        return _require_u02(writing)

    async def _targets(
        self, context: ScopeContext, chains: Sequence[CheckpointChain] | None
    ) -> tuple[CheckpointChain, ...]:
        if not isinstance(context, ScopeContext):
            raise CheckpointContextRejected()
        if chains is None:
            heads = await self._store.heads(context)
            selected = {head.chain for head in heads}
        else:
            if isinstance(chains, CheckpointChain) or not all(
                isinstance(chain, CheckpointChain) for chain in chains
            ):
                raise TypeError("chains debe ser una secuencia de CheckpointChain")
            selected = set(chains)
        return tuple(sorted(selected, key=CheckpointChain.sort_key))

    async def _write_one(self, context: ScopeContext, chain: CheckpointChain) -> CheckpointResult:
        for attempt in range(1, self._max_attempts + 1):
            head = await self._store.head(context, chain)
            if head is None or head.last_sequence == 0:
                return CheckpointResult(chain, CheckpointOutcome.EMPTY_CHAIN, None)
            if head.last_is_checkpoint:
                current = await self._store.latest(context, chain)
                return CheckpointResult(chain, CheckpointOutcome.ALREADY_CURRENT, current)
            content = self._sign(context.organization_id, head)
            try:
                stored = await self._store.append(context, chain, content)
            except CheckpointCoverageConflict:
                _log.info(
                    "la cabeza avanzó mientras se firmaba el punto de control; se reintenta",
                    chain_kind=chain.kind.value,
                    attempt=attempt,
                )
                continue
            return CheckpointResult(chain, CheckpointOutcome.WRITTEN, stored)
        raise CheckpointCoverageConflict()

    def _sign(self, organization_id: uuid.UUID, head: ChainHeadState) -> CheckpointContent:
        if not 1 <= head.last_sequence <= MAX_SIGNABLE_SEQUENCE:
            raise ValueError("la secuencia de la cabeza no tiene forma canónica RFC 8785")
        taken_at = format_timestamp(self._clock.now())
        payload = signed_payload(
            organization_id, head.chain, head.last_sequence, head.last_hash, taken_at
        )
        envelope = self._signer.sign(SigningPurpose.CHECKPOINT, payload)
        if not isinstance(envelope, PlatformSignedEnvelope):
            raise TypeError("el firmante no devolvió un sobre de punto de control")
        return CheckpointContent(
            covered_sequence=head.last_sequence,
            covered_hash=head.last_hash,
            taken_at=taken_at,
            key_id=envelope.key_id,
            signature=envelope.signature,
        )
