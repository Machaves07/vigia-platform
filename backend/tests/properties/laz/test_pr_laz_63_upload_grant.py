"""PR-LAZ-63 y SUP-U05-09: forma de la concesión de subida de un adjunto de cierre.

- Toda concesión generada: clave del patrón ``closure/`` de la organización, planta, zona y
  sujeto de la acción (nunca ``node/`` ni ``documents/``), vigencia de 15 minutos del ``Clock``
  inyectado y **un solo** ``PUT`` firmado por ``S3Storage`` con ``content-type`` y
  ``x-amz-checksum-sha256`` entre las cabeceras firmadas (la firma no toca la red).
- Tipo y tamaño con su ``detail_code``; esquema sin código.
- Autorización por zona: una persona con ``actions.manage`` en otra zona de la misma planta no
  obtiene nada (``ResourceNotFound``, denegación auditada).
- Límite de 60 por hora y persona: 21 concesiones simultáneas sobre un límite de 20 en la
  fixture dan exactamente 20.
- Almacén caído: ``StorageUnavailable`` declarado; el circuito queda abierto y la siguiente
  concesión no toca el almacén hasta la sonda de los 60 s.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.attachment_support import AttachmentWorld, MemoryStore, attachment_world, sha256_hex, utc
from tests.authz_support import sealed_context
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.loop.adapters.attachments.grants import (
    ATTACHMENT_GRANT_TTL,
    AttachmentUploadGrant,
    ClosureUploadSigner,
)
from vigia_platform.loop.adapters.attachments.repository import ActionPlace
from vigia_platform.loop.adapters.attachments.rules import (
    CLOSURE_KEY_PATTERN,
    MAX_ATTACHMENT_BYTES,
    AttachmentRejected,
    AttachmentSchemaInvalid,
)
from vigia_platform.loop.adapters.attachments.store import StoreCircuit
from vigia_platform.loop.adapters.http.attachment_routes import rejection_error, unavailable_error
from vigia_platform.loop.application.attachments import AttachmentRateLimited, IssuedAttachmentGrant
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import AllowedScope, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.outbox.breaker import CircuitState
from vigia_platform.shared.ratelimit import Budget
from vigia_platform.shared.storage import (
    ObjectHead,
    PresignedRequest,
    S3Storage,
    StorageCredentials,
    StorageSettings,
    StorageUnavailable,
)

_EXT = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}


@given(
    st.uuids(version=4),
    st.uuids(version=4),
    st.uuids(version=4),
    st.uuids(version=4),
    st.sampled_from(sorted(_EXT)),
    st.integers(1, MAX_ATTACHMENT_BYTES),
    st.binary(min_size=1, max_size=32),
    st.datetimes(
        min_value=dt.datetime(2026, 1, 1),  # noqa: DTZ001 - Hypothesis exige límites ingenuos
        max_value=dt.datetime(2040, 1, 1),  # noqa: DTZ001
        timezones=st.just(dt.UTC),
    ),
)
def test_pr_laz_63_every_grant_is_a_closure_key_of_its_place_for_fifteen_minutes(
    organization: uuid.UUID,
    plant: uuid.UUID,
    zone: uuid.UUID,
    subject: uuid.UUID,
    content_type: str,
    size: int,
    payload: bytes,
    now: dt.datetime,
) -> None:
    clock = SimulatedClock(now)
    grant = AttachmentUploadGrant.issue(
        organization_id=organization,
        plant_id=plant,
        zone_id=zone,
        subject_id=subject,
        content_type=content_type,
        size_bytes=size,
        sha256=sha256_hex(payload),
        clock=clock,
    )
    match = CLOSURE_KEY_PATTERN.fullmatch(grant.storage_key)
    assert match is not None
    assert match["organization_id"] == str(organization)
    assert (match["plant_id"], match["zone_id"]) == (str(plant), str(zone))
    assert match["subject_id"] == str(subject)
    assert match["attachment_id"] == str(grant.attachment_id)
    assert grant.storage_key.endswith("." + _EXT[content_type])
    assert "/node/" not in grant.storage_key and "/documents/" not in grant.storage_key
    assert grant.attachment_id.version == 7
    assert grant.expires_at - now == ATTACHMENT_GRANT_TTL == timedelta(minutes=15)


class _SigningOnly:
    """``S3Storage`` real para firmar; ``HEAD`` sin objeto (la firma no toca la red)."""

    def __init__(self, clock: SimulatedClock) -> None:
        self.s3 = S3Storage(
            StorageSettings(
                bucket="vigia-evidence-prueba",
                credentials=StorageCredentials("AKIASINTETICA", "secreto-sintetico"),
            ),
            clock,
        )

    async def head_object(self, key: str) -> ObjectHead | None:
        return None

    async def presign_put(self, *args: object, **kwargs: object) -> PresignedRequest:
        return await self.s3.presign_put(*args, **kwargs)  # type: ignore[arg-type]


@given(st.sampled_from(sorted(_EXT)), st.binary(min_size=1, max_size=64), st.integers(0, 899))
def test_pr_laz_63_the_signed_put_carries_the_checksum_among_signed_headers(
    content_type: str, payload: bytes, elapsed: int
) -> None:
    clock = SimulatedClock(utc())
    place = [uuid.uuid4() for _ in range(4)]
    grant = AttachmentUploadGrant.issue(
        organization_id=place[0],
        plant_id=place[1],
        zone_id=place[2],
        subject_id=place[3],
        content_type=content_type,
        size_bytes=len(payload),
        sha256=sha256_hex(payload),
        clock=clock,
    )
    clock.advance(elapsed)
    storage = _SigningOnly(clock)
    signer = ClosureUploadSigner(storage, StoreCircuit(clock), clock)  # type: ignore[arg-type]
    signed = asyncio.run(signer.sign(grant))
    assert signed.method == "PUT"
    query = parse_qs(urlsplit(signed.url).query)
    signed_headers = query["X-Amz-SignedHeaders"][0].split(";")
    assert "x-amz-checksum-sha256" in signed_headers
    assert "content-type" in signed_headers
    assert int(query["X-Amz-Expires"][0]) == 900 - elapsed
    assert signed.expires_at == grant.expires_at
    assert (
        signed.headers["x-amz-checksum-sha256"]
        == base64.b64encode(bytes.fromhex(grant.sha256)).decode()
    )
    assert urlsplit(signed.url).path.endswith(grant.storage_key)
    assert "uploadId" not in query and "partNumber" not in query  # nunca por partes


def test_a_grant_past_its_validity_is_not_signed() -> None:
    clock = SimulatedClock(utc())
    grant = AttachmentUploadGrant.issue(
        organization_id=uuid.uuid4(),
        plant_id=uuid.uuid4(),
        zone_id=uuid.uuid4(),
        subject_id=uuid.uuid4(),
        content_type="image/png",
        size_bytes=1,
        sha256="a" * 64,
        clock=clock,
    )
    clock.advance(ATTACHMENT_GRANT_TTL.total_seconds())
    signer = ClosureUploadSigner(MemoryStore(clock), StoreCircuit(clock), clock)
    with pytest.raises(AttachmentSchemaInvalid):
        asyncio.run(signer.sign(grant))


# --- El servicio ----------------------------------------------------------------------------------


def _context(organization: uuid.UUID, level: ScopeLevel, scope: uuid.UUID) -> ScopeContext:
    return sealed_context(organization, [AllowedScope(level, scope, Role.COORDINATOR_SST)])


def _world(budget: Budget | None = None) -> tuple[AttachmentWorld, ActionPlace, uuid.UUID]:
    clock = SimulatedClock(utc())
    world = attachment_world(clock) if budget is None else attachment_world(clock, budget=budget)
    place = ActionPlace(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    action = uuid.uuid4()
    world.repository.actions[action] = place
    return world, place, action


def _grant(
    world: AttachmentWorld, context: ScopeContext, action: uuid.UUID, **body: object
) -> IssuedAttachmentGrant:
    fields = {"content_type": "image/png", "size_bytes": 10, "sha256": "a" * 64, **body}
    return asyncio.run(world.service.grant_upload(context, action, **fields))


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"content_type": "image/gif"}, LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED),
        ({"content_type": "IMAGE/PNG"}, LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED),
        ({"content_type": "image/png; x=1"}, LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED),
        ({"size_bytes": MAX_ATTACHMENT_BYTES + 1}, LoopDetailCode.ATTACHMENT_TOO_LARGE),
    ],
)
def test_type_and_size_are_rejected_with_their_detail_code(
    body: dict[str, object], detail: LoopDetailCode
) -> None:
    world, place, action = _world()
    with pytest.raises(AttachmentRejected) as raised:
        _grant(
            world, _context(place.organization_id, ScopeLevel.ZONE, place.zone_id), action, **body
        )
    assert raised.value.detail is detail
    assert world.store.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"size_bytes": 0},
        {"size_bytes": True},
        {"sha256": "A" * 64},
        {"sha256": "a" * 65},
        {"content_type": ""},
    ],
)
def test_a_body_outside_the_schema_is_rejected_without_a_business_code(
    body: dict[str, object],
) -> None:
    world, place, action = _world()
    with pytest.raises(AttachmentSchemaInvalid):
        _grant(
            world, _context(place.organization_id, ScopeLevel.ZONE, place.zone_id), action, **body
        )


def test_ten_megabytes_exactly_are_granted() -> None:
    world, place, action = _world()
    issued = _grant(
        world,
        _context(place.organization_id, ScopeLevel.ZONE, place.zone_id),
        action,
        size_bytes=MAX_ATTACHMENT_BYTES,
    )
    assert issued.grant.size_bytes == MAX_ATTACHMENT_BYTES


@pytest.mark.parametrize("level", [ScopeLevel.ORGANIZATION, ScopeLevel.PLANT, ScopeLevel.ZONE])
def test_the_grant_is_scoped_to_the_actions_zone(level: ScopeLevel) -> None:
    world, place, action = _world()
    scope = {
        ScopeLevel.ORGANIZATION: place.organization_id,
        ScopeLevel.PLANT: place.plant_id,
        ScopeLevel.ZONE: place.zone_id,
    }[level]
    issued = _grant(world, _context(place.organization_id, level, scope), action)
    key = issued.grant.storage_key
    assert key.startswith(
        f"org/{place.organization_id}/plant/{place.plant_id}/zone/{place.zone_id}"
        f"/closure/{place.subject_id}/"
    )


@pytest.mark.parametrize("level", [ScopeLevel.PLANT, ScopeLevel.ZONE])
def test_another_zone_or_plant_of_the_same_organization_gets_nothing(level: ScopeLevel) -> None:
    world, place, action = _world()
    with pytest.raises(ResourceNotFound):
        _grant(world, _context(place.organization_id, level, uuid.uuid4()), action)
    assert len(world.denied.denied) == 1
    assert world.store.calls == []


def test_another_organization_gets_nothing() -> None:
    world, _place, action = _world()
    other = uuid.uuid4()
    with pytest.raises(ResourceNotFound):
        _grant(world, _context(other, ScopeLevel.ORGANIZATION, other), action)


def test_a_role_without_actions_manage_gets_nothing() -> None:
    world, place, action = _world()
    context = sealed_context(
        place.organization_id, [AllowedScope(ScopeLevel.ZONE, place.zone_id, Role.COPASST)]
    )
    with pytest.raises(ResourceNotFound):
        _grant(world, context, action)


def test_concurrent_grants_never_exceed_the_hourly_budget() -> None:
    """21 concesiones a la vez sobre un límite de 20: exactamente 20, otra persona no se ve."""
    world, place, action = _world(Budget(20, window_seconds=60 * 60))
    world.store.pause = True
    world.repository.pause = True
    context = _context(place.organization_id, ScopeLevel.ZONE, place.zone_id)

    async def one() -> bool:
        try:
            await world.service.grant_upload(
                context, action, content_type="image/png", size_bytes=10, sha256="a" * 64
            )
        except AttachmentRateLimited:
            return False
        return True

    async def burst() -> list[bool]:
        return list(await asyncio.gather(*(one() for _ in range(21))))

    accepted = asyncio.run(burst())
    assert accepted.count(True) == 20
    assert accepted.count(False) == 1
    other = _context(place.organization_id, ScopeLevel.ZONE, place.zone_id)
    _grant(world, other, action)  # el cubo es por persona


def test_the_budget_refills_after_an_hour_of_the_injected_clock() -> None:
    world, place, action = _world(Budget(2, window_seconds=60 * 60))
    context = _context(place.organization_id, ScopeLevel.ZONE, place.zone_id)
    _grant(world, context, action)
    _grant(world, context, action)
    with pytest.raises(AttachmentRateLimited):
        _grant(world, context, action)
    world.clock.advance(30 * 60)
    _grant(world, context, action)


def test_a_stopped_store_is_declared_and_the_circuit_spares_it_until_the_probe() -> None:
    world, place, action = _world()
    context = _context(place.organization_id, ScopeLevel.ZONE, place.zone_id)
    world.store.down = True
    with pytest.raises(StorageUnavailable):
        _grant(world, context, action)
    calls = len(world.store.calls)
    with pytest.raises(StorageUnavailable):
        _grant(world, context, action)
    assert len(world.store.calls) == calls  # sin tocar el almacén
    world.store.down = False
    world.clock.advance(59)
    with pytest.raises(StorageUnavailable):
        _grant(world, context, action)
    world.clock.advance(1)
    issued = _grant(world, context, action)  # la sonda pasa y cierra
    assert issued.upload.method == "PUT"
    assert world.service.signer.circuit.state is CircuitState.CLOSED


@pytest.mark.parametrize(
    "detail", [LoopDetailCode.ATTACHMENT_TYPE_NOT_ALLOWED, LoopDetailCode.ATTACHMENT_TOO_LARGE]
)
def test_business_rejections_travel_as_invalid_request_with_their_detail_code(
    detail: LoopDetailCode,
) -> None:
    error = rejection_error(AttachmentRejected(detail))
    assert (error.code, error.status_code, error.detail_code) == (
        ApiErrorCode.INVALID_REQUEST,
        400,
        detail.value,
    )


def test_an_unavailable_store_travels_as_storage_unavailable_with_retry() -> None:
    error = unavailable_error(StorageUnavailable("head_object"))
    assert (error.code, error.status_code) == (ApiErrorCode.STORAGE_UNAVAILABLE, 503)
    assert error.retry_after_seconds == 5
