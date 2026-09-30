"""``ledger.canonical``: una sola canonicalización por registro (LC-NUC-09, TASK-109).

- **PR-NUC-17** (oráculo U-01): para entidades del contrato generadas por el kit de conformidad,
  ``canonical_bytes`` en la plataforma es byte a byte el ``canonicalize`` de U-01, tanto desde el
  documento JSON como desde el modelo Pydantic validado.
- **PR-NUC-48** (ida y vuelta): ``parse(canonical(c)) = c``,
  ``canonical(parse(canonical(c))) = canonical(c)`` y, en la base, ``content_json`` de la fila es
  ``c`` como documento.
- Bordes: el umbral de 16 KB (exacto en 16 384 y 16 385 bytes), la cota del tamaño nunca por
  debajo del real, y toda entrada inválida termina en ``CanonicalFormError``.
- Un documento de 256 KB se canonicaliza sin bloquear el bucle de eventos: un temporizador
  concurrente sigue avanzando; la contraprueba en línea lo deja parado.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel
from vigia_contracts.canonical import canonicalize
from vigia_contracts.conformance.generators import (
    detection_for_review,
    encode,
    finding,
    heartbeat,
    observability_event,
    update_result,
    zone_catalog,
)
from vigia_contracts.models.detection_for_review import DetectionForReviewSubmission
from vigia_contracts.models.finding import FindingSubmission
from vigia_contracts.models.heartbeat import Heartbeat
from vigia_contracts.models.observability_event import ObservabilityEventSubmission
from vigia_contracts.models.update_result import UpdateResult
from vigia_contracts.models.zone_catalog import ZoneCatalog

from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import (
    DatabaseLoop,
    MigratedDatabase,
    insert_record,
    migrated_database,
    record_values,
    register_record_types,
    set_organization,
)
from tests.properties.envelope_strategies import MAX_SAFE_INTEGER, texts
from vigia_platform.ledger import canonical
from vigia_platform.ledger.canonical import (
    LARGE_DOCUMENT_BYTES,
    CanonicalFormError,
    canonical_bytes,
    canonical_bytes_sync,
    parse,
)
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.cpu_pool import CpuPool

# --- Utilidades -----------------------------------------------------------------------------------


def same_document(left: object, right: object) -> bool:
    """Igualdad de documentos JSON: números por valor de doble, booleanos distintos de números."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        if isinstance(left, float) or isinstance(right, float):
            return float(left) == float(right)
        return left == right
    if isinstance(left, list | tuple) and isinstance(right, list | tuple):
        return len(left) == len(right) and all(map(same_document, left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            same_document(left[key], right[key]) for key in left
        )
    return type(left) is type(right) and left == right


class SpyPool(CpuPool):
    """Pool real que cuenta cuántas canonicalizaciones recibió."""

    def __init__(self) -> None:
        super().__init__(SystemClock(), max_workers=1)
        self.calls = 0

    async def run(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await super().run(function, *args, **kwargs)


@pytest.fixture(scope="module")
def spy_loop() -> Iterator[tuple[DatabaseLoop, SpyPool]]:
    loop, pool = DatabaseLoop(), SpyPool()
    yield loop, pool
    pool.shutdown()
    loop.close()


def _via_pool(
    spy: tuple[DatabaseLoop, SpyPool], document: object, *, limit: int | None = None
) -> tuple[bytes, bool]:
    """``canonical_bytes`` con el pool espía; devuelve los bytes y si pasó por el pool."""
    loop, pool = spy
    before = pool.calls
    if limit is None:
        data = loop.run(canonical_bytes(document, pool=pool))  # type: ignore[arg-type]
    else:
        with mock.patch.object(canonical, "LARGE_DOCUMENT_BYTES", limit):
            data = loop.run(canonical_bytes(document, pool=pool))  # type: ignore[arg-type]
    return data, pool.calls > before


# --- Generadores ----------------------------------------------------------------------------------

_finite_floats = st.floats(allow_nan=False, allow_infinity=False)
_numbers = st.one_of(
    st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER),
    _finite_floats,
    st.sampled_from([0.0, -0.0, 1e21, 1e-7, 5e-324, 1.7976931348623157e308, 2.0**53, 1e17]),
    # Dobles enteros de magnitud ≥ 2**53: RFC 8785 los escribe sin exponente.
    st.integers(53, 70).map(lambda exponent: float(2**exponent)),
)
_json_leaf = st.one_of(st.none(), st.booleans(), _numbers, texts())
json_values = st.recursive(
    _json_leaf,
    lambda children: st.one_of(
        st.lists(children, max_size=5), st.dictionaries(texts(max_size=12), children, max_size=5)
    ),
    max_leaves=25,
)
"""Cualquier documento JSON que RFC 8785 puede escribir (sin ``\\u0000``, ver ``texts``)."""

json_objects = st.dictionaries(texts(max_size=12), json_values, max_size=6)


@st.composite
def contract_entities(draw: st.DrawFn) -> tuple[dict[str, Any], type[BaseModel]]:
    """Una entidad del contrato generada por el kit de U-01 y su modelo Pydantic."""
    catalog = draw(zone_catalog())
    choices: list[tuple[st.SearchStrategy[dict[str, Any]], type[BaseModel]]] = [
        (st.just(catalog), ZoneCatalog),
        (finding(catalog), FindingSubmission),
        (detection_for_review(catalog), DetectionForReviewSubmission),
        (observability_event(catalog), ObservabilityEventSubmission),
        (heartbeat(catalog), Heartbeat),
        (update_result(), UpdateResult),
    ]
    strategy, model = draw(st.sampled_from(choices))
    return draw(strategy), model


# --- PR-NUC-17: bytes iguales a U-01 --------------------------------------------------------------


@given(entity=contract_entities())
def test_contract_entities_canonicalize_like_u01(
    spy_loop: tuple[DatabaseLoop, SpyPool], entity: tuple[dict[str, Any], type[BaseModel]]
) -> None:
    document, model_type = entity
    model = model_type.model_validate_json(encode(document), strict=True)
    expected = canonicalize(model)
    assert canonicalize(document) == expected
    assert canonical_bytes_sync(document) == expected
    assert canonical_bytes_sync(model) == expected
    assert _via_pool(spy_loop, document)[0] == expected
    assert _via_pool(spy_loop, model)[0] == expected
    # Por el pool también (umbral 0): mismos bytes.
    assert _via_pool(spy_loop, model, limit=0) == (expected, True)


# --- PR-NUC-48: ida y vuelta ----------------------------------------------------------------------


@given(document=json_values)
def test_parse_of_canonical_is_the_document(document: Any) -> None:
    data = canonical_bytes_sync(document)
    parsed = parse(data)
    assert same_document(parsed, document)
    assert canonical_bytes_sync(parsed) == data


@given(document=json_values)
def test_canonical_equals_u01_for_any_json(document: Any) -> None:
    assert canonical_bytes_sync(document) == canonicalize(document)


@given(document=json_values)
def test_parse_accepts_any_json_spelling_of_the_document(document: Any) -> None:
    """Bytes JSON no canónicos (orden, espacios, escapes ASCII) se leen al mismo documento."""
    spelled = json.dumps(document, ensure_ascii=True, indent=1, sort_keys=False).encode()
    parsed = parse(spelled)
    assert same_document(parsed, document)
    assert canonical_bytes_sync(parsed) == canonical_bytes_sync(document)


# --- Umbral de 16 KB ------------------------------------------------------------------------------


def _ascii_document(size: int) -> dict[str, str]:
    """``{"a": "xx…"}`` con exactamente ``size`` bytes canónicos."""
    return {"a": "x" * (size - len('{"a":""}'))}


@pytest.mark.parametrize(
    ("size", "in_pool"),
    [
        (LARGE_DOCUMENT_BYTES - 1, False),
        (LARGE_DOCUMENT_BYTES, False),
        (LARGE_DOCUMENT_BYTES + 1, True),
        (256 * 1024, True),
    ],
)
def test_threshold_is_exact_at_16_kib(
    spy_loop: tuple[DatabaseLoop, SpyPool], size: int, in_pool: bool
) -> None:
    document = _ascii_document(size)
    data, used_pool = _via_pool(spy_loop, document)
    assert len(data) == size
    assert data == canonicalize(document)
    assert used_pool is in_pool


@pytest.mark.parametrize("escaped", ['"', "\\", 'a"\\'])
@pytest.mark.parametrize(("extra", "in_pool"), [(0, False), (1, True)])
def test_threshold_counts_escaped_quotes_and_backslashes(
    spy_loop: tuple[DatabaseLoop, SpyPool], escaped: str, extra: int, in_pool: bool
) -> None:
    """Comillas y barras inversas ocupan dos bytes: el borde de 16 KB sigue siendo exacto."""
    unit = len(canonicalize(escaped)) - 2
    repeats, rest = divmod(LARGE_DOCUMENT_BYTES - len('{"a":""}'), unit)
    document = {"a": escaped * repeats + "x" * (rest + extra)}
    data, used_pool = _via_pool(spy_loop, document)
    assert len(data) == LARGE_DOCUMENT_BYTES + extra
    assert used_pool is in_pool


def test_small_document_with_controls_may_go_to_the_pool(
    spy_loop: tuple[DatabaseLoop, SpyPool],
) -> None:
    """Con controles la cota es la de peor caso (6 bytes por carácter): va al pool antes."""
    document = {"a": "\x01" * (LARGE_DOCUMENT_BYTES // 6 + 1)}
    data, used_pool = _via_pool(spy_loop, document)
    assert data == canonicalize(document)
    assert used_pool


@given(document=json_values)
def test_size_bound_never_underestimates(
    spy_loop: tuple[DatabaseLoop, SpyPool], document: Any
) -> None:
    """Con el umbral justo por debajo del tamaño real, siempre va al pool (ninguna fuga)."""
    expected = canonicalize(document)
    assert _via_pool(spy_loop, document, limit=len(expected) - 1) == (expected, True)


@given(
    document=st.recursive(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER),
            st.text(st.characters(min_codepoint=0x20, max_codepoint=0x7E)),
        ),
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(st.characters(min_codepoint=0x20, max_codepoint=0x7E)),
                            children, max_size=4),
        ),
        max_leaves=15,
    )
)  # fmt: skip
def test_size_bound_is_exact_for_printable_ascii_and_integers(
    spy_loop: tuple[DatabaseLoop, SpyPool], document: Any
) -> None:
    """Sin dobles ni controles la cota es exacta: con el umbral igual al tamaño, en línea."""
    expected = canonicalize(document)
    assert _via_pool(spy_loop, document, limit=len(expected)) == (expected, False)


