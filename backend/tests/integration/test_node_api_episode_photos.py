"""``POST /api/nodes/episode-photos`` contra PostgreSQL 16 real (TASK-572; A-112; BR-GOB-84).

La ruta real (cadena fija, ``NodeApiGate`` con la identidad por certificado contra la base) y la
operación que compone la raíz, como ``vigia_app``. El nodo simulado es el del kit v1.5.0 en
proceso: el generador ``episode_photo`` del kit (imagen sintética codificada en JPEG con la marca)
y su cliente de transporte generado (``transport.api.commissioning.post_episode_photo``).

- **Aceptación**: una foto con imagen del generador del kit responde ``accepted`` y la ``sha256``
  de la imagen guardada es la declarada; una sin imagen, con ``pending_cap``, también, con su
  motivo; repetidas, ``accepted_duplicate`` con el mismo ``Receipt``. ``expires_at`` es
  exactamente ``received_at`` más 30 días (reloj inyectado). Ni los registros ni las métricas de
  la petición llevan la imagen ni su base64 (comprobación por campos).
- **Orden**: retirada la compuerta de montaje y cambiado el catálogo, el mismo cuerpo vuelve a ser
  ``accepted_duplicate``; una foto nueva, ``zone_gate_not_approved``.
- **Rechazos**, uno por caso, sin dejar fila: sin marca, con ``APP1`` o con bytes tras ``EOI``
  (``clip_not_anonymized``); suma o tamaño alterados (``clip_hash_mismatch``); base64 inválido
  (``schema_invalid`` en ``image.data_base64``); más de 307 200 bytes (``payload_too_large``); la
  zona de otro nodo con el cuerpo además inválido (``node_zone_mismatch``); montaje sin aprobar
  (``zone_gate_not_approved``); cámara fuera del catálogo (``schema_invalid`` en ``camera_id``).
- **Idempotencia concurrente**: diez presentaciones simultáneas del mismo ``photo_id`` (dos
  instancias) dejan una fila y el mismo ``Receipt`` en las diez; dos simultáneas con bytes
  distintos dejan una fila y un ``idempotency_conflict``. Una barrera retiene a todas justo antes
  del ``INSERT`` hasta que llegan, así que la carrera se resuelve siempre en la base.
- **Coherencia en la base**: una fila con imagen y motivo, o sin ninguno de los dos, la rechaza el
  ``CHECK`` de ``gob_0042``.
- **Señal ``no_data``** (seguimiento de VIG-449): la ingesta real rechaza una detección con una
  lectura válida de una señal declarada sin dato.

Topes: la base 60 s y la barrera 60 s (eventos, nunca topes de «llegó a tiempo»). Solo datos
generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import base64
import copy
import datetime as dt
import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
from vigia_contracts.conformance.checks._core import draw_seeded
from vigia_contracts.conformance.checks._platform import rebase
from vigia_contracts.conformance.generators import episode_photo
from vigia_contracts.models import api
from vigia_contracts.transport import Client as TransportClient
from vigia_contracts.transport.api.commissioning import post_episode_photo

from tests.episode_photo_support import image, jpeg
from tests.fleet_episode_photo_support import PhotoInstance, PhotoStack, metric_points
from tests.fleet_ingest_support import IngestSite, ingest_stack
from tests.integration.conftest import PostgresEndpoint
from tests.node_api_support import DAY, VERSION, alb_headers
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

SUBMISSIONS: Final = 10
BARRIER_SECONDS: Final = 60.0
SEED: Final = 20261112
LIMIT: Final = 307_200
BASE: Final = "http://testserver/api/nodes"


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[PhotoStack]:
    with ingest_stack(postgres_endpoint, "episode_photos") as built:
        photos = PhotoStack(built)
        try:
            yield photos
        finally:
            photos.run(photos.release())


@pytest.fixture(autouse=True)
def _release_instances(stack: PhotoStack) -> Iterator[None]:
    yield
    stack.run(stack.release())


def _rejection(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("field")


def _receipt(response: Any) -> Any:
    return api.parse_receipt(response.content)


# --- Aceptación con el nodo simulado del kit v1.5.0 -----------------------------------------------


def _kit_photo(stack: PhotoStack, site: IngestSite, *, with_image: bool) -> dict[str, Any]:
    """Una foto del generador del kit para el catálogo publicado de ``site``, trasladada a ahora
    y con un ``photo_id`` nuevo del reloj inyectado."""
    catalog = stack.ingest.catalog(site, stack.now() - 400 * DAY)
    drawn = draw_seeded(
        episode_photo(catalog, node_id=str(site.node_id), with_image=with_image), SEED
    )
    document: dict[str, Any] = rebase(copy.deepcopy(drawn), stack.now() - dt.timedelta(minutes=1))
    document["photo_id"] = str(uuid7(stack.ingest.authz.sessions.clock))
    api.parse_episode_photo_submission(json.dumps(document).encode())
    return document


async def _send_with_kit(
    instance: PhotoInstance, site: IngestSite, document: dict[str, Any]
) -> Any:
    """El cliente de transporte generado del kit, sobre la aplicación en proceso."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=instance.app),
        base_url=BASE,
        headers=alb_headers(site.certificate),
        timeout=120.0,
    ) as http:
        client = TransportClient(BASE, BASE).set_async_httpx_client(http)
        return await post_episode_photo.asyncio_detailed(
            client=client,
            body=api.parse_episode_photo_submission(json.dumps(document).encode()),
            x_vigia_contract_version=VERSION,
            idempotency_key=str(document["photo_id"]),
        )


