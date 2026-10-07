"""G-13 · Clips huérfanos como canal de almacenamiento (business-rules §12; SECURITY-11).

**Qué intenta**: usar las concesiones de subida de clips como un depósito gratuito: subir objetos
(grandes, repetidos o fuera de su zona) sin enviar nunca el hallazgo que los cita.

**Qué lo detiene**:

- BR-GOB-93: la concesión es por clip, con ``storage_key`` ligada a organización, planta, zona y
  nodo, ``max_size_bytes`` igual al tamaño declarado (a lo sumo 50 MB), vencimiento de a lo sumo
  15 minutos y **un solo** ``PUT`` con las cabeceras exactas (``x-amz-checksum-sha256`` y
  ``x-amz-meta-vigia-anonymized: 1``, A-29): un ``PUT`` con otros bytes no casa con la suma
  firmada, así que bajo la clave nunca queda otro contenido (repetir los **mismos** bytes dentro
  de la vigencia es el límite conocido de TASK-222: el contrato fijado no admite
  ``If-None-Match`` en ``required_headers``);
- BR-GOB-94: un clip subido cuyo hallazgo no llega en 24 h pasa a ``orphan`` (una sola vez:
  la pasada siguiente no lo vuelve a contar), alimenta ``orphan_clips_growing`` en el inventario
  y nada se borra del depósito; los clips de verificación (``purpose = verification``) no cuentan.

Los ``PUT`` van de verdad a ``vigia-evidence`` en LocalStack; la marca es la tarea periódica real
``mark_orphan_clips`` de ``vigia-worker`` (``GobPlatform.run_task``).

Por las rutas reales de la aplicación completa (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.factories import uuid7
from tests.gob_platform_support import LONG_SECONDS, GobPlatform, GobZone, Onboarding, local_url, ok
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.context import Role

pytestmark = pytest.mark.integration

MAX_CLIP_BYTES = 52_428_800
DAY = 24 * 3600


def _request(
    zone: GobZone, data: bytes, purpose: str = "evidence", **changes: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "clip_id": str(uuid7()),
        "camera_id": str(zone.cameras[0]),
        "zone_id": str(zone.zone_id),
        "media_kind": "video",
        "content_type": "video/mp4",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "duration_ms": 10_000,
        "purpose": purpose,
    }
    body.update(changes)
    return body


def _grant(gob: GobPlatform, zone: GobZone, body: dict[str, Any]) -> Any:
    return gob.node_call("POST", NodeRoute.CLIP_UPLOAD.path, certificate=zone.cert, body=body)


def _put(grant: dict[str, Any], data: bytes) -> httpx.Response:
    return httpx.put(
        local_url(grant["upload_url"]),
        content=data,
        headers=grant["required_headers"],
        timeout=LONG_SECONDS,
    )


def _status(gob: GobPlatform, clip_id: str) -> Any:
    (row,) = gob.fetch(
        "SELECT status, orphaned_at, storage_key FROM fleet.clip_upload_grant WHERE clip_id = $1",
        uuid.UUID(clip_id),
    )
    return row


def test_g13_a_grant_is_one_bounded_put_to_a_key_of_its_own(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    data = secrets.token_bytes(512)
    body = _request(zone, data)
    grant = ok(_grant(gob, zone, body))
    now = gob.now()
    expires = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
    assert timedelta(0) < expires - now <= timedelta(minutes=15)
    assert grant["max_size_bytes"] == len(data)
    assert re.fullmatch(
        rf"org/{zone.organization_id}/plant/{zone.plant_id}/zone/{zone.zone_id}"
        rf"/node/{zone.node}/{body['clip_id']}\.mp4",
        grant["storage_key"],
    )
    headers = grant["required_headers"]
    assert headers["x-amz-meta-vigia-anonymized"] == "1"
    assert (
        headers["x-amz-checksum-sha256"] == base64.b64encode(hashlib.sha256(data).digest()).decode()
    )

    # Otros bytes del mismo tamaño no casan con la suma firmada, ni antes ni después de subir.
    assert _put(grant, secrets.token_bytes(512)).status_code != 200
    assert _put(grant, data).status_code == 200
    assert _put(grant, secrets.token_bytes(512)).status_code != 200
    # Límite conocido (TASK-222, ``clip_storage.py``; xfail de
    # ``test_fleet_clip_uploads_localstack.py::test_a_second_put_on_the_same_key_is_rejected``):
    # sin ``If-None-Match`` firmado, repetir los **mismos** bytes dentro de los 15 minutos se
    # admite; nunca deja otro contenido bajo la clave.
    _put(grant, data)
    versions = gob.s3.list_object_versions(
        Bucket=gob.evidence_bucket, Prefix=grant["storage_key"]
    ).get("Versions", [])
    assert 1 <= len(versions) <= 2
    for version in versions:
        head = gob.s3.head_object(
            Bucket=gob.evidence_bucket,
            Key=grant["storage_key"],
            VersionId=version["VersionId"],
            ChecksumMode="ENABLED",
        )
        assert head["ChecksumSHA256"] == headers["x-amz-checksum-sha256"]
        assert head["ContentLength"] == len(data)

    # Más de 50 MB declarados no se conceden.
    oversized = _grant(gob, zone, _request(zone, data, size_bytes=MAX_CLIP_BYTES + 1))
    assert oversized.status_code == 422, oversized.text
    assert oversized.json()["code"] in {"schema_invalid", "clip_too_large"}


def test_g13_an_uncited_clip_is_orphan_at_24_hours_once_and_never_deleted(
    gob: GobPlatform,
) -> None:
    flow = Onboarding(gob)
    zone = flow.productive_zone()
    started = gob.now() - timedelta(minutes=5)
    orphan_data = secrets.token_bytes(256)
    orphan = ok(_grant(gob, zone, _request(zone, orphan_data)))
    assert _put(orphan, orphan_data).status_code == 200
    cited = flow.finding(zone, started)
    assert flow.post_finding(zone, cited).status_code == 200
    cited_clip = cited["cameras"][0]["clips"][0]["clip_id"]

    gob.advance(DAY - 600)
    gob.run_task("mark_orphan_clips", zone.organization_id)
    assert _status(gob, orphan["clip_id"])["status"] != "orphan"  # todavía no
    gob.advance(1200)
    gob.run_task("mark_orphan_clips", zone.organization_id)
    first = _status(gob, orphan["clip_id"])
    assert first["status"] == "orphan" and first["orphaned_at"] is not None
    assert _status(gob, cited_clip)["status"] == "used"

    # Una segunda pasada no lo vuelve a contar ni lo mueve.
    gob.advance(3600)
    gob.run_task("mark_orphan_clips", zone.organization_id)
    assert _status(gob, orphan["clip_id"])["orphaned_at"] == first["orphaned_at"]
    # Nada se borra del depósito.
    head = gob.s3.head_object(Bucket=gob.evidence_bucket, Key=first["storage_key"])
    assert head["ContentLength"] == len(orphan_data)
    # El inventario lo muestra como aviso del nodo (sesión nueva: la de ayer venció).
    _, admin = gob.person(zone.organization_id, Role.ADMINISTRATOR)
    detail = ok(gob.call("GET", f"/fleet/nodes/{zone.node}", cookie=admin))
    assert "orphan_clips_growing" in json.dumps(detail)


def test_g13_verification_clips_never_become_orphan(gob: GobPlatform) -> None:
    flow = Onboarding(gob)
    zone = flow.zone()
    flow.mount(zone)  # el clip de verificación es del comisionamiento (nota T-02)
    data = secrets.token_bytes(256)
    grant = ok(_grant(gob, zone, _request(zone, data, purpose="verification")))
    assert _put(grant, data).status_code == 200
    gob.advance(DAY + 3600)
    gob.run_task("mark_orphan_clips", zone.organization_id)
    assert _status(gob, grant["clip_id"])["status"] != "orphan"
