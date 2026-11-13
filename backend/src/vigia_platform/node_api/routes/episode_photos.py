"""``POST episode-photos``: la foto del episodio de la sombra (TASK-572; A-112; NFR-GOB-33).

El manejador de negocio (``NodeOperation``) que la raíz registra en la ``NodeApiGate``: la
verificación previa común ya admitió la petición (120 por minuto con ráfaga de 30 por nodo,
versión, certificado, cuerpo ≤ 307 200 bytes y lector estricto ``EpisodePhotoSubmission`` de
v1.5.0).

- ``before_schema`` (entre los pasos 3 y 4 de BR-GOB-84, como ``clip_uploads``): la organización,
  la planta y el nodo del cuerpo, si son UUID canónicos, tienen que ser los del certificado, y la
  zona, una de las asignadas al nodo: si no, ``node_zone_mismatch`` **antes** que cualquier
  ``schema_invalid`` del resto del cuerpo (el alcance gana al esquema);
- ``handle``: ``EpisodePhotoService.accept`` (pasos 4 a 9) y el ``Receipt`` (``accepted`` o
  ``accepted_duplicate`` con el recibo original, BR-CTR-27).

**Observabilidad**: ``episode_photo_received_total`` por resultado, ``rejection_code`` y si trae
imagen (``image_presence`` = ``present`` o ``absent``) y una línea de registro por presentación con
solo identificadores y listas cerradas. Nunca los bytes, el base64, la suma ni el tamaño de la
imagen (NFR-GOB-25). Los rechazos los traduce ``rejections.translate``, la traducción única de
``node_api``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Final

from vigia_contracts.models.enumerations import RejectionCode
from vigia_contracts.models.episode_photo import EpisodePhotoSubmission

from vigia_platform.fleet.adapters.postgres.episode_photo_store import PostgresEpisodePhotoStore
from vigia_platform.fleet.application.episode_photos import (
    EpisodePhotoDependencies,
    EpisodePhotoService,
)
from vigia_platform.fleet.domain.episode_photo import DEFAULT_RETENTION_DAYS
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.node_api.observability import NodeResult
from vigia_platform.node_api.rejections import NodeRejection, translate
from vigia_platform.node_api.router import NodeOperation, NodeReply, NodeRequest
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

if TYPE_CHECKING:
    from vigia_platform.shared.runtime.units import UnitServices

__all__ = ["episode_photo_operation", "episode_photo_route", "scope_before_schema"]

PRESENT: Final = "present"
ABSENT: Final = "absent"

_SCOPE_FIELDS: Final = ("organization_id", "plant_id", "node_id")

_log = get_logger("node_api.episode_photos")


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


def _image_of(document: object) -> str:
    """``present`` si la presentación trae ``image`` (validada o sin validar)."""
    if isinstance(document, EpisodePhotoSubmission):
        return ABSENT if document.image is None else PRESENT
    if isinstance(document, Mapping) and isinstance(document.get("image"), Mapping):
        return PRESENT
    return ABSENT


def episode_photo_operation(
    service: EpisodePhotoService, *, metrics: PlatformMetrics | None = None
) -> NodeOperation:
    """El manejador de ``POST episode-photos`` sobre ``service``."""

    def observe(
        node: NodeScope,
        result: NodeResult,
        image: str,
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
        if code is not None:
            attributes["rejection_code"] = code.value
            fields["rejection_code"] = code.value
        zone_id = _canonical_uuid(zone)
        if zone_id is not None:
            fields["zone_id"] = str(zone_id)
        instruments.episode_photo_received_total.add(1, attributes)
        _log.info("foto del episodio atendida", **fields)

    def rejected(node: NodeScope, error: BaseException, image: str, zone: object) -> None:
        rejection = translate(error)
        observe(
            node, NodeResult.REJECTED, image, None if rejection is None else rejection.code, zone
        )

    async def on_rejection(
        node: NodeScope, error: BaseException, received_at: datetime, presentation: object
    ) -> None:
        zone = presentation.get("zone_id") if isinstance(presentation, Mapping) else None
        rejected(node, error, _image_of(presentation), zone)

    async def handle(request: NodeRequest) -> NodeReply:
        node, document = request.node, request.document
        if node is None or not isinstance(document, EpisodePhotoSubmission):
            raise RuntimeError("foto del episodio sin alcance de nodo o sin cuerpo verificado")
        image = _image_of(document)
        try:
            reply = await service.accept(
                node,
                document,
                received_at=request.received_at,
                idempotency_key=request.idempotency_key,
                contract_version=request.contract_version,
            )
        except Exception as error:
            rejected(node, error, image, str(document.zone_id))
            raise
        result = NodeResult.ACCEPTED_DUPLICATE if reply.duplicate else NodeResult.ACCEPTED
        observe(node, result, image, zone=str(document.zone_id))
        return NodeReply(model=reply.receipt, duplicate=reply.duplicate)

    return NodeOperation(
        handle=handle, before_schema=scope_before_schema, on_rejection=on_rejection
    )


def episode_photo_route(services: UnitServices) -> NodeOperation:
    """La operación compuesta por la raíz: la base, el reloj y las métricas del proceso y el
    vencimiento de ``VIGIA_EPISODE_PHOTO_RETENTION_DAYS`` (30 días sin configuración)."""
    config = services.config
    retention = DEFAULT_RETENTION_DAYS if config is None else config.episode_photo_retention_days
    service = EpisodePhotoService(
        EpisodePhotoDependencies(
            database=services.database,
            clock=services.clock,
            store=PostgresEpisodePhotoStore(services.database),
            retention_days=retention,
        )
    )
    return episode_photo_operation(service, metrics=services.metrics)
