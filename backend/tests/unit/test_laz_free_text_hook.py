"""Lo que U-02 añade a ``FreeTextPolicyRegistry`` para los validadores de U-04 (TASK-303).

- ``apply(..., organization_id=...)`` entrega la organización al validador en el candidato
  (BLM §9: ``validate(organization_id, path, text)``); sin ella, ``None``;
- ``register_preparer``: solo para un validador ya registrado, una vez y antes de sellar;
  ``prepare`` corre los preparadores en el orden de registro con la ``FreeTextPreparation``;
- ``FreeTextRejected.detail_code`` es ``None`` en la política base y lo conserva el escritor en su
  ``LedgerRejection`` (``EscritorExpediente._apply_free_text``).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any, cast

import pytest
from pydantic import Field, StrictStr
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable
from vigia_contracts.models.enumerations import AcceptanceStatus

from tests.writer_support import unit_context
from vigia_platform.ledger.application import writer as writer_module
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.ledger.canonical import canonical_bytes_sync
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextPreparation,
    FreeTextRejected,
)
from vigia_platform.ledger.registry import (
    ChainLevel,
    ContentModel,
    RecordType,
    RecordTypeRegistry,
)
from vigia_platform.loop.adapters.postgres.declared_names import _NAMES as DECLARED_NAMES
from vigia_platform.loop.adapters.postgres.vocabulary import _HEAD as VOCABULARY_HEAD
from vigia_platform.loop.registration import FREE_TEXT_REGISTRARS
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit

FIELD = FreeTextField("order_probe", "/note", 1, 200)


async def _rows(statement: Executable, parameters: Mapping[str, Any]) -> Sequence[Row[Any]]:
    return ()


def test_apply_hands_the_organization_to_the_validators() -> None:
    received: list[FreeTextCandidate] = []
    registry = FreeTextPolicyRegistry()
    registry.register("probe", lambda candidate, _field: received.append(candidate))
    organization = uuid.uuid4()
    registry.apply("Revisión", FIELD, organization_id=organization)
    registry.apply("Revisión", FIELD)
    assert [candidate.organization_id for candidate in received] == [organization, None]


def test_a_preparer_needs_its_validator_once_and_before_sealing() -> None:
    async def preparer(_: FreeTextPreparation) -> None:
        return None

    registry = FreeTextPolicyRegistry()
    with pytest.raises(ValueError, match="no está registrado"):
        registry.register_preparer("probe", preparer)
    registry.register("probe", lambda _candidate, _field: None)
    assert not registry.has_preparers
    registry.register_preparer("probe", preparer)
    assert registry.has_preparers
    with pytest.raises(ValueError, match="ya está registrado"):
        registry.register_preparer("probe", preparer)
    registry.register("other", lambda _candidate, _field: None)
    registry.seal()
    with pytest.raises(RuntimeError):
        registry.register_preparer("other", preparer)


def test_prepare_runs_the_preparers_in_order() -> None:
    calls: list[tuple[str, uuid.UUID]] = []
    registry = FreeTextPolicyRegistry()
    for name in ("first", "second"):
        registry.register(name, lambda _candidate, _field: None)

        async def preparer(preparation: FreeTextPreparation, name: str = name) -> None:
            calls.append((name, preparation.organization_id))

        registry.register_preparer(name, preparer)
    organization = uuid.uuid4()
    clock = SimulatedClock(datetime(2026, 10, 18, tzinfo=UTC))
    asyncio.run(registry.prepare(FreeTextPreparation(organization, _rows, clock)))
    assert calls == [("first", organization), ("second", organization)]


def test_the_preparers_of_one_write_share_their_reads() -> None:
    """La misma sentencia con los mismos parámetros va a la base una vez por preparación (las dos
    de U-04 leen la misma fila de la lista, VIG-261); otra sentencia, otros parámetros u otra
    preparación vuelven a leer."""
    head = object()
    names = object()
    reads: list[tuple[object, dict[str, Any]]] = []

    async def counted(statement: Executable, parameters: Mapping[str, Any]) -> Sequence[Row[Any]]:
        reads.append((statement, dict(parameters)))
        return ()

    seen: list[Sequence[Row[Any]]] = []
    registry = FreeTextPolicyRegistry()
    for name, extra in (("first", None), ("second", names)):
        registry.register(name, lambda _candidate, _field: None)

        async def preparer(preparation: FreeTextPreparation, extra: object | None = extra) -> None:
            organization = {"organization_id": preparation.organization_id}
            seen.append(await preparation.rows(head, organization))  # type: ignore[arg-type]
            seen.append(await preparation.rows(head, organization))  # type: ignore[arg-type]
            if extra is not None:
                await preparation.rows(extra, organization)  # type: ignore[arg-type]
                await preparation.rows(head, {"organization_id": uuid.uuid4()})  # type: ignore[arg-type]

        registry.register_preparer(name, preparer)
    organization = uuid.uuid4()
    clock = SimulatedClock(datetime(2026, 10, 22, tzinfo=UTC))
    asyncio.run(registry.prepare(FreeTextPreparation(organization, counted, clock)))
    assert [statement for statement, _ in reads] == [head, names, head]
    assert reads[0][1] == {"organization_id": organization}
    assert all(rows == () for rows in seen)
    asyncio.run(registry.prepare(FreeTextPreparation(organization, counted, clock)))
    assert [statement for statement, _ in reads[3:]] == [head, names, head]


def test_the_u04_preparers_read_the_vocabulary_list_once_per_write() -> None:
    """Con los dos preparadores reales de U-04, cada escritura lee la fila de la lista una vez
    (antes, dos: una transacción propia cada una en la ingesta del nodo, VIG-261); los nombres
    declarados solo en la primera, mientras la entrada no caduca."""
    reads: list[object] = []

    async def counted(statement: Executable, parameters: Mapping[str, Any]) -> Sequence[Row[Any]]:
        reads.append(statement)
        return ()

    registry = FreeTextPolicyRegistry()
    for register in FREE_TEXT_REGISTRARS:
        register(registry)
    organization = uuid.uuid4()
    clock = SimulatedClock(datetime(2026, 10, 22, tzinfo=UTC))
    asyncio.run(registry.prepare(FreeTextPreparation(organization, counted, clock)))
    assert reads == [VOCABULARY_HEAD, DECLARED_NAMES]
    asyncio.run(registry.prepare(FreeTextPreparation(organization, counted, clock)))
    assert reads[2:] == [VOCABULARY_HEAD]


def test_unhashable_parameters_are_read_every_time() -> None:
    reads: list[object] = []

    async def counted(statement: Executable, parameters: Mapping[str, Any]) -> Sequence[Row[Any]]:
        reads.append(statement)
        return ()

    async def preparer(preparation: FreeTextPreparation) -> None:
        for _ in range(2):
            await preparation.rows(_rows, {"ids": [1, 2]})  # type: ignore[arg-type]

    registry = FreeTextPolicyRegistry()
    registry.register("probe", lambda _candidate, _field: None)
    registry.register_preparer("probe", preparer)
    clock = SimulatedClock(datetime(2026, 10, 22, tzinfo=UTC))
    asyncio.run(registry.prepare(FreeTextPreparation(uuid.uuid4(), counted, clock)))
    assert len(reads) == 2


def test_the_base_policy_carries_no_detail_code() -> None:
    with pytest.raises(FreeTextRejected) as raised:
        FreeTextPolicyRegistry().apply("<b>x</b>", FIELD)
    assert (raised.value.reason, raised.value.detail_code) == ("markup", None)


# --- El escritor: preparadores y paso 5 en una sola transacción de lectura (VIG-261) -----------


class _NoteProbe(ContentModel):
    probe_key: Annotated[StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9]+$")]
    note: Annotated[StrictStr, Field(min_length=1, max_length=200)]


_NOTE_TYPE = "note_probe"


class _OneReadDatabase:
    """Base doble del escritor: cuenta transacciones de lectura y sentencias; el paso 7 no llega.

    ``existing`` es la fila de ``ledger.record_source_key`` que devuelve la búsqueda del paso 5.
    """

    def __init__(self, existing: object | None = None) -> None:
        self.existing = existing
        self.read_transactions = 0
        self.statements: list[object] = []

    async def _rows(
        self, statement: Executable, parameters: Mapping[str, Any] | None = None
    ) -> Sequence[Any]:
        self.statements.append(statement)
        if statement is writer_module._BY_SOURCE_KEY:
            return [] if self.existing is None else [self.existing]
        return []

    async def read(
        self, context: Any, statement: Executable, parameters: Mapping[str, Any] | None = None
    ) -> Sequence[Any]:
        self.read_transactions += 1
        return await self._rows(statement, parameters)

    async def read_many(self, context: Any, work: Any) -> Any:
        self.read_transactions += 1
        return await work(self._rows)

    def transaction(self, context: Any) -> Any:
        raise AssertionError("paso 7")


def _note_writer(database: _OneReadDatabase, *, preparers: bool = True) -> EscritorExpediente:
    registry = RecordTypeRegistry()
    registry.register(
        RecordType(
            record_type=_NOTE_TYPE,
            writer_unit=ActorUnit.U04,
            chain_level=ChainLevel.ORGANIZATION,
            schema_version=1,
            content_model=_NoteProbe,
            source_key_path="/probe_key",
            free_text_paths=("/note",),
        )
    )
    free_text = FreeTextPolicyRegistry()
    if preparers:
        for register in FREE_TEXT_REGISTRARS:
            register(free_text)
    return EscritorExpediente(
        database=database,
        registry=registry,
        free_text=free_text,
        evidence=cast(Any, None),
        outbox=cast(Any, None),
        clock=SimulatedClock(datetime(2026, 10, 22, tzinfo=UTC)),
    )


_NOTE = {"probe_key": "k1", "note": "Revisión de la guarda norte"}


def _existing(content_hash: str) -> SimpleNamespace:
    return SimpleNamespace(
        record_id=uuid.uuid4(),
        received_at=datetime(2026, 10, 21, tzinfo=UTC),
        content_hash=content_hash,
    )


def _write(writer: EscritorExpediente, document: dict[str, Any]) -> Any:
    context = unit_context(uuid.uuid4(), ActorUnit.U04)
    return asyncio.run(writer.write(context, _NOTE_TYPE, document))


def test_the_preparers_and_the_source_key_share_one_read_transaction() -> None:
    """Sin la transacción del llamador, los preparadores de U-04 y la búsqueda del paso 5 van en
    una transacción de lectura; antes eran tres (dos preparadores y la búsqueda)."""
    database = _OneReadDatabase()
    with pytest.raises(AssertionError, match="paso 7"):
        _write(_note_writer(database), dict(_NOTE))
    assert database.read_transactions == 1
    assert database.statements == [VOCABULARY_HEAD, DECLARED_NAMES, writer_module._BY_SOURCE_KEY]


def test_the_prefetched_row_resolves_duplicates_and_conflicts_in_step_5() -> None:
    content_hash = hashlib.sha256(canonical_bytes_sync(dict(_NOTE))).hexdigest()
    existing = _existing(content_hash)
    duplicate = _OneReadDatabase(existing)
    receipt = _write(_note_writer(duplicate), dict(_NOTE))
    assert isinstance(receipt, Receipt)
    assert receipt.status is AcceptanceStatus.ACCEPTED_DUPLICATE
    assert receipt.record_id == existing.record_id
    conflict = _OneReadDatabase(_existing("0" * 64))
    rejection = _write(_note_writer(conflict), dict(_NOTE))
    assert isinstance(rejection, LedgerRejection)
    assert rejection.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT
    assert (duplicate.read_transactions, conflict.read_transactions) == (1, 1)


def test_free_text_is_still_rejected_before_the_source_key_conflict() -> None:
    """El orden fijo no cambia: la fila ya leída del paso 5 no adelanta su rechazo al paso 4."""
    database = _OneReadDatabase(_existing("0" * 64))
    rejection = _write(_note_writer(database), {"probe_key": "k1", "note": "<b>guarda</b>"})
    assert isinstance(rejection, LedgerRejection)
    assert rejection.code is LedgerRejectionCode.FREE_TEXT_REJECTED


def test_without_preparers_the_source_key_keeps_its_own_read() -> None:
    database = _OneReadDatabase()
    with pytest.raises(AssertionError, match="paso 7"):
        _write(_note_writer(database, preparers=False), dict(_NOTE))
    assert database.statements == [writer_module._BY_SOURCE_KEY]
    assert database.read_transactions == 1
