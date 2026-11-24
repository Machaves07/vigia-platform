"""La foto del episodio con la sesión del kit v1.5.0 contra la plataforma real (TASK-572; A-112).

La suite del kit (``vigia-conformance run``) aún no presenta fotos del episodio contra una
plataforma: ``checks/_platform.py::RECORD_OPERATIONS`` del contrato v1.5.0 no tiene
``episode_photo`` (pendiente del contrato, «Precisiones del 12-11» de A-112). Hasta entonces, esta
prueba usa lo mismo que la suite para hablar con la plataforma real —la composición de producción
detrás del balanceador ``nodes.`` con mTLS y la raíz efímera de ``vigia-node-ca``— y comprueba la
**guarda de A-75** de la foto: ``anonymized`` constante ``true``, la marca ``vigia_anonymized=1`` y
los segmentos del JPEG (sin ``APP1`` a ``APP15``, sin ``APP0`` que no sea el ``JFIF`` de 16 bytes y
sin nada tras ``EOI``):

- con ``PlatformSession`` del kit (cliente, cabeceras y registro de rechazos de la suite) y una foto
  del generador ``episode_photo`` del kit (imagen sintética codificada en JPEG con la marca) para el
  catálogo de la zona aprobada del nodo de prueba: ``accepted`` y, repetida,
  ``accepted_duplicate`` con el mismo ``Receipt``;
- ``anonymized`` ``false`` o ``1``: ``schema_invalid`` en ``image.anonymized``;
- sin marca, con ``APP1`` (EXIF), con un ``APP0`` ``JFXX`` o con bytes tras ``EOI``:
  ``clip_not_anonymized`` en ``image.data_base64``; nada queda guardado.

**Foto del hallazgo** (v1.6.0, TASK-603, A-128) con el nodo simulado del kit
(``record_with_photos`` con ``clips=False``: un hallazgo o una detección sin clip y sus fotos
``productive`` con imagen, la ``0``/``peak`` y la ráfaga): las fotos se envían **antes** que su
registro y se aceptan; el registro sin clip se acepta después; cada foto queda con su
``record_id`` e índice; el reenvío es ``accepted_duplicate`` con el mismo ``Receipt`` y otro
``photo_id`` en el índice 0 del tramo, ``schema_invalid`` en ``photo_index``.

Cada rechazo es un ``RejectionResponse`` del contrato (lo lee el lector tolerante de la sesión).
Solo datos sintéticos (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import pytest
from vigia_contracts.conformance.checks import PlatformUrl
from vigia_contracts.conformance.checks._core import derive_seed, draw_seeded
from vigia_contracts.conformance.checks._platform import PlatformSession, Reply
from vigia_contracts.conformance.cli import load_provision
from vigia_contracts.conformance.generators import episode_photo, record_with_photos
from vigia_contracts.conformance.generators._primitives import format_ms, parse_ms
from vigia_contracts.models import api

from tests.conformance.conftest import PlatformTarget, conformance_seed
from tests.conformance.platform_target import ENROLLMENT_PATH, INGEST_PATH
from tests.episode_photo_support import JFXX, image, jpeg

pytestmark = pytest.mark.integration

OPERATION: Final = "post_episode_photo"
PATH: Final = "/episode-photos"
NOT_ANONYMIZED: Final = ("clip_not_anonymized", "image.data_base64")


@pytest.fixture(scope="module")
def session(platform_target: PlatformTarget) -> Iterator[PlatformSession]:
    seed = derive_seed(conformance_seed(), "foto-del-episodio")
    provision = load_provision(platform_target.provisioned.provision_file)
    platform = PlatformUrl(
        platform_target.nodes.url + INGEST_PATH,
        platform_target.app.url + ENROLLMENT_PATH,
        provision.primary,
        second=provision.second,
        certificates=provision.certificates,
        verify=provision.verify,
    )
    built = PlatformSession(platform.open(seed), seed)
    try:
        yield built
    finally:
        built.close()


SHORT_SEGMENT_MS: Final = 5_000
"""Tramo fijo de la foto: corto y terminado un segundo antes de ahora (``rebased``)."""
SEEDS: Final = 50
"""Semillas del generador que recorre la prueba de independencia del sorteo (revisión 1)."""


def _short_segment(document: dict[str, Any]) -> dict[str, Any]:
    """El tramo de ``document`` acotado a ``SHORT_SEGMENT_MS`` que termina donde termina, con el
    reloj sincronizado y un desfase pequeño, el cuadro dentro del tramo y la duración coherente.

    El generador sortea tramos de hasta 1 h y el estado del reloj; con el reloj sincronizado, la
    plataforma mira la compuerta de montaje en ``started_at ± tolerancia`` y la zona de la
    plataforma de conformidad se aprueba al aprovisionarla, segundos antes. Un tramo largo empezaría
    antes de la aprobación (``zone_gate_not_approved``, correcto pero ajeno a lo que se prueba)."""
    node_time = document["node_time"]
    end = parse_ms(node_time["ended_at"])
    start = end - SHORT_SEGMENT_MS
    node_time["started_at"] = format_ms(start)
    node_time["clock"] = {**node_time["clock"], "synchronized": True, "offset_ms": 12}
    document["episode_duration_ms"] = SHORT_SEGMENT_MS
    if "frame_at" in document:
        document["frame_at"] = format_ms(start + 1_000)
    return document


def _photo(
    session: PlatformSession, *, seed: int | None = None, **image_changes: Any
) -> dict[str, Any]:
    """Una foto del generador del kit para la zona aprobada, trasladada a ahora, con un tramo
    corto (``_short_segment``) y un ``photo_id`` nuevo; ``image_changes`` sustituye campos de la
    imagen."""
    zone = session.primary.approved_zone
    assert zone is not None
    drawn = draw_seeded(
        episode_photo(dict(zone.catalog), node_id=session.primary.node_id, with_image=True),
        session.seed if seed is None else seed,
    )
    document = _short_segment(session.rebased(drawn))
    document["photo_id"] = session.fresh_uuid7()
    document["image"] = {**document["image"], **image_changes}
    return document


def _submit(session: PlatformSession, document: dict[str, Any]) -> Reply:
    return session.request(
        OPERATION,
        "POST",
        PATH,
        node_id=session.primary.node_id,
        body=json.dumps(document).encode(),
        headers={"Idempotency-Key": str(document["photo_id"])},
    )


def _stored(platform_target: PlatformTarget, photo_id: str) -> list[Any]:
    rows: list[Any] = platform_target.stack.fetch(
        "SELECT sha256, image_absent_reason FROM fleet.episode_photo WHERE photo_id = $1",
        uuid.UUID(photo_id),
    )
    return rows


def test_a_kit_photo_is_accepted_and_its_repetition_is_a_duplicate(
    session: PlatformSession, platform_target: PlatformTarget
) -> None:
    document = _photo(session)
    api.parse_episode_photo_submission(json.dumps(document).encode())
    first, again = _submit(session, document), _submit(session, document)
    assert first.receipt is not None, first.outcome_es()
    assert again.receipt == {**first.receipt, "status": "accepted_duplicate"}, again.outcome_es()
    rows = _stored(platform_target, document["photo_id"])
    assert [(row["sha256"], row["image_absent_reason"]) for row in rows] == [
        (document["image"]["sha256"], None)
    ]


@pytest.mark.parametrize("value", [False, 1])
def test_a_photo_not_declared_anonymized_is_schema_invalid(
    session: PlatformSession, platform_target: PlatformTarget, value: Any
) -> None:
    document = _photo(session, anonymized=value)
    reply = _submit(session, document)
    assert (reply.status, reply.code, reply.field) == (422, "schema_invalid", "image.anonymized")
    assert _stored(platform_target, document["photo_id"]) == []


@pytest.mark.parametrize(
    "data",
    [jpeg(marked=False), jpeg(exif=True), jpeg(app0=JFXX), jpeg(trailing=b"\xff\xd8otra")],
    ids=["sin-marca", "app1-exif", "app0-jfxx", "cola-tras-eoi"],
)
def test_the_a75_guard_rejects_what_is_not_anonymized(
    session: PlatformSession, platform_target: PlatformTarget, data: bytes
) -> None:
    document = _photo(session)
    document["image"] = image(data, blur_params_version=document["image"]["blur_params_version"])
    reply = _submit(session, document)
    assert (reply.status, reply.code, reply.field) == (422, *NOT_ANONYMIZED), reply.outcome_es()
    assert reply.rejection is not None and reply.rejection["retryable"] is False
    assert _stored(platform_target, document["photo_id"]) == []


def test_any_seed_of_the_generator_is_accepted(
    session: PlatformSession, platform_target: PlatformTarget
) -> None:
    """Revisión de VIG-452: la prueba no depende de la semilla de conformidad. Con el tramo
    corto, las fotos de 50 semillas distintas del generador se aceptan todas."""
    refused = []
    for index in range(SEEDS):
        seed = derive_seed(conformance_seed(), "foto-del-episodio-semillas", index)
        reply = _submit(session, _photo(session, seed=seed))
        if reply.receipt is None:
            refused.append((index, reply.outcome_es()))
    assert refused == [], refused


# --- Foto del hallazgo (v1.6.0, TASK-603) ------------------------------------------------------


def _record_with_photos(session: PlatformSession) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Un registro sin clip del kit y sus fotos ``productive`` para la zona aprobada, trasladados
    juntos a ahora (el mismo instante final), con identificadores nuevos: las fotos citan el
    nuevo identificador del registro."""
    zone = session.primary.approved_zone
    assert zone is not None
    record, photos = draw_seeded(
        record_with_photos(
            dict(zone.catalog),
            node_id=session.primary.node_id,
            clips=False,
            with_image=True,
            burst=True,
        ),
        derive_seed(session.seed, "foto-del-hallazgo"),
    )
    end = session.now()
    prepared = session.prepare(record, end=end)
    field = "finding_id" if "finding_id" in prepared else "detection_id"
    moved = []
    for photo in photos:
        document = session.rebased(photo, end=end)
        document["photo_id"] = session.fresh_uuid7()
        document["record_id"] = prepared[field]
        api.parse_episode_photo_submission(json.dumps(document).encode())
        moved.append(document)
    return prepared, moved


