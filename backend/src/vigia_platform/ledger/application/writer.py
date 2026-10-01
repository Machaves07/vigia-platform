"""``EscritorExpediente``: el único camino de escritura del expediente (LC-NUC-10; BR-NUC-43 a 50).

``write(context, record_type, content) -> Receipt | LedgerRejection`` aplica el **orden fijo**
de verificación; el primer fallo fija el código (como BR-CTR-32):

1. **contexto** presente y de la organización del contenido (``context_absent``);
2. **tipo** registrado y escribible por la unidad del actor (``record_type_unknown``);
3. **esquema y tamaño**: a lo sumo 256 KB (cota calculada sin serializar, antes de validar),
   esquema estricto del tipo sin coerción ni valores por defecto (en el pool de CPU si pasa de
   16 KB), alcance coherente, ``source_key`` de 1 a 64 caracteres, regla de etiqueta proyectable
   y bytes canónicos ≤ 256 KB (``content_invalid`` con el puntero del primer campo que falla),
   todo antes de cualquier consulta a la base;
4. **texto libre**: cada ruta de ``free_text_paths`` pasa ``FreeTextPolicy``; se guarda el texto
   en NFC (``free_text_rejected``);
5. **idempotencia** por (organización, tipo, ``source_key``): mismo ``content_hash`` →
   ``accepted_duplicate`` con el recibo original y sin registro nuevo; distinto →
   ``idempotency_conflict`` (BR-NUC-48);
6. **evidencias**: cada ``ClipReference`` de ``evidence_paths`` contra los metadatos del objeto
   (``evidence_missing``, ``evidence_hash_mismatch``, ``evidence_not_anonymized``; BR-NUC-64);
7. **escritura y encadenado** (``chain_locked_timeout`` si la exclusión de la cadena no llega a
   tiempo).

Todo lo costoso ocurre **antes** de abrir la transacción (PAT-NUC-RES-08): validación,
canonicalización (una sola vez, PAT-NUC-REN-01), consulta de idempotencia, ``HEAD`` de las
evidencias en paralelo (PAT-NUC-REN-06) y cálculo de la etiqueta. Dentro solo van el ``INSERT``
del registro (el disparador toma la exclusión, asigna secuencia y ``received_at`` y calcula los
hashes), las evidencias, la etiqueta y los eventos de la bandeja; el ``Receipt`` se emite
**después** de confirmar (BR-NUC-50, BR-CTR-31). Si algo falla dentro, no queda nada.

Dos escrituras concurrentes con la misma clave pasan las dos el paso 5: la segunda choca con la
unicidad de ``ledger.record_source_key`` en el disparador, su transacción se revierte entera y se
resuelve de nuevo como duplicado o conflicto.

El registro guarda la instantánea del actor del contexto (nombre, ``role_in_use``,
``concession_id``, unidad) y su ``correlation_id`` (BR-NUC-49). ``to_contract_rejection`` traduce
un rechazo al ``RejectionResponse`` del contrato cuando escribe U-03.

Fallos transitorios que no son un rechazo: ``StorageUnavailable`` (almacén caído en el paso 6) y
``TemporarilyUnavailable`` (base) salen como excepción; ningún mensaje repite el contenido.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast

from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable
from vigia_contracts.models.clip_reference import ClipReference
from vigia_contracts.models.enumerations import AcceptanceStatus, RejectionCode
from vigia_contracts.models.receipt import Receipt as ContractReceipt
from vigia_contracts.models.rejection_response import RejectionResponse

from vigia_platform.ledger.canonical import (
    LARGE_DOCUMENT_BYTES,
    CanonicalFormError,
    canonical_bytes,
    exceeds_canonical_size,
    parse,
)
from vigia_platform.ledger.content_paths import (
    Located,
    contract_field,
    iter_segments,
    locate,
    pointer,
)
from vigia_platform.ledger.content_paths import replace as replace_value
from vigia_platform.ledger.evidence import (
    CONTRACT_REJECTION_CODE,
    EvidenceCheck,
    EvidenceCode,
    EvidenceOwner,
    first_failure,
)
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.ledger.labels_projection import (
    LabelProjection,
    LabelRuleViolation,
    evidence_ids_statement,
    insert_label,
    project,
)
from vigia_platform.ledger.registry import (
    ChainLevel,
    CompiledType,
    RecordTypeRegistry,
    RecordTypeUnknown,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import (
    ContextOrigin,
    ScopeContext,
    ScopeLevel,
    handles_absent_context,
    report_context_absent,
    repository,
)
from vigia_platform.shared.cpu_pool import CpuPool, get_cpu_pool
from vigia_platform.shared.db import ChainLockedTimeout, Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.storage import StorageUnavailable

__all__ = [
    "CHAIN_LEVEL_CONSTRAINT",
    "CHECKPOINT_COVERAGE_CONSTRAINT",
    "CHECK_VIOLATION",
    "MAX_CONTENT_BYTES",
    "MAX_SOURCE_KEY_CHARS",
    "EscritorExpediente",
    "EvidenceReferenceVerifier",
    "LedgerDatabase",
    "LedgerRejection",
    "LedgerRejectionCode",
    "Receipt",
    "RecordScope",
    "to_contract_rejection",
    "violated_constraint",
]

MAX_CONTENT_BYTES: Final = 256 * 1024
"""Tope del contenido canónico (BR-NUC-44; restricción ``ledger_record_content_size``)."""

MAX_SOURCE_KEY_CHARS: Final = 64
"""Tope de ``source_key`` (domain-entities §3.1; restricción ``ledger_record_source_key``)."""

_SOURCE_KEY_CONSTRAINT: Final = "record_source_key_pkey"
_UNIQUE_VIOLATION: Final = "23505"
CHECK_VIOLATION: Final = "23514"
CHAIN_LEVEL_CONSTRAINT: Final = "ledger_record_chain_level"
"""Restricción con la que el disparador rechaza la planta de la cadena (``nuc_0005``)."""
CHECKPOINT_COVERAGE_CONSTRAINT: Final = "ledger_record_checkpoint_coverage"
"""Restricción con la que el disparador rechaza un punto de control que no cubre la cabeza."""
_RETRY_AFTER_SECONDS: Final = 5

_log = get_logger("ledger.writer")


class LedgerRejectionCode(enum.StrEnum):
    """``ledger_rejection_code`` (domain-entities §1), en el orden fijo de verificación."""

    CONTEXT_ABSENT = "context_absent"
    RECORD_TYPE_UNKNOWN = "record_type_unknown"
    CONTENT_INVALID = "content_invalid"
    FREE_TEXT_REJECTED = "free_text_rejected"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_HASH_MISMATCH = "evidence_hash_mismatch"
    EVIDENCE_NOT_ANONYMIZED = "evidence_not_anonymized"
    CHAIN_LOCKED_TIMEOUT = "chain_locked_timeout"


_MESSAGES: Final[Mapping[LedgerRejectionCode, str]] = {
    LedgerRejectionCode.CONTEXT_ABSENT: (
        "La escritura no tiene un contexto de alcance de la organización del contenido."
    ),
    LedgerRejectionCode.RECORD_TYPE_UNKNOWN: (
        "El tipo de registro no está registrado o la unidad que escribe no puede escribirlo."
    ),
    LedgerRejectionCode.CONTENT_INVALID: (
        "El contenido no cumple el esquema estricto del tipo de registro o supera su tamaño."
    ),
    LedgerRejectionCode.FREE_TEXT_REJECTED: (
        "Un campo de texto libre no cumple la política de texto libre."
    ),
    LedgerRejectionCode.IDEMPOTENCY_CONFLICT: (
        "Ya existe un registro con la misma clave de idempotencia y distinto contenido."
    ),
    LedgerRejectionCode.EVIDENCE_MISSING: (
        "Una evidencia referenciada no existe en el almacén para este registro."
    ),
    LedgerRejectionCode.EVIDENCE_HASH_MISMATCH: (
        "El tamaño o la suma SHA-256 de una evidencia no coinciden con el objeto almacenado."
    ),
    LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED: (
        "Una evidencia no lleva la marca de anonimización."
    ),
    LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT: (
        "La cadena del expediente está ocupada; reintenta la escritura."
    ),
}

_EVIDENCE_CODES: Final[Mapping[EvidenceCode, LedgerRejectionCode]] = {
    EvidenceCode.EVIDENCE_MISSING: LedgerRejectionCode.EVIDENCE_MISSING,
    EvidenceCode.EVIDENCE_HASH_MISMATCH: LedgerRejectionCode.EVIDENCE_HASH_MISMATCH,
    EvidenceCode.EVIDENCE_NOT_ANONYMIZED: LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED,
}


# --- Valores del puerto -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Receipt:
    """``Receipt`` (domain-entities §3.6): se emite solo tras confirmar la transacción."""

    record_id: uuid.UUID
    received_at: datetime
    status: AcceptanceStatus

    def to_contract(self) -> ContractReceipt:
        """El recibo del contrato (``platform_record_id``, marca en UTC con milisegundos)."""
        return ContractReceipt.model_validate_json(
            json.dumps(
                {
                    "platform_record_id": str(self.record_id),
                    "received_at": _timestamp(self.received_at),
                    "status": self.status.value,
                }
            )
        )


@dataclass(frozen=True, slots=True)
class LedgerRejection:
    """Rechazo del expediente: código cerrado, puntero JSON del campo y mensaje genérico.

    ``field`` nunca lleva contenido del registro; ``message_es`` es el del código.
    """

    code: LedgerRejectionCode
    field: str | None = None
    message_es: str = ""

    @classmethod
    def of(cls, code: LedgerRejectionCode, field: str | None = None) -> LedgerRejection:
        return cls(code=code, field=field or None, message_es=_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class RecordScope:
    """Planta, zona y nodo del registro (el ``scope`` del sobre, domain-entities §3.1).

    Lo que el contenido ya lleva en ``/plant_id``, ``/zone_id`` o ``/node_id`` manda; aquí se
    aporta lo que el contenido no lleva (por ejemplo la planta de ``node_zone_assigned``).
    """

    plant_id: uuid.UUID | None = None
    zone_id: uuid.UUID | None = None
    node_id: uuid.UUID | None = None


def _timestamp(moment: datetime) -> str:
    """Marca en UTC con milisegundos truncados y ``Z`` (la forma del contrato)."""
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


_CONTRACT_CODES: Final[Mapping[LedgerRejectionCode, RejectionCode]] = {
    LedgerRejectionCode.RECORD_TYPE_UNKNOWN: RejectionCode.SCHEMA_INVALID,
    LedgerRejectionCode.CONTENT_INVALID: RejectionCode.SCHEMA_INVALID,
    LedgerRejectionCode.FREE_TEXT_REJECTED: RejectionCode.SCHEMA_INVALID,
    LedgerRejectionCode.IDEMPOTENCY_CONFLICT: RejectionCode.IDEMPOTENCY_CONFLICT,
    LedgerRejectionCode.EVIDENCE_MISSING: CONTRACT_REJECTION_CODE[EvidenceCode.EVIDENCE_MISSING],
    LedgerRejectionCode.EVIDENCE_HASH_MISMATCH: CONTRACT_REJECTION_CODE[
        EvidenceCode.EVIDENCE_HASH_MISMATCH
    ],
    LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED: CONTRACT_REJECTION_CODE[
        EvidenceCode.EVIDENCE_NOT_ANONYMIZED
    ],
    LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT: RejectionCode.TEMPORARILY_UNAVAILABLE,
}
"""Traducción al contrato cuando escribe U-03 (domain-entities §3.6). ``context_absent`` no tiene
traducción: U-03 siempre escribe con contexto, así que es un error interno."""

_CONTRACT_MESSAGES: Final[Mapping[RejectionCode, str]] = {
    RejectionCode.SCHEMA_INVALID: "El registro no cumple el esquema del contrato.",
    RejectionCode.IDEMPOTENCY_CONFLICT: (
        "Ya existe un registro con el mismo identificador y distinto contenido."
    ),
    RejectionCode.CLIP_MISSING: "Un clip referenciado no está en el almacén.",
    RejectionCode.CLIP_HASH_MISMATCH: "El tamaño o el SHA-256 de un clip no coinciden.",
    RejectionCode.CLIP_NOT_ANONYMIZED: "Un clip no lleva la marca de anonimización.",
    RejectionCode.TEMPORARILY_UNAVAILABLE: "La plataforma está ocupada; reintenta más tarde.",
}


def to_contract_rejection(rejection: LedgerRejection) -> RejectionResponse:
    """``RejectionResponse`` del contrato para un rechazo de una escritura de U-03.

    ``field`` pasa de puntero JSON a la forma del contrato (``cameras[0].clips[0].sha256``).
    ``ValueError`` si el código no tiene traducción (``context_absent``).
    """
    code = _CONTRACT_CODES.get(rejection.code)
    if code is None:
        raise ValueError(f"el rechazo {rejection.code.value} no tiene código del contrato")
    retryable = code is RejectionCode.TEMPORARILY_UNAVAILABLE
    document: dict[str, JsonValue] = {
        "code": code.value,
        "retryable": retryable,
        "message_es": _CONTRACT_MESSAGES[code],
    }
    if rejection.field is not None:
        field = contract_field(list(iter_segments(rejection.field)))
        if field is not None:
            document["field"] = field
    if retryable:
        document["retry_after_seconds"] = _RETRY_AFTER_SECONDS
    # El modelo del contrato valida la forma de transporte (JSON), como la recibe el nodo.
    return RejectionResponse.model_validate_json(json.dumps(document))


# --- Puertos que usa --------------------------------------------------------------------------


class LedgerDatabase(Protocol):
    """La parte de ``shared.db.Database`` que usa el escritor."""

    def transaction(self, context: ScopeContext) -> Any:
        """``async with`` que entrega una ``Transaction`` con el contexto fijado."""
        ...

    async def read(
        self,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Row[Any]]: ...


class EvidenceReferenceVerifier(Protocol):
    """``EvidenceVerifier.verify_references`` (LC-NUC-15)."""

    async def verify_references(
        self, owner: EvidenceOwner, refs: Sequence[ClipReference]
    ) -> list[EvidenceCheck]: ...


# --- Sentencias -------------------------------------------------------------------------------

_INSERT_RECORD: Final = text(
    "INSERT INTO ledger.ledger_record (record_id, organization_id, plant_id, record_type,"
    " schema_version, actor_kind, actor_id, actor_display_name_snapshot, actor_role_in_use,"
    " actor_concession_id, actor_unit, scope_plant_id, scope_zone_id, scope_node_id,"
    " correlation_id, occurred_at, source_key, content)"
    " VALUES (:record_id, :organization_id, :plant_id, :record_type, :schema_version,"
    " :actor_kind, :actor_id, :actor_display_name_snapshot, :actor_role_in_use,"
    " :actor_concession_id, :actor_unit, :scope_plant_id, :scope_zone_id, :scope_node_id,"
    " :correlation_id, :occurred_at, :source_key, :content)"
    " RETURNING record_id, received_at, chain_sequence"
)

_INSERT_EVIDENCE: Final = text(
    "INSERT INTO ledger.evidence (evidence_id, organization_id, plant_id, zone_id, node_id,"
    " record_id, clip_id, camera_id, storage_key, sha256, size_bytes, content_type, media_kind,"
    " duration_ms, segment, starts_at, ends_at, verification_method, verified_at)"
    " VALUES (:evidence_id, :organization_id, :plant_id, :zone_id, :node_id, :record_id,"
    " :clip_id, :camera_id, :storage_key, :sha256, :size_bytes, :content_type, :media_kind,"
    " :duration_ms, :segment, :starts_at, :ends_at, 'object_metadata', :verified_at)"
)

_BY_SOURCE_KEY: Final = text(
    "SELECT r.record_id, r.received_at, r.content_hash FROM ledger.record_source_key AS k"
    " JOIN ledger.ledger_record AS r"
    " ON r.record_id = k.record_id AND r.received_at = k.received_at"
    " AND r.organization_id = k.organization_id"
    " WHERE k.organization_id = :organization_id AND k.record_type = :record_type"
    " AND k.source_key = :source_key"
)

_ZONE_PLANT: Final = text(
    "SELECT z.plant_id FROM identity.zone AS z"
    " WHERE z.organization_id = :organization_id AND z.zone_id = :zone_id"
)


# --- Preparación (todo antes de abrir la transacción) ----------------------------------------


@dataclass(frozen=True, slots=True)
class _Evidence:
    reference: ClipReference
    check: EvidenceCheck


@dataclass(frozen=True, slots=True)
class _Prepared:
    compiled: CompiledType
    content: bytes
    content_hash: str
    chain_plant: uuid.UUID | None
    scope: RecordScope
    source_key: str | None
    evidences: tuple[_Evidence, ...]
    label: LabelProjection | None
    label_evidence_ids: tuple[uuid.UUID, ...]
    events: tuple[NewEvent, ...]


class _Rejected(Exception):
    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(rejection.code.value)
        self.rejection = rejection


def _reject(code: LedgerRejectionCode, field: str | None = None) -> _Rejected:
    return _Rejected(LedgerRejection.of(code, field))


def _document(content: object) -> Any:
    """El documento JSON del contenido: un ``Mapping`` o un modelo (solo lo que se fijó)."""
    if isinstance(content, BaseModel):
        try:
            return content.model_dump(mode="json", by_alias=True, exclude_unset=True)
        except (ValueError, TypeError, RecursionError):
            raise _reject(LedgerRejectionCode.CONTENT_INVALID) from None
    return content


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _error_pointer(error: ValidationError) -> str | None:
    """Puntero del primer campo que falla, sin el valor (PR-NUC-54)."""
    errors = error.errors(include_input=False, include_url=False, include_context=False)
    if not errors:
        return None
    segments = [part for part in errors[0].get("loc", ()) if isinstance(part, str | int)]
    # Pydantic añade a la ruta la etiqueta de la rama de una unión (``str``, ``function-after``):
    # solo se conservan nombres de campo e índices.
    kept: list[str | int] = [
        part
        for part in segments
        if isinstance(part, int) or (part.isidentifier() and part.islower())
    ]
    return pointer(kept) or None


def _schema_checked(compiled: CompiledType, document: Mapping[str, Any]) -> dict[str, Any]:
    """El documento validado contra el esquema estricto y leído como RFC 8785 (paso 3).

    Pura y sin E/S: corre en el bucle o, con un contenido grande, en el pool de CPU.
    """
    invalid = LedgerRejectionCode.CONTENT_INVALID
    try:
        raw = json.dumps(document, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError, OverflowError):
        raise _reject(invalid) from None
    try:
        compiled.validate_json(raw)
    except ValidationError as error:
        raise _reject(invalid, _error_pointer(error)) from None
    except (ValueError, RecursionError, OverflowError):
        raise _reject(invalid) from None
    try:
        # Lectura estricta de RFC 8785 del mismo texto: lo que se canonicaliza y se guarda es el
        # contenido tal como llegó, sin valores por defecto (BR-NUC-44).
        parsed = parse(raw.encode("utf-8", "surrogatepass"))
    except CanonicalFormError:
        raise _reject(invalid) from None
    if not isinstance(parsed, dict):  # pragma: no cover - ya validado como objeto
        raise _reject(invalid)
    return parsed


def _resolve_scope(
    document: Mapping[str, Any], scope: RecordScope | None, compiled: CompiledType
) -> tuple[RecordScope, uuid.UUID | None]:
    """El alcance del registro y la planta de su cadena (BR-NUC-45)."""
    given = scope or RecordScope()
    resolved: dict[str, uuid.UUID | None] = {}
    for name in ("plant_id", "zone_id", "node_id"):
        explicit = getattr(given, name)
        if explicit is not None and type(explicit) is not uuid.UUID:
            raise TypeError(f"scope.{name} debe ser uuid.UUID")
        in_content = _uuid_or_none(document.get(name))
        if in_content is not None and explicit is not None and in_content != explicit:
            raise _reject(LedgerRejectionCode.CONTENT_INVALID, f"/{name}")
        resolved[name] = in_content if in_content is not None else explicit
    record_scope = RecordScope(**resolved)
    definition = compiled.definition
    if compiled.chain_level is ChainLevel.PLANT:
        if record_scope.plant_id is None:
            raise _reject(LedgerRejectionCode.CONTENT_INVALID, "/plant_id")
        chain_plant: uuid.UUID | None = record_scope.plant_id
    elif definition.chain_follows_scope:
        chain_plant = record_scope.plant_id
    else:
        chain_plant = None
    needs_zone = bool(definition.evidence_paths) or definition.label_rule is not None
    if needs_zone and (record_scope.plant_id is None or record_scope.zone_id is None):
        raise _reject(
            LedgerRejectionCode.CONTENT_INVALID,
            "/plant_id" if record_scope.plant_id is None else "/zone_id",
        )
    return record_scope, chain_plant


def _record_place(
    document: Mapping[str, Any], scope: RecordScope | None
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Planta y zona del registro: lo que lleva el contenido manda sobre ``scope``."""
    given = scope or RecordScope()
    plant_id = _uuid_or_none(document.get("plant_id")) or given.plant_id
    zone_id = _uuid_or_none(document.get("zone_id")) or given.zone_id
    return plant_id, zone_id


