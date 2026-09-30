"""Entorno de prueba de ``EscritorExpediente`` y ``AuditWriter`` (TASK-113).

Lo comparten ``tests/properties/test_ledger_chain_stateful.py``,
``tests/properties/test_writer_idempotency.py`` y ``tests/properties/test_writer_atomicity.py``:

- **Tipos de prueba** registrados en un ``RecordTypeRegistry`` y sincronizados en
  ``ledger.record_type``: ``zone_created`` y ``organization_created`` (los de U-02),
  ``finding_received`` (U-03, ``FindingSubmission`` del contrato, clave ``/finding_id`` y clips de
  evidencia; contenido de los generadores del kit de U-01), ``order_probe`` (U-03: organización,
  clave, texto libre, clips y un evento: pasa por los siete pasos) y ``classification_probe``
  (U-04, con ``label_rule`` y un evento).
- ``TransactionProbe`` y ``ProbedDatabase``: el adaptador ``shared.db`` real como ``vigia_app``,
  envuelto para saber cuándo hay una transacción de escritura abierta y para inyectar fallos en la
  n-ésima sentencia o en el ``COMMIT``.
- ``InstrumentedStorage``: almacén en memoria (``head_object``) que cuenta cada llamada hecha con
  una transacción abierta.
- El **oráculo de cadenas**: ``verify_ledger_chains`` y ``verify_audit_chain`` releen como
  superusuario las filas de una organización y comprueban secuencias contiguas, marcas no
  decrecientes, ``content_hash`` y ``record_hash`` (o ``entry_hash``) recalculados en Python y la
  cabeza de cada cadena.

Solo datos generados: los clips son bytes aleatorios que nunca se suben (solo sus metadatos).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, StrictStr
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable
from vigia_contracts.models.clip_reference import ClipReference
from vigia_contracts.models.common import UUID
from vigia_contracts.models.finding import FindingSubmission

from tests.factories import uuid7
from tests.identity_db import MigratedDatabase
from tests.ledger_database import (
    DatabaseLoop,
    audit_envelope,
    chain_hash,
    genesis_hash,
    record_envelope,
)
from tests.outbox_support import (
    PROBE_EVENT,
    SOLO_EVENT,
    InjectedFault,
    StatementFaults,
    app_database,
    probe_catalog,
)
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import (
    ChainLevel,
    ContentModel,
    LabelRule,
    RecordType,
    RecordTypeRegistry,
)
from vigia_platform.shared.clock import SimulatedClock
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
from vigia_platform.shared.db import ConnectionPort, Database, PoolPort, Transaction
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.storage import (
    ANONYMIZED_METADATA_KEY,
    ChecksumType,
    ObjectHead,
    StorageUnavailable,
)

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)

FINDING_TYPE = "finding_received"
ORDER_TYPE = "order_probe"
CLASSIFICATION_TYPE = "classification_probe"
ZONE_TYPE = "zone_created"
ORGANIZATION_TYPE = "organization_created"

FINDING_FREE_TEXT = (
    "/contract_version",
    "/software_version",
    "/node_time/clock/source",
    "/cameras[*]/clips[*]/storage_key",
)
"""Rutas del ``FindingSubmission`` que el registro cuenta como texto libre (VIG-40)."""

SnakeCode = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
]


class OrderProbe(ContentModel):
    """Tipo sintético que atraviesa los siete pasos del orden fijo."""

    probe_id: UUID
    organization_id: UUID
    plant_id: UUID
    zone_id: UUID
    node_id: UUID
    level: Annotated[StrictInt, Field(ge=0, le=1000)]
    note: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    clips: Annotated[tuple[ClipReference, ...], Field(min_length=0, max_length=3)] = ()


class Signer(ContentModel):
    user_id: UUID
    role: Literal["coordinator_sst", "copasst"]


class ClassificationProbe(ContentModel):
    """Forma mínima de ``classification`` de U-04 (§5.3) para proyectar una etiqueta."""

    classification_id: UUID
    plant_id: UUID
    zone_id: UUID
    anchor_record_id: UUID
    family: Literal["dwell", "coexistence", "startup_transition", "guard_bypass"]
    outcome: Literal["confirmed", "authorized_operation", "false_positive"]
    reason_category: SnakeCode
    signer: Signer


LABEL_RULE = LabelRule(
    subject_record_path="/anchor_record_id",
    family_path="/family",
    outcome_path="/outcome",
    reason_category_path="/reason_category",
    labeled_by_path="/signer",
)

PROBE_TYPES = (
    RecordType(
        record_type=FINDING_TYPE,
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=FindingSubmission,
        source_key_path="/finding_id",
        free_text_paths=FINDING_FREE_TEXT,
        evidence_paths=("/cameras[*]/clips[*]",),
    ),
    RecordType(
        record_type=ORDER_TYPE,
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=OrderProbe,
        source_key_path="/probe_id",
        free_text_paths=("/note", "/clips[*]/storage_key"),
        evidence_paths=("/clips[*]",),
        outbox_events=(PROBE_EVENT,),
    ),
    RecordType(
        record_type=CLASSIFICATION_TYPE,
        writer_unit=ActorUnit.U04,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=ClassificationProbe,
        source_key_path="/classification_id",
        label_rule=LABEL_RULE,
        outbox_events=(SOLO_EVENT,),
    ),
)


def build_registry(extra_types: Sequence[RecordType] = ()) -> RecordTypeRegistry:
    registry = RecordTypeRegistry()
    for definition in (*U02_RECORD_TYPES, *PROBE_TYPES, *extra_types):
        registry.register(definition)
    return registry


_SAVE_RECORD_TYPE = text(
    "INSERT INTO ledger.record_type (record_type, writer_unit, chain_level, schema_version,"
    " content_schema, source_key_path, free_text_paths, evidence_paths, label_rule,"
    " outbox_events) VALUES (:record_type, :writer_unit, :chain_level, :schema_version,"
    " CAST(:content_schema AS jsonb), :source_key_path, CAST(:free_text_paths AS text[]),"
    " CAST(:evidence_paths AS text[]), CAST(:label_rule AS jsonb),"
    " CAST(:outbox_events AS text[]))"
)


# --- Contextos --------------------------------------------------------------------------------


def unit_context(
    organization_id: uuid.UUID,
    unit: ActorUnit,
    *,
    kind: ActorKind = ActorKind.USER,
    display_name: str = "Coordinación SST sintética",
    role: Role | None = Role.COORDINATOR_SST,
    concession_id: uuid.UUID | None = None,
) -> ScopeContext:
    """Contexto de ``organization_id`` con un actor de la unidad ``unit``."""
    origin = {
        ActorKind.USER: ContextOrigin.SESSION,
        ActorKind.PROVIDER_USER: ContextOrigin.SESSION,
        ActorKind.NODE: ContextOrigin.SESSION,
        ActorKind.SYSTEM: ContextOrigin.OUTBOX_EVENT,
        ActorKind.OPERATOR: ContextOrigin.ADMIN_COMMAND,
    }[kind]
    if kind is ActorKind.PROVIDER_USER:
        concession_id = concession_id or uuid.uuid4()
    actor = Actor(
        kind=kind,
        id=uuid.uuid4(),
        display_name_snapshot=display_name,
        unit=unit,
        role_in_use=role if kind in (ActorKind.USER, ActorKind.PROVIDER_USER) else None,
        concession_id=concession_id,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=origin,
        allowed_scopes=[
            AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.COORDINATOR_SST)
        ],
        correlation_id=uuid7(),
        session_id_hash=hashlib.sha256(os.urandom(8)).hexdigest()
        if origin is ContextOrigin.SESSION
        else None,
    )


# --- Base instrumentada -----------------------------------------------------------------------


@dataclass
class Fault:
    """Fallo inyectado en la próxima transacción: la sentencia ``statement`` o el ``COMMIT``."""

    statement: int | None = None
    commit: bool = False


_inside_transaction: ContextVar[bool] = ContextVar("writer_inside_transaction", default=False)
"""Si la tarea en curso (y las que crea) tiene abierta su transacción de escritura."""


@dataclass
class TransactionProbe:
    """Cuántas transacciones de escritura están abiertas y cuántas se abrieron.

    ``inside()`` responde por la **escritura en curso**: con escrituras concurrentes, otra puede
    tener su transacción abierta mientras esta consulta el almacén, y eso es correcto.
    """

    open: int = 0
    opened: int = 0
    statements: list[int] = field(default_factory=list)

    @staticmethod
    def inside() -> bool:
        return _inside_transaction.get()


class _ArmedConnection:
    """Conexión del pool cuyo ``COMMIT`` falla una vez si la sonda lo pide."""

    def __init__(self, connection: ConnectionPort, owner: ProbedDatabase) -> None:
        self._connection = connection
        self._owner = owner

    async def commit(self) -> None:
        if self._owner.fail_next_commit:
            self._owner.fail_next_commit = False
            raise InjectedFault("fallo inyectado en el COMMIT")
        await self._connection.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ArmedPool:
    def __init__(self, pool: PoolPort, owner: ProbedDatabase) -> None:
        self._pool = pool
        self._owner = owner

    async def acquire(self) -> ConnectionPort:
        return _ArmedConnection(await self._pool.acquire(), self._owner)  # type: ignore[return-value]

    async def dispose(self) -> None:
        await self._pool.dispose()


class ProbedDatabase:
    """``shared.db.Database`` con sonda de transacción e inyección de fallos."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.probe = TransactionProbe()
        self.next_fault: Fault | None = None
        self.fail_next_commit = False
        pools = database._pools
        for pool_class, pool in list(pools.items()):
            pools[pool_class] = _ArmedPool(pool, self)

    def transaction(self, context: ScopeContext) -> contextlib.AbstractAsyncContextManager[Any]:
        return self._transaction(context)

    @contextlib.asynccontextmanager
    async def _transaction(self, context: ScopeContext) -> AsyncIterator[Transaction]:
        fault, self.next_fault = self.next_fault, None
        self.fail_next_commit = fault is not None and fault.commit
        self.probe.open += 1
        self.probe.opened += 1
        token = _inside_transaction.set(True)
        counter: StatementFaults | None = None
        try:
            async with self.database.transaction(context) as transaction:
                counter = StatementFaults.install(
                    transaction, None if fault is None else fault.statement
                )
                yield transaction
        finally:
            _inside_transaction.reset(token)
            self.fail_next_commit = False
            self.probe.open -= 1
            if counter is not None:
                self.probe.statements.append(counter.statements)

    async def read(
        self,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Row[Any]]:
        return await self.database.read(context, statement, parameters)