def _fields_with_bytes(value: object, forbidden: set[str]) -> list[str]:
    """Las claves prohibidas y los valores iguales a uno prohibido (por campos, nunca subcadena)."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"image", "data_base64"}:
                found.append(str(key))
            found += _fields_with_bytes(item, forbidden)
    elif isinstance(value, list | tuple):
        for item in value:
            found += _fields_with_bytes(item, forbidden)
    elif isinstance(value, bytes | bytearray | memoryview):
        found.append("bytes")
    elif isinstance(value, str) and value in forbidden:
        found.append("valor")
    return found


def test_acceptance_with_and_without_image_and_their_duplicates(
    stack: PhotoStack, caplog: pytest.LogCaptureFixture
) -> None:
    site = stack.site()
    with_image = _kit_photo(stack, site, with_image=True)
    without = _kit_photo(stack, site, with_image=False)
    without.pop("camera_id", None)
    without.pop("frame_at", None)
    without["image_absent_reason"] = "pending_cap"
    caplog.set_level(logging.INFO, logger="vigia")
    replies = [stack.run(_send_with_kit(stack.primary, site, doc)) for doc in (with_image, without)]
    assert [reply.status_code for reply in replies] == [200, 200], [r.content for r in replies]
    receipts = [_receipt(reply) for reply in replies]
    assert [receipt.status.value for receipt in receipts] == ["accepted", "accepted"]

    rows = {str(row["photo_id"]): row for row in stack.rows(site.organization_id)}
    stored = rows[with_image["photo_id"]]
    declared = with_image["image"]
    assert hashlib.sha256(bytes(stored["image"])).hexdigest() == declared["sha256"]
    assert stored["sha256"] == declared["sha256"]
    assert bytes(stored["image"]) == base64.b64decode(declared["data_base64"])
    assert (stored["width"], stored["height"]) == (declared["width"], declared["height"])
    assert stored["image_absent_reason"] is None
    absent = rows[without["photo_id"]]
    assert absent["image"] is None and absent["sha256"] is None
    assert absent["image_absent_reason"] == "pending_cap"
    # Vencimiento: exactamente 30 días después de la recepción, con el reloj inyectado.
    for receipt, document in zip(receipts, (with_image, without), strict=True):
        row = rows[document["photo_id"]]
        assert format_timestamp(row["received_at"]) == receipt.received_at
        assert row["expires_at"] == row["received_at"] + dt.timedelta(days=30)
        assert str(row["platform_record_id"]) == receipt.platform_record_id

    # Repetidas: el mismo Receipt, accepted_duplicate y ninguna fila nueva.
    again = [stack.run(_send_with_kit(stack.primary, site, doc)) for doc in (with_image, without)]
    for original, reply in zip(receipts, again, strict=True):
        duplicate = _receipt(reply)
        assert duplicate.status.value == "accepted_duplicate"
        assert (duplicate.platform_record_id, duplicate.received_at) == (
            original.platform_record_id,
            original.received_at,
        )
    assert len(stack.rows(site.organization_id)) == 2

    # Sin bytes en el registro ni en las métricas (por campos).
    forbidden = {declared["data_base64"]}
    records = [r for r in caplog.records if r.name.startswith("vigia")]
    assert any(getattr(r, "vigia_fields", {}).get("result") == "accepted" for r in records)
    leaks = [
        f"{r.name}: {found}"
        for r in records
        if (found := _fields_with_bytes(getattr(r, "vigia_fields", {}), forbidden))
        or r.getMessage() in forbidden
    ]
    assert not leaks, leaks
    points = metric_points(stack.primary.reader)
    assert {
        (attributes["result"], attributes["image_presence"]): value for attributes, value in points
    } == {
        ("accepted", "present"): 1,
        ("accepted", "absent"): 1,
        ("accepted_duplicate", "present"): 1,
        ("accepted_duplicate", "absent"): 1,
    }
    for attributes, _ in points:
        assert set(attributes) <= {"result", "image_presence", "rejection_code"}
        assert not _fields_with_bytes(attributes, forbidden)


# --- Orden: la idempotencia antes que la imagen, el catálogo y la compuerta -----------------------


def test_a_resent_photo_is_a_duplicate_after_the_gate_and_the_catalog_changed(
    stack: PhotoStack,
) -> None:
    site = stack.site()
    document = stack.photo(site)
    first = stack.post(site, document)
    assert first.status_code == 200, first.text
    # Se retira la compuerta de montaje y el catálogo pasa a otra versión del estándar.
    now = stack.now()
    stack.mounting(site, approved=False, at=now - dt.timedelta(minutes=30))
    stack.ingest.publish_catalog(site, now - dt.timedelta(minutes=30), version=2)
    again = stack.post(site, document)
    assert again.status_code == 200, again.text
    assert _receipt(again).status.value == "accepted_duplicate"
    assert _receipt(again).platform_record_id == _receipt(first).platform_record_id
    # Una foto nueva del mismo instante sí la para la compuerta (no pasa por una guarda muda).
    fresh = stack.photo(site, standard_version=2)
    assert _rejection(stack.post(site, fresh)) == (403, "zone_gate_not_approved", "zone_id")
    assert len(stack.rows(site.organization_id)) == 1


# --- Rechazos, uno por caso -----------------------------------------------------------------------


def _oversized(document: dict[str, Any]) -> bytes:
    body = json.dumps(document).encode()
    return body[:-1] + b" " * (LIMIT + 1 - len(body)) + b"}"


def _foreign_zone_and_invalid(site: IngestSite, document: dict[str, Any]) -> dict[str, Any]:
    changed = {**document, "zone_id": str(site.other_zone_id)}
    del changed["model_version"]
    return changed


REJECTIONS: Final[dict[str, tuple[Any, tuple[int, str, str | None]]]] = {
    "sin marca": (
        lambda stack, site: stack.photo(site, photo=image(jpeg(marked=False))),
        (422, "clip_not_anonymized", "image.data_base64"),
    ),
    "con APP1 EXIF": (
        lambda stack, site: stack.photo(site, photo=image(jpeg(exif=True))),
        (422, "clip_not_anonymized", "image.data_base64"),
    ),
    "bytes tras EOI": (
        lambda stack, site: stack.photo(site, photo=image(jpeg(trailing=b"\xff\xd8otra"))),
        (422, "clip_not_anonymized", "image.data_base64"),
    ),
    "sha256 alterada": (
        lambda stack, site: stack.photo(site, photo=image(jpeg(), sha256="ab" * 32)),
        (422, "clip_hash_mismatch", "image.sha256"),
    ),
    "tamaño alterado": (
        lambda stack, site: stack.photo(site, photo=image(jpeg(), size_bytes=10)),
        (422, "clip_hash_mismatch", "image.size_bytes"),
    ),
    "base64 inválido": (
        lambda stack, site: stack.photo(
            site, photo=image(jpeg(), data_base64="no-es-base64!!"), validate=False
        ),
        (422, "schema_invalid", "image.data_base64"),
    ),
    "cámara fuera del catálogo": (
        lambda stack, site: stack.photo(site, camera_id=uuid.uuid4()),
        (422, "schema_invalid", "camera_id"),
    ),
}


@pytest.mark.parametrize("case", sorted(REJECTIONS))
def test_each_rejection_has_its_code_and_writes_nothing(stack: PhotoStack, case: str) -> None:
    site = stack.site()
    build, expected = REJECTIONS[case]
    response = stack.post(site, build(stack, site))
    assert _rejection(response) == expected, response.text
    assert stack.rows(site.organization_id) == []


def test_a_body_over_307200_bytes_is_payload_too_large(stack: PhotoStack) -> None:
    site = stack.site()
    document = stack.photo(site)
    body = _oversized(document)
    assert len(body) == LIMIT + 1
    response = stack.post(site, body, headers=stack.headers(site, document))
    assert response.status_code == 413 and response.json()["code"] == "payload_too_large"
    # Uno que cabe justo, con el mismo contenido, pasa el tamaño (y se acepta).
    fits = json.dumps(document).encode()
    fits = fits[:-1] + b" " * (LIMIT - len(fits)) + b"}"
    assert len(fits) == LIMIT
    accepted = stack.post(site, fits, headers=stack.headers(site, document))
    assert accepted.status_code == 200, accepted.text


def test_the_zone_of_another_node_wins_over_an_invalid_body(stack: PhotoStack) -> None:
    site = stack.site()
    changed = _foreign_zone_and_invalid(site, stack.photo(site))
    assert _rejection(stack.post(site, changed)) == (403, "node_zone_mismatch", "zone_id")
    # La misma petición con la zona propia: la rechaza el esquema.
    own = {**changed, "zone_id": str(site.zone_id)}
    assert _rejection(stack.post(site, own))[:2] == (422, "schema_invalid")
    assert stack.rows(site.organization_id) == []


def test_without_the_mounting_gate_the_photo_is_rejected(stack: PhotoStack) -> None:
    site = stack.site(mounting=False)
    response = stack.post(site, stack.photo(site))
    assert _rejection(response) == (403, "zone_gate_not_approved", "zone_id")
    assert stack.rows(site.organization_id) == []
    # Con la compuerta aprobada desde antes del tramo, la misma zona la acepta.
    stack.mounting(site, approved=True, at=stack.now() - dt.timedelta(hours=1))
    assert stack.post(site, stack.photo(site)).status_code == 200


# --- Idempotencia concurrente ---------------------------------------------------------------------


class Barrier:
    """Retiene los ``count`` primeros ``claim`` (el ``INSERT … ON CONFLICT``) hasta que llegan."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.arrived = 0
        self.all_here = asyncio.Event()

    def wrap(self, instance: PhotoInstance) -> None:
        store: Any = instance.service._deps.store
        original = store.claim

        async def claim(*args: Any, **kwargs: Any) -> Any:
            if self.arrived < self.count:
                self.arrived += 1
                if self.arrived == self.count:
                    self.all_here.set()
                await asyncio.wait_for(self.all_here.wait(), BARRIER_SECONDS)
            return await original(*args, **kwargs)

        store.claim = claim


