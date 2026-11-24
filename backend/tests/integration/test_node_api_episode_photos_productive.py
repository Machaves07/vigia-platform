"""La foto del hallazgo (``productive``) por ``POST /api/nodes/episode-photos`` contra PostgreSQL 16
real (TASK-603; A-128 §5; BR-GOB-84, 92 y 96; BR-CTR-27).

La ruta real (cadena fija, ``NodeApiGate`` con la identidad por certificado contra la base) y la
operación que compone la raíz, como ``vigia_app``, sobre ``tests/fleet_episode_photo_support.py``
(el rastro de los rechazos lo deja el ``IngestService`` real de la pila). Los sitios tienen la
compuerta de uso aprobada desde hace 400 días y, salvo que se diga, **sin** compuerta de montaje:
la foto del hallazgo mira la de uso.

- **Aceptada**: con imagen, guarda su ``record_id``, índice, papel y ``expires_at`` del modo (nulo
  con 0 días; exactamente ``received_at`` + 365 días con 365); el reenvío idéntico es
  ``accepted_duplicate`` con el mismo ``Receipt``; la métrica lleva ``zone_mode = productive``.
- **Compuerta de uso** en ``node_time.started_at``: revocada, o aprobada después del tramo →
  ``zone_gate_not_approved``, ninguna fila y su ``ingest_rejected`` (``record_kind =
  episode_photo``) con auditoría; con la de montaje aprobada y la de uso no, igual
  (``m_productiva_con_compuerta_de_montaje``).
- **Orden de BR-GOB-84**: imagen inválida y compuerta no aprobada → el código de la imagen
  (``m_compuerta_antes_que_imagen``); una foto ya aceptada reenviada con la compuerta retirada →
  ``accepted_duplicate``.
- **Un índice por tramo**: otro ``photo_id`` en el mismo índice → ``schema_invalid`` en
  ``photo_index`` y la primera fila intacta; con la compuerta retirada, la compuerta decide antes
  (el orden de la plataforma simulada); otro índice del tramo, la ráfaga hasta el 20, se acepta.
- **Sin orden con el registro**: la foto se acepta sin que su hallazgo haya llegado
  (``m_exige_registro_previo``; la unión con el detalle está en ``test_finding_photo_read.py``).
- **Imagen rechazada fuera de la base**: ninguna tupla, viva ni muerta, del montón ni de la TOAST
  lleva sus bytes (``pageinspect``, como TASK-572), y su rechazo queda auditado.
- **Concurrencia**: dos presentaciones idénticas simultáneas (dos instancias) dejan una fila y dos
  recibos iguales; dos con el mismo índice y distinto ``photo_id``, una fila y un
  ``schema_invalid`` en ``photo_index``. Una barrera retiene a las dos justo antes del ``INSERT``.

Topes: la base 60 s y la barrera 60 s (eventos, nunca topes de «llegó a tiempo»). Solo datos
generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from vigia_contracts.models import api

from tests.episode_photo_support import image, jpeg
from tests.fleet_episode_photo_support import PhotoInstance, PhotoStack, metric_points
from tests.fleet_ingest_support import IngestSite, ingest_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.shared.ids import uuid7

pytestmark = pytest.mark.integration

BARRIER_SECONDS: Final = 60.0
HOUR: Final = dt.timedelta(hours=1)
MARKER: Final = b"VIG487-IMAGEN-RECHAZADA-" * 4
"""Bytes sintéticos reconocibles dentro de los datos del barrido de la imagen rechazada."""
REJECTED_FIELDS: Final = {"node_id", "zone_id", "record_kind", "code", "correlation_id"}


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[PhotoStack]:
    with ingest_stack(postgres_endpoint, "episode_photos_productive") as built:
        photos = PhotoStack(built)
        try:
            yield photos
        finally:
            photos.run(photos.release())


@pytest.fixture(autouse=True)
def _release_instances(stack: PhotoStack) -> Iterator[None]:
    yield
    stack.run(stack.release())


# --- Presentaciones y lecturas --------------------------------------------------------------------


def _site(stack: PhotoStack, **options: Any) -> IngestSite:
    """Un sitio con la compuerta de uso aprobada y, por defecto, sin la de montaje."""
    options.setdefault("mounting", False)
    return stack.site(**options)


def _productive(
    stack: PhotoStack,
    site: IngestSite,
    *,
    record_id: uuid.UUID | None = None,
    index: int = 0,
    episode: dict[str, Any] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Una ``EpisodePhotoSubmission`` ``productive`` válida de ``site`` (la del kit v1.6.0:
    ``record_id`` v7, ``peak`` el 0 y ``burst`` los demás)."""
    document = stack.photo(site, validate=False, **options)
    document["zone_mode"] = "productive"
    document["record_id"] = str(record_id or uuid7(stack.ingest.authz.sessions.clock))
    document["photo_index"] = index
    document["photo_role"] = "peak" if index == 0 else "burst"
    if episode is not None:
        document["episode"] = dict(episode)
    api.parse_episode_photo_submission(json.dumps(document).encode())
    return document


