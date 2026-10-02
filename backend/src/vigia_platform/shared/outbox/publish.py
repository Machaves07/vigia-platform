"""Publicación en la bandeja de salida (LC-NUC-23, parte 1; BR-NUC-75, BR-NUC-83; PR-NUC-32).

``OutboxPort.publish(transaction, event)`` recibe la **transacción del cambio** que produce el
evento (``shared.db.Transaction``) y en ella inserta el ``OutboxEvent`` y una ``OutboxDelivery``
``pending`` por consumidor suscrito. No abre ni confirma nada: si esa transacción se revierte, el
evento no existe (PR-NUC-32). El puerto no expone su implementación (BR-NUC-83): quien publica
solo ve ``NewEvent`` y ``Publication``.

Antes de la primera sentencia se valida todo, en este orden, y cualquier fallo lanza
``OutboxRejected`` sin tocar la base:

1. el catálogo está sellado (``outbox_not_ready``: se publica después de arrancar);
2. el nombre está registrado en ``EventType`` (``event_type_unknown``);
3. la partición: ``plant_id`` es un ``UUID`` o nada (``partition_invalid``); la clave es
   ``organization_id:plant_id`` o ``organization_id:organization``, y la organización es siempre
   la del contexto de la transacción (bajo concesión, la del cliente);
4. ``ledger_sequence``, si va, es un entero de 1 a 2^63 - 1 (``ledger_sequence_invalid``);
5. la carga cabe en 64 KB (``payload_too_large``) y valida contra el modelo estricto de su tipo
   (``payload_invalid``): solo identificadores, enumeraciones y marcas, sin campos de más ni
   coerción. El tamaño se mide sobre el texto que guarda PostgreSQL (``payload::text``, con los
   separadores ``", "`` y ``": "`` de ``jsonb``), el mismo que limita la restricción de la tabla.

``event_id`` es un UUID v7 y ``created_at`` la hora del ``Clock`` inyectado, en milisegundos,
pero nunca anterior al ``created_at`` más reciente de su partición: el despacho ordena por
``created_at`` y, con varias instancias, un reloj atrasado no debe colocar un evento delante de
otro ya confirmado (el empate lo deshacen ``ledger_sequence`` y ``publish_seq``). Un reloj muy
adelantado arrastra la hora de los siguientes; no se corrige (el orden manda) y, por encima de
``CLOCK_SKEW_WARNING``, se registra. La entrega nace
vencida (``next_attempt_at = created_at``). ``correlation_id`` sale del contexto, y
``trace_id``/``span_id`` del tramo de OpenTelemetry en curso, si lo hay: el despachador enlaza
con él el tramo de la entrega (PAT-NUC-MAN-01). ``publish_seq`` lo asigna la base al insertar.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from opentelemetry import trace as otel_trace
from pydantic import BaseModel, ValidationError
from sqlalchemy import text

from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ContextAbsent, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.registries import CompiledEventType, OutboxCatalog

__all__ = [
    "MAX_LEDGER_SEQUENCE",
    "MAX_PAYLOAD_BYTES",
    "NewEvent",
    "Outbox",
    "OutboxEvent",
    "OutboxPort",
    "OutboxRejected",
    "Publication",
    "partition_key",
    "stored_payload_size",
]

MAX_PAYLOAD_BYTES: Final = 64 * 1024
"""Tope de la carga (domain-entities §4.3): bytes UTF-8 de ``payload::text``."""

MAX_LEDGER_SEQUENCE: Final = 2**63 - 1

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

CLOCK_SKEW_WARNING: Final = timedelta(seconds=5)
"""Si ``created_at`` sube más que esto por encima del reloj propio, otra instancia escribió con un
reloj adelantado: se registra para que operación lo vea ``[objetivo propio]``. El orden manda
sobre la exactitud de la hora y no se corrige: bajar la marca pondría el evento delante."""

_log = get_logger("shared.outbox")

_ORGANIZATION_PARTITION: Final = "organization"

_INSERT_EVENT: Final = text(
    "INSERT INTO shared.outbox_event (event_id, organization_id, plant_id, event_name,"
    " ledger_sequence, payload, correlation_id, created_at, trace_id, span_id)"
    " VALUES (:event_id, :organization_id, :plant_id, :event_name, :ledger_sequence,"
    " CAST(:payload AS jsonb), :correlation_id,"
    " GREATEST(CAST(:created_at AS timestamptz), (SELECT max(previous.created_at)"
    " FROM shared.outbox_event AS previous WHERE previous.organization_id = :organization_id"
    " AND previous.partition_key = :partition_key)),"
    " :trace_id, :span_id)"
    " RETURNING created_at"
)

_INSERT_DELIVERIES: Final = text(
    "INSERT INTO shared.outbox_delivery (event_id, consumer_name, organization_id, status,"
    " attempts, next_attempt_at)"
    " SELECT :event_id, consumer_name, :organization_id, 'pending', 0, :created_at"
    " FROM unnest(CAST(:consumers AS text[])) AS consumer_name"
)


class OutboxRejected(Exception):
    """La publicación no se hace; nada se insertó. ``code`` es una lista cerrada."""

    def __init__(self, code: str, event_name: object, detail: str) -> None:
        self.code = code
        self.event_name = event_name if isinstance(event_name, str) else repr(event_name)
        self.detail = detail
        super().__init__(f"{code}: evento «{self.event_name}»: {detail}")


@dataclass(frozen=True, kw_only=True)
class NewEvent:
    """Lo que publica una unidad: el resto (organización, identificador, hora) lo pone el puerto."""

    event_name: str
    payload: Mapping[str, Any] | BaseModel
    plant_id: uuid.UUID | None = None
    ledger_sequence: int | None = None


@dataclass(frozen=True, kw_only=True)
class OutboxEvent:
    """Un evento de la bandeja tal como queda persistido (domain-entities §4.3)."""

    event_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID | None
    event_name: str
    partition_key: str
    ledger_sequence: int | None
    payload: Mapping[str, Any]
    correlation_id: uuid.UUID
    created_at: datetime
    trace_id: str | None = None
    """Traza W3C (32 hexadecimales) del tramo que publicó, o nada."""
    span_id: str | None = None
    """Tramo W3C (16 hexadecimales) que publicó, o nada."""


@dataclass(frozen=True)
class Publication:
    """El evento insertado y los consumidores a los que quedó una entrega ``pending``."""

    event: OutboxEvent
    consumers: tuple[str, ...]


class OutboxPort(Protocol):
    """Puerto de la bandeja (business-logic-model §10.1); el reproceso es ``outbox.replay``."""

    async def publish(self, transaction: Transaction, event: NewEvent) -> Publication: ...


def partition_key(organization_id: uuid.UUID, plant_id: uuid.UUID | None) -> str:
    """La misma que genera la columna ``shared.outbox_event.partition_key``."""
    return f"{organization_id}:{_ORGANIZATION_PARTITION if plant_id is None else plant_id}"


def stored_payload_size(document: Mapping[str, Any]) -> int:
    """Bytes de ``payload::text`` en PostgreSQL: ``jsonb`` separa con ``", "`` y ``": "`` y no
    escapa lo que no es ASCII."""
    return len(
        json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(", ", ": ")).encode()
    )


def _current_trace() -> tuple[str | None, str | None]:
    """``(trace_id, span_id)`` del tramo en curso en hexadecimal W3C, o ``(None, None)``."""
    span_context = otel_trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None, None
    return f"{span_context.trace_id:032x}", f"{span_context.span_id:016x}"


def _uuid7(milliseconds: int, random: bytes) -> uuid.UUID:
    """UUID v7 (RFC 9562): 48 bits de milisegundos, versión, 74 bits aleatorios y variante."""
    rand_a = int.from_bytes(random[:2], "big") & 0x0FFF
    rand_b = int.from_bytes(random[2:10], "big") & ((1 << 62) - 1)
    value = (milliseconds & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return uuid.UUID(int=value)


def _payload_text(event_name: str, payload: object) -> str:
    """El JSON compacto de la carga, sin validar todavía; rechaza lo que no es JSON."""
    try:
        if isinstance(payload, BaseModel):
            return payload.model_dump_json()
        if not isinstance(payload, Mapping):
            raise TypeError("la carga debe ser un objeto")
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, OverflowError) as error:
        raise OutboxRejected(
            "payload_invalid", event_name, f"la carga no es un objeto JSON ({type(error).__name__})"
        ) from None


def _validated_payload(compiled: CompiledEventType, payload: object) -> dict[str, Any]:
    name = compiled.event_name
    document = _payload_text(name, payload)
    # El texto compacto nunca es más largo que el de jsonb: pasado el tope, no se valida.
    if len(document.encode()) > MAX_PAYLOAD_BYTES:
        raise OutboxRejected("payload_too_large", name, f"la carga supera {MAX_PAYLOAD_BYTES} B")
    try:
        model = compiled.payload_model.model_validate_json(document, strict=True)
    except ValidationError as error:
        errors = error.errors(include_input=False, include_url=False)
        first: Mapping[str, Any] = errors[0] if errors else {}
        location = "/" + "/".join(str(part) for part in first.get("loc", ()))
        raise OutboxRejected(
            "payload_invalid", name, f"{first.get('type', 'invalid')} en {location}"
        ) from None
    except (ValueError, RecursionError) as error:
        raise OutboxRejected(
            "payload_invalid", name, f"la carga no valida ({type(error).__name__})"
        ) from None
    validated: dict[str, Any] = model.model_dump(mode="json")
    if stored_payload_size(validated) > MAX_PAYLOAD_BYTES:
        raise OutboxRejected("payload_too_large", name, f"la carga supera {MAX_PAYLOAD_BYTES} B")
    return validated


def _check_partition(name: str, event: NewEvent) -> None:
    if event.plant_id is not None and type(event.plant_id) is not uuid.UUID:
        raise OutboxRejected("partition_invalid", name, "plant_id debe ser uuid.UUID o None")
    sequence: object = event.ledger_sequence
    if sequence is not None and (
        type(sequence) is not int or not 1 <= sequence <= MAX_LEDGER_SEQUENCE
    ):
        raise OutboxRejected(
            "ledger_sequence_invalid", name, f"ledger_sequence debe ir de 1 a {MAX_LEDGER_SEQUENCE}"
        )


@repository
class Outbox:
    """Implementación de ``OutboxPort`` sobre ``shared.outbox_event`` y ``outbox_delivery``."""

    def __init__(
        self,
        catalog: OutboxCatalog,
        clock: Clock,
        *,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._catalog = catalog
        self._clock = clock
        self._random_bytes = random_bytes

    def prepare(self, transaction: Transaction, event: NewEvent) -> Publication:
        """Valida y construye lo que ``publish`` insertará, sin tocar la base."""
        if not isinstance(transaction, Transaction):
            # Sin la transacción del cambio no hay contexto ni atomicidad (BR-NUC-02, 75).
            raise ContextAbsent()
        if not isinstance(event, NewEvent):
            raise OutboxRejected("payload_invalid", "?", "el evento debe ser un NewEvent")
        name: object = event.event_name
        if not self._catalog.sealed:
            raise OutboxRejected(
                "outbox_not_ready", name, "el catálogo no está sincronizado ni sellado"
            )
        compiled = self._catalog.event_types.get(name) if isinstance(name, str) else None
        if compiled is None:
            raise OutboxRejected("event_type_unknown", name, "evento no registrado en EventType")
        _check_partition(compiled.event_name, event)
        payload = _validated_payload(compiled, event.payload)
        context = transaction.context
        now = self._clock.now()
        if now.tzinfo is None:
            raise ValueError("el reloj debe devolver una hora con zona")
        now = now.astimezone(UTC)
        created_at = now.replace(microsecond=now.microsecond // 1000 * 1000)
        milliseconds = (created_at - _EPOCH) // timedelta(milliseconds=1)
        trace_id, span_id = _current_trace()
        outbox_event = OutboxEvent(
            event_id=_uuid7(milliseconds, self._random_bytes(10)),
            organization_id=context.organization_id,
            plant_id=event.plant_id,
            event_name=compiled.event_name,
            partition_key=partition_key(context.organization_id, event.plant_id),
            ledger_sequence=event.ledger_sequence,
            payload=payload,
            correlation_id=context.correlation_id,
            created_at=created_at,
            trace_id=trace_id,
            span_id=span_id,
        )
        return Publication(outbox_event, self._catalog.consumers.subscribers(compiled.event_name))

    async def publish(self, transaction: Transaction, event: NewEvent) -> Publication:
        """Inserta el evento y sus entregas en ``transaction``; no confirma."""
        publication = self.prepare(transaction, event)
        inserted = publication.event
        result = await transaction.execute(
            _INSERT_EVENT,
            {
                "event_id": inserted.event_id,
                "organization_id": inserted.organization_id,
                "partition_key": inserted.partition_key,
                "plant_id": inserted.plant_id,
                "event_name": inserted.event_name,
                "ledger_sequence": inserted.ledger_sequence,
                "payload": json.dumps(inserted.payload, ensure_ascii=False, allow_nan=False),
                "correlation_id": inserted.correlation_id,
                "created_at": inserted.created_at,
                "trace_id": inserted.trace_id,
                "span_id": inserted.span_id,
            },
        )
        created_at: datetime = result.scalar_one()
        if created_at != inserted.created_at:
            if created_at - inserted.created_at > CLOCK_SKEW_WARNING:
                _log.warning("reloj desfasado entre instancias: el evento hereda otra hora")
            inserted = replace(inserted, created_at=created_at)
            publication = Publication(inserted, publication.consumers)
        if publication.consumers:
            await transaction.execute(
                _INSERT_DELIVERIES,
                {
                    "event_id": inserted.event_id,
                    "organization_id": inserted.organization_id,
                    "created_at": inserted.created_at,
                    "consumers": list(publication.consumers),
                },
            )
        return publication