def _simultaneous(
    stack: PhotoStack, site: IngestSite, documents: list[dict[str, Any]]
) -> list[httpx.Response]:
    other = stack.instance()
    barrier = Barrier(len(documents))
    barrier.wrap(stack.primary)
    barrier.wrap(other)
    instances = [stack.primary if index % 2 == 0 else other for index in range(len(documents))]

    async def run() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(
                    stack.send(site, document, instance)
                    for document, instance in zip(documents, instances, strict=True)
                )
            )
        )

    try:
        responses: list[httpx.Response] = stack.run(run())
        return responses
    finally:
        del stack.primary.service._deps.store.claim  # type: ignore[attr-defined]


def test_ten_simultaneous_submissions_leave_one_row_and_the_same_receipt(
    stack: PhotoStack,
) -> None:
    site = stack.site()
    document = stack.photo(site)
    responses = _simultaneous(stack, site, [document] * SUBMISSIONS)
    assert [r.status_code for r in responses] == [200] * SUBMISSIONS, [r.text for r in responses]
    receipts = [_receipt(response) for response in responses]
    assert sorted(r.status.value for r in receipts) == ["accepted"] + ["accepted_duplicate"] * (
        SUBMISSIONS - 1
    )
    assert {(r.platform_record_id, r.received_at) for r in receipts} == {
        (receipts[0].platform_record_id, receipts[0].received_at)
    }
    (row,) = stack.rows(site.organization_id)
    assert str(row["platform_record_id"]) == receipts[0].platform_record_id


