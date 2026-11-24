"""``fleet.episode_photos``: ingesta de la foto del episodio (TASK-572, A-112; TASK-603, A-128).

``POST /api/nodes/episode-photos`` (``node_api.routes.episode_photos``) llega aquí con la
verificación previa común de ``node_api`` hecha: (1) versión, (2) certificado y zona del nodo
**antes** que el esquema (``before_schema``), (3) tamaño (307 200 bytes) y (4) esquema con el modelo
estricto ``EpisodePhotoSubmission`` del contrato vigente (v1.6.0). Este servicio completa, en el
orden de BR-GOB-84 (el primer fallo decide el código):

- **paso 4, lo que el esquema no expresa**: ``contract_version`` del cuerpo igual a la cabecera,
  ``Idempotency-Key`` igual a ``photo_id`` (BR-CTR-26) y ``data_base64`` que se decodifica →
  ``schema_invalid``. El lector estricto de v1.6.0 ya exige ``record_id``, ``photo_index`` y
  ``photo_role`` coherentes con ``zone_mode`` (r1 y r2 de TASK-600);
- **paso 5, idempotencia atómica** (BR-GOB-89): en **una** transacción, ``INSERT … ON CONFLICT DO
  NOTHING`` de la fila y, si ya había una con ese ``photo_id`` en la organización, la comparación
  de su ``canonical_sha256`` con el de la presentación: igual → ``accepted_duplicate`` con el
  ``Receipt`` original, **aunque después hayan cambiado la compuerta o el catálogo**; distinto →
  ``idempotency_conflict``. Una presentación simultánea del mismo ``photo_id`` espera en la clave a
  que la primera confirme o se revierta;
- **paso 6, imagen** (si la hay): ``fleet.domain.episode_photo.image_violation`` (tamaño, suma,
  ``jpeg_facts``, dimensiones y marca; no mira los píxeles);
- **paso 7**: antigüedad (``timestamp_out_of_window``, con ``sent_records_retention_days`` del
  nodo, como los demás registros fechados), el estándar vigente y la cámara del catálogo de la
  zona y sus valores de proceso, si los trae (``schema_invalid`` en ``standard``, ``camera_id`` o
  ``process_context.variables…``, A-129);
- **paso 8**: la compuerta aprobada en la ventana del hecho (``node_time.started_at`` con la
  tolerancia; ``zone_gate_not_approved``): la de **montaje** en ``commissioning`` (A-112) y la **de
  uso** en ``productive``, la misma consulta que los hallazgos (BR-GOB-92, A-128 §5);
- **un índice por tramo** (``productive``, tras la compuerta, como la plataforma simulada de
  v1.6.0): si otro ``photo_id`` ya ocupa su ``(zona, episodio, tramo, photo_index)``
  (``PhotoSlotTaken`` del almacén), ``schema_invalid`` en ``photo_index``, permanente, y la fila
  que ya estaba no cambia. Si la que lo ocupa resulta ser **la misma** ``photo_id`` (dos envíos
  simultáneos), se resuelve como el paso 5;
- **paso 9**: la transacción confirma. Si cualquier paso falla, se revierte y no queda nada.

La foto del hallazgo **no** exige que su registro haya llegado antes (A-128 §5): la lectura
(TASK-589) las une por ``record_id``, zona, episodio y tramo cuando están los dos; una foto cuyo
registro no llega nunca no se ve y vence según su modo.

La foto **no** entra en el expediente ni en la cadena (QF10). Vence según su modo
(``fleet.domain.episode_photo.photo_row``): ``VIGIA_EPISODE_PHOTO_RETENTION_DAYS`` en
``commissioning`` y ``VIGIA_FINDING_PHOTO_RETENTION_DAYS`` en ``productive``. Los rechazos de
``productive`` dejan su rastro ``ingest_rejected`` como los hallazgos (BR-GOB-96; lo pide la ruta);
los de ``commissioning``, solo métrica y registro (A-112). Ningún paso lee la hora del sistema:
``received_at`` llega de la verificación previa y el identificador del recibo, del mismo ``Clock``
inyectado.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from opentelemetry import trace as otel_trace
from vigia_contracts.canonical import canonical_sha256
from vigia_contracts.models._base import ContractModel
from vigia_contracts.models.enumerations import AcceptanceStatus, RejectionCode
from vigia_contracts.models.receipt import Receipt as ContractReceipt

from vigia_platform.fleet.adapters.postgres.episode_photo_store import PhotoSlotTaken, StoredPhoto
from vigia_platform.fleet.application.ingest import IngestRejected, IngestReply
from vigia_platform.fleet.domain.clock_tolerance import FactTime, Window
from vigia_platform.fleet.domain.episode_photo import (
    DEFAULT_FINDING_RETENTION_DAYS,
    DEFAULT_RETENTION_DAYS,
    EpisodePhotoRow,
    ZoneMode,
    catalog_violation,
    check_finding_retention_days,
    check_retention_days,
    decode_image,
    image_violation,
    photo_row,
)
from vigia_platform.fleet.domain.ingest_order import (
    CatalogVersionView,
    GateSpan,
    usage_approved_during,
)
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.tracing import TRACER_NAME, span_name
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "EpisodePhotoDependencies",
    "EpisodePhotoService",
    "EpisodePhotoStore",
]

CLAIM_SPAN: Final = span_name("fleet.episode_photo.idempotency")
CATALOG_SPAN: Final = span_name("fleet.episode_photo.catalog")
GATE_SPAN: Final = span_name("fleet.episode_photo.gate")


class EpisodePhotoStore(Protocol):
    """Las sentencias de la ingesta de la foto (``PostgresEpisodePhotoStore``)."""

    async def claim(self, transaction: Transaction, row: EpisodePhotoRow) -> bool: ...

    async def stored(self, transaction: Transaction, photo_id: uuid.UUID) -> StoredPhoto | None: ...

    async def retention_days(self, transaction: Transaction, node_id: uuid.UUID) -> int | None: ...

    async def catalog_versions(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> Sequence[CatalogVersionView]: ...

    async def mounting_spans(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> Sequence[GateSpan]: ...

    async def usage_spans(
        self, transaction: Transaction, zone_id: uuid.UUID, window: Window
    ) -> Sequence[GateSpan]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class EpisodePhotoDependencies:
    """Lo que recibe el servicio (lo construye la raíz de composición)."""

    database: LedgerDatabase
    clock: Clock
    store: EpisodePhotoStore
    retention_days: int = DEFAULT_RETENTION_DAYS
    """``VIGIA_EPISODE_PHOTO_RETENTION_DAYS``: de 1 a 90 o el servicio no se construye."""
    finding_retention_days: int = DEFAULT_FINDING_RETENTION_DAYS
    """``VIGIA_FINDING_PHOTO_RETENTION_DAYS`` (foto del hallazgo, TASK-588): de 0 a 1 825 (0 = sin
    vencimiento) o el servicio no se construye."""
    tracer: otel_trace.Tracer = field(default_factory=lambda: otel_trace.get_tracer(TRACER_NAME))


def _receipt(record_id: uuid.UUID, received_at: datetime, status: AcceptanceStatus) -> Any:
    document = {
        "platform_record_id": str(record_id),
        "received_at": format_timestamp(received_at),
        "status": status.value,
    }
    return ContractReceipt.model_validate_json(json.dumps(document))


def _fact(submission: Mapping[str, Any], received_at: datetime) -> FactTime:
    node_time = submission["node_time"]
    clock = node_time["clock"]
    return FactTime(
        started_at=datetime.fromisoformat(node_time["started_at"]),
        ended_at=datetime.fromisoformat(node_time["ended_at"]),
        synchronized=bool(clock["synchronized"]),
        offset_ms=int(clock["offset_ms"]),
        received_at=received_at,
    )


class EpisodePhotoService:
    """``fleet.episode_photos``: pasos 4 (resto) a 9 de la foto del episodio."""

    def __init__(self, deps: EpisodePhotoDependencies) -> None:
        check_retention_days(deps.retention_days)
        check_finding_retention_days(deps.finding_retention_days)
        self._deps = deps

    def __repr__(self) -> str:
        return "EpisodePhotoService()"

    @property
    def retention_days(self) -> int:
        return self._deps.retention_days

    async def accept(
        self,
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
        deps = self._deps
        submission = document.to_json_value()
        zone_id = uuid.UUID(str(submission["zone_id"]))
        # (4) lo que el esquema no expresa.
        if submission.get("contract_version") != contract_version:
            raise IngestRejected(
                RejectionCode.SCHEMA_INVALID, field="contract_version", zone_id=zone_id
            )
        if idempotency_key != submission["photo_id"]:
            raise IngestRejected(
                RejectionCode.SCHEMA_INVALID,
                field="Idempotency-Key",
                body_level=True,
                zone_id=zone_id,
            )
        image = submission.get("image")
        try:
            data = None if image is None else decode_image(image)
        except ValueError:
            raise IngestRejected(
                RejectionCode.SCHEMA_INVALID, field="image.data_base64", zone_id=zone_id
            ) from None
        received = to_millisecond(received_at)
        fact = _fact(submission, received)
        row = photo_row(
            submission,
            data=data,
            received_at=received,
            retention_days=deps.retention_days,
            finding_retention_days=deps.finding_retention_days,
            canonical_sha256=canonical_sha256(submission),
            platform_record_id=uuid7(deps.clock),
        )
        # La imagen se mira en memoria, antes de la transacción: una imagen que no pasa nunca llega
        # a la base, ni como tupla muerta del montón ni en el WAL (A-75, A-112). El código que
        # recibe el nodo sigue siendo el de BR-GOB-84: la idempotencia (5) antes que la imagen (6).
        violation = None if image is None or data is None else image_violation(image, data)
        attributes = {"node_id": str(node.node_id), "zone_id": str(zone_id)}
        slot_taken = False
        async with deps.database.transaction(node.context) as transaction:
            # (5) idempotencia: la fila entra, o la que ya estaba decide. Con la imagen rechazada
            # solo se consulta la que ya estaba, sin INSERT.
            with deps.tracer.start_as_current_span(CLAIM_SPAN, attributes=attributes):
                if violation is None:
                    try:
                        previous = await self._claim(transaction, row)
                    except PhotoSlotTaken:
                        # Otro photo_id en su índice del tramo (o el mismo, si dos envíos
                        # simultáneos chocaron en el índice): se decide tras la compuerta.
                        slot_taken = True
                        previous = await self._existing(transaction, row)
                else:
                    previous = await self._existing(transaction, row)
            if previous is not None:
                return previous
            # (6) imagen.
            if violation is not None:  # paso 6: el rechazo de la imagen, sin fila escrita
                raise IngestRejected(violation.code, field=violation.field, zone_id=zone_id)
            # (7) antigüedad, catálogo y cámara.
            with deps.tracer.start_as_current_span(CATALOG_SPAN, attributes=attributes):
                await self._check_catalog(transaction, node, zone_id, submission, fact)
            # (8) compuerta: la de montaje en commissioning y la de uso en productive.
            with deps.tracer.start_as_current_span(GATE_SPAN, attributes=attributes):
                if row.zone_mode is ZoneMode.PRODUCTIVE:
                    spans = await deps.store.usage_spans(transaction, zone_id, fact.gate_window)
                else:
                    spans = await deps.store.mounting_spans(transaction, zone_id, fact.gate_window)
            if not usage_approved_during(spans, fact.gate_window):
                raise IngestRejected(
                    RejectionCode.ZONE_GATE_NOT_APPROVED, field="zone_id", zone_id=zone_id
                )
            # Un índice por tramo (productive): el INSERT ya se deshizo hasta su SAVEPOINT.
            if slot_taken:
                raise IngestRejected(
                    RejectionCode.SCHEMA_INVALID, field="photo_index", zone_id=zone_id
                )
        # (9) la transacción confirmó.
        return IngestReply(
            _receipt(row.platform_record_id, received, AcceptanceStatus.ACCEPTED),
            duplicate=False,
        )

    async def _claim(self, transaction: Transaction, row: EpisodePhotoRow) -> IngestReply | None:
        """``None`` si la fila entró; el duplicado con su recibo original, o el conflicto."""
        store = self._deps.store
        if await store.claim(transaction, row):
            return None
        stored = await store.stored(transaction, row.photo_id)
        if stored is None:  # pragma: no cover - la clave existe si el INSERT chocó con ella
            raise RuntimeError("foto del episodio sin fila tras el conflicto de clave")
        return self._resolve(stored, row)

    async def _existing(self, transaction: Transaction, row: EpisodePhotoRow) -> IngestReply | None:
        """El paso 5 sin ``INSERT`` (imagen rechazada): ``None`` si no hay fila con ese
        ``photo_id``; si la hay, ``idempotency_conflict`` (un cuerpo con una imagen que no pasa
        nunca es el de una foto aceptada)."""
        stored = await self._deps.store.stored(transaction, row.photo_id)
        return None if stored is None else self._resolve(stored, row)

    @staticmethod
    def _resolve(stored: StoredPhoto, row: EpisodePhotoRow) -> IngestReply:
        """``accepted_duplicate`` con el recibo original, o ``idempotency_conflict``."""
        if stored.canonical_sha256 != row.canonical_sha256:
            raise IngestRejected(
                RejectionCode.IDEMPOTENCY_CONFLICT, field="photo_id", zone_id=row.zone_id
            )
        return IngestReply(
            _receipt(
                stored.platform_record_id,
                stored.received_at,
                AcceptanceStatus.ACCEPTED_DUPLICATE,
            ),
            duplicate=True,
        )

    async def _check_catalog(
        self,
        transaction: Transaction,
        node: NodeScope,
        zone_id: uuid.UUID,
        submission: Mapping[str, Any],
        fact: FactTime,
    ) -> None:
        store = self._deps.store
        if fact.too_old(await store.retention_days(transaction, node.node_id)):
            raise IngestRejected(
                RejectionCode.TIMESTAMP_OUT_OF_WINDOW, field="node_time", zone_id=zone_id
            )
        window = fact.catalog_window
        versions = await store.catalog_versions(transaction, zone_id, window)
        violation = catalog_violation(versions, submission, window)
        if violation is not None:
            raise IngestRejected(RejectionCode.SCHEMA_INVALID, field=violation, zone_id=zone_id)