# --- 256 KB sin bloquear el bucle -----------------------------------------------------------------


def _document_of_256_kib() -> dict[str, Any]:
    """Documento con muchos nodos y exactamente 256 KB canónicos (el tope de ``content``)."""
    document: dict[str, Any] = {
        f"z{index:05d}": {"zona": "Envasado ñ" * 8, "n": index, "ok": True, "xs": [1, 2, 3]}
        for index in range(1_800)
    }
    padding = 256 * 1024 - len(canonicalize(document)) - len(',"zz":""')
    document["zz"] = "x" * padding
    assert len(canonicalize(document)) == 256 * 1024
    return document


async def _ticks_while(awaitable: Any) -> tuple[list[float], float, float]:
    """Marcas de un temporizador de 1 ms que corre mientras se espera ``awaitable``."""
    loop = asyncio.get_running_loop()
    ticks: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            ticks.append(loop.time())
            await asyncio.sleep(0.001)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.01)
    started = loop.time()
    await awaitable
    finished = loop.time()
    stop.set()
    await task
    return ticks, started, finished


@pytest.mark.asyncio
async def test_256_kib_document_does_not_block_the_event_loop() -> None:
    document = _document_of_256_kib()
    pool = CpuPool(SystemClock())
    try:
        ticks, started, finished = await _ticks_while(canonical_bytes(document, pool=pool))
    finally:
        pool.shutdown()
    during = [tick for tick in ticks if started < tick < finished]
    edges = [started, *during, finished]
    longest_gap = max(later - earlier for earlier, later in itertools.pairwise(edges))
    # El temporizador siguió avanzando: el bucle nunca estuvo parado toda la canonicalización.
    assert during, f"el bucle no avanzó en {finished - started:.3f} s"
    assert longest_gap < finished - started


