"""Puntos de control firmados (TASK-117, LC-NUC-14): PR-NUC-21, idempotencia por cabeza y claves.

Sin base de datos: ``CheckpointService`` sobre ``MemoryCheckpointStore``, un almacén en memoria
que aplica la misma guarda que el disparador de ``nuc_0005`` (un punto de control solo entra si
cubre exactamente la cabeza) y encadena con hashes propios. La firma es la de ``SigningService``
real con sus dobles de ``tests/signing_support`` (claves generadas; ningún secreto real).

- **PR-NUC-21**: la firma de todo punto de control verifica con la clave pública publicada, con el
  verificador puro del paquete y con ``cryptography``, y falla ante **cualquier bit alterado** del
  contenido (``covered_sequence``, ``covered_hash``, ``taken_at``, ``key_id``), de la firma (en
  sus 64 bytes o en su texto base64) o de la cadena que identifica el mensaje (``kind``,
  ``organization_id``, ``plant_id``). El mensaje firmado es byte a byte el que comprueba
  ``chain_walk.checkpoint_message``.
- **Idempotencia por cabeza**: tras cualquier secuencia generada de escrituras y pasadas de la
  tarea, cada cadena no vacía termina en un punto de control que cubre su registro anterior, y una
  segunda pasada seguida no escribe nada.
- **Conflicto con la cabeza**: si otra escritura se adelanta, se relee y se reintenta; si se
  adelanta en todos los intentos, la cadena queda sin punto de control y se informa.
- **Claves**: ``checkpoint_public_keys()`` conserva una clave ``checkpoint`` tras rotarla y
  retirarla, y un punto de control firmado con ella sigue verificando.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.canonical import canonicalize

from tests.factories import make_context, uuid7
from tests.signing_support import SigningWorld, bootstrapped_world, provider_context
from vigia_platform.ledger.application.checkpoint_task import (
    WRITE_CHECKPOINTS,
    WRITE_CHECKPOINTS_SCHEDULE,
    register_write_checkpoints,
    write_checkpoints_handler,
)
from vigia_platform.ledger.chain.chain_walk import checkpoint_message
from vigia_platform.ledger.chain.checkpoints import (
    MAX_SIGNABLE_SEQUENCE,
    ChainHeadState,
    ChainKind,
    CheckpointChain,
    CheckpointContent,
    CheckpointContextRejected,
    CheckpointCoverageConflict,
    CheckpointOutcome,
    CheckpointService,
    CheckpointWriteFailed,
    StoredCheckpoint,
    event_payload,
    signed_payload,
    verify_checkpoint,
)
from vigia_platform.ledger.chain.pure_rfc8785 import CanonicalizationError
from vigia_platform.ledger.record_types.u02 import Checkpoint
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    ContextOrigin,
    ScopeContext,
    _seal_scope_context,
)
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry, Schedule
from vigia_platform.shared.outbox.u02_events import CheckpointWritten
from vigia_platform.shared.signing import KeyStatus, SigningPurpose, verify_detached
from vigia_platform.shared.signing.keys import format_timestamp

MAX_SEQUENCE = 2**63 - 1

# --- Almacén en memoria -------------------------------------------------------------------------


@dataclass
class MemoryEntry:
    sequence: int
    entry_id: uuid.UUID
    entry_hash: str
    checkpoint: CheckpointContent | None


@dataclass
class MemoryChain:
    genesis: str
    entries: list[MemoryEntry] = field(default_factory=list)
    offset: int = 0
    """Secuencia de la cabeza antes del primer registro guardado (cabezas generadas)."""

    @property
    def last_sequence(self) -> int:
        return self.entries[-1].sequence if self.entries else self.offset

    @property
    def last_hash(self) -> str:
        return self.entries[-1].entry_hash if self.entries else self.genesis

    def append(self, checkpoint: CheckpointContent | None, note: str = "") -> MemoryEntry:
        sequence = self.last_sequence + 1
        digest = hashlib.sha256(f"{sequence}:{self.last_hash}:{note}".encode()).hexdigest()
        entry = MemoryEntry(sequence, uuid.uuid4(), digest, checkpoint)
        self.entries.append(entry)
        return entry


type Hook = Callable[[CheckpointChain], Awaitable[None]]


class MemoryCheckpointStore:
    """``CheckpointStore`` en memoria con la guarda de cobertura del disparador."""

    def __init__(self, organization_id: uuid.UUID) -> None:
        self.organization_id = organization_id
        self.chains: dict[CheckpointChain, MemoryChain] = {}
        self.events: list[dict[str, Any]] = []
        self.calls = 0
        self.before_append: Hook | None = None
        self.fail_chain: CheckpointChain | None = None

    def chain(self, chain: CheckpointChain, *, offset: int = 0, genesis: str = "") -> MemoryChain:
        if chain not in self.chains:
            seed = genesis or hashlib.sha256(f"genesis:{chain.sort_key()}".encode()).hexdigest()
            self.chains[chain] = MemoryChain(seed, offset=offset)
        return self.chains[chain]

    def write_record(self, chain: CheckpointChain, note: str = "registro") -> None:
        self.chain(chain).append(None, note)

    def _check(self, context: ScopeContext) -> None:
        self.calls += 1
        assert context.organization_id == self.organization_id

    async def heads(self, context: ScopeContext) -> Sequence[ChainHeadState]:
        self._check(context)
        return tuple(
            self._head(chain, memory) for chain, memory in self.chains.items() if memory.entries
        )

    async def head(self, context: ScopeContext, chain: CheckpointChain) -> ChainHeadState | None:
        self._check(context)
        memory = self.chains.get(chain)
        if memory is None or (not memory.entries and memory.offset == 0):
            return None
        return self._head(chain, memory)

    @staticmethod
    def _head(chain: CheckpointChain, memory: MemoryChain) -> ChainHeadState:
        last = memory.entries[-1] if memory.entries else None
        return ChainHeadState(
            chain,
            memory.last_sequence,
            memory.last_hash,
            last is not None and last.checkpoint is not None,
        )

    async def latest(
        self, context: ScopeContext, chain: CheckpointChain
    ) -> StoredCheckpoint | None:
        self._check(context)
        memory = self.chains.get(chain)
        for entry in reversed(memory.entries if memory else []):
            if entry.checkpoint is not None:
                return StoredCheckpoint(
                    chain, entry.sequence, entry.entry_id, entry.entry_hash, entry.checkpoint
                )
        return None

    async def append(
        self, context: ScopeContext, chain: CheckpointChain, content: CheckpointContent
    ) -> StoredCheckpoint:
        self._check(context)
        if self.before_append is not None:
            await self.before_append(chain)
        if chain == self.fail_chain:
            raise ConnectionError("base caída a mitad de la escritura")
        memory = self.chain(chain)
        # La guarda del disparador (nuc_0005): cubre exactamente la cabeza o no entra.
        if (content.covered_sequence, content.covered_hash) != (
            memory.last_sequence,
            memory.last_hash,
        ):
            raise CheckpointCoverageConflict()
        entry = memory.append(content, json.dumps(content.to_json(), sort_keys=True))
        self.events.append(event_payload(chain, content))
        return StoredCheckpoint(chain, entry.sequence, entry.entry_id, entry.entry_hash, content)


# --- Entorno ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def world() -> SigningWorld:
    return asyncio.run(bootstrapped_world())


def u02_context(organization_id: uuid.UUID) -> ScopeContext:
    return make_context(kind=ActorKind.SYSTEM, organization_id=organization_id)


def unit_context(organization_id: uuid.UUID, unit: ActorUnit) -> ScopeContext:
    actor = Actor(
        kind=ActorKind.SYSTEM, id=uuid.uuid4(), display_name_snapshot="Sistema", unit=unit
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.PERIODIC_ITERATION,
        allowed_scopes=(),
        correlation_id=uuid7(),
    )


def service_for(
    world: SigningWorld, store: MemoryCheckpointStore, **options: Any
) -> CheckpointService:
    return CheckpointService(store=store, signer=world.service, clock=world.clock, **options)


def published(service: CheckpointService) -> dict[str, bytes]:
    return {key.key_id: key.public_key_bytes() for key in service.checkpoint_public_keys()}


chains_st = st.one_of(
    st.just(CheckpointChain.audit()),
    st.just(CheckpointChain.organization()),
    st.uuids(version=4).map(CheckpointChain.plant),
)
hex64 = st.binary(min_size=32, max_size=32).map(bytes.hex)


# --- PR-NUC-21 ----------------------------------------------------------------------------------


def _flip_char(text: str, index: int, bit: int) -> str:
    """Un bit alterado en un carácter ASCII (bits 0 a 6: sigue siendo ASCII y distinto)."""
    position = index % len(text)
    flipped = chr(ord(text[position]) ^ (1 << (bit % 7)))
    return text[:position] + flipped + text[position + 1 :]


def _flip_signature_bit(signature: str, bit: int) -> str:
    raw = bytearray(base64.b64decode(signature))
    raw[bit // 8] ^= 1 << (bit % 8)
    return base64.b64encode(bytes(raw)).decode("ascii")


def _flip_uuid(value: uuid.UUID, bit: int) -> uuid.UUID:
    return uuid.UUID(int=value.int ^ (1 << bit))


def _mutations(
    organization_id: uuid.UUID, chain: CheckpointChain, content: CheckpointContent, data: Any
) -> Iterator[tuple[str, uuid.UUID, CheckpointChain, CheckpointContent]]:
    """Cada campo del mensaje o de la firma con un bit alterado."""
    index = data.draw(st.integers(0, 200), label="index")
    bit = data.draw(st.integers(0, 511), label="bit")
    yield (
        "covered_sequence",
        organization_id,
        chain,
        replace(
            content,
            covered_sequence=content.covered_sequence ^ (1 << (bit % 63)),
        ),
    )
    for name in ("covered_hash", "taken_at", "key_id"):
        altered = _flip_char(getattr(content, name), index, bit)
        yield name, organization_id, chain, replace(content, **{name: altered})
    yield (
        "signature_bytes",
        organization_id,
        chain,
        replace(content, signature=_flip_signature_bit(content.signature, bit)),
    )
    # El texto base64 de la firma (los 86 caracteres con datos, sin el relleno).
    text = content.signature
    position = index % 86
    yield (
        "signature_text",
        organization_id,
        chain,
        replace(content, signature=_flip_char(text[:86], position, bit) + text[86:]),
    )
    yield "organization_id", _flip_uuid(organization_id, bit % 128), chain, content
    other_kind = ChainKind.LEDGER if chain.kind is ChainKind.AUDIT else ChainKind.AUDIT
    if chain.plant_id is None:
        yield "kind", organization_id, CheckpointChain(other_kind, None), content
    else:
        yield (
            "plant_id",
            organization_id,
            CheckpointChain(ChainKind.LEDGER, _flip_uuid(chain.plant_id, bit % 128)),
            content,
        )
        yield "plant_id_dropped", organization_id, CheckpointChain.organization(), content


@given(
    organization_id=st.uuids(version=4),
    chain=chains_st,
    covered_sequence=st.one_of(
        st.integers(1, MAX_SIGNABLE_SEQUENCE), st.sampled_from([1, 2, MAX_SIGNABLE_SEQUENCE])
    ),
    covered_hash=hex64,
    elapsed_ms=st.integers(0, 300 * 24 * 3600 * 1000),
    data=st.data(),
)
def test_signature_verifies_and_fails_on_any_flipped_bit(
    world: SigningWorld,
    organization_id: uuid.UUID,
    chain: CheckpointChain,
    covered_sequence: int,
    covered_hash: str,
    elapsed_ms: int,
    data: st.DataObject,
) -> None:
    """PR-NUC-21: verifica con la clave publicada y falla ante cualquier bit alterado."""
    store = MemoryCheckpointStore(organization_id)
    store.chain(chain, offset=covered_sequence - 1).append(None, covered_hash)
    head = store.chains[chain]
    service = service_for(world, store)
    moment = world.clock.now() + timedelta(milliseconds=elapsed_ms)
    clock = type(world.clock)(moment)
    timed = CheckpointService(store=store, signer=world.service, clock=clock)

    (result,) = asyncio.run(timed.write_checkpoints_now(u02_context(organization_id), [chain]))
    assert result.outcome is CheckpointOutcome.WRITTEN
    assert result.checkpoint is not None
    content = result.checkpoint.content
    assert (content.covered_sequence, content.covered_hash) == (
        covered_sequence,
        head.entries[0].entry_hash,
    )
    assert content.taken_at == format_timestamp(moment)
    # El contenido cumple el tipo registrado (el escritor lo aceptaría) y el evento su modelo.
    Checkpoint.__pydantic_validator__.validate_json(json.dumps(content.to_json()), strict=True)
    CheckpointWritten.model_validate_json(json.dumps(store.events[-1]))

    # El mensaje firmado es el del verificador de paquetes, con las dos canonicalizaciones.
    payload = signed_payload(
        organization_id, chain, content.covered_sequence, content.covered_hash, content.taken_at
    )
    message = checkpoint_message(
        chain.kind.value,
        str(organization_id),
        None if chain.plant_id is None else str(chain.plant_id),
        content.covered_sequence,
        content.covered_hash,
        content.taken_at,
    )
    assert canonicalize(payload) == message
    keys = published(service)
    active = next(k for k in service.checkpoint_public_keys() if k.status is KeyStatus.ACTIVE)
    assert content.key_id == active.key_id
    assert verify_checkpoint(organization_id, chain, content, keys)
    assert verify_detached(active.public_key, message, content.signature)

    for name, organization, altered_chain, altered in _mutations(
        organization_id, chain, content, data
    ):
        assert not verify_checkpoint(organization, altered_chain, altered, keys), name
        try:
            altered_message = checkpoint_message(
                altered_chain.kind.value,
                str(organization),
                None if altered_chain.plant_id is None else str(altered_chain.plant_id),
                altered.covered_sequence,
                altered.covered_hash,
                altered.taken_at,
            )
        except CanonicalizationError:
            # Un bit alto de la secuencia la saca de I-JSON: no hay mensaje que pueda verificar.
            assert altered.covered_sequence > MAX_SIGNABLE_SEQUENCE, name
            continue
        assert not (
            altered.key_id == active.key_id
            and verify_detached(active.public_key, altered_message, altered.signature)
        ), name


def test_sequence_beyond_ijson_range_is_never_signed_nor_verified(world: SigningWorld) -> None:
    """Borde de BR-NUC-53: 2^53 - 1 se firma; 2^53 no tiene forma canónica y no se firma."""
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    chain = CheckpointChain.organization()
    store.chain(chain, offset=MAX_SIGNABLE_SEQUENCE)
    store.write_record(chain)  # cabeza en 2^53
    service = service_for(world, store)
    with pytest.raises(CheckpointWriteFailed) as caught:
        asyncio.run(service.write_checkpoints_now(u02_context(organization_id), [chain]))
    assert isinstance(caught.value.failures[0][1], ValueError)
    assert store.events == []
    # Un contenido con esa secuencia y una clave publicada no verifica ni lanza.
    key_id = service.checkpoint_public_keys()[0].key_id
    content = CheckpointContent(
        MAX_SIGNABLE_SEQUENCE + 1, "a" * 64, "2026-09-30T00:00:00.000Z", key_id, "A" * 86 + "=="
    )
    assert not verify_checkpoint(organization_id, chain, content, published(service))


def test_signature_with_unknown_or_malformed_key_fails(world: SigningWorld) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    store.write_record(CheckpointChain.audit())
    service = service_for(world, store)
    (result,) = asyncio.run(service.write_checkpoints_now(u02_context(organization_id)))
    assert result.checkpoint is not None
    content = result.checkpoint.content
    keys = published(service)
    assert verify_checkpoint(organization_id, CheckpointChain.audit(), content, keys)
    assert not verify_checkpoint(organization_id, CheckpointChain.audit(), content, {})
    for signature in ("", "no es base64", content.signature[:-2], "A" * 86 + "=="):
        assert not verify_checkpoint(
            organization_id,
            CheckpointChain.audit(),
            replace(content, signature=signature),
            keys,
        )


# --- Idempotencia por cabeza --------------------------------------------------------------------

operations = st.lists(
    st.one_of(
        st.tuples(st.just("record"), st.integers(0, 3)),
        st.tuples(st.just("task"), st.just(0)),
    ),
    min_size=1,
    max_size=25,
)


@given(ops=operations, plants=st.lists(st.uuids(version=4), min_size=1, max_size=2, unique=True))
def test_task_is_idempotent_per_head(
    world: SigningWorld, ops: list[tuple[str, int]], plants: list[uuid.UUID]
) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    service = service_for(world, store)
    context = u02_context(organization_id)
    chains = [
        CheckpointChain.audit(),
        CheckpointChain.organization(),
        *(CheckpointChain.plant(p) for p in plants),
    ]
    keys = published(service)

    def entries() -> int:
        return sum(len(chain.entries) for chain in store.chains.values())

    for operation, target in ops:
        if operation == "record":
            store.write_record(chains[target % len(chains)])
            continue
        before = {chain: memory.last_sequence for chain, memory in store.chains.items()}
        results = asyncio.run(service.write_checkpoints_now(context))
        assert {r.chain for r in results} == {c for c, m in store.chains.items() if m.entries}
        for result in results:
            memory = store.chains[result.chain]
            last = memory.entries[-1]
            assert last.checkpoint is not None
            assert result.checkpoint is not None
            assert result.checkpoint.sequence == last.sequence
            previous = memory.entries[-2] if len(memory.entries) > 1 else None
            assert previous is not None, "un punto de control siempre cubre un registro"
            assert (last.checkpoint.covered_sequence, last.checkpoint.covered_hash) == (
                previous.sequence,
                previous.entry_hash,
            )
            assert verify_checkpoint(organization_id, result.chain, last.checkpoint, keys)
            wrote = before[result.chain] != memory.last_sequence
            assert (result.outcome is CheckpointOutcome.WRITTEN) is wrote
        # Una segunda pasada seguida, sobre las mismas cabezas, no escribe nada.
        written, events = entries(), len(store.events)
        again = asyncio.run(service.write_checkpoints_now(context))
        assert {r.outcome for r in again} <= {CheckpointOutcome.ALREADY_CURRENT}
        assert (entries(), len(store.events)) == (written, events)
        assert [r.checkpoint for r in again] == [r.checkpoint for r in results]


def test_empty_or_unknown_chain_gets_no_checkpoint(world: SigningWorld) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    service = service_for(world, store)
    context = u02_context(organization_id)
    assert asyncio.run(service.write_checkpoints_now(context)) == ()
    chain = CheckpointChain.plant(uuid.uuid4())
    (result,) = asyncio.run(service.write_checkpoints_now(context, [chain]))
    assert (result.outcome, result.checkpoint) == (CheckpointOutcome.EMPTY_CHAIN, None)
    assert store.events == []
    assert asyncio.run(service.latest_checkpoints(context)) == ()


def test_latest_checkpoints_returns_the_newest_per_chain(world: SigningWorld) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    service = service_for(world, store)
    context = u02_context(organization_id)
    plant = CheckpointChain.plant(uuid.uuid4())
    store.write_record(plant)
    store.write_record(CheckpointChain.audit())
    first = asyncio.run(service.write_checkpoints_now(context))
    store.write_record(plant)
    second = asyncio.run(service.write_checkpoints_now(context, [plant]))
    store.write_record(plant)  # la cabeza ya no es un punto de control
    latest = {c.chain: c for c in asyncio.run(service.latest_checkpoints(context))}
    assert latest[plant] == second[0].checkpoint
    audit = next(r for r in first if r.chain == CheckpointChain.audit())
    assert latest[CheckpointChain.audit()] == audit.checkpoint
    assert asyncio.run(service.latest_checkpoints(context, [CheckpointChain.organization()])) == ()


# --- Conflicto con la cabeza y fallos -----------------------------------------------------------


@given(interleaved=st.integers(0, 5), max_attempts=st.integers(1, 4))
def test_head_moved_is_retried_then_reported(
    world: SigningWorld, interleaved: int, max_attempts: int
) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    chain = CheckpointChain.plant(uuid.uuid4())
    store.write_record(chain)
    remaining = [interleaved]

    async def concurrent_write(target: CheckpointChain) -> None:
        if remaining[0] > 0:
            remaining[0] -= 1
            store.write_record(target, "escritura concurrente")

    store.before_append = concurrent_write
    service = service_for(world, store, max_attempts=max_attempts)
    context = u02_context(organization_id)
    if interleaved < max_attempts:
        (result,) = asyncio.run(service.write_checkpoints_now(context))
        assert result.outcome is CheckpointOutcome.WRITTEN
        memory = store.chains[chain]
        assert memory.entries[-1].checkpoint is not None
        assert len(memory.entries) == 1 + interleaved + 1
        assert result.checkpoint is not None
        assert result.checkpoint.content.covered_sequence == 1 + interleaved
    else:
        with pytest.raises(CheckpointWriteFailed) as caught:
            asyncio.run(service.write_checkpoints_now(context))
        assert caught.value.results == ()
        ((failed, error),) = caught.value.failures
        assert failed == chain and isinstance(error, CheckpointCoverageConflict)
        assert all(entry.checkpoint is None for entry in store.chains[chain].entries)
        assert store.events == []


def test_one_failing_chain_does_not_block_the_others(world: SigningWorld) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    plant = CheckpointChain.plant(uuid.uuid4())
    for chain in (CheckpointChain.audit(), CheckpointChain.organization(), plant):
        store.write_record(chain)
    store.fail_chain = CheckpointChain.organization()
    service = service_for(world, store)
    context = u02_context(organization_id)
    with pytest.raises(CheckpointWriteFailed) as caught:
        asyncio.run(service.write_checkpoints_now(context))
    assert {r.chain for r in caught.value.results} == {CheckpointChain.audit(), plant}
    assert [chain for chain, _ in caught.value.failures] == [CheckpointChain.organization()]
    assert isinstance(caught.value.failures[0][1], ConnectionError)
    # La siguiente pasada completa la cadena que faltaba y no repite las demás.
    store.fail_chain = None
    results = asyncio.run(service.write_checkpoints_now(context))
    outcomes = {r.chain: r.outcome for r in results}
    assert outcomes[CheckpointChain.organization()] is CheckpointOutcome.WRITTEN
    assert outcomes[plant] is CheckpointOutcome.ALREADY_CURRENT
    assert len(store.events) == 3


def test_signing_unavailable_writes_nothing(world: SigningWorld) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    store.write_record(CheckpointChain.audit())

    class Unready:
        def sign(self, purpose: SigningPurpose, payload: Any) -> Any:
            raise RuntimeError("firma no disponible")

        def public_keys(self, purpose: SigningPurpose) -> tuple[Any, ...]:
            return ()

    service = CheckpointService(store=store, signer=Unready(), clock=world.clock)
    with pytest.raises(CheckpointWriteFailed):
        asyncio.run(service.write_checkpoints_now(u02_context(organization_id)))
    assert store.events == []
    assert store.chains[CheckpointChain.audit()].entries[-1].checkpoint is None


# --- Contexto -----------------------------------------------------------------------------------


@pytest.mark.parametrize("unit", [ActorUnit.U03, ActorUnit.U04])
def test_other_unit_context_is_rejected_before_touching_the_store(
    world: SigningWorld, unit: ActorUnit
) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    store.write_record(CheckpointChain.audit())
    service = service_for(world, store)
    with pytest.raises(CheckpointContextRejected):
        asyncio.run(service.write_checkpoints_now(unit_context(organization_id, unit)))
    with pytest.raises(CheckpointContextRejected):
        asyncio.run(service.write_checkpoints_now(None))  # type: ignore[arg-type]
    assert store.calls == 0


def test_writer_context_translates_u04_to_u02_of_the_same_organization(
    world: SigningWorld,
) -> None:
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    store.write_record(CheckpointChain.audit())
    seen: list[ScopeContext] = []

    def to_u02(context: ScopeContext) -> ScopeContext:
        seen.append(context)
        return u02_context(context.organization_id)

    service = service_for(world, store, writer_context=to_u02)
    u04 = unit_context(organization_id, ActorUnit.U04)
    (result,) = asyncio.run(service.write_checkpoints_now(u04))
    assert result.outcome is CheckpointOutcome.WRITTEN
    assert seen == [u04]

    # Un traductor que devuelve otra organización u otra unidad no sirve.
    for bad in (
        lambda c: u02_context(uuid.uuid4()),
        lambda c: unit_context(c.organization_id, ActorUnit.U04),
    ):
        rejected = service_for(world, store, writer_context=bad)
        with pytest.raises(CheckpointContextRejected):
            asyncio.run(rejected.write_checkpoints_now(u04))


def test_invalid_arguments(world: SigningWorld) -> None:
    store = MemoryCheckpointStore(uuid.uuid4())
    with pytest.raises(ValueError, match="plant_id"):
        CheckpointChain(ChainKind.AUDIT, uuid.uuid4())
    with pytest.raises(TypeError):
        CheckpointChain("ledger", None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CheckpointChain(ChainKind.LEDGER, "planta")  # type: ignore[arg-type]
    for attempts in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="max_attempts"):
            service_for(world, store, max_attempts=attempts)
    service = service_for(world, store)
    context = u02_context(store.organization_id)
    with pytest.raises(TypeError):
        asyncio.run(service.write_checkpoints_now(context, CheckpointChain.audit()))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        asyncio.run(service.write_checkpoints_now(context, ["audit"]))  # type: ignore[list-item]


# --- Lectura estricta del contenido persistido --------------------------------------------------

VALID = {
    "covered_sequence": 7,
    "covered_hash": "a" * 64,
    "taken_at": "2026-09-30T00:00:00.000Z",
    "key_id": "checkpoint-2026-09",
    "signature": "A" * 86 + "==",
}


@pytest.mark.parametrize(
    "change",
    [
        {"covered_sequence": True},
        {"covered_sequence": -1},
        {"covered_sequence": 2**63},
        {"covered_sequence": 7.0},
        {"covered_sequence": "7"},
        {"covered_hash": "A" * 64},
        {"covered_hash": "a" * 63},
        {"covered_hash": "a" * 64 + "\n"},
        {"taken_at": "2026-09-30T00:00:00Z"},
        {"taken_at": "2026-09-30T00:00:00.000+00:00"},
        {"key_id": ""},
        {"key_id": "Checkpoint"},
        {"key_id": "k" * 65},
        {"signature": "A" * 87 + "="},
        {"signature": "A" * 86 + "=="[:1]},
        {"extra": 1},
        {"signature": None},
    ],
)
def test_content_from_json_is_strict(change: dict[str, Any]) -> None:
    document = {**VALID, **change}
    if change == {"signature": None}:
        del document["signature"]
    with pytest.raises(ValueError):
        CheckpointContent.from_json(document)


def test_content_from_json_round_trip_and_edges() -> None:
    assert CheckpointContent.from_json(VALID).to_json() == VALID
    for sequence in (0, MAX_SEQUENCE):
        CheckpointContent.from_json({**VALID, "covered_sequence": sequence})
    with pytest.raises(ValueError):
        CheckpointContent.from_json([VALID])


# --- Claves públicas (criterio 3, BR-NUC-55) ----------------------------------------------------


def test_public_keys_keep_a_rotated_and_retired_checkpoint_key() -> None:
    world = asyncio.run(bootstrapped_world())
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    store.write_record(CheckpointChain.audit())
    service = service_for(world, store)
    context = u02_context(organization_id)
    (old,) = asyncio.run(service.write_checkpoints_now(context))
    assert old.checkpoint is not None
    old_key = old.checkpoint.content.key_id

    rotated = asyncio.run(
        world.service.rotate(SigningPurpose.CHECKPOINT, context=provider_context())
    )
    assert rotated.previous_key_id == old_key
    statuses = {k.key_id: k.status for k in service.checkpoint_public_keys()}
    assert statuses == {old_key: KeyStatus.OVERLAPPING, rotated.new_key.key_id: KeyStatus.ACTIVE}

    # Pasado el solapamiento, la antigua queda retirada y sigue publicada.
    world.clock.advance(timedelta(days=31).total_seconds())
    transitions = asyncio.run(world.service.retire_expired())
    assert [t.key_id for t in transitions] == [old_key]
    keys = service.checkpoint_public_keys()
    statuses = {k.key_id: k.status for k in keys}
    assert statuses[old_key] is KeyStatus.RETIRED
    assert statuses[rotated.new_key.key_id] is KeyStatus.ACTIVE
    assert all(
        k.to_json().keys()
        == {"key_id", "algorithm", "public_key", "status", "valid_from", "valid_until"}
        for k in keys
    )

    # El punto de control antiguo sigue verificando; el nuevo firma con la clave nueva.
    assert verify_checkpoint(
        organization_id, CheckpointChain.audit(), old.checkpoint.content, published(service)
    )
    store.write_record(CheckpointChain.audit())
    (new,) = asyncio.run(service.write_checkpoints_now(context))
    assert new.checkpoint is not None
    assert new.checkpoint.content.key_id == rotated.new_key.key_id
    assert verify_checkpoint(
        organization_id, CheckpointChain.audit(), new.checkpoint.content, published(service)
    )
    # Solo claves checkpoint: ninguna de otro propósito.
    others = {
        k.key_id
        for purpose in SigningPurpose
        if purpose is not SigningPurpose.CHECKPOINT
        for k in world.service.public_keys(purpose)
    }
    assert not others & {k.key_id for k in keys}


# --- Tarea registrada ---------------------------------------------------------------------------


def test_task_is_registered_daily_at_midnight_and_runs_per_organization(
    world: SigningWorld,
) -> None:
    registry = PeriodicTaskRegistry()
    organization_id = uuid.uuid4()
    store = MemoryCheckpointStore(organization_id)
    store.write_record(CheckpointChain.organization())
    service = service_for(world, store)
    task = register_write_checkpoints(registry, write_checkpoints_handler(service))
    assert (task.task_name, task.unit) == (WRITE_CHECKPOINTS, ActorUnit.U02)
    assert task.schedule == WRITE_CHECKPOINTS_SCHEDULE == Schedule.daily(hour=0, minute=0)
    assert registry.get(WRITE_CHECKPOINTS) is task

    @dataclass
    class FakeTransaction:
        context: ScopeContext

    transaction = FakeTransaction(u02_context(organization_id))
    asyncio.run(task.handler(transaction))  # type: ignore[arg-type]
    asyncio.run(task.handler(transaction))  # type: ignore[arg-type]
    assert len(store.events) == 1
