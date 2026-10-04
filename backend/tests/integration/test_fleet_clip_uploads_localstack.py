"""Concesiones de clip y clip de verificación de extremo a extremo: PostgreSQL 16 y LocalStack.

TASK-222 (LC-GOB-13; BR-GOB-93; PAT-GOB-SEG-03; nº 32). Las rutas reales del contrato
(``create_app`` con la cadena fija y la ``NodeApiGate`` real sobre ``PostgresNodeContextStore``)
con los manejadores de la tarea, los servicios sobre un ``S3Storage`` real contra un depósito
versionado de LocalStack y la ruta de consola por su servicio con contextos de sesión reales
(``tests/fleet_clip_support.py``). El almacén va envuelto en un doble que cuenta cada operación y
hace fallar cualquiera que no sea ``head_object`` o ``presign_put``: nunca hay ``get_object``.

- Criterio 2: la URL acepta el ``PUT`` con exactamente las cabeceras del mapa; sin la suma, con
  otra suma u otros bytes, el almacén lo rechaza; la clave lleva organización, planta, zona y nodo;
  ``expires_at - issued_at = 15 min``. El rechazo del **segundo** ``PUT`` de los mismos bytes
  dentro de la vigencia exige ``If-None-Match: *``, que el contrato fijado no deja poner en
  ``required_headers``: la prueba queda marcada (``xfail`` estricto) y otra muestra que LocalStack
  sí hace cumplir la escritura condicional cuando la URL la firma.
- Criterio 3: la confirmación distingue ``clip_missing``, ``clip_hash_mismatch``,
  ``clip_too_large`` y ``clip_not_anonymized`` solo con ``head_object``; una concesión
  ``evidence`` responde ``schema_invalid`` 422; el recibo es el ``VerificationClip`` creado.
- Criterio 4 (primera mitad): 5 confirmaciones simultáneas crean **un** clip con el mismo recibo.
- Criterio 5: zona no asignada o de otra organización, clip de otro nodo y listado de otra
  organización (con la sonda de cada filtro).
- Criterio 6 (concesión): almacén sin respuesta → ``storage_unavailable`` en ≤ 5 s y sin fila.
- Criterio 7: ninguna URL prefirmada en registros, métricas, trazas ni eventos; contadores por
  nodo sin zona.
- Repetición (PR-GOB-20), listado con cursor, ``first_served_at`` una sola vez (también con dos
  lecturas a la vez) y lectura del proveedor auditada.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from opentelemetry import trace as otel_trace
from sqlalchemy import text
from vigia_contracts.models.api import (
    parse_rejection_response,
    parse_response_clip_upload_grant,
    parse_response_verification_clip_receipt,
)

from tests.api_support import World
from tests.dispatch_support import metric_points
from tests.factories import uuid7
from tests.fleet_clip_support import (
    LONG_TIMEOUT_SECONDS,
    ClipWorld,
    CountingStorage,
    HttpsUrls,
    Hung,
    clip_world,
    video,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
from tests.node_api_support import Probe, node_app, node_gate
from tests.properties.gob.telemetry_harness import TelemetryCapture
from vigia_platform.fleet.adapters.postgres.clip_grant_store import PostgresClipGrants
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.application.clip_confirmation import (
    ClipConfirmationService,
    CommissioningClips,
    CommissioningClipsRequestInvalid,
)
from vigia_platform.fleet.application.clip_grants import ClipGrantService, ZoneNotAssigned
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.node_api.identity import PostgresNodeContextStore
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.routes.clip_uploads import clip_upload_operation
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.clock import SimulatedClock, SystemClock
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.storage import StorageUnavailable, sha256_b64

pytestmark = pytest.mark.integration

FIFTEEN_MINUTES = timedelta(minutes=15)
WALL_CLOCK = SystemClock()
"""Solo para medir el tope de producción del almacén sin respuesta (5 s)."""


@pytest.fixture(scope="module")
def clips(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[ClipWorld]:
    with clip_world(postgres_endpoint, localstack_endpoint, "fleet_clips") as world:
        yield world


def _code(response: httpx.Response) -> tuple[int, str, str | None]:
    body = parse_rejection_response(response.content)
    return response.status_code, body.code.value, body.field


@contextlib.contextmanager
def _target(storage: CountingStorage, target: Any) -> Iterator[None]:
    previous = storage.target
    storage.target = target
    try:
        yield
    finally:
        storage.target = previous


# --- Criterio 2: la URL y el almacén -----------------------------------------------------------


def test_the_url_accepts_a_put_with_exactly_the_headers_of_the_map(clips: ClipWorld) -> None:
    node = clips.node()
    data = video("exacto")
    issued = clips.issue(node, data)
    headers = dict(issued.upload.headers)
    assert headers == {
        "content-type": "video/mp4",
        "x-amz-checksum-sha256": sha256_b64(data),
        "x-amz-meta-vigia-anonymized": "1",
    }
    assert clips.put(issued.upload.url, headers, data).status_code == 200
    head = clips.s3.head_object(
        Bucket=clips.bucket, Key=issued.grant.storage_key, ChecksumMode="ENABLED"
    )
    assert head["ChecksumSHA256"] == sha256_b64(data)
    assert head["Metadata"] == {"vigia-anonymized": "1"}
    assert head["ContentType"] == "video/mp4"


@pytest.mark.parametrize("change", ["no_checksum", "other_checksum", "other_bytes", "no_meta"])
def test_without_the_checksum_or_with_another_sum_the_store_rejects_the_put(
    clips: ClipWorld, change: str
) -> None:
    node = clips.node()
    data = video(f"rechazo-{change}")
    issued = clips.issue(node, data)
    headers = dict(issued.upload.headers)
    body = data
    if change == "no_checksum":
        del headers["x-amz-checksum-sha256"]
    elif change == "other_checksum":
        headers["x-amz-checksum-sha256"] = sha256_b64(b"otra suma")
    elif change == "other_bytes":
        body = video("otros bytes", size=len(data))
    else:
        del headers["x-amz-meta-vigia-anonymized"]
    response = clips.put(issued.upload.url, headers, body)
    assert response.status_code in (400, 403), response.text
    with pytest.raises(clips.s3.exceptions.ClientError):
        clips.s3.head_object(Bucket=clips.bucket, Key=issued.grant.storage_key)


def test_a_second_put_with_other_bytes_never_changes_the_object(clips: ClipWorld) -> None:
    node = clips.node()
    data = video("segundo-put")
    issued = clips.uploaded(node, data)
    other = video("segundo-put-otros", size=len(data))
    second = clips.put(issued.upload.url, issued.upload.headers, other)
    assert second.status_code == 400, second.text  # BadDigest: la suma firmada no es la suya
    versions = clips.s3.list_object_versions(Bucket=clips.bucket, Prefix=issued.grant.storage_key)[
        "Versions"
    ]
    assert len(versions) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Un segundo PUT de los mismos bytes dentro de la vigencia se rechaza solo con"
        " If-None-Match: *, y el contrato fijado (RequiredHeaders cerrado) no deja ponerlo en"
        " required_headers: queda para el cambio de contrato y la comprobación de despliegue"
        " nº 13 (VIG-174)."
    ),
)
def test_a_second_put_on_the_same_key_is_rejected(clips: ClipWorld) -> None:
    node = clips.node()
    data = video("segundo-put-iguales")
    issued = clips.uploaded(node, data)
    second = clips.put(issued.upload.url, issued.upload.headers, data)
    assert second.status_code == 412


def test_localstack_enforces_the_conditional_write_when_the_url_signs_it(
    clips: ClipWorld,
) -> None:
    # Lo que haría falta para el criterio: LocalStack sí rechaza el segundo PUT con la condición
    # firmada (base de la comprobación de despliegue nº 13).
    key = f"sonda/{uuid.uuid4()}.mp4"
    data = video("condicional")
    url = clips.s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": clips.bucket, "Key": key, "IfNoneMatch": "*"},
        ExpiresIn=60,
    )
    assert httpx.put(url, content=data, headers={"If-None-Match": "*"}).status_code == 200
    second = httpx.put(url, content=data, headers={"If-None-Match": "*"})
    assert second.status_code == 412, second.text


def test_the_key_names_organization_plant_zone_and_node_and_expires_in_15_minutes(
    clips: ClipWorld,
) -> None:
    node = clips.node()
    data = video("clave")
    body = clips.body(node, data)
    response = clips.post_grant(node, body)
    assert response.status_code == 200, response.text
    grant = parse_response_clip_upload_grant(response.content)
    db = node.db
    assert grant.storage_key == (
        f"org/{db.organization_id}/plant/{db.plant_id}/zone/{db.zone_id}/node/{db.node_id}/"
        f"{body['clip_id']}.mp4"
    )
    row = clips.grant_row(uuid.UUID(body["clip_id"]))
    assert row["expires_at"] - row["issued_at"] == FIFTEEN_MINUTES
    assert row["status"] == "issued" and row["purpose"] == "verification"
    assert grant.purpose.value == "verification"
    assert grant.max_size_bytes == len(data)
    stored = row["required_headers"]
    assert grant.required_headers.to_json_value() == (
        json.loads(stored) if isinstance(stored, str) else stored
    )
    assert len(grant.required_headers.to_json_value()) <= 8


def test_purpose_defaults_to_evidence_and_the_grant_returns_it(clips: ClipWorld) -> None:
    node = clips.node()
    body = clips.body(node, video("sin-proposito"), purpose=None)
    response = clips.post_grant(node, body)
    assert response.status_code == 200, response.text
    assert parse_response_clip_upload_grant(response.content).purpose.value == "evidence"
    assert clips.grant_row(uuid.UUID(body["clip_id"]))["purpose"] == "evidence"


# --- Repetición de la petición (PR-GOB-20) --------------------------------------------------------


def test_the_same_live_request_returns_a_new_url_with_the_same_conditions(
    clips: ClipWorld,
) -> None:
    node = clips.node()
    data = video("repetida")
    body = clips.body(node, data)
    first = parse_response_clip_upload_grant(clips.post_grant(node, body).content)
    clips.clock.advance(60)
    second_response = clips.post_grant(node, body)
    assert second_response.status_code == 200, second_response.text
    second = parse_response_clip_upload_grant(second_response.content)
    assert second.expires_at == first.expires_at
    assert second.storage_key == first.storage_key
    assert second.required_headers == first.required_headers
    rows = clips.fetch(
        "SELECT count(*) AS n FROM fleet.clip_upload_grant WHERE clip_id = $1",
        uuid.UUID(body["clip_id"]),
    )
    assert rows[0]["n"] == 1


@pytest.mark.parametrize("change", ["sha256", "purpose", "size"])
def test_the_same_clip_with_other_parameters_is_schema_invalid_on_clip_id(
    clips: ClipWorld, change: str
) -> None:
    node = clips.node()
    data = video(f"otros-{change}")
    body = clips.body(node, data)
    assert clips.post_grant(node, body).status_code == 200
    other = dict(body)
    if change == "sha256":
        other["sha256"] = hashlib.sha256(b"otro").hexdigest()
    elif change == "purpose":
        other["purpose"] = "evidence"
    else:
        other["size_bytes"] = len(data) + 1
    assert _code(clips.post_grant(node, other)) == (422, "schema_invalid", "clip_id")


def test_an_uploaded_clip_never_gets_another_url(clips: ClipWorld) -> None:
    node = clips.node()
    data = video("ya-subido")
    issued = clips.uploaded(node, data)
    body = clips.body(node, data, clip_id=issued.grant.clip_id)
    assert _code(clips.post_grant(node, body)) == (422, "schema_invalid", "clip_id")


def test_an_expired_grant_without_object_is_reissued_on_the_same_row(clips: ClipWorld) -> None:
    node = clips.node()
    data = video("vencida")
    body = clips.body(node, data)
    clip_id = uuid.UUID(body["clip_id"])
    assert clips.post_grant(node, body).status_code == 200
    before = clips.grant_row(clip_id)
    clips.clock.advance(FIFTEEN_MINUTES.total_seconds() + 5)
    response = clips.post_grant(node, body)
    assert response.status_code == 200, response.text
    after = clips.grant_row(clip_id)
    assert after["issued_at"] >= before["expires_at"]
    assert after["expires_at"] - after["issued_at"] == FIFTEEN_MINUTES
    assert after["status"] == "issued"
    grant = parse_response_clip_upload_grant(response.content)
    assert grant.expires_at.endswith("Z")


def test_the_database_refuses_to_renew_a_live_or_closed_grant(clips: ClipWorld) -> None:
    # reissue_guard (gob_0021): issued_at solo se mueve en una concesión issued y vencida.
    node = clips.node()
    issued = clips.issue(node, video("guarda"))
    context = clips.scope(node).context

    async def bump(status: str | None = None) -> None:
        async with clips.authz.sessions.database.transaction(context) as transaction:
            if status is not None:
                await transaction.execute(
                    text(
                        "UPDATE fleet.clip_upload_grant SET status = :status, used_at = :now"
                        " WHERE clip_id = :clip_id"
                    ),
                    {"status": status, "now": clips.now(), "clip_id": issued.grant.clip_id},
                )
            await transaction.execute(
                text(
                    "UPDATE fleet.clip_upload_grant SET issued_at = expires_at,"
                    " expires_at = expires_at + interval '15 minutes' WHERE clip_id = :clip_id"
                ),
                {"clip_id": issued.grant.clip_id},
            )

    async def backwards() -> None:
        async with clips.authz.sessions.database.transaction(context) as transaction:
            await transaction.execute(
                text(
                    "UPDATE fleet.clip_upload_grant SET issued_at = issued_at"
                    " - interval '1 minute', expires_at = expires_at - interval '1 minute'"
                    " WHERE clip_id = :clip_id"
                ),
                {"clip_id": issued.grant.clip_id},
            )

    with pytest.raises(Exception, match="solo se reemite"):
        clips.run(backwards())
    with pytest.raises(Exception, match="solo se reemite"):
        clips.run(bump("used"))
    assert clips.grant_row(issued.grant.clip_id)["status"] == "issued"


# --- Criterio 3: confirmación por metadatos -------------------------------------------------------


def test_the_receipt_is_the_verification_clip_created_and_repeating_returns_it(
    clips: ClipWorld,
) -> None:
    node = clips.node()
    data = video("confirmado")
    issued = clips.uploaded(node, data)
    clip_id = issued.grant.clip_id
    before = clips.storage.calls["get_object"]
    first = clips.post_confirmation(node, clip_id)
    assert first.status_code == 200, first.text
    receipt = parse_response_verification_clip_receipt(first.content)
    (row,) = clips.clip_rows(clip_id)
    assert receipt.clip_id == str(row["clip_id"]) == str(clip_id)
    assert receipt.zone_id == str(row["zone_id"]) == str(node.db.zone_id)
    assert receipt.node_id == str(row["node_id"]) == str(node.db.node_id)
    assert receipt.sha256 == row["sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt.received_at == row["received_at"].isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    assert row["blur_check_result"] is None and row["first_served_at"] is None
    grant = clips.grant_row(clip_id)
    assert grant["status"] == "used" and grant["used_at"] == row["received_at"]
    clips.clock.advance(30)
    again = clips.post_confirmation(node, clip_id)
    assert again.status_code == 200 and again.content == first.content
    assert len(clips.clip_rows(clip_id)) == 1
    assert clips.storage.calls["get_object"] == before == 0


@pytest.mark.parametrize(
    "case", ["clip_missing", "clip_hash_mismatch", "clip_too_large", "clip_not_anonymized"]
)
def test_the_confirmation_tells_each_cause_apart_with_head_object_only(
    clips: ClipWorld, case: str
) -> None:
    node = clips.node()
    data = video(f"causa-{case}")
    issued = clips.issue(node, data)
    key = issued.grant.storage_key
    if case == "clip_hash_mismatch":
        clips.put_directly(key, video("otro contenido", size=len(data)))
    elif case == "clip_too_large":
        clips.put_directly(key, video("mas grande", size=len(data) + 1))
    elif case == "clip_not_anonymized":
        clips.put_directly(key, data, metadata={"vigia-anonymized": "0"})
    heads = clips.storage.calls["head_object"]
    response = clips.post_confirmation(node, issued.grant.clip_id)
    assert _code(response)[:2] == (422, case)
    assert clips.storage.calls["head_object"] == heads + 1
    assert clips.storage.calls["get_object"] == 0
    assert clips.clip_rows(issued.grant.clip_id) == []
    assert clips.grant_row(issued.grant.clip_id)["status"] == "issued"


def test_confirming_an_evidence_grant_is_schema_invalid_422_on_purpose(clips: ClipWorld) -> None:
    node = clips.node()
    data = video("evidencia")
    issued = clips.uploaded(node, data, purpose="evidence")
    assert _code(clips.post_confirmation(node, issued.grant.clip_id)) == (
        422,
        "schema_invalid",
        "purpose",
    )
    assert clips.clip_rows(issued.grant.clip_id) == []


# --- Criterio 4: cinco confirmaciones simultáneas ------------------------------------------------


class BarrierStorage:
    """Retiene cada ``head_object`` hasta que llegan ``parties``: todas las confirmaciones pasan
    la lectura previa y la verificación antes de que ninguna abra su transacción."""

    def __init__(self, target: Any, parties: int) -> None:
        self.target = target
        self.barrier = asyncio.Barrier(parties)

    async def head_object(self, key: str) -> Any:
        head = await self.target.head_object(key)
        async with asyncio.timeout(LONG_TIMEOUT_SECONDS):
            await self.barrier.wait()
        return head

    async def presign_put(self, *arguments: Any) -> Any:  # pragma: no cover - no se usa
        return await self.target.presign_put(*arguments)


@pytest.mark.parametrize("run", range(3))
def test_five_simultaneous_confirmations_create_one_clip_and_the_same_receipt(
    clips: ClipWorld, run: int
) -> None:
    node = clips.node()
    data = video(f"cinco-{run}")
    issued = clips.uploaded(node, data)
    service = ClipConfirmationService(
        database=clips.authz.sessions.database,
        store=ClipObjectStore(
            BarrierStorage(clips.storage, 5),
            head_timeout_seconds=LONG_TIMEOUT_SECONDS,
            presign_timeout_seconds=LONG_TIMEOUT_SECONDS,
        ),
        clock=clips.clock,
        metrics=clips.metrics,
    )
    scope = clips.scope(node)

    async def five() -> list[Any]:
        return list(
            await asyncio.gather(
                *(service.confirm(scope, issued.grant.clip_id) for _ in range(5)),
                return_exceptions=True,
            )
        )

    results = clips.run(five())
    errors = [result for result in results if isinstance(result, BaseException)]
    assert errors == []
    assert len(set(results)) == 1
    assert len(clips.clip_rows(issued.grant.clip_id)) == 1


# --- Criterio 5: guardas de alcance -------------------------------------------------------------


def test_a_grant_for_a_zone_not_assigned_to_the_node_is_node_zone_mismatch(
    clips: ClipWorld,
) -> None:
    organization = clips.organization()
    node = clips.node(organization)
    other = clips.node(organization)  # otra zona de la misma planta, asignada a otro nodo
    body = clips.body(node, video("zona-ajena"), zone_id=other.db.zone_id)
    assert _code(clips.post_grant(node, body)) == (403, "node_zone_mismatch", "zone_id")
    assert clips.grant_row(uuid.UUID(body["clip_id"])) is None


def test_a_grant_for_a_zone_of_another_organization_is_node_zone_mismatch(
    clips: ClipWorld,
) -> None:
    node = clips.node()
    foreign = clips.node()
    body = clips.body(node, video("otra-org"), zone_id=foreign.db.zone_id)
    assert _code(clips.post_grant(node, body)) == (403, "node_zone_mismatch", "zone_id")
    assert clips.grant_row(uuid.UUID(body["clip_id"])) is None


def test_the_zone_scope_wins_over_the_schema(clips: ClipWorld) -> None:
    node = clips.node()
    foreign = clips.node()
    body = clips.body(node, video("antes"), zone_id=foreign.db.zone_id, size_bytes="mil")
    assert _code(clips.post_grant(node, body)) == (403, "node_zone_mismatch", "zone_id")
    own = clips.body(node, video("antes"), size_bytes="mil")
    assert _code(clips.post_grant(node, own))[:2] == (422, "schema_invalid")


def test_the_service_checks_the_zone_even_without_the_route(clips: ClipWorld) -> None:
    # Segunda guarda (la de ``before_schema`` no corre aquí): el servicio no confía en la ruta.
    node = clips.node()
    foreign = clips.node()
    request = clips.request(clips.body(node, video("servicio"), zone_id=foreign.db.zone_id))
    with pytest.raises(ZoneNotAssigned):
        clips.run(clips.grants.issue(clips.scope(node), request))


def test_confirming_the_clip_of_another_node_is_node_zone_mismatch(clips: ClipWorld) -> None:
    organization = clips.organization()
    owner = clips.node(organization)
    neighbour = clips.node(organization)
    foreign = clips.node()
    issued = clips.uploaded(owner, video("de-otro"))
    clip_id = issued.grant.clip_id
    same_org = clips.post_confirmation(neighbour, clip_id)
    other_org = clips.post_confirmation(foreign, clip_id)
    missing = clips.post_confirmation(neighbour, uuid7())
    assert (
        _code(same_org)
        == _code(other_org)
        == _code(missing)
        == (
            403,
            "node_zone_mismatch",
            None,
        )
    )
    assert same_org.content == missing.content
    assert clips.clip_rows(clip_id) == []
    assert clips.storage.calls["get_object"] == 0


def test_a_confirmed_clip_is_never_served_to_another_node(clips: ClipWorld) -> None:
    # Guarda del servicio (``grant.node_id``): sin ella, el recibo ya creado saldría hacia otro
    # nodo de la organización sin pasar por el candado.
    organization = clips.organization()
    owner = clips.node(organization)
    neighbour = clips.node(organization)
    issued = clips.uploaded(owner, video("ya-confirmado"))
    assert clips.post_confirmation(owner, issued.grant.clip_id).status_code == 200
    heads = clips.storage.calls["head_object"]
    assert _code(clips.post_confirmation(neighbour, issued.grant.clip_id)) == (
        403,
        "node_zone_mismatch",
        None,
    )
    assert clips.storage.calls["head_object"] == heads  # ni siquiera consulta el almacén


def test_the_confirmation_lock_only_finds_the_grant_of_that_node(clips: ClipWorld) -> None:
    # Filtro de nodo del candado (la RLS solo filtra por organización).
    organization = clips.organization()
    owner = clips.node(organization)
    neighbour = clips.node(organization)
    issued = clips.issue(owner, video("candado"))
    repository = PostgresClipGrants(clips.authz.sessions.database)
    context = clips.scope(owner).context

    async def lock(node_id: uuid.UUID) -> Any:
        async with clips.authz.sessions.database.transaction(context) as transaction:
            return await repository.lock_for_confirmation(
                transaction, node_id, issued.grant.clip_id
            )

    assert clips.run(lock(neighbour.db.node_id)) is None
    assert clips.run(lock(owner.db.node_id)) is not None


def test_commissioning_clips_of_another_organization_are_not_found(clips: ClipWorld) -> None:
    node = clips.node()
    clips.post_confirmation(node, clips.uploaded(node, video("listado-b")).grant.clip_id)
    stranger = clips.member(clips.organization()[0])
    with pytest.raises(ResourceNotFound):
        clips.run(clips.listing.page(stranger, node.db.zone_id))
    with pytest.raises(ResourceNotFound):
        clips.run(clips.listing.page(stranger, uuid.uuid4()))


def test_commissioning_clips_only_list_the_zone_asked_for(clips: ClipWorld) -> None:
    organization = clips.organization()
    first = clips.node(organization)
    second = clips.node(organization)
    mine = clips.uploaded(first, video("zona-1")).grant.clip_id
    theirs = clips.uploaded(second, video("zona-2")).grant.clip_id
    assert clips.post_confirmation(first, mine).status_code == 200
    assert clips.post_confirmation(second, theirs).status_code == 200
    reader = clips.member(organization[0])
    page = clips.run(clips.listing.page(reader, first.db.zone_id))
    assert [clip.clip_id for clip in page.clips] == [mine]


# --- Listado: cursor, first_served_at y auditoría del proveedor --------------------------------


def test_the_listing_pages_newest_first_and_marks_first_served_once(clips: ClipWorld) -> None:
    node = clips.node()
    confirmed = []
    for index in range(3):
        issued = clips.uploaded(node, video(f"pagina-{index}"))
        clips.clock.advance(1)
        assert clips.post_confirmation(node, issued.grant.clip_id).status_code == 200
        confirmed.append(issued.grant.clip_id)
    reader = clips.member(node.db.organization_id)
    clips.clock.advance(5)
    first = clips.run(clips.listing.page(reader, node.db.zone_id, limit=2))
    assert [clip.clip_id for clip in first.clips] == confirmed[::-1][:2]
    assert first.next_after == confirmed[1]
    served_at = clips.now().replace(microsecond=clips.now().microsecond // 1000 * 1000)
    assert all(clip.first_served_at == served_at for clip in first.clips)
    clips.clock.advance(5)
    rest = clips.run(clips.listing.page(reader, node.db.zone_id, after=first.next_after))
    assert [clip.clip_id for clip in rest.clips] == [confirmed[0]] and rest.next_after is None
    again = clips.run(clips.listing.page(reader, node.db.zone_id))
    rows = {
        uuid.UUID(str(row["clip_id"])): row["first_served_at"]
        for row in clips.fetch(
            "SELECT clip_id, first_served_at FROM fleet.verification_clip WHERE zone_id = $1",
            node.db.zone_id,
        )
    }
    assert rows[confirmed[2]] == rows[confirmed[1]] == served_at
    assert rows[confirmed[0]] == served_at + timedelta(seconds=5)
    assert {clip.clip_id: clip.first_served_at for clip in again.clips} == rows
    with pytest.raises(CommissioningClipsRequestInvalid):
        clips.run(clips.listing.page(reader, node.db.zone_id, after=uuid7()))


class ReadTogether(PostgresClipGrants):
    """Las dos lecturas leen la página (con ``first_served_at`` nulo) antes de que ninguna
    escriba la marca."""

    def __init__(self, database: Any, barrier: asyncio.Barrier) -> None:
        super().__init__(database)
        self.barrier = barrier

    async def zone_clips(self, transaction: Any, zone_id: uuid.UUID, **kwargs: Any) -> Any:
        found = await super().zone_clips(transaction, zone_id, **kwargs)
        async with asyncio.timeout(LONG_TIMEOUT_SECONDS):
            await self.barrier.wait()
        return found


@pytest.mark.parametrize("run", range(3))
def test_two_simultaneous_listings_mark_first_served_once(clips: ClipWorld, run: int) -> None:
    # Cada lectura con su reloj (un segundo de diferencia): sin la condición «aún nulo», la
    # segunda reescribiría la marca de la primera (la base lo rechaza: cierre único).
    node = clips.node()
    issued = clips.uploaded(node, video(f"dos-lecturas-{run}"))
    assert clips.post_confirmation(node, issued.grant.clip_id).status_code == 200
    reader = clips.member(node.db.organization_id)
    barrier = asyncio.Barrier(2)
    database = clips.authz.sessions.database
    late = SimulatedClock(clips.now() + timedelta(seconds=1))

    def listing(clock: Any) -> CommissioningClips:
        return CommissioningClips(
            database=database,
            authorizer=clips.authz.authorizer,
            audit=clips.authz.sessions.audit,
            clock=clock,
            grants=ReadTogether(database, barrier),
        )

    async def two() -> list[Any]:
        return list(
            await asyncio.gather(
                listing(clips.clock).page(reader, node.db.zone_id),
                listing(late).page(reader, node.db.zone_id),
                return_exceptions=True,
            )
        )

    results = clips.run(two())
    assert [r for r in results if isinstance(r, BaseException)] == []
    (row,) = clips.clip_rows(issued.grant.clip_id)
    served = {clip.first_served_at for page in results for clip in page.clips}
    # Una sola marca escrita (la de quien escribió primero); la otra lectura no la cambió.
    assert row["first_served_at"] in served - {None}


def test_a_provider_read_under_concession_is_audited(clips: ClipWorld) -> None:
    node = clips.node()
    issued = clips.uploaded(node, video("proveedor"))
    assert clips.post_confirmation(node, issued.grant.clip_id).status_code == 200
    installer = clips.installer(node.db.organization_id)
    before = clips.fetch(
        "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'catalog_read' AND scope_zone_id = $2",
        node.db.organization_id,
        node.db.zone_id,
    )[0]["n"]
    page = clips.run(clips.listing.page(installer, node.db.zone_id))
    assert [clip.clip_id for clip in page.clips] == [issued.grant.clip_id]
    after = clips.fetch(
        "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'catalog_read' AND scope_zone_id = $2",
        node.db.organization_id,
        node.db.zone_id,
    )[0]["n"]
    assert after == before + 1


# --- Criterio 6: almacén sin respuesta ----------------------------------------------------------


def test_a_hung_store_answers_storage_unavailable_within_5_seconds_and_writes_nothing(
    clips: ClipWorld,
) -> None:
    # Mide el tope de producción (5 s, NFR-GOB-43): margen de segundos, nunca de milisegundos.
    node = clips.node()
    request = clips.request(clips.body(node, video("colgado")))
    scope = clips.scope(node)
    with _target(clips.storage, Hung()):
        started = WALL_CLOCK.monotonic()
        with pytest.raises(StorageUnavailable):
            clips.run(clips.fragile_grants.issue(scope, request))
        elapsed = WALL_CLOCK.monotonic() - started
    assert elapsed <= 5.0 + 1.5, elapsed
    assert clips.grant_row(uuid.UUID(str(request.clip_id))) is None
    # Restablecido el almacén, el reintento funciona.
    assert clips.run(clips.fragile_grants.issue(scope, request)).grant.status.value == "issued"


def test_a_hung_store_on_the_route_is_storage_unavailable_and_retryable(
    clips: ClipWorld,
) -> None:
    node = clips.node()
    body = clips.body(node, video("colgado-ruta"))
    fragile = ClipGrantService(
        database=clips.authz.sessions.database,
        store=ClipObjectStore(HttpsUrls(Hung())),
        clock=clips.clock,
        metrics=clips.metrics,
    )
    app = node_app(
        World(clock=clips.clock),
        node_gate(
            contexts=clips.authz.contexts,
            store=PostgresNodeContextStore(clips.authz.sessions.database),
            clock=clips.clock,
            probe=Probe(),
            operations={NodeRoute.CLIP_UPLOAD: clip_upload_operation(fragile)},
        ),
        routes=(NodeRoute.CLIP_UPLOAD,),
    )

    async def post() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://nodes.vigia.test", timeout=30
        ) as client:
            return await client.post(NodeRoute.CLIP_UPLOAD.path, json=body, headers=node.headers)

    response = clips.run(post())
    rejection = parse_rejection_response(response.content)
    assert response.status_code == 503 and rejection.code.value == "storage_unavailable"
    assert rejection.retryable is True and rejection.retry_after_seconds is not None
    assert clips.grant_row(uuid.UUID(body["clip_id"])) is None


# --- Criterio 7: ninguna URL prefirmada fuera de la respuesta ---------------------------------


def test_no_presigned_url_reaches_logs_metrics_traces_or_events(
    clips: ClipWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = clips.node()
    with TelemetryCapture() as capture:
        monkeypatch.setattr(otel_trace, "get_tracer", lambda *_, **__: capture.tracer)
        metrics = capture.telemetry.metrics
        service = ClipGrantService(
            database=clips.authz.sessions.database,
            store=ClipObjectStore(HttpsUrls(clips.storage)),
            clock=clips.clock,
            metrics=metrics,
        )
        app = node_app(
            World(clock=clips.clock),
            node_gate(
                contexts=clips.authz.contexts,
                store=PostgresNodeContextStore(clips.authz.sessions.database),
                clock=clips.clock,
                probe=Probe(),
                operations={NodeRoute.CLIP_UPLOAD: clip_upload_operation(service)},
                responses=NodeResponses(clips.clock, metrics=metrics),
            ),
            routes=(NodeRoute.CLIP_UPLOAD,),
        )

        async def post() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://nodes.vigia.test",
                timeout=30,
            ) as client:
                return await client.post(
                    NodeRoute.CLIP_UPLOAD.path,
                    json=clips.body(node, video("telemetria")),
                    headers=node.headers,
                )

        response = clips.run(post())
        assert response.status_code == 200, response.text
        url = parse_response_clip_upload_grant(response.content).upload_url
    query = url.split("?", 1)[1]
    signature = next(part for part in query.split("&") if part.startswith("X-Amz-Signature="))
    assert capture.leaks([url, query, signature, url.split("?", 1)[0] + "?"]) == []
    emitted = "\n".join(capture.emitted())
    assert str(node.db.node_id) in emitted  # el arnés sí ve lo legítimo
    assert capture.published == []


def test_per_node_metrics_are_counters_labelled_only_by_node(clips: ClipWorld) -> None:
    node = clips.node()
    issued = clips.uploaded(node, video("contadores"))
    assert clips.post_confirmation(node, issued.grant.clip_id).status_code == 200
    for name in (MetricName.CLIP_GRANTS_ISSUED_TOTAL, MetricName.CLIP_GRANTS_USED_TOTAL):
        points = metric_points(clips.reader, name)
        assert points and all(set(attributes) == {"node_id"} for attributes, _ in points)
        assert any(attributes["node_id"] == str(node.db.node_id) for attributes, _ in points)
