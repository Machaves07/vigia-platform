"""``GET /commissioning-records/{id}/document`` sobre PostgreSQL 16 real (TASK-217; LC-GOB-08).

Servicios reales como ``vigia_app`` (``tests/close_record_support.py``) y la aplicación real con
``RecordDocumentRenderer`` en ``CatalogHttp``; el render es el de WeasyPrint, envuelto para
guardar el HTML intermedio que recibe.

- **Campo a campo** (NFR-GOB-69): el HTML del documento contiene cada valor de
  ``GET /commissioning-records/{id}`` del mismo acta (actas cerradas por el servicio, también con
  aceptación de falsas alarmas); el PDF es válido y pesa menos de 2 MB.
- **A demanda y sin almacenar** (BR-GOB-50): cada petición vuelve a generar el documento y nada
  se escribe (actas, expediente, eventos, depósito).
- **Escape**: un ``reason_es`` con ``<`` y ``&`` llega escapado al HTML.
- **Tiempo de espera** (NFR-GOB-05): un render que no termina en el tope reducido responde
  ``temporarily_unavailable`` con ``retry_after_seconds`` y ``Retry-After``, sin cuerpo parcial.
- **Alcance**: el acta de otra organización, la de una zona fuera del alcance de una persona con
  alcance de zona y la de otra planta fuera de la concesión responden ``not_found`` igual que un
  acta inexistente (el filtro de zona lo prueba la persona con alcance de otra zona de la misma
  planta); la lectura bajo concesión queda auditada (``catalog_read``, A-56).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any, Final

import httpx
import pytest

from tests.agreements_support import SAME_ORIGIN
from tests.close_record_support import CloseWorld, Zone, close_world
from tests.integration.conftest import PostgresEndpoint
from tests.record_document_support import assert_field_by_field, html_fields, json_leaves
from vigia_platform.catalog.adapters.rendering import RecordDocumentRenderer
from vigia_platform.catalog.adapters.rendering.record_document import render_pdf
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.cpu_pool import CpuPool

pytestmark = pytest.mark.integration

MAX_DOCUMENT_BYTES: Final = 2 * 1024 * 1024
REDUCED_TIMEOUT: Final = 1.0
"""Tope reducido: el doble lento no termina nunca antes de que lo suelte la prueba."""
RELEASE_SECONDS: Final = 60.0
MARKUP_REASON: Final = "Reflejo del portón: altura < 2 m & brillo > 80 % en el turno de noche"


class Recording:
    """El render real de WeasyPrint que guarda cada HTML intermedio que recibe."""

    def __init__(self) -> None:
        self.html: list[str] = []

    def __call__(self, html: str) -> bytes:
        self.html.append(html)
        return render_pdf(html)


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[CloseWorld]:
    with close_world(postgres_endpoint, "record_document") as world:
        yield world


@pytest.fixture(scope="module")
def pool() -> Iterator[CpuPool]:
    created = CpuPool(SystemClock())
    yield created
    created.shutdown(wait=True)


@pytest.fixture(scope="module")
def labels() -> PlatformLabels:
    return PlatformLabels.load()


@pytest.fixture(scope="module")
def recording() -> Recording:
    return Recording()


@pytest.fixture(scope="module")
def client(
    world: CloseWorld, pool: CpuPool, labels: PlatformLabels, recording: Recording
) -> Iterator[httpx.AsyncClient]:
    renderer = RecordDocumentRenderer(pool=pool, labels=labels, pdf=recording)
    created = world.client_with(record_documents=renderer)
    yield created
    world.run(created.aclose())


def _document(world: CloseWorld, client: httpx.AsyncClient, record_id: object, zone: Zone) -> Any:
    return world.request(
        "GET", f"/commissioning-records/{record_id}/document", zone.mounted, None, client
    )


def _get(
    world: CloseWorld, client: httpx.AsyncClient, path: str, cookie: SessionCookie
) -> httpx.Response:
    """Petición de una persona del cliente (sin concesión)."""
    world.advance()
    headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
    response: httpx.Response = world.run(client.request("GET", path, headers=headers))
    return response


def _not_found(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 404, response.text
    body: dict[str, Any] = response.json()
    assert body["code"] == "not_found"
    assert b"%PDF" not in response.content
    return {key: value for key, value in body.items() if key != "correlation_id"}


def _closed(world: CloseWorld, **changes: Any) -> tuple[Zone, uuid.UUID]:
    zone = world.zone(**changes)
    world.ready(zone)
    return zone, world.close(zone).commissioning_record_id


# --- Campo a campo y PDF ------------------------------------------------------------------------


@pytest.mark.parametrize("false_alarm", [False, True])
def test_the_document_matches_the_structured_record_field_by_field(
    world: CloseWorld, client: httpx.AsyncClient, recording: Recording, false_alarm: bool
) -> None:
    zone = world.zone(standards=3)
    world.ready(zone)
    if false_alarm:
        row = zone.session.matrix_rows[0].row_id
        world.passes(zone, rows=[row], per_row=1, result="false_alarm")
    record = world.close(zone, acceptance=MARKUP_REASON if false_alarm else None)
    structured = world.request(
        "GET", f"/commissioning-records/{record.commissioning_record_id}", zone.mounted
    )
    assert structured.status_code == 200, structured.text
    view = structured.json()
    seen = len(recording.html)

    response = _document(world, client, record.commissioning_record_id, zone)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (
        f'inline; filename="acta-{record.commissioning_record_id}.pdf"'
    )
    pdf = response.content
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) < MAX_DOCUMENT_BYTES
    (html,) = recording.html[seen:]
    assert_field_by_field(view, html)
    shown = html_fields(html)
    assert len(shown) == len(json_leaves(view)) > 50
    assert shown["commissioning_record_id"] == str(record.commissioning_record_id)
    if false_alarm:
        assert shown["false_alarm_acceptance.reason_es"] == MARKUP_REASON
        assert "altura &lt; 2 m &amp; brillo &gt; 80 %" in html
        assert "< 2 m &" not in html


def test_the_document_is_generated_on_every_request_and_never_stored(
    world: CloseWorld, client: httpx.AsyncClient, recording: Recording
) -> None:
    zone, record_id = _closed(world)
    before = world.written(zone)
    heads, gets = world.store.heads, world.store.gets
    seen = len(recording.html)

    first = _document(world, client, record_id, zone)
    second = _document(world, client, record_id, zone)

    assert first.status_code == second.status_code == 200
    assert len(recording.html) == seen + 2  # se regenera: nada se sirve de una copia
    assert world.written(zone) == before  # ni acta, ni expediente, ni eventos nuevos
    assert (world.store.heads, world.store.gets) == (heads, gets)  # el depósito, sin tocar


def test_reading_the_document_under_concession_is_audited(
    world: CloseWorld, client: httpx.AsyncClient
) -> None:
    zone, record_id = _closed(world)
    query = (
        "SELECT scope_zone_id FROM shared.audit_entry WHERE operation = 'catalog_read'"
        " AND actor_concession_id = $1"
    )
    count = len(world.fetch(query, zone.mounted.concession))

    assert _document(world, client, record_id, zone).status_code == 200

    rows = world.fetch(query, zone.mounted.concession)
    assert len(rows) == count + 1 and rows[-1]["scope_zone_id"] == zone.zone_id


# --- Tiempo de espera ---------------------------------------------------------------------------


def _stuck() -> tuple[Callable[[str], bytes], threading.Event, threading.Event]:
    started, release = threading.Event(), threading.Event()

    def pdf(html: str) -> bytes:
        started.set()
        release.wait(RELEASE_SECONDS)
        return b"%PDF-1.7 documento tardio %%EOF"

    return pdf, started, release


def test_a_render_that_does_not_finish_answers_temporarily_unavailable_without_a_body(
    world: CloseWorld, pool: CpuPool, labels: PlatformLabels
) -> None:
    zone, record_id = _closed(world)
    pdf, started, release = _stuck()
    renderer = RecordDocumentRenderer(
        pool=pool, labels=labels, timeout_seconds=REDUCED_TIMEOUT, pdf=pdf
    )
    slow = world.client_with(record_documents=renderer)
    try:
        response = _document(world, slow, record_id, zone)
        assert started.is_set()
        assert response.status_code == 503, response.text
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert body["code"] == "temporarily_unavailable"
        assert body["retry_after_seconds"] == 10
        assert response.headers["retry-after"] == "10"
        assert b"%PDF" not in response.content
    finally:
        release.set()
        world.run(slow.aclose())


def test_while_an_orphaned_render_holds_the_slot_the_route_answers_at_once(
    world: CloseWorld, pool: CpuPool, labels: PlatformLabels
) -> None:
    # Revisión de VIG-160: el render agotado sigue en su hilo y conserva su puesto; la petición
    # siguiente no llega al pool y responde temporarily_unavailable sin generar nada.
    zone, record_id = _closed(world)
    pdf, started, release = _stuck()
    calls: list[str] = []

    def counted(html: str) -> bytes:
        calls.append(html)
        return pdf(html)

    renderer = RecordDocumentRenderer(
        pool=pool,
        labels=labels,
        timeout_seconds=REDUCED_TIMEOUT,
        max_concurrent=1,
        pdf=counted,
    )
    slow = world.client_with(record_documents=renderer)
    try:
        first = _document(world, slow, record_id, zone)
        assert started.is_set()
        second = _document(world, slow, record_id, zone)
        for response in (first, second):
            assert response.status_code == 503, response.text
            assert response.json()["code"] == "temporarily_unavailable"
            assert response.json()["retry_after_seconds"] == 10
            assert b"%PDF" not in response.content
        assert len(calls) == 1  # la segunda no se envió al pool
    finally:
        release.set()
        world.run(slow.aclose())


# --- Alcance ------------------------------------------------------------------------------------


def test_another_organizations_record_is_not_found(
    world: CloseWorld, client: httpx.AsyncClient
) -> None:
    mine = world.zone()
    theirs, record_id = _closed(world)

    foreign = _not_found(_document(world, client, record_id, mine))
    missing = _not_found(_document(world, client, uuid.uuid4(), mine))

    assert foreign == missing
    assert theirs.zone_id != mine.zone_id


def test_a_zone_scoped_person_only_reads_the_document_of_their_zone(
    world: CloseWorld, client: httpx.AsyncClient
) -> None:
    # El filtro de zona: dos zonas de la misma planta y la misma organización.
    site = world.walk.a.g.site(plants=1, zones=2)
    (plant, zone_a), (same_plant, zone_b) = site.zones()
    assert plant == same_plant
    zone, record_id = _closed(world, site=site, index=0)
    assert zone.zone_id == zone_a
    people = world.walk.a
    inside = people.signer(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_a)
    outside = people.signer(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_b)
    path = f"/commissioning-records/{record_id}/document"

    allowed = _get(world, client, path, inside.cookie)
    assert allowed.status_code == 200, allowed.text
    assert allowed.content.startswith(b"%PDF-")
    denied = _not_found(_get(world, client, path, outside.cookie))
    missing = _not_found(
        _get(world, client, f"/commissioning-records/{uuid.uuid4()}/document", outside.cookie)
    )
    assert denied == missing


def test_a_plant_concession_does_not_reach_a_record_of_the_other_plant(
    world: CloseWorld, client: httpx.AsyncClient
) -> None:
    site = world.walk.a.g.site(plants=2)
    (plant_a, _), _ = site.zones()
    other, record_id = _closed(world, site=site, index=1)
    _, cookie, concession = world.walk.a.installer_session(site, ScopeLevel.PLANT, plant_a)
    outsider = type(other.mounted)(
        site, plant_a, other.zone_id, other.installer, cookie, concession, {}
    )
    response = world.request(
        "GET", f"/commissioning-records/{record_id}/document", outsider, None, client
    )
    _not_found(response)
