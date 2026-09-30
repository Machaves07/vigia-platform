"""La tarea ``write_checkpoints`` contra PostgreSQL 16 real (TASK-117, LC-NUC-14; BR-NUC-45, 53).

Base migrada hasta ``nuc_0005`` y escritor, auditoría y bandeja reales como ``vigia_app`` (nunca
superusuario). La firma es la de ``SigningService`` con sus dobles en memoria (claves generadas).

- **Criterio 2**: la tarea, ejecutada dos veces seguidas sobre la misma cabeza, escribe un solo
  punto de control por cadena (expediente de planta, de organización y auditoría) y un solo
  ``checkpoint_written`` por cadena; las cadenas siguen íntegras con el oráculo de PR-NUC-13 y con
  el recorrido del verificador de paquetes (``chain_walk``) y las claves publicadas.
- **Planta** (BR-NUC-45, ``nuc_0005``): el punto de control entra en la cadena de la planta; un
  tipo cuyo nivel en ``ledger.record_type`` no concuerda con el registro sale como
  ``content_invalid`` en ``/plant_id``, no como un ``23514`` sin traducir.
- **Guarda del disparador**: un punto de control que no cubre exactamente la cabeza no entra
  (``ledger_record_checkpoint_coverage``), ni en el expediente ni en la auditoría; si otra
  escritura se adelanta entre la lectura de la cabeza y el ``INSERT``, el servicio relee y
  reintenta.
- **Fallos a mitad de operación**: un fallo en cualquier sentencia o en el ``COMMIT`` no deja
  punto de control, registro ni evento; la pasada siguiente lo escribe una sola vez.
- **PR-NUC-20 con el verificador**: con un punto de control ``c1`` escrito por la tarea y otro
  ``c2`` posterior, el recorrido con ``c1`` como ancla pasa sobre la cadena real y falla sobre una
  copia cuyo prefijo se reescribió recalculando todos los hashes y volviendo a firmar.

Solo datos generados.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

import pytest
from pydantic import Field, StrictInt
from sqlalchemy import exc as sa_exc
from vigia_contracts.canonical import canonicalize
from vigia_contracts.models.common import UUID

from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.signing_support import SigningWorld, bootstrapped_world
from tests.verifier_packages import row_to_entry
from tests.writer_support import (
    Fault,
    Place,
    WriterEnvironment,
    organization_counts,
    organization_document,
    unit_context,
    verify_audit_chain,
    verify_ledger_chains,
    writer_environment,
    zone_document,
)
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.audit_writer import AuditOperation
from vigia_platform.ledger.application.checkpoint_task import (
    register_write_checkpoints,
    write_checkpoints_handler,
)
from vigia_platform.ledger.application.writer import (
    CHECK_VIOLATION,
    CHECKPOINT_COVERAGE_CONSTRAINT,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
    violated_constraint,
)
from vigia_platform.ledger.chain.chain_walk import ChainRef, ChainWalker, genesis_hash
from vigia_platform.ledger.chain.checkpoints import (
    ChainKind,
    CheckpointChain,
    CheckpointContent,
    CheckpointCoverageConflict,
    CheckpointOutcome,
    CheckpointService,
    CheckpointWriteFailed,
    StoredCheckpoint,
    signed_payload,
)
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry
from vigia_platform.shared.signing import SigningPurpose

pytestmark = pytest.mark.integration

SCOPE_PROBE = "scope_probe"


class ScopeProbe(ContentModel):
    """Tipo de organización que sigue al alcance, como ``provider_query``."""

    probe_id: UUID
    level: Annotated[StrictInt, Field(ge=0, le=100)]


PROBE_TYPES = (
    RecordType(
        record_type=SCOPE_PROBE,
        writer_unit=ActorUnit.U02,
        chain_level=ChainLevel.ORGANIZATION,
        schema_version=1,
        content_model=ScopeProbe,
        chain_follows_scope=True,
    ),
)


@dataclass
class Environment:
    env: WriterEnvironment
    world: SigningWorld
    store: SqlCheckpointStore
    service: CheckpointService


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[Environment]:
    with (
        migrated_database(postgres_endpoint, "vigia_checkpoints") as migrated,
        writer_environment(migrated, extra_types=PROBE_TYPES) as env,
    ):
        world = env.loop.run(bootstrapped_world())
        assert env.outbox is not None
        store = SqlCheckpointStore(
            database=env.database, writer=env.writer, audit=env.audit, outbox=env.outbox
        )
        service = CheckpointService(store=store, signer=world.service, clock=world.clock)
        yield Environment(env, world, store, service)


def system_context(organization_id: uuid.UUID) -> ScopeContext:
    return unit_context(organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)


def write(env: WriterEnvironment, context: ScopeContext, record_type: str, content: Any) -> Receipt:
    result = env.loop.run(env.writer.write(context, record_type, content))
    assert isinstance(result, Receipt), result
    return result


def audit(env: WriterEnvironment, context: ScopeContext, count: int = 1) -> None:
    for _ in range(count):
        env.loop.run(env.audit.append(context, AuditOperation.LEDGER_READ, result_count=1))


def populate(env: WriterEnvironment, place: Place) -> ScopeContext:
    """Registros en las tres cadenas de la organización de ``place``."""
    context = system_context(place.organization_id)
    write(env, context, "organization_created", organization_document(place.organization_id))
    for name in ("Zona de prensas", "Zona de hornos"):
        write(env, context, "zone_created", zone_document(place, name))
    audit(env, context, 2)
    return context


async def _fetch(migrated: MigratedDatabase, query: str, *args: Any) -> list[Any]:
    connection = await migrated.connect()
    try:
        return list(await connection.fetch(query, *args))
    finally:
        await connection.close()


def chain_rows(
    environment: Environment, organization_id: uuid.UUID, chain: CheckpointChain
) -> list[Any]:
    env = environment.env
    if chain.kind is ChainKind.AUDIT:
        return env.loop.run(
            _fetch(
                env.migrated,
                "SELECT * FROM shared.audit_entry WHERE organization_id = $1"
                " ORDER BY chain_sequence",
                organization_id,
            )
        )
    return env.loop.run(
        _fetch(
            env.migrated,
            "SELECT * FROM ledger.ledger_record WHERE organization_id = $1"
            " AND plant_id IS NOT DISTINCT FROM $2 ORDER BY chain_sequence",
            organization_id,
            chain.plant_id,
        )
    )


def public_keys(environment: Environment) -> dict[str, bytes]:
    return {k.key_id: k.public_key_bytes() for k in environment.service.checkpoint_public_keys()}


def walk(
    environment: Environment,
    organization_id: uuid.UUID,
    chain: CheckpointChain,
    entries: list[dict[str, Any]],
    anchors: dict[int, str] | None = None,
) -> Any:
    walker = ChainWalker(
        ChainRef(
            chain.kind.value,
            str(organization_id),
            None if chain.plant_id is None else str(chain.plant_id),
        ),
        public_keys(environment),
        anchors=anchors or {},
    )
    for entry in entries:
        walker.feed(entry)
    return walker.finish()


def entries_of(
    environment: Environment, organization_id: uuid.UUID, chain: CheckpointChain
) -> list[dict[str, Any]]:
    kind = chain.kind.value
    return [row_to_entry(kind, row) for row in chain_rows(environment, organization_id, chain)]


def checkpoint_events(environment: Environment, organization_id: uuid.UUID) -> list[Any]:
    env = environment.env
    return env.loop.run(
        _fetch(
            env.migrated,
            "SELECT plant_id, ledger_sequence, payload FROM shared.outbox_event"
            " WHERE organization_id = $1 AND event_name = 'checkpoint_written'"
            " ORDER BY created_at, event_id",
            organization_id,
        )
    )


def run_task(environment: Environment, context: ScopeContext) -> None:
    """La tarea registrada, como la invoca el planificador: en una transacción por organización."""
    registry = PeriodicTaskRegistry()
    task = register_write_checkpoints(registry, write_checkpoints_handler(environment.service))

    async def once() -> None:
        async with environment.env.database.transaction(context) as transaction:
            await task.handler(transaction)

    environment.env.loop.run(once())


# --- Criterio 2: dos pasadas seguidas sobre la misma cabeza -------------------------------------


def test_task_twice_on_the_same_head_writes_one_checkpoint_per_chain(
    environment: Environment,
) -> None:
    env = environment.env
    place = Place.new()
    organization_id = place.organization_id
    context = populate(env, place)
    chains = (
        CheckpointChain.audit(),
        CheckpointChain.organization(),
        CheckpointChain.plant(place.plant_id),
    )
    before = env.loop.run(organization_counts(env.migrated, organization_id))

    run_task(environment, context)
    after_first = env.loop.run(organization_counts(env.migrated, organization_id))
    assert after_first["ledger.ledger_record"] == before["ledger.ledger_record"] + 2
    assert after_first["shared.audit_entry"] == before["shared.audit_entry"] + 1
    assert after_first["shared.outbox_event"] == before["shared.outbox_event"] + 3

    run_task(environment, context)
    assert env.loop.run(organization_counts(env.migrated, organization_id)) == after_first
    results = env.loop.run(environment.service.write_checkpoints_now(context))
    assert [r.outcome for r in results] == [CheckpointOutcome.ALREADY_CURRENT] * 3
    assert env.loop.run(organization_counts(env.migrated, organization_id)) == after_first

    # Íntegras con el oráculo de PR-NUC-13 y con el recorrido del verificador de paquetes.
    lengths = env.loop.run(verify_ledger_chains(env.migrated, organization_id))
    assert lengths == {None: 2, place.plant_id: 3}
    assert env.loop.run(verify_audit_chain(env.migrated, organization_id)) == 3
    latest = {c.chain: c for c in env.loop.run(environment.service.latest_checkpoints(context))}
    assert set(latest) == set(chains)
    for chain in chains:
        entries = entries_of(environment, organization_id, chain)
        result = walk(environment, organization_id, chain, entries)
        assert result.intact, result.broken
        (seen,) = result.checkpoints
        last = entries[-1]
        assert seen.sequence == len(entries) == latest[chain].sequence
        assert seen.covered_sequence == len(entries) - 1
        hash_key = "entry_hash" if chain.kind is ChainKind.AUDIT else "record_hash"
        assert seen.covered_hash == entries[-2][hash_key]
        assert latest[chain].entry_hash == last[hash_key]
        content = last["filters" if chain.kind is ChainKind.AUDIT else "content"]
        assert CheckpointContent.from_json(content) == latest[chain].content

    # El registro de planta va en la cadena de la planta con su alcance.
    plant_row = chain_rows(environment, organization_id, CheckpointChain.plant(place.plant_id))[-1]
    assert (plant_row["record_type"], plant_row["plant_id"], plant_row["scope_plant_id"]) == (
        "checkpoint",
        place.plant_id,
        place.plant_id,
    )
    assert plant_row["actor_unit"] == "U-02"

    events = checkpoint_events(environment, organization_id)
    assert len(events) == 3
    payloads = {
        (json.loads(e["payload"])["chain_kind"], e["plant_id"]): (e, json.loads(e["payload"]))
        for e in events
    }
    assert set(payloads) == {("audit", None), ("ledger", None), ("ledger", place.plant_id)}
    event, payload = payloads[("ledger", place.plant_id)]
    assert event["ledger_sequence"] == 3
    assert payload == {
        "chain_kind": "ledger",
        "covered_sequence": 2,
        "covered_hash": latest[CheckpointChain.plant(place.plant_id)].content.covered_hash,
        "taken_at": latest[CheckpointChain.plant(place.plant_id)].content.taken_at,
    }
    assert payloads[("audit", None)][0]["ledger_sequence"] is None


def test_new_records_get_a_new_checkpoint_and_empty_organizations_none(
    environment: Environment,
) -> None:
    env = environment.env
    place = Place.new()
    context = populate(env, place)
    run_task(environment, context)
    write(env, context, "zone_created", zone_document(place, "Zona nueva"))
    results = {r.chain: r for r in env.loop.run(environment.service.write_checkpoints_now(context))}
    plant = CheckpointChain.plant(place.plant_id)
    assert results[plant].outcome is CheckpointOutcome.WRITTEN
    assert results[CheckpointChain.organization()].outcome is CheckpointOutcome.ALREADY_CURRENT
    assert results[CheckpointChain.audit()].outcome is CheckpointOutcome.ALREADY_CURRENT
    assert env.loop.run(verify_ledger_chains(env.migrated, place.organization_id)) == {
        None: 2,
        place.plant_id: 5,
    }

    # Una organización sin registros: ninguna cadena, ningún punto de control.
    empty = system_context(uuid.uuid4())
    assert env.loop.run(environment.service.write_checkpoints_now(empty)) == ()
    (result,) = env.loop.run(
        environment.service.write_checkpoints_now(empty, [CheckpointChain.audit()])
    )
    assert result.outcome is CheckpointOutcome.EMPTY_CHAIN
    assert env.loop.run(environment.service.latest_checkpoints(empty)) == ()


# --- Planta y nivel de cadena (BR-NUC-45) -------------------------------------------------------


def test_chain_level_mismatch_is_content_invalid_not_a_raw_23514(
    environment: Environment,
) -> None:
    env = environment.env
    place = Place.new()
    context = system_context(place.organization_id)
    document = {"probe_id": str(uuid.uuid4()), "level": 1}
    scope = RecordScope(plant_id=place.plant_id)
    # Con la columna sincronizada, el tipo que sigue al alcance entra en la cadena de la planta.
    result = env.loop.run(env.writer.write(context, SCOPE_PROBE, document, scope=scope))
    assert isinstance(result, Receipt)

    async def desync(value: bool) -> None:
        connection = await env.migrated.connect()
        try:
            await connection.execute(
                "UPDATE ledger.record_type SET chain_follows_scope = $1 WHERE record_type = $2",
                value,
                SCOPE_PROBE,
            )
        finally:
            await connection.close()

    env.loop.run(desync(False))
    try:
        before = env.loop.run(organization_counts(env.migrated, place.organization_id))
        other = {"probe_id": str(uuid.uuid4()), "level": 2}
        rejected = env.loop.run(env.writer.write(context, SCOPE_PROBE, other, scope=scope))
        assert rejected == LedgerRejection.of(LedgerRejectionCode.CONTENT_INVALID, "/plant_id")
        assert env.loop.run(organization_counts(env.migrated, place.organization_id)) == before
        # Sin planta sigue entrando en la cadena de organización.
        assert isinstance(env.loop.run(env.writer.write(context, SCOPE_PROBE, other)), Receipt)
    finally:
        env.loop.run(desync(True))

    # ``ledger.record_type`` guarda la declaración del registro (columna de nuc_0005).
    rows = env.loop.run(
        _fetch(
            env.migrated,
            "SELECT record_type FROM ledger.record_type WHERE chain_follows_scope"
            " ORDER BY record_type",
        )
    )
    assert [row["record_type"] for row in rows] == [
        "checkpoint",
        "provider_concession_expired",
        "provider_concession_granted",
        "provider_concession_revoked",
        "provider_query",
        SCOPE_PROBE,
    ]


# --- Guarda del disparador y reintento ----------------------------------------------------------


def _stale(
    environment: Environment,
    organization_id: uuid.UUID,
    chain: CheckpointChain,
    *,
    sequence: Any,
    digest: str,
) -> dict[str, Any]:
    world = environment.world
    taken_at = "2026-09-30T00:00:00.000Z"
    payload = signed_payload(organization_id, chain, 1, digest, taken_at)
    envelope = world.service.sign(SigningPurpose.CHECKPOINT, payload)
    return {
        "covered_sequence": sequence,
        "covered_hash": digest,
        "taken_at": taken_at,
        "key_id": envelope.key_id,
        "signature": envelope.signature,
    }


def test_trigger_rejects_a_checkpoint_that_does_not_cover_the_head(
    environment: Environment,
) -> None:
    env = environment.env
    place = Place.new()
    organization_id = place.organization_id
    context = populate(env, place)
    heads = {h.chain: h for h in env.loop.run(environment.store.heads(context))}
    plant = CheckpointChain.plant(place.plant_id)
    before = env.loop.run(organization_counts(env.migrated, organization_id))

    # Expediente de planta: secuencia anterior, hash distinto, secuencia siguiente.
    head = heads[plant]
    for sequence, digest in (
        (head.last_sequence - 1, head.last_hash),
        (head.last_sequence, "0" * 64),
        (head.last_sequence + 1, head.last_hash),
    ):
        content = _stale(environment, organization_id, plant, sequence=sequence, digest=digest)
        with pytest.raises(sa_exc.IntegrityError) as caught:
            env.loop.run(
                env.writer.write(
                    context, "checkpoint", content, scope=RecordScope(plant_id=place.plant_id)
                )
            )
        assert violated_constraint(caught.value, CHECK_VIOLATION) == CHECKPOINT_COVERAGE_CONSTRAINT

    # Auditoría: además, contenido nulo, secuencia como texto o como decimal.
    head = heads[CheckpointChain.audit()]
    stale_audit: list[dict[str, Any] | None] = [
        _stale(
            environment,
            organization_id,
            CheckpointChain.audit(),
            sequence=head.last_sequence - 1,
            digest=head.last_hash,
        ),
        {
            **_stale(
                environment,
                organization_id,
                CheckpointChain.audit(),
                sequence=0,
                digest=head.last_hash,
            ),
            "covered_sequence": str(head.last_sequence),
        },
        {
            **_stale(
                environment,
                organization_id,
                CheckpointChain.audit(),
                sequence=0,
                digest=head.last_hash,
            ),
            "covered_sequence": head.last_sequence + 0.5,
        },
        None,
    ]
    for filters in stale_audit:
        with pytest.raises(sa_exc.IntegrityError) as caught:
            env.loop.run(env.audit.append(context, AuditOperation.CHECKPOINT, filters=filters))
        assert violated_constraint(caught.value, CHECK_VIOLATION) == CHECKPOINT_COVERAGE_CONSTRAINT

    assert env.loop.run(organization_counts(env.migrated, organization_id)) == before
    # El mismo contenido con la cabeza exacta sí entra (la guarda no rechaza de más).
    exact = _stale(
        environment,
        organization_id,
        CheckpointChain.audit(),
        sequence=head.last_sequence,
        digest=head.last_hash,
    )
    env.loop.run(env.audit.append(context, AuditOperation.CHECKPOINT, filters=exact))


class InterleavingStore(SqlCheckpointStore):
    """Escribe un registro ajeno entre la lectura de la cabeza y el ``INSERT`` del punto."""

    def __init__(
        self, base: SqlCheckpointStore, env: WriterEnvironment, place: Place, times: int
    ) -> None:
        self.__dict__.update(base.__dict__)
        self._env = env
        self._place = place
        self.remaining = times

    async def append(
        self, context: ScopeContext, chain: CheckpointChain, content: CheckpointContent
    ) -> StoredCheckpoint:
        if self.remaining > 0:
            self.remaining -= 1
            if chain.kind is ChainKind.AUDIT:
                await self._env.audit.append(context, AuditOperation.LEDGER_READ)
            else:
                result = await self._env.writer.write(
                    context, "zone_created", zone_document(self._place, "Zona concurrente")
                )
                assert isinstance(result, Receipt)
        return await super().append(context, chain, content)


@pytest.mark.parametrize("kind", [ChainKind.LEDGER, ChainKind.AUDIT])
def test_a_write_between_head_and_insert_is_retried(
    environment: Environment, kind: ChainKind
) -> None:
    env = environment.env
    place = Place.new()
    organization_id = place.organization_id
    context = populate(env, place)
    chain = (
        CheckpointChain.audit()
        if kind is ChainKind.AUDIT
        else CheckpointChain.plant(place.plant_id)
    )
    before = len(chain_rows(environment, organization_id, chain))

    store = InterleavingStore(environment.store, env, place, times=1)
    service = CheckpointService(
        store=store, signer=environment.world.service, clock=environment.world.clock
    )
    (result,) = env.loop.run(service.write_checkpoints_now(context, [chain]))
    assert result.outcome is CheckpointOutcome.WRITTEN
    entries = entries_of(environment, organization_id, chain)
    assert len(entries) == before + 2  # el concurrente y el punto de control, que lo cubre
    assert result.checkpoint is not None
    assert result.checkpoint.content.covered_sequence == before + 1
    assert walk(environment, organization_id, chain, entries).intact

    # Si la cabeza avanza en todos los intentos, no queda punto de control ni evento. Antes, un
    # registro nuevo: sobre una cabeza que ya es punto de control no se intenta nada.
    if kind is ChainKind.AUDIT:
        audit(env, context)
    else:
        write(env, context, "zone_created", zone_document(place, "Zona posterior"))
    events = len(checkpoint_events(environment, organization_id))
    store = InterleavingStore(environment.store, env, place, times=3)
    service = CheckpointService(
        store=store, signer=environment.world.service, clock=environment.world.clock, max_attempts=3
    )
    with pytest.raises(CheckpointWriteFailed) as caught:
        env.loop.run(service.write_checkpoints_now(context, [chain]))
    assert isinstance(caught.value.failures[0][1], CheckpointCoverageConflict)
    entries = entries_of(environment, organization_id, chain)
    assert len(entries) == before + 2 + 1 + 3
    assert store.remaining == 0
    marker = "operation" if kind is ChainKind.AUDIT else "record_type"
    assert entries[-1][marker] != "checkpoint"
    assert len(checkpoint_events(environment, organization_id)) == events


# --- Fallos a mitad de operación ----------------------------------------------------------------


@pytest.mark.parametrize("kind", [ChainKind.LEDGER, ChainKind.AUDIT])
def test_failure_in_any_statement_or_commit_leaves_nothing_and_the_next_run_writes_once(
    environment: Environment, kind: ChainKind
) -> None:
    env = environment.env
    place = Place.new()
    organization_id = place.organization_id
    context = populate(env, place)
    chain = CheckpointChain.audit() if kind is ChainKind.AUDIT else CheckpointChain.organization()
    faults = [Fault(commit=True)] + [Fault(statement=n) for n in range(1, 6)]
    failures = 0
    for fault in faults:
        before = env.loop.run(organization_counts(env.migrated, organization_id))
        env.database.next_fault = fault
        try:
            (result,) = env.loop.run(environment.service.write_checkpoints_now(context, [chain]))
        except CheckpointWriteFailed:
            failures += 1
            assert env.loop.run(organization_counts(env.migrated, organization_id)) == before
            continue
        finally:
            env.database.next_fault = None
        # La sentencia n no existía: la escritura terminó; la cadena ya está al día.
        assert result.outcome is CheckpointOutcome.WRITTEN
        break
    assert failures >= 2  # al menos el INSERT y el COMMIT
    results = env.loop.run(environment.service.write_checkpoints_now(context, [chain]))
    assert results[0].outcome is CheckpointOutcome.ALREADY_CURRENT
    entries = entries_of(environment, organization_id, chain)
    marker = "operation" if kind is ChainKind.AUDIT else "record_type"
    assert [e[marker] for e in entries].count("checkpoint") == 1
    assert walk(environment, organization_id, chain, entries).intact


# --- PR-NUC-20 con el verificador ---------------------------------------------------------------

_ENVELOPE_EXCLUDED = {"content", "previous_hash", "record_hash"}


def _rewrite(
    environment: Environment,
    organization_id: uuid.UUID,
    chain: CheckpointChain,
    entries: list[dict[str, Any]],
    change_at: int | None,
) -> list[dict[str, Any]]:
    """Reescribe la cadena como quien controla la base **y** la clave: todo recalculado."""
    previous = genesis_hash(
        str(organization_id), None if chain.plant_id is None else str(chain.plant_id)
    )
    rewritten: list[dict[str, Any]] = []
    for position, original in enumerate(entries):
        entry = copy.deepcopy(original)
        if position == change_at:
            entry["content"] = {"rewritten": position}
        if entry["record_type"] == "checkpoint":
            taken_at = entry["content"]["taken_at"]
            sequence = entry["chain_sequence"] - 1
            payload = signed_payload(organization_id, chain, sequence, previous, taken_at)
            envelope = environment.world.service.sign(SigningPurpose.CHECKPOINT, payload)
            entry["content"] = {
                "covered_sequence": sequence,
                "covered_hash": previous,
                "taken_at": taken_at,
                "key_id": envelope.key_id,
                "signature": envelope.signature,
            }
        entry["content_hash"] = hashlib.sha256(canonicalize(entry["content"])).hexdigest()
        envelope_document = {k: v for k, v in entry.items() if k not in _ENVELOPE_EXCLUDED}
        entry["previous_hash"] = previous
        entry["record_hash"] = hashlib.sha256(
            canonicalize(envelope_document) + previous.encode("ascii")
        ).hexdigest()
        previous = entry["record_hash"]
        rewritten.append(entry)
    return rewritten


def test_previous_checkpoint_anchor_detects_a_rewritten_prefix(environment: Environment) -> None:
    env = environment.env
    place = Place.new()
    organization_id = place.organization_id
    context = populate(env, place)
    chain = CheckpointChain.plant(place.plant_id)
    (c1,) = env.loop.run(environment.service.write_checkpoints_now(context, [chain]))
    for name in ("Zona A", "Zona B"):
        write(env, context, "zone_created", zone_document(place, name))
    (c2,) = env.loop.run(environment.service.write_checkpoints_now(context, [chain]))
    assert c1.checkpoint is not None and c2.checkpoint is not None
    assert c2.checkpoint.sequence > c1.checkpoint.sequence

    entries = entries_of(environment, organization_id, chain)
    anchors = {c1.checkpoint.sequence: c1.checkpoint.entry_hash}
    result = walk(environment, organization_id, chain, entries, anchors)
    assert result.intact, result.broken
    assert result.anchors_matched == (c1.checkpoint.sequence,)
    assert [c.sequence for c in result.checkpoints] == [
        c1.checkpoint.sequence,
        c2.checkpoint.sequence,
    ]

    # La copia reescrita sin cambios es la misma cadena (el reescritor es fiel).
    assert _rewrite(environment, organization_id, chain, entries, None) == entries
    for change_at in range(len(entries)):
        if entries[change_at]["record_type"] == "checkpoint":
            continue
        rewritten = _rewrite(environment, organization_id, chain, entries, change_at)
        alone = walk(environment, organization_id, chain, rewritten)
        assert alone.intact, alone.broken  # internamente íntegra: solo el ancla la delata
        anchored = walk(environment, organization_id, chain, rewritten, anchors)
        prefix_unchanged = change_at + 1 > c1.checkpoint.sequence
        assert anchored.intact is prefix_unchanged, (change_at, anchored.broken)
        if not prefix_unchanged:
            assert anchored.broken.reason == "previous_checkpoint_mismatch"
            assert anchored.broken.sequence == c1.checkpoint.sequence
