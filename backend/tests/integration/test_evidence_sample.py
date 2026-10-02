"""``EvidencePort`` y la tarea ``evidence_sample`` contra PostgreSQL 16 y LocalStack (TASK-121).

Base migrada hasta ``nuc_0006`` y escritor, auditoría y bandeja reales como ``vigia_app`` (nunca
superusuario); depósito propio **versionado** en LocalStack con los clips sintéticos subidos con
su suma SHA-256 y el metadato ``x-amz-meta-vigia-anonymized: 1``, como los sube el nodo.

- **Criterio 1** (BR-NUC-66): ``url_lectura`` emite una URL de solo lectura que caduca a los
  5 minutos o antes, fijada a la versión verificada, y cada emisión deja una entrada
  ``evidence_read_granted``. Fuera del alcance de ``evidence.read`` (otro rol, otra zona, otra
  organización) responde ``not_found`` y queda auditado como ``denied``; un objeto sustituido no
  se concede.
- **Criterio 2** (NFR-NUC-33, pendiente nº 21): un clip sin marca inyectado en LocalStack produce
  ``security_alert``, el evento ``evidence_marker_verification_failed`` con su carga cerrada y
  ``marker_verification_result = broken``; el objeto sigue existiendo. Contenedor ilegible y
  objeto sustituido dan ``metadata_unreadable`` y ``marker_unverifiable``.
- **Criterio 3**: la selección se vuelve a calcular igual con la semilla registrada en
  ``ledger.evidence_sample_run`` y en la auditoría; una segunda pasada reutiliza la semilla.
- **Criterio 4**: una segunda escritura de ``marker_verification_result`` falla en la base
  (también como superusuario), y ``resultados_marca`` de 500 evidencias es una sola consulta.
- **Fallos a mitad de operación**: el almacén caído con un clip lo deja ``pending`` y la pasada
  termina en ``StorageUnavailable``; un fallo en cualquier sentencia de la escritura del
  resultado no deja ni transición ni evento; la pasada siguiente completa lo pendiente.

Solo datos generados: contenedores MP4 sintéticos de ``synthetic_clip`` (U-01), nunca clips reales.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable
from vigia_contracts.conformance.stub_platform.objects import synthetic_clip

from tests.factories import uuid7
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.ledger_database import evidence_values, insert_evidence
from tests.writer_support import (
    NOW,
    ORDER_TYPE,
    Fault,
    Place,
    WriterEnvironment,
    order_document,
    unit_context,
    writer_environment,
)
from vigia_platform.ledger.application.evidence_read import (
    READ_URL_TTL,
    EvidenceNotFound,
    EvidenceService,
    EvidenceUnreadable,
    MarkerResult,
)
from vigia_platform.ledger.application.evidence_sample import (
    EVIDENCE_SAMPLE,
    EvidenceSampler,
    FailureReason,
    evidence_sample_handler,
    register_evidence_sample,
    sample_size,
    select_sample,
)
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry
from vigia_platform.shared.outbox.u02_events import EvidenceMarkerVerificationFailed
from vigia_platform.shared.storage import (
    ANONYMIZED_METADATA_KEY,
    ObjectHead,
    S3Storage,
    StorageUnavailable,
    sha256_b64,
)

pytestmark = pytest.mark.integration

DAY = NOW.date()
"""Día en que el escritor registra las evidencias (``verified_at`` del reloj simulado)."""

RESTRICT_VIOLATION = "23001"
INSUFFICIENT_PRIVILEGE = "42501"


# --- Entorno ----------------------------------------------------------------------------------


@dataclass
class Environment:
    env: WriterEnvironment
    s3: Any
    bucket: str
    storage: S3Storage
    service: EvidenceService
    endpoint: LocalStackEndpoint

    def run(self, awaitable: Any) -> Any:
        return self.env.loop.run(awaitable)

    @property
    def migrated(self) -> MigratedDatabase:
        return self.env.migrated

    def sampler(self, storage: Any = None, seed: bytes | None = None) -> EvidenceSampler:
        seeds = iter([seed] if seed is not None else [])
        return EvidenceSampler(
            database=self.env.database,
            audit=self.env.audit,
            outbox=self.env.outbox,  # type: ignore[arg-type]
            storage=storage or self.storage,
            clock=self.env.clock,
            random_bytes=(lambda n: next(seeds, None) or bytes(uuid.uuid4().bytes * 2)[:n]),
        )


@pytest.fixture(scope="module")
def environment(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[Environment]:
    s3 = localstack_endpoint.aws_client("s3")
    with (
        migrated_database(postgres_endpoint, "evidence_sample") as migrated,
        writer_environment(migrated) as env,
        versioned_bucket(s3, "vigia-evidence-sample") as bucket,
    ):
        storage = S3Storage(localstack_endpoint.storage_settings(bucket), env.clock)
        service = EvidenceService(database=env.database, audit=env.audit, storage=storage)
        yield Environment(env, s3, bucket, storage, service, localstack_endpoint)


def person_context(
    organization_id: uuid.UUID, scopes: Sequence[AllowedScope], role: Role
) -> ScopeContext:
    actor = Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        display_name_snapshot="Persona sintética",
        unit=ActorUnit.U02,
        role_in_use=role,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.SESSION,
        allowed_scopes=list(scopes),
        correlation_id=uuid7(),
        session_id_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )


def periodic_context(organization_id: uuid.UUID) -> ScopeContext:
    """El contexto de iteración periódica con que el planificador invoca la tarea."""
    actor = Actor(
        kind=ActorKind.SYSTEM,
        id=uuid.uuid4(),
        display_name_snapshot="vigia-worker",
        unit=ActorUnit.U02,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.PERIODIC_ITERATION,
        allowed_scopes=[AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.ADMINISTRATOR)],
        correlation_id=uuid7(),
    )


# --- Clips sintéticos en LocalStack -----------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    evidence_id: uuid.UUID
    storage_key: str
    content: bytes
    version_id: str


def _clip_document(place: Place, content: bytes, offset_ms: int) -> dict[str, Any]:
    clip_id = str(uuid7())
    starts = NOW + timedelta(milliseconds=offset_ms)

    def stamp(moment: datetime) -> str:
        return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"

    return {
        "clip_id": clip_id,
        "camera_id": str(uuid.uuid4()),
        "media_kind": "video",
        "content_type": "video/mp4",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "duration_ms": 10_000,
        "starts_at": stamp(starts),
        "ends_at": stamp(starts + timedelta(seconds=10)),
        "segment": "full",
        "anonymized": True,
        "storage_key": place.storage_key(clip_id),
    }


def upload(environment: Environment, key: str, content: bytes, *, marker: str = "1") -> str:
    """Sube como el nodo: suma SHA-256 exigida y metadato de anonimización; devuelve la versión."""
    response = environment.s3.put_object(
        Bucket=environment.bucket,
        Key=key,
        Body=content,
        ContentType="video/mp4",
        ChecksumSHA256=sha256_b64(content),
        Metadata={ANONYMIZED_METADATA_KEY: marker},
    )
    return str(response["VersionId"])


def register_clips(environment: Environment, place: Place, contents: Sequence[bytes]) -> list[Clip]:
    """Sube los clips y los registra con ``EscritorExpediente`` (hasta 3 por registro)."""
    env = environment.env
    clips: list[Clip] = []
    for start in range(0, len(contents), 3):
        chunk = contents[start : start + 3]
        document = order_document(place, clips=0)
        document["clips"] = [
            _clip_document(place, content, index * 1000) for index, content in enumerate(chunk)
        ]
        versions = []
        for clip, content in zip(document["clips"], chunk, strict=True):
            versions.append(upload(environment, clip["storage_key"], content))
            env.storage.put(clip)
        context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
        receipt = environment.run(env.writer.write(context, ORDER_TYPE, document))
        assert isinstance(receipt, Receipt), receipt
        ids = environment.run(_evidence_ids(environment.migrated, receipt.record_id))
        for clip, content, version in zip(document["clips"], chunk, versions, strict=True):
            clips.append(Clip(ids[clip["storage_key"]], clip["storage_key"], content, version))
    return clips


# --- Lecturas como superusuario ----------------------------------------------------------------


async def _fetch(migrated: MigratedDatabase, statement: str, *args: Any) -> list[Any]:
    connection = await migrated.connect()
    try:
        return list(await connection.fetch(statement, *args))
    finally:
        await connection.close()


async def _evidence_ids(migrated: MigratedDatabase, record_id: uuid.UUID) -> dict[str, uuid.UUID]:
    rows = await _fetch(
        migrated,
        "SELECT storage_key, evidence_id FROM ledger.evidence WHERE record_id = $1",
        record_id,
    )
    return {row["storage_key"]: row["evidence_id"] for row in rows}


def evidence_row(environment: Environment, evidence_id: uuid.UUID) -> Any:
    rows = environment.run(
        _fetch(
            environment.migrated,
            "SELECT * FROM ledger.evidence WHERE evidence_id = $1",
            evidence_id,
        )
    )
    assert len(rows) == 1
    return rows[0]


def audit_entries(
    environment: Environment, organization_id: uuid.UUID, operation: str
) -> list[Any]:
    return environment.run(
        _fetch(
            environment.migrated,
            "SELECT operation, outcome, resource_kind, resource_id, result_count, scope_plant_id,"
            " scope_zone_id, filters_json FROM shared.audit_entry"
            " WHERE organization_id = $1 AND operation = $2 ORDER BY chain_sequence",
            organization_id,
            operation,
        )
    )


def events(environment: Environment, organization_id: uuid.UUID, name: str) -> list[Any]:
    return environment.run(
        _fetch(
            environment.migrated,
            "SELECT plant_id, payload FROM shared.outbox_event"
            " WHERE organization_id = $1 AND event_name = $2 ORDER BY created_at, event_id",
            organization_id,
            name,
        )
    )


def object_versions(environment: Environment, key: str) -> list[str]:
    listing = environment.s3.list_object_versions(Bucket=environment.bucket, Prefix=key)
    return [item["VersionId"] for item in listing.get("Versions", []) if item["Key"] == key]


def run_task(
    environment: Environment, organization_id: uuid.UUID, sampler: EvidenceSampler
) -> None:
    """La tarea registrada, como la invoca el planificador: en una transacción por organización."""
    registry = PeriodicTaskRegistry()
    task = register_evidence_sample(registry, evidence_sample_handler(sampler))
    assert task.task_name == EVIDENCE_SAMPLE

    async def once() -> None:
        async with environment.env.database.transaction(
            periodic_context(organization_id)
        ) as transaction:
            await task.handler(transaction)

    environment.run(once())


def on_next_day(environment: Environment) -> None:
    """El reloj a la hora de la tarea (01:00 UTC del día siguiente al registro)."""
    environment.env.clock.set(datetime.combine(DAY + timedelta(days=1), datetime.min.time(), UTC))
    environment.env.clock.advance(3600)


@pytest.fixture(autouse=True)
def _registration_day(environment: Environment) -> Iterator[None]:
    """Cada prueba registra sus evidencias el día ``DAY`` a la hora de ``NOW``."""
    environment.env.clock.set(NOW)
    yield
    environment.env.clock.set(NOW)


# --- Criterio 1: URL de lectura de 5 minutos, auditada -----------------------------------------


def test_read_url_expires_within_five_minutes_and_each_grant_is_audited(
    environment: Environment,
) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("lectura")])
    coordinator = person_context(
        place.organization_id,
        [AllowedScope(ScopeLevel.ZONE, place.zone_id, Role.COORDINATOR_SST)],
        Role.COORDINATOR_SST,
    )
    issued_at = environment.env.clock.now()

    grant = environment.run(environment.service.url_lectura(coordinator, clip.evidence_id))

    assert grant.evidence_id == clip.evidence_id
    assert issued_at < grant.expires_at <= issued_at + timedelta(minutes=5)
    assert timedelta(minutes=5) == READ_URL_TTL
    query = parse_qs(urlsplit(grant.url).query)
    assert int(query["X-Amz-Expires"][0]) <= 300
    assert query["versionId"] == [clip.version_id]
    assert grant.url not in repr(grant)
    response = httpx.get(grant.url, timeout=10)
    assert response.status_code == 200
    assert response.content == clip.content
    # Solo lectura: la firma es de GET; ni PUT ni DELETE pasan con la misma URL.
    assert httpx.put(grant.url, content=b"x", timeout=10).status_code == 403
    assert httpx.delete(grant.url, timeout=10).status_code == 403

    entries = audit_entries(environment, place.organization_id, "evidence_read_granted")
    assert [
        (e["outcome"], e["resource_kind"], e["resource_id"], e["result_count"]) for e in entries
    ] == [("success", "evidence", clip.evidence_id, 1)]
    assert (entries[0]["scope_plant_id"], entries[0]["scope_zone_id"]) == (
        place.plant_id,
        place.zone_id,
    )

    # Dos concesiones, dos entradas.
    environment.run(environment.service.url_lectura(coordinator, clip.evidence_id))
    assert len(audit_entries(environment, place.organization_id, "evidence_read_granted")) == 2


def test_read_url_is_pinned_to_the_verified_version(environment: Environment) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("versión")])
    context = person_context(
        place.organization_id,
        [AllowedScope(ScopeLevel.ORGANIZATION, place.organization_id, Role.PLANT_MANAGER)],
        Role.PLANT_MANAGER,
    )
    grant = environment.run(environment.service.url_lectura(context, clip.evidence_id))

    # Alguien deja otros bytes en la misma clave (el depósito versionado conserva los dos).
    upload(environment, clip.storage_key, synthetic_clip("sustituto"))
    assert len(object_versions(environment, clip.storage_key)) == 2

    # La URL ya emitida sigue sirviendo los bytes verificados, no los nuevos.
    assert httpx.get(grant.url, timeout=10).content == clip.content
    # Y no se concede una URL nueva sobre bytes que no son los del expediente.
    with pytest.raises(EvidenceUnreadable):
        environment.run(environment.service.url_lectura(context, clip.evidence_id))
    entries = audit_entries(environment, place.organization_id, "evidence_read_granted")
    assert [(e["outcome"], e["result_count"]) for e in entries] == [("success", 1), ("error", 0)]


def test_read_url_outside_evidence_read_is_not_found_and_audited_as_denied(
    environment: Environment,
) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("alcance")])
    organization_id = place.organization_id
    other_zone = uuid.uuid4()
    denied = [
        # Roles sin evidence.read, aun con alcance sobre la zona o la organización (RF-PLA-11).
        person_context(
            organization_id,
            [AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.ADMINISTRATOR)],
            Role.ADMINISTRATOR,
        ),
        person_context(
            organization_id,
            [AllowedScope(ScopeLevel.ZONE, place.zone_id, Role.LINE_MANAGER)],
            Role.LINE_MANAGER,
        ),
        person_context(
            organization_id,
            [AllowedScope(ScopeLevel.ZONE, place.zone_id, Role.COPASST)],
            Role.COPASST,
        ),
        # Rol con evidence.read, pero sobre otra zona o sobre otra planta.
        person_context(
            organization_id,
            [AllowedScope(ScopeLevel.ZONE, other_zone, Role.COORDINATOR_SST)],
            Role.COORDINATOR_SST,
        ),
        person_context(
            organization_id,
            [AllowedScope(ScopeLevel.PLANT, uuid.uuid4(), Role.PLANT_MANAGER)],
            Role.PLANT_MANAGER,
        ),
        # Alcance de organización de **otra** organización dentro del contexto.
        person_context(
            organization_id,
            [AllowedScope(ScopeLevel.ORGANIZATION, uuid.uuid4(), Role.COORDINATOR_SST)],
            Role.COORDINATOR_SST,
        ),
        # Sin alcances: falla cerrado.
        person_context(organization_id, [], Role.COORDINATOR_SST),
    ]
    for context in denied:
        with pytest.raises(EvidenceNotFound):
            environment.run(environment.service.url_lectura(context, clip.evidence_id))
    entries = audit_entries(environment, organization_id, "evidence_read_granted")
    assert len(entries) == len(denied)
    assert {(e["outcome"], e["resource_id"], e["result_count"]) for e in entries} == {
        ("denied", clip.evidence_id, 0)
    }
    assert all(e["scope_plant_id"] is None and e["scope_zone_id"] is None for e in entries)

    # Otra organización no la ve (seguridad a nivel de fila), aunque tenga el rol.
    stranger = uuid.uuid4()
    with pytest.raises(EvidenceNotFound):
        environment.run(
            environment.service.url_lectura(
                person_context(
                    stranger,
                    [AllowedScope(ScopeLevel.ORGANIZATION, stranger, Role.COORDINATOR_SST)],
                    Role.COORDINATOR_SST,
                ),
                clip.evidence_id,
            )
        )
    # La planta con plant_manager sí puede.
    plant = person_context(
        organization_id,
        [AllowedScope(ScopeLevel.PLANT, place.plant_id, Role.PLANT_MANAGER)],
        Role.PLANT_MANAGER,
    )
    assert environment.run(environment.service.url_lectura(plant, clip.evidence_id))


# --- Seguimientos de VIG-65 (TASK-137): versión ausente, auditoría caída y carreras ---


class _WithoutVersion:
    """``head_object`` como un almacén sin versionado: ``version_id`` nulo."""

    def __init__(self, inner: S3Storage) -> None:
        self._inner = inner
        self.presigned = 0

    async def head_object(self, key: str) -> ObjectHead | None:
        head = await self._inner.head_object(key)
        return None if head is None else replace(head, version_id=None)

    async def presign_get(self, key: str, ttl: timedelta = READ_URL_TTL, **kw: Any) -> Any:
        self.presigned += 1
        return await self._inner.presign_get(key, ttl, **kw)


def _coordinator(place: Place) -> ScopeContext:
    return person_context(
        place.organization_id,
        [AllowedScope(ScopeLevel.ZONE, place.zone_id, Role.COORDINATOR_SST)],
        Role.COORDINATOR_SST,
    )


def test_read_url_without_version_id_fails_closed(environment: Environment) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("sin versión")])
    storage = _WithoutVersion(environment.storage)
    service = EvidenceService(
        database=environment.env.database, audit=environment.env.audit, storage=storage
    )
    with pytest.raises(EvidenceUnreadable):
        environment.run(service.url_lectura(_coordinator(place), clip.evidence_id))
    # Ni siquiera se firma: una URL sin versión serviría cualquier versión posterior del objeto.
    assert storage.presigned == 0
    entries = audit_entries(environment, place.organization_id, "evidence_read_granted")
    assert [(e["outcome"], e["result_count"]) for e in entries] == [("error", 0)]


class _AuditDownOnSuccess:
    """El escritor de auditoría real, salvo que la entrada de la concesión no puede escribirse."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def provider_organization_id(self) -> uuid.UUID:
        return self._inner.provider_organization_id  # type: ignore[no-any-return]

    async def append(self, context: ScopeContext, operation: Any, **kw: Any) -> Any:
        if kw.get("result_count") == 1:
            raise StorageUnavailable("audit_entry")  # cualquier fallo al escribir la entrada
        return await self._inner.append(context, operation, **kw)