def _require_scope_within_context(
    context: ScopeContext, document: object, scope: RecordScope | None
) -> None:
    """Con un contexto de sesión, la planta y la zona del registro están en ``allowed_scopes``.

    Lo que lleva el contenido manda sobre ``scope`` (como en ``_resolve_scope``). Un registro sin
    planta ni zona (de organización) no se comprueba aquí; tampoco los contextos de evento,
    iteración u orden administrativa, que actúan sobre su organización entera. Fuera de alcance
    → ``context_absent`` con el puntero del campo, igual que otra organización.
    """
    if context.origin is not ContextOrigin.SESSION or not isinstance(document, Mapping):
        return
    plant_id, zone_id = _record_place(document, scope)
    if plant_id is None and zone_id is None:
        return
    if not context.covers(plant_id, zone_id):
        # Una planta cubierta cubre sus zonas: si falla, es la planta, salvo que el contexto
        # solo tenga zonas (entonces es la zona).
        zone_only = zone_id is not None and any(
            scope.scope_level is ScopeLevel.ZONE for scope in context.allowed_scopes
        )
        pointer = "/zone_id" if zone_only or plant_id is None else "/plant_id"
        raise _reject(LedgerRejectionCode.CONTEXT_ABSENT, pointer)


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@repository
class EscritorExpediente:
    """El puerto ``EscritorExpediente`` (business-logic-model §10.1) sobre PostgreSQL."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        registry: RecordTypeRegistry,
        free_text: FreeTextPolicyRegistry,
        evidence: EvidenceReferenceVerifier,
        outbox: OutboxPort,
        clock: Clock,
        cpu_pool: CpuPool | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._registry = registry
        self._free_text = free_text
        self._evidence = evidence
        self._outbox = outbox
        self._clock = clock
        self._cpu_pool = cpu_pool
        self._random_bytes = random_bytes

    @handles_absent_context
    async def write(
        self,
        context: ScopeContext | None,
        record_type: str,
        content: Mapping[str, Any] | BaseModel,
        *,
        scope: RecordScope | None = None,
        events: Sequence[NewEvent] = (),
        occurred_at: datetime | None = None,
        projection: Callable[[Transaction], Awaitable[None]] | None = None,
        transaction: Transaction | None = None,
    ) -> Receipt | LedgerRejection:
        """Escribe un registro; ``Receipt`` tras confirmar, o el rechazo del primer fallo.

        ``events`` son las cargas de los eventos que la escritura publica (cada nombre debe estar
        en ``outbox_events`` del tipo); el escritor les fija la planta de la cadena y la
        secuencia del registro. ``occurred_at`` es la marca del hecho para la línea de tiempo
        (fuera del sobre).

        ``projection`` es la proyección que el registro respalda (p. ej. la fila de
        ``identity.provider_concession``, domain-entities §2.11): corre dentro de la transacción
        del paso 7, antes del ``INSERT`` del registro, así que la fila y el registro se confirman
        o se revierten juntos. Solo corre si el registro se escribe (no en un rechazo ni en un
        ``accepted_duplicate``); lo que lance sale tal cual, con la transacción revertida.

        Con ``transaction`` (de la organización del contexto) el paso 7 va en la transacción del
        llamador: el registro solo existe si ella confirma, junto con lo que el llamador escribió
        (p. ej. la planta y su registro ``plant_created``, TASK-126). Los pasos 1 a 6 no cambian.
        El ``Receipt`` llega antes de confirmar y vale solo si el llamador confirma; un fallo de
        la escritura (cadena ocupada, disparador) sale como excepción y revierte su transacción.
        """
        if not isinstance(context, ScopeContext):
            report_context_absent("EscritorExpediente.write")
            return LedgerRejection.of(LedgerRejectionCode.CONTEXT_ABSENT)
        if occurred_at is not None and (
            not isinstance(occurred_at, datetime) or occurred_at.utcoffset() is None
        ):
            raise TypeError("occurred_at debe ser una marca con zona horaria")
        if transaction is not None and (
            not isinstance(transaction, Transaction)
            or transaction.context.organization_id != context.organization_id
        ):
            raise TypeError("transaction debe ser una Transaction de la organización del contexto")
        try:
            prepared = await self._prepare(
                context, record_type, content, scope, events, transaction
            )
        except _Rejected as rejected:
            return rejected.rejection
        if isinstance(prepared, Receipt):
            return prepared
        if transaction is not None:
            if projection is not None:
                await projection(transaction)
            record_id = uuid7(self._clock, self._random_bytes)
            inserted = await self._insert(transaction, context, prepared, record_id, occurred_at)
            return Receipt(
                record_id=inserted.record_id,
                received_at=inserted.received_at,
                status=AcceptanceStatus.ACCEPTED,
            )
        return await self._commit(context, prepared, occurred_at, projection)

    # --- pasos 1 a 6 ---------------------------------------------------------------------------

    async def _prepare(
        self,
        context: ScopeContext,
        record_type: str,
        content: object,
        scope: RecordScope | None,
        events: Sequence[NewEvent],
        transaction: Transaction | None = None,
    ) -> _Prepared | Receipt:
        document = _document(content)
        # (1) contexto de la organización del contenido.
        if isinstance(document, Mapping) and "organization_id" in document:
            organization = _uuid_or_none(document["organization_id"])
            if organization is not None and organization != context.organization_id:
                raise _reject(LedgerRejectionCode.CONTEXT_ABSENT, "/organization_id")
        # (1) y la planta o zona del registro, dentro del alcance de la sesión (BR-NUC-45). La
        # coherencia zona → planta, que consulta la base, va al empezar el paso 5.
        _require_scope_within_context(context, document, scope)
        # (2) tipo registrado y unidad autorizada.
        compiled = self._compiled(context, record_type)
        # (3) esquema y tamaño, con todo lo que se exige al contenido: la clave de idempotencia
        # (≤ 64 caracteres) y la regla de etiqueta, antes de cualquier consulta a la base.
        document, scope_resolved, chain_plant = await self._validate(compiled, document, scope)
        source_key = self._source_key(compiled, document)
        label = self._project_label(compiled, document)
        # (4) texto libre, sobre el documento ya válido: se guarda el texto en NFC. Las rutas de
        # la clave y de la etiqueta nunca son texto libre (el registro lo exige): no cambian.
        self._apply_free_text(compiled, document)
        content_bytes = await self._canonical(document)
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        # (1) con la base: la zona de una sesión existe y es de la planta del registro. Es la
        # primera consulta: los pasos 1 a 4 nunca la alcanzan con un contenido inválido.
        await self._require_zone_of_plant(context, document, scope, transaction)
        # (5) idempotencia.
        if source_key is not None:
            existing = await self._by_source_key(context, compiled, source_key)
            if existing is not None:
                return self._resolve_duplicate(existing, content_hash)
        # (6) evidencias.
        evidences = await self._verify_evidence(context, compiled, document, scope_resolved)
        # Preparación de la escritura: evidencias de la etiqueta y eventos, fuera de la transacción.
        label_evidence = await self._label_evidence(context, label)
        return _Prepared(
            compiled=compiled,
            content=content_bytes,
            content_hash=content_hash,
            chain_plant=chain_plant,
            scope=scope_resolved,
            source_key=source_key,
            evidences=evidences,
            label=label,
            label_evidence_ids=label_evidence,
            events=self._events(compiled, events),
        )

    async def _require_zone_of_plant(
        self,
        context: ScopeContext,
        document: object,
        scope: RecordScope | None,
        transaction: Transaction | None = None,
    ) -> None:
        """Con una sesión, la zona del registro existe y es de su planta (BR-NUC-45).

        ``AllowedScope.covers`` de zona no mira la planta: sin esta comprobación, quien tiene
        una zona escribiría con su zona y la planta de otro, y el registro entraría en la cadena
        de esa planta. Con alcance de planta pasa lo simétrico: quien tiene la planta P escribiría
        en la cadena de P un registro con una zona de otra planta (o inexistente), y quien tiene
        esa zona lo vería (seguimiento de VIG-73 cerrado en TASK-126). Por eso se consulta
        ``identity.zone`` siempre que una sesión escribe planta y zona, cubra lo que cubra. Con la
        transacción del llamador la consulta va en ella: ve la zona que el llamador acaba de crear
        (``zone_created``). Zona inexistente o de otra planta → ``context_absent`` en
        ``/plant_id`` (la pareja planta-zona no es coherente; el puntero es el de VIG-73).
        """
        if context.origin is not ContextOrigin.SESSION or not isinstance(document, Mapping):
            return
        plant_id, zone_id = _record_place(document, scope)
        if plant_id is None or zone_id is None:
            return
        parameters = {"organization_id": context.organization_id, "zone_id": zone_id}
        if transaction is not None:
            rows: Sequence[Row[Any]] = (await transaction.execute(_ZONE_PLANT, parameters)).all()
        else:
            rows = await self._database.read(context, _ZONE_PLANT, parameters)
        if not rows or rows[0].plant_id != plant_id:
            raise _reject(LedgerRejectionCode.CONTEXT_ABSENT, "/plant_id")

    def _compiled(self, context: ScopeContext, record_type: object) -> CompiledType:
        if not isinstance(record_type, str):
            raise _reject(LedgerRejectionCode.RECORD_TYPE_UNKNOWN)
        try:
            compiled = self._registry.get(record_type)
        except RecordTypeUnknown:
            raise _reject(LedgerRejectionCode.RECORD_TYPE_UNKNOWN) from None
        if compiled.writer_unit is not context.actor.unit:
            raise _reject(LedgerRejectionCode.RECORD_TYPE_UNKNOWN)
        return compiled

    async def _validate(
        self, compiled: CompiledType, document: object, scope: RecordScope | None
    ) -> tuple[Any, RecordScope, uuid.UUID | None]:
        invalid = LedgerRejectionCode.CONTENT_INVALID
        if not isinstance(document, Mapping):
            raise _reject(invalid)
        # Tope de 256 KB antes de serializar (acotado por el propio tope).
        if exceeds_canonical_size(cast(JsonValue, document), MAX_CONTENT_BYTES):
            raise _reject(invalid)
        if exceeds_canonical_size(cast(JsonValue, document), LARGE_DOCUMENT_BYTES):
            # Un contenido grande se valida en el pool de CPU, como se canonicaliza
            # (PAT-NUC-REN-05): cerca de 256 KB serían varios milisegundos en el bucle.
            pool = self._cpu_pool if self._cpu_pool is not None else get_cpu_pool()
            parsed = await pool.run(_schema_checked, compiled, document)
        else:
            parsed = _schema_checked(compiled, document)
        record_scope, chain_plant = _resolve_scope(parsed, scope, compiled)
        return parsed, record_scope, chain_plant

    def _apply_free_text(self, compiled: CompiledType, document: Any) -> None:
        for path in compiled.definition.free_text_paths:
            field = compiled.free_text_fields[path]
            for located in locate(document, path):
                if not isinstance(located.value, str):  # pragma: no cover - esquema
                    raise _reject(LedgerRejectionCode.CONTENT_INVALID, located.pointer)
                try:
                    normalized = self._free_text.apply(located.value, field)
                except FreeTextRejected:
                    raise _reject(LedgerRejectionCode.FREE_TEXT_REJECTED, located.pointer) from None
                if normalized != located.value:
                    replace_value(document, located.segments, normalized)

    async def _canonical(self, document: JsonValue) -> bytes:
        try:
            content = await canonical_bytes(document, pool=self._cpu_pool)
        except CanonicalFormError:
            raise _reject(LedgerRejectionCode.CONTENT_INVALID) from None
        if not 1 <= len(content) <= MAX_CONTENT_BYTES:
            raise _reject(LedgerRejectionCode.CONTENT_INVALID)
        return content

    @staticmethod
    def _source_key(compiled: CompiledType, document: Any) -> str | None:
        path = compiled.definition.source_key_path
        if path is None:
            return None
        found = locate(document, path)
        if len(found) != 1 or not isinstance(found[0].value, str):
            raise _reject(LedgerRejectionCode.CONTENT_INVALID, path)
        value: str = found[0].value
        if not 1 <= len(value) <= MAX_SOURCE_KEY_CHARS:
            raise _reject(LedgerRejectionCode.CONTENT_INVALID, found[0].pointer)
        return value

    async def _by_source_key(
        self, context: ScopeContext, compiled: CompiledType, source_key: str
    ) -> Row[Any] | None:
        rows = await self._database.read(
            context,
            _BY_SOURCE_KEY,
            {
                "organization_id": context.organization_id,
                "record_type": compiled.record_type,
                "source_key": source_key,
            },
        )
        return rows[0] if rows else None

    @staticmethod
    def _resolve_duplicate(existing: Row[Any], content_hash: str) -> Receipt:
        if existing.content_hash != content_hash:
            raise _reject(LedgerRejectionCode.IDEMPOTENCY_CONFLICT)
        return Receipt(
            record_id=existing.record_id,
            received_at=existing.received_at,
            status=AcceptanceStatus.ACCEPTED_DUPLICATE,
        )

    async def _verify_evidence(
        self,
        context: ScopeContext,
        compiled: CompiledType,
        document: Any,
        scope: RecordScope,
    ) -> tuple[_Evidence, ...]:
        located: list[Located] = []
        references: list[ClipReference] = []
        for path in compiled.definition.evidence_paths:
            for item in locate(document, path):
                try:
                    reference = ClipReference.__pydantic_validator__.validate_json(
                        json.dumps(item.value, ensure_ascii=False, allow_nan=False), strict=True
                    )
                except (ValidationError, TypeError, ValueError):
                    raise _reject(LedgerRejectionCode.CONTENT_INVALID, item.pointer) from None
                located.append(item)
                references.append(reference)
        if not references:
            return ()
        if scope.plant_id is None or scope.zone_id is None or scope.node_id is None:
            # Sin nodo la clave no puede ser la del registro: ningún objeto le corresponde.
            raise _reject(LedgerRejectionCode.EVIDENCE_MISSING, located[0].pointer)
        owner = EvidenceOwner(context.organization_id, scope.plant_id, scope.zone_id, scope.node_id)
        try:
            checks = await self._evidence.verify_references(owner, references)
        except StorageUnavailable as error:
            # La causa encadenada puede llevar la clave del objeto: no se propaga.
            raise StorageUnavailable(error.operation) from None
        failed = first_failure(checks)
        if failed is not None and failed.code is not None:
            index = next(i for i, check in enumerate(checks) if check is failed)
            raise _reject(_EVIDENCE_CODES[failed.code], located[index].pointer)
        return tuple(
            _Evidence(reference, check) for reference, check in zip(references, checks, strict=True)
        )

    @staticmethod
    def _project_label(compiled: CompiledType, document: Any) -> LabelProjection | None:
        rule = compiled.definition.label_rule
        if rule is None:
            return None
        try:
            return project(rule, document)
        except LabelRuleViolation as violation:
            raise _reject(LedgerRejectionCode.CONTENT_INVALID, violation.pointer) from None

    async def _label_evidence(
        self, context: ScopeContext, projection: LabelProjection | None
    ) -> tuple[uuid.UUID, ...]:
        if projection is None:
            return ()
        rows = await self._database.read(
            context,
            evidence_ids_statement(),
            {
                "organization_id": context.organization_id,
                "record_id": projection.subject_record_id,
            },
        )
        return tuple(row.evidence_id for row in rows)

    @staticmethod
    def _events(compiled: CompiledType, events: Sequence[NewEvent]) -> tuple[NewEvent, ...]:
        declared = compiled.definition.outbox_events
        for event in events:
            if not isinstance(event, NewEvent) or event.event_name not in declared:
                raise ValueError(
                    f"el tipo {compiled.record_type} no declara ese evento en outbox_events"
                )
        return tuple(events)

    # --- paso 7 --------------------------------------------------------------------------------

    async def _commit(
        self,
        context: ScopeContext,
        prepared: _Prepared,
        occurred_at: datetime | None,
        projection: Callable[[Transaction], Awaitable[None]] | None = None,
    ) -> Receipt | LedgerRejection:
        record_id = uuid7(self._clock, self._random_bytes)
        try:
            async with self._database.transaction(context) as transaction:
                if projection is not None:
                    await projection(transaction)
                inserted = await self._insert(
                    transaction, context, prepared, record_id, occurred_at
                )
        except ChainLockedTimeout:
            return LedgerRejection.of(LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT)
        except sa_exc.IntegrityError as error:
            if _is_chain_level_violation(error):
                # El registro de tipos y ``ledger.record_type`` no concuerdan en el nivel.
                _log.error(
                    "el disparador rechazó la cadena del registro",
                    record_type=prepared.compiled.record_type,
                )
                return LedgerRejection.of(LedgerRejectionCode.CONTENT_INVALID, "/plant_id")
            if prepared.source_key is None or not _is_source_key_violation(error):
                raise
            # Otra escritura con la misma clave confirmó antes: esta se revirtió entera.
            existing = await self._by_source_key(context, prepared.compiled, prepared.source_key)
            if existing is None:  # pragma: no cover - la clave existe si hubo violación
                raise
            try:
                return self._resolve_duplicate(existing, prepared.content_hash)
            except _Rejected as rejected:
                return rejected.rejection
        return Receipt(
            record_id=inserted.record_id,
            received_at=inserted.received_at,
            status=AcceptanceStatus.ACCEPTED,
        )

    async def _insert(
        self,
        transaction: Transaction,
        context: ScopeContext,
        prepared: _Prepared,
        record_id: uuid.UUID,
        occurred_at: datetime | None,
    ) -> Row[Any]:
        actor = context.actor
        compiled = prepared.compiled
        scope = prepared.scope
        result = await transaction.execute(
            _INSERT_RECORD,
            {
                "record_id": record_id,
                "organization_id": context.organization_id,
                "plant_id": prepared.chain_plant,
                "record_type": compiled.record_type,
                "schema_version": compiled.schema_version,
                "actor_kind": actor.kind.value,
                "actor_id": actor.id,
                "actor_display_name_snapshot": actor.display_name_snapshot,
                "actor_role_in_use": None if actor.role_in_use is None else actor.role_in_use.value,
                "actor_concession_id": actor.concession_id,
                "actor_unit": actor.unit.value,
                "scope_plant_id": scope.plant_id,
                "scope_zone_id": scope.zone_id,
                "scope_node_id": scope.node_id,
                "correlation_id": context.correlation_id,
                "occurred_at": occurred_at,
                "source_key": prepared.source_key,
                "content": prepared.content,
            },
        )
        inserted = result.one()
        for evidence in prepared.evidences:
            await self._insert_evidence(transaction, context, scope, record_id, evidence)
        # ``_resolve_scope`` ya exigió planta y zona a todo tipo con regla de etiqueta.
        if prepared.label is not None and scope.plant_id is not None and scope.zone_id is not None:
            await insert_label(
                transaction,
                prepared.label,
                label_id=uuid7(self._clock, self._random_bytes),
                plant_id=scope.plant_id,
                zone_id=scope.zone_id,
                source_record_id=record_id,
                labeled_at=inserted.received_at,
                evidence_ids=prepared.label_evidence_ids,
            )
        for event in prepared.events:
            await self._outbox.publish(
                transaction,
                replace(
                    event,
                    plant_id=prepared.chain_plant,
                    ledger_sequence=int(inserted.chain_sequence),
                ),
            )
        return inserted

    async def _insert_evidence(
        self,
        transaction: Transaction,
        context: ScopeContext,
        scope: RecordScope,
        record_id: uuid.UUID,
        evidence: _Evidence,
    ) -> None:
        reference = evidence.reference
        await transaction.execute(
            _INSERT_EVIDENCE,
            {
                "evidence_id": uuid7(self._clock, self._random_bytes),
                "organization_id": context.organization_id,
                "plant_id": scope.plant_id,
                "zone_id": scope.zone_id,
                "node_id": scope.node_id,
                "record_id": record_id,
                "clip_id": uuid.UUID(reference.clip_id),
                "camera_id": uuid.UUID(reference.camera_id),
                "storage_key": reference.storage_key,
                "sha256": reference.sha256,
                "size_bytes": reference.size_bytes,
                "content_type": _value(reference.content_type),
                "media_kind": _value(reference.media_kind),
                "duration_ms": reference.duration_ms,
                "segment": _value(reference.segment),
                "starts_at": _parse_timestamp(reference.starts_at),
                "ends_at": _parse_timestamp(reference.ends_at),
                "verified_at": evidence.check.verified_at,
            },
        )


def _value(member: object) -> str:
    value = getattr(member, "value", member)
    return str(value)


def violated_constraint(error: sa_exc.IntegrityError, sqlstate: str) -> str | None:
    """Nombre de la restricción que violó ``error`` con ``sqlstate``, o ``None``."""
    candidates: list[object] = [error, error.orig, getattr(error.orig, "__cause__", None)]
    for candidate in candidates:
        if getattr(candidate, "sqlstate", None) != sqlstate:
            continue
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


def _is_source_key_violation(error: sa_exc.IntegrityError) -> bool:
    """La violación de unicidad es la de ``ledger.record_source_key`` (misma clave)."""
    return violated_constraint(error, _UNIQUE_VIOLATION) == _SOURCE_KEY_CONSTRAINT


def _is_chain_level_violation(error: sa_exc.IntegrityError) -> bool:
    """El disparador rechazó la planta de la cadena para el nivel del tipo (BR-NUC-45)."""
    return violated_constraint(error, CHECK_VIOLATION) == CHAIN_LEVEL_CONSTRAINT
