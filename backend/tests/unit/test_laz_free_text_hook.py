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
from vigia_platform.loop.adapters.postgres.declared_names import _NAMES as DECLARED_NAMES
from vigia_platform.loop.adapters.postgres.vocabulary import _HEAD as VOCABULARY_HEAD
from vigia_platform.loop.registration import FREE_TEXT_REGISTRARS
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