def _rows(stack: PhotoStack, site: IngestSite) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in stack.fetch(
            "SELECT photo_id, zone_mode, record_id, photo_index, photo_role, episode_id,"
            " segment_index, sha256, received_at, expires_at, platform_record_id"
            " FROM fleet.episode_photo WHERE organization_id = $1 ORDER BY photo_index, photo_id",
            site.organization_id,
        )
    ]


def _rejection(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("field")


def _receipt(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    return api.parse_receipt(response.content)


def _points(stack: PhotoStack) -> dict[tuple[str, str | None], float]:
    """``(resultado, modo) → valor`` de ``episode_photo_received_total`` de la instancia
    principal (se acumula en el módulo: las pruebas miran la diferencia)."""
    points: dict[tuple[str, str | None], float] = {}
    for attributes, value in metric_points(stack.primary.reader):
        key = (str(attributes["result"]), attributes.get("zone_mode"))
        points[key] = points.get(key, 0) + value
    return points


def _trail(stack: PhotoStack, site: IngestSite) -> tuple[list[Any], list[Any]]:
    """Los ``ingest_rejected`` de la cadena y las entradas de auditoría de la organización."""
    ingest = stack.ingest
    return (
        ingest.records("ingest_rejected", site.organization_id),
        ingest.audit(site.organization_id),
    )


# --- Aceptada, con la retención de su modo --------------------------------------------------------


def test_a_productive_photo_is_stored_with_its_record_index_role_and_mode_retention(
    stack: PhotoStack,
) -> None:
    site = _site(stack)
    before = _points(stack)
    record_id = uuid7(stack.ingest.authz.sessions.clock)
    peak = _productive(stack, site, record_id=record_id)
    burst = _productive(stack, site, record_id=record_id, index=20, episode=peak["episode"])
    receipts = [_receipt(stack.post(site, document)) for document in (peak, burst)]
    assert [receipt.status.value for receipt in receipts] == ["accepted", "accepted"]
    rows = _rows(stack, site)
    assert [
        (row["zone_mode"], row["record_id"], row["photo_index"], row["photo_role"]) for row in rows
    ] == [("productive", record_id, 0, "peak"), ("productive", record_id, 20, "burst")]
    for row, receipt, document in zip(rows, receipts, (peak, burst), strict=True):
        assert str(row["platform_record_id"]) == receipt.platform_record_id
        assert row["sha256"] == document["image"]["sha256"]
        assert row["expires_at"] is None  # 0 días: sin vencimiento automático (BR-NUC-65)
    # Reenvío idéntico: el mismo Receipt y ninguna fila nueva (BR-CTR-27).
    again = _receipt(stack.post(site, peak))
    assert again.status.value == "accepted_duplicate"
    assert (again.platform_record_id, again.received_at) == (
        receipts[0].platform_record_id,
        receipts[0].received_at,
    )
    assert len(_rows(stack, site)) == 2
    assert _trail(stack, site) == ([], [])
    after = _points(stack)
    assert {key: after[key] - before.get(key, 0) for key in after} == {
        ("accepted", "productive"): 2,
        ("accepted_duplicate", "productive"): 1,
    }
    for attributes, _ in metric_points(stack.primary.reader):
        assert set(attributes) <= {"result", "rejection_code", "image_presence", "zone_mode"}


def test_with_365_days_the_photo_expires_exactly_365_days_after_its_reception(
    stack: PhotoStack,
) -> None:
    site = _site(stack)
    instance = stack.instance(finding_retention_days=365)
    document = _productive(stack, site)
    receipt = _receipt(stack.run(stack.send(site, document, instance)))
    (row,) = _rows(stack, site)
    assert str(row["platform_record_id"]) == receipt.platform_record_id
    assert row["expires_at"] == row["received_at"] + dt.timedelta(days=365)


# --- Compuerta de uso en el instante del tramo -------------------------------------------------


def test_without_the_usage_gate_at_the_segment_start_the_photo_is_rejected_with_its_trail(
    stack: PhotoStack,
) -> None:
    # Montaje aprobado y uso nunca aprobado: la foto del hallazgo mira la de uso.
    site = _site(stack, mounting=True, usage_approved=False)
    response = stack.post(site, _productive(stack, site))
    assert _rejection(response) == (403, "zone_gate_not_approved", "zone_id"), response.text
    assert _rows(stack, site) == []
    records, audit = _trail(stack, site)
    (record,) = records
    assert set(record["content"]) >= REJECTED_FIELDS
    assert (record["content"]["record_kind"], record["content"]["code"]) == (
        "episode_photo",
        "zone_gate_not_approved",
    )
    assert record["content"]["zone_id"] == str(site.zone_id)
    assert [entry["filters"] for entry in audit] == [
        {"code": "zone_gate_not_approved", "record_kind": "episode_photo"}
    ]
    # Aprobada después de empezar el tramo (más allá de la tolerancia): sigue rechazada.
    stack.ingest.usage(site, approved=True, at=stack.now() - dt.timedelta(minutes=5))
    late = _productive(stack, site, started=stack.now() - dt.timedelta(minutes=10))
    assert _rejection(stack.post(site, late))[1] == "zone_gate_not_approved"
    # Un tramo que empieza con el uso ya aprobado se acepta, sin compuerta de montaje nueva.
    fresh = _productive(stack, site, started=stack.now() - dt.timedelta(minutes=1))
    assert _receipt(stack.post(site, fresh)).status.value == "accepted"


def test_the_usage_gate_decides_even_without_the_mounting_gate(stack: PhotoStack) -> None:
    # m_productiva_con_compuerta_de_montaje: sin montaje aprobado y con uso aprobado, se acepta.
    site = _site(stack, mounting=False)
    assert (
        stack.fetch(
            "SELECT 1 FROM catalog.gate_state_history WHERE zone_id = $1 AND gate = 'mounting'",
            site.zone_id,
        )
        == []
    )
    assert _receipt(stack.post(site, _productive(stack, site))).status.value == "accepted"
    # Y con el uso revocado desde antes del tramo, aunque el montaje esté aprobado, no.
    stack.mounting(site, approved=True, at=stack.now() - 2 * HOUR)
    stack.ingest.usage(site, approved=False, at=stack.now() - HOUR)
    response = stack.post(site, _productive(stack, site))
    assert _rejection(response) == (403, "zone_gate_not_approved", "zone_id"), response.text


# --- Orden de BR-GOB-84 -------------------------------------------------------------------------


def test_a_bad_image_wins_over_the_gate_and_a_resend_wins_over_both(stack: PhotoStack) -> None:
    site = _site(stack)
    accepted = _productive(stack, site)
    first = _receipt(stack.post(site, accepted))
    stack.ingest.usage(site, approved=False, at=stack.now() - HOUR)
    # m_compuerta_antes_que_imagen: imagen inválida y compuerta retirada → el código de la imagen.
    bad = _productive(stack, site, photo=image(jpeg(marked=False)))
    assert _rejection(stack.post(site, bad)) == (422, "clip_not_anonymized", "image.data_base64")
    altered = _productive(stack, site, photo=image(jpeg(), sha256="ab" * 32))
    assert _rejection(stack.post(site, altered)) == (422, "clip_hash_mismatch", "image.sha256")
    # Idempotencia antes que la imagen y la compuerta: el reenvío es el duplicado original.
    again = _receipt(stack.post(site, accepted))
    assert again.status.value == "accepted_duplicate"
    assert again.platform_record_id == first.platform_record_id
    # La compuerta sí para a una foto nueva con imagen válida (la sonda no es muda).
    fresh = _productive(stack, site)
    assert _rejection(stack.post(site, fresh))[1] == "zone_gate_not_approved"
    assert [row["photo_id"] for row in _rows(stack, site)] == [uuid.UUID(accepted["photo_id"])]
    _, audit = _trail(stack, site)
    assert [entry["filters"]["code"] for entry in audit] == [
        "clip_not_anonymized",
        "clip_hash_mismatch",
        "zone_gate_not_approved",
    ]


def test_the_trail_is_only_for_productive_and_the_shadow_keeps_its_exception(
    stack: PhotoStack,
) -> None:
    # BR-GOB-96 en productive; en commissioning, como TASK-572 (A-112): sin ingest_rejected.
    site = _site(stack, mounting=True)
    shadow = stack.photo(site, photo=image(jpeg(marked=False)))
    assert _rejection(stack.post(site, shadow))[1] == "clip_not_anonymized"
    foreign = {**stack.photo(site), "zone_id": str(site.other_zone_id)}
    assert _rejection(stack.post(site, foreign))[1] == "node_zone_mismatch"
    assert _trail(stack, site) == ([], [])
    # La misma zona ajena en productive (antes del esquema): rastro con su record_kind.
    wrong = {**_productive(stack, site), "zone_id": str(site.other_zone_id)}
    del wrong["model_version"]
    assert _rejection(stack.post(site, wrong)) == (403, "node_zone_mismatch", "zone_id")
    records, audit = _trail(stack, site)
    assert [(r["content"]["record_kind"], r["content"]["code"]) for r in records] == [
        ("episode_photo", "node_zone_mismatch")
    ]
    assert [entry["filters"] for entry in audit] == [
        {"code": "node_zone_mismatch", "record_kind": "episode_photo"}
    ]


# --- Un índice por tramo ------------------------------------------------------------------------


def test_a_second_photo_id_in_the_same_index_is_schema_invalid_and_the_first_stays(
    stack: PhotoStack,
) -> None:
    site = _site(stack)
    record_id = uuid7(stack.ingest.authz.sessions.clock)
    first = _productive(stack, site, record_id=record_id)
    _receipt(stack.post(site, first))
    (before,) = _rows(stack, site)
    rival = _productive(stack, site, record_id=record_id, episode=first["episode"])
    assert rival["photo_id"] != first["photo_id"]
    response = stack.post(site, rival)
    assert _rejection(response) == (422, "schema_invalid", "photo_index"), response.text
    assert json.loads(response.content)["retryable"] is False
    assert _rows(stack, site) == [before]
    # Otro índice del mismo tramo sí entra (la ráfaga de la fase 2, sin cambios de código).
    burst = _productive(stack, site, record_id=record_id, index=1, episode=first["episode"])
    assert _receipt(stack.post(site, burst)).status.value == "accepted"
    # Con la compuerta retirada decide la compuerta, antes que el índice (plataforma simulada).
    stack.ingest.usage(site, approved=False, at=stack.now() - HOUR)
    late = _productive(stack, site, record_id=record_id, episode=first["episode"])
    assert _rejection(stack.post(site, late))[1] == "zone_gate_not_approved"
    _, audit = _trail(stack, site)
    assert [entry["filters"]["code"] for entry in audit] == [
        "schema_invalid",
        "zone_gate_not_approved",
    ]


# --- Sin orden con el registro ------------------------------------------------------------------


def test_a_photo_is_accepted_before_its_finding_arrives(stack: PhotoStack) -> None:
    # m_exige_registro_previo: ningún finding_received de la zona y la foto se acepta.
    site = _site(stack)
    finding = stack.ingest.finding(site)
    photo = _productive(
        stack,
        site,
        record_id=uuid.UUID(finding["finding_id"]),
        episode=finding["episode"],
    )
    assert stack.ingest.records("finding_received", site.organization_id) == []
    assert _receipt(stack.post(site, photo)).status.value == "accepted"
    (row,) = _rows(stack, site)
    assert str(row["record_id"]) == finding["finding_id"]


# --- Imagen rechazada fuera de la base (A-75, A-112) ----------------------------------------------


def _heap_hits(stack: PhotoStack, marker: bytes) -> int:
    """Tuplas (vivas o muertas) de ``fleet.episode_photo`` y de su TOAST con ``marker``."""
    stack.execute("CREATE EXTENSION IF NOT EXISTS pageinspect")
    relations = ["fleet.episode_photo"]
    (toast,) = stack.fetch(
        "SELECT reltoastrelid::regclass::text AS name FROM pg_class"
        " WHERE oid = 'fleet.episode_photo'::regclass AND reltoastrelid <> 0"
    ) or [{"name": None}]
    if toast["name"] is not None:
        relations.append(toast["name"])
    hits = 0
    for relation in relations:
        (row,) = stack.fetch(
            "SELECT count(*) AS n FROM generate_series(0,"
            " (pg_relation_size($1::regclass) / current_setting('block_size')::int) - 1) AS p,"
            " heap_page_items(get_raw_page($1::text, p::int)) AS t"
            " WHERE t.t_data IS NOT NULL AND position($2::bytea IN t.t_data) > 0",
            relation,
            marker,
        )
        hits += int(row["n"])
    return hits


@pytest.mark.parametrize(
    "defect",
    [{"marked": False}, {"exif": True}, {"trailing": b"cola"}],
    ids=["sin-marca", "app1-exif", "cola-tras-eoi"],
)
def test_a_rejected_productive_image_never_reaches_the_heap_nor_the_toast(
    stack: PhotoStack, defect: dict[str, Any]
) -> None:
    site = _site(stack)
    marker = MARKER + uuid.uuid4().hex.encode()
    data = jpeg(scan=b"\x12\x34" + marker, **defect)
    response = stack.post(site, _productive(stack, site, photo=image(data)))
    assert _rejection(response)[1] == "clip_not_anonymized", response.text
    assert _rows(stack, site) == []
    assert _heap_hits(stack, marker) == 0
    # La sonda no es muda: una imagen aceptada con su propia marca sí está en el montón.
    kept = MARKER + uuid.uuid4().hex.encode()
    good = _productive(stack, site, photo=image(jpeg(scan=b"\x12\x34" + kept)))
    assert _receipt(stack.post(site, good)).status.value == "accepted"
    assert _heap_hits(stack, kept) == 1


# --- Concurrencia: una garantía atómica, una prueba -------------------------------------------


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
    instances = [stack.instance(), stack.instance(stack.ingest.database())]
    barrier = Barrier(len(documents))
    for instance in instances:
        barrier.wrap(instance)

    async def run() -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                *(
                    stack.send(site, document, instances[index % 2])
                    for index, document in enumerate(documents)
                )
            )
        )

    responses: list[httpx.Response] = stack.run(run())
    return responses


