"""``fleet.ingest``: hallazgos, detecciones para revisión y eventos de observabilidad (LC-GOB-12).

S-PLA-07 (BL §2.4), BR-GOB-83 a 96 y PR-GOB-01, 02, 04, 06, 07 y 17. Las rutas del contrato
(``node_api.routes.findings``, ``detection_reviews`` y ``observability_events``) llegan aquí con la
verificación previa común de ``node_api`` hecha: (1) versión, (2) certificado y alcance, (3) tamaño
y (4) esquema con los modelos generados de U-01. Este servicio completa:

- **paso 2, parte del cuerpo** (``check_body_scope``, antes del esquema para que el paso 2 gane al
  4): organización, planta y nodo del cuerpo iguales a los del certificado, y la zona **asignada
  al nodo en el instante del hecho** (``node_time.started_at``, o ``received_at`` sin reloj
  sincronizado; BR-GOB-88) → ``node_zone_mismatch``. Si esos campos no se pueden leer, decide el
  paso 4;
- **paso 4, lo que no expresa el esquema** (como la plataforma simulada de U-01):
  ``contract_version`` del cuerpo igual a la cabecera y ``Idempotency-Key`` igual al identificador
  del registro (BR-CTR-26) → ``schema_invalid``;
- **paso 5** (``accept``): el registro guardado lleva el ``receipt`` dentro (el contenido de los
  tres tipos es el modelo del contrato **con** recibo, TASK-204), así que cada intento llevaría un
  recibo distinto y el hash del escritor nunca coincidiría. La ingesta resuelve la idempotencia
  comparando el registro aceptado **sin** recibo con la presentación (``same_submission``): igual
  → ``accepted_duplicate`` con el ``Receipt`` original; distinto → ``idempotency_conflict``
  (BR-GOB-89). Si dos envíos simultáneos pasan los dos, el escritor rechaza el segundo por la
  unicidad de la clave y se vuelve a resolver igual;
- **paso 6** y **paso 9**: ``EscritorExpediente.write`` (validación, canonicalización y
  ``head_object`` de los clips antes de abrir la transacción, PAT-NUC-RES-08), con
  ``record_id`` = ``platform_record_id`` del recibo y ``occurred_at`` = ``node_time.started_at``;
- **pasos 7 y 8**, en la ``projection`` del escritor: dentro de la transacción y **antes** del
  ``INSERT`` del registro, así que van después de los clips y nada se escribe si fallan. (7)
  antigüedad (``timestamp_out_of_window``) y, en hallazgos y detecciones, el estándar vigente en la
  ventana del hecho con umbrales coherentes (``schema_invalid`` con ``field`` del estándar); (8)
  compuerta de uso aprobada en la ventana ``[t - tol, t + tol]`` (``zone_gate_not_approved``). Los
  eventos de observabilidad se aceptan en cualquier modo (BR-GOB-92);
- **proyecciones** en la misma transacción: las ``ClipUploadGrant`` ``evidence`` citadas pasan
  ``issued → used`` (la marca que ``mark_orphan_clips`` de TASK-222 respeta, BR-GOB-94) y un cierre
  cuyo ``opened_event_id`` no está aceptado queda en ``fleet.observability_orphan_close`` (cierre
  huérfano: se acepta, nunca se rechaza);
- **eventos** en la misma transacción (``events`` del escritor; T-08): ``finding_received``,
  ``detection_for_review_received`` (D-3, BR-GOB-95: se escribe y se publica aunque no haya
  consumidores) y ``observability_event_received``.

**Rechazos** (``record_rejection``, que ``node_api`` llama con el código ya traducido): todo
rechazo **permanente** deja su ``AuditEntry`` ``ingest_rejected`` (código, nodo, zona y
``correlation_id``; BR-GOB-96, PAT-GOB-SEG-08) y, con ``zone_gate_not_approved`` o
``node_zone_mismatch``, además el registro ``ingest_rejected`` en la cadena de la planta del
certificado, en la misma transacción: solo identificadores y código, **nunca** el contenido, y
``zone_id`` solo si la zona es de la organización del certificado. Un fallo de infraestructura es
transitorio y sin reintento propio (``storage_unavailable``, ``temporarily_unavailable``); nunca
hay aceptación optimista ni parcial.

**Orden de los candados** de una aceptación (los toma la transacción del escritor, en este orden):
(1) las filas de ``fleet.clip_upload_grant`` citadas, por ``clip_id``; (2) la fila del cierre
huérfano (``ON CONFLICT DO NOTHING`` sobre su clave); (3) la cabeza de la cadena de la planta (el
disparador del ``INSERT``). ``ingest_rejected`` solo toma la (3), y ``mark_orphan_clips`` solo la
(1), con el mismo orden de ``clip_id``.

Ningún paso lee la hora del sistema: ``received_at`` llega de la verificación previa (``Clock``).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from opentelemetry import trace as otel_trace
from vigia_contracts.models._base import ContractModel
from vigia_contracts.models.enumerations import AcceptanceStatus, RejectionCode
from vigia_contracts.models.receipt import Receipt as ContractReceipt

from vigia_platform.fleet.adapters.postgres.ingest_queries import AcceptedRecord
from vigia_platform.fleet.application.common import FleetWriteFailed
from vigia_platform.fleet.domain.clock_tolerance import FactTime, Window
from vigia_platform.fleet.domain.ingest_order import (
    RECEIPT_FIELD,
    AssignmentSpan,
    CatalogVersionView,
    GateSpan,
    IngestKind,
    assigned_at,
    catalog_violation,
    cited_clip_ids,
    same_submission,
    usage_approved_during,
)
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import (
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
    to_contract_rejection,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.tracing import TRACER_NAME, span_name
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "INGEST_REJECTED_RECORD_TYPE",
    "TRANSIENT_CODES",
    "IngestAudit",
    "IngestDependencies",
    "IngestRejected",
    "IngestReply",
    "IngestService",
    "IngestStore",
    "IngestWriter",
]

INGEST_REJECTED_RECORD_TYPE: Final = "ingest_rejected"
RECORDED_CODES: Final = frozenset(
    {RejectionCode.ZONE_GATE_NOT_APPROVED, RejectionCode.NODE_ZONE_MISMATCH}
)
"""Los dos rechazos que dejan además el registro ``ingest_rejected`` (respuesta 17, BR-GOB-96)."""
TRANSIENT_CODES: Final = frozenset(
    {
        RejectionCode.TEMPORARILY_UNAVAILABLE,
        RejectionCode.RATE_LIMITED,
        RejectionCode.STORAGE_UNAVAILABLE,
    }
)
"""Los transitorios de BR-CTR-30: nunca se auditan como rechazo."""
REVIEW_REASON: Final = "low_confidence"
"""``review_reason`` de ``detection_for_review_received``: el contrato define la detección para
revisión por su banda de confianza (``review ≤ max_confidence < publication``, BR-CTR-10) y no
lleva motivo propio; la ingesta publica ``low_confidence`` (decisión declarada en TASK-221)."""

SCOPE_SPAN: Final = span_name("fleet.ingest.scope")
IDEMPOTENCY_SPAN: Final = span_name("fleet.ingest.idempotency")
LEDGER_SPAN: Final = span_name("fleet.ingest.ledger")
CATALOG_SPAN: Final = span_name("fleet.ingest.catalog")
GATE_SPAN: Final = span_name("fleet.ingest.gate")
REJECTION_SPAN: Final = span_name("fleet.ingest.rejection")

_TIMESTAMP: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_SCOPE_FIELDS: Final = ("organization_id", "plant_id", "node_id")


# --- Errores y puertos ---------------------------------------------------------------------------


class IngestRejected(Exception):
    """Un rechazo del contrato que decide la ingesta (``node_api`` lo traduce tal cual).

    ``zone_id`` es la zona que nombra la presentación, si se pudo leer: la auditoría la registra
    solo si es de la organización del certificado. Nunca lleva contenido.
    """

    def __init__(
        self,
        code: RejectionCode,
        *,
        field: str | None = None,
        body_level: bool = False,
        zone_id: uuid.UUID | None = None,
    ) -> None:
        code = RejectionCode(code)
        super().__init__(code.value)
        self.code = code
        self.field = field
        self.body_level = body_level
        self.zone_id = zone_id

    def __repr__(self) -> str:
        return f"IngestRejected({self.code.value!r}, field={self.field!r})"


class IngestStore(Protocol):
    """Las consultas y proyecciones de la ingesta (``PostgresIngestStore``)."""

    async def assignments(
        self, context: ScopeContext, node_id: uuid.UUID, zone_id: uuid.UUID, at: datetime
    ) -> Sequence[AssignmentSpan]: ...

    async def zone_in_organization(self, context: ScopeContext, zone_id: uuid.UUID) -> bool: ...

    async def accepted(
        self, context: ScopeContext, kind: IngestKind, source_key: str
    ) -> AcceptedRecord | None: ...

    async def retention_days(self, transaction: Transaction, node_id: uuid.UUID) -> int | None: ...

    async def catalog_versions(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> Sequence[CatalogVersionView]: ...

    async def usage_spans(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> Sequence[GateSpan]: ...

    async def event_accepted(self, transaction: Transaction, event_id: str) -> bool: ...

    async def mark_orphan_close(
        self,
        transaction: Transaction,
        *,
        event_id: uuid.UUID,
        opened_event_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        node_id: uuid.UUID,
        ledger_record_id: uuid.UUID,
        received_at: datetime,
    ) -> None: ...

    async def mark_cited(
        self,
        transaction: Transaction,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        node_id: uuid.UUID,
        clip_ids: Sequence[uuid.UUID],
        now: datetime,
    ) -> Sequence[uuid.UUID]: ...


class IngestWriter(Protocol):
    """``EscritorExpediente.write`` (LC-NUC-10)."""

    async def write(
        self,
        context: ScopeContext | None,
        record_type: str,
        content: Mapping[str, Any],
        *,
        scope: RecordScope | None = None,
        events: Sequence[NewEvent] = (),
        occurred_at: datetime | None = None,
        projection: Any = None,
        transaction: Transaction | None = None,
        record_id: uuid.UUID | None = None,
    ) -> Receipt | LedgerRejection: ...


class IngestAudit(Protocol):
    """``AuditWriter.append`` de U-02."""

    async def append(
        self,
        context: ScopeContext,
        operation: AuditOperation | str,
        *,
        outcome: AuditOutcome | str = AuditOutcome.SUCCESS,
        plant_id: uuid.UUID | None = None,
        zone_id: uuid.UUID | None = None,
        resource: ResourceRef | None = None,
        filters: Mapping[str, Any] | None = None,
        result_count: int | None = None,
        transaction: Transaction | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestDependencies:
    """Lo que recibe el servicio de la ingesta (lo construye la raíz de composición)."""

    database: LedgerDatabase
    writer: IngestWriter
    audit: IngestAudit
    clock: Clock
    store: IngestStore
    tracer: otel_trace.Tracer = field(default_factory=lambda: otel_trace.get_tracer(TRACER_NAME))


@dataclass(frozen=True, slots=True)
class IngestReply:
    """El ``Receipt`` del contrato que recibe el nodo y si fue un duplicado."""

    receipt: ContractReceipt
    duplicate: bool


# --- Lectura suelta del cuerpo (paso 2, antes del esquema) ---------------------------------------


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _fact_instant(document: Mapping[str, Any], received_at: datetime) -> datetime | None:
    """El instante en que se mira la asignación, o ``None`` si no se puede leer (decide el 4)."""
    node_time = document.get("node_time")
    if not isinstance(node_time, Mapping):
        return None
    clock = node_time.get("clock")
    synchronized = clock.get("synchronized") if isinstance(clock, Mapping) else None
    started = _instant(node_time.get("started_at"))
    if type(synchronized) is not bool or started is None:
        return None
    return started if synchronized else to_millisecond(received_at)


def _fact(document: Mapping[str, Any], received_at: datetime) -> FactTime:
    """El ``FactTime`` de un documento ya validado con el modelo estricto."""
    node_time = document["node_time"]
    clock = node_time["clock"]
    return FactTime(
        started_at=datetime.fromisoformat(node_time["started_at"]),
        ended_at=datetime.fromisoformat(node_time["ended_at"]),
        synchronized=bool(clock["synchronized"]),
        offset_ms=int(clock["offset_ms"]),
        received_at=to_millisecond(received_at),
    )


def _receipt(document: Mapping[str, Any]) -> ContractReceipt:
    """El ``Receipt`` del contrato, validado con el lector estricto (forma de transporte)."""
    return ContractReceipt.model_validate_json(json.dumps(dict(document)))


# --- Servicio ------------------------------------------------------------------------------------


class IngestService:
    """``fleet.ingest`` (LC-GOB-12): lo que ``node_api`` orquesta en las tres rutas."""

    def __init__(self, deps: IngestDependencies) -> None:
        self._deps = deps

    def __repr__(self) -> str:
        return "IngestService()"

    # --- Paso 2: la parte del alcance que depende del cuerpo ---------------------------------

    async def check_body_scope(
        self, kind: IngestKind, node: NodeScope, document: object, received_at: datetime
    ) -> None:
        """Organización, planta, nodo y zona en el instante del hecho, sobre el JSON sin validar.

        ``IngestRejected(node_zone_mismatch)`` si no corresponden al certificado; un campo que no
        se puede leer lo rechaza después el esquema (paso 4).
        """
        if not isinstance(node, NodeScope) or not isinstance(document, Mapping):
            return
        zone_id = _uuid(document.get("zone_id"))
        expected = {
            "organization_id": node.organization_id,
            "plant_id": node.plant_id,
            "node_id": node.node_id,
        }
        for name in _SCOPE_FIELDS:
            presented = _uuid(document.get(name))
            if presented is not None and presented != expected[name]:
                raise IngestRejected(RejectionCode.NODE_ZONE_MISMATCH, field=name, zone_id=zone_id)
        at = _fact_instant(document, received_at)
        if zone_id is None or at is None:
            return
        with self._deps.tracer.start_as_current_span(
            SCOPE_SPAN, attributes=self._attributes(node, zone_id)
        ):
            spans = await self._deps.store.assignments(node.context, node.node_id, zone_id, at)
        if not assigned_at(spans, zone_id, at):
            raise IngestRejected(RejectionCode.NODE_ZONE_MISMATCH, field="zone_id", zone_id=zone_id)

    # --- Pasos 4 (resto) a 9 -----------------------------------------------------------------

    async def accept(
        self,
        kind: IngestKind,
        node: NodeScope,
        document: ContractModel,
        *,
        received_at: datetime,
        idempotency_key: str | None,
        contract_version: str | None,
    ) -> IngestReply:
        """Acepta la presentación ``document`` de ``node`` o lanza el primer rechazo."""
        if not isinstance(node, NodeScope) or not isinstance(document, ContractModel):
            raise TypeError("accept recibe el alcance del nodo y la presentación del contrato")
        kind = IngestKind(kind)
        submission: dict[str, Any] = document.model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        zone_id = uuid.UUID(submission["zone_id"])
        # (4) lo que el esquema no expresa: versión del cuerpo y clave de idempotencia.
        if submission.get("contract_version") != contract_version:
            raise IngestRejected(
                RejectionCode.SCHEMA_INVALID, field="contract_version", zone_id=zone_id
            )
        source_key = str(submission[kind.id_field])
        if idempotency_key != source_key:
            raise IngestRejected(
                RejectionCode.SCHEMA_INVALID,
                field="Idempotency-Key",
                body_level=True,
                zone_id=zone_id,
            )
        fact = _fact(submission, received_at)
        # (5) idempotencia: el registro aceptado sin su recibo frente a la presentación.
        with self._deps.tracer.start_as_current_span(
            IDEMPOTENCY_SPAN, attributes=self._attributes(node, zone_id)
        ):
            existing = await self._deps.store.accepted(node.context, kind, source_key)
        if existing is not None:
            return self._duplicate(kind, existing, submission, zone_id)
        record_id = uuid7(self._deps.clock)
        receipt = {
            "platform_record_id": str(record_id),
            "received_at": format_timestamp(fact.received_at),
            "status": AcceptanceStatus.ACCEPTED.value,
        }
        event = NewEvent(
            event_name=kind.event_name,
            payload=_event_payload(kind, submission, record_id, receipt["received_at"]),
        )

        async def project(transaction: Transaction) -> None:
            await self._checks_and_projections(
                transaction, kind, node, zone_id, submission, fact, record_id
            )

        # (6) clips y (9) escritura, con (7) y (8) en la proyección, antes del INSERT.
        with self._deps.tracer.start_as_current_span(
            LEDGER_SPAN, attributes=self._attributes(node, zone_id)
        ):
            written = await self._deps.writer.write(
                node.context,
                kind.record_type,
                {**submission, RECEIPT_FIELD: receipt},
                scope=RecordScope(plant_id=node.plant_id, zone_id=zone_id, node_id=node.node_id),
                events=(event,),
                occurred_at=fact.started_at,
                projection=project,
                record_id=record_id,
            )
        if isinstance(written, Receipt) and written.status is AcceptanceStatus.ACCEPTED:
            return IngestReply(_receipt(receipt), duplicate=False)
        if isinstance(written, Receipt) or written.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT:
            # Otra petición con la misma clave confirmó antes: se resuelve como en el paso 5.
            existing = await self._deps.store.accepted(node.context, kind, source_key)
            if existing is None:  # pragma: no cover - la clave existe si el escritor la vio
                raise RuntimeError("clave de idempotencia sin registro")
            return self._duplicate(kind, existing, submission, zone_id)
        raise _from_ledger(written, zone_id)

    async def _checks_and_projections(
        self,
        transaction: Transaction,
        kind: IngestKind,
        node: NodeScope,
        zone_id: uuid.UUID,
        submission: Mapping[str, Any],
        fact: FactTime,
        record_id: uuid.UUID,
    ) -> None:
        """Pasos 7 y 8 y las dos proyecciones, en la transacción del registro."""
        deps = self._deps
        attributes = self._attributes(node, zone_id)
        # (7) antigüedad máxima y catálogo.
        with deps.tracer.start_as_current_span(CATALOG_SPAN, attributes=attributes):
            if fact.too_old(await deps.store.retention_days(transaction, node.node_id)):
                raise IngestRejected(
                    RejectionCode.TIMESTAMP_OUT_OF_WINDOW, field="node_time", zone_id=zone_id
                )
            if kind.gated:
                window = fact.catalog_window
                versions = await deps.store.catalog_versions(transaction, zone_id, window)
                violation = catalog_violation(versions, submission, kind, window)
                if violation is not None:
                    raise IngestRejected(
                        RejectionCode.SCHEMA_INVALID, field=violation, zone_id=zone_id
                    )
        # (8) compuerta de uso en el instante del hecho, con la tolerancia.
        if kind.gated:
            with deps.tracer.start_as_current_span(GATE_SPAN, attributes=attributes):
                window = fact.gate_window
                spans = await deps.store.usage_spans(transaction, zone_id, window)
            if not usage_approved_during(spans, window):
                raise IngestRejected(
                    RejectionCode.ZONE_GATE_NOT_APPROVED, field="zone_id", zone_id=zone_id
                )
        # Proyecciones: concesiones citadas y cierre huérfano.
        clips = cited_clip_ids(submission)
        if clips:
            await deps.store.mark_cited(
                transaction,
                plant_id=node.plant_id,
                zone_id=zone_id,
                node_id=node.node_id,
                clip_ids=clips,
                now=fact.received_at,
            )
        if kind is IngestKind.OBSERVABILITY_EVENT and submission["phase"] == "closed":
            opened = str(submission["opened_event_id"])
            if not await deps.store.event_accepted(transaction, opened):
                await deps.store.mark_orphan_close(
                    transaction,
                    event_id=uuid.UUID(str(submission["event_id"])),
                    opened_event_id=uuid.UUID(opened),
                    plant_id=node.plant_id,
                    zone_id=zone_id,
                    node_id=node.node_id,
                    ledger_record_id=record_id,
                    received_at=fact.received_at,
                )

    def _duplicate(
        self,
        kind: IngestKind,
        existing: AcceptedRecord,
        submission: Mapping[str, Any],
        zone_id: uuid.UUID,
    ) -> IngestReply:
        """``accepted_duplicate`` con el recibo original, o ``idempotency_conflict`` (BR-GOB-89)."""
        if not same_submission(existing.content, submission):
            raise IngestRejected(
                RejectionCode.IDEMPOTENCY_CONFLICT, field=kind.id_field, zone_id=zone_id
            )
        stored = existing.content.get(RECEIPT_FIELD)
        if not isinstance(stored, Mapping):
            raise RuntimeError("registro de la ingesta sin recibo")
        receipt = {
            "platform_record_id": stored["platform_record_id"],
            "received_at": stored["received_at"],
            "status": AcceptanceStatus.ACCEPTED_DUPLICATE.value,
        }
        return IngestReply(_receipt(receipt), duplicate=True)

    # --- Rechazos permanentes ----------------------------------------------------------------

    async def record_rejection(
        self,
        kind: IngestKind,
        node: NodeScope,
        code: RejectionCode,
        *,
        received_at: datetime,
        zone_id: uuid.UUID | None = None,
    ) -> None:
        """Auditoría del rechazo permanente ``code`` y, si toca, ``ingest_rejected`` (BR-GOB-96).

        Las dos escrituras van en una transacción; si no se pueden hacer, la excepción sale y el
        nodo recibe un transitorio (reintenta y se vuelve a rechazar con rastro).
        """
        kind, code = IngestKind(kind), RejectionCode(code)
        if code in TRANSIENT_CODES:
            return
        deps = self._deps
        context = node.context
        with deps.tracer.start_as_current_span(
            REJECTION_SPAN, attributes=self._attributes(node, zone_id)
        ):
            zone = (
                zone_id
                if zone_id is not None and await deps.store.zone_in_organization(context, zone_id)
                else None
            )
            received = to_millisecond(received_at)
            async with deps.database.transaction(context) as transaction:
                await deps.audit.append(
                    context,
                    AuditOperation.INGEST_REJECTED,
                    outcome=AuditOutcome.DENIED,
                    plant_id=node.plant_id,
                    zone_id=zone,
                    resource=ResourceRef("node", node.node_id),
                    filters={"record_kind": kind.record_kind.value, "code": code.value},
                    transaction=transaction,
                )
                if code not in RECORDED_CODES:
                    return
                content: dict[str, Any] = {
                    "node_id": str(node.node_id),
                    "record_kind": kind.record_kind.value,
                    "code": code.value,
                    "correlation_id": str(context.correlation_id),
                    "received_at": format_timestamp(received),
                }
                if zone is not None:
                    content["zone_id"] = str(zone)
                written = await deps.writer.write(
                    context,
                    INGEST_REJECTED_RECORD_TYPE,
                    content,
                    scope=RecordScope(plant_id=node.plant_id),
                    occurred_at=received,
                    transaction=transaction,
                )
                if isinstance(written, LedgerRejection):
                    raise FleetWriteFailed(written)

    @staticmethod
    def _attributes(node: NodeScope, zone_id: uuid.UUID | None) -> dict[str, str]:
        attributes = {
            "correlation_id": str(node.context.correlation_id),
            "node_id": str(node.node_id),
        }
        if zone_id is not None:
            attributes["zone_id"] = str(zone_id)
        return attributes


def _event_payload(
    kind: IngestKind, submission: Mapping[str, Any], record_id: uuid.UUID, received_at: str
) -> dict[str, Any]:
    """La carga del evento (unión de la nota T-08 e interfaces §2): solo identificadores."""
    common = {
        "zone_id": submission["zone_id"],
        "node_id": submission["node_id"],
        "platform_record_id": str(record_id),
    }
    if kind is IngestKind.FINDING:
        standard = submission["standard"]
        return {
            **common,
            "finding_id": submission["finding_id"],
            "received_at": received_at,
            "family": submission["family"],
            "tier": submission["tier"],
            "standard": {"standard_id": standard["standard_id"], "version": standard["version"]},
        }
    if kind is IngestKind.DETECTION_FOR_REVIEW:
        return {
            **common,
            "detection_id": submission["detection_id"],
            "received_at": received_at,
            "review_reason": REVIEW_REASON,
            "idempotency_key": submission["detection_id"],
        }
    return {
        **common,
        "event_id_node": submission["event_id"],
        "subject_kind": submission["subject"]["kind"],
        "state": submission["state"],
        "phase": submission["phase"],
    }


def _from_ledger(rejection: LedgerRejection, zone_id: uuid.UUID) -> IngestRejected:
    """La traducción **única** del escritor al contrato (``to_contract_rejection``)."""
    try:
        document = to_contract_rejection(rejection)
    except ValueError:
        raise RuntimeError("rechazo del expediente sin código del contrato") from None
    return IngestRejected(RejectionCode(document.code), field=document.field, zone_id=zone_id)