def test_no_url_leaves_the_service_when_the_grant_cannot_be_audited(
    environment: Environment,
) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("auditoría caída")])
    service = EvidenceService(
        database=environment.env.database,
        audit=_AuditDownOnSuccess(environment.env.audit),  # type: ignore[arg-type]
        storage=environment.storage,
    )
    with pytest.raises(StorageUnavailable):
        environment.run(service.url_lectura(_coordinator(place), clip.evidence_id))
    assert audit_entries(environment, place.organization_id, "evidence_read_granted") == []


class _ReplacedBeforeSigning:
    """Carrera 1: otros bytes llegan a la misma clave entre el ``HEAD`` y la firma de la URL."""

    def __init__(self, environment: Environment, substitute: bytes) -> None:
        self._environment = environment
        self._substitute = substitute

    async def head_object(self, key: str) -> ObjectHead | None:
        return await self._environment.storage.head_object(key)

    async def presign_get(self, key: str, ttl: timedelta = READ_URL_TTL, **kw: Any) -> Any:
        upload(self._environment, key, self._substitute)
        return await self._environment.storage.presign_get(key, ttl, **kw)


def test_race_replacement_between_head_and_signing_still_serves_the_verified_bytes(
    environment: Environment,
) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("carrera")])
    service = EvidenceService(
        database=environment.env.database,
        audit=environment.env.audit,
        storage=_ReplacedBeforeSigning(environment, synthetic_clip("sustituto en carrera")),
    )
    grant = environment.run(service.url_lectura(_coordinator(place), clip.evidence_id))
    assert len(object_versions(environment, clip.storage_key)) == 2
    query = parse_qs(urlsplit(grant.url).query)
    assert query["versionId"] == [clip.version_id]
    assert httpx.get(grant.url, timeout=10).content == clip.content


