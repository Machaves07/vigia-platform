"""El resultado de actualización que reporta el nodo (TASK-226; LC-GOB-17; BR-GOB-103; BL §2.7).

``node_api.routes.update_results`` llega aquí con la verificación previa común hecha: (1) versión,
(2) certificado, (3) tamaño ≤ 64 KB y (4) esquema con el modelo estricto ``UpdateResult`` de U-01,
y con la parte del paso 2 que depende del cuerpo ya comprobada antes del esquema
(``check_body_scope``: organización, planta y nodo iguales a los del certificado, si no
``node_zone_mismatch``). Este servicio completa:

- **paso 4, lo que no expresa el esquema**: ``contract_version`` del cuerpo igual a la cabecera,
  ``Idempotency-Key`` igual a ``update_result_id`` (BR-CTR-26) y ``target_version`` que quepa en
  ``ReleaseVersion`` (la que lleva el evento) → ``schema_invalid``;
- **idempotencia** por ``update_result_id`` (``source_key`` del registro): el mismo contenido
  (nodo, versión y resultado) → ``accepted_duplicate`` con el ``Receipt`` original; otro →
  ``idempotency_conflict``. El registro guarda ``reported_at`` = la marca de **recepción** de cada
  intento (nota de §3.12), así que el hash del escritor nunca coincidiría: la comparación es la de
  ``UpdateReport.same_as`` con la fila aceptada;
- **escritura** en **una** transacción, en el orden de los candados de ``target_versions``:
  (0) la fila de ``fleet.update_result`` (``ON CONFLICT DO NOTHING``: un envío simultáneo con el
  mismo identificador espera a que el primero confirme y no inserta; se resuelve otra vez como
  duplicado o conflicto), (1) la fila de inventario del nodo y ``NodeInventory.last_update_result``
  = el resultado con la recepción más reciente, (2) el registro ``update_result_received``
  ``{update_result_id, node_id, target_version, result, reported_at}`` por
  ``EscritorExpediente.write`` con el evento ``update_result_received`` ``{node_id,
  target_version, result}`` (BR-GOB-103).

``reverted`` y ``failed`` no son errores de la plataforma: se escriben, se publican y se proyectan
igual que ``applied`` (nota T-05). Un fallo de infraestructura es transitorio y sin aceptación
parcial. Ningún paso lee la hora del sistema: ``received_at`` llega de la verificación previa.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from opentelemetry import trace as otel_trace
from vigia_contracts.models.enumerations import AcceptanceStatus, RejectionCode
from vigia_contracts.models.receipt import Receipt as ContractReceipt
from vigia_contracts.models.update_result import UpdateResult as ContractUpdateResult

from vigia_platform.fleet.adapters.postgres.fleet_version_store import (
    AcceptedResult,
    PostgresFleetVersionStore,
)
from vigia_platform.fleet.application.ingest import IngestRejected
from vigia_platform.fleet.domain.fleet_versions import (
    RESULT_RECORD_TYPE,
    UPDATE_RESULT_EVENT,
    UpdateReport,
    is_release_version,
    outcome_of,
)
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
    to_contract_rejection,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.tracing import TRACER_NAME, span_name
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = ["UpdateResultReply", "UpdateResultService"]

_SCOPE_FIELDS: Final = ("organization_id", "plant_id", "node_id")
WRITE_SPAN: Final = span_name("fleet.update_result.write")


@dataclass(frozen=True, slots=True)
class UpdateResultReply:
    """El ``Receipt`` del contrato que recibe el nodo y si fue un duplicado."""

    receipt: ContractReceipt
    duplicate: bool


class _AlreadyInserted(Exception):
    """Otra petición con el mismo ``update_result_id`` confirmó antes (se resuelve después)."""


def _receipt(accepted: AcceptedResult, status: AcceptanceStatus) -> ContractReceipt:
    """El ``Receipt`` del contrato, validado con el lector estricto (forma de transporte)."""
    return ContractReceipt.model_validate_json(
        json.dumps(
            {
                "platform_record_id": str(accepted.ledger_record_id),
                "received_at": format_timestamp(accepted.report.reported_at),
                "status": status.value,
            }
        )
    )


class UpdateResultService:
    """``fleet.versions`` (LC-GOB-17): el resultado que ``node_api`` orquesta en su ruta."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        clock: Clock,
        tracer: otel_trace.Tracer | None = None,
    ) -> None:
        self.database = database
        self.writer = writer
        self.clock = clock
        self.tracer = tracer if tracer is not None else otel_trace.get_tracer(TRACER_NAME)
        self._store = PostgresFleetVersionStore(database)

    def __repr__(self) -> str:
        return "UpdateResultService()"

    @staticmethod
    def check_body_scope(node: NodeScope | None, document: object) -> None:
        """Organización, planta y nodo del cuerpo (aún sin validar) contra el certificado.

        Un valor que no es un UUID lo rechaza después el esquema; uno legible y distinto del
        certificado es ``node_zone_mismatch`` antes de mirar el esquema (el paso 2 gana al 4).
        """
        if node is None or not isinstance(document, Mapping):
            return
        expected = {
            "organization_id": node.organization_id,
            "plant_id": node.plant_id,
            "node_id": node.node_id,
        }
        for name in _SCOPE_FIELDS:
            value = document.get(name)
            if not isinstance(value, str):
                continue
            try:
                presented = uuid.UUID(value)
            except ValueError:
                continue
            if presented != expected[name]:
                raise IngestRejected(RejectionCode.NODE_ZONE_MISMATCH, field=name)

    async def accept(
        self,
        node: NodeScope,
        document: ContractUpdateResult,
        *,
        received_at: datetime,
        idempotency_key: str | None,
        contract_version: str | None,
    ) -> UpdateResultReply:
        """Acepta el resultado ``document`` de ``node`` o lanza el primer rechazo."""
        if not isinstance(node, NodeScope) or not isinstance(document, ContractUpdateResult):
            raise TypeError("accept recibe el alcance del nodo y el UpdateResult del contrato")
        # (2) defensa en profundidad: el cuerpo ya validado frente al certificado.
        for name, expected in (
            ("organization_id", node.organization_id),
            ("plant_id", node.plant_id),
            ("node_id", node.node_id),
        ):
            if uuid.UUID(str(getattr(document, name))) != expected:
                raise IngestRejected(RejectionCode.NODE_ZONE_MISMATCH, field=name)
        # (4) lo que el esquema no expresa.
        if document.contract_version != contract_version:
            raise IngestRejected(RejectionCode.SCHEMA_INVALID, field="contract_version")
        update_result_id = uuid.UUID(str(document.update_result_id))
        if idempotency_key != str(update_result_id):
            raise IngestRejected(
                RejectionCode.SCHEMA_INVALID, field="Idempotency-Key", body_level=True
            )
        if not is_release_version(document.target_version):
            raise IngestRejected(RejectionCode.SCHEMA_INVALID, field="target_version")
        report = UpdateReport(
            update_result_id=update_result_id,
            node_id=node.node_id,
            target_version=document.target_version,
            result=outcome_of(document.outcome),
            reported_at=to_millisecond(received_at),
        )
        existing = await self._store.accepted_result(node.context, update_result_id)
        if existing is not None:
            return self._duplicate(existing, report)
        try:
            accepted = await self._write(node, report)
        except _AlreadyInserted:
            existing = await self._store.accepted_result(node.context, update_result_id)
            if existing is None:
                # La clave existe y no es de esta organización: nunca se dice de quién es.
                raise IngestRejected(
                    RejectionCode.IDEMPOTENCY_CONFLICT, field="update_result_id"
                ) from None
            return self._duplicate(existing, report)
        return UpdateResultReply(_receipt(accepted, AcceptanceStatus.ACCEPTED), duplicate=False)

    async def _write(self, node: NodeScope, report: UpdateReport) -> AcceptedResult:
        """Fila, proyección, registro y evento en una transacción (orden de los candados)."""
        store = self._store
        record_id = uuid7(self.clock)
        attributes = {
            "correlation_id": str(node.context.correlation_id),
            "node_id": str(node.node_id),
        }
        with self.tracer.start_as_current_span(WRITE_SPAN, attributes=attributes):
            async with self.database.transaction(node.context) as transaction:
                if not await store.insert_result(transaction, node.plant_id, report, record_id):
                    raise _AlreadyInserted()
                await store.lock_inventory(transaction, (node.node_id,))
                await store.project_result(transaction, node.plant_id, node.node_id)
                written = await self.writer.write(
                    node.context,
                    RESULT_RECORD_TYPE,
                    report.record_content(),
                    scope=RecordScope(plant_id=node.plant_id, node_id=node.node_id),
                    events=(
                        NewEvent(event_name=UPDATE_RESULT_EVENT, payload=report.event_payload()),
                    ),
                    occurred_at=report.reported_at,
                    transaction=transaction,
                    record_id=record_id,
                )
                if isinstance(written, LedgerRejection):
                    raise _from_ledger(written)
                if not isinstance(written, Receipt) or written.status is not (
                    AcceptanceStatus.ACCEPTED
                ):
                    # El expediente ya tenía la clave sin fila de resultado: nunca se da por bueno.
                    raise IngestRejected(
                        RejectionCode.IDEMPOTENCY_CONFLICT, field="update_result_id"
                    )
        return AcceptedResult(report=report, ledger_record_id=record_id)

    @staticmethod
    def _duplicate(existing: AcceptedResult, report: UpdateReport) -> UpdateResultReply:
        """``accepted_duplicate`` con el recibo original, o ``idempotency_conflict``."""
        if not existing.report.same_as(report):
            raise IngestRejected(RejectionCode.IDEMPOTENCY_CONFLICT, field="update_result_id")
        return UpdateResultReply(
            _receipt(existing, AcceptanceStatus.ACCEPTED_DUPLICATE), duplicate=True
        )


def _from_ledger(rejection: LedgerRejection) -> IngestRejected:
    """La traducción **única** del escritor al contrato (``to_contract_rejection``)."""
    if rejection.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT:
        return IngestRejected(RejectionCode.IDEMPOTENCY_CONFLICT, field="update_result_id")
    try:
        document = to_contract_rejection(rejection)
    except ValueError:
        raise RuntimeError("rechazo del expediente sin código del contrato") from None
    return IngestRejected(RejectionCode(document.code), field=document.field)