def test_two_identical_simultaneous_photos_leave_one_row_and_two_equal_receipts(
    stack: PhotoStack,
) -> None:
    site = _site(stack)
    document = _productive(stack, site)
    responses = _simultaneous(stack, site, [document, document])
    receipts = [_receipt(response) for response in responses]
    assert sorted(receipt.status.value for receipt in receipts) == [
        "accepted",
        "accepted_duplicate",
    ]
    assert len({(r.platform_record_id, r.received_at) for r in receipts}) == 1
    (row,) = _rows(stack, site)
    assert str(row["platform_record_id"]) == receipts[0].platform_record_id


def test_two_simultaneous_photo_ids_in_the_same_index_leave_one_row_and_one_rejection(
    stack: PhotoStack,
) -> None:
    site = _site(stack)
    record_id = uuid7(stack.ingest.authz.sessions.clock)
    first = _productive(stack, site, record_id=record_id)
    second = _productive(stack, site, record_id=record_id, episode=first["episode"])
    responses = _simultaneous(stack, site, [first, second])
    outcomes = sorted(
        "accepted" if r.status_code == 200 else f"{r.json()['code']}:{r.json()['field']}"
        for r in responses
    )
    assert outcomes == ["accepted", "schema_invalid:photo_index"], [r.text for r in responses]
    (row,) = _rows(stack, site)
    winner = next(r for r in responses if r.status_code == 200)
    assert str(row["platform_record_id"]) == _receipt(winner).platform_record_id
