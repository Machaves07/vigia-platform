"""``fleet.episode_photos``: ingesta de la foto del episodio de la sombra (TASK-572; A-112).

``POST /api/nodes/episode-photos`` (``node_api.routes.episode_photos``) llega aquí con la
verificación previa común de ``node_api`` hecha: (1) versión, (2) certificado y zona del nodo
**antes** que el esquema (``before_schema``), (3) tamaño (307 200 bytes) y (4) esquema con el modelo
estricto ``EpisodePhotoSubmission`` de v1.5.0. Este servicio completa, en el orden de BR-GOB-84
(el primer fallo decide el código):

- **paso 4, lo que el esquema no expresa**: ``contract_version`` del cuerpo igual a la cabecera,
  ``Idempotency-Key`` igual a ``photo_id`` (BR-CTR-26) y ``data_base64`` que se decodifica →
  ``schema_invalid``;
- **paso 5, idempotencia atómica** (BR-GOB-89): en **una** transacción, ``INSERT … ON CONFLICT DO
  NOTHING`` de la fila y, si ya había una con ese ``photo_id`` en la organización, la comparación
  de su ``canonical_sha256`` con el de la presentación: igual → ``accepted_duplicate`` con el
  ``Receipt`` original, **aunque después hayan cambiado la compuerta o el catálogo**; distinto →
  ``idempotency_conflict``. Una presentación simultánea del mismo ``photo_id`` espera en la clave a
  que la primera confirme o se revierta;
- **paso 6, imagen** (si la hay): ``fleet.domain.episode_photo.image_violation`` (tamaño, suma,
  ``jpeg_facts``, dimensiones y marca; no mira los píxeles);
- **paso 7**: antigüedad (``timestamp_out_of_window``, con ``sent_records_retention_days`` del
  nodo, como los demás registros fechados) y el estándar vigente y la cámara del catálogo de la
  zona (``schema_invalid`` en ``standard`` o ``camera_id``);
- **paso 8**: la compuerta de **montaje** aprobada en la ventana del hecho
  (``zone_gate_not_approved``; A-112 la usa aquí para el montaje);
- **paso 9**: la transacción confirma. Si cualquier paso falla, se revierte y no queda nada.

La foto **no** entra en el expediente ni en la cadena (QF10): ni registro, ni evento, ni rastro
``ingest_rejected``. Vence a los ``retention_days`` (``VIGIA_EPISODE_PHOTO_RETENTION_DAYS``, 30 por
defecto, de 1 a 90) de su recepción. Ningún paso lee la hora del sistema: ``received_at`` llega de
la verificación previa y el identificador del recibo, del mismo ``Clock`` inyectado.
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

from vigia_platform.fleet.adapters.postgres.episode_photo_store import StoredPhoto
from vigia_platform.fleet.application.ingest import IngestRejected, IngestReply
from vigia_platform.fleet.domain.clock_tolerance import FactTime, Window
from vigia_platform.fleet.domain.episode_photo import (
    DEFAULT_RETENTION_DAYS,
    EpisodePhotoRow,
    catalog_violation,
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


@dataclass(frozen=True, slots=True, kw_only=True)
class EpisodePhotoDependencies:
    """Lo que recibe el servicio (lo construye la raíz de composición)."""

    database: LedgerDatabase
    clock: Clock
    store: EpisodePhotoStore
    retention_days: int = DEFAULT_RETENTION_DAYS
    """``VIGIA_EPISODE_PHOTO_RETENTION_DAYS``: de 1 a 90 o el servicio no se construye."""
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
            canonical_sha256=canonical_sha256(submission),
            platform_record_id=uuid7(deps.clock),
        )
        attributes = {"node_id": str(node.node_id), "zone_id": str(zone_id)}
        async with deps.database.transaction(node.context) as transaction:
            # (5) idempotencia: la fila entra, o la que ya estaba decide.
            with deps.tracer.start_as_current_span(CLAIM_SPAN, attributes=attributes):
                previous = await self._claim(transaction, row)
            if previous is not None:
                return previous
            # (6) imagen.
            if image is not None and data is not None:
                violation = image_violation(image, data)
                if violation is not None:
                    raise IngestRejected(violation.code, field=violation.field, zone_id=zone_id)
            # (7) antigüedad, catálogo y cámara.
            with deps.tracer.start_as_current_span(CATALOG_SPAN, attributes=attributes):
                await self._check_catalog(transaction, node, zone_id, submission, fact)
            # (8) compuerta de montaje.
            with deps.tracer.start_as_current_span(GATE_SPAN, attributes=attributes):
                spans = await deps.store.mounting_spans(transaction, zone_id, fact.gate_window)
            if not usage_approved_during(spans, fact.gate_window):
                raise IngestRejected(
                    RejectionCode.ZONE_GATE_NOT_APPROVED, field="zone_id", zone_id=zone_id
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
