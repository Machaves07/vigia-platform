"""PR-NUC-41: reintento acotado en lecturas, escritura nunca repetida (PAT-NUC-RES-01, LC-NUC-29).

Para cualquier secuencia generada de fallos inyectados en lecturas y escrituras:

- ninguna escritura se ejecuta dos veces: intentos por escritura = 1;
- una lectura que falla una vez por conexión y luego responde devuelve el resultado; una que
  falla dos veces devuelve ``temporarily_unavailable``; intentos por lectura ≤ 2, con una sola
  espera de 100 ms entre ellos;
- tras un fallo posterior al ``COMMIT`` no hay reintento y el resultado se declara desconocido;
- los fallos que no son de conexión no se reintentan: ``lock_timeout`` → ``chain_locked_timeout``,
  ``statement_timeout`` → ``temporarily_unavailable``, el resto sale sin traducir;
- cada intento fija las tres variables del contexto antes de cualquier sentencia;
- sin contexto no se pide ninguna conexión (``ContextAbsent``).

El pool es un doble con guion de fallos por intento: cuenta intentos, sentencias, ``COMMIT``
enviados y efectos confirmados. La prueba de integración con PostgreSQL real está en
``tests/integration/test_set_local.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.engine import Result
from sqlalchemy.sql import Executable

from tests.factories import make_context, scope_contexts
from vigia_platform.shared.context import ActorKind, ContextAbsent, ScopeContext
from vigia_platform.shared.db import (
    MAX_READ_ATTEMPTS,
    READ_RETRY_DELAY_SECONDS,
    RETRY_AFTER_SECONDS,
    ChainLockedTimeout,
    ConnectionPort,
    Database,
    PoolClass,
    PoolPort,
    ProcessKind,
    RouteClass,
    TemporarilyUnavailable,
    TransactionAborted,
    route_class_scope,
    scope_parameters,
)

USER_STATEMENT = text("SELECT 1")


class Phase(enum.Enum):
    """Punto del intento donde se inyecta el fallo."""

    ACQUIRE = "acquire"
    BEGIN = "begin"
    SET_SCOPE = "set_scope"
    EXECUTE = "execute"
    COMMIT_BEFORE_APPLY = "commit_before_apply"
    COMMIT_AFTER_APPLY = "commit_after_apply"
    RELEASE = "release"  # solo para ``hang_at``: los guiones de fallos no la sortean

    @property
    def at_commit(self) -> bool:
        return self in (Phase.COMMIT_BEFORE_APPLY, Phase.COMMIT_AFTER_APPLY)


class Kind(enum.Enum):
    """Clase de error inyectado."""

    CONNECTION = "connection"
    LOCK_TIMEOUT = "lock_timeout"
    STATEMENT_TIMEOUT = "statement_timeout"
    OTHER = "other"


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class InjectedOther(Exception):
    """Error sin SQLSTATE que no es de conexión (p. ej. un fallo del llamador)."""


def _dbapi(cls: type[sa_exc.DBAPIError], sqlstate: str, *, invalidated: bool = False) -> Exception:
    return cls("SELECT 1", {}, _Orig(sqlstate), connection_invalidated=invalidated)


CONNECTION_ERRORS = st.sampled_from(
    [
        lambda: ConnectionResetError("reiniciada"),
        lambda: ConnectionRefusedError("rechazada"),
        lambda: TimeoutError(),
        lambda: sa_exc.TimeoutError("pool agotado"),
        lambda: _dbapi(sa_exc.OperationalError, "08006"),
        lambda: _dbapi(sa_exc.OperationalError, "57P01"),
        lambda: _dbapi(sa_exc.DBAPIError, "XX000", invalidated=True),
        lambda: sa_exc.InterfaceError("SELECT 1", {}, Exception("connection is closed")),
    ]
)
OTHER_ERRORS = st.sampled_from(
    [
        lambda: _dbapi(sa_exc.ProgrammingError, "42601"),  # sintaxis
        lambda: _dbapi(sa_exc.IntegrityError, "23505"),  # unicidad
        lambda: InjectedOther("fallo propio"),
    ]
)


@dataclass(frozen=True)
class Fault:
    phase: Phase
    kind: Kind
    make: Any

    def error(self) -> Exception:
        return cast(Exception, self.make())


@st.composite
def faults(draw: st.DrawFn) -> Fault:
    phase = draw(st.sampled_from([p for p in Phase if p is not Phase.RELEASE]))
    kind = draw(st.sampled_from(Kind))
    if kind is Kind.CONNECTION:
        make = draw(CONNECTION_ERRORS)
    elif kind is Kind.LOCK_TIMEOUT:
        make = lambda: _dbapi(sa_exc.OperationalError, "55P03")  # noqa: E731
    elif kind is Kind.STATEMENT_TIMEOUT:
        make = draw(
            st.sampled_from(
                [
                    lambda: _dbapi(sa_exc.OperationalError, "57014"),
                    lambda: _dbapi(sa_exc.OperationalError, "40001"),
                    lambda: _dbapi(sa_exc.OperationalError, "40P01"),
                ]
            )
        )
    else:
        make = draw(OTHER_ERRORS)
    return Fault(phase, kind, make)


ATTEMPT_SCRIPTS = st.lists(st.one_of(st.none(), faults()), min_size=1, max_size=3)
"""Guion de fallos: un elemento por intento (``None`` = el intento va bien)."""


@dataclass
class Journal:
    """Lo que el doble observó."""

    attempts: int = 0
    user_statements: int = 0
    commits_sent: int = 0
    effects_applied: int = 0
    released: int = 0
    discarded: int = 0
    terminated: int = 0
    sleeps: list[float] = field(default_factory=list)
    begins: list[bool] = field(default_factory=list)
    scope_before_statement: list[bool] = field(default_factory=list)
    scopes: list[dict[str, str]] = field(default_factory=list)


class _Rows:
    def all(self) -> list[tuple[int]]:
        return [(1,)]


class FakeConnection:
    """Conexión doble. ``hang_at``: el servidor deja de responder en esa fase, y la llamada
    solo termina (con error de conexión) cuando se corta el transporte, como con asyncpg."""

    def __init__(self, journal: Journal, fault: Fault | None, hang_at: Phase | None = None) -> None:
        self.journal = journal
        self.fault = fault
        self.hang_at = hang_at
        self.scope_set = False
        self.read_only = False
        self.pending = 0
        self._cut = asyncio.Event()

    def _maybe_fail(self, phase: Phase) -> None:
        if self.fault is not None and self.fault.phase is phase:
            raise self.fault.error()

    async def _maybe_hang(self, phase: Phase) -> None:
        if self.hang_at is phase:
            await self._cut.wait()
            raise ConnectionResetError("transporte cortado")

    def terminate(self) -> None:
        self.journal.terminated += 1
        self._cut.set()

    async def begin(self, *, read_only: bool) -> None:
        self.journal.begins.append(read_only)
        self.read_only = read_only
        self._maybe_fail(Phase.BEGIN)

    async def execute(
        self, statement: Executable, parameters: Mapping[str, Any] | None
    ) -> Result[Any]:
        if "set_config" in str(statement):
            self._maybe_fail(Phase.SET_SCOPE)
            self.journal.scopes.append(dict(parameters or {}))
            self.scope_set = True
            return cast(Result[Any], _Rows())
        self.journal.scope_before_statement.append(self.scope_set)
        await self._maybe_hang(Phase.EXECUTE)
        self._maybe_fail(Phase.EXECUTE)
        self.journal.user_statements += 1
        if not self.read_only:
            self.pending += 1
        return cast(Result[Any], _Rows())

    async def commit(self) -> None:
        self.journal.commits_sent += 1
        await self._maybe_hang(Phase.COMMIT_BEFORE_APPLY)
        self._maybe_fail(Phase.COMMIT_BEFORE_APPLY)
        self.journal.effects_applied += self.pending
        self.pending = 0
        self._maybe_fail(Phase.COMMIT_AFTER_APPLY)

    async def release(self) -> None:
        await self._maybe_hang(Phase.RELEASE)
        self.journal.released += 1

    async def discard(self) -> None:
        self.journal.discarded += 1


class FakePool:
    """Pool con guion: el intento ``n`` usa ``script[n]`` (o va bien si no hay más)."""

    def __init__(self, journal: Journal, script: Sequence[Fault | None] = ()) -> None:
        self.journal = journal
        self.script = list(script)

    async def acquire(self) -> ConnectionPort:
        index = self.journal.attempts
        self.journal.attempts += 1
        fault = self.script[index] if index < len(self.script) else None
        if fault is not None and fault.phase is Phase.ACQUIRE:
            raise fault.error()
        return FakeConnection(self.journal, fault)

    async def dispose(self) -> None:
        return None


def _database(pools: Mapping[PoolClass, PoolPort], journal: Journal, **kwargs: Any) -> Database:
    async def sleep(seconds: float) -> None:
        journal.sleeps.append(seconds)

    return Database(
        pools,
        process=kwargs.pop("process", ProcessKind.API),
        attempt_timeout_seconds=kwargs.pop("attempt_timeout_seconds", 5.0),
        command_timeout_seconds=kwargs.pop("command_timeout_seconds", 5.0),
        sleep=sleep,
    )


def _api_database(journal: Journal, script: Sequence[Fault | None]) -> Database:
    pool = FakePool(journal, script)
    return _database({PoolClass.NODE: pool, PoolClass.PERSON: pool}, journal)


def _expected_error(fault: Fault) -> type[BaseException]:
    if fault.kind is Kind.LOCK_TIMEOUT:
        return ChainLockedTimeout
    if fault.kind in (Kind.CONNECTION, Kind.STATEMENT_TIMEOUT):
        return TemporarilyUnavailable
    if fault.phase.at_commit and not isinstance(fault.error(), sa_exc.DBAPIError):
        return TemporarilyUnavailable  # sin respuesta del servidor al confirmar: desconocido
    return type(fault.error())


def _retryable(fault: Fault | None) -> bool:
    return fault is not None and fault.kind is Kind.CONNECTION and not fault.phase.at_commit


async def _write(database: Database, context: ScopeContext) -> None:
    async with database.transaction(context) as transaction:
        await transaction.execute(USER_STATEMENT)


# --- Propiedades --------------------------------------------------------------------------------


@given(context=scope_contexts(), script=ATTEMPT_SCRIPTS)
def test_read_attempts_at_most_two_and_retry_only_connection_failures(
    context: ScopeContext, script: list[Fault | None]
) -> None:
    journal = Journal()
    database = _api_database(journal, script)
    first = script[0]
    second = script[1] if len(script) > 1 else None

    outcome: object
    try:
        outcome = [tuple(row) for row in asyncio.run(database.read(context, USER_STATEMENT))]
    except Exception as error:
        outcome = error

    assert 1 <= journal.attempts <= MAX_READ_ATTEMPTS
    if first is None:
        assert outcome == [(1,)]
        assert journal.attempts == 1
    elif _retryable(first):
        assert journal.attempts == 2
        assert journal.sleeps == [READ_RETRY_DELAY_SECONDS]
        if second is None:
            assert outcome == [(1,)]
        elif _retryable(second):
            assert type(outcome) is TemporarilyUnavailable
            assert outcome.retry_after_seconds == RETRY_AFTER_SECONDS
            assert not outcome.commit_outcome_unknown
        else:
            assert type(outcome) is _expected_error(second)
    else:
        assert journal.attempts == 1
        assert journal.sleeps == []
        assert type(outcome) is _expected_error(first)
        if first.phase.at_commit and isinstance(outcome, TemporarilyUnavailable):
            assert outcome.commit_outcome_unknown == (first.kind is not Kind.STATEMENT_TIMEOUT)

    # Cada intento abre en solo lectura y fija las tres variables antes de la sentencia.
    assert all(journal.begins)
    assert all(journal.scope_before_statement)
    assert journal.scopes == [scope_parameters(context)] * len(journal.scopes)
    # Cada conexión sacada se devuelve o se descarta exactamente una vez.
    connections = journal.attempts - sum(
        1 for fault in script[: journal.attempts] if fault and fault.phase is Phase.ACQUIRE
    )
    assert journal.released + journal.discarded == connections


@given(context=scope_contexts(), script=ATTEMPT_SCRIPTS)
def test_write_runs_exactly_once_and_never_retries(
    context: ScopeContext, script: list[Fault | None]
) -> None:
    journal = Journal()
    database = _api_database(journal, script)
    fault = script[0]

    outcome: BaseException | None = None
    try:
        asyncio.run(_write(database, context))
    except Exception as error:
        outcome = error

    assert journal.attempts == 1
    assert journal.sleeps == []
    assert journal.user_statements <= 1
    assert journal.commits_sent <= 1
    assert journal.effects_applied <= 1
    assert journal.begins in ([], [False])  # [] si falló al sacar la conexión
    assert all(journal.scope_before_statement)
    if fault is None:
        assert outcome is None
        assert journal.effects_applied == 1
    else:
        assert type(outcome) is _expected_error(fault)
        assert journal.effects_applied == (1 if fault.phase is Phase.COMMIT_AFTER_APPLY else 0)
        if isinstance(outcome, TemporarilyUnavailable):
            assert outcome.retry_after_seconds == RETRY_AFTER_SECONDS
            unknown = fault.phase.at_commit and fault.kind is not Kind.STATEMENT_TIMEOUT
            assert outcome.commit_outcome_unknown == unknown


@given(
    context=scope_contexts(),
    operations=st.lists(st.sampled_from(["read", "write"]), min_size=1, max_size=8),
    script=st.lists(st.one_of(st.none(), faults()), max_size=16),
)
def test_any_sequence_never_applies_a_write_twice(
    context: ScopeContext, operations: list[str], script: list[Fault | None]
) -> None:
    """Secuencia de lecturas y escrituras sobre el mismo adaptador, con los fallos en cola.

    Cada intento consume el siguiente fallo de la cola, sea de la operación que sea: los topes
    se cumplen por operación y ninguna escritura aplica su efecto más de una vez.
    """
    journal = Journal()
    database = _api_database(journal, script)

    async def run() -> None:
        for operation in operations:
            attempts, effects = journal.attempts, journal.effects_applied
            with contextlib.suppress(Exception):
                if operation == "read":
                    await database.read(context, USER_STATEMENT)
                else:
                    await _write(database, context)
            if operation == "write":
                assert journal.attempts - attempts == 1
                assert journal.effects_applied - effects <= 1
            else:
                assert 1 <= journal.attempts - attempts <= MAX_READ_ATTEMPTS
                assert journal.effects_applied == effects  # una lectura no escribe

    asyncio.run(run())
    assert journal.effects_applied <= operations.count("write")
    assert journal.commits_sent <= journal.attempts
    assert len(journal.sleeps) <= operations.count("read")


NOT_A_CONTEXT = st.one_of(
    st.none(),
    st.text(max_size=10),
    st.integers(),
    st.dictionaries(st.text(max_size=5), st.text(max_size=5), max_size=3),
    st.uuids(),
)


@given(value=NOT_A_CONTEXT)
def test_without_context_no_connection_is_requested(value: object) -> None:
    journal = Journal()
    database = _api_database(journal, [])

    with pytest.raises(ContextAbsent):
        database.transaction(cast(ScopeContext, value))
    with pytest.raises(ContextAbsent):
        asyncio.run(database.read(cast(ScopeContext, value), USER_STATEMENT))
    assert journal.attempts == 0


# --- Casos de borde -----------------------------------------------------------------------------


def test_swallowed_statement_error_never_commits() -> None:
    journal = Journal()
    fault = Fault(Phase.EXECUTE, Kind.OTHER, lambda: _dbapi(sa_exc.IntegrityError, "23505"))
    database = _api_database(journal, [fault])

    async def swallow() -> None:
        async with database.transaction(make_context()) as transaction:
            with pytest.raises(sa_exc.IntegrityError):
                await transaction.execute(USER_STATEMENT)
            assert transaction.failed
            with pytest.raises(TransactionAborted):
                await transaction.execute(USER_STATEMENT)

    with pytest.raises(TransactionAborted):
        asyncio.run(swallow())
    assert journal.commits_sent == 0
    assert journal.released == 1


def test_caller_error_rolls_back_without_commit() -> None:
    journal = Journal()
    database = _api_database(journal, [])

    async def fail() -> None:
        async with database.transaction(make_context()) as transaction:
            await transaction.execute(USER_STATEMENT)
            raise InjectedOther("fallo del llamador")

    with pytest.raises(InjectedOther):
        asyncio.run(fail())
    assert journal.commits_sent == 0
    assert (journal.released, journal.discarded) == (1, 0)


def test_cancellation_discards_the_connection_and_is_not_retried() -> None:
    journal = Journal()
    fault = Fault(Phase.EXECUTE, Kind.OTHER, asyncio.CancelledError)
    database = _api_database(journal, [fault])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(database.read(make_context(), USER_STATEMENT))
    assert journal.attempts == 1
    assert (journal.released, journal.discarded) == (0, 1)


def test_hung_server_read_ends_after_two_bounded_attempts() -> None:
    """Un servidor que no responde: cada intento acaba en su tope y la lectura en 2 intentos."""
    journal = Journal()

    class HungPool(FakePool):
        async def acquire(self) -> ConnectionPort:
            self.journal.attempts += 1
            await asyncio.sleep(60)
            raise AssertionError("inalcanzable")

    pool = HungPool(journal)
    database = _database(
        {PoolClass.NODE: pool, PoolClass.PERSON: pool}, journal, attempt_timeout_seconds=0.05
    )

    async def timed() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(TemporarilyUnavailable):
            await database.read(make_context(), USER_STATEMENT)
        return loop.time() - start

    elapsed = asyncio.run(timed())
    assert journal.attempts == 2
    assert elapsed < 2 * 0.05 + 1.0


def test_abandoned_attempts_return_their_late_connections() -> None:
    """Al vencer el tope el intento no se cancela: si luego entrega conexión, vuelve al pool."""
    journal = Journal()

    class SlowPool(FakePool):
        async def acquire(self) -> ConnectionPort:
            self.journal.attempts += 1
            await asyncio.sleep(0.2)
            return FakeConnection(self.journal, None)

    pool = SlowPool(journal)
    database = _database(
        {PoolClass.NODE: pool, PoolClass.PERSON: pool}, journal, attempt_timeout_seconds=0.05
    )

    async def scenario() -> None:
        with pytest.raises(TemporarilyUnavailable):
            async with database.transaction(make_context()):
                raise AssertionError("no debe abrirse")
        with pytest.raises(TemporarilyUnavailable):
            await database.read(make_context(), USER_STATEMENT)
        await database._drain(5.0)  # los abandonados terminan solos y devuelven su conexión
        assert not database._abandoned

    asyncio.run(scenario())
    assert journal.attempts == 3  # 1 escritura + 2 lecturas
    assert journal.user_statements == 2  # las lecturas abandonadas terminan solas
    assert journal.commits_sent == 2
    assert journal.effects_applied == 0
    assert (journal.released, journal.discarded) == (3, 0)


COMMAND_TIMEOUT = 0.05
ATTEMPT_TIMEOUT = 0.1
SLACK = 0.5


class HangingPool(FakePool):
    """Cada conexión deja de responder en ``hang_at`` (sentencia, COMMIT o devolución)."""

    def __init__(self, journal: Journal, hang_at: Phase) -> None:
        super().__init__(journal)
        self.hang_at = hang_at

    async def acquire(self) -> ConnectionPort:
        self.journal.attempts += 1
        return FakeConnection(self.journal, None, hang_at=self.hang_at)


@given(
    context=scope_contexts(),
    hang_at=st.sampled_from([Phase.EXECUTE, Phase.COMMIT_BEFORE_APPLY, Phase.RELEASE]),
    operation=st.sampled_from(["read", "write"]),
)
def test_a_hung_server_never_blocks_the_caller(
    context: ScopeContext, hang_at: Phase, operation: str
) -> None:
    """Revisión de VIG-26: con el servidor colgado en la sentencia, el COMMIT o la devolución,
    la operación termina dentro de su tope, la escritura tiene exactamente 1 intento y ninguna
    conexión se queda fuera del pool (cada una se devuelve o se retira una sola vez)."""
    journal = Journal()
    pool = HangingPool(journal, hang_at)
    database = _database(
        {PoolClass.NODE: pool, PoolClass.PERSON: pool},
        journal,
        attempt_timeout_seconds=ATTEMPT_TIMEOUT,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )

    async def scenario() -> tuple[object, float]:
        loop = asyncio.get_running_loop()
        start = loop.time()
        outcome: object
        try:
            if operation == "read":
                outcome = [tuple(row) for row in await database.read(context, USER_STATEMENT)]
            else:
                await _write(database, context)
                outcome = None
        except Exception as error:
            outcome = error
        elapsed = loop.time() - start
        await database._drain(5.0)
        assert not database._abandoned
        return outcome, elapsed

    outcome, elapsed = asyncio.run(scenario())

    if operation == "write":
        assert journal.attempts == 1
        assert elapsed < COMMAND_TIMEOUT + SLACK
        if hang_at is Phase.EXECUTE:
            assert type(outcome) is TemporarilyUnavailable
            assert not outcome.commit_outcome_unknown
            assert journal.commits_sent == 0
        elif hang_at is Phase.COMMIT_BEFORE_APPLY:
            assert type(outcome) is TemporarilyUnavailable
            assert outcome.commit_outcome_unknown
        else:  # confirmada; solo se colgó la devolución: se retira sin molestar al llamador
            assert outcome is None
            assert journal.effects_applied == 1
    else:
        assert journal.effects_applied == 0
        if hang_at is Phase.EXECUTE:
            assert type(outcome) is TemporarilyUnavailable
            assert journal.attempts == MAX_READ_ATTEMPTS
            assert elapsed < MAX_READ_ATTEMPTS * ATTEMPT_TIMEOUT + SLACK
        elif hang_at is Phase.COMMIT_BEFORE_APPLY:
            assert type(outcome) is TemporarilyUnavailable
            assert outcome.commit_outcome_unknown
            assert journal.attempts == 1  # tras el COMMIT, nunca se reintenta
            assert elapsed < ATTEMPT_TIMEOUT + SLACK
        else:
            assert outcome == [(1,)]
            assert journal.attempts == 1
            assert elapsed < ATTEMPT_TIMEOUT + SLACK
    # Toda conexión colgada se cortó y se retiró; ninguna se devolvió y además se retiró.
    assert journal.terminated >= 1
    assert journal.released + journal.discarded == journal.attempts


def test_dispose_is_bounded_while_a_step_hangs() -> None:
    """``dispose`` no espera sin límite a un paso que no termina (p. ej. una comprobación
    previa contra un servidor colgado, que el adaptador no puede cortar)."""
    journal = Journal()

    class NeverPool(FakePool):
        async def acquire(self) -> ConnectionPort:
            self.journal.attempts += 1
            await asyncio.Event().wait()
            raise AssertionError("inalcanzable")

    pool = NeverPool(journal)
    database = _database(
        {PoolClass.NODE: pool, PoolClass.PERSON: pool},
        journal,
        attempt_timeout_seconds=ATTEMPT_TIMEOUT,
        command_timeout_seconds=COMMAND_TIMEOUT,
    )

    async def scenario() -> float:
        with pytest.raises(TemporarilyUnavailable):
            await database.read(make_context(), USER_STATEMENT)
        assert database._abandoned  # los dos intentos siguen colgados
        loop = asyncio.get_running_loop()
        start = loop.time()
        await database.dispose()
        assert not database._abandoned
        return loop.time() - start

    elapsed = asyncio.run(scenario())
    assert elapsed < 2 * ATTEMPT_TIMEOUT + SLACK


def test_swallowed_connection_error_discards_without_network() -> None:
    """Menor 2 de la revisión: si el llamador se traga un error de conexión y sale del bloque,
    la conexión rota se descarta (sin ir a la red para revertir), igual que en la rama de
    excepción, y se avisa con ``TransactionAborted``."""
    journal = Journal()
    fault = Fault(Phase.EXECUTE, Kind.CONNECTION, lambda: ConnectionResetError("rota"))
    database = _api_database(journal, [fault])

    async def swallow() -> None:
        async with database.transaction(make_context()) as transaction:
            with pytest.raises(TemporarilyUnavailable):
                await transaction.execute(USER_STATEMENT)
            assert transaction.broken

    with pytest.raises(TransactionAborted):
        asyncio.run(swallow())
    assert (journal.released, journal.discarded, journal.commits_sent) == (0, 1, 0)


def test_route_class_selects_the_pool_and_worker_uses_its_own() -> None:
    journals = {pool_class: Journal() for pool_class in PoolClass}
    pools = {pool_class: FakePool(journals[pool_class]) for pool_class in PoolClass}
    api = _database(
        {PoolClass.NODE: pools[PoolClass.NODE], PoolClass.PERSON: pools[PoolClass.PERSON]},
        Journal(),
    )
    worker = _database(
        {PoolClass.WORKER: pools[PoolClass.WORKER]}, Journal(), process=ProcessKind.WORKER
    )
    node_context = make_context(kind=ActorKind.NODE)

    async def scenario() -> None:
        await api.read(node_context, USER_STATEMENT)  # sin clase: person
        with route_class_scope(RouteClass.NODE):
            await api.read(node_context, USER_STATEMENT)
            await worker.read(node_context, USER_STATEMENT)  # el worker ignora la clase
        with route_class_scope(RouteClass.PERSON):
            await _write(api, node_context)

    asyncio.run(scenario())
    assert journals[PoolClass.PERSON].attempts == 2
    assert journals[PoolClass.NODE].attempts == 1
    assert journals[PoolClass.WORKER].attempts == 1


def test_database_rejects_pools_that_do_not_match_the_process() -> None:
    journal = Journal()
    pool = FakePool(journal)
    with pytest.raises(ValueError, match="no corresponden"):
        _database({PoolClass.WORKER: pool}, journal)
    with pytest.raises(ValueError, match="no corresponden"):
        _database({PoolClass.NODE: pool}, journal)
    with pytest.raises(ValueError, match="no corresponden"):
        _database(
            {PoolClass.NODE: pool, PoolClass.PERSON: pool}, journal, process=ProcessKind.WORKER
        )
    with pytest.raises(TypeError):
        route_class_scope(cast(RouteClass, "node")).__enter__()
