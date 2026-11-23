"""TASK-589: la foto del hallazgo en el detalle, en la ficha de vida y su imagen auditada (A-128;
BR-LAZ-06 y 09; A-88; A-112 «Precisiones del 13-11»).

Sobre PostgreSQL 16 real como ``vigia_app`` y la aplicación real (``detail_stack``: el detalle, la
ficha de vida y ``FindingPhotoReader`` sobre el mismo ``FindingDetailService``), con hallazgos
ingresados por la ruta del nodo y las filas de ``fleet.episode_photo`` sembradas como superusuario
(JPEG **sintético**, ``episode_photo_read_support.photo_bytes``):

- **Solo las fotos del registro**: un hallazgo de dos tramos con las fotos ``productive`` 0 y 1
  del primero las trae en ese registro, por índice, y ninguna en el segundo; no aparecen la foto
  ``commissioning`` del mismo episodio, una ``productive`` de otro ``record_id``, ni una con el
  mismo ``record_id`` de otra zona o de otro tramo (``m_foto_de_otro_registro``).
- **Hora agregada** en zona unipersonal: las marcas de cada foto, como el resto del detalle
  (``m_hora_exacta_unipersonal``).
- **Una sola auditoría**: el detalle y la ficha con 21 fotos por tramo dejan las mismas entradas
  que sin fotos (la ficha, exactamente una) y ninguna ``finding_photo_read``.
- **Imagen auditada**: los bytes exactos con ``success``; ajena, de otra zona, ``commissioning``,
  vencida, sin imagen, de un hallazgo invisible o sin la clave en la cadena → ``not_found`` y
  ``denied``; bytes alterados en la base → ``internal_error`` y ``error``
  (``m_sin_integridad``).
- **Concurrencia**: lecturas lanzadas a la vez que la purga que borra la fila devuelven los bytes
  completos con ``success`` o ``not_found`` con ``denied``, nunca una respuesta parcial.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest

from tests.episode_photo_read_support import photo_bytes
from tests.episode_photo_support import BLUR_VERSION
from tests.fleet_alarm_support import system
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.loop_classification_support import SAME_ORIGIN, Who
from tests.loop_detail_support import DetailStack, detail_stack
from vigia_platform.fleet.application.episode_photo_purge import EpisodePhotoPurger, PurgeReport
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.loop.domain.time_aggregation import presented
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

IMAGE: Final = "/loop/findings/{finding_state_id}/photos/{photo_id}/image"
TIMELINE: Final = "/loop/findings/{finding_state_id}/timeline"
_WINDOW: Final = 60
_READERS: Final = 12
_DAY: Final = dt.timedelta(days=1)

_INSERT: Final = (
    "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id, node_id,"
    " episode_id, segment_index, classified_as, family, standard_id, standard_version,"
    " max_confidence, started_at, ended_at, received_at, expires_at, camera_id, frame_at, width,"
    " height, size_bytes, sha256, blur_params_version, image, image_absent_reason,"
    " canonical_sha256, platform_record_id, zone_mode, record_id, photo_index, photo_role)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, 'finding', 'coexistence', $8, 1, 0.91, $9, $10, $11,"
    " $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27)"
)


@dataclass(frozen=True)
class Photo:
    """Una foto sembrada: su identificador, sus marcas exactas y sus bytes (``None`` sin imagen)."""

    photo_id: uuid.UUID
    data: bytes | None
    started_at: dt.datetime
    ended_at: dt.datetime
    received_at: dt.datetime
    frame_at: dt.datetime | None
    expires_at: dt.datetime | None


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[DetailStack]:
    with detail_stack(postgres_endpoint, "finding_photo_read") as built:
        built.declare_standard(built.world.a)
        yield built


@pytest.fixture(autouse=True)
def _healthy(stack: DetailStack) -> Iterator[None]:
    stack.ports.reset()
    stack.evidence_store.down = False
    yield
    stack.ports.reset()


@pytest.fixture(scope="module")
def coordinator(stack: DetailStack) -> Who:
    return stack.member(stack.world.a)


# --- siembra -------------------------------------------------------------------------------------


def _node_record(subject: IngestedSubject) -> uuid.UUID:
    return uuid.UUID(str(subject.document["finding_id"]))


def _episode(subject: IngestedSubject) -> uuid.UUID:
    return uuid.UUID(str(subject.document["episode"]["episode_id"]))


def _seed(
    stack: DetailStack,
    organization: LoopOrganization,
    *,
    record_id: uuid.UUID | None,
    episode_id: uuid.UUID,
    segment_index: int = 0,
    photo_index: int = 0,
    zone_id: uuid.UUID | None = None,
    mode: str = "productive",
    reason: str | None = None,
    received_at: dt.datetime | None = None,
    expires_at: dt.datetime | None = None,
) -> Photo:
    """Una fila de ``fleet.episode_photo`` (como superusuario) con un JPEG sintético propio."""
    photo_id = uuid.uuid4()
    received = received_at if received_at is not None else stack.world.stack.now()
    started = received - dt.timedelta(seconds=37, milliseconds=123)
    ended = started + dt.timedelta(seconds=4, milliseconds=250)
    data = None if reason is not None else photo_bytes(photo_id)
    frame = None if data is None else started + dt.timedelta(seconds=1, milliseconds=77)
    expires = expires_at
    if mode == "commissioning" and expires is None:
        expires = received + dt.timedelta(days=30)
    stack.world.stack.execute(
        _INSERT,
        organization.organization_id,
        photo_id,
        organization.plant_id,
        zone_id if zone_id is not None else organization.zone_id,
        organization.site.node_id,
        episode_id,
        segment_index,
        organization.site.standard_id,
        started,
        ended,
        received,
        expires,
        None if data is None else organization.site.camera_id,
        frame,
        None if data is None else 320,
        None if data is None else 240,
        None if data is None else len(data),
        None if data is None else hashlib.sha256(data).hexdigest(),
        None if data is None else BLUR_VERSION,
        data,
        reason,
        hashlib.sha256(photo_id.bytes).hexdigest(),
        uuid.uuid4(),
        mode,
        record_id,
        photo_index,
        "peak" if photo_index == 0 else "burst",
    )
    return Photo(photo_id, data, started, ended, received, frame, expires)


def _two_segments(stack: DetailStack) -> tuple[IngestedSubject, IngestedSubject]:
    """Un hallazgo de dos tramos del mismo episodio, ingresados y proyectados."""
    world = stack.world
    first = stack.received()
    ingest = world.stack
    started = dt.datetime.fromisoformat(str(first.document["node_time"]["started_at"]))
    document = ingest.finding(first.organization.site, started + dt.timedelta(seconds=30))
    document["episode"] = {
        **first.document["episode"],
        "segment_index": 1,
        "previous_segment_record_id": str(first.record_id),
    }
    second = world._ingest(first.organization, IngestKind.FINDING, document)
    world.project(second)
    return first, second


# --- peticiones y lecturas -----------------------------------------------------------------------


def _ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _image(stack: DetailStack, who: Who, finding_state_id: uuid.UUID, photo_id: uuid.UUID) -> Any:
    return stack.get(who, IMAGE.format(finding_state_id=finding_state_id, photo_id=photo_id))


async def _aimage(
    stack: DetailStack, who: Who, finding_state_id: uuid.UUID, photo_id: uuid.UUID
) -> httpx.Response:
    cookie, _concession = who
    headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
    path = IMAGE.format(finding_state_id=finding_state_id, photo_id=photo_id)
    return await stack.client.get(path, headers=headers)


def _photo_audit(stack: DetailStack, photo_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in stack.fetch(
            "SELECT organization_id, outcome, actor_id, scope_plant_id, scope_zone_id,"
            " resource_kind, resource_id FROM shared.audit_entry"
            " WHERE operation = 'finding_photo_read' AND resource_id = $1"
            " ORDER BY organization_id, chain_sequence",
            photo_id,
        )
    ]


def _last_sequence(stack: DetailStack, organization: LoopOrganization) -> int:
    (row,) = stack.fetch(
        "SELECT coalesce(max(chain_sequence), 0) AS n FROM shared.audit_entry"
        " WHERE organization_id = $1",
        organization.organization_id,
    )
    return int(row["n"])


def _operations_since(
    stack: DetailStack, organization: LoopOrganization, sequence: int
) -> list[str]:
    return [
        str(row["operation"])
        for row in stack.fetch(
            "SELECT operation FROM shared.audit_entry WHERE organization_id = $1"
            " AND chain_sequence > $2 ORDER BY chain_sequence",
            organization.organization_id,
            sequence,
        )
    ]


def _not_found(response: httpx.Response) -> None:
    assert (response.status_code, response.json()["code"]) == (404, "not_found"), response.text
    assert response.headers["content-type"] != "image/jpeg"


def _stamp(moment: dt.datetime) -> str:
    return format_timestamp(moment.astimezone(dt.UTC))


def _shown(moment: dt.datetime) -> str:
    return _stamp(presented(moment, single_occupancy=True, window_minutes=_WINDOW))


# --- solo las fotos del registro -----------------------------------------------------------------


def test_the_detail_brings_only_the_photos_of_each_record_in_index_order(
    stack: DetailStack, coordinator: Who
) -> None:
    first, second = _two_segments(stack)
    organization = first.organization
    finding_state_id = stack.finding_state_id(first)
    stack.occupancy(finding_state_id, False)
    record, episode = _node_record(first), _episode(first)
    peak = _seed(stack, organization, record_id=record, episode_id=episode, photo_index=0)
    burst = _seed(stack, organization, record_id=record, episode_id=episode, photo_index=1)
    # Lo que no es de ese registro y su tramo:
    shadow = _seed(stack, organization, record_id=None, episode_id=episode, mode="commissioning")
    foreign = _seed(stack, organization, record_id=uuid.uuid4(), episode_id=episode, photo_index=2)
    other_zone = _seed(
        stack,
        organization,
        record_id=record,
        episode_id=episode,
        zone_id=organization.site.other_zone_id,
    )
    other_segment = _seed(
        stack, organization, record_id=record, episode_id=episode, segment_index=1, photo_index=3
    )
    body = _ok(stack.detail(coordinator, finding_state_id))
    by_segment = {r["segment_index"]: r for r in body["records"]}
    assert set(by_segment) == {0, 1}
    assert by_segment[1]["record_id"] == str(second.record_id)
    assert [p["photo_id"] for p in by_segment[0]["photos"]] == [
        str(peak.photo_id),
        str(burst.photo_id),
    ]
    assert by_segment[1]["photos"] == []
    shown = {p["photo_id"] for r in body["records"] for p in r["photos"]}
    for absent in (shadow, foreign, other_zone, other_segment):
        assert str(absent.photo_id) not in shown
    (first_photo, second_photo) = by_segment[0]["photos"]
    assert first_photo == {
        "photo_id": str(peak.photo_id),
        "photo_index": 0,
        "photo_role": "peak",
        "classified_as": "finding",
        "camera_id": str(organization.site.camera_id),
        "frame_at": _stamp(peak.frame_at) if peak.frame_at else None,
        "family": "coexistence",
        "standard": {"standard_id": str(organization.site.standard_id), "version": 1},
        "max_confidence": 0.91,
        "started_at": _stamp(peak.started_at),
        "ended_at": _stamp(peak.ended_at),
        "episode_duration_ms": 4250,
        "received_at": _stamp(peak.received_at),
        "expires_at": None,
        "has_image": True,
        "image_absent_reason": None,
        "width": 320,
        "height": 240,
    }
    assert (second_photo["photo_index"], second_photo["photo_role"]) == (1, "burst")


def test_the_timeline_carries_the_photos_of_the_reception_of_each_segment(
    stack: DetailStack, coordinator: Who
) -> None:
    first, second = _two_segments(stack)
    organization = first.organization
    finding_state_id = stack.finding_state_id(first)
    stack.occupancy(finding_state_id, False)
    peak = _seed(stack, organization, record_id=_node_record(first), episode_id=_episode(first))
    late = _seed(
        stack,
        organization,
        record_id=_node_record(second),
        episode_id=_episode(first),
        segment_index=1,
    )
    body = _ok(stack.get(coordinator, TIMELINE.format(finding_state_id=finding_state_id)))
    photos = {
        m["record_id"]: [p["photo_id"] for p in m["photos"]]
        for m in body["milestones"]
        if m["kind"] == "finding_received"
    }
    assert photos == {
        str(first.record_id): [str(peak.photo_id)],
        str(second.record_id): [str(late.photo_id)],
    }
    assert all(m["photos"] == [] for m in body["milestones"] if m["kind"] != "finding_received")


# --- hora agregada -------------------------------------------------------------------------------


def test_in_a_single_occupancy_zone_every_mark_of_a_photo_is_aggregated(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    organization = finding.organization
    finding_state_id = stack.finding_state_id(finding)
    stack.occupancy(finding_state_id, True, _WINDOW)
    received = stack.world.stack.now()
    photo = _seed(
        stack,
        organization,
        record_id=_node_record(finding),
        episode_id=_episode(finding),
        received_at=received,
        expires_at=received + dt.timedelta(days=400),
    )
    expected = {
        "frame_at": _shown(photo.frame_at) if photo.frame_at else None,
        "started_at": _shown(photo.started_at),
        "ended_at": _shown(photo.ended_at),
        "received_at": _shown(photo.received_at),
        "expires_at": _shown(photo.expires_at) if photo.expires_at else None,
    }
    assert expected["received_at"] != _stamp(photo.received_at)  # la marca exacta cambia
    body = _ok(stack.detail(coordinator, finding_state_id))
    assert body["time_aggregated"] is True
    ((shown,),) = [r["photos"] for r in body["records"]]
    assert {key: shown[key] for key in expected} == expected
    timeline = _ok(stack.get(coordinator, TIMELINE.format(finding_state_id=finding_state_id)))
    ((reception,),) = [m["photos"] for m in timeline["milestones"] if m["photos"]]
    assert {key: reception[key] for key in expected} == expected


# --- una sola auditoría --------------------------------------------------------------------------


def test_21_photos_per_segment_leave_the_same_audit_as_none(
    stack: DetailStack, coordinator: Who
) -> None:
    organization = stack.world.a
    plain = stack.received()
    full = stack.received()
    for index in range(21):
        _seed(
            stack,
            organization,
            record_id=_node_record(full),
            episode_id=_episode(full),
            photo_index=index,
        )
    audited: dict[str, list[str]] = {}
    for name, subject in (("plain", plain), ("full", full)):
        finding_state_id = stack.finding_state_id(subject)
        before = _last_sequence(stack, organization)
        body = _ok(stack.detail(coordinator, finding_state_id))
        audited[name] = _operations_since(stack, organization, before)
        ((record,),) = [body["records"]]
        assert len(record["photos"]) == (21 if name == "full" else 0)
        before = _last_sequence(stack, organization)
        timeline = _ok(stack.get(coordinator, TIMELINE.format(finding_state_id=finding_state_id)))
        assert _operations_since(stack, organization, before) == ["ledger_detail_read"]
        if name == "full":
            ((photos,),) = [[m["photos"] for m in timeline["milestones"] if m["photos"]]]
            assert [p["photo_index"] for p in photos] == list(range(21))
            assert [p["photo_index"] for p in record["photos"]] == list(range(21))
    assert audited["full"] == audited["plain"]
    assert audited["full"].count("ledger_detail_read") == 1
    assert "finding_photo_read" not in audited["full"]


# --- la imagen -----------------------------------------------------------------------------------


def test_the_image_serves_the_exact_bytes_and_audits_success(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    organization = finding.organization
    finding_state_id = stack.finding_state_id(finding)
    photo = _seed(
        stack, organization, record_id=_node_record(finding), episode_id=_episode(finding)
    )
    response = _image(stack, coordinator, finding_state_id, photo.photo_id)
    assert response.status_code == 200, response.text
    assert response.content == photo.data
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in response.headers
    assert _photo_audit(stack, photo.photo_id) == [
        {
            "organization_id": organization.organization_id,
            "outcome": "success",
            "actor_id": stack.context(coordinator).actor.id,
            "scope_plant_id": organization.plant_id,
            "scope_zone_id": organization.zone_id,
            "resource_kind": "finding_photo",
            "resource_id": photo.photo_id,
        }
    ]


def test_a_photo_that_is_not_of_one_of_its_records_is_not_found_and_denied(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    other = stack.received()
    organization = finding.organization
    finding_state_id = stack.finding_state_id(finding)
    record, episode = _node_record(finding), _episode(finding)
    now = stack.world.stack.now()
    refused = {
        "ajena": _seed(
            stack, organization, record_id=_node_record(other), episode_id=_episode(other)
        ),
        "otra zona": _seed(
            stack,
            organization,
            record_id=record,
            episode_id=episode,
            zone_id=organization.site.other_zone_id,
        ),
        "otro tramo": _seed(
            stack, organization, record_id=record, episode_id=episode, segment_index=1
        ),
        "commissioning": _seed(
            stack, organization, record_id=None, episode_id=episode, mode="commissioning"
        ),
        "vencida": _seed(
            stack,
            organization,
            record_id=record,
            episode_id=episode,
            photo_index=1,
            received_at=now - 2 * _DAY,
            expires_at=now - _DAY,
        ),
        "sin imagen": _seed(
            stack,
            organization,
            record_id=record,
            episode_id=episode,
            photo_index=2,
            reason="frame_unavailable",
        ),
    }
    for name, photo in refused.items():
        _not_found(_image(stack, coordinator, finding_state_id, photo.photo_id))
        (entry,) = _photo_audit(stack, photo.photo_id)
        assert (entry["outcome"], entry["scope_zone_id"]) == ("denied", organization.zone_id), name


def test_an_invisible_finding_or_a_role_without_the_key_is_not_found_and_denied(
    stack: DetailStack, coordinator: Who
) -> None:
    other = stack.world.organization()
    stack.declare_standard(other)
    finding = stack.received(other)
    photo = _seed(stack, other, record_id=_node_record(finding), episode_id=_episode(finding))
    finding_state_id = stack.finding_state_id(finding)
    # Otra organización: igual que un hallazgo inexistente, y nada en la cadena de la otra.
    _not_found(_image(stack, coordinator, finding_state_id, photo.photo_id))
    _not_found(_image(stack, coordinator, uuid.uuid4(), photo.photo_id))
    entries = _photo_audit(stack, photo.photo_id)
    assert [(e["organization_id"], e["outcome"]) for e in entries] == [
        (stack.world.a.organization_id, "denied"),
        (stack.world.a.organization_id, "denied"),
    ]
    assert all(e["scope_zone_id"] is None for e in entries)
    # La cadena deniega a un rol sin ``findings.read`` antes de la ruta: también su entrada.
    line_manager = stack.member(other, role=Role.LINE_MANAGER)
    _not_found(_image(stack, line_manager, finding_state_id, photo.photo_id))
    denied = [
        e
        for e in _photo_audit(stack, photo.photo_id)
        if e["organization_id"] == other.organization_id
    ]
    assert [(e["outcome"], e["scope_zone_id"]) for e in denied] == [("denied", None)]
    # El dueño del hallazgo sí la ve.
    owner = stack.member(other)
    response = _image(stack, owner, finding_state_id, photo.photo_id)
    assert (response.status_code, response.content) == (200, photo.data)


def test_altered_bytes_are_never_served_and_audit_an_error(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    organization = finding.organization
    finding_state_id = stack.finding_state_id(finding)
    photo = _seed(
        stack, organization, record_id=_node_record(finding), episode_id=_episode(finding)
    )
    assert photo.data is not None
    altered = photo.data[:-3] + bytes(b ^ 0xFF for b in photo.data[-3:])
    stack.world.stack.execute(
        "UPDATE fleet.episode_photo SET image = $2 WHERE photo_id = $1", photo.photo_id, altered
    )
    response = _image(stack, coordinator, finding_state_id, photo.photo_id)
    assert (response.status_code, response.json()["code"]) == (500, "internal_error")
    assert altered not in response.content and photo.data not in response.content
    assert [e["outcome"] for e in _photo_audit(stack, photo.photo_id)] == ["error"]


# --- concurrencia --------------------------------------------------------------------------------


def test_reads_launched_together_with_the_purge_are_whole_or_not_found(
    stack: DetailStack, coordinator: Who
) -> None:
    finding = stack.received()
    organization = finding.organization
    finding_state_id = stack.finding_state_id(finding)
    received = stack.world.stack.now()
    expires = received + 3 * _DAY
    photo = _seed(
        stack,
        organization,
        record_id=_node_record(finding),
        episode_id=_episode(finding),
        received_at=received,
        expires_at=expires,
    )
    purger = EpisodePhotoPurger(database=stack.database, clock=SimulatedClock(expires))

    async def purge() -> PurgeReport:
        async with stack.database.transaction(system(organization.organization_id)) as transaction:
            report: PurgeReport = await purger.purge(transaction)
            return report

    async def scenario() -> tuple[list[httpx.Response], PurgeReport]:
        reads = [
            _aimage(stack, coordinator, finding_state_id, photo.photo_id) for _ in range(_READERS)
        ]
        *responses, report = await asyncio.wait_for(asyncio.gather(*reads, purge()), timeout=120)
        return list(responses), report

    responses, report = stack.run(scenario())
    assert photo.photo_id in report.expired
    outcomes: list[str] = []
    for response in responses:
        if response.status_code == 200:
            assert response.content == photo.data, "respuesta parcial o distinta"
            outcomes.append("success")
        else:
            _not_found(response)
            outcomes.append("denied")
    audit = [e["outcome"] for e in _photo_audit(stack, photo.photo_id)]
    assert sorted(audit) == sorted(outcomes)
    _not_found(_image(stack, coordinator, finding_state_id, photo.photo_id))