def test_race_two_concurrent_grants_are_two_audited_urls(environment: Environment) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("concurrentes")])
    context = _coordinator(place)

    async def both() -> list[Any]:
        return list(
            await asyncio.gather(
                environment.service.url_lectura(context, clip.evidence_id),
                environment.service.url_lectura(context, clip.evidence_id),
            )
        )

    grants = environment.run(both())
    assert all(httpx.get(g.url, timeout=10).content == clip.content for g in grants)
    entries = audit_entries(environment, place.organization_id, "evidence_read_granted")
    assert [(e["outcome"], e["result_count"]) for e in entries] == [("success", 1)] * 2


# --- Criterio 2: clip sin marca inyectado en LocalStack ----------------------------------------


def _unreadable_container() -> bytes:
    """Bytes que no son un MP4 legible (caja ``ftyp`` truncada); sin datos de persona."""
    return b"\x00\x00\x00\x40ftypisom" + bytes(8)


def test_unmarked_clip_raises_security_alert_event_and_broken_mark_without_deleting(
    environment: Environment,
) -> None:
    place = Place.new()
    organization_id = place.organization_id
    contents = [
        synthetic_clip("con marca 1"),
        synthetic_clip("sin marca", marked=False),
        _unreadable_container(),
        synthetic_clip("sustituido"),
        synthetic_clip("con marca 2"),
    ]
    marked_1, unmarked, unreadable, replaced, marked_2 = register_clips(
        environment, place, contents
    )
    # El objeto de ``replaced`` se sustituye después de registrar: sus bytes ya no son los
    # verificados, así que su marca no se puede verificar.
    upload(environment, replaced.storage_key, synthetic_clip("otro contenido"))
    versions_before = {
        clip.storage_key: object_versions(environment, clip.storage_key)
        for clip in (marked_1, unmarked, unreadable, replaced, marked_2)
    }

    on_next_day(environment)
    sampled_at = environment.env.clock.now()
    run_task(environment, organization_id, environment.sampler())

    expected = {
        marked_1.evidence_id: (MarkerResult.INTACT, None),
        marked_2.evidence_id: (MarkerResult.INTACT, None),
        unmarked.evidence_id: (MarkerResult.BROKEN, FailureReason.MARKER_MISSING),
        unreadable.evidence_id: (MarkerResult.BROKEN, FailureReason.METADATA_UNREADABLE),
        replaced.evidence_id: (MarkerResult.BROKEN, FailureReason.MARKER_UNVERIFIABLE),
    }
    for evidence_id, (result, _) in expected.items():
        row = evidence_row(environment, evidence_id)
        assert row["marker_verification_result"] == result.value
        assert row["marker_verified_at"] == sampled_at
        assert row["container_marker_sampled_at"] == sampled_at
        assert row["verification_method"] == "object_metadata"  # el de la escritura, intacto

    # El evento del pendiente nº 21, uno por clip roto, con su carga cerrada.
    failed = events(environment, organization_id, "evidence_marker_verification_failed")
    broken = {k: v for k, v in expected.items() if v[0] is MarkerResult.BROKEN}
    assert len(failed) == len(broken)
    for event in failed:
        payload = json.loads(event["payload"])
        EvidenceMarkerVerificationFailed.model_validate(payload, strict=True)
        evidence_id = uuid.UUID(payload["evidence_id"])
        row = evidence_row(environment, evidence_id)
        assert payload == {
            "evidence_id": str(evidence_id),
            "record_id": str(row["record_id"]),
            "organization_id": str(organization_id),
            "plant_id": str(place.plant_id),
            "zone_id": str(place.zone_id),
            "node_id": str(place.node_id),
            "clip_id": str(row["clip_id"]),
            "verification_method": "full_read",
            "container_marker_sampled_at": "2026-09-30T01:00:00.000Z",
            "failure_reason": broken[evidence_id][1].value,  # type: ignore[union-attr]
        }
        assert event["plant_id"] == place.plant_id
        # Sin texto libre: todo valor es un identificador, una enumeración o una marca.
        assert all(isinstance(value, str) and len(value) <= 36 for value in payload.values())

    alerts = [
        json.loads(e["payload"]) for e in events(environment, organization_id, "security_alert")
    ]
    assert sorted(a["resource_id"] for a in alerts) == sorted(str(k) for k in broken)
    assert {(a["alert_kind"], a["resource_kind"]) for a in alerts} == {
        ("evidence_marker_mismatch", "evidence")
    }

    # Queda en la auditoría, encadenada, con el motivo cerrado.
    checks = [
        e
        for e in audit_entries(environment, organization_id, "integrity_verification")
        if e["resource_kind"] == "evidence"
    ]
    assert sorted(e["resource_id"] for e in checks) == sorted(broken)
    assert all(e["outcome"] == "error" for e in checks)
    assert {json.loads(e["filters_json"])["failure_reason"] for e in checks} == {
        reason.value for _, reason in broken.values() if reason is not None
    }

    # Nunca se borra: cada objeto y cada versión siguen ahí, y el registro también.
    for key, versions in versions_before.items():
        assert object_versions(environment, key) == versions
        assert environment.run(environment.storage.head_object(key)) is not None

    # La marca de revisión se lee por lote.
    coordinator = person_context(
        organization_id,
        [AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.COORDINATOR_SST)],
        Role.COORDINATOR_SST,
    )
    results = environment.run(environment.service.resultados_marca(coordinator, list(expected)))
    assert {k: v.result for k, v in results.items()} == {k: v[0] for k, v in expected.items()}

    # Una segunda pasada del mismo día no cambia nada ni publica nada nuevo.
    run_task(environment, organization_id, environment.sampler())
    assert len(events(environment, organization_id, "evidence_marker_verification_failed")) == len(
        broken
    )
    assert len(events(environment, organization_id, "security_alert")) == len(broken)