@pytest.mark.asyncio
async def test_inline_canonicalization_would_block_the_loop() -> None:
    """Contraprueba: con el umbral desactivado, el mismo documento para el temporizador."""
    document = _document_of_256_kib()
    with mock.patch.object(canonical, "LARGE_DOCUMENT_BYTES", math.inf):
        ticks, started, finished = await _ticks_while(canonical_bytes(document))
    assert [tick for tick in ticks if started < tick < finished] == []


# --- Entradas inválidas: siempre CanonicalFormError -----------------------------------------------


def _deep(depth: int) -> list[Any]:
    root: list[Any] = []
    current = root
    for _ in range(depth):
        child: list[Any] = []
        current.append(child)
        current = child
    return root


@pytest.mark.parametrize(
    "document",
    [
        float("nan"),
        float("inf"),
        -float("inf"),
        {"x": float("nan")},
        MAX_SAFE_INTEGER + 1,
        -(MAX_SAFE_INTEGER + 1),
        10**5000,
        {1: "clave no textual"},
        {True: 1},
        b"bytes",
        {"x": {1, 2}},
        "\ud800",
        {"\udfff": 1},
        uuid.UUID(int=1),
        _deep(5_000),
    ],
    ids=lambda value: type(value).__name__,
)
def test_invalid_documents_raise_canonical_form_error(
    spy_loop: tuple[DatabaseLoop, SpyPool], document: Any
) -> None:
    with pytest.raises(CanonicalFormError):
        canonical_bytes_sync(document)
    with pytest.raises(CanonicalFormError):
        _via_pool(spy_loop, document)
    with pytest.raises(CanonicalFormError):
        _via_pool(spy_loop, document, limit=0)