# --- Almacén instrumentado --------------------------------------------------------------------


class InstrumentedStorage:
    """``head_object`` en memoria que registra si se llamó con una transacción abierta."""

    def __init__(self, probe: TransactionProbe) -> None:
        self._probe = probe
        self.objects: dict[str, ObjectHead] = {}
        self.calls = 0
        self.calls_in_transaction = 0
        self.unavailable = False

    async def head_object(self, key: str) -> ObjectHead | None:
        self.calls += 1
        if self._probe.inside():
            self.calls_in_transaction += 1
        if self.unavailable:
            try:
                raise ConnectionError(f"fallo de red sintético en {key}")
            except ConnectionError as error:
                raise StorageUnavailable("head_object") from error
        return self.objects.get(key)

    def put(
        self,
        reference: Mapping[str, Any],
        *,
        size_bytes: int | None = None,
        sha256: str | None = None,
        marker: str | None = "1",
    ) -> None:
        """Deja en el almacén el objeto que ``reference`` describe (o uno alterado)."""
        digest = bytes.fromhex(sha256 or str(reference["sha256"]))
        self.objects[str(reference["storage_key"])] = ObjectHead(
            key=str(reference["storage_key"]),
            size_bytes=int(reference["size_bytes"]) if size_bytes is None else size_bytes,
            checksum_sha256=base64.b64encode(digest).decode(),
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type=str(reference["content_type"]),
            metadata={} if marker is None else {ANONYMIZED_METADATA_KEY: marker},
            version_id="v1",
        )