# --- Criterio 3: selección reproducible con la semilla registrada ------------------------------


def _insert_evidences(
    environment: Environment, place: Place, count: int, *, day: date = DAY
) -> list[uuid.UUID]:
    """Evidencias de ``place`` sin objeto en el almacén, insertadas como superusuario."""

    async def insert() -> list[uuid.UUID]:
        connection = await environment.migrated.connect()
        try:
            # El disparador que reclama evidence_id corre como vigia_migrate, sujeto a la
            # seguridad a nivel de fila: necesita la organización del contexto.
            await connection.execute(
                "SELECT set_config('vigia.organization_id', $1, false)",
                str(place.organization_id),
            )
            ids = []
            for index in range(count):
                values = evidence_values(
                    place.organization_id,
                    place.plant_id,
                    uuid.uuid4(),
                    datetime.combine(day, datetime.min.time(), UTC) + timedelta(seconds=index),
                )
                values.update(
                    zone_id=place.zone_id,
                    node_id=place.node_id,
                    storage_key=place.storage_key(str(values["clip_id"])),
                )
                await insert_evidence(connection, values)
                ids.append(values["evidence_id"])
            return ids
        finally:
            await connection.close()

    ids: list[uuid.UUID] = environment.run(insert())
    return ids


def test_sample_selection_is_reproducible_with_the_recorded_seed(
    environment: Environment,
) -> None:
    place = Place.new()
    organization_id = place.organization_id
    population = _insert_evidences(environment, place, 1_050)
    # Un clip del día anterior y otro del siguiente no entran en la población.
    _insert_evidences(environment, place, 1, day=DAY - timedelta(days=1))
    _insert_evidences(environment, place, 1, day=DAY + timedelta(days=1))
    seed = bytes(range(32))
    sampler = environment.sampler(seed=seed)
    context = periodic_context(organization_id)

    outcome = environment.run(sampler.sample_day(context, DAY))

    assert sample_size(1_050) == 11  # 1 % redondeado hacia arriba (10,5 → 11)
    assert outcome.run.population == 1_050
    assert outcome.run.sample_size == 11
    assert outcome.run.seed == seed.hex()
    assert outcome.selected == select_sample(seed, population, 11)
    # Todas sin objeto en el almacén: la marca no se puede verificar, nada se borra.
    assert outcome.broken == dict.fromkeys(outcome.selected, FailureReason.MARKER_UNVERIFIABLE)

    # La semilla está registrada: en la tabla de la muestra y en la auditoría encadenada.
    runs = environment.run(
        _fetch(
            environment.migrated,
            "SELECT sample_day, seed, population, sample_size FROM ledger.evidence_sample_run"
            " WHERE organization_id = $1",
            organization_id,
        )
    )
    assert [tuple(r) for r in runs] == [(DAY, seed.hex(), 1_050, 11)]
    registered = [
        json.loads(e["filters_json"])
        for e in audit_entries(environment, organization_id, "integrity_verification")
        if e["resource_kind"] is None
    ]
    assert registered == [
        {
            "task": EVIDENCE_SAMPLE,
            "sample_day": DAY.isoformat(),
            "seed": seed.hex(),
            "population": 1_050,
            "sample_size": 11,
        }
    ]

    # Con la semilla registrada y la población se recalcula la misma muestra: son exactamente
    # las evidencias que la base marca como muestreadas.
    sampled = environment.run(
        _fetch(
            environment.migrated,
            "SELECT evidence_id FROM ledger.evidence WHERE organization_id = $1"
            " AND container_marker_sampled_at IS NOT NULL",
            organization_id,
        )
    )
    recorded_seed = bytes.fromhex(runs[0]["seed"])
    assert {r["evidence_id"] for r in sampled} == set(
        select_sample(recorded_seed, reversed(population), runs[0]["sample_size"])
    )

    # Otra pasada con otra semilla aleatoria reutiliza la registrada y no repite nada.
    again = environment.run(environment.sampler(seed=bytes(32)).sample_day(context, DAY))
    assert again.run.seed == seed.hex()
    assert again.selected == outcome.selected
    assert again.already_verified == outcome.selected
    assert again.intact == () and again.broken == {}


