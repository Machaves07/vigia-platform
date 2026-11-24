"""``POST episode-photos``: la foto del episodio (TASK-572, A-112; TASK-603, A-128; NFR-GOB-33).

El manejador de negocio (``NodeOperation``) que la raíz registra en la ``NodeApiGate``: la
verificación previa común ya admitió la petición (120 por minuto con ráfaga de 30 por nodo,
versión, certificado, cuerpo ≤ 307 200 bytes y lector estricto ``EpisodePhotoSubmission`` de
v1.6.0). La foto de la sombra (``commissioning``) y la del hallazgo (``productive``) usan la misma
ruta; el servicio aplica el régimen de cada modo.

- ``before_schema`` (entre los pasos 3 y 4 de BR-GOB-84, como ``clip_uploads``): la organización,
  la planta y el nodo del cuerpo, si son UUID canónicos, tienen que ser los del certificado, y la
  zona, una de las asignadas al nodo: si no, ``node_zone_mismatch`` **antes** que cualquier
  ``schema_invalid`` del resto del cuerpo (el alcance gana al esquema);
- ``handle``: ``EpisodePhotoService.accept`` (pasos 4 a 9) y el ``Receipt`` (``accepted`` o
  ``accepted_duplicate`` con el recibo original, BR-CTR-27).

**Rechazos de ``productive``** (BR-GOB-96; la excepción de A-112 era solo para la sombra): todo
rechazo permanente de una presentación ``productive`` deja su ``ingest_rejected`` con
``record_kind = episode_photo`` por ``IngestService.record_rejection``, como los hallazgos (y, con
``zone_gate_not_approved`` o ``node_zone_mismatch``, el registro en la cadena de la planta). El modo
es el del documento validado o, en un rechazo anterior al esquema, el ``zone_mode`` del JSON sin
validar si es **exactamente** ``productive``; sin modo legible (cuerpo ilegible o demasiado grande,
versión) decide la regla de la sombra: sin rastro. Un fallo al dejar el rastro sale en su lugar
(transitorio: el nodo reintenta).

**Observabilidad**: ``episode_photo_received_total`` por resultado, ``rejection_code``, si trae
imagen (``image_presence`` = ``present`` o ``absent``) y, en la foto del hallazgo, ``zone_mode =
productive`` (lista cerrada de ``redaction``); la de la sombra o con el modo ilegible va sin la
etiqueta, la misma serie de TASK-572. Una línea de registro por presentación con solo
identificadores, listas cerradas y el modo si se leyó. Nunca nodo ni zona en la métrica, ni los
bytes, el base64, la suma ni el tamaño de la imagen (NFR-GOB-25). Los rechazos los traduce
``rejections.translate``, la traducción única de ``node_api``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Final, Protocol

from vigia_contracts.models.enumerations import RecordKind, RejectionCode
from vigia_contracts.models.episode_photo import EpisodePhotoSubmission

from vigia_platform.fleet.adapters.postgres.episode_photo_store import PostgresEpisodePhotoStore
from vigia_platform.fleet.application.episode_photos import (
    EpisodePhotoDependencies,
    EpisodePhotoService,
)
from vigia_platform.fleet.application.ingest import IngestRejected
from vigia_platform.fleet.domain.episode_photo import (
    DEFAULT_FINDING_RETENTION_DAYS,
    DEFAULT_RETENTION_DAYS,
    ZoneMode,
)
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.node_api.observability import NodeResult
from vigia_platform.node_api.rejections import TRANSIENT_CODES, NodeRejection, translate
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

if TYPE_CHECKING:
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = [
    "RejectionRecorder",
    "episode_photo_operation",
    "episode_photo_route",
    "scope_before_schema",
]

PRESENT: Final = "present"
ABSENT: Final = "absent"

_SCOPE_FIELDS: Final = ("organization_id", "plant_id", "node_id")

_MODES: Final = {mode.value: mode for mode in ZoneMode}

_log = get_logger("node_api.episode_photos")


class RejectionRecorder(Protocol):
    """``IngestService.record_rejection``: el rastro de un rechazo permanente (BR-GOB-96)."""

    async def record_rejection(
        self,
        kind: RecordKind,
        node: NodeScope,
        code: RejectionCode,
        *,
        received_at: datetime,
        zone_id: uuid.UUID | None = None,
    ) -> None: ...


def _canonical_uuid(value: object) -> uuid.UUID | None:
    """El UUID de ``value`` si es su forma canónica; si no, ``None`` (decide el esquema)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def scope_before_schema(node: NodeScope | None, body: object) -> None:
    """``node_zone_mismatch`` si el cuerpo nombra otra organización, planta, nodo o zona."""
    if node is None or not isinstance(body, Mapping):
        return
    expected = {
        "organization_id": node.organization_id,
        "plant_id": node.plant_id,
        "node_id": node.node_id,
    }
    for name in _SCOPE_FIELDS:
        presented = _canonical_uuid(body.get(name))
        if presented is not None and presented != expected[name]:
            raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field=name)
    zone = _canonical_uuid(body.get("zone_id"))
    if zone is not None and not node.covers_zone(zone):
        raise NodeRejection(RejectionCode.NODE_ZONE_MISMATCH, field="zone_id")


