"""Lectura de la foto del episodio por la instaladora contra PostgreSQL 16 (TASK-573; A-112).

La aplicación real (``create_app`` con la cadena fija, ``ContextAuthorizer`` con su
``provider_query`` y ``EpisodePhotoReader`` como ``vigia_app``), con fotos sintéticas sembradas en
``fleet.episode_photo`` (``episode_photo_read_support``):

- **Lectura** (criterio 1): la instaladora con concesión vigente recibe exactamente los bytes
  guardados, con la misma ``sha256`` y las cuatro cabeceras, y deja **una** entrada
  ``episode_photo_read`` ``success`` en la cadena del cliente con su concesión, la zona y la foto;
  ninguna lleva bytes ni la suma (``m_cachea``, ``m_sin_auditoria``).
- **Listado**: por clave (50 por defecto, 200 como mucho, ``after``/``next_after``), ``from``/``to``
  sobre ``started_at``, metadatos e ``image_absent_reason`` sin bytes, suma ni URL, y **ninguna**
  entrada ``episode_photo_read`` (bajo concesión, una ``fleet_read`` por petición).
- **Roles y alcance** (criterio 2): los seis roles sin ``commissioning.run`` (también el operador
  de la plataforma), la instaladora sin concesión, con la concesión de otra planta o vencida y la
  foto de otra organización reciben ``not_found`` en el listado y en la imagen, igual que un
  identificador inexistente; cada petición de imagen deja su entrada ``denied``
  (``m_forbidden``, ``m_coordinador_lee``).
- **Zona en productive** (criterio 3): al aprobarse la compuerta de uso, ``not_found`` en el acto
  (``m_sirve_en_productivo``); una foto recibida tras una revocación es de la nueva puesta en
  marcha y se ve.
- **Auditoría caída** (criterio 4): con la escritura de la entrada fallando, no sale ningún byte.
- **Integridad** (criterio 5): con un byte de la fila alterado, ``internal_error`` sin bytes y la
  entrada ``error`` (``m_sin_reverificar``).
- **Vencimiento al leer** (criterio 6): a 1 ms de ``expires_at`` se sirve; en ``expires_at``,
  ``not_found`` (``m_reloj_del_sistema``: el reloj simulado va 2 h por delante de la base).

Topes: 60 s por petición (cliente de la pila). Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest

from tests.authz_support import Site
from tests.episode_photo_read_support import (
    IMAGE_PATH,
    LIST_PATH,
    Stored,
    entries,
    get,
    image_audit,
    stored,
    usage,
)
from tests.fleet_http_support import FleetStack, fleet_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY
from vigia_platform.fleet.application.episode_photo_read import EpisodePhotoReader
from vigia_platform.identity.auth.sessions import SessionCookie
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

MS: Final = dt.timedelta(milliseconds=1)
HOUR: Final = dt.timedelta(hours=1)
IMAGE_HEADERS: Final = {
    "content-type": "image/jpeg",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "content-disposition": "inline",
}
LISTED_KEYS: Final = frozenset(
    {
        "photo_id",
        "zone_id",
        "node_id",
        "episode_id",
        "segment_index",
        "classified_as",
        "family",
        "standard",
        "max_confidence",
        "started_at",
        "ended_at",
        "received_at",
        "expires_at",
        "camera_id",
        "frame_at",
        "image",
        "image_absent_reason",
    }
)
"""Las claves de cada foto del listado: ni ``data``, ni ``sha256``, ni ``url``."""
CLIENT_ROLES: Final = (
    Role.COORDINATOR_SST,
    Role.PLANT_MANAGER,
    Role.ADMINISTRATOR,
    Role.LINE_MANAGER,
    Role.COPASST,
)
Who = tuple[SessionCookie, uuid.UUID | None]


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[FleetStack]:
    with fleet_stack(postgres_endpoint, "episode_photo_read") as built:
        yield built


def _first(site: Site, plant_index: int = 0) -> tuple[uuid.UUID, uuid.UUID]:
    plant = list(site.plants)[plant_index]
    return plant, site.plants[plant][0]


def _photo(stack: FleetStack, site: Site, plant_index: int = 0, **options: Any) -> Stored:
    plant, zone = _first(site, plant_index)
    return stored(stack, site.organization_id, plant, zone, **options)


def _list(stack: FleetStack, who: Who, zone: uuid.UUID, **params: str) -> httpx.Response:
    return get(stack, who, LIST_PATH.format(zone_id=zone), params=params or None)


def _comparable(response: httpx.Response) -> tuple[int, Any]:
    body = response.json()
    return response.status_code, {k: v for k, v in body.items() if k != "correlation_id"}


def _code(response: httpx.Response) -> Any:
    return response.json().get("code")


def _values(document: Any) -> list[Any]:
    """Todos los valores escalares de un JSON (para buscar uno por igualdad, no por subcadena)."""
    if isinstance(document, dict):
        return [v for value in document.values() for v in _values(value)]
    if isinstance(document, list):
        return [v for value in document for v in _values(value)]
    return [document]


# --- Lectura ------------------------------------------------------------------------------------


def test_the_installer_receives_exactly_the_stored_bytes_with_the_four_headers(
    stack: FleetStack,
) -> None:
    site = stack.site()
    photo = _photo(stack, site)
    installer = stack.installer(site)
    response = get(stack, installer, photo.image_path)
    assert response.status_code == 200, response.text
    assert response.content == photo.data
    (row,) = stack.fetch(
        "SELECT sha256, size_bytes FROM fleet.episode_photo WHERE photo_id = $1", photo.photo_id
    )
    assert hashlib.sha256(response.content).hexdigest() == row["sha256"]
    assert len(response.content) == row["size_bytes"]
    # Cada cabecera, una sola vez y con su valor exacto (m_cachea: sin no-store, en rojo).
    assert {name: response.headers.get_list(name) for name in IMAGE_HEADERS} == {
        name: [value] for name, value in IMAGE_HEADERS.items()
    }
    # Una entrada success en la cadena del cliente, con la concesión, la zona y la foto, sin
    # filtros (ni bytes ni suma).
    audit = image_audit(stack, photo.photo_id)
    assert [
        (
            e["organization_id"],
            e["outcome"],
            e["actor_concession_id"],
            e["actor_role_in_use"],
            e["scope_plant_id"],
            e["scope_zone_id"],
            e["resource_kind"],
            e["filters"],
        )
        for e in audit
    ] == [
        (
            site.organization_id,
            "success",
            installer[1],
            "provider_installer",
            photo.plant_id,
            photo.zone_id,
            "episode_photo",
            None,
        )
    ]
    # Cada petición deja la suya: la segunda lectura, otra entrada.
    assert get(stack, installer, photo.image_path).content == photo.data
    assert [e["outcome"] for e in image_audit(stack, photo.photo_id)] == ["success", "success"]


def test_a_record_without_image_is_listed_with_its_reason_and_its_image_is_not_found(
    stack: FleetStack,
) -> None:
    site = stack.site()
    photo = _photo(stack, site, reason="pending_cap")
    installer = stack.installer(site)
    listed = _list(stack, installer, photo.zone_id)
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()["photos"]
    assert (item["photo_id"], item["image"], item["image_absent_reason"]) == (
        str(photo.photo_id),
        None,
        "pending_cap",
    )
    response = get(stack, installer, photo.image_path)
    missing = get(stack, installer, IMAGE_PATH.format(photo_id=uuid.uuid4()))
    assert _comparable(response) == _comparable(missing)
    assert _code(response) == "not_found"
    assert [e["outcome"] for e in image_audit(stack, photo.photo_id)] == ["denied"]


# --- Listado ------------------------------------------------------------------------------------


def test_the_listing_pages_by_key_with_metadata_and_never_bytes_nor_audit(
    stack: FleetStack,
) -> None:
    site = stack.site()
    now = stack.authz.now()
    photos = [
        _photo(stack, site, started_at=now - (index + 1) * dt.timedelta(minutes=5))
        for index in range(5)
    ]
    absent = _photo(stack, site, reason="disk_pressure", started_at=now - 3 * HOUR)
    expected = [*photos, absent]  # started_at descendente
    installer = stack.installer(site)
    zone = photos[0].zone_id
    seen: list[dict[str, Any]] = []
    params: dict[str, str] = {"limit": "2"}
    pages = 0
    while True:
        response = _list(stack, installer, zone, **params)
        assert response.status_code == 200, response.text
        assert response.headers.get_list("cache-control") == ["no-store"]
        body = response.json()
        pages += 1
        seen += body["photos"]
        if body["next_after"] is None:
            break
        assert body["next_after"] == body["photos"][-1]["photo_id"]
        params = {"limit": "2", "after": body["next_after"]}
    assert pages == 3
    assert [item["photo_id"] for item in seen] == [str(p.photo_id) for p in expected]
    for item, photo in zip(seen, expected, strict=True):
        assert set(item) == LISTED_KEYS
        assert item["zone_id"] == str(zone)
        if photo.data is None:
            assert (item["image"], item["image_absent_reason"]) == (None, "disk_pressure")
        else:
            assert item["image"] == {
                "width": 320,
                "height": 240,
                "size_bytes": len(photo.data),
                "blur_params_version": "blur-v1",
            }
            assert item["image_absent_reason"] is None
            # Ni la suma ni los bytes, por igualdad de valores (nunca por subcadena).
            values = _values(item)
            assert hashlib.sha256(photo.data).hexdigest() not in values
            assert base64.b64encode(photo.data).decode() not in values
    # Ninguna entrada episode_photo_read por el listado; bajo concesión, una fleet_read por página.
    for photo in expected:
        assert image_audit(stack, photo.photo_id) == []
    reads = [
        e
        for e in entries(stack, AuditOperation.FLEET_READ.value)
        if e["organization_id"] == site.organization_id
    ]
    assert [(e["outcome"], e["scope_zone_id"], e["result_count"]) for e in reads] == [
        ("success", zone, 2),
        ("success", zone, 2),
        ("success", zone, 2),
    ]


def test_the_listing_filters_from_to_on_the_episode_start(stack: FleetStack) -> None:
    site = stack.site()
    now = stack.authz.now()
    photos = [_photo(stack, site, started_at=now - index * HOUR) for index in range(1, 5)]
    installer = stack.installer(site)
    zone = photos[0].zone_id
    since, until = photos[2].started_at, photos[0].started_at
    response = _list(
        stack,
        installer,
        zone,
        **{"from": format_timestamp(since), "to": format_timestamp(until)},
    )
    assert response.status_code == 200, response.text
    # [from, to): entran la de from y la de en medio; la de to, no.
    assert [item["photo_id"] for item in response.json()["photos"]] == [
        str(photos[1].photo_id),
        str(photos[2].photo_id),
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"after": "no-es-un-cursor"},
        {"after": str(uuid.UUID(int=7))},
        {"from": "2026-11-13T10:00:00.000Z", "to": "2026-11-13T10:00:00.000Z"},
        {"from": "ayer"},
        {"orden": "asc"},
        {"after": "OWN-UPPER"},
    ],
    ids=[
        "limit-0",
        "limit-201",
        "cursor-mal",
        "cursor-ajeno",
        "intervalo-vacio",
        "from-mal",
        "extra",
        "cursor-no-canonico",
    ],
)
def test_out_of_range_listing_parameters_are_invalid_request(
    stack: FleetStack, params: dict[str, str]
) -> None:
    site = stack.site()
    photo = _photo(stack, site)
    if params.get("after") == "OWN-UPPER":
        # El photo_id de una foto viva de la zona, en mayúsculas: el cursor es el canónico o nada.
        params = {"after": str(photo.photo_id).upper()}
    response = _list(stack, stack.installer(site), photo.zone_id, **params)
    assert (response.status_code, _code(response)) == (400, "invalid_request"), response.text


def test_the_listing_gives_fifty_by_default_and_two_hundred_at_most(stack: FleetStack) -> None:
    site = stack.site(plants=1, zones=1)
    now = stack.authz.now()
    for index in range(201):
        _photo(stack, site, started_at=now - index * dt.timedelta(seconds=10))
    installer = stack.installer(site)
    zone = _first(site)[1]
    default = _list(stack, installer, zone).json()
    assert len(default["photos"]) == 50 and default["next_after"] is not None
    widest = _list(stack, installer, zone, limit="200").json()
    assert len(widest["photos"]) == 200 and widest["next_after"] is not None
    rest = _list(stack, installer, zone, limit="200", after=widest["next_after"]).json()
    assert len(rest["photos"]) == 1 and rest["next_after"] is None


# --- Roles y alcance ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", CLIENT_ROLES, ids=[role.value for role in CLIENT_ROLES])
def test_a_client_role_gets_not_found_like_a_missing_photo_and_its_denial_is_audited(
    stack: FleetStack, role: Role
) -> None:
    site = stack.site()
    photo = _photo(stack, site)
    who = stack.member(site, role)
    _assert_hidden(stack, who, photo)
    # La denegación de la cadena (sin commissioning.run) deja su episode_photo_read denied, en la
    # cadena de la organización del contexto y sin zona: la petición no llegó a ver la foto.
    assert [
        (e["organization_id"], e["outcome"], e["scope_zone_id"], e["actor_concession_id"])
        for e in image_audit(stack, photo.photo_id)
    ] == [(site.organization_id, "denied", None, None)]


def test_the_platform_operator_gets_not_found(stack: FleetStack) -> None:
    site = stack.site()
    photo = _photo(stack, site)
    authz = stack.authz
    operator = authz.add_provider_user(Role.PLATFORM_OPERATOR)
    who: Who = (authz.open_session(authz.provider_organization_id, operator), None)
    _assert_hidden(stack, who, photo)
    assert [(e["organization_id"], e["outcome"]) for e in image_audit(stack, photo.photo_id)] == [
        (authz.provider_organization_id, "denied")
    ]


def test_the_installer_of_another_plant_or_organization_gets_not_found(
    stack: FleetStack,
) -> None:
    site, other = stack.site(), stack.site()
    photo = _photo(stack, site, plant_index=1)
    plant = next(iter(site.plants))
    outsiders: dict[str, Who] = {
        "otra planta": stack.installer(site, ScopeLevel.PLANT, plant),
        "otra organización": stack.installer(other),
        "sin concesión": (
            stack.authz.open_session(
                stack.authz.provider_organization_id, stack.authz.add_provider_user()
            ),
            None,
        ),
    }
    for label, who in outsiders.items():
        before = len(image_audit(stack, photo.photo_id))
        _assert_hidden(stack, who, photo)
        outcomes = [e["outcome"] for e in image_audit(stack, photo.photo_id)[before:]]
        assert outcomes == ["denied"], (label, outcomes)
    # La propia planta sí: la prueba no pasa por una guarda que lo rechaza todo.
    assert get(stack, stack.installer(site), photo.image_path).content == photo.data


def test_the_service_itself_denies_without_commissioning_run(stack: FleetStack) -> None:
    """Defensa en profundidad: sin la cadena delante, el servicio vuelve a autorizar sobre la zona
    real (``m_coordinador_lee`` en el servicio). La foto es visible para la RLS de su
    organización, así que la entrada ``denied`` lleva su zona (y ``authorize``, su
    ``authorization_denied``)."""
    site = stack.site()
    photo = _photo(stack, site)
    reader = stack.services.episode_photos
    assert reader is not None
    for role in (Role.COORDINATOR_SST, Role.ADMINISTRATOR):
        context = stack.context(stack.member(site, role))
        with pytest.raises(ResourceNotFound):
            stack.run(reader.image(context, photo.photo_id))
        with pytest.raises(ResourceNotFound):
            stack.run(reader.page(context, photo.zone_id))
    assert [(e["outcome"], e["scope_zone_id"]) for e in image_audit(stack, photo.photo_id)] == [
        ("denied", photo.zone_id)
    ] * 2
    denials = [
        e
        for e in entries(stack, "authorization_denied")
        if str(e["resource_id"]) == str(photo.photo_id)
    ]
    assert [(e["resource_kind"], e["scope_zone_id"]) for e in denials] == [
        ("episode_photo", photo.zone_id)
    ] * 2


def test_the_installer_with_an_expired_concession_gets_not_found(stack: FleetStack) -> None:
    site = stack.site()
    photo = _photo(stack, site)
    authz = stack.authz
    installer = authz.add_provider_user()
    concession = authz.add_concession(
        site.organization_id,
        installer,
        granted_at=authz.now() - dt.timedelta(days=10),
        duration=dt.timedelta(days=2),
    )
    who: Who = (authz.open_session(authz.provider_organization_id, installer), concession)
    for path in (photo.image_path, LIST_PATH.format(zone_id=photo.zone_id)):
        response = get(stack, who, path)
        assert _code(response) in ("not_found", "unauthenticated"), response.text
        assert response.headers.get("content-type") != "image/jpeg"
    assert all(e["outcome"] == "denied" for e in image_audit(stack, photo.photo_id))


def _assert_hidden(stack: FleetStack, who: Who, photo: Stored) -> None:
    """El listado y la imagen de ``photo`` responden igual que los de un recurso inexistente."""
    pairs = (
        (LIST_PATH.format(zone_id=photo.zone_id), LIST_PATH.format(zone_id=uuid.uuid4())),
        (photo.image_path, IMAGE_PATH.format(photo_id=uuid.uuid4())),
    )
    for own, missing in pairs:
        seen, absent = get(stack, who, own), get(stack, who, missing)
        assert _comparable(seen) == _comparable(absent), (own, seen.text)
        assert (seen.status_code, _code(seen)) == (404, "not_found"), seen.text


# --- Zona en productive -------------------------------------------------------------------------


def test_once_the_usage_gate_is_approved_the_photos_answer_not_found_at_once(
    stack: FleetStack,
) -> None:
    site = stack.site()
    photo = _photo(stack, site)
    installer = stack.installer(site)
    assert get(stack, installer, photo.image_path).content == photo.data
    usage(
        stack,
        site.organization_id,
        photo.plant_id,
        photo.zone_id,
        approved=True,
        at=stack.authz.now(),
    )
    _assert_hidden(stack, installer, photo)
    assert [e["outcome"] for e in image_audit(stack, photo.photo_id)] == ["success", "denied"]
    # Revocada después, la foto de antes sigue retirada; la recibida tras la revocación, no.
    stack.tick(60)
    usage(
        stack,
        site.organization_id,
        photo.plant_id,
        photo.zone_id,
        approved=False,
        at=stack.authz.now(),
    )
    stack.tick(60)
    later = _photo(stack, site)
    assert _code(get(stack, installer, photo.image_path)) == "not_found"
    assert get(stack, installer, later.image_path).content == later.data
    listed = _list(stack, installer, photo.zone_id).json()["photos"]
    assert [item["photo_id"] for item in listed] == [str(later.photo_id)]


# --- Auditoría e integridad ---------------------------------------------------------------------


class FailingAudit:
    """``AuditWriter`` que falla al escribir ``episode_photo_read`` (y deja pasar el resto)."""

    def __init__(self, inner: AuditWriter) -> None:
        self._inner = inner

    async def append(self, context: Any, operation: Any, **fields: Any) -> Any:
        if operation == AuditOperation.EPISODE_PHOTO_READ:
            raise RuntimeError("auditoría no disponible (sonda de TASK-573)")
        return await self._inner.append(context, operation, **fields)


def test_with_the_audit_failing_no_byte_leaves(stack: FleetStack) -> None:
    site = stack.site()
    photo = _photo(stack, site)
    installer = stack.installer(site)
    services = getattr(stack.app.state, FLEET_STATE_KEY)
    failing = EpisodePhotoReader(
        database=stack.database,
        authorizer=stack.authz.authorizer,
        audit=FailingAudit(stack.authz.sessions.audit),  # type: ignore[arg-type]
        clock=stack.authz.sessions.clock,
    )
    setattr(stack.app.state, FLEET_STATE_KEY, dataclasses.replace(services, episode_photos=failing))
    coordinator = stack.member(site, Role.COORDINATOR_SST)
    try:
        response = get(stack, installer, photo.image_path)
        # Las denegaciones no dependen de la base: con la auditoría caída siguen siendo not_found.
        missing = get(stack, installer, IMAGE_PATH.format(photo_id=uuid.uuid4()))
        gate = get(stack, coordinator, photo.image_path)
    finally:
        setattr(stack.app.state, FLEET_STATE_KEY, services)
    assert response.status_code == 500, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert _code(response) == "internal_error"
    assert image_audit(stack, photo.photo_id) == []
    assert [(r.status_code, _code(r)) for r in (missing, gate)] == [(404, "not_found")] * 2
    # Con la auditoría de vuelta, la misma petición sí se sirve.
    assert get(stack, installer, photo.image_path).content == photo.data


def test_a_byte_altered_in_the_row_is_never_served_and_is_audited_as_error(
    stack: FleetStack,
) -> None:
    site = stack.site()
    photo, untouched = _photo(stack, site), _photo(stack, site)
    stack.execute(
        "UPDATE fleet.episode_photo SET image = set_byte(image, 20, get_byte(image, 20) # 1)"
        " WHERE photo_id = $1",
        photo.photo_id,
    )
    installer = stack.installer(site)
    response = get(stack, installer, photo.image_path)
    assert (response.status_code, _code(response)) == (500, "internal_error"), response.text
    assert response.headers["content-type"].startswith("application/json")
    assert [e["outcome"] for e in image_audit(stack, photo.photo_id)] == ["error"]
    # La vecina intacta se sirve: la verificación no rechaza todo.
    assert get(stack, installer, untouched.image_path).content == untouched.data


# --- Vencimiento al leer ------------------------------------------------------------------------


def test_one_millisecond_before_expires_at_it_is_served_and_at_expires_at_not_found(
    stack: FleetStack,
) -> None:
    authz = stack.authz
    clock = authz.sessions.clock
    start = clock.now()
    site = stack.site()
    # Vence 2 h por delante de la hora real: con el reloj del sistema en lugar del inyectado, la
    # foto seguiría sirviéndose en expires_at (m_reloj_del_sistema).
    photo = _photo(
        stack, site, received_at=start - dt.timedelta(days=1), expires_at=start + 2 * HOUR
    )
    installer = authz.add_provider_user()
    concession = authz.add_concession(site.organization_id, installer, granted_at=start - HOUR)
    try:
        clock.set(photo.expires_at - MS)
        who: Who = (authz.open_session(authz.provider_organization_id, installer), concession)
        assert get(stack, who, photo.image_path).content == photo.data
        listed = _list(stack, who, photo.zone_id).json()["photos"]
        assert [item["photo_id"] for item in listed] == [str(photo.photo_id)]
        clock.set(photo.expires_at)
        assert _code(get(stack, who, photo.image_path)) == "not_found"
        assert _list(stack, who, photo.zone_id).json()["photos"] == []
    finally:
        clock.set(start)
    assert [e["outcome"] for e in image_audit(stack, photo.photo_id)] == ["success", "denied"]


def test_the_listing_never_carries_the_image_column(stack: FleetStack) -> None:
    """El listado de una foto de 30 KB pesa lo mismo que el de una sin imagen (ni un byte)."""
    site = stack.site()
    big = _photo(stack, site, padding=30_000)
    installer = stack.installer(site)
    body = _list(stack, installer, big.zone_id).content
    assert len(body) < 2_000, len(body)
    assert json.loads(body)["photos"][0]["image"]["size_bytes"] == len(big.data or b"")