# --- Entorno ----------------------------------------------------------------------------------


@dataclass
class WriterEnvironment:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    database: ProbedDatabase
    storage: InstrumentedStorage
    registry: RecordTypeRegistry
    writer: EscritorExpediente
    audit: AuditWriter
    clock: SimulatedClock
    provider_organization_id: uuid.UUID


@contextlib.contextmanager
def writer_environment(
    migrated: MigratedDatabase,
    *,
    pool_size: int = 6,
    lock_timeout_ms: int = 2_000,
    extra_types: Sequence[RecordType] = (),
) -> Iterator[WriterEnvironment]:
    """Escritor sobre la base migrada, como ``vigia_app``, con los tipos de prueba sincronizados.

    ``extra_types`` añade tipos al registro (p. ej. los de U-03 que lee la cobertura).
    """
    loop = DatabaseLoop()
    database = app_database(migrated, worker_pool_size=pool_size, lock_timeout_ms=lock_timeout_ms)
    probed = ProbedDatabase(database)
    clock = SimulatedClock(NOW)
    registry = build_registry(extra_types)
    catalog = probe_catalog()
    system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)

    async def synchronize() -> None:
        async with database.transaction(system) as transaction:
            await catalog.synchronize(SqlOutboxCatalogStore(transaction), clock)
            for compiled in registry.latest():
                row = compiled.to_persisted()
                await transaction.execute(
                    _SAVE_RECORD_TYPE,
                    {
                        "record_type": row.record_type,
                        "writer_unit": row.writer_unit,
                        "chain_level": row.chain_level,
                        "schema_version": row.schema_version,
                        "content_schema": json.dumps(row.content_schema),
                        "source_key_path": row.source_key_path,
                        "free_text_paths": list(row.free_text_paths),
                        "evidence_paths": list(row.evidence_paths),
                        "label_rule": None
                        if row.label_rule is None
                        else json.dumps(row.label_rule),
                        "outbox_events": list(row.outbox_events),
                    },
                )
        registry.seal()

    storage = InstrumentedStorage(probed.probe)
    provider = uuid.uuid4()
    writer = EscritorExpediente(
        database=probed,
        registry=registry,
        free_text=FreeTextPolicyRegistry(),
        evidence=EvidenceVerifier(storage, clock),
        outbox=Outbox(catalog, clock),
        clock=clock,
    )
    audit = AuditWriter(database=probed, clock=clock, provider_organization_id=provider)
    try:
        loop.run(synchronize())
        yield WriterEnvironment(
            loop, migrated, probed, storage, registry, writer, audit, clock, provider
        )
    finally:
        loop.run(database.dispose())
        loop.close()


