"""PR-NUC-32 (parte bandeja) y la carga sin texto libre (TASK-111; BR-NUC-75, 83).

- **Atomicidad**: para toda secuencia generada de publicaciones en una transacción (eventos con
  cero, uno o dos consumidores, con o sin planta y secuencia) y todo punto de fallo inyectado
  (una excepción del llamador tras la k-ésima publicación, o el fallo de la n-ésima sentencia),
  si la transacción no confirma no queda **ningún** ``OutboxEvent`` ni ``OutboxDelivery`` de la
  organización; si confirma, quedan exactamente un evento por publicación y una entrega
  ``pending`` por consumidor suscrito. Contra PostgreSQL 16 real, como ``vigia_app``.
- **La carga rechaza texto libre**: (a) todo modelo de carga generado con una cadena abierta
  (sin lista cerrada ni patrón cerrado, en la raíz, anidada, en una lista u opcional) se rechaza
  al registrar; (b) toda carga generada con un campo de más o con texto arbitrario en un campo
  cerrado se rechaza al publicar **sin enviar ninguna sentencia**, y el mensaje no repite el
  texto.

Semillas y ejemplos del perfil activo (``tests/conftest.py``).
"""

from __future__ import annotations

import re
import string
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import Field, StrictStr, create_model
from vigia_contracts.models.common import UUID

from tests.factories import make_context
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import DatabaseLoop
from tests.outbox_support import (
    BULK_EVENT,
    PROBE_EVENT,
    SOLO_EVENT,
    SUBSCRIBERS,
    InjectedFault,
    StatementFaults,
    app_database,
    outbox_counts,
    probe_catalog,
    probe_payload,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import Database, TransactionAborted
from vigia_platform.shared.outbox.publish import NewEvent, Outbox, OutboxRejected
from vigia_platform.shared.outbox.registries import (
    EventType,
    OutboxCatalog,
    OutboxRegistrationRejected,
    PayloadModel,
)
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Environment:
    loop: DatabaseLoop
    migrated: MigratedDatabase
    database: Database
    outbox: Outbox


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[Environment]:
    loop = DatabaseLoop()
    with migrated_database(postgres_endpoint, "outbox_atomicity") as migrated:
        database = app_database(migrated)
        catalog = probe_catalog()

        async def synchronize() -> None:
            async with database.transaction(make_context(kind=ActorKind.SYSTEM)) as transaction:
                await catalog.synchronize(SqlOutboxCatalogStore(transaction), SimulatedClock(NOW))

        try:
            loop.run(synchronize())
            yield Environment(loop, migrated, database, Outbox(catalog, SimulatedClock(NOW)))
        finally:
            loop.run(database.dispose())
            loop.close()


# --- PR-NUC-32: reversión inyectada ---------------------------------------------------------


@st.composite
def new_events(draw: st.DrawFn) -> NewEvent:
    name = draw(st.sampled_from([PROBE_EVENT, BULK_EVENT, SOLO_EVENT]))
    if name == BULK_EVENT:
        count = draw(st.integers(0, 20))
        payload: dict[str, Any] = {
            "zone_ids": [str(uuid.uuid4()) for _ in range(count)],
            "pad": draw(st.text(string.ascii_lowercase, max_size=64)),
        }
    else:
        payload = probe_payload(
            state=draw(st.sampled_from(["observable", "degraded", "unobservable"])),
            code=draw(st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)),
        )
    return NewEvent(
        event_name=name,
        payload=payload,
        plant_id=draw(st.none() | st.uuids(version=4)),
        ledger_sequence=draw(st.none() | st.integers(1, 2**63 - 1)),
    )


@dataclass(frozen=True)
class Fault:
    """``after_publish``: el llamador falla tras esa publicación; ``statement``: esa sentencia."""

    after_publish: int | None = None
    statement: int | None = None


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[list[NewEvent], Fault | None]:
    events = draw(st.lists(new_events(), min_size=1, max_size=5))
    statements = sum(1 + bool(SUBSCRIBERS[e.event_name]) for e in events)
    fault = draw(
        st.none()
        | st.builds(Fault, after_publish=st.integers(1, len(events)))
        | st.builds(Fault, statement=st.integers(1, statements))
    )
    return events, fault