def test_two_simultaneous_submissions_with_different_bytes_leave_one_row_and_a_conflict(
    stack: PhotoStack,
) -> None:
    site = stack.site()
    first = stack.photo(site)
    second = stack.photo(
        site,
        photo_id=uuid.UUID(first["photo_id"]),
        photo=image(jpeg(scan=b"\x9a\xbc\xde\xf0")),
    )
    assert first["photo_id"] == second["photo_id"] and first != second
    responses = _simultaneous(stack, site, [first, second])
    outcomes = sorted(
        "accepted" if r.status_code == 200 else str(r.json()["code"]) for r in responses
    )
    assert outcomes == ["accepted", "idempotency_conflict"], [r.text for r in responses]
    assert len(stack.rows(site.organization_id)) == 1


# --- Coherencia en la base (gob_0042) -------------------------------------------------------------


@pytest.mark.parametrize("variant", ["imagen y motivo", "ni imagen ni motivo"])
def test_the_check_rejects_an_image_with_its_reason_or_neither(
    stack: PhotoStack, variant: str
) -> None:
    site = stack.site()
    data = jpeg()
    with_image = variant == "imagen y motivo"
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        stack.execute(
            "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id,"
            " node_id, episode_id, segment_index, classified_as, family, standard_id,"
            " standard_version, max_confidence, started_at, ended_at, received_at, expires_at,"
            " camera_id, frame_at, width, height, size_bytes, sha256, blur_params_version, image,"
            " image_absent_reason, canonical_sha256, platform_record_id) VALUES ($1, $2, $3, $4,"
            " $5, $6, 0, 'finding', 'coexistence', $7, 1, 0.9, $8, $8, $8,"
            " $8::timestamptz + interval '30 days', $9, $8, 320, 240, $10, $11, 'blur-v1', $12,"
            " $13, $14, $15)",
            site.organization_id,
            uuid.uuid4(),
            site.plant_id,
            site.zone_id,
            site.node_id,
            uuid.uuid4(),
            site.standard_id,
            stack.now(),
            site.camera_id,
            len(data),
            hashlib.sha256(data).hexdigest(),
            data if with_image else None,
            "pending_cap" if with_image else None,
            "cd" * 32,
            uuid.uuid4(),
        )
    assert raised.value.constraint_name in {
        "episode_photo_image_xor_reason",
        "episode_photo_image_facts",
    }