# --- Contenidos -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    """Una planta de una organización, con una zona y un nodo."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID

    @classmethod
    def new(cls, organization_id: uuid.UUID | None = None) -> Place:
        return cls(organization_id or uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    def storage_key(self, clip_id: str, ext: str = "mp4") -> str:
        return (
            f"org/{self.organization_id}/plant/{self.plant_id}/zone/{self.zone_id}"
            f"/node/{self.node_id}/{clip_id}.{ext}"
        )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def clip_document(place: Place, *, offset_ms: int = 0) -> dict[str, Any]:
    """Una ``ClipReference`` de video coherente con ``place`` (bytes aleatorios, no subidos)."""
    clip_id = str(uuid7())
    starts = NOW + timedelta(milliseconds=offset_ms)
    return {
        "clip_id": clip_id,
        "camera_id": str(uuid.uuid4()),
        "media_kind": "video",
        "content_type": "video/mp4",
        "sha256": hashlib.sha256(os.urandom(16)).hexdigest(),
        "size_bytes": 1 + int.from_bytes(os.urandom(2), "big"),
        "duration_ms": 10_000,
        "starts_at": _stamp(starts),
        "ends_at": _stamp(starts + timedelta(seconds=10)),
        "segment": "full",
        "anonymized": True,
        "storage_key": place.storage_key(clip_id),
    }


def order_document(
    place: Place, *, probe_id: uuid.UUID | None = None, level: int = 1, clips: int = 1
) -> dict[str, Any]:
    return {
        "probe_id": str(probe_id or uuid7()),
        "organization_id": str(place.organization_id),
        "plant_id": str(place.plant_id),
        "zone_id": str(place.zone_id),
        "node_id": str(place.node_id),
        "level": level,
        "note": "Revisión de la guarda norte",
        "clips": [clip_document(place, offset_ms=index * 1000) for index in range(clips)],
    }


def zone_document(place: Place, name: str = "Zona de prensas") -> dict[str, Any]:
    return {
        "zone_id": str(uuid.uuid4()),
        "plant_id": str(place.plant_id),
        "code": "Z-01",
        "name": name,
        "created_by": str(uuid.uuid4()),
    }


def organization_document(organization_id: uuid.UUID) -> dict[str, Any]:
    return {
        "organization_id": str(organization_id),
        "code": "ORG-01",
        "name": "Organización sintética",
        "kind": "client",
        "concession_max_days": 30,
        "concession_default_days": 7,
        "created_by": str(uuid.uuid4()),
    }


def localize_finding(document: Mapping[str, Any], place: Place) -> dict[str, Any]:
    """Un ``Finding`` del kit llevado a ``place``: identificadores y claves de sus clips."""
    finding = json.loads(json.dumps(document))
    finding["finding_id"] = str(uuid7())
    for name in ("organization_id", "plant_id", "zone_id", "node_id"):
        finding[name] = str(getattr(place, name))
    for camera in finding["cameras"]:
        for clip in camera["clips"]:
            # El kit repite identificadores entre ejemplos; cada clip real tiene su propia clave.
            clip["clip_id"] = str(uuid7())
            clip["storage_key"] = place.storage_key(clip["clip_id"])
    return finding


def clips_of(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if "cameras" in document:
        return [clip for camera in document["cameras"] for clip in camera["clips"]]
    return list(document.get("clips", ()))


def classification_document(
    place: Place, anchor_record_id: uuid.UUID, *, outcome: str = "confirmed"
) -> dict[str, Any]:
    return {
        "classification_id": str(uuid7()),
        "plant_id": str(place.plant_id),
        "zone_id": str(place.zone_id),
        "anchor_record_id": str(anchor_record_id),
        "family": "coexistence",
        "outcome": outcome,
        "reason_category": "guard_open",
        "signer": {"user_id": str(uuid.uuid4()), "role": "coordinator_sst"},
    }


# --- Conteos y oráculo -------------------------------------------------------------------------


async def _superuser(migrated: MigratedDatabase) -> Any:
    return await migrated.connect()


async def organization_counts(
    migrated: MigratedDatabase, organization_id: uuid.UUID
) -> Counter[str]:
    """Filas de la organización en cada tabla que toca una escritura (sin seguridad de fila)."""
    connection = await _superuser(migrated)
    try:
        counts: Counter[str] = Counter()
        for table in (
            "ledger.ledger_record",
            "ledger.record_source_key",
            "ledger.record_identity",
            "ledger.evidence",
            "ledger.label",
            "shared.outbox_event",
            "shared.outbox_delivery",
            "shared.audit_entry",
        ):
            counts[table] = int(
                await connection.fetchval(
                    f"SELECT count(*) FROM {table} WHERE organization_id = $1",  # noqa: S608
                    organization_id,
                )
            )
        heads = await connection.fetch(
            "SELECT kind, plant_id, last_sequence FROM ledger.chain_head"
            " WHERE organization_id = $1",
            organization_id,
        )
        for head in heads:
            counts[f"head:{head['kind']}:{head['plant_id']}"] = int(head["last_sequence"])
        return counts
    finally:
        await connection.close()


async def fetch_record(migrated: MigratedDatabase, record_id: uuid.UUID) -> Any:
    connection = await _superuser(migrated)
    try:
        return await connection.fetchrow(
            "SELECT * FROM ledger.ledger_record WHERE record_id = $1", record_id
        )
    finally:
        await connection.close()


async def verify_ledger_chains(
    migrated: MigratedDatabase, organization_id: uuid.UUID
) -> dict[uuid.UUID | None, int]:
    """PR-NUC-13 sobre todas las cadenas del expediente de la organización; longitud por cadena."""
    connection = await _superuser(migrated)
    try:
        rows = await connection.fetch(
            "SELECT * FROM ledger.ledger_record WHERE organization_id = $1"
            " ORDER BY plant_id NULLS FIRST, chain_sequence",
            organization_id,
        )
        heads = {
            head["plant_id"]: head
            for head in await connection.fetch(
                "SELECT * FROM ledger.chain_head WHERE organization_id = $1 AND kind = 'ledger'",
                organization_id,
            )
        }
    finally:
        await connection.close()
    chains: dict[uuid.UUID | None, list[Any]] = {}
    for row in rows:
        chains.setdefault(row["plant_id"], []).append(row)
    lengths: dict[uuid.UUID | None, int] = {}
    for plant_id, chain in chains.items():
        previous = genesis_hash(organization_id, plant_id)
        last_received: datetime | None = None
        for expected, row in enumerate(chain, start=1):
            assert row["chain_sequence"] == expected, (plant_id, expected, row["chain_sequence"])
            assert row["previous_hash"] == previous
            if last_received is not None:
                assert row["received_at"] >= last_received
            last_received = row["received_at"]
            assert row["content_hash"] == hashlib.sha256(bytes(row["content"])).hexdigest()
            assert row["record_hash"] == chain_hash(record_envelope(row), previous)
            previous = row["record_hash"]
        head = heads[plant_id]
        assert head["last_sequence"] == len(chain)
        assert head["last_hash"] == previous
        lengths[plant_id] = len(chain)
    assert set(heads) == set(chains)
    return lengths


async def verify_audit_chain(migrated: MigratedDatabase, organization_id: uuid.UUID) -> int:
    """La misma propiedad sobre la cadena de auditoría de la organización (RNF-MAN-07)."""
    connection = await _superuser(migrated)
    try:
        rows = await connection.fetch(
            "SELECT * FROM shared.audit_entry WHERE organization_id = $1 ORDER BY chain_sequence",
            organization_id,
        )
        head = await connection.fetchrow(
            "SELECT * FROM ledger.chain_head WHERE organization_id = $1 AND kind = 'audit'",
            organization_id,
        )
    finally:
        await connection.close()
    previous = genesis_hash(organization_id, None)
    last: datetime | None = None
    for expected, row in enumerate(rows, start=1):
        assert row["chain_sequence"] == expected
        assert row["previous_hash"] == previous
        if last is not None:
            assert row["occurred_at"] >= last
        last = row["occurred_at"]
        assert row["entry_hash"] == chain_hash(audit_envelope(row), previous)
        previous = row["entry_hash"]
    if rows:
        assert head is not None
        assert head["last_sequence"] == len(rows)
        assert head["last_hash"] == previous
    return len(rows)