def test_boundary_integers_are_accepted() -> None:
    assert canonical_bytes_sync([MAX_SAFE_INTEGER, -MAX_SAFE_INTEGER]) == (
        b"[9007199254740991,-9007199254740991]"
    )


def test_error_message_does_not_quote_content() -> None:
    """El mensaje no cita el contenido (puede acabar en un registro, PR-NUC-54)."""
    with pytest.raises(CanonicalFormError) as raised:
        canonical_bytes_sync({"nombre": "texto-secreto", "n": 2**60})
    assert "texto-secreto" not in str(raised.value)
    assert str(2**60) not in str(raised.value)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"   ",
        b"{",
        b"NaN",
        b"[Infinity]",
        b"[-Infinity]",
        b"1e400",
        b"[-1e400]",
        b"1" * 400,
        b"1" * 5_000,
        b'{"a":1,"a":2}',
        b'{"a":{"b":1,"b":1}}',
        b'"\\ud800"',
        b'{"\\udc00":1}',
        b'["\\ud83d"]',
        b"\xff\xfe",
        b'"\xc3"',
        b'\xef\xbb\xbf{"a":1}',
        b"[" * 100_000 + b"]" * 100_000,
        b'{"a":1} x',
        b"'a'",
    ],
)
def test_invalid_bytes_raise_canonical_form_error(data: bytes) -> None:
    with pytest.raises(CanonicalFormError):
        parse(data)


@pytest.mark.parametrize("data", ["texto", 12, None, ["[]"]])
def test_parse_requires_bytes(data: object) -> None:
    with pytest.raises(CanonicalFormError):
        parse(data)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'"\\ud83d\\ude00"', "\U0001f600"),
        (b'"\\uD83D\\uDE00"', "\U0001f600"),
        (b"9007199254740991", MAX_SAFE_INTEGER),
        (b"-9007199254740991", -MAX_SAFE_INTEGER),
        (b"100000000000000000", 1e17),
        (b"1e+21", 1e21),
        (b"-0", 0),
        (bytearray(b"[1]"), [1]),
        (memoryview(b'{"a":null}'), {"a": None}),
    ],
)
def test_parse_edges(data: bytes, expected: object) -> None:
    parsed = parse(data)
    assert same_document(parsed, expected)
    assert canonical_bytes_sync(parsed) == canonicalize(expected)  # type: ignore[arg-type]


def test_integer_beyond_safe_range_is_read_as_double() -> None:
    parsed = parse(b"[9007199254740993]")
    assert parsed == [9007199254740992.0]
    assert isinstance(parsed, list)
    assert isinstance(parsed[0], float)


# --- PR-NUC-48 en la base: content_json igual al documento ----------------------------------------


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_canonical") as migrated:
        yield migrated


@pytest.fixture(scope="module")
def app(database: MigratedDatabase, spy_loop: tuple[DatabaseLoop, SpyPool]) -> Iterator[Any]:
    loop, _ = spy_loop
    owner = loop.run(database.connect())
    loop.run(register_record_types(owner))
    loop.run(owner.close())
    connection = loop.run(database.connect("vigia_app"))
    yield connection
    loop.run(connection.close())


@pytest.mark.integration
@given(document=json_objects)
def test_content_json_of_the_row_is_the_document(
    spy_loop: tuple[DatabaseLoop, SpyPool], app: Any, document: dict[str, Any]
) -> None:
    loop, _ = spy_loop
    organization_id = uuid.uuid4()
    data = canonical_bytes_sync(document)
    values = record_values(organization_id, uuid.uuid4())
    values["content"] = data

    async def write() -> Any:
        async with app.transaction():
            await set_organization(app, organization_id)
            row = await insert_record(app, values)
            content_json = await app.fetchval(
                "SELECT content_json::text FROM ledger.ledger_record WHERE record_id = $1",
                values["record_id"],
            )
            return row, content_json

    row, content_json = loop.run(write())
    assert row["content"] == data
    # jsonb escribe los números con todas sus cifras: se leen como RFC 8785 (dobles).
    from_database = parse(content_json.encode())
    assert same_document(from_database, document)
    assert canonical_bytes_sync(from_database) == data
    assert same_document(parse(row["content"]), document)