@given(scenario=scenarios())
def test_publish_is_visible_iff_the_transaction_commits(
    environment: Environment, scenario: tuple[list[NewEvent], Fault | None]
) -> None:
    events, fault = scenario
    context = make_context(kind=ActorKind.USER)

    async def run() -> None:
        async with environment.database.transaction(context) as transaction:
            if fault is not None and fault.statement is not None:
                StatementFaults.install(transaction, fail_at=fault.statement)
            for index, event in enumerate(events, start=1):
                await environment.outbox.publish(transaction, event)
                if fault is not None and fault.after_publish == index:
                    raise InjectedFault("reversión inyectada tras publicar")

    if fault is None:
        environment.loop.run(run())
        expected = (len(events), sum(len(SUBSCRIBERS[e.event_name]) for e in events))
    else:
        with pytest.raises((InjectedFault, TransactionAborted)):
            environment.loop.run(run())
        expected = (0, 0)
    assert environment.loop.run(outbox_counts(environment.migrated, context.organization_id)) == (
        expected
    )


# --- La carga rechaza texto libre -----------------------------------------------------------

_OPEN_PATTERNS = (
    None,
    r"^[A-Za-z ]{1,40}$",
    r"^.{1,40}$",
    r"[a-z]+",
    r"^[a-z]+$|^.*$",
    r"^\w+$",
    r"^[^<]{1,40}$",
    r"^[a-z<>]{1,40}$",
)


@st.composite
def free_text_models(draw: st.DrawFn) -> type[PayloadModel]:
    pattern = draw(st.sampled_from(_OPEN_PATTERNS))
    field = Annotated[StrictStr, Field(max_length=draw(st.integers(1, 1024)), pattern=pattern)]
    placement = draw(st.sampled_from(["root", "optional", "list", "nested"]))
    name = draw(st.sampled_from(["detail", "code_text", "summary", "label", "reference"]))
    annotation: Any
    if placement == "root":
        annotation = field
    elif placement == "optional":
        annotation = field | None
    elif placement == "list":
        annotation = Annotated[list[field], Field(max_length=4)]  # type: ignore[valid-type]
    else:
        inner = create_model("Inner", __base__=PayloadModel, **{name: (field, ...)})
        annotation = inner
    fields: dict[str, Any] = {"zone_id": (UUID, ...), name: (annotation, None)}
    return create_model("Generated", __base__=PayloadModel, **fields)


@given(model=free_text_models())
def test_payload_models_with_free_text_never_register(model: type[PayloadModel]) -> None:
    catalog = OutboxCatalog()
    with pytest.raises(OutboxRegistrationRejected, match="texto libre en la carga"):
        catalog.event_types.register(
            EventType(
                event_name="generated_event",
                publisher_unit=ActorUnit.U02,
                payload_model=model,
                description_es="Evento generado",
            )
        )
    assert catalog.event_types.event_names() == ()


_STRUCTURAL_DETAIL = re.compile(r"[a-z_]+ en /[a-z_]*")

_HOSTILE_TEXT = st.text(min_size=1, max_size=80) | st.sampled_from(
    [
        "Juan Pérez",
        "walk_test\n",
        "walk​test",
        "w\u0430lk_test",  # a cirílica
        "walk test",
        "WALK_TEST",
        " walk_test",
        "<b>x</b>",
    ]
)


@st.composite
def free_text_payloads(draw: st.DrawFn) -> dict[str, Any]:
    text = draw(_HOSTILE_TEXT)
    where = draw(st.sampled_from(["extra", "code", "state", "zone_id", "observed_at"]))
    if where == "extra":
        key = draw(st.sampled_from(["comment", "note", "observer", "reason", "description"]))
        return probe_payload(**{key: text})
    closed = {
        "code": r"[a-z][a-z0-9_]{0,63}",
        "state": r"observable|degraded|unobservable",
        "zone_id": r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        "observed_at": r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z",
    }
    assume(re.fullmatch(closed[where], text) is None)
    return probe_payload(**{where: text})


@given(payload=free_text_payloads())
def test_free_text_payload_is_rejected_before_any_statement(
    environment: Environment, payload: dict[str, Any]
) -> None:
    context = make_context()

    async def run() -> tuple[OutboxRejected, int]:
        async with environment.database.transaction(context) as transaction:
            faults = StatementFaults.install(transaction)
            with pytest.raises(OutboxRejected) as caught:
                await environment.outbox.publish(
                    transaction, NewEvent(event_name=PROBE_EVENT, payload=payload)
                )
            return caught.value, faults.statements

    rejected, statements = environment.loop.run(run())
    assert rejected.code == "payload_invalid"
    assert statements == 0
    # El detalle solo nombra el tipo de error y la ruta, nunca el valor recibido.
    assert _STRUCTURAL_DETAIL.fullmatch(rejected.detail), rejected.detail
    assert environment.loop.run(outbox_counts(environment.migrated, context.organization_id)) == (
        0,
        0,
    )
