"""PR-LAZ-57: la paginación por clave de la bandeja (TASK-307; LC-LAZ-05; PAT-LAZ-ESC-03).

Sobre PostgreSQL 16 real como ``vigia_app`` (``inbox_stack``) y **dos conexiones**: la bandeja
lee por el pool de la aplicación mientras otra conexión (la del superusuario) inserta filas **más
nuevas** de la misma zona **a la vez** (``asyncio.gather``) que se pide cada página. Con cualquier
tamaño de página (1 a 6), en los dos sentidos y con cubos repetidos (empates resueltos por
``finding_state_id``):

- recorrer todas las páginas devuelve **todas** las filas que existían al empezar, **una vez**;
- ninguna fila sale dos veces y ninguna página supera ``limit`` (ni 200);
- lo que aparece además es solo lo insertado durante el recorrido.

Las garantías de cero llamadas, una sentencia, índice parcial y tope de 200 están en
``tests/integration/test_loop_inbox_routes.py``. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject
from tests.loop_inbox_support import InboxStack, inbox_stack
from vigia_platform.loop.adapters.postgres.inbox_query import InboxCursor, InboxFilters
from vigia_platform.loop.application.inbox import MAX_INBOX_PAGE, InboxPage

pytestmark = pytest.mark.integration

BUDGET: Final = {"ci": 25, "pr": 25, "nightly": 200}
"""Ejemplos por perfil: cada uno siembra y recorre una zona sobre la base real."""
EXAMPLES: Final = BUDGET.get(settings.get_current_profile_name(), BUDGET["nightly"])
BASE: Final = datetime(2026, 10, 5, tzinfo=UTC)
INITIAL_ROWS: Final = 9


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[InboxStack]:
    with inbox_stack(postgres_endpoint, "loop_inbox_keyset") as built:
        yield built


@pytest.fixture(scope="module")
def template(stack: InboxStack) -> IngestedSubject:
    return stack.finding(stack.world.a)


@pytest.fixture(scope="module")
def clone_sql(stack: InboxStack) -> str:
    """``INSERT … SELECT`` que copia la fila plantilla con identificador, zona y marca nuevos."""
    columns = [
        str(row["column_name"])
        for row in stack.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'loop' AND table_name = 'finding_state'"
            " ORDER BY ordinal_position"
        )
    ]
    replaced = {
        "finding_state_id": "$2",
        "subject_id": "$3",
        "zone_id": "$4",
        "occurred_at_exact": "$5",
        "occurred_at_bucket": "$5",
    }
    values = ", ".join(replaced.get(column, column) for column in columns)
    return (
        f"INSERT INTO loop.finding_state ({', '.join(columns)})"  # noqa: S608 - columnas del catálogo
        f" SELECT {values} FROM loop.finding_state WHERE finding_state_id = $1"
    )


@given(
    limit=st.integers(1, 6),
    inserts=st.lists(st.integers(0, 3), min_size=1, max_size=6),
    newest_first=st.booleans(),
    same_bucket=st.booleans(),
)
@settings(max_examples=EXAMPLES, deadline=None)
def test_pr_laz_57_all_pages_neither_lose_nor_repeat_with_concurrent_newer_rows(
    stack: InboxStack,
    template: IngestedSubject,
    clone_sql: str,
    limit: int,
    inserts: list[int],
    newest_first: bool,
    same_bucket: bool,
) -> None:
    organization = stack.world.a
    zone_id = stack.zone_in(organization, organization.plant_id)
    admin = stack.world.stack.authz.sessions.admin
    context = stack.world.context(organization)
    filters = InboxFilters(zone_ids=(zone_id,))
    newest = [BASE]

    async def insert(moment: datetime) -> uuid.UUID:
        finding_state_id = uuid.uuid4()
        await admin.execute(
            clone_sql, template.record_id, finding_state_id, uuid.uuid4(), zone_id, moment
        )
        return finding_state_id

    async def seed() -> set[uuid.UUID]:
        moments = [BASE if same_bucket else BASE - timedelta(hours=i) for i in range(INITIAL_ROWS)]
        return {await insert(moment) for moment in moments}

    async def newer(count: int) -> set[uuid.UUID]:
        made: set[uuid.UUID] = set()
        for _ in range(count):
            newest[0] += timedelta(hours=1)
            made.add(await insert(newest[0]))
        return made

    async def walk() -> tuple[list[uuid.UUID], set[uuid.UUID], list[int]]:
        seen: list[uuid.UUID] = []
        inserted: set[uuid.UUID] = set()
        sizes: list[int] = []
        after: InboxCursor | None = None
        step = 0
        while True:
            count = inserts[step] if step < len(inserts) else 0
            page, made = await asyncio.gather(
                stack.service.page(
                    context, filters=filters, after=after, limit=limit, newest_first=newest_first
                ),
                newer(count),
            )
            assert isinstance(page, InboxPage)
            inserted |= made
            sizes.append(len(page.items))
            seen += [item.finding_state_id for item in page.items]
            if page.next_cursor is None:
                return seen, inserted, sizes
            after = page.next_cursor
            step += 1
            # Cada página avanza al menos una fila: más páginas que filas es un bucle.
            assert step <= INITIAL_ROWS + sum(inserts), "el recorrido no termina"

    initial = stack.run(seed())
    seen, inserted, sizes = stack.run(walk())
    assert len(seen) == len(set(seen)), "una fila salió dos veces"
    assert initial <= set(seen), "se perdió una fila que existía al empezar"
    assert set(seen) <= initial | inserted
    assert all(size <= limit <= MAX_INBOX_PAGE for size in sizes)
    # El recorrido terminó: lo que hay ahora en la zona es lo de antes más lo insertado.
    rows = stack.fetch(
        "SELECT finding_state_id FROM loop.finding_state WHERE zone_id = $1", zone_id
    )
    assert {row["finding_state_id"] for row in rows} == initial | inserted
