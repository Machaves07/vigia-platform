"""PR-NUC-22: la evidencia se acepta si y solo si existe, tamaño y hash coinciden y lleva la marca.

Cada combinación generada de fallos produce su código específico (BR-NUC-64), en el orden fijo
existencia (con la clave coherente, BR-NUC-65) → tamaño y suma → marca de anonimización. Aquí
el almacén es un doble en memoria que devuelve los metadatos que daría ``HEAD``; la misma
propiedad contra LocalStack con sumas de verificación reales está en
``tests/integration/test_storage_timeouts.py`` (``test_pr_nuc_22_against_localstack``).

La parte "ninguna escritura parcial" de PR-NUC-22 es del escritor (TASK-113).
"""

from __future__ import annotations

import asyncio
import base64
import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.clock import SimulatedClock
from vigia_contracts.models.clip_reference import ClipReference
from vigia_contracts.models.enumerations import RejectionCode

from tests.properties.evidence_strategies import IMAGE, VIDEO, clip_reference, owners, uuid7s
from vigia_platform.ledger.evidence import (
    CODE_BY_FAILURE,
    CONTRACT_REJECTION_CODE,
    EvidenceCheck,
    EvidenceCode,
    EvidenceFailure,
    EvidenceOwner,
    EvidenceVerifier,
    first_failure,
)
from vigia_platform.shared.storage import (
    ChecksumType,
    ObjectHead,
    StorageUnavailable,
)

START = datetime(2026, 9, 29, 12, tzinfo=UTC)


class Fault(enum.StrEnum):
    """Fallos que se pueden combinar sobre una misma referencia."""

    KEY_INVALID = "key_invalid"
    KEY_FOREIGN = "key_foreign"
    ABSENT = "absent"
    SIZE = "size"
    CHECKSUM_ABSENT = "checksum_absent"
    CHECKSUM_COMPOSITE = "checksum_composite"
    CHECKSUM_OTHER = "checksum_other"
    MARKER_ABSENT = "marker_absent"
    MARKER_OTHER = "marker_other"


PRECEDENCE: tuple[tuple[Fault, EvidenceFailure], ...] = (
    (Fault.KEY_INVALID, EvidenceFailure.STORAGE_KEY_INVALID),
    (Fault.KEY_FOREIGN, EvidenceFailure.STORAGE_KEY_MISMATCH),
    (Fault.ABSENT, EvidenceFailure.OBJECT_ABSENT),
    (Fault.SIZE, EvidenceFailure.SIZE_MISMATCH),
    (Fault.CHECKSUM_ABSENT, EvidenceFailure.CHECKSUM_ABSENT),
    (Fault.CHECKSUM_COMPOSITE, EvidenceFailure.CHECKSUM_ABSENT),
    (Fault.CHECKSUM_OTHER, EvidenceFailure.CHECKSUM_MISMATCH),
    (Fault.MARKER_ABSENT, EvidenceFailure.MARKER_ABSENT),
    (Fault.MARKER_OTHER, EvidenceFailure.MARKER_INVALID),
)
"""Oráculo: el primer fallo presente en este orden fija el motivo."""

MARKER_VALUES = st.sampled_from(["0", "true", "yes", "01", " 1", "1 ", "TRUE", "2"])


def expected_failure(faults: frozenset[Fault]) -> EvidenceFailure | None:
    return next((failure for fault, failure in PRECEDENCE if fault in faults), None)


@dataclass
class FakeStorage:
    """``head_object`` en memoria; registra las claves consultadas y cuántas hay en vuelo."""

    heads: dict[str, ObjectHead] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)
    queried: list[str] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    async def head_object(self, key: str) -> ObjectHead | None:
        self.queried.append(key)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if key in self.unavailable:
                raise StorageUnavailable("head_object")
            return self.heads.get(key)
        finally:
            self.in_flight -= 1


@dataclass(frozen=True)
class Case:
    reference: ClipReference
    faults: frozenset[Fault]
    head: ObjectHead | None