def _mode_of(document: object) -> ZoneMode | None:
    """El modo de la presentación (validada o sin validar), o ``None`` si no se puede leer.

    Sin validar, solo cuenta un valor **exactamente** igual a uno de la lista cerrada."""
    if isinstance(document, EpisodePhotoSubmission):
        mode = document.to_json_value().get("zone_mode", ZoneMode.COMMISSIONING.value)
    elif isinstance(document, Mapping):
        mode = document.get("zone_mode")
    else:
        return None
    return _MODES.get(mode) if isinstance(mode, str) else None


def _image_of(document: object) -> str:
    """``present`` si la presentación trae ``image`` (validada o sin validar)."""
    if isinstance(document, EpisodePhotoSubmission):
        return ABSENT if document.image is None else PRESENT
    if isinstance(document, Mapping) and isinstance(document.get("image"), Mapping):
        return PRESENT
    return ABSENT


def episode_photo_operation(
    service: EpisodePhotoService,
    *,
    metrics: PlatformMetrics | None = None,
    rejections: RejectionRecorder | None = None,
) -> NodeOperation:
    """El manejador de ``POST episode-photos`` sobre ``service``; ``rejections`` deja el rastro de
    los rechazos de ``productive`` (la raíz siempre lo pasa, ``episode_photo_route``; sin él, un
    rechazo de ``productive`` falla en lugar de quedar sin rastro)."""

    def observe(
        node: NodeScope,
        result: NodeResult,
        image: str,
        mode: ZoneMode | None,
        code: RejectionCode | None = None,
        zone: object = None,
    ) -> None:
        instruments = metrics if metrics is not None else get_metrics()
        attributes: dict[str, object] = {"result": result.value, "image_presence": image}
        fields: dict[str, object] = {
            "organization_id": str(node.organization_id),
            "plant_id": str(node.plant_id),
            "node_id": str(node.node_id),
            "result": result.value,
            "image_presence": image,
        }
        if mode is ZoneMode.PRODUCTIVE:
            attributes["zone_mode"] = mode.value
        if mode is not None:
            fields["zone_mode"] = mode.value
        if code is not None:
            attributes["rejection_code"] = code.value
            fields["rejection_code"] = code.value
        zone_id = _canonical_uuid(zone)
        if zone_id is not None:
            fields["zone_id"] = str(zone_id)
        instruments.episode_photo_received_total.add(1, attributes)
        _log.info("foto del episodio atendida", **fields)

    async def rejected(
        node: NodeScope,
        error: BaseException,
        received_at: datetime,
        image: str,
        mode: ZoneMode | None,
        zone: object,
    ) -> None:
        rejection = translate(error)
        code = None if rejection is None else rejection.code
        observe(node, NodeResult.REJECTED, image, mode, code, zone)
        if code is None or code in TRANSIENT_CODES or mode is not ZoneMode.PRODUCTIVE:
            return  # la sombra: solo métrica y registro (A-112, «Precisiones del 13-11»)
        if rejections is None:
            raise RuntimeError("la foto del hallazgo rechazada necesita su rastro (BR-GOB-96)")
        zone_id = error.zone_id if isinstance(error, IngestRejected) else _canonical_uuid(zone)
        await rejections.record_rejection(
            RecordKind.EPISODE_PHOTO, node, code, received_at=received_at, zone_id=zone_id
        )

    async def on_rejection(
        node: NodeScope, error: BaseException, received_at: datetime, presentation: object
    ) -> None:
        zone = presentation.get("zone_id") if isinstance(presentation, Mapping) else None
        await rejected(
            node, error, received_at, _image_of(presentation), _mode_of(presentation), zone
        )

    async def handle(request: NodeRequest) -> NodeReply:
        node, document = request.node, request.document
        if node is None or not isinstance(document, EpisodePhotoSubmission):
            raise RuntimeError("foto del episodio sin alcance de nodo o sin cuerpo verificado")
        image, mode = _image_of(document), _mode_of(document)
        try:
            reply = await service.accept(
                node,
                document,
                received_at=request.received_at,
                idempotency_key=request.idempotency_key,
                contract_version=request.contract_version,
            )
        except Exception as error:
            await rejected(node, error, request.received_at, image, mode, str(document.zone_id))
            raise
        result = NodeResult.ACCEPTED_DUPLICATE if reply.duplicate else NodeResult.ACCEPTED
        observe(node, result, image, mode, zone=str(document.zone_id))
        return NodeReply(model=reply.receipt, duplicate=reply.duplicate)

    return NodeOperation(
        handle=handle, before_schema=scope_before_schema, on_rejection=on_rejection
    )


def episode_photo_route(services: UnitServices, *, rejections: RejectionRecorder) -> NodeOperation:
    """La operación compuesta por la raíz: la base, el reloj y las métricas del proceso, los
    vencimientos de ``VIGIA_EPISODE_PHOTO_RETENTION_DAYS`` (30 días sin configuración) y
    ``VIGIA_FINDING_PHOTO_RETENTION_DAYS`` (0, sin vencimiento) y el rastro de los rechazos de
    ``productive`` (``rejections``, el ``IngestService`` de la ingesta)."""
    config = services.config
    retention = DEFAULT_RETENTION_DAYS if config is None else config.episode_photo_retention_days
    finding_retention = (
        DEFAULT_FINDING_RETENTION_DAYS if config is None else config.finding_photo_retention_days
    )
    service = EpisodePhotoService(
        EpisodePhotoDependencies(
            database=services.database,
            clock=services.clock,
            store=PostgresEpisodePhotoStore(services.database),
            retention_days=retention,
            finding_retention_days=finding_retention,
        )
    )
    return episode_photo_operation(service, metrics=services.metrics, rejections=rejections)