def test_small_population_is_sampled_whole_and_empty_day_registers_zero(
    environment: Environment,
) -> None:
    place = Place.new()
    ids = _insert_evidences(environment, place, 7)
    context = periodic_context(place.organization_id)
    outcome = environment.run(environment.sampler().sample_day(context, DAY))
    assert set(outcome.selected) == set(ids)
    empty = environment.run(environment.sampler().sample_day(context, DAY - timedelta(days=10)))
    assert empty.selected == () and empty.run.population == 0 and empty.run.sample_size == 0


# --- Criterio 4: transición única en la base y lectura por lote ---------------------------------


async def _update_as(
    migrated: MigratedDatabase,
    role: str | None,
    organization_id: uuid.UUID,
    statement: str,
    *args: Any,
) -> str:
    connection = await migrated.connect(role)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('vigia.organization_id', $1, true)", str(organization_id)
            )
            result: str = await connection.execute(statement, *args)
            return result
    finally:
        await connection.close()


def test_second_write_of_the_marker_result_fails_in_the_database(
    environment: Environment,
) -> None:
    place = Place.new()
    organization_id = place.organization_id
    (verified, pending) = _insert_evidences(environment, place, 2)
    at = datetime(2026, 9, 30, 1, tzinfo=UTC)
    first = (
        "UPDATE ledger.evidence SET marker_verification_result = $2, marker_verified_at = $3,"
        " container_marker_sampled_at = $3 WHERE evidence_id = $1"
    )
    # La primera transición, como vigia_app, pasa.
    assert (
        environment.run(
            _update_as(
                environment.migrated, "vigia_app", organization_id, first, verified, "intact", at
            )
        )
        == "UPDATE 1"
    )

    def rejected(role: str | None, statement: str, *args: Any) -> str:
        with pytest.raises(asyncpg.PostgresError) as caught:
            environment.run(
                _update_as(environment.migrated, role, organization_id, statement, *args)
            )
        return str(caught.value.sqlstate)

    for role in ("vigia_app", None):  # None: el superusuario del contenedor
        # Segunda escritura sobre una evidencia ya verificada.
        assert rejected(role, first, verified, "broken", at) == RESTRICT_VIOLATION
        assert rejected(role, first, verified, "intact", at + timedelta(days=1)) == (
            RESTRICT_VIOLATION
        )
        # Volver a pending, o cambiar solo la marca de tiempo.
        assert (
            rejected(
                role,
                "UPDATE ledger.evidence SET marker_verification_result = 'pending',"
                " marker_verified_at = NULL, container_marker_sampled_at = NULL"
                " WHERE evidence_id = $1",
                verified,
            )
            == RESTRICT_VIOLATION
        )
        # Borrar nunca: vigia_app no tiene el privilegio y al superusuario lo para el disparador.
        assert rejected(role, "DELETE FROM ledger.evidence WHERE evidence_id = $1", verified) == (
            INSUFFICIENT_PRIVILEGE if role == "vigia_app" else RESTRICT_VIOLATION
        )
    # Sobre una pendiente: la transición con otra columna cambiada no pasa…
    assert (
        rejected(
            None,
            "UPDATE ledger.evidence SET marker_verification_result = 'broken',"
            " marker_verified_at = $2, container_marker_sampled_at = $2, sha256 = $3"
            " WHERE evidence_id = $1",
            pending,
            at,
            "b" * 64,
        )
        == RESTRICT_VIOLATION
    )
    # …ni sin sus marcas (el disparador y la restricción la rechazan)…
    assert rejected(
        None,
        "UPDATE ledger.evidence SET marker_verification_result = 'intact' WHERE evidence_id = $1",
        pending,
    ) in {RESTRICT_VIOLATION, "23514"}
    # …y vigia_app no puede tocar ninguna otra columna (privilegio por columna).
    assert (
        rejected(
            "vigia_app",
            "UPDATE ledger.evidence SET sha256 = $2 WHERE evidence_id = $1",
            pending,
            "c" * 64,
        )
        == INSUFFICIENT_PRIVILEGE
    )
    row = evidence_row(environment, verified)
    assert (row["marker_verification_result"], row["marker_verified_at"]) == ("intact", at)
    assert evidence_row(environment, pending)["marker_verification_result"] == "pending"


