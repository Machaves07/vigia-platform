"""Motor de verificación de cadenas en dos pasos (LC-NUC-13; PAT-NUC-REN-02; BR-NUC-56, 58).

``IntegrityService`` implementa ``IntegrityPort`` (business-logic-model §5 y §10.1):
``verify(context, chain, mode)`` y ``last_results(context)``. Una cadena es la de
``ledger.chain.checkpoints``: expediente de planta, expediente de organización o auditoría.

Se lee la cabeza (``ChainHead``) y se recorre la cadena por **lotes de 10 000 secuencias**
``[objetivo propio]``, desde la génesis (``full`` y ``on_demand``) o desde el último registro
verificado íntegro (``incremental``: su secuencia y su hash están en el resultado anterior de la
cadena, en la auditoría). Por lote:

1. **Paso 1, en la base** (``IntegrityStore.scan``): una consulta recalcula ``content_hash`` sobre
   los bytes persistidos y ``record_hash`` (o ``entry_hash``) con el sobre canónico de la base,
   compara ``previous_hash`` con el hash del registro anterior (``LAG``; el primero del lote, con
   el hash con el que termina el lote anterior), exige secuencias contiguas y la ``source_key`` que
   el tipo declara, y devuelve solo la **primera** secuencia rota del lote.
2. **Paso 2, en Python**, sobre los registros anteriores a esa rotura:

   - forma canónica: los bytes de ``content`` son el canónico RFC 8785 de ``content_json``
     (protección frente a un fallo de la propia aplicación), sobre el 100 % en ``full`` y
     ``on_demand`` y sobre una muestra del 1 % y al menos 100 registros por lote en
     ``incremental`` (la semilla de la muestra queda en el resultado, para repetirla);
   - **siempre**, cada punto de control con ``chain_walk`` (el mismo recorrido que el verificador
     de paquetes): forma, cobertura exacta del registro anterior, clave conocida y firma Ed25519
     con las claves públicas ``checkpoint`` publicadas, también las retiradas (BR-NUC-55).

Al terminar, la cabeza almacenada debe coincidir con lo recalculado: misma última secuencia, mismo
hash y ningún registro más allá (``head_mismatch``). El primer fallo fija la secuencia rota (la
esperada, la anterior más uno) y su motivo, con los códigos de ``chain_walk``.

El resultado se audita siempre como ``integrity_verification`` (cadena, modo, secuencias cubiertas,
resultado; el registro roto como ``resource_ref``) y, ante ``broken``, en la misma transacción se
publica ``integrity_compromised`` y se suma ``integrity_compromised_total`` (alarma de máxima
severidad, NFR-NUC-38). Una cadena rota no se repara (BR-NUC-58): la incremental siguiente vuelve
a partir del último punto íntegro y la vuelve a encontrar rota hasta la restauración.

Si la base falla a mitad (``TemporarilyUnavailable`` u otro error), no hay resultado: no se audita
nada ni se da la cadena por íntegra (P5); el planificador reintenta.

Módulo crítico aislado (NFR-NUC-25): no importa FastAPI ni SQLAlchemy. La base, la auditoría y la
bandeja llegan por ``IntegrityStore`` (``ledger.adapters.integrity_store``), que corre en el worker
con ``statement_timeout`` de 30 s. No lee la hora del sistema: la del resultado es la del reloj
inyectado.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import json
import math
import os
import random
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_platform.ledger.chain.chain_walk import Break, ChainRef, ChainWalker, genesis_hash
from vigia_platform.ledger.chain.checkpoints import (
    ChainKind,
    CheckpointChain,
    CheckpointPublicKey,
)
from vigia_platform.ledger.chain.pure_rfc8785 import (
    MAX_SAFE_INTEGER,
    CanonicalizationError,
    canonicalize,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "BATCH_SIZE",
    "INTEGRITY_COMPROMISED",
    "SAMPLE_MINIMUM",
    "SAMPLE_PERCENT",
    "BatchScan",
    "CanonicalRow",
    "CheckpointKeySource",
    "HeadSnapshot",
    "IntegrityResult",
    "IntegrityService",
    "IntegrityStatus",
    "IntegrityStore",
    "VerificationMode",
    "VerifiedPoint",
    "audit_entry",
    "canonical_break",
    "event_payload",
    "ledger_entry",
    "sample_sequences",
    "sql_pass",
]

BATCH_SIZE: Final = 10_000
"""Secuencias por lote ``[objetivo propio]`` (PAT-NUC-REN-02)."""
SAMPLE_PERCENT: Final = 1
"""Porcentaje de cada lote que la incremental comprueba en forma canónica."""
SAMPLE_MINIMUM: Final = 100
"""Mínimo de registros por lote de esa muestra (todo el lote si tiene menos)."""

INTEGRITY_COMPROMISED: Final = "integrity_compromised"

_SEED_BYTES: Final = 16
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_SEED: Final = re.compile(r"[0-9a-f]{32}")

_log = get_logger("ledger.chain.verify")


# --- Valores -------------------------------------------------------------------------------------


class VerificationMode(enum.StrEnum):
    """Modo de la verificación (la ``verification_mode`` de ``integrity_compromised``)."""

    INCREMENTAL = "incremental"
    """Diaria: desde el último registro verificado íntegro; muestra canónica del 1 %."""
    FULL = "full"
    """Mensual: desde la génesis; forma canónica del 100 %."""
    ON_DEMAND = "on_demand"
    """A demanda con ``integrity.verify``: como ``full``."""

    @property
    def from_genesis(self) -> bool:
        return self is not VerificationMode.INCREMENTAL


class IntegrityStatus(enum.StrEnum):
    INTACT = "intact"
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class HeadSnapshot:
    """``ChainHead`` de la cadena y la mayor secuencia almacenada, en una misma instantánea.

    Una cadena sin cabeza es la génesis: secuencia 0 y el hash de génesis.
    """

    last_sequence: int
    last_hash: str
    max_sequence: int
    """Mayor ``chain_sequence`` guardada (0 sin registros): si supera a la cabeza, sobra algo."""


@dataclass(frozen=True, slots=True)
class BatchScan:
    """Lo que devuelve el paso 1 sobre un lote ``[first, last]``."""

    broken: Break | None
    """Primera secuencia rota del lote, o ``None``."""
    last_hash: str | None
    """Hash del registro ``last`` si el lote está íntegro: el enlace del lote siguiente."""


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    """Un registro para la comprobación de forma canónica del paso 2."""

    sequence: int
    entry_id: str
    document: str | None
    """``content_json::text`` (o ``filters_json::text``); ``None`` si la entrada no tiene."""
    content_hash: str | None
    """``content_hash`` (o ``filters_hash``), que el paso 1 ya comprobó sobre los bytes."""


@dataclass(frozen=True, slots=True)
class VerifiedPoint:
    """Hasta dónde llegó la última verificación íntegra de la cadena (estado incremental)."""

    sequence: int
    record_hash: str


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    """Resultado de una verificación; se audita como ``integrity_verification``."""

    chain: CheckpointChain
    mode: VerificationMode
    status: IntegrityStatus
    from_sequence: int
    """Primera secuencia examinada (la siguiente al punto de partida)."""
    to_sequence: int
    """Última secuencia verificada íntegra (la cabeza si ``intact``)."""
    verified_hash: str | None
    """Hash del registro ``to_sequence`` si ``intact``: el punto de partida de la incremental."""
    head_sequence: int
    broken_sequence: int | None
    broken_entry_id: uuid.UUID | None
    reason: str | None
    """Motivo de ``chain_walk`` (``record_hash_mismatch``, ``bad_signature``…) si ``broken``."""
    canonical_checked: int
    checkpoints_checked: int
    sample_seed: str | None
    """Semilla de la muestra canónica de la incremental (32 hexadecimales)."""
    verified_at: datetime | None = None
    """Marca de la entrada de auditoría del resultado."""

    @property
    def intact(self) -> bool:
        return self.status is IntegrityStatus.INTACT

    @property
    def verified_point(self) -> VerifiedPoint | None:
        if not self.intact or self.verified_hash is None:
            return None
        return VerifiedPoint(self.to_sequence, self.verified_hash)

    def to_filters(self) -> dict[str, Any]:
        """``filters`` de la entrada de auditoría: solo códigos, secuencias y hashes."""
        return {
            "chain_kind": self.chain.kind.value,
            "plant_id": None if self.chain.plant_id is None else str(self.chain.plant_id),
            "mode": self.mode.value,
            "result": self.status.value,
            "from_sequence": self.from_sequence,
            "to_sequence": self.to_sequence,
            "verified_hash": self.verified_hash,
            "head_sequence": self.head_sequence,
            "broken_sequence": self.broken_sequence,
            "reason": self.reason,
            "canonical_checked": self.canonical_checked,
            "checkpoints_checked": self.checkpoints_checked,
            "sample_seed": self.sample_seed,
        }

    @classmethod
    def from_filters(
        cls,
        document: object,
        *,
        broken_entry_id: uuid.UUID | None = None,
        verified_at: datetime | None = None,
    ) -> IntegrityResult:
        """Lectura estricta de un resultado auditado; ``ValueError`` si no tiene la forma."""
        if not isinstance(document, dict) or document.keys() != _FILTER_KEYS:
            raise ValueError("el resultado auditado no tiene las claves esperadas")
        plant = document["plant_id"]
        chain = CheckpointChain(
            ChainKind(document["chain_kind"]), None if plant is None else uuid.UUID(str(plant))
        )
        verified_hash = document["verified_hash"]
        seed = document["sample_seed"]
        reason = document["reason"]
        if verified_hash is not None and not _matches(_HEX64, verified_hash):
            raise ValueError("verified_hash no es un SHA-256 hexadecimal")
        if seed is not None and not _matches(_SEED, seed):
            raise ValueError("sample_seed no válida")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason no es un código")
        result = cls(
            chain=chain,
            mode=VerificationMode(document["mode"]),
            status=IntegrityStatus(document["result"]),
            from_sequence=_count(document["from_sequence"]),
            to_sequence=_count(document["to_sequence"]),
            verified_hash=verified_hash,
            head_sequence=_count(document["head_sequence"]),
            broken_sequence=None
            if document["broken_sequence"] is None
            else _count(document["broken_sequence"]),
            broken_entry_id=broken_entry_id,
            reason=reason,
            canonical_checked=_count(document["canonical_checked"]),
            checkpoints_checked=_count(document["checkpoints_checked"]),
            sample_seed=seed,
            verified_at=verified_at,
        )
        if result.intact != (result.broken_sequence is None) or (
            result.intact and result.verified_hash is None
        ):
            raise ValueError("resultado incoherente")
        return result


_FILTER_KEYS: Final = frozenset(
    {
        "chain_kind",
        "plant_id",
        "mode",
        "result",
        "from_sequence",
        "to_sequence",
        "verified_hash",
        "head_sequence",
        "broken_sequence",
        "reason",
        "canonical_checked",
        "checkpoints_checked",
        "sample_seed",
    }
)


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("se esperaba un entero no negativo")
    return value


def event_payload(result: IntegrityResult, detected_at: datetime) -> dict[str, Any]:
    """Carga de ``integrity_compromised`` (``IntegrityCompromised`` de ``u02_events``)."""
    if result.broken_sequence is None:
        raise ValueError("solo una cadena rota publica integrity_compromised")
    return {
        "chain_kind": result.chain.kind.value,
        "first_failed_sequence": result.broken_sequence,
        "verification_mode": result.mode.value,
        "detected_at": format_timestamp(detected_at),
    }


# --- Filas de la base en la forma del paquete (para ``chain_walk``) -----------------------------


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _parse_int(text: str) -> int | float:
    """Los números JSON son dobles (I-JSON): un entero fuera del rango seguro se lee como doble."""
    value = int(text)
    return value if abs(value) <= MAX_SAFE_INTEGER else float(text)


def _document(data: object) -> Any:
    if data is None:
        return None
    raw = data if isinstance(data, str) else bytes(data)  # type: ignore[call-overload]
    return json.loads(raw, parse_int=_parse_int)


def _actor(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": row["actor_kind"],
        "id": _text(row["actor_id"]),
        "display_name_snapshot": row["actor_display_name_snapshot"],
        "role_in_use": row["actor_role_in_use"],
        "concession_id": _text(row["actor_concession_id"]),
        "unit": row["actor_unit"],
    }


def ledger_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    """Columnas de ``ledger.ledger_record`` → registro en la forma del paquete."""
    return {
        "record_id": _text(row["record_id"]),
        "organization_id": _text(row["organization_id"]),
        "plant_id": _text(row["plant_id"]),
        "chain_sequence": row["chain_sequence"],
        "record_type": row["record_type"],
        "schema_version": row["schema_version"],
        "actor": _actor(row),
        "scope": {
            "plant_id": _text(row["scope_plant_id"]),
            "zone_id": _text(row["scope_zone_id"]),
            "node_id": _text(row["scope_node_id"]),
        },
        "correlation_id": _text(row["correlation_id"]),
        "received_at": format_timestamp(row["received_at"]),
        "content": _document(row["content"]),
        "content_hash": row["content_hash"],
        "previous_hash": row["previous_hash"],
        "record_hash": row["record_hash"],
    }


def audit_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    """Columnas de ``shared.audit_entry`` → entrada en la forma del paquete."""
    resource = (
        None
        if row["resource_kind"] is None and row["resource_id"] is None
        else {"kind": row["resource_kind"], "id": _text(row["resource_id"])}
    )
    return {
        "entry_id": _text(row["entry_id"]),
        "organization_id": _text(row["organization_id"]),
        "chain_sequence": row["chain_sequence"],
        "actor": _actor(row),
        "operation": row["operation"],
        "scope": {"plant_id": _text(row["scope_plant_id"]), "zone_id": _text(row["scope_zone_id"])},
        "resource_ref": resource,
        "filters": _document(row["filters"]),
        "filters_hash": row["filters_hash"],
        "result_count": row["result_count"],
        "outcome": row["outcome"],
        "correlation_id": _text(row["correlation_id"]),
        "occurred_at": format_timestamp(row["occurred_at"]),
        "previous_hash": row["previous_hash"],
        "entry_hash": row["entry_hash"],
    }


# --- Paso 2: forma canónica ----------------------------------------------------------------------


def canonical_break(row: CanonicalRow) -> Break | None:
    """Rotura si los bytes persistidos no son el canónico de su documento ``jsonb``.

    El paso 1 ya comprobó ``content_hash = SHA-256(bytes)``; aquí basta con que el canónico de
    ``content_json`` tenga ese mismo hash (los bytes son canónicos si y solo si coinciden).
    """
    if row.document is None:
        return None
    try:
        document = json.loads(row.document, parse_int=_parse_int)
        digest = hashlib.sha256(canonicalize(document)).hexdigest()
    except (CanonicalizationError, ValueError, RecursionError):
        digest = None
    if digest is not None and digest == row.content_hash:
        return None
    return Break(row.sequence, row.entry_id, "content_not_canonical")


def sample_sequences(first: int, last: int, rng: random.Random) -> list[int]:
    """Muestra de la incremental en ``[first, last]``: el 1 % y al menos 100, sin repetir."""
    size = last - first + 1
    if size <= 0:
        return []
    wanted = min(size, max(SAMPLE_MINIMUM, math.ceil(size * SAMPLE_PERCENT / 100)))
    return sorted(rng.sample(range(first, last + 1), wanted))


# --- Puertos -------------------------------------------------------------------------------------


class CheckpointKeySource(Protocol):
    """Las claves públicas ``checkpoint`` (``CheckpointService.checkpoint_public_keys``)."""

    def checkpoint_public_keys(self) -> Sequence[CheckpointPublicKey]: ...


class IntegrityStore(Protocol):
    """Lo que el motor necesita de la base (``ledger.adapters.integrity_store``).

    Todo con el ``ScopeContext`` recibido: la seguridad a nivel de fila limita a su organización.
    """

    async def chains(self, context: ScopeContext) -> Sequence[CheckpointChain]:
        """Las cadenas con cabeza de la organización."""
        ...

    async def head(self, context: ScopeContext, chain: CheckpointChain) -> HeadSnapshot:
        """Cabeza y mayor secuencia en una misma instantánea (génesis si no hay cabeza)."""
        ...

    async def scan(
        self, context: ScopeContext, chain: CheckpointChain, first: int, last: int, start_hash: str
    ) -> BatchScan:
        """Paso 1 sobre ``[first, last]``; ``start_hash`` es el hash del registro ``first - 1``."""
        ...

    async def checkpoints(
        self, context: ScopeContext, chain: CheckpointChain, first: int, last: int
    ) -> Sequence[Mapping[str, Any]]:
        """Filas completas de los puntos de control de ``[first, last]``, por secuencia."""
        ...

    async def documents(
        self,
        context: ScopeContext,
        chain: CheckpointChain,
        first: int,
        last: int,
        sequences: Sequence[int] | None,
    ) -> Sequence[CanonicalRow]:
        """Documentos de ``[first, last]`` (todos, o solo ``sequences``), por secuencia."""
        ...

    async def last_verified(
        self, context: ScopeContext, chain: CheckpointChain
    ) -> VerifiedPoint | None:
        """El punto del último resultado ``intact`` de la cadena, o ``None``."""
        ...

    async def results(self, context: ScopeContext) -> Sequence[IntegrityResult]:
        """El último resultado de cada cadena."""
        ...

    async def record(
        self,
        context: ScopeContext,
        result: IntegrityResult,
        event: Mapping[str, Any] | None,
    ) -> datetime:
        """Audita el resultado y, con ``event``, publica ``integrity_compromised`` en la misma
        transacción; devuelve la marca de la entrada de auditoría."""
        ...


# --- Paso 1 --------------------------------------------------------------------------------------


async def sql_pass(
    store: IntegrityStore,
    context: ScopeContext,
    chain: CheckpointChain,
    *,
    start: VerifiedPoint,
    head: HeadSnapshot,
    batch_size: int = BATCH_SIZE,
) -> Break | None:
    """Solo el paso 1 sobre toda la cadena, con la comprobación final de la cabeza.

    Es lo que ``IntegrityService.verify`` hace sin el paso 2; PR-NUC-49 lo compara con el
    recorrido en Python.
    """
    first, previous = start.sequence + 1, start.record_hash
    if head.last_sequence < start.sequence:
        return Break(head.last_sequence + 1, None, "head_mismatch")
    while first <= head.last_sequence:
        last = min(first + batch_size - 1, head.last_sequence)
        scan = await store.scan(context, chain, first, last, previous)
        if scan.broken is not None:
            return scan.broken
        if scan.last_hash is None:  # pragma: no cover - el adaptador siempre lo devuelve
            raise RuntimeError("el paso 1 no devolvió el hash del lote")
        previous, first = scan.last_hash, last + 1
    return _head_break(head, previous)


def _head_break(head: HeadSnapshot, recomputed: str) -> Break | None:
    if head.max_sequence > head.last_sequence:
        return Break(
            head.last_sequence + 1,
            None,
            "head_mismatch",
            f"hay registros hasta la secuencia {head.max_sequence}",
        )
    if recomputed != head.last_hash:
        return Break(head.last_sequence, None, "head_mismatch")
    return None


# --- Servicio ------------------------------------------------------------------------------------


@dataclass
class _Progress:
    canonical: int = 0
    checkpoints: int = 0


class IntegrityService:
    """``IntegrityPort`` (LC-NUC-13)."""

    def __init__(
        self,
        *,
        store: IntegrityStore,
        keys: CheckpointKeySource,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
        batch_size: int = BATCH_SIZE,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size debe ser un entero positivo")
        self._store = store
        self._keys = keys
        self._clock = clock
        self._random_bytes = random_bytes
        self._batch_size = batch_size
        self._metrics = metrics

    async def verify(
        self, context: ScopeContext, chain: CheckpointChain, mode: VerificationMode
    ) -> IntegrityResult:
        """Verifica ``chain`` en ``mode``, audita el resultado y, si está rota, lo eleva."""
        if not isinstance(context, ScopeContext):
            raise TypeError("context debe ser ScopeContext")
        if not isinstance(chain, CheckpointChain):
            raise TypeError("chain debe ser CheckpointChain")
        mode = VerificationMode(mode)
        result = await self._walk(context, chain, mode)
        detected_at = self._clock.now()
        event = None if result.intact else event_payload(result, detected_at)
        recorded_at = await self._store.record(context, result, event)
        result = replace(result, verified_at=recorded_at)
        if result.intact:
            _log.info(
                "cadena verificada íntegra",
                chain_kind=chain.kind.value,
                verification_mode=mode.value,
                to_sequence=result.to_sequence,
            )
        else:
            (self._metrics or get_metrics()).integrity_compromised_total.add(
                1,
                {"chain_kind": chain.kind.value, "organization_id": str(context.organization_id)},
            )
            _log.error(
                "cadena rota: integrity_compromised",
                chain_kind=chain.kind.value,
                verification_mode=mode.value,
                broken_sequence=result.broken_sequence,
                reason=result.reason,
                organization_id=context.organization_id,
            )
        return result

    async def verify_all(
        self, context: ScopeContext, mode: VerificationMode
    ) -> tuple[IntegrityResult, ...]:
        """Todas las cadenas de la organización del contexto, una tras otra."""
        chains = sorted(await self._store.chains(context), key=CheckpointChain.sort_key)
        return tuple([await self.verify(context, chain, mode) for chain in chains])

    async def last_results(self, context: ScopeContext) -> tuple[IntegrityResult, ...]:
        """El último resultado auditado de cada cadena de la organización."""
        if not isinstance(context, ScopeContext):
            raise TypeError("context debe ser ScopeContext")
        results = await self._store.results(context)
        return tuple(sorted(results, key=lambda result: result.chain.sort_key()))

    # --- interno -------------------------------------------------------------------------------

    def _public_keys(self) -> dict[str, bytes]:
        keys: dict[str, bytes] = {}
        for key in self._keys.checkpoint_public_keys():
            try:
                raw = base64.b64decode(key.public_key, validate=True)
            except (binascii.Error, ValueError):
                continue  # una clave ilegible no verifica nada: su firma saldrá unknown_key
            if base64.b64encode(raw).decode("ascii") != key.public_key:
                continue  # base64 no canónico: otro texto para la misma clave no es la publicada
            keys[key.key_id] = raw
        return keys

    async def _walk(
        self, context: ScopeContext, chain: CheckpointChain, mode: VerificationMode
    ) -> IntegrityResult:
        head = await self._store.head(context, chain)
        genesis = VerifiedPoint(0, _genesis(context, chain))
        start = genesis
        if not mode.from_genesis:
            start = await self._store.last_verified(context, chain) or genesis
        seed: str | None = None
        rng: random.Random | None = None
        if not mode.from_genesis:
            seed = self._random_bytes(_SEED_BYTES).hex()
            # La muestra no protege nada por ser impredecible: basta con que sea reproducible
            # desde la semilla auditada.
            rng = random.Random(int(seed, 16))  # noqa: S311
        progress = _Progress()
        keys = self._public_keys()
        broken = await self._batches(context, chain, start, head, keys, rng, progress)
        if broken is None:
            return IntegrityResult(
                chain=chain,
                mode=mode,
                status=IntegrityStatus.INTACT,
                from_sequence=start.sequence + 1,
                to_sequence=head.last_sequence,
                verified_hash=head.last_hash,
                head_sequence=head.last_sequence,
                broken_sequence=None,
                broken_entry_id=None,
                reason=None,
                canonical_checked=progress.canonical,
                checkpoints_checked=progress.checkpoints,
                sample_seed=seed,
            )
        sequence = broken.sequence if broken.sequence is not None else start.sequence + 1
        return IntegrityResult(
            chain=chain,
            mode=mode,
            status=IntegrityStatus.BROKEN,
            from_sequence=start.sequence + 1,
            to_sequence=max(start.sequence, sequence - 1),
            verified_hash=None,
            head_sequence=head.last_sequence,
            broken_sequence=max(sequence, 1),
            broken_entry_id=None if broken.entry_id is None else uuid.UUID(broken.entry_id),
            reason=broken.reason,
            canonical_checked=progress.canonical,
            checkpoints_checked=progress.checkpoints,
            sample_seed=seed,
        )

    async def _batches(
        self,
        context: ScopeContext,
        chain: CheckpointChain,
        start: VerifiedPoint,
        head: HeadSnapshot,
        keys: Mapping[str, bytes],
        rng: random.Random | None,
        progress: _Progress,
    ) -> Break | None:
        if head.last_sequence < start.sequence:
            return Break(head.last_sequence + 1, None, "head_mismatch")
        first, previous = start.sequence + 1, start.record_hash
        while first <= head.last_sequence:
            last = min(first + self._batch_size - 1, head.last_sequence)
            scan = await self._store.scan(context, chain, first, last, previous)
            limit = last if scan.broken is None else (scan.broken.sequence or first) - 1
            second = await self._second_pass(context, chain, first, limit, keys, rng, progress)
            if second is not None:
                return second
            if scan.broken is not None:
                return scan.broken
            if scan.last_hash is None:  # pragma: no cover - el adaptador siempre lo devuelve
                raise RuntimeError("el paso 1 no devolvió el hash del lote")
            previous, first = scan.last_hash, last + 1
        return _head_break(head, previous)

    async def _second_pass(
        self,
        context: ScopeContext,
        chain: CheckpointChain,
        first: int,
        last: int,
        keys: Mapping[str, bytes],
        rng: random.Random | None,
        progress: _Progress,
    ) -> Break | None:
        """Paso 2 sobre ``[first, last]`` (registros que el paso 1 dio por buenos)."""
        if last < first:
            return None
        found: list[Break] = []
        reference = ChainRef(
            chain.kind.value,
            str(context.organization_id),
            None if chain.plant_id is None else str(chain.plant_id),
        )
        convert = audit_entry if chain.kind is ChainKind.AUDIT else ledger_entry
        for row in await self._store.checkpoints(context, chain, first, last):
            entry = convert(row)
            sequence = int(entry["chain_sequence"])
            walker = ChainWalker(
                reference,
                keys,
                start_sequence=sequence - 1,
                start_hash=str(entry["previous_hash"]),
            )
            failure = walker.feed(entry)
            progress.checkpoints += 1
            if failure is not None:
                found.append(failure)
                break
        sequences = None if rng is None else sample_sequences(first, last, rng)
        for document in await self._store.documents(context, chain, first, last, sequences):
            progress.canonical += 1
            failure = canonical_break(document)
            if failure is not None:
                found.append(failure)
                break
        if not found:
            return None
        return min(found, key=lambda item: item.sequence or 0)


def _genesis(context: ScopeContext, chain: CheckpointChain) -> str:
    return genesis_hash(
        str(context.organization_id), None if chain.plant_id is None else str(chain.plant_id)
    )
