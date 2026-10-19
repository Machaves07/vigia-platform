"""El compositor real contra PostgreSQL 16 y LocalStack (TASK-318; BR-LAZ-88 a 101; PR-LAZ-26;
NFR-LAZ-03).

**Con el consumidor real** (``export_worker`` como ``vigia_app``, solicitud por HTTP, el flujo
``ExportStream`` sobre LocalStack y el escritor del expediente):

- **De punta a punta**: la solicitud termina ``ready`` con su ``export_package``, el registro
  ``export_produced`` (sus conteos, hashes y puntos de control) con su evento ``export_available``
  en la misma transacción, ``record_id`` en la solicitud, y los volúmenes en el almacén con el
  manifiesto dentro de ``vol-01``.
- **Fallo cerrado** (BR-LAZ-91 y 101; un doble por puerto que falla): sin texto del estándar,
  sin cobertura, sin compuerta o sin puntos de control o claves, la solicitud termina ``failed``
  con su código, **sin** ``export_package`` ni ``export_produced``, y bajo
  ``…/export/{export_id}/`` LocalStack no tiene **ningún objeto** (``ListObjectsV2``) ni ninguna
  subida por partes a medias.

**Con el expediente real** (el mundo de U-04 de dos organizaciones, ``LoopWorld``): hallazgos y
detecciones ingresados por las rutas del contrato en las dos organizaciones y clasificados por el
escritor real; el compositor lee con ``LectorExpediente``, ``CoveragePort``, ``CatalogQueryPort`` y
``GateQueryPort`` reales (contados por ``InstrumentedPorts``), los lugares de ``identity`` y los
puntos de control escritos de verdad por ``CheckpointService``. El paquete lleva **exactamente**
lo que da el oráculo por fuerza bruta (plegar todo el expediente de la organización leído con
``LectorExpediente``), nada de la otra organización, y U-03 solo recibe formas por lote y rango.

Solo datos generados. Reloj simulado desde la hora de la base (retro 14); topes de 60 s.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, Final

import pytest

from tests.export_stream_support import MemoryExportStore, RecordingSink, read_volume
from tests.export_support import (
    CountingCheckpoints,
    ExportStack,
    RecordingAlarm,
    export_body,
    export_stack,
)
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.laz_support import (
    IngestedSubject,
    InstrumentedPorts,
    LoopWorld,
    loop_world,
    register_u04_and_catalog,
)
from tests.properties.laz.strategies.export import WorldPorts, job_world
from tests.signing_support import bootstrapped_world
from tests.writer_support import unit_context
from vigia_platform.accreditation.adapters.postgres.export_reads import (
    SqlCheckpointLookup,
    SqlEpisodeHistory,
    SqlExportPlaces,
)
from vigia_platform.accreditation.export.composer_port import (
    CHECKPOINT_UNAVAILABLE,
    ExportJob,
    ExportPackage,
    ExportSink,
)
from vigia_platform.accreditation.export.contents import (
    EMPTY_SHA256,
    ExportComposer,
    ExportPorts,
)
from vigia_platform.accreditation.export.lease import LeaseLost, SqlExportLeaseStore
from vigia_platform.accreditation.export.stream import ExportStream, ExportStreamSettings
from vigia_platform.accreditation.export.volumes import MANIFEST_NAME, ExportLocation
from vigia_platform.accreditation.export.worker import ExportWorker
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import (
    MAX_PAGE_SIZE,
    LectorExpediente,
    LedgerFilters,
    PageRequest,
    RecordCursor,
)
from vigia_platform.ledger.chain.checkpoints import CheckpointChain, CheckpointService
from vigia_platform.loop.domain.enums import PackageForm
from vigia_platform.loop.projection.fold import PROJECTED_RECORD_TYPES, ProjectionInput, fold
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.signing.keys import to_millisecond
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

KIB: Final = 1024
SETTINGS: Final = ExportStreamSettings(
    # 256 KiB no bastaba desde que el compositor real escribe el documento portátil (TASK-319,
    # VIG-236): con las dos fuentes Noto Sans completas (``full_fonts=True``, determinismo del
    # documento), el PDF ronda los 500 KiB él solo.
    part_size=5 * 1024 * KIB,
    volume_max_bytes=4 * 1024 * KIB,
    manifest_reserve_bytes=128 * KIB,
)


# --- con el consumidor real ------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExportStack]:
    with export_stack(postgres_endpoint, "export_contents") as built:
        yield built


@pytest.fixture(scope="module")
def bucket(localstack_endpoint: LocalStackEndpoint) -> Iterator[tuple[Any, str]]:
    s3 = localstack_endpoint.aws_client("s3")
    with versioned_bucket(s3, "vigia-export-contents") as name:
        yield localstack_endpoint, name


class WorldComposer:
    """El compositor real sobre el mundo determinista del trabajo (``job_world``): la base da los
    identificadores (solicitud, organización, planta, zonas y puntos de control)."""

    def __init__(self, stream: ExportStream, fail: frozenset[str] = frozenset()) -> None:
        self._stream = stream
        self._fail = fail
        self.ports: WorldPorts | None = None

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        self.ports = WorldPorts(job_world(job, findings_per_zone=3), fail=self._fail)
        composer = ExportComposer(stream=self._stream, ports=self.ports.ports())
        return await composer.compose(job, sink)


def _requested(stack: ExportStack) -> tuple[uuid.UUID, uuid.UUID]:
    site = stack.site(plants=1, zones=2)
    plant = next(iter(site.plants))
    person = stack.member(site)
    start = stack.now() - timedelta(days=40)
    response = stack.request_export(
        person, export_body(plant, list(site.plants[plant]), start=start, form="with_clips")
    )
    assert response.status_code == 202, response.text
    return site.organization_id, uuid.UUID(response.json()["export_id"])


def _worker(stack: ExportStack, storage: S3Storage, composer: Any) -> ExportWorker:
    return ExportWorker(
        leases=SqlExportLeaseStore(database=stack.database, writer=stack.writer),
        checkpoints=CountingCheckpoints(),
        uploads=ExportStream(store=storage, settings=SETTINGS),
        composer=composer,
        alarm=RecordingAlarm(),
        clock=stack.clock,
        owner=f"worker-{uuid.uuid4().hex[:8]}",
    )


def _objects(endpoint: LocalStackEndpoint, bucket: str, prefix: str) -> tuple[list[str], list[str]]:
    s3 = endpoint.aws_client("s3")
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    uploads = s3.list_multipart_uploads(Bucket=bucket, Prefix=prefix).get("Uploads", [])
    return [item["Key"] for item in listed], [item["Key"] for item in uploads]


def _produced(stack: ExportStack, organization: uuid.UUID) -> list[Any]:
    return stack.fetch(
        "SELECT record_id, ledger.vigia_bytes_to_jsonb(content)::text AS content"
        " FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type = 'export_produced'",
        organization,
    )


def test_the_real_composer_ends_ready_with_export_produced_and_export_available(
    stack: ExportStack, bucket: tuple[LocalStackEndpoint, str]
) -> None:
    endpoint, name = bucket
    storage = S3Storage(endpoint.storage_settings(name), stack.clock)
    organization, export_id = _requested(stack)
    composer = WorldComposer(ExportStream(store=storage, settings=SETTINGS))
    worker = _worker(stack, storage, composer)
    stack.run(worker.run(stack.event_context(organization), export_id))
    row = stack.export_row(export_id)
    assert row["state"] == "ready", row
    package = stack.package_row(export_id)
    assert package is not None
    manifest = (
        json.loads(package["manifest"])
        if isinstance(package["manifest"], str)
        else package["manifest"]
    )
    assert manifest["export_id"] == str(export_id)
    assert manifest["package_form"] == "with_clips"
    (produced,) = _produced(stack, organization)
    assert row["record_id"] == produced["record_id"]
    content = json.loads(produced["content"])
    assert content["export_id"] == str(export_id)
    assert content["archive_sha256"] == package["archive_sha256"]
    assert content["document_sha256"] == package["document_sha256"]
    assert content["finding_count"] == manifest["finding_count"] == 6
    assert content["checkpoint_record_ids"] == [str(v) for v in row["checkpoint_record_ids"]]
    assert content["verifier_version"] == "absent"
    (event,) = stack.fetch(
        "SELECT payload::text AS payload FROM shared.outbox_event WHERE organization_id = $1"
        " AND event_name = 'export_available'",
        organization,
    )
    payload = json.loads(event["payload"])
    assert payload["record_id"] == str(produced["record_id"])
    assert payload["export_id"] == str(export_id)
    # Los volúmenes están en el almacén y el manifiesto viaja en vol-01.
    location = ExportLocation(organization, row["plant_id"], export_id)
    keys, uploads = _objects(endpoint, name, location.prefix)
    assert location.volume_key(1) in keys and uploads == []
    volume = stack.run(storage.get_object(location.volume_key(1)))
    assert json.loads(read_volume(volume).read(MANIFEST_NAME)) == manifest


@pytest.mark.parametrize(
    ("fail", "code"),
    [
        ("standards", "loop_standard_text_unavailable"),
        ("coverage", "accreditation_coverage_unavailable"),
        ("gates", "loop_gate_state_unavailable"),
        ("keys", CHECKPOINT_UNAVAILABLE),
        ("checkpoints", CHECKPOINT_UNAVAILABLE),
    ],
)
def test_a_missing_input_fails_declared_without_any_object(
    stack: ExportStack, bucket: tuple[LocalStackEndpoint, str], fail: str, code: str
) -> None:
    endpoint, name = bucket
    storage = S3Storage(endpoint.storage_settings(name), stack.clock)
    organization, export_id = _requested(stack)
    composer = WorldComposer(ExportStream(store=storage, settings=SETTINGS), frozenset({fail}))
    worker = _worker(stack, storage, composer)
    stack.run(worker.run(stack.event_context(organization), export_id))
    row = stack.export_row(export_id)
    assert (row["state"], row["failure_code"], row["record_id"]) == ("failed", code, None)
    assert stack.package_row(export_id) is None
    assert _produced(stack, organization) == []
    location = ExportLocation(organization, row["plant_id"], export_id)
    assert _objects(endpoint, name, location.prefix) == ([], [])
    assert composer.ports is not None and composer.ports.calls["clip_bytes"] == 0


# --- export_produced en la transacción de ready ------------------------------------------------


def _real_package() -> ExportPackage:
    """Un paquete con los conteos del compositor real (sin bytes: el cierre no los mira)."""
    return ExportPackage(
        document_sha256=EMPTY_SHA256,
        archive_sha256="a" * 64,
        manifest={"manifest_version": 1},
        size_bytes=1,
        artifact_count=5,
        verifier_version="absent",
        manifest_sha256="b" * 64,
        finding_counts={
            "finding_count": 0,
            "open_finding_count": 0,
            "closed_finding_count": 0,
            "unclosable_count": 0,
            "discarded_count": 0,
        },
    )


def _acquired(stack: ExportStack, leases: SqlExportLeaseStore, owner: str) -> Any:
    organization, export_id = _requested(stack)
    context = stack.event_context(organization)
    acquisition = stack.run(leases.acquire(context, export_id, owner, stack.now()))
    lease = acquisition.lease
    assert lease is not None
    lease = stack.run(
        leases.fix_first_attempt(context, lease, (uuid.uuid4(),), stack.now(), stack.now())
    )
    return organization, export_id, context, lease


def test_a_process_that_lost_the_lease_leaves_no_export_produced(stack: ExportStack) -> None:
    """``export_produced`` va en la transacción de ``ready``: si la valla no confirma (otro
    proceso tomó la solicitud), el registro y su evento se deshacen con ella."""
    leases = SqlExportLeaseStore(database=stack.database, writer=stack.writer)
    organization, export_id, context, lease = _acquired(stack, leases, "worker-a")
    stack.clock.set(lease.until + timedelta(milliseconds=1))
    taken = stack.run(leases.acquire(context, export_id, "worker-b", stack.now())).lease
    assert taken is not None
    now = stack.now()
    with pytest.raises(LeaseLost):
        stack.run(
            leases.finish_ready(context, lease, _real_package(), now, now + timedelta(days=30))
        )
    assert _produced(stack, organization) == []
    assert stack.package_row(export_id) is None
    stack.run(leases.finish_ready(context, taken, _real_package(), now, now + timedelta(days=30)))
    (produced,) = _produced(stack, organization)
    assert stack.export_row(export_id)["record_id"] == produced["record_id"]


def test_two_concurrent_closings_write_a_single_export_produced(stack: ExportStack) -> None:
    """Dos cierres a la vez del mismo arrendamiento (dos conexiones): uno gana; nunca dos
    registros ``export_produced`` ni dos ``export_available``."""
    leases = SqlExportLeaseStore(database=stack.database, writer=stack.writer)
    organization, export_id, context, lease = _acquired(stack, leases, "worker-c")
    other = SqlExportLeaseStore(database=stack.new_database(), writer=stack.writer)
    now = stack.now()

    async def race() -> list[Any]:
        return await asyncio.gather(
            leases.finish_ready(context, lease, _real_package(), now, now + timedelta(days=30)),
            other.finish_ready(context, lease, _real_package(), now, now + timedelta(days=30)),
            return_exceptions=True,
        )

    outcomes = stack.run(race())
    assert sum(outcome is None for outcome in outcomes) == 1, outcomes
    assert len(_produced(stack, organization)) == 1
    events = stack.fetch(
        "SELECT 1 FROM shared.outbox_event WHERE organization_id = $1"
        " AND event_name = 'export_available'",
        organization,
    )
    assert len(events) == 1
    assert stack.export_row(export_id)["state"] == "ready"


# --- con el expediente real ------------------------------------------------------------------


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[LoopWorld]:
    with loop_world(
        postgres_endpoint, "export_contents_ledger", register=register_u04_and_catalog
    ) as built:
        yield built


def _ledger_now(world: LoopWorld) -> Any:
    """El reloj con que la base sella ``received_at`` (el periodo se compara con él), un
    milisegundo por delante de lo ya recibido."""
    (row,) = world.stack.fetch("SELECT clock_timestamp() AS now")
    return to_millisecond(row["now"]) + timedelta(milliseconds=1)


def _segment(
    world: LoopWorld, organization: Any, previous: IngestedSubject | None, index: int
) -> dict[str, Any]:
    """Un tramo ``index`` de un episodio: el de ``previous`` (cita su ``finding_id``, como el
    nodo) o, sin él, uno nuevo cuyo tramo anterior no llega nunca."""
    document = world.stack.finding(organization.site)
    if previous is not None:
        episode_id = previous.document["episode"]["episode_id"]
        previous_id = previous.document["finding_id"]
    else:
        episode_id, previous_id = str(world.stack.uuid7()), str(world.stack.uuid7())
    document["episode"] = {
        **document["episode"],
        "episode_id": episode_id,
        "segment_index": index,
        "previous_segment_record_id": previous_id,
    }
    return document


def _all_records(world: LoopWorld, organization: Any) -> list[ProjectionInput]:
    """Todo el expediente de la organización, página a página por ``LectorExpediente``."""
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    reader = LectorExpediente(database=database, audit=sessions.audit, clock=sessions.clock)
    context = world.event_context(organization)
    items: list[ProjectionInput] = []
    cursor: RecordCursor | None = None
    while True:
        page = world.stack.run(
            reader.list(
                context,
                LedgerFilters(record_types=tuple(sorted(PROJECTED_RECORD_TYPES))),
                PageRequest(size=MAX_PAGE_SIZE, after=cursor),
            )
        )
        items.extend(
            ProjectionInput(
                record_id=r.record_id,
                organization_id=r.organization_id,
                plant_id=r.plant_id,
                chain_sequence=r.chain_sequence,
                record_type=r.record_type,
                received_at=r.received_at,
                document=r.document(),
            )
            for r in page.items
        )
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


def test_pr_laz_26_on_the_real_ledger_with_the_brute_force_oracle(world: LoopWorld) -> None:
    a, b = world.a, world.b
    # El texto declarado del estándar que citan los hallazgos (BR-LAZ-95), vigente desde que se
    # publicó el catálogo de la zona (el sitio de la ingesta solo publica la versión del catálogo).
    world.stack.execute(
        "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
        " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
        " predicate, catalog_version, reason_es) VALUES ($1, $2, $3, $4, 1, 'coexistence',"
        " 'Zona de prensa', 'Ninguna persona dentro del perímetro con la máquina energizada.',"
        " $5, $6, '{}', 1, 'Versión sintética del estándar')",
        a.organization_id,
        a.plant_id,
        a.zone_id,
        a.site.standard_id,
        json.dumps(
            {"user_id": str(uuid.uuid4()), "display_name": "Firmante", "role": "administrator"}
        ),
        world.stack.now() - timedelta(days=400),
    )
    # Un episodio que empezó antes del periodo: su tramo 0 llega antes y su tramo 1, dentro.
    earlier = world.ingest_finding(a)
    world.stack.tick(5)
    start = _ledger_now(world)
    world.stack.tick(5)
    # Expediente de las dos organizaciones en el mismo intervalo.
    confirmed = world.ingest_finding(a)
    discarded = world.ingest_finding(a)
    pending = world.ingest_finding(a)
    detection = world.ingest_detection(a)
    foreign = world.ingest_finding(b)
    continued = world._ingest(a, IngestKind.FINDING, _segment(world, a, earlier, 1))
    # Un episodio cuyo tramo 0 no llega nunca: su tramo 1 lo ancla en el periodo.
    orphan = world._ingest(a, IngestKind.FINDING, _segment(world, a, None, 1))
    for subject in (earlier, confirmed, discarded, pending, detection, foreign, continued, orphan):
        world.project(subject)
    world.write(a, "classification", world.classification_content(confirmed))
    world.write(
        a,
        "classification",
        world.classification_content(
            discarded,
            outcome="false_positive",
            reason_category="reflection_or_glare",
            effect_on_loop="none",
        ),
    )
    world.stack.tick(5)
    end = _ledger_now(world)
    # Puntos de control de verdad (U-02) y su búsqueda por identificador.
    database = world.stack.primary.database
    sessions = world.stack.authz.sessions
    signing = world.stack.run(bootstrapped_world())
    checkpoints = CheckpointService(
        store=SqlCheckpointStore(
            database=database,
            writer=world.stack.primary.writer,
            audit=sessions.audit,
            outbox=world.stack.outbox,
        ),
        signer=signing.service,
        clock=sessions.clock,
    )
    u02 = unit_context(a.organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)
    results = world.stack.run(
        checkpoints.write_checkpoints_now(
            u02, (CheckpointChain.plant(a.plant_id), CheckpointChain.audit())
        )
    )
    ids = tuple(r.checkpoint.entry_id for r in results if r.checkpoint is not None)
    assert ids
    job = ExportJob(
        export_id=uuid.uuid4(),
        organization_id=a.organization_id,
        plant_id=a.plant_id,
        zone_ids=(a.zone_id,),
        period_from=to_millisecond(start),
        period_to=to_millisecond(end),
        package_form=PackageForm.WITHOUT_CLIPS,
        requested_by={"user_id": str(uuid.uuid4()), "display_name": "P", "role_in_use": None},
        correlation_id=uuid.uuid4(),
        attempt=1,
        generated_at=to_millisecond(end + timedelta(seconds=1)),
        checkpoint_record_ids=ids,
        context=world.event_context(a),
    )
    world.stack.tick(5)
    doubles = WorldPorts(job_world(job))
    counted = InstrumentedPorts(
        organization_id=a.organization_id, plant_id=a.plant_id, wrapped=world.real_ports()
    )
    ports = ExportPorts(
        reader=LectorExpediente(database=database, audit=sessions.audit, clock=sessions.clock),
        catalog=counted,
        gates=counted,
        fleet=doubles,
        coverage=counted,
        markers=EvidenceService(
            database=database, audit=sessions.audit, storage=world.stack.storage
        ),
        checkpoints=SqlCheckpointLookup(database, checkpoints),
        places=SqlExportPlaces(database),
        blobs=doubles,
        episodes=SqlEpisodeHistory(database),
    )
    store = MemoryExportStore()
    composer = ExportComposer(stream=ExportStream(store=store, settings=SETTINGS), ports=ports)
    produced = world.stack.run(composer.compose(job, RecordingSink()))
    manifest = produced.manifest
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    archive = read_volume(store.latest(location.volume_key(1)))
    found = json.loads(archive.read(f"findings/zone-{a.zone_id}.json"))["findings"]
    # Oráculo por fuerza bruta: todo el expediente de la organización, plegado, y los sujetos
    # cuyo primer registro de U-03 cae en el periodo.
    items = _all_records(world, a)
    first: dict[str, Any] = {}
    for item in sorted(items, key=lambda entry: entry.chain_sequence):
        if item.record_type in ("finding_received", "detection_for_review_received"):
            key = item.document.get("detection_id") or item.document["episode"]["episode_id"]
            first.setdefault(str(key), item.received_at)
    expected = {
        str(subject): row.lifecycle_state
        for (organization_id, _), projection in fold(items).items()
        if organization_id == a.organization_id
        for subject, row in projection.findings.items()
        if row.zone_id == a.zone_id and job.period_from <= first[str(subject)] < job.period_to
    }
    assert {f["subject_id"]: f["lifecycle_state"] for f in found} == expected
    assert set(expected) == {
        str(subject.subject_id) for subject in (confirmed, discarded, pending, detection, orphan)
    }
    assert str(earlier.subject_id) not in expected  # empezó antes del periodo
    assert expected[str(confirmed.subject_id)] == "open"
    assert expected[str(discarded.subject_id)] == "discarded"
    assert str(foreign.subject_id) not in {f["subject_id"] for f in found}
    assert manifest["finding_count"] == 5
    assert manifest["organization"]["organization_id"] == str(a.organization_id)
    assert {c["entry_id"] for c in manifest["checkpoints"]} == {str(i) for i in ids}
    assert manifest["checkpoint_public_keys"]
    # U-03 solo por lote y por rango.
    u03 = {k: v for k, v in counted.calls.items() if k != "linea_de_tiempo"}
    assert set(u03) <= {"single_occupancy_many", "standards_at_many", "gate_history"}
    assert u03["gate_history"] == 1 and u03["standards_at_many"] == 1