def _b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


@st.composite
def cases(draw: st.DrawFn, owner: EvidenceOwner) -> Case:
    faults = frozenset(draw(st.sets(st.sampled_from(list(Fault)), max_size=4)))
    clip_id = draw(uuid7s())
    content_type = draw(st.sampled_from([VIDEO, IMAGE]))
    sha = draw(st.binary(min_size=32, max_size=32)).hex()
    size = draw(st.integers(min_value=1, max_value=52_428_800))
    reference = clip_reference(
        owner, clip_id=clip_id, content_type=content_type, sha256=sha, size_bytes=size
    )
    key = reference.storage_key
    if Fault.KEY_INVALID in faults:
        key = draw(st.sampled_from([key + "/", key.upper(), "documents/" + key, key[:-4]]))
    elif Fault.KEY_FOREIGN in faults:
        foreign = draw(owners())
        key = key.replace(str(owner.node_id), str(foreign.node_id))
        if foreign.node_id == owner.node_id:
            faults = faults - {Fault.KEY_FOREIGN}
    if key != reference.storage_key:
        reference = reference.model_copy(update={"storage_key": key})
    if Fault.ABSENT in faults:
        return Case(reference, faults, None)

    stored_size = size + draw(st.sampled_from([-1, 1, size])) if Fault.SIZE in faults else size
    checksum: str | None = _b64(sha)
    checksum_type: ChecksumType | None = ChecksumType.FULL_OBJECT
    if Fault.CHECKSUM_ABSENT in faults:
        checksum, checksum_type = None, None
    elif Fault.CHECKSUM_COMPOSITE in faults:
        checksum, checksum_type = _b64(sha) + "-2", ChecksumType.COMPOSITE
    elif Fault.CHECKSUM_OTHER in faults:
        other = draw(st.binary(min_size=32, max_size=32).filter(lambda b: b.hex() != sha)).hex()
        checksum = _b64(other)
    metadata: dict[str, str] = {"vigia-anonymized": "1"}
    if Fault.MARKER_ABSENT in faults:
        metadata = draw(st.sampled_from([{}, {"vigia-anonymised": "1"}, {"anonymized": "1"}]))
    elif Fault.MARKER_OTHER in faults:
        metadata = {"vigia-anonymized": draw(MARKER_VALUES)}
    head = ObjectHead(
        key=key,
        size_bytes=stored_size,
        checksum_sha256=checksum,
        checksum_type=checksum_type,
        content_type=content_type,
        metadata=metadata,
        version_id="v1",
    )
    return Case(reference, faults, head)


@st.composite
def records(draw: st.DrawFn) -> tuple[EvidenceOwner, list[Case]]:
    owner = draw(owners())
    # Cada clip de un registro es distinto: la misma clave no puede tener dos objetos.
    unique_clip = lambda case: case.reference.clip_id  # noqa: E731
    return owner, draw(st.lists(cases(owner), min_size=0, max_size=4, unique_by=unique_clip))


def _storage_for(items: Sequence[Case]) -> FakeStorage:
    storage = FakeStorage()
    for item in items:
        if item.head is not None:
            storage.heads[item.head.key] = item.head
    return storage


def _verify(
    storage: FakeStorage, owner: EvidenceOwner, refs: Sequence[ClipReference]
) -> list[EvidenceCheck]:
    verifier = EvidenceVerifier(storage, SimulatedClock(START))
    return asyncio.run(verifier.verify_references(owner, refs))


OWNER = EvidenceOwner(
    uuid.UUID("0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a01"),
    uuid.UUID("0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a02"),
    uuid.UUID("0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a03"),
    uuid.UUID("0b6f1a52-5c1e-4a55-9d0e-2f1d1f4e8a04"),
)
CLIPS = [uuid.UUID(f"01920000-0000-7000-8000-00000000000{n}") for n in range(1, 4)]


