"""Piezas puras del escritor y de la auditoría, sin base (TASK-113).

- Rutas declaradas sobre el contenido (``ledger.content_paths``): punteros concretos, ``[*]``,
  ausentes y ``null``, y la forma de ``field`` del contrato.
- Traducción de ``LedgerRejection`` al ``RejectionResponse`` del contrato para U-03 y ``Receipt``
  del contrato.
- ``AuditWriter`` valida todo antes de tocar la base: operación y resultado de sus listas,
  ``filters`` ≤ 4 KB, ``result_count``, ``resource_ref`` y la cadena de la proveedora para los
  eventos sin organización.
- Seguimientos que VIG-40 y VIG-46 dejaron para el escritor: la validación no depende de un
  ``model_validate_json`` redefinido, y ``LARGE_DOCUMENT_BYTES`` vale 16 KB.
- VIG-129: la auditoría rechaza una transacción de otra organización sin ejecutar nada (M10);
  ``filters`` enorme se rechaza sin canonicalizarlo; el paso 3 del escritor (esquema, tope de
  ``source_key``, regla de etiqueta) fija ``content_invalid`` antes del texto libre y de cualquier
  consulta a la base; un contenido de más de 16 KB se valida en el pool de CPU.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any, Literal, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import Field, StrictInt, StrictStr, ValidationError
from vigia_contracts.models.common import UUID
from vigia_contracts.models.enumerations import AcceptanceStatus, RejectionCode

from tests.hibp_service import metrics_with_reader
from tests.writer_support import unit_context
from vigia_platform.ledger import canonical
from vigia_platform.ledger.application import audit_writer
from vigia_platform.ledger.application.audit_writer import (
    MAX_FILTERS_BYTES,
    AuditOperation,
    AuditRejected,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    to_contract_rejection,
)
from vigia_platform.ledger.content_paths import (
    ContentPathError,
    contract_field,
    iter_segments,
    locate,
    pointer,
    replace,
)
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.registry import (
    ChainLevel,
    ContentModel,
    LabelRule,
    RecordType,
    RecordTypeRegistry,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit, ContextAbsent
from vigia_platform.shared.db import (
    ChainLockedTimeout,
    Database,
    TemporarilyUnavailable,
    Transaction,
)
from vigia_platform.shared.observability.metrics import MetricName

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)


# --- content_paths ---------------------------------------------------------------------------

_DOCUMENT: dict[str, Any] = {
    "note": "a",
    "cameras": [
        {"clips": [{"k": 1}, {"k": 2}]},
        {"clips": []},
        {"clips": [None, {"k": 3}]},
    ],
    "absent": None,
}


def test_locate_expands_lists_and_skips_absent_and_null() -> None:
    found = locate(_DOCUMENT, "/cameras[*]/clips[*]/k")
    assert [(f.pointer, f.value) for f in found] == [
        ("/cameras/0/clips/0/k", 1),
        ("/cameras/0/clips/1/k", 2),
        ("/cameras/2/clips/1/k", 3),
    ]
    assert locate(_DOCUMENT, "/absent") == []
    assert locate(_DOCUMENT, "/missing/deeper") == []
    assert locate(_DOCUMENT, "/note[*]") == []  # no es una lista
    assert [f.pointer for f in locate(_DOCUMENT, "/note")] == ["/note"]


@pytest.mark.parametrize("path", ["", "note", "/Note", "/note/", "/a[*][*]", "/a b", "//a"])
def test_malformed_paths_are_refused(path: str) -> None:
    with pytest.raises(ContentPathError):
        locate(_DOCUMENT, path)


def test_replace_and_pointer_round_trip() -> None:
    document = json.loads(json.dumps(_DOCUMENT))
    target = locate(document, "/cameras[*]/clips[*]/k")[2]
    replace(document, target.segments, 30)
    assert document["cameras"][2]["clips"][1]["k"] == 30
    assert list(iter_segments(pointer(("a/b", "c~d", 0)))) == ["a/b", "c~d", 0]
    assert list(iter_segments("")) == []


def test_contract_field_form() -> None:
    assert contract_field(["cameras", 0, "clips", 1, "sha256"]) == "cameras[0].clips[1].sha256"
    assert contract_field(["level"]) == "level"
    assert contract_field([]) is None
    assert contract_field(["x" * 300]) is None
    assert contract_field(["a b"]) is None


# --- traducción al contrato ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        (LedgerRejectionCode.RECORD_TYPE_UNKNOWN, RejectionCode.SCHEMA_INVALID, False),
        (LedgerRejectionCode.CONTENT_INVALID, RejectionCode.SCHEMA_INVALID, False),
        (LedgerRejectionCode.FREE_TEXT_REJECTED, RejectionCode.SCHEMA_INVALID, False),
        (LedgerRejectionCode.IDEMPOTENCY_CONFLICT, RejectionCode.IDEMPOTENCY_CONFLICT, False),
        (LedgerRejectionCode.EVIDENCE_MISSING, RejectionCode.CLIP_MISSING, False),
        (LedgerRejectionCode.EVIDENCE_HASH_MISMATCH, RejectionCode.CLIP_HASH_MISMATCH, False),
        (LedgerRejectionCode.EVIDENCE_NOT_ANONYMIZED, RejectionCode.CLIP_NOT_ANONYMIZED, False),
        (LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT, RejectionCode.TEMPORARILY_UNAVAILABLE, True),
    ],
)
def test_rejections_translate_to_the_contract(
    code: LedgerRejectionCode, expected: RejectionCode, retryable: bool
) -> None:
    response = to_contract_rejection(LedgerRejection.of(code, "/cameras/0/clips/1/sha256"))
    assert response.code is expected
    assert response.retryable is retryable
    assert response.field == "cameras[0].clips[1].sha256"
    assert (response.retry_after_seconds is not None) is retryable


def test_context_absent_has_no_contract_code() -> None:
    with pytest.raises(ValueError, match="context_absent"):
        to_contract_rejection(LedgerRejection.of(LedgerRejectionCode.CONTEXT_ABSENT))


def test_rejection_messages_are_generic_and_bounded() -> None:
    for code in LedgerRejectionCode:
        rejection = LedgerRejection.of(code, "/note")
        assert 0 < len(rejection.message_es) <= 512


def test_receipt_to_contract() -> None:
    receipt = Receipt(
        record_id=uuid.UUID("018cc251-f400-7000-8000-000000000001"),
        received_at=datetime(2026, 9, 29, 10, 30, 0, 123456, tzinfo=UTC),
        status=AcceptanceStatus.ACCEPTED_DUPLICATE,
    )
    contract = receipt.to_contract()
    assert contract.received_at == "2026-09-29T10:30:00.123Z"
    assert contract.platform_record_id == str(receipt.record_id)
    assert contract.status is AcceptanceStatus.ACCEPTED_DUPLICATE


# --- AuditWriter sin base --------------------------------------------------------------------


class _NoDatabase:
    """Cualquier uso de la base es un fallo: la validación va antes."""

    def transaction(self, context: Any) -> Any:
        raise AssertionError("la auditoría tocó la base con una entrada inválida")

    async def read(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("la auditoría leyó la base")


def _audit() -> AuditWriter:
    return AuditWriter(
        database=_NoDatabase(), clock=SimulatedClock(NOW), provider_organization_id=uuid.uuid4()
    )


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"operation": "delete_everything"}, "audit_operation_unknown"),
        ({"outcome": "maybe"}, "audit_entry_invalid"),
        ({"filters": {"q": "x" * MAX_FILTERS_BYTES}}, "filters_too_large"),
        ({"filters": {"n": float("nan")}}, "audit_entry_invalid"),
        ({"result_count": -1}, "audit_entry_invalid"),
        ({"result_count": 2**31}, "audit_entry_invalid"),
        ({"result_count": True}, "audit_entry_invalid"),
        ({"resource": ResourceRef(kind="Ledger Record", id=uuid.uuid4())}, "audit_entry_invalid"),
        ({"plant_id": "no-uuid"}, "audit_entry_invalid"),
    ],
)
def test_invalid_audit_entries_are_refused_before_the_database(
    changes: dict[str, Any], code: str
) -> None:
    arguments: dict[str, Any] = {"operation": AuditOperation.LEDGER_READ}
    arguments.update(changes)
    operation = arguments.pop("operation")
    context = unit_context(uuid.uuid4(), ActorUnit.U02)
    with pytest.raises(AuditRejected) as raised:
        asyncio.run(_audit().append(context, operation, **arguments))
    assert raised.value.code == code


def test_filters_at_the_limit_pass_validation() -> None:
    writer = _audit()
    context = unit_context(uuid.uuid4(), ActorUnit.U02)
    # {"q":"…"} ocupa 8 bytes más el texto: justo 4 096 pasa y llega a la base.
    filters = {"q": "x" * (MAX_FILTERS_BYTES - 8)}
    with pytest.raises(AssertionError, match="tocó la base"):
        asyncio.run(writer.append(context, AuditOperation.LEDGER_READ, filters=filters))


def test_audit_without_context_or_outside_the_provider_chain() -> None:
    writer = _audit()
    with pytest.raises(ContextAbsent):
        asyncio.run(writer.append(None, AuditOperation.LEDGER_READ))  # type: ignore[arg-type]
    other = unit_context(uuid.uuid4(), ActorUnit.U02)
    with pytest.raises(AuditRejected, match="proveedora"):
        asyncio.run(writer.append_without_organization(other, AuditOperation.LOGIN_FAILED))


# --- Seguimientos de VIG-40 y VIG-46 -----------------------------------------------------------


class _Lenient(ContentModel):
    level: Annotated[StrictInt, Field(ge=0, le=10)]

    @classmethod
    def model_validate_json(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return cls.model_construct(level=0)


def test_validation_does_not_depend_on_an_overridden_model_validate_json() -> None:
    compiled = RecordTypeRegistry().register(
        RecordType(
            record_type="lenient_probe",
            writer_unit=ActorUnit.U02,
            chain_level=ChainLevel.ORGANIZATION,
            schema_version=1,
            content_model=_Lenient,
        )
    )
    with pytest.raises(ValidationError):
        compiled.validate_json('{"level": "not an int", "extra": 1}')


def test_large_document_threshold_is_16_kib() -> None:
    assert canonical.LARGE_DOCUMENT_BYTES == 16 * 1024


def test_exceeds_canonical_size_bounds() -> None:
    assert not canonical.exceeds_canonical_size({"a": "x" * 10}, 20)
    assert canonical.exceeds_canonical_size({"a": "x" * 30}, 20)
    assert canonical.exceeds_canonical_size({"a": list(range(10**6))}, 1024)


# --- VIG-129: auditoría en una transacción de otra organización (mutación M10) ----------------


class _RecordingConnection:
    """Conexión falsa: cuenta las sentencias; la auditoría no debe llegar a ejecutar ninguna."""

    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.executed += 1
        raise AssertionError("la auditoría ejecutó una sentencia")


def _transaction_of(organization_id: uuid.UUID) -> tuple[Transaction, _RecordingConnection]:
    connection = _RecordingConnection()
    context = unit_context(organization_id, ActorUnit.U02)
    return Transaction(connection, cast(Database, _NoDatabase()), context), connection


def test_audit_in_a_transaction_of_another_organization_is_refused() -> None:
    writer = _audit()
    context = unit_context(uuid.uuid4(), ActorUnit.U02)
    foreign, connection = _transaction_of(uuid.uuid4())
    with pytest.raises(AuditRejected, match="otra organización") as raised:
        asyncio.run(writer.append(context, AuditOperation.LEDGER_READ, transaction=foreign))
    assert raised.value.code == "audit_entry_invalid"
    assert connection.executed == 0


def test_audit_in_a_transaction_of_the_same_organization_reaches_the_statement() -> None:
    """Control de la prueba anterior: con la misma organización sí se ejecuta la sentencia."""
    writer = _audit()
    organization_id = uuid.uuid4()
    own, _ = _transaction_of(organization_id)
    context = unit_context(organization_id, ActorUnit.U02)
    with pytest.raises(AttributeError):
        # ``_NoDatabase`` no tiene ``_within``: basta con ver que la entrada llegó a ejecutarse.
        asyncio.run(writer.append(context, AuditOperation.LEDGER_READ, transaction=own))


# --- VIG-129: ``filters`` se acota antes de canonicalizar -------------------------------------


def test_huge_filters_are_refused_without_canonicalizing(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(document: Any) -> bytes:
        raise AssertionError("se canonicalizó un filters enorme")

    monkeypatch.setattr(audit_writer, "canonical_bytes_sync", never)
    context = unit_context(uuid.uuid4(), ActorUnit.U02)
    filters: dict[str, Any] = {"zones": [str(uuid.uuid4())] * 50_000}
    with pytest.raises(AuditRejected) as raised:
        asyncio.run(_audit().append(context, AuditOperation.LEDGER_READ, filters=filters))
    assert raised.value.code == "filters_too_large"


@pytest.mark.parametrize(
    "filters",
    [
        {"f": [0.0] * 1000},  # la cota cuenta 25 B por doble; ocupan 2 B con la coma
        {"q": "\u00a0" * 1000},  # no imprimible: la cota cuenta 6 B; en UTF-8 ocupa 2
    ],
    ids=["floats", "non_printable"],
)
def test_filters_whose_bound_overestimates_are_measured_exactly(filters: dict[str, Any]) -> None:
    """La cota sin serializar pasa de 4 KB, pero los bytes canónicos caben: llega a la base."""
    assert canonical.exceeds_canonical_size(filters, MAX_FILTERS_BYTES)
    assert len(canonical.canonical_bytes_sync(filters)) <= MAX_FILTERS_BYTES
    context = unit_context(uuid.uuid4(), ActorUnit.U02)
    with pytest.raises(AssertionError, match="tocó la base"):
        asyncio.run(_audit().append(context, AuditOperation.LEDGER_READ, filters=filters))


def test_filters_just_over_the_limit_are_too_large() -> None:
    context = unit_context(uuid.uuid4(), ActorUnit.U02)
    filters = {"q": "x" * (MAX_FILTERS_BYTES - 7)}
    with pytest.raises(AuditRejected) as raised:
        asyncio.run(_audit().append(context, AuditOperation.LEDGER_READ, filters=filters))
    assert raised.value.code == "filters_too_large"


# --- VIG-129: el paso 3 completo antes de cualquier consulta a la base (BR-NUC-44) ------------

_KEY = Annotated[StrictStr, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]{1,100}$")]
_CATEGORY = Annotated[StrictStr, Field(min_length=2, max_length=32, pattern=r"^[a-z0-9_-]{2,32}$")]


class _Signer(ContentModel):
    user_id: UUID
    role: Literal["coordinator_sst", "copasst"]


class _Step3Probe(ContentModel):
    """Tipo que el registro acepta y cuyo paso 3 va más allá del esquema: la clave admite hasta
    100 caracteres (el escritor, 64) y la categoría admite «-» (la etiqueta exige snake_case)."""

    probe_key: _KEY
    plant_id: UUID
    zone_id: UUID
    anchor_record_id: UUID
    family: Literal["dwell", "coexistence"]
    outcome: Literal["confirmed", "false_positive"]
    reason_category: _CATEGORY
    level: Annotated[StrictInt, Field(ge=0, le=10)]
    signer: _Signer
    note: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    codes: Annotated[tuple[_KEY, ...], Field(max_length=4000)] = ()


_STEP3_TYPE = "step3_probe"


def _step3_writer(cpu_pool: Any = None) -> EscritorExpediente:
    registry = RecordTypeRegistry()
    registry.register(
        RecordType(
            record_type=_STEP3_TYPE,
            writer_unit=ActorUnit.U04,
            chain_level=ChainLevel.PLANT,
            schema_version=1,
            content_model=_Step3Probe,
            source_key_path="/probe_key",
            free_text_paths=("/note",),
            label_rule=LabelRule(
                subject_record_path="/anchor_record_id",
                family_path="/family",
                outcome_path="/outcome",
                reason_category_path="/reason_category",
                labeled_by_path="/signer",
            ),
        )
    )
    return EscritorExpediente(
        database=_NoDatabase(),
        registry=registry,
        free_text=FreeTextPolicyRegistry(),
        evidence=cast(Any, None),
        outbox=cast(Any, None),
        clock=SimulatedClock(NOW),
        cpu_pool=cpu_pool,
    )


def _step3_document() -> dict[str, Any]:
    return {
        "probe_key": "k1",
        "plant_id": str(uuid.uuid4()),
        "zone_id": str(uuid.uuid4()),
        "anchor_record_id": str(uuid.uuid4()),
        "family": "coexistence",
        "outcome": "confirmed",
        "reason_category": "guard_open",
        "level": 3,
        "signer": {"user_id": str(uuid.uuid4()), "role": "coordinator_sst"},
        "note": "Revisión de la guarda norte",
    }


_STEP3_FAILURES: dict[str, tuple[LedgerRejectionCode, str]] = {
    # En el orden del escritor: esquema, clave, etiqueta (paso 3) y texto libre (paso 4).
    "schema": (LedgerRejectionCode.CONTENT_INVALID, "/level"),
    "source_key_65": (LedgerRejectionCode.CONTENT_INVALID, "/probe_key"),
    "label_rule": (LedgerRejectionCode.CONTENT_INVALID, "/reason_category"),
    "free_text": (LedgerRejectionCode.FREE_TEXT_REJECTED, "/note"),
}


def _break(document: dict[str, Any], failure: str) -> None:
    if failure == "schema":
        document["level"] = 11
    elif failure == "source_key_65":
        document["probe_key"] = "a" * 65
    elif failure == "label_rule":
        document["reason_category"] = "guard-open"
    else:
        document["note"] = "guarda <b>norte</b>"


@given(failures=st.sets(st.sampled_from(list(_STEP3_FAILURES)), min_size=1))
def test_step_three_codes_come_first_and_never_reach_the_database(failures: set[str]) -> None:
    """Con cualquier combinación de fallos, el primero en el orden de BR-NUC-44 fija el código y
    ninguno consulta la base (``_NoDatabase`` falla ante cualquier lectura o transacción): una
    clave de 65 caracteres o una etiqueta imposible nunca llegan a idempotencia ni a evidencias."""
    document = _step3_document()
    for failure in failures:
        _break(document, failure)
    context = unit_context(uuid.uuid4(), ActorUnit.U04)
    result = asyncio.run(_step3_writer().write(context, _STEP3_TYPE, document))
    first = next(name for name in _STEP3_FAILURES if name in failures)
    assert isinstance(result, LedgerRejection), result
    assert (result.code, result.field) == _STEP3_FAILURES[first]


@pytest.mark.parametrize(("length", "reaches_database"), [(64, True), (65, False)])
def test_source_key_limit_is_checked_in_step_three(length: int, reaches_database: bool) -> None:
    document = _step3_document()
    document["probe_key"] = "a" * length
    context = unit_context(uuid.uuid4(), ActorUnit.U04)
    if reaches_database:
        with pytest.raises(AssertionError, match="leyó la base"):
            asyncio.run(_step3_writer().write(context, _STEP3_TYPE, document))
    else:
        result = asyncio.run(_step3_writer().write(context, _STEP3_TYPE, document))
        assert result == LedgerRejection.of(LedgerRejectionCode.CONTENT_INVALID, "/probe_key")


class _SpyPool:
    """``CpuPool`` síncrono que anota qué funciones se le enviaron."""

    def __init__(self) -> None:
        self.functions: list[str] = []

    async def run(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        self.functions.append(function.__name__)
        return function(*args, **kwargs)


@pytest.mark.parametrize("valid", [True, False])
def test_large_content_is_validated_in_the_cpu_pool(valid: bool) -> None:
    pool = _SpyPool()
    document = _step3_document()
    document["codes"] = ["abcdefgh"] * 2000  # unos 22 KB canónicos
    assert canonical.exceeds_canonical_size(document, canonical.LARGE_DOCUMENT_BYTES)
    if not valid:
        document["codes"][-1] = "NO"
    context = unit_context(uuid.uuid4(), ActorUnit.U04)
    writer = _step3_writer(pool)
    if valid:
        with pytest.raises(AssertionError, match="leyó la base"):
            asyncio.run(writer.write(context, _STEP3_TYPE, document))
    else:
        result = asyncio.run(writer.write(context, _STEP3_TYPE, document))
        assert result == LedgerRejection.of(LedgerRejectionCode.CONTENT_INVALID, "/codes/1999")
    assert pool.functions[0] == "_schema_checked"


def test_small_content_is_validated_in_the_event_loop() -> None:
    pool = _SpyPool()
    context = unit_context(uuid.uuid4(), ActorUnit.U04)
    with pytest.raises(AssertionError, match="leyó la base"):
        asyncio.run(_step3_writer(pool).write(context, _STEP3_TYPE, _step3_document()))
    assert pool.functions == []


# --- VIG-90: métricas de contención del paso 7 (PAT-NUC-RES-08, NFR-NUC-38) ---------------------

_POOL_WAIT_SECONDS = 1.0
_HELD_SECONDS = 0.25


class _TimedDatabase:
    """Abrir la transacción tarda ``_POOL_WAIT_SECONDS`` (la espera del pool, que no cuenta)."""

    def __init__(self, clock: SimulatedClock) -> None:
        self._clock = clock

    @contextlib.asynccontextmanager
    async def _transaction(self, context: Any) -> AsyncIterator[Any]:
        self._clock.advance(_POOL_WAIT_SECONDS)
        yield object()

    def transaction(self, context: Any) -> Any:
        return self._transaction(context)


def _metrics_writer(
    clock: SimulatedClock, outcome: BaseException | None
) -> tuple[EscritorExpediente, Any]:
    metrics, reader = metrics_with_reader()
    writer = EscritorExpediente(
        database=_TimedDatabase(clock),
        registry=RecordTypeRegistry(),
        free_text=FreeTextPolicyRegistry(),
        evidence=cast(Any, None),
        outbox=cast(Any, None),
        clock=clock,
        metrics=metrics,
    )

    async def insert(*_: Any) -> Any:
        clock.advance(_HELD_SECONDS)
        if outcome is not None:
            raise outcome
        return SimpleNamespace(record_id=uuid.uuid4(), received_at=NOW)

    writer._insert = insert  # type: ignore[method-assign]
    return writer, reader


def _points(reader: Any, name: MetricName) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:  # nada medido todavía
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name.value
        for point in metric.data.data_points
    ]


@pytest.mark.parametrize(
    ("chain_plant", "chain_kind"), [(uuid.uuid4(), "plant"), (None, "organization")]
)
def test_an_accepted_write_counts_one_write_and_its_lock_time(
    chain_plant: uuid.UUID | None, chain_kind: str
) -> None:
    clock = SimulatedClock(NOW)
    writer, reader = _metrics_writer(clock, None)
    prepared = cast(Any, SimpleNamespace(chain_plant=chain_plant))
    context = unit_context(uuid.uuid4(), ActorUnit.U03)
    result = asyncio.run(writer._commit(context, prepared, None))
    assert isinstance(result, Receipt)
    (write,) = _points(reader, MetricName.LEDGER_WRITES_TOTAL)
    assert (write.value, dict(write.attributes)) == (1, {"chain_kind": chain_kind})
    assert _points(reader, MetricName.CHAIN_LOCKED_TIMEOUT_TOTAL) == []
    (wait,) = _points(reader, MetricName.CHAIN_LOCK_WAIT_MS)
    # Solo lo que duró la transacción abierta, sin la espera del pool.
    assert (wait.count, wait.sum) == (1, _HELD_SECONDS * 1000)
    assert dict(wait.attributes) == {"chain_kind": chain_kind}


def test_a_chain_locked_timeout_counts_in_the_rate_and_its_wait() -> None:
    clock = SimulatedClock(NOW)
    writer, reader = _metrics_writer(clock, ChainLockedTimeout())
    prepared = cast(Any, SimpleNamespace(chain_plant=uuid.uuid4()))
    context = unit_context(uuid.uuid4(), ActorUnit.U03)
    result = asyncio.run(writer._commit(context, prepared, None))
    assert result == LedgerRejection.of(LedgerRejectionCode.CHAIN_LOCKED_TIMEOUT)
    (timeouts,) = _points(reader, MetricName.CHAIN_LOCKED_TIMEOUT_TOTAL)
    (writes,) = _points(reader, MetricName.LEDGER_WRITES_TOTAL)
    # La condición ``chain_locked_timeout_rate`` es el cociente de las dos (NFR-NUC-38).
    assert (timeouts.value, writes.value) == (1, 1)
    assert dict(timeouts.attributes) == {"chain_kind": "plant"}
    (wait,) = _points(reader, MetricName.CHAIN_LOCK_WAIT_MS)
    assert (wait.count, wait.sum) == (1, _HELD_SECONDS * 1000)


def test_any_other_failure_inside_the_transaction_is_not_a_chain_write() -> None:
    clock = SimulatedClock(NOW)
    writer, reader = _metrics_writer(clock, TemporarilyUnavailable())
    prepared = cast(Any, SimpleNamespace(chain_plant=uuid.uuid4()))
    context = unit_context(uuid.uuid4(), ActorUnit.U03)
    with pytest.raises(TemporarilyUnavailable):
        asyncio.run(writer._commit(context, prepared, None))
    for name in (
        MetricName.LEDGER_WRITES_TOTAL,
        MetricName.CHAIN_LOCKED_TIMEOUT_TOTAL,
        MetricName.CHAIN_LOCK_WAIT_MS,
    ):
        assert _points(reader, name) == []