@dataclass
class CountingDatabase:
    """Envuelve la base de la prueba y cuenta lecturas y transacciones."""

    inner: Any
    reads: int = 0
    transactions: int = 0

    async def read(
        self,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Row[Any]]:
        self.reads += 1
        rows: Sequence[Row[Any]] = await self.inner.read(context, statement, parameters)
        return rows

    def transaction(self, context: ScopeContext) -> Any:
        self.transactions += 1
        return self.inner.transaction(context)


def test_batch_marker_results_of_500_evidences_is_one_query(environment: Environment) -> None:
    place = Place.new()
    organization_id = place.organization_id
    ids = _insert_evidences(environment, place, 500)
    stranger = Place.new()
    (foreign,) = _insert_evidences(environment, stranger, 1)
    counting = CountingDatabase(environment.env.database)
    service = EvidenceService(
        database=counting,  # type: ignore[arg-type]
        audit=environment.env.audit,
        storage=environment.storage,
    )
    context = person_context(
        organization_id,
        [AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.COORDINATOR_SST)],
        Role.COORDINATOR_SST,
    )

    results = environment.run(service.resultados_marca(context, [*ids, foreign, *ids[:10]]))

    assert (counting.reads, counting.transactions) == (1, 0)
    assert set(results) == set(ids)  # la de otra organización no aparece
    assert {v.result for v in results.values()} == {MarkerResult.PENDING}
    assert all(v.marker_verified_at is None for v in results.values())

    # Con alcance de zona solo ve su zona; un lote vacío no consulta.
    other_zone = person_context(
        organization_id,
        [AllowedScope(ScopeLevel.ZONE, uuid.uuid4(), Role.COORDINATOR_SST)],
        Role.COORDINATOR_SST,
    )
    assert environment.run(service.resultados_marca(other_zone, ids)) == {}
    assert environment.run(service.resultados_marca(context, [])) == {}
    assert counting.reads == 2


