"""Fotos del episodio guardadas y su lectura para las pruebas de TASK-573 (A-112; NFR-CTR-43).

Sobre ``fleet_http_support.FleetStack`` (la aplicación real con la cadena fija,
``ContextAuthorizer`` y ``EpisodePhotoReader``), como superusuario para sembrar:

- ``stored``: una fila de ``fleet.episode_photo`` con un JPEG **sintético**
  (``episode_photo_support.jpeg``; los bytes del barrido salen del ``photo_id``, así que cada foto
  es distinta sin depender de ninguna semilla) o con el motivo de su ausencia;
- ``usage``: la compuerta de **uso** de la zona aprobada o revocada desde un instante (la historia
  de ``catalog.gate_state_history``, que es lo que miran la lectura y la purga);
- ``get``: una petición sin avanzar el reloj (las pruebas del vencimiento fijan el instante);
- ``image_audit`` y ``entries``: lo que quedó en ``shared.audit_entry``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx

from tests.episode_photo_support import BLUR_VERSION, jpeg
from tests.fleet_http_support import CONCESSION_HEADER, SAME_ORIGIN, FleetStack
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie

__all__ = [
    "IMAGE_PATH",
    "LIST_PATH",
    "Stored",
    "entries",
    "get",
    "image_audit",
    "photo_bytes",
    "stored",
    "usage",
]

RETENTION: Final = dt.timedelta(days=30)
LIST_PATH: Final = "/zones/{zone_id}/episode-photos"
IMAGE_PATH: Final = "/episode-photos/{photo_id}/image"

_INSERT: Final = (
    "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id, node_id,"
    " episode_id, segment_index, classified_as, family, standard_id, standard_version,"
    " max_confidence, started_at, ended_at, received_at, expires_at, camera_id, frame_at, width,"
    " height, size_bytes, sha256, blur_params_version, image, image_absent_reason,"
    " canonical_sha256, platform_record_id) VALUES ($1, $2, $3, $4, $5, $6, 0, 'finding',"
    " 'coexistence', $7, 1, 0.91, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,"
    " $21, $22)"
)


@dataclass(frozen=True)
class Stored:
    """Una foto sembrada: su identificador, su lugar y sus bytes (``None`` sin imagen)."""

    photo_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    data: bytes | None
    started_at: dt.datetime
    received_at: dt.datetime
    expires_at: dt.datetime

    @property
    def image_path(self) -> str:
        return IMAGE_PATH.format(photo_id=self.photo_id)


def photo_bytes(photo_id: uuid.UUID, *, padding: int = 0) -> bytes:
    """Un JPEG sintético propio de ``photo_id`` con ``padding`` bytes más en el barrido, sacados
    de una cadena de sumas (no se comprimen: con ``padding`` grande la imagen va a la TOAST)."""
    blocks = (
        hashlib.sha256(photo_id.bytes + index.to_bytes(4, "big")).digest()
        for index in range(padding // 32 + 1)
    )
    extra = b"".join(blocks)[:padding] if padding else b""
    return jpeg(scan=photo_id.bytes + extra)


def stored(
    stack: FleetStack,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    *,
    received_at: dt.datetime | None = None,
    started_at: dt.datetime | None = None,
    expires_at: dt.datetime | None = None,
    reason: str | None = None,
    padding: int = 0,
) -> Stored:
    """Una foto en ``zone_id`` recibida en ``received_at`` (por defecto, ahora)."""
    photo_id = uuid.uuid4()
    received = received_at if received_at is not None else stack.authz.now()
    started = started_at if started_at is not None else received - dt.timedelta(seconds=30)
    expires = expires_at if expires_at is not None else received + RETENTION
    data = None if reason is not None else photo_bytes(photo_id, padding=padding)
    stack.execute(
        _INSERT,
        organization_id,
        photo_id,
        plant_id,
        zone_id,
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        started,
        started + dt.timedelta(seconds=4),
        received,
        expires,
        None if data is None else uuid.uuid4(),
        None if data is None else started + dt.timedelta(seconds=1),
        None if data is None else 320,
        None if data is None else 240,
        None if data is None else len(data),
        None if data is None else hashlib.sha256(data).hexdigest(),
        None if data is None else BLUR_VERSION,
        data,
        reason,
        hashlib.sha256(photo_id.bytes).hexdigest(),
        uuid.uuid4(),
    )
    return Stored(photo_id, organization_id, plant_id, zone_id, data, started, received, expires)


def usage(
    stack: FleetStack,
    organization_id: uuid.UUID,
    plant_id: uuid.UUID,
    zone_id: uuid.UUID,
    *,
    approved: bool,
    at: dt.datetime,
) -> None:
    """La compuerta de uso de la zona pasa a ``approved`` o ``revoked`` en ``at``."""
    stack.execute(
        "UPDATE catalog.gate_state_history SET effective_until = $2"
        " WHERE zone_id = $1 AND gate = 'usage' AND effective_until IS NULL",
        zone_id,
        at,
    )
    stack.execute(
        "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
        " status, effective_from, decided_by, reason_es, ledger_record_id, record_id)"
        " VALUES ($1, $2, $3, 'usage', $4, $5, $6, $7, $8, $9)",
        organization_id,
        plant_id,
        zone_id,
        "approved" if approved else "revoked",
        at,
        stack.authz.operator_id,
        None if approved else "Retirada sintética del uso de la zona",
        uuid.uuid4(),
        uuid.uuid4(),
    )


def get(
    stack: FleetStack,
    who: tuple[SessionCookie, uuid.UUID | None],
    path: str,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """``GET path`` como ``who`` **sin** avanzar el reloj."""
    cookie, concession = who
    headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
    if concession is not None:
        headers[CONCESSION_HEADER] = str(concession)
    response: httpx.Response = stack.run(stack.client.get(path, params=params, headers=headers))
    return response


def entries(stack: FleetStack, operation: str) -> list[dict[str, Any]]:
    """Todas las entradas de ``operation``, de todas las organizaciones, en orden de cadena."""
    return [
        dict(row)
        for row in stack.fetch(
            "SELECT organization_id, outcome, actor_id, actor_concession_id, actor_role_in_use,"
            " scope_plant_id, scope_zone_id, resource_kind, resource_id, result_count,"
            " filters_json::text AS filters FROM shared.audit_entry WHERE operation = $1"
            " ORDER BY organization_id, chain_sequence",
            operation,
        )
    ]


def image_audit(stack: FleetStack, photo_id: uuid.UUID) -> list[dict[str, Any]]:
    """Las entradas ``episode_photo_read`` de ``photo_id``."""
    return [
        e for e in entries(stack, "episode_photo_read") if str(e["resource_id"]) == str(photo_id)
    ]
