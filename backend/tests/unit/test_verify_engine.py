"""Bordes puros del motor de verificación (TASK-118): muestra, resultado auditado y forma canónica.

- ``sample_sequences``: el 1 % y al menos 100 por lote, sin repetir y dentro del lote; todo el lote
  si tiene menos de 100; reproducible desde la semilla.
- ``IntegrityResult.from_filters``: lectura estricta del resultado auditado (claves exactas,
  enteros no negativos, hashes y semilla con su patrón, coherencia ``result``/``broken_sequence``).
- ``canonical_break``: documento canónico, no canónico, ilegible, anidado en exceso o con enteros
  fuera del rango seguro; nunca una excepción sin controlar.
- ``event_payload`` solo para ``broken``; ``batch_size`` entero positivo.

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.canonical import canonicalize

from vigia_platform.ledger.chain.checkpoints import CheckpointChain
from vigia_platform.ledger.chain.verify import (
    SAMPLE_MINIMUM,
    CanonicalRow,
    IntegrityResult,
    IntegrityService,
    IntegrityStatus,
    VerificationMode,
    canonical_break,
    event_payload,
    sample_sequences,
)
from vigia_platform.shared.clock import SimulatedClock

PLANT = uuid.UUID("6f6e6d6c-7b7a-4988-9c9d-0e0f0a0b0c0d")


@pytest.mark.parametrize(
    ("size", "wanted"),
    [(0, 0), (1, 1), (99, 99), (100, 100), (101, 100), (10_000, 100), (10_001, 101)],
)
def test_sample_size_bounds(size: int, wanted: int) -> None:
    sample = sample_sequences(5, 5 + size - 1, random.Random(1))  # noqa: S311
    assert len(sample) == wanted
    assert sample == sorted(set(sample))
    assert all(5 <= sequence < 5 + size for sequence in sample)


@given(first=st.integers(1, 10**9), size=st.integers(1, 30_000), seed=st.integers(0, 2**128))
def test_sample_is_reproducible_and_inside_the_batch(first: int, size: int, seed: int) -> None:
    last = first + size - 1
    sample = sample_sequences(first, last, random.Random(seed))  # noqa: S311
    assert sample == sample_sequences(first, last, random.Random(seed))  # noqa: S311
    assert len(sample) == min(size, max(SAMPLE_MINIMUM, -(-size // 100)))
    assert first <= sample[0] and sample[-1] <= last


def _result(status: IntegrityStatus = IntegrityStatus.BROKEN) -> IntegrityResult:
    broken = status is IntegrityStatus.BROKEN
    return IntegrityResult(
        chain=CheckpointChain.plant(PLANT),
        mode=VerificationMode.INCREMENTAL,
        status=status,
        from_sequence=5,
        to_sequence=6 if broken else 9,
        verified_hash=None if broken else "a" * 64,
        head_sequence=9,
        broken_sequence=7 if broken else None,
        broken_entry_id=None,
        reason="record_hash_mismatch" if broken else None,
        canonical_checked=3,
        checkpoints_checked=1,
        sample_seed="0" * 32,
    )


@pytest.mark.parametrize("status", list(IntegrityStatus))
def test_filters_round_trip(status: IntegrityStatus) -> None:
    result = _result(status)
    assert IntegrityResult.from_filters(result.to_filters()) == result


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("mode"),
        lambda d: d.update(extra=1),
        lambda d: d.update(mode="weekly"),
        lambda d: d.update(result="empty"),
        lambda d: d.update(chain_kind="other"),
        lambda d: d.update(from_sequence=-1),
        lambda d: d.update(from_sequence=True),
        lambda d: d.update(to_sequence=1.0),
        lambda d: d.update(sample_seed="0" * 31),
        lambda d: d.update(sample_seed="G" * 32),
        lambda d: d.update(verified_hash="A" * 64),
        lambda d: d.update(reason=3),
        lambda d: d.update(broken_sequence=None),
        lambda d: d.update(plant_id="planta"),
    ],
)
def test_filters_are_read_strictly(change: Any) -> None:
    document = _result().to_filters()
    change(document)
    with pytest.raises(ValueError):
        IntegrityResult.from_filters(document)


def test_intact_without_verified_hash_is_incoherent() -> None:
    document = _result(IntegrityStatus.INTACT).to_filters()
    document["verified_hash"] = None
    with pytest.raises(ValueError, match="incoherente"):
        IntegrityResult.from_filters(document)


def test_event_payload_only_for_broken() -> None:
    moment = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
    assert event_payload(_result(), moment) == {
        "chain_kind": "ledger",
        "first_failed_sequence": 7,
        "verification_mode": "incremental",
        "detected_at": "2026-09-30T01:00:00.000Z",
    }
    with pytest.raises(ValueError):
        event_payload(_result(IntegrityStatus.INTACT), moment)


def _row(document: str | None, content: bytes | None = None) -> CanonicalRow:
    digest = None if content is None else hashlib.sha256(content).hexdigest()
    return CanonicalRow(sequence=4, entry_id=str(PLANT), document=document, content_hash=digest)


def test_canonical_break_cases() -> None:
    canonical = canonicalize({"b": 1, "a": [2, "é"]})
    assert canonical_break(_row('{"a": [2, "é"], "b": 1}', canonical)) is None
    assert canonical_break(_row(None)) is None
    other = b'{"b":1,"a":[2,"\\u00e9"]}'
    failure = canonical_break(_row('{"a": [2, "é"], "b": 1}', other))
    assert failure is not None
    assert (failure.sequence, failure.reason) == (4, "content_not_canonical")


@pytest.mark.parametrize(
    "document",
    [
        "NaN",
        '{"a": Infinity}',
        "[" * 100_000 + "]" * 100_000,
        '{"n": ' + "9" * 5_000 + "}",
        "no es json",
        '{"a": "\\ud800"}',
    ],
)
def test_canonical_break_never_raises(document: str) -> None:
    failure = canonical_break(_row(document, b"{}"))
    assert failure is not None and failure.reason == "content_not_canonical"


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_batch_size_must_be_a_positive_integer(size: Any) -> None:
    with pytest.raises(ValueError):
        IntegrityService(
            store=None,  # type: ignore[arg-type]
            keys=None,  # type: ignore[arg-type]
            clock=SimulatedClock(datetime(2026, 9, 30, tzinfo=UTC)),
            batch_size=size,
        )