@given(records())
def test_each_combination_of_faults_produces_its_code(
    record: tuple[EvidenceOwner, list[Case]],
) -> None:
    owner, items = record
    storage = _storage_for(items)
    checks = _verify(storage, owner, [item.reference for item in items])

    assert [check.reference for check in checks] == [item.reference for item in items]
    for item, check in zip(items, checks, strict=True):
        expected = expected_failure(item.faults)
        assert check.failure is expected, item.faults
        assert check.ok is (expected is None)
        assert check.code is (None if expected is None else CODE_BY_FAILURE[expected])
        assert check.verified_at == START
    # Con la clave coherente, cada referencia se consulta una vez; con clave incoherente, ninguna.
    coherent = [
        item.reference.storage_key
        for item in items
        if not item.faults & {Fault.KEY_INVALID, Fault.KEY_FOREIGN}
    ]
    assert sorted(storage.queried) == sorted(coherent)
    failed = [item for item in items if expected_failure(item.faults) is not None]
    first = first_failure(checks)
    if failed:
        assert first is not None
        assert first.reference == failed[0].reference
    else:
        assert first is None


@given(records())
def test_accepts_iff_everything_matches(record: tuple[EvidenceOwner, list[Case]]) -> None:
    owner, items = record
    checks = _verify(_storage_for(items), owner, [item.reference for item in items])
    assert (first_failure(checks) is None) is all(not item.faults for item in items)


@given(records(), st.data())
def test_unreachable_store_is_transient_never_missing(
    record: tuple[EvidenceOwner, list[Case]], data: st.DataObject
) -> None:
    owner, items = record
    coherent = [item for item in items if not item.faults & {Fault.KEY_INVALID, Fault.KEY_FOREIGN}]
    if not coherent:
        return
    storage = _storage_for(items)
    storage.unavailable.add(data.draw(st.sampled_from(coherent)).reference.storage_key)
    with pytest.raises(StorageUnavailable) as raised:
        _verify(storage, owner, [item.reference for item in items])
    assert raised.value.code == "storage_unavailable"
    assert raised.value.retryable is True


def _absent_refs() -> list[ClipReference]:
    return [
        clip_reference(OWNER, clip_id=clip, content_type=VIDEO, sha256="a" * 64, size_bytes=10)
        for clip in CLIPS
    ]


def test_references_of_a_record_are_queried_concurrently() -> None:
    storage = FakeStorage()
    checks = _verify(storage, OWNER, _absent_refs())
    assert storage.max_in_flight == len(CLIPS)
    assert [check.failure for check in checks] == [EvidenceFailure.OBJECT_ABSENT] * len(CLIPS)


def test_empty_reference_list_needs_no_query() -> None:
    storage = FakeStorage()
    assert _verify(storage, OWNER, []) == []
    assert storage.queried == []


def test_unexpected_error_is_not_swallowed() -> None:
    class Broken(FakeStorage):
        async def head_object(self, key: str) -> ObjectHead | None:
            raise RuntimeError("fallo inesperado")

    with pytest.raises(RuntimeError):
        _verify(Broken(), OWNER, _absent_refs())


def test_codes_translate_to_the_contract() -> None:
    """``domain-entities.md`` §3.6: el puerto traduce al ``RejectionResponse`` del nodo."""
    expected: Mapping[EvidenceCode, RejectionCode] = {
        EvidenceCode.EVIDENCE_MISSING: RejectionCode.CLIP_MISSING,
        EvidenceCode.EVIDENCE_HASH_MISMATCH: RejectionCode.CLIP_HASH_MISMATCH,
        EvidenceCode.EVIDENCE_NOT_ANONYMIZED: RejectionCode.CLIP_NOT_ANONYMIZED,
    }
    assert dict(CONTRACT_REJECTION_CODE) == expected
    assert set(CODE_BY_FAILURE) == set(EvidenceFailure)
    assert StorageUnavailable("x").code == RejectionCode.STORAGE_UNAVAILABLE.value
