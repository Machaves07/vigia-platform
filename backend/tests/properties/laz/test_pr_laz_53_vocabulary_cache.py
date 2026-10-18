"""PR-LAZ-53: la caché por proceso valida igual que la lista de esa versión leída de la base.

TASK-303 (PAT-LAZ-REN-02; NFR-LAZ-04; domain-entities §2.12 y su nota de NFR Design):

- **igualdad**: para toda lista de cliente generada (lemas y patrones añadidos en una versión) y
  todo texto, el veredicto con la caché es el de compilar esa lista directamente, y
  ``list_version`` es la versión de la entrada usada; otra organización sigue con la inicial;
- **sin red ni base en la validación**: una vez comprobada la versión, el validador valida con un
  almacén que lanza en cualquier lectura y con ``socket.connect``, ``getaddrinfo`` y
  ``asyncpg.connect`` sustituidos por dobles que lanzan;
- **refresco**: versión nueva → relee; misma versión → no relee antes de 60 s y relee después
  (reloj simulado del escritor); ``invalidate`` (``vocabulary_list_changed``) → vuelve a la
  inicial y relee; una lista que no compila conserva la anterior;
- **sin retroceso de versión** (prueba concurrente): dos comprobaciones a la vez que leen las
  versiones 2 y 3, con la de la 2 compilando más tarde, dejan la 3;
- **integración** (PostgreSQL 16 como ``vigia_app``): la lista y sus términos sembrados en
  ``loop`` (añadidos y retirados por versión, dos organizaciones) se leen con
  ``PostgresVocabularyStore``; el veredicto es el de la lista que un superusuario lee directamente
  de esa versión, con su ``lemmatizer_version``; el almacén filtra por organización aun sin
  seguridad de fila (lectura como superusuario); y el escritor real comprueba la versión al abrir
  la escritura: el término del cliente se rechaza en su organización y no en otra.

Reloj simulado; solo datos generados.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import Executable

from tests.identity_db import migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.properties.laz.strategies.texts import allowed_texts, blocked_vocabulary_texts
from tests.vocabulary_support import (
    FIELD,
    MemoryStore,
    SeededTerm,
    customer_term,
    seed_list,
    vocabulary_writer,
)
from tests.writer_support import (
    ORDER_TYPE,
    Place,
    WriterEnvironment,
    order_document,
    unit_context,
    writer_environment,
)
from vigia_platform.ledger.application.writer import (
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
)
from vigia_platform.ledger.free_text import (
    FreeTextCandidate,
    FreeTextPreparation,
    RowReader,
    canonical_form,
)
from vigia_platform.loop.adapters.postgres.vocabulary import PostgresVocabularyStore
from vigia_platform.loop.domain.enums import BlockedTermCategory, BlockedTermForm, BlockedTermOrigin
from vigia_platform.loop.validators.cache import TTL_SECONDS, VocabularyCache
from vigia_platform.loop.validators.folding import validation_form
from vigia_platform.loop.validators.initial_list import INITIAL_LIST_VERSION
from vigia_platform.loop.validators.vocabulary import (
    BlockedVocabularyRejected,
    BlockedVocabularyValidator,
    BlockedVocabularyVerdict,
    CompiledVocabulary,
    VocabularyList,
    VocabularyTerm,
    compile_vocabulary,
    find_blocked,
)
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorUnit

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

CUSTOMER_LEMMAS = ("pasillo", "baliza", "bocina", "montacargas", "cinta", "puerta", "umbral")
CUSTOMER_PATTERNS = (r"zona[\W_]*norte", r"linea[\W_]*sur", r"parada[\W_]*de[\W_]*emergencia")


async def _no_rows(statement: Executable, parameters: Mapping[str, Any]) -> Sequence[Row[Any]]:
    raise AssertionError("el almacén en memoria no usa el lector de filas")


def _preparation(organization_id: uuid.UUID, clock: SimulatedClock) -> FreeTextPreparation:
    return FreeTextPreparation(organization_id=organization_id, rows=_no_rows, clock=clock)


def _candidate(text: str, organization_id: uuid.UUID | None) -> FreeTextCandidate:
    return FreeTextCandidate(text, canonical_form(text), organization_id)


def _expected(text: str, vocabulary: VocabularyList) -> BlockedVocabularyVerdict | None:
    return find_blocked(validation_form(_candidate(text, None)), compile_vocabulary(vocabulary))


@st.composite
def customer_lists(draw: st.DrawFn) -> VocabularyList:
    lemmas = draw(st.lists(st.sampled_from(CUSTOMER_LEMMAS), unique=True, max_size=4))
    patterns = draw(st.lists(st.sampled_from(CUSTOMER_PATTERNS), unique=True, max_size=2))
    terms = tuple(customer_term(value) for value in lemmas) + tuple(
        customer_term(value, form=BlockedTermForm.PATTERN) for value in patterns
    )
    return VocabularyList(version=draw(st.integers(2, 10_000)), terms=terms)


TEXTS = st.one_of(allowed_texts(), blocked_vocabulary_texts().map(lambda blocked: blocked.text))


# --- Igualdad con la lista de esa versión ---------------------------------------------------------


@given(vocabulary=customer_lists(), texts=st.lists(TEXTS, min_size=1, max_size=5))
def test_the_cached_verdict_is_the_verdict_of_that_list_version(
    vocabulary: VocabularyList, texts: list[str]
) -> None:
    organization, other = uuid.uuid4(), uuid.uuid4()
    store = MemoryStore({organization: vocabulary})
    cache = VocabularyCache(store)
    asyncio.run(cache.refresh(_preparation(organization, SimulatedClock(NOW))))
    validator = BlockedVocabularyValidator(cache.compiled_for)
    for text in texts:
        verdict = validator.verdict(_candidate(text, organization))
        assert verdict == _expected(text, vocabulary)
        if verdict is not None:
            assert verdict.list_version == vocabulary.version
        # Otra organización (o ninguna) valida con la lista inicial: la caché es por organización.
        initial = VocabularyList(version=INITIAL_LIST_VERSION, terms=())
        assert validator.verdict(_candidate(text, other)) == _expected(text, initial)
        assert validator.verdict(_candidate(text, None)) == _expected(text, initial)
    assert cache.compiled_for(organization).list_version == vocabulary.version


def test_a_customer_term_is_rejected_only_in_its_organization() -> None:
    organization = uuid.uuid4()
    vocabulary = VocabularyList(version=4, terms=(customer_term("pasillo"),))
    cache = VocabularyCache(MemoryStore({organization: vocabulary}))
    asyncio.run(cache.refresh(_preparation(organization, SimulatedClock(NOW))))
    validator = BlockedVocabularyValidator(cache.compiled_for)
    text = "Revisión de los pasillos de la planta"
    verdict = validator.verdict(_candidate(text, organization))
    assert verdict is not None
    assert (verdict.term_id, verdict.list_version) == (vocabulary.terms[0].term_id, 4)
    assert validator.verdict(_candidate(text, uuid.uuid4())) is None


# --- Sin red ni base en la validación -------------------------------------------------------------


class RaisingStore(MemoryStore):
    async def head(self, organization_id: uuid.UUID, rows: RowReader) -> Any:
        raise AssertionError("el validador consultó la base")

    async def load(self, organization_id: uuid.UUID, head: Any, rows: RowReader) -> Any:
        raise AssertionError("el validador consultó la base")


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> Any:
        raise AssertionError("el validador abrió una conexión")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(asyncpg, "connect", refuse)


def test_the_validator_opens_no_network_and_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    organization = uuid.uuid4()
    store = MemoryStore({organization: VocabularyList(version=3, terms=(customer_term("cinta"),))})
    cache = VocabularyCache(store)
    asyncio.run(cache.refresh(_preparation(organization, SimulatedClock(NOW))))
    reads = (store.heads, store.loads)
    cache._store = RaisingStore()  # cualquier lectura a partir de aquí lanza
    _no_network(monkeypatch)
    validator = BlockedVocabularyValidator(cache.compiled_for)
    texts = ("Revisión de la cinta transportadora", "Zona despejada", "Revisión de la guarda")
    verdicts = [validator.verdict(_candidate(text, organization)) for text in texts]
    assert [verdict is not None for verdict in verdicts] == [True, True, False]
    for organization_id in (organization, uuid.uuid4(), None):
        with pytest.raises(BlockedVocabularyRejected):
            validator(_candidate("La zona quedó vacía", organization_id), FIELD)
    assert (store.heads, store.loads) == reads


# --- Refresco por versión, por caducidad y por evento ------------------------------------------


def test_a_new_version_is_read_and_the_same_version_only_after_the_ttl() -> None:
    organization = uuid.uuid4()
    store = MemoryStore({organization: VocabularyList(version=2, terms=(customer_term("cinta"),))})
    cache = VocabularyCache(store)
    clock = SimulatedClock(NOW)

    def refresh() -> CompiledVocabulary:
        asyncio.run(cache.refresh(_preparation(organization, clock)))
        return cache.compiled_for(organization)

    assert refresh().list_version == 2 and (store.heads, store.loads) == (1, 1)
    clock.advance(TTL_SECONDS - 1)
    assert refresh().list_version == 2 and (store.heads, store.loads) == (2, 1)
    clock.advance(2)  # 61 s desde la carga: caducada aunque nadie la invalidara
    assert refresh().list_version == 2 and (store.heads, store.loads) == (3, 2)
    store.lists[organization] = VocabularyList(version=3, terms=(customer_term("bocina"),))
    assert refresh().list_version == 3 and (store.heads, store.loads) == (4, 3)


def test_invalidate_returns_to_the_initial_list_until_the_next_check() -> None:
    organization = uuid.uuid4()
    store = MemoryStore({organization: VocabularyList(version=5, terms=())})
    cache = VocabularyCache(store)
    clock = SimulatedClock(NOW)
    asyncio.run(cache.refresh(_preparation(organization, clock)))
    assert cache.compiled_for(organization).list_version == 5
    cache.invalidate(organization)
    assert cache.compiled_for(organization).list_version == INITIAL_LIST_VERSION
    asyncio.run(cache.refresh(_preparation(organization, clock)))
    assert cache.compiled_for(organization).list_version == 5 and store.loads == 2


def test_without_a_list_row_the_initial_list_applies() -> None:
    cache = VocabularyCache(MemoryStore())
    organization = uuid.uuid4()
    asyncio.run(cache.refresh(_preparation(organization, SimulatedClock(NOW))))
    assert cache.compiled_for(organization).list_version == INITIAL_LIST_VERSION


def test_a_list_that_does_not_compile_keeps_the_previous_one() -> None:
    organization = uuid.uuid4()
    store = MemoryStore({organization: VocabularyList(version=2, terms=(customer_term("cinta"),))})
    cache = VocabularyCache(store)
    clock = SimulatedClock(NOW)
    asyncio.run(cache.refresh(_preparation(organization, clock)))
    broken = customer_term("(sin cerrar", form=BlockedTermForm.PATTERN)
    store.lists[organization] = VocabularyList(version=3, terms=(broken,))
    asyncio.run(cache.refresh(_preparation(organization, clock)))
    assert cache.compiled_for(organization).list_version == 2


# --- Sin retroceso de versión (concurrente) -------------------------------------------------------


class _RacingStore(MemoryStore):
    """La primera comprobación lee la versión 2; la segunda, la 3 (un cambio entre las dos)."""

    def __init__(self, organization: uuid.UUID) -> None:
        super().__init__({organization: VocabularyList(version=2, terms=())})
        self.organization = organization
        self.versions = {
            2: VocabularyList(version=2, terms=()),
            3: VocabularyList(version=3, terms=(customer_term("bocina"),)),
        }

    async def head(self, organization_id: uuid.UUID, rows: RowReader) -> Any:
        head = await super().head(organization_id, rows)
        self.lists[self.organization] = self.versions[3]
        return head

    async def load(self, organization_id: uuid.UUID, head: Any, rows: RowReader) -> Any:
        self.loads += 1
        return self.versions[head.version]  # la versión que leyó su comprobación


async def _race(organization: uuid.UUID) -> CompiledVocabulary:
    old_compiled = asyncio.Event()
    new_stored = asyncio.Event()

    async def run_cpu(compiler: Any, vocabulary: VocabularyList) -> CompiledVocabulary:
        compiled: CompiledVocabulary = compiler(vocabulary)
        if vocabulary.version == 2:
            await new_stored.wait()  # la versión vieja termina de compilar después
            old_compiled.set()
        return compiled

    cache = VocabularyCache(_RacingStore(organization), run_cpu=run_cpu)
    clock = SimulatedClock(NOW)

    async def old() -> None:
        await cache.refresh(_preparation(organization, clock))

    async def new() -> None:
        await asyncio.sleep(0)  # deja que la primera lea la versión 2
        await cache.refresh(_preparation(organization, clock))
        new_stored.set()
        await old_compiled.wait()

    await asyncio.wait_for(asyncio.gather(old(), new()), timeout=30)
    return cache.compiled_for(organization)


@pytest.mark.parametrize("attempt", range(3))
def test_two_concurrent_checks_never_go_back_to_an_older_version(attempt: int) -> None:
    assert asyncio.run(_race(uuid.uuid4())).list_version == 3


# --- Integración con PostgreSQL ---------------------------------------------------------------


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[WriterEnvironment]:
    with (
        migrated_database(postgres_endpoint, "laz_vocabulary_cache") as migrated,
        writer_environment(migrated) as environment,
    ):
        yield environment


SEEDED = (
    SeededTerm("pasillo"),
    SeededTerm(r"linea[\W_]*sur", form=BlockedTermForm.PATTERN, added_in_version=2),
    SeededTerm("baliza", added_in_version=1, removed_in_version=2),
    SeededTerm("bocina", added_in_version=3),
)
"""Versión 2: ``pasillo`` y ``linea sur`` vigentes; ``baliza`` retirada; ``bocina`` futura."""


@pytest.fixture(scope="module")
def seeded(environment: WriterEnvironment) -> tuple[uuid.UUID, uuid.UUID]:
    """Dos organizaciones: la primera con su lista en la versión 2; la segunda, con otra lista."""
    first, second = Place.new(), Place.new()
    environment.loop.run(
        seed_list(environment.migrated, first.organization_id, 2, SEEDED, lemmatizer_version="x")
    )
    environment.loop.run(
        seed_list(environment.migrated, second.organization_id, 7, (SeededTerm("umbral"),))
    )
    return first.organization_id, second.organization_id


async def _superuser_list(
    environment: WriterEnvironment, organization: uuid.UUID
) -> VocabularyList:
    """El oráculo: la lista de la versión vigente, leída como superusuario y con su propio SQL."""
    connection = await environment.migrated.connect()
    try:
        head = await connection.fetchrow(
            "SELECT version, lemmatizer_version FROM loop.blocked_vocabulary_list"
            " WHERE organization_id = $1",
            organization,
        )
        rows = await connection.fetch(
            "SELECT * FROM loop.blocked_term WHERE organization_id = $1", organization
        )
    finally:
        await connection.close()
    version = int(head["version"])
    terms = tuple(
        VocabularyTerm(
            term_id=row["term_id"],
            category=BlockedTermCategory(row["category"]),
            form=BlockedTermForm(row["form"]),
            value=row["value"],
            suggestion_es=row["suggestion_es"],
            origin=BlockedTermOrigin(row["origin"]),
        )
        for row in sorted(rows, key=lambda row: row["term_id"])
        if row["added_in_version"] <= version
        and (row["removed_in_version"] is None or row["removed_in_version"] > version)
    )
    return VocabularyList(version, terms, head["lemmatizer_version"])


def _app_reader(environment: WriterEnvironment, organization: uuid.UUID) -> RowReader:
    context = unit_context(organization, ActorUnit.U03)

    async def rows(statement: Executable, parameters: Mapping[str, Any]) -> Sequence[Row[Any]]:
        return await environment.database.read(context, statement, parameters)

    return rows


@pytest.mark.integration
@given(
    texts=st.lists(
        TEXTS
        | st.sampled_from(
            ("Revisión del pasillo norte", "Línea sur detenida", "Baliza revisada", "Bocina nueva")
        ),
        min_size=1,
        max_size=6,
    )
)
def test_the_cache_reads_the_list_of_that_version_from_the_database(
    environment: WriterEnvironment, seeded: tuple[uuid.UUID, uuid.UUID], texts: list[str]
) -> None:
    organization, _ = seeded
    oracle = environment.loop.run(_superuser_list(environment, organization))
    assert oracle.version == 2 and {term.value for term in oracle.terms} == {
        "pasillo",
        r"linea[\W_]*sur",
    }
    cache = VocabularyCache(PostgresVocabularyStore())
    preparation = FreeTextPreparation(
        organization, _app_reader(environment, organization), environment.clock
    )
    environment.loop.run(cache.refresh(preparation))
    compiled = cache.compiled_for(organization)
    assert (compiled.list_version, compiled.lemmatizer_version) == (2, "x")
    validator = BlockedVocabularyValidator(cache.compiled_for)
    for text in texts:
        assert validator.verdict(_candidate(text, organization)) == _expected(text, oracle)


@pytest.fixture(scope="module")
def superuser_reader(environment: WriterEnvironment) -> Iterator[RowReader]:
    """Lecturas como superusuario, que no pasan por la seguridad de fila."""
    engine = create_async_engine(
        environment.migrated.as_role(None).sqlalchemy_url, poolclass=NullPool
    )

    async def rows(statement: Executable, parameters: Mapping[str, Any]) -> Sequence[Row[Any]]:
        async with engine.connect() as connection:
            return (await connection.execute(statement, parameters)).all()

    yield rows
    environment.loop.run(engine.dispose())


@pytest.mark.integration
def test_the_store_filters_by_organization_even_without_row_security(
    environment: WriterEnvironment,
    seeded: tuple[uuid.UUID, uuid.UUID],
    superuser_reader: RowReader,
) -> None:
    first, second = seeded
    store = PostgresVocabularyStore()
    for organization, version, values in (
        (first, 2, {"pasillo", r"linea[\W_]*sur"}),
        (second, 7, {"umbral"}),
    ):
        head = environment.loop.run(store.head(organization, superuser_reader))
        assert head is not None and head.version == version
        loaded = environment.loop.run(store.load(organization, head, superuser_reader))
        assert {term.value for term in loaded.terms} == values
    assert environment.loop.run(store.head(uuid.uuid4(), superuser_reader)) is None


@pytest.mark.integration
def test_the_writer_checks_the_version_when_it_opens_the_write(
    environment: WriterEnvironment, seeded: tuple[uuid.UUID, uuid.UUID]
) -> None:
    first, second = seeded
    writer = vocabulary_writer(environment)
    for organization, expected_rejected in ((first, True), (second, False)):
        place = Place(organization, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        document = order_document(place, clips=0)
        document["note"] = "Revisión del pasillo norte"
        context = unit_context(organization, ActorUnit.U03)
        result = environment.loop.run(writer.write(context, ORDER_TYPE, document))
        if expected_rejected:
            assert isinstance(result, LedgerRejection)
            assert (result.code, result.detail_code) == (
                LedgerRejectionCode.FREE_TEXT_REJECTED,
                "loop_blocked_vocabulary_term",
            )
        else:
            assert isinstance(result, Receipt)
