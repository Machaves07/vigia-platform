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
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable

from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextPreparation,
    FreeTextRejected,
)
from vigia_platform.shared.clock import SimulatedClock

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


def test_the_base_policy_carries_no_detail_code() -> None:
    with pytest.raises(FreeTextRejected) as raised:
        FreeTextPolicyRegistry().apply("<b>x</b>", FIELD)
    assert (raised.value.reason, raised.value.detail_code) == ("markup", None)
