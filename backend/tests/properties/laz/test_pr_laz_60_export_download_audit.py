"""PR-LAZ-60 (exportación) y SUP-U05-40 y 23: cada emisión de una URL de descarga, de volumen o
del documento, deja **exactamente una** entrada de auditoría declarada como concesión de acceso;
ninguna operación lista ``export/`` (TASK-345; PAT-LAZ-SEG-04; NFR-LAZ-16).

- **Propiedad** (secuencias generadas de descargas de volúmenes y del documento, por la persona
  con alcance, por otra organización, por un gerente de otra planta, de una exportación
  inexistente y de una sin terminar): las entradas ``export_download_granted`` nuevas son
  exactamente las URL emitidas, una por URL, en el mismo orden, cada una con el artefacto (el
  volumen o ``document.pdf``) y su SHA-256, el actor, el instante, la vigencia de la URL (15 min
  del reloj inyectado) y ``records = access_grant_not_download``; las descargas que no emiten
  URL no dejan ninguna. El firmante solo firma las claves de esa exportación, cada una con su
  ``version_id``, una vez por URL emitida.
- **Sin listado**: el puerto del almacén de la descarga solo firma una clave y una versión, y
  ni ``download`` ni las rutas mencionan una operación de listado.

Sobre PostgreSQL 16 real (la auditoría es la de U-02) con un firmante en memoria: los bytes los
cubre ``tests/integration/test_export_download_routes.py``. Solo datos generados.
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.export_support import ExportStack, Person, export_body, export_stack
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.accreditation.adapters.http import export_routes
from vigia_platform.accreditation.export import download
from vigia_platform.accreditation.export.composer_port import ExportJob, ExportPackage, ExportSink
from vigia_platform.accreditation.export.download import ExportDownloadSigner
from vigia_platform.accreditation.export.volumes import (
    ExportLocation,
    StoredObject,
    StoredObjects,
    Volume,
)
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import PresignedRequest

pytestmark = pytest.mark.integration

OPERATION: Final = "export_download_granted"
VOLUMES: Final = 3


@dataclass
class RecordingSigner:
    """``ExportDownloadSigner`` en memoria con el reloj de la pila: anota cada firma."""

    clock: Any
    signed: list[tuple[str, str | None]] = field(default_factory=list)

    async def presign_attachment_get(
        self, key: str, ttl: timedelta = timedelta(minutes=15), *, version_id: str | None = None
    ) -> PresignedRequest:
        self.signed.append((key, version_id))
        expires_at: datetime = self.clock.now() + ttl
        url = f"https://almacen.invalid/{key}?v={version_id}"
        return PresignedRequest("GET", url, {}, expires_at)


def _digest(text: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, text).hex * 2


class StoredComposer:
    """Compositor que declara tres volúmenes y el documento escritos (sin bytes)."""

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        document = _digest(f"{job.export_id}-document")
        stored = StoredObjects(
            volumes=tuple(
                Volume(
                    number=number,
                    size_bytes=1000 * number,
                    sha256=_digest(f"{job.export_id}-{number}"),
                    version_id=f"v{number}",
                )
                for number in range(1, VOLUMES + 1)
            ),
            document=StoredObject(size_bytes=10, sha256=document, version_id="vd"),
        )
        return ExportPackage(
            document_sha256=document,
            archive_sha256=_digest(f"{job.export_id}-archive"),
            manifest={"manifest_version": 1},
            size_bytes=6000,
            artifact_count=4,
            verifier_version="1.0.0",
            stored=stored,
        )


@dataclass
class World:
    stack: ExportStack
    signer: RecordingSigner
    owner: Person
    other_org: Person
    other_plant: Person
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    ready: uuid.UUID
    pending: uuid.UUID


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    signers: list[RecordingSigner] = []

    def storage(clock: Any) -> RecordingSigner:
        signers.append(RecordingSigner(clock))
        return signers[0]

    with export_stack(postgres_endpoint, "pr_laz_60_export", storage=storage) as stack:
        site = stack.site(plants=2, zones=2)
        plant, other = list(site.plants)
        owner = stack.member(site)
        start = stack.now() - timedelta(days=40)
        exports = []
        for _ in range(2):
            response = stack.request_export(
                owner, export_body(plant, list(site.plants[plant]), start=start)
            )
            assert response.status_code == 202, response.text
            exports.append(uuid.UUID(response.json()["export_id"]))
        worker = stack.export_worker(composer=StoredComposer())
        stack.run(worker.run(stack.event_context(site.organization_id), exports[0]))
        assert stack.export_row(exports[0])["state"] == "ready"
        other_plant = stack.member(site, Role.PLANT_MANAGER, ScopeLevel.PLANT, other)
        other_org = stack.member(stack.site(plants=1, zones=1))
        yield World(
            stack,
            signers[0],
            owner,
            other_org,
            other_plant,
            site.organization_id,
            plant,
            exports[0],
            exports[1],
        )


def _entries_after(world: World, sequence: int) -> list[Any]:
    return world.stack.fetch(
        "SELECT chain_sequence, actor_id, outcome, resource_kind, resource_id, scope_plant_id,"
        " convert_from(filters, 'UTF8') AS filters, occurred_at FROM shared.audit_entry"
        " WHERE operation = $1 AND organization_id = $2 AND chain_sequence > $3"
        " ORDER BY chain_sequence",
        OPERATION,
        world.organization_id,
        sequence,
    )


def _last_sequence(world: World) -> int:
    (row,) = world.stack.fetch(
        "SELECT coalesce(max(chain_sequence), 0) AS last FROM shared.audit_entry"
        " WHERE organization_id = $1",
        world.organization_id,
    )
    return int(row["last"])


_CALLS: Final = st.lists(
    st.tuples(
        st.sampled_from(["volumes", "document"]),
        st.sampled_from(["owner", "other_org", "other_plant", "missing", "pending"]),
    ),
    min_size=1,
    max_size=6,
)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(calls=_CALLS)
def test_pr_laz_60_one_audit_entry_per_emitted_download_url(
    world: World, calls: list[tuple[str, str]]
) -> None:
    stack = world.stack
    location = ExportLocation(world.organization_id, world.plant_id, world.ready)
    before = _last_sequence(world)
    world.signer.signed.clear()
    emitted: list[tuple[str, str, str]] = []  # (artefacto, sha256, expires_at)
    for route, who in calls:
        person = {
            "owner": world.owner,
            "other_org": world.other_org,
            "other_plant": world.other_plant,
            "missing": world.owner,
            "pending": world.owner,
        }[who]
        export_id = {"missing": uuid.uuid4(), "pending": world.pending}.get(who, world.ready)
        if route == "volumes":
            response = stack.send(person, "GET", f"/accreditation/exports/{export_id}/download")
        else:
            response = stack.send(
                person, "POST", f"/accreditation/exports/{export_id}/download-url"
            )
        if who == "owner":
            assert response.status_code == 200, response.text
            body = response.json()
            if route == "volumes":
                emitted += [(v["name"], v["sha256"], v["expires_at"]) for v in body["volumes"]]
            else:
                emitted.append(("document.pdf", body["document_sha256"], body["expires_at"]))
        elif who == "pending":
            assert response.status_code == 409, response.text
            assert response.json()["export_state"] == "requested"
        else:
            assert response.status_code == 404, response.text
    entries = _entries_after(world, before)
    assert len(entries) == len(emitted)
    expires = format_timestamp(stack.now() + timedelta(minutes=15))
    for entry, (artifact, sha256, expires_at) in zip(entries, emitted, strict=True):
        assert json.loads(entry["filters"]) == {
            "artifact": artifact,
            "sha256": sha256,
            "records": "access_grant_not_download",
            "expires_at": expires_at,
        }
        assert expires_at == expires
        assert entry["actor_id"] == world.owner.user_id
        assert (entry["outcome"], entry["resource_kind"]) == ("success", "export")
        assert (entry["resource_id"], entry["scope_plant_id"]) == (world.ready, world.plant_id)
        assert entry["occurred_at"] is not None
    # Una firma por URL emitida, solo de las claves de esa exportación y con su versión.
    expected_keys = {
        **{f"vol-{n:02d}.zip": (location.volume_key(n), f"v{n}") for n in range(1, VOLUMES + 1)},
        "document.pdf": (location.document_key, "vd"),
    }
    assert world.signer.signed == [expected_keys[artifact] for artifact, _, _ in emitted]


def test_no_operation_lists_the_export_prefix() -> None:
    port = {
        name for name, _ in inspect.getmembers(ExportDownloadSigner) if not name.startswith("_")
    }
    assert port == {"presign_attachment_get"}
    for module in (download, export_routes):
        source = inspect.getsource(module)
        for word in ("list_objects", "Prefix=", "pending_multipart_uploads", "paginat"):
            assert word not in source, (module.__name__, word)