def _productive_rows(platform_target: PlatformTarget, record_id: str) -> list[Any]:
    rows: list[Any] = platform_target.stack.fetch(
        "SELECT photo_id, photo_index, photo_role, sha256 FROM fleet.episode_photo"
        " WHERE zone_mode = 'productive' AND record_id = $1 ORDER BY photo_index",
        uuid.UUID(record_id),
    )
    return rows


def test_kit_photos_before_their_record_without_clips_are_accepted(
    session: PlatformSession, platform_target: PlatformTarget
) -> None:
    record, photos = _record_with_photos(session)
    assert all(camera["clips"] == [] for camera in record["cameras"])
    replies = [_submit(session, photo) for photo in photos]
    assert [reply.receipt is not None for reply in replies] == [True] * len(photos), [
        reply.outcome_es() for reply in replies
    ]
    submitted = session.submit(record)
    assert submitted.receipt is not None, submitted.outcome_es()
    record_id = str(photos[0]["record_id"])
    assert [
        (str(row["photo_id"]), row["photo_index"], row["photo_role"], row["sha256"])
        for row in _productive_rows(platform_target, record_id)
    ] == [
        (photo["photo_id"], photo["photo_index"], photo["photo_role"], photo["image"]["sha256"])
        for photo in photos
    ]
    first = replies[0].receipt
    assert first is not None
    again = _submit(session, photos[0])
    assert again.receipt == {**first, "status": "accepted_duplicate"}, again.outcome_es()
    rival = {**photos[0], "photo_id": session.fresh_uuid7()}
    reply = _submit(session, rival)
    assert (reply.status, reply.code, reply.field) == (422, "schema_invalid", "photo_index")
    assert len(_productive_rows(platform_target, record_id)) == len(photos)