# --- Fallos a mitad de operación ----------------------------------------------------------------


class FlakyStorage:
    """``S3Storage`` real salvo para las claves de ``down``: ``StorageUnavailable``."""

    def __init__(self, inner: S3Storage, down: set[str], *, at: str = "get") -> None:
        self._inner = inner
        self.down = down
        self._at = at

    async def head_object(self, key: str) -> ObjectHead | None:
        if key in self.down and self._at == "head":
            raise StorageUnavailable("head_object")
        return await self._inner.head_object(key)

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        if key in self.down and self._at == "get":
            raise StorageUnavailable("get_object")
        return await self._inner.get_object(key, version_id=version_id)


@pytest.mark.parametrize("at", ["head", "get"])
def test_storage_down_mid_sample_leaves_the_clip_pending_and_the_retry_completes(
    environment: Environment, at: str
) -> None:
    place = Place.new()
    organization_id = place.organization_id
    clips = register_clips(
        environment,
        place,
        [synthetic_clip("a"), synthetic_clip("b", marked=False), synthetic_clip("c")],
    )
    on_next_day(environment)
    flaky = FlakyStorage(environment.storage, {clips[1].storage_key}, at=at)

    with pytest.raises(StorageUnavailable):
        run_task(environment, organization_id, environment.sampler(storage=flaky))
    assert evidence_row(environment, clips[1].evidence_id)["marker_verification_result"] == (
        "pending"
    )
    assert {
        evidence_row(environment, c.evidence_id)["marker_verification_result"]
        for c in (clips[0], clips[2])
    } == {"intact"}
    assert events(environment, organization_id, "evidence_marker_verification_failed") == []

    flaky.down.clear()
    run_task(environment, organization_id, environment.sampler(storage=flaky))
    assert evidence_row(environment, clips[1].evidence_id)["marker_verification_result"] == (
        "broken"
    )
    assert len(events(environment, organization_id, "evidence_marker_verification_failed")) == 1


