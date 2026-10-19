"""Descarga de una exportación por HTTP (TASK-345; SUP-U05-40 y 23; PAT-LAZ-SEG-04; NFR-LAZ-16 y
18; PR-LAZ-60 sobre la pila real).

Sobre PostgreSQL 16 como ``vigia_app``, LocalStack y la aplicación real: la exportación la admite
``POST /accreditation/exports`` y la compone el consumidor real con ``ExportStream`` y el
compositor sintético determinista; las URL las firma ``S3Storage`` con el **reloj de la pila**.

- ``GET …/download``: una URL por volumen, de 15 minutos del mismo reloj (``X-Amz-Expires=900``),
  que descarga **esos** bytes como archivo (``Content-Disposition: attachment``); la respuesta de
  la interfaz es ``no-store`` y ``nosniff``. ``POST …/download-url``: la del documento portátil.
- Cada URL deja **exactamente una** entrada ``export_download_granted`` con el artefacto, su
  SHA-256, el actor, el instante y la vigencia, y ``records = access_grant_not_download``.
- Sin ``ready`` (``requested``) o con la descarga vencida (``expired``, a los 30 días del mismo
  reloj): ``409 conflict`` con ``export_state`` en el cuerpo y ninguna concesión.
- Alcance: otra organización, otra planta y una zona de las dos responden ``not_found`` sin
  firmar ni auditar ninguna concesión.
- Concurrencia: N descargas a la vez dan N por volúmenes URL y otras tantas entradas.
- Si una entrada no se escribe, la transacción se deshace: ninguna URL sale y no queda ninguna
  entrada de las anteriores (la auditoría va en la transacción de la emisión).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tests.export_stream_support import StreamingComposer, SyntheticArtifact, SyntheticScope
from tests.export_support import (
    SAME_ORIGIN,
    ExportStack,
    Person,
    export_body,
    export_stack,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from vigia_platform.accreditation.export.stream import ExportStream, ExportStreamSettings
from vigia_platform.accreditation.export.volumes import ExportLocation
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

KIB: Final = 1024
SETTINGS: Final = ExportStreamSettings(
    part_size=5 * KIB * KIB, volume_max_bytes=512 * KIB, manifest_reserve_bytes=64 * KIB
)
SCOPE: Final = SyntheticScope(
    (
        SyntheticArtifact("findings.json", "findings_json", 100 * KIB, True),
        SyntheticArtifact("clips/a.mp4", "clip", 300 * KIB),
        SyntheticArtifact("clips/b.mp4", "clip", 300 * KIB),
    ),
    document_size=20 * KIB,
)
OPERATION: Final = "export_download_granted"


@dataclass
class FlakyAudit:
    """La auditoría de la pila, con un fallo inyectable en la entrada número ``fail_at``."""

    inner: Any
    fail_at: int | None = None
    calls: int = field(default=0)

    async def append(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError("la auditoría no responde")
        return await self.inner.append(*args, **kwargs)


@dataclass
class World:
    stack: ExportStack
    endpoint: LocalStackEndpoint
    bucket: str
    audit: FlakyAudit

    def storage(self) -> S3Storage:
        return S3Storage(self.endpoint.storage_settings(self.bucket), self.stack.clock)


@pytest.fixture(scope="module")
def world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[World]:
    holder: list[FlakyAudit] = []
    with versioned_bucket(localstack_endpoint.aws_client("s3"), "vigia-download") as bucket:

        def storage(clock: Any) -> S3Storage:
            return S3Storage(localstack_endpoint.storage_settings(bucket), clock)

        def audit(inner: Any) -> FlakyAudit:
            holder.append(FlakyAudit(inner))
            return holder[0]

        with export_stack(
            postgres_endpoint, "export_download", storage=storage, audit=audit
        ) as stack:
            yield World(stack, localstack_endpoint, bucket, holder[0])


@dataclass(frozen=True)
class Ready:
    person: Person
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_ids: tuple[uuid.UUID, ...]
    export_id: uuid.UUID
    composer: StreamingComposer


def _requested(world: World, zones: int = 2) -> tuple[Person, uuid.UUID, list[uuid.UUID], Any]:
    stack = world.stack
    site = stack.site(plants=2, zones=zones)
    plant = next(iter(site.plants))
    person = stack.member(site)
    response = stack.request_export(
        person, export_body(plant, list(site.plants[plant]), start=stack.now() - timedelta(days=40))
    )
    assert response.status_code == 202, response.text
    return person, plant, list(site.plants[plant]), (site, uuid.UUID(response.json()["export_id"]))


def _ready(world: World) -> Ready:
    stack = world.stack
    person, plant, zones, (_, export_id) = _requested(world)
    stream = ExportStream(store=world.storage(), settings=SETTINGS)
    composer = StreamingComposer(stream, SCOPE)
    worker = stack.export_worker(composer=composer, uploads=stream)
    stack.run(worker.run(stack.event_context(person.organization_id), export_id))
    assert stack.export_row(export_id)["state"] == "ready"
    return Ready(person, person.organization_id, plant, tuple(zones), export_id, composer)


def _grants(world: World, export_id: uuid.UUID) -> list[Any]:
    return world.stack.fetch(
        "SELECT actor_id, operation, outcome, resource_kind, resource_id, scope_plant_id,"
        " convert_from(filters, 'UTF8') AS filters, occurred_at FROM shared.audit_entry"
        " WHERE operation = $1 AND resource_id = $2 ORDER BY chain_sequence",
        OPERATION,
        export_id,
    )


def _download(world: World, person: Person, export_id: Any) -> httpx.Response:
    return world.stack.send(person, "GET", f"/accreditation/exports/{export_id}/download")


def _document(world: World, person: Person, export_id: Any) -> httpx.Response:
    return world.stack.send(person, "POST", f"/accreditation/exports/{export_id}/download-url")


def _fetch(url: str) -> httpx.Response:
    return httpx.get(url, timeout=60.0)


def test_the_volume_list_gives_one_15_minute_url_per_volume_each_audited(world: World) -> None:
    ready = _ready(world)
    stack = world.stack
    response = _download(world, ready.person, ready.export_id)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    result = ready.composer.results[0]
    assert body["archive_sha256"] == result.archive_sha256
    assert [v["name"] for v in body["volumes"]] == [v.name for v in result.stored.volumes]
    assert len(body["volumes"]) >= 2
    expires = format_timestamp(stack.now() + timedelta(minutes=15))
    for view, volume in zip(body["volumes"], result.stored.volumes, strict=True):
        assert view["expires_at"] == expires
        assert parse_qs(urlparse(view["url"]).query)["X-Amz-Expires"] == ["900"]
        fetched = _fetch(view["url"])
        assert fetched.status_code == 200
        assert fetched.headers["content-disposition"] == "attachment"
        assert hashlib.sha256(fetched.content).hexdigest() == volume.sha256 == view["sha256"]
    grants = _grants(world, ready.export_id)
    assert len(grants) == len(body["volumes"])
    for grant, view in zip(grants, body["volumes"], strict=True):
        filters = json.loads(grant["filters"])
        assert filters == {
            "artifact": view["name"],
            "sha256": view["sha256"],
            "records": "access_grant_not_download",
            "expires_at": view["expires_at"],
        }
        assert (grant["outcome"], grant["resource_kind"]) == ("success", "export")
        assert grant["actor_id"] == ready.person.user_id
        assert grant["scope_plant_id"] == ready.plant_id
        assert grant["occurred_at"] is not None


def test_the_document_url_serves_the_portable_document_and_is_audited(world: World) -> None:
    ready = _ready(world)
    response = _document(world, ready.person, ready.export_id)
    assert response.status_code == 200, response.text
    body = response.json()
    result = ready.composer.results[0]
    assert body["document_sha256"] == result.document_sha256
    assert body["expires_at"] == format_timestamp(world.stack.now() + timedelta(minutes=15))
    fetched = _fetch(body["url"])
    assert fetched.headers["content-disposition"] == "attachment"
    assert hashlib.sha256(fetched.content).hexdigest() == body["document_sha256"]
    location = ExportLocation(ready.organization_id, ready.plant_id, ready.export_id)
    assert urlparse(body["url"]).path.endswith(location.document_key)
    (grant,) = _grants(world, ready.export_id)
    assert json.loads(grant["filters"])["artifact"] == "document.pdf"
    assert json.loads(grant["filters"])["records"] == "access_grant_not_download"


def test_not_ready_and_expired_answer_409_with_the_state_and_grant_nothing(world: World) -> None:
    stack = world.stack
    person, _, _, (_, requested) = _requested(world)
    for call in (_download, _document):
        response = call(world, person, requested)
        assert response.status_code == 409, response.text
        body = response.json()
        assert (body["code"], body["export_state"]) == ("conflict", "requested")
        assert "detail_code" not in body and body["correlation_id"]
        assert response.headers["cache-control"] == "no-store"
    assert _grants(world, requested) == []
    ready = _ready(world)
    started = stack.now()
    try:
        stack.clock.set(started + timedelta(days=30))
        for call in (_download, _document):
            response = call(world, ready.person, ready.export_id)
            assert response.status_code == 409, response.text
            assert response.json()["export_state"] == "expired"
        assert stack.status(ready.person, ready.export_id).json()["export_state"] == "expired"
        # Un milisegundo antes de vencer, todavía se descarga.
        stack.clock.set(started + timedelta(days=30) - timedelta(milliseconds=1))
        assert _download(world, ready.person, ready.export_id).status_code == 200
    finally:
        stack.clock.set(started + timedelta(seconds=1))
    assert len(_grants(world, ready.export_id)) == len(ready.composer.results[0].stored.volumes)


def test_out_of_scope_answers_like_a_missing_export_and_grants_nothing(world: World) -> None:
    stack = world.stack
    ready = _ready(world)
    other_org = stack.member(stack.site(plants=1, zones=1))
    site_plants = [
        row["plant_id"]
        for row in stack.fetch(
            "SELECT plant_id FROM identity.plant WHERE organization_id = $1 AND plant_id <> $2",
            ready.organization_id,
            ready.plant_id,
        )
    ]
    other_plant = Person(ready.organization_id, stack.authz.add_user(ready.organization_id))
    stack.authz.assign(
        ready.organization_id,
        other_plant.user_id,
        Role.PLANT_MANAGER,
        ScopeLevel.PLANT,
        site_plants[0],
    )
    # Con ``export.create`` sobre una sola de las dos zonas: tampoco (todas, como la solicitud).
    one_zone = Person(ready.organization_id, stack.authz.add_user(ready.organization_id))
    stack.authz.assign(
        ready.organization_id,
        one_zone.user_id,
        Role.COORDINATOR_SST,
        ScopeLevel.ZONE,
        ready.zone_ids[0],
    )
    for person in (other_org, other_plant, one_zone):
        for call in (_download, _document):
            response = call(world, person, ready.export_id)
            assert response.status_code == 404, (person, response.text)
            assert response.json()["code"] == "not_found"
    for call in (_download, _document):
        assert call(world, ready.person, uuid.uuid4()).status_code == 404
    assert _grants(world, ready.export_id) == []


def test_concurrent_downloads_each_get_their_urls_and_entries(world: World) -> None:
    stack = world.stack
    ready = _ready(world)
    volumes = len(ready.composer.results[0].stored.volumes)
    count = 6

    cookies = [
        stack.authz.open_session(ready.organization_id, ready.person.user_id) for _ in range(count)
    ]

    async def burst() -> list[httpx.Response]:
        requests = [
            stack.client.get(
                f"/accreditation/exports/{ready.export_id}/download",
                headers={**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"},
            )
            for cookie in cookies
        ]
        return list(await asyncio.gather(*requests))

    responses = stack.run(burst())
    assert [response.status_code for response in responses] == [200] * count
    urls = [volume["url"] for response in responses for volume in response.json()["volumes"]]
    assert len(urls) == count * volumes
    grants = _grants(world, ready.export_id)
    assert len(grants) == count * volumes


def test_if_an_entry_cannot_be_written_no_url_leaves_and_no_entry_stays(world: World) -> None:
    ready = _ready(world)
    assert len(ready.composer.results[0].stored.volumes) >= 2
    world.audit.calls, world.audit.fail_at = 0, 2  # la segunda entrada (segundo volumen) falla
    try:
        response = _download(world, ready.person, ready.export_id)
    finally:
        world.audit.fail_at = None
    assert response.status_code == 500, response.text
    assert "url" not in response.text
    # La primera entrada iba en la misma transacción: también se deshizo.
    assert _grants(world, ready.export_id) == []
    # Y sin fallo, la misma descarga deja sus entradas.
    assert _download(world, ready.person, ready.export_id).status_code == 200
    assert len(_grants(world, ready.export_id)) == len(ready.composer.results[0].stored.volumes)