# --- Seguimiento de VIG-449: lectura válida de una señal no_data ----------------------------------


def test_the_real_ingest_rejects_a_valid_reading_of_a_no_data_signal(stack: PhotoStack) -> None:
    ingest = stack.ingest
    site = stack.site()
    at = stack.now() - dt.timedelta(hours=1)
    payload = ingest.catalog(site, at, version=2)
    payload["signals"][0]["no_data"] = True
    payload["standards"][0]["version"] = 1  # la detección cita la versión 1 del estándar
    ingest.execute(
        "UPDATE catalog.zone_catalog_version SET superseded_at = $2"
        " WHERE zone_id = $1 AND catalog_version = 1",
        site.zone_id,
        at,
    )
    ingest.execute(
        "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
        " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
        " payload, envelope, single_occupancy, ledger_record_id)"
        " VALUES ($1, $2, $3, 2, $4, $5, 'administrator', 'Señal sin dato sintética',"
        " ARRAY['signals'], $6::jsonb, $7::jsonb, false, $8)",
        site.organization_id,
        site.plant_id,
        site.zone_id,
        at,
        ingest.authz.operator_id,
        json.dumps(payload),
        json.dumps({"payload": payload}),
        uuid.uuid4(),
    )
    detection = ingest.detection(site)
    assert detection["signals"][0]["valid"] is True
    response = ingest.post(site, IngestKind.DETECTION_FOR_REVIEW, detection)
    assert _rejection(response) == (422, "schema_invalid", "signals[0].valid"), response.text