RECORD_FAULTS = (
    Fault(statement=1),  # la transición
    Fault(statement=2),  # evidence_marker_verification_failed
    Fault(statement=3),  # security_alert
    Fault(statement=4),  # la entrada de auditoría
    Fault(commit=True),
)


@pytest.mark.parametrize("fault", RECORD_FAULTS)
def test_failure_while_recording_a_broken_mark_leaves_nothing_behind(
    environment: Environment, fault: Fault
) -> None:
    place = Place.new()
    organization_id = place.organization_id
    (clip,) = register_clips(environment, place, [synthetic_clip("roto", marked=False)])
    on_next_day(environment)
    sampler = environment.sampler()
    context = periodic_context(organization_id)
    original = sampler._record

    async def record_with_fault(*args: Any) -> bool:
        # El fallo cae en la transacción del resultado, no en la de la semilla.
        environment.env.database.next_fault = fault
        return await original(*args)

    sampler._record = record_with_fault  # type: ignore[method-assign]
    with pytest.raises(Exception):  # noqa: B017 - el fallo inyectado o su traducción
        environment.run(sampler.sample_day(context, DAY))
    environment.env.database.next_fault = None
    assert evidence_row(environment, clip.evidence_id)["marker_verification_result"] == "pending"
    assert events(environment, organization_id, "evidence_marker_verification_failed") == []
    assert events(environment, organization_id, "security_alert") == []
    assert [
        e
        for e in audit_entries(environment, organization_id, "integrity_verification")
        if e["resource_kind"] == "evidence"
    ] == []

    # La pasada siguiente lo completa una sola vez.
    run_task(environment, organization_id, environment.sampler())
    assert evidence_row(environment, clip.evidence_id)["marker_verification_result"] == "broken"
    assert len(events(environment, organization_id, "evidence_marker_verification_failed")) == 1
    assert len(events(environment, organization_id, "security_alert")) == 1


class SwappingStorage:
    """``HEAD`` real, pero la descarga devuelve otros bytes (marcados): una versión cambiada
    entre la consulta y la descarga, o un almacén que miente."""

    def __init__(self, inner: S3Storage) -> None:
        self._inner = inner

    async def head_object(self, key: str) -> ObjectHead | None:
        return await self._inner.head_object(key)

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        # Mismo tamaño y misma marca; solo cambia el contenido del mdat.
        content = await self._inner.get_object(key, version_id=version_id)
        return content.replace(b"verificado", b"VERIFICADO")


def test_downloaded_bytes_that_are_not_the_verified_ones_are_unverifiable(
    environment: Environment,
) -> None:
    place = Place.new()
    (clip,) = register_clips(environment, place, [synthetic_clip("verificado")])
    on_next_day(environment)
    sampler = environment.sampler(storage=SwappingStorage(environment.storage))
    outcome = environment.run(sampler.sample_day(periodic_context(place.organization_id)))
    # Los bytes descargados llevan la marca, pero no son los del expediente: no cuenta.
    assert outcome.broken == {clip.evidence_id: FailureReason.MARKER_UNVERIFIABLE}
    assert evidence_row(environment, clip.evidence_id)["marker_verification_result"] == "broken"


def test_head_object_checksum_matches_what_the_node_uploaded(environment: Environment) -> None:
    """Sanidad del entorno: LocalStack guarda la suma y el metadato con que sube el nodo."""
    content = synthetic_clip("sanidad")
    key = Place.new().storage_key(str(uuid7()))
    upload(environment, key, content)
    head = environment.run(environment.storage.head_object(key))
    assert head is not None
    assert head.full_object_sha256_hex == hashlib.sha256(content).hexdigest()
    assert head.metadata[ANONYMIZED_METADATA_KEY] == "1"
    assert base64.b64decode(head.checksum_sha256 or "") == hashlib.sha256(content).digest()
