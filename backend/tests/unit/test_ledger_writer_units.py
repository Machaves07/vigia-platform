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
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
from pydantic import Field, StrictInt, ValidationError
from vigia_contracts.models.enumerations import AcceptanceStatus, RejectionCode

from tests.writer_support import unit_context
from vigia_platform.ledger import canonical
from vigia_platform.ledger.application.audit_writer import (
    MAX_FILTERS_BYTES,
    AuditOperation,
    AuditRejected,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import (
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
from vigia_platform.ledger.registry import ChainLevel, ContentModel, RecordType, RecordTypeRegistry
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit, ContextAbsent

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
