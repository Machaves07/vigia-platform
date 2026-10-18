"""PR-LAZ-60 y SUP-U05-10: cada emisión de una URL de lectura de adjunto deja **exactamente una**
entrada de auditoría, declarada como concesión de acceso; ninguna operación lista ``closure/``.

- Propiedad (secuencias generadas de lecturas: en alcance, de otra zona, inexistentes, de otra
  organización y con el objeto alterado): las entradas ``success`` son exactamente las URL
  emitidas, cada una con el identificador del adjunto, su planta y su zona, la vigencia de la URL
  y ``records = access_grant_not_download``; las lecturas que no emiten URL no dejan ``success``.
- Concurrencia: N lecturas simultáneas dan N URL y N entradas.
- Si la entrada no se escribe, la URL no sale.
- Sin listado: ni el puerto del almacén de U-04 ni ``S3Storage`` ofrecen ninguna operación de
  listado, y ninguna lectura llama a otra cosa que ``HEAD`` y la firma de esa clave.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.attachment_support import AttachmentWorld, attachment_world, pdf, sha256_hex, utc
from tests.authz_support import sealed_context
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.loop.adapters.attachments import grants, read, store, verify
from vigia_platform.loop.adapters.attachments.read import AttachmentUnreadable
from vigia_platform.loop.adapters.attachments.repository import StoredAttachment
from vigia_platform.loop.adapters.attachments.store import ClosureObjectStorage
from vigia_platform.loop.application import attachments as application
from vigia_platform.loop.application.attachments import ACCESS_GRANT_DECLARATION
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import AllowedScope, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import S3Storage

_OPERATION = AuditOperation.CLOSURE_ATTACHMENT_READ_GRANTED.value


def _stored(world: AttachmentWorld, organization: uuid.UUID) -> tuple[uuid.UUID, StoredAttachment]:
    attachment_id, plant, zone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    data = pdf(attachment_id.hex.encode())
    key = f"org/{organization}/plant/{plant}/zone/{zone}/closure/{uuid.uuid4()}/{attachment_id}.pdf"
    world.store.put(key, data, "application/pdf")
    stored = StoredAttachment(organization, plant, zone, key, sha256_hex(data), len(data))
    world.repository.attachments[attachment_id] = stored
    return attachment_id, stored


def _reader(
    organization: uuid.UUID, zone: uuid.UUID, role: Role = Role.PLANT_MANAGER
) -> ScopeContext:
    return sealed_context(organization, [AllowedScope(ScopeLevel.ZONE, zone, role)])


_KINDS = st.sampled_from(["in_scope", "other_zone", "missing", "other_org", "tampered", "copasst"])


@given(st.lists(_KINDS, min_size=1, max_size=12), st.integers(0, 3600))
def test_pr_laz_60_one_audit_entry_per_emitted_url(kinds: list[str], start: int) -> None:
    clock = SimulatedClock(utc())
    clock.advance(start)
    world = attachment_world(clock)
    organization = uuid.uuid4()
    emitted = []
    for kind in kinds:
        attachment_id, stored = _stored(world, organization)
        context = _reader(organization, stored.zone_id)
        if kind == "other_zone":
            context = _reader(organization, uuid.uuid4())
        elif kind == "copasst":
            context = _reader(organization, stored.zone_id, Role.COPASST)
        elif kind == "other_org":
            other = uuid.uuid4()
            context = _reader(other, stored.zone_id)
        elif kind == "missing":
            attachment_id = uuid.uuid4()
        elif kind == "tampered":
            world.store.put(stored.storage_key, b"%PDF-otro", "application/pdf")
        try:
            grant = asyncio.run(world.service.read_url(context, attachment_id))
        except (ResourceNotFound, AttachmentUnreadable):
            assert kind != "in_scope"
            continue
        assert kind == "in_scope"
        emitted.append((attachment_id, stored, grant))
        clock.advance(1)
    successes = world.audit.successes()
    assert len(successes) == len(emitted)
    for entry, (attachment_id, stored, grant) in zip(successes, emitted, strict=True):
        assert entry.operation == _OPERATION
        assert entry.resource_id == attachment_id
        assert (entry.plant_id, entry.zone_id) == (stored.plant_id, stored.zone_id)
        assert entry.filters == {
            "records": ACCESS_GRANT_DECLARATION,
            "expires_at": format_timestamp(grant.expires_at),
        }
        assert entry.result_count == 1
        assert (
            timedelta(0)
            < grant.expires_at - (utc() + timedelta(seconds=start))
            <= timedelta(minutes=15, seconds=len(kinds))
        )
    assert set(world.store.calls) <= {"head_object", "presign_attachment_get"}


def test_concurrent_reads_leave_one_entry_each() -> None:
    world = attachment_world(SimulatedClock(utc()))
    world.store.pause = True
    world.audit.pause = True
    world.repository.pause = True
    organization = uuid.uuid4()
    attachment_id, stored = _stored(world, organization)
    context = _reader(organization, stored.zone_id)

    async def burst() -> list[object]:
        return list(
            await asyncio.gather(
                *(world.service.read_url(context, attachment_id) for _ in range(10))
            )
        )

    grants_emitted = asyncio.run(burst())
    assert len(grants_emitted) == 10
    assert len(world.audit.successes()) == 10
    assert all(entry.resource_id == attachment_id for entry in world.audit.successes())


def test_the_url_is_pinned_to_the_verified_version_for_fifteen_minutes() -> None:
    world = attachment_world(SimulatedClock(utc()))
    organization = uuid.uuid4()
    attachment_id, stored = _stored(world, organization)
    grant = asyncio.run(
        world.service.read_url(_reader(organization, stored.zone_id), attachment_id)
    )
    assert grant.url.endswith("?v=v1")
    assert grant.expires_at == utc() + timedelta(minutes=15)


def test_without_its_audit_entry_the_url_never_leaves() -> None:
    world = attachment_world(SimulatedClock(utc()))
    organization = uuid.uuid4()
    attachment_id, stored = _stored(world, organization)

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("la auditoría no se escribió")

    world.audit.append = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        asyncio.run(world.service.read_url(_reader(organization, stored.zone_id), attachment_id))


@pytest.mark.parametrize("role", [Role.COORDINATOR_SST, Role.PLANT_MANAGER])
def test_the_two_reading_roles_read(role: Role) -> None:
    world = attachment_world(SimulatedClock(utc()))
    organization = uuid.uuid4()
    attachment_id, stored = _stored(world, organization)
    asyncio.run(world.service.read_url(_reader(organization, stored.zone_id, role), attachment_id))


@pytest.mark.parametrize("role", [Role.ADMINISTRATOR, Role.COPASST, Role.LINE_MANAGER])
def test_roles_without_closure_attachments_read_get_nothing(role: Role) -> None:
    world = attachment_world(SimulatedClock(utc()))
    organization = uuid.uuid4()
    attachment_id, stored = _stored(world, organization)
    with pytest.raises(ResourceNotFound):
        asyncio.run(
            world.service.read_url(_reader(organization, stored.zone_id, role), attachment_id)
        )
    assert world.audit.successes() == []
    assert world.store.calls == []


def test_another_plant_of_the_same_organization_gets_nothing() -> None:
    world = attachment_world(SimulatedClock(utc()))
    organization = uuid.uuid4()
    attachment_id, _ = _stored(world, organization)
    context = sealed_context(
        organization, [AllowedScope(ScopeLevel.PLANT, uuid.uuid4(), Role.PLANT_MANAGER)]
    )
    with pytest.raises(ResourceNotFound):
        asyncio.run(world.service.read_url(context, attachment_id))


_LISTING = ("list", "objects_v2", "paginat", "keys")


def test_no_operation_lists_the_closure_prefix() -> None:
    port = {
        name for name, _ in inspect.getmembers(ClosureObjectStorage) if not name.startswith("_")
    }
    assert port == {
        "head_object",
        "get_object_prefix",
        "get_object_suffix",
        "presign_put",
        "presign_attachment_get",
    }
    public = [name for name in dir(S3Storage) if not name.startswith("_")]
    assert not [name for name in public if any(word in name for word in _LISTING)]
    for module in (grants, read, store, verify, application):
        source = inspect.getsource(module)
        assert "list_objects" not in source and "Prefix=" not in source
