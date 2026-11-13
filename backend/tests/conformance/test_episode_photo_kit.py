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
from vigia_contracts.conformance.generators import episode_photo
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


def _photo(session: PlatformSession, **image_changes: Any) -> dict[str, Any]:
    """Una foto del generador del kit para la zona aprobada, trasladada a ahora y con un
    ``photo_id`` nuevo; ``image_changes`` sustituye campos de la imagen."""
    zone = session.primary.approved_zone
    assert zone is not None
    drawn = draw_seeded(
        episode_photo(dict(zone.catalog), node_id=session.primary.node_id, with_image=True),
        session.seed,
    )
    document = session.rebased(drawn)
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
