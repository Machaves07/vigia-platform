"""Adaptador único de PostgreSQL (LC-NUC-29; PAT-NUC-RES-01, RES-03, SEG-01; NFR-NUC-36, 37).

Toda consulta de datos pasa por aquí y solo con un ``ScopeContext``:

- ``async with db.transaction(context) as tx``: abre la transacción y, antes de cualquier otra
  sentencia, fija con ``set_config(..., true)`` (el ``SET LOCAL`` con parámetros) las tres
  variables que leen las políticas de seguridad a nivel de fila: ``vigia.organization_id``,
  ``vigia.actor_kind`` y ``vigia.concession_id`` (vacía salvo bajo concesión). Al salir sin
  error confirma; con error revierte. **Nunca se reintenta**: un fallo de conexión en cualquier
  punto termina en ``TemporarilyUnavailable`` con ``retry_after_seconds = 5``; un fallo al
  confirmar, además, con ``commit_outcome_unknown`` (la transacción pudo confirmarse).
- ``await db.read(context, statement)``: la misma apertura en una transacción ``READ ONLY``;
  si falla por conexión antes del ``COMMIT`` se reintenta **una sola vez** tras 100 ms con otra
  conexión; si vuelve a fallar, ``TemporarilyUnavailable``. Tras enviar el ``COMMIT`` nunca se
  reintenta (PR-NUC-41).
- Sin contexto no existe forma de abrir una transacción: ``transaction`` y ``read`` lanzan
  ``ContextAbsent`` antes de pedir una conexión al pool (BR-NUC-02).

Traducción de errores: ``lock_not_available`` (``lock_timeout``) → ``ChainLockedTimeout``;
conexión perdida o imposible, tiempo de espera agotado, pool agotado, ``statement_timeout``,
serialización o interbloqueo → ``TemporarilyUnavailable``. El resto (p. ej. una violación de
unicidad) sale sin traducir para que el llamador decida.

**Selector de motor por clase de ruta** (pendientes nº 17 y 37, adenda A-21): en ``vigia-api``
hay dos pools por proceso, ``node`` de 10 y ``person`` de 5 conexiones, sin desbordamiento; el
eslabón de clase de ruta de la cadena de middleware (TASK-134) fija la clase con
``route_class_scope``; sin clase, ``person``. Un pool agotado no frena al otro (mamparo). En
``vigia-worker``, un pool de 20.

Tiempos de espera (NFR-NUC-36, ``[objetivos propios]``): conexión 5 s; ``statement_timeout``
10 s (30 s solo en el worker) y ``lock_timeout`` 2 s fijados en el servidor por conexión; espera
de pool 5 s; cada comando tiene además un tope en el cliente de ``statement_timeout`` + 1 s (un
servidor que no responde no cuelga la petición); y la apertura (y el intento de lectura
completo) tiene un tope de conexión + comando. Al vencer ese tope el llamador recibe el error y
el intento se abandona **sin cancelarlo**: termina solo, acotado por los topes del controlador,
y devuelve su conexión al pool (cancelar a SQLAlchemy a mitad del cierre de una conexión rota
la dejaría fuera del pool). Comprobación previa de cada conexión (``pool_pre_ping``) y
``sslmode=verify-full`` por defecto.

Métricas: ``db_pool_size`` y ``db_pool_in_use`` con el atributo ``pool_class``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import ssl
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    Sequence,
)
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy import event, text
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Result, Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import QueuePool
from sqlalchemy.sql import Executable

from vigia_platform.shared.context import ContextAbsent, ScopeContext
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics

__all__ = [
    "MAX_READ_ATTEMPTS",
    "READ_RETRY_DELAY_SECONDS",
    "RETRY_AFTER_SECONDS",
    "ChainLockedTimeout",
    "ConnectionPort",
    "Database",
    "DatabaseSettings",
    "PoolClass",
    "PoolPort",
    "ProcessKind",
    "RouteClass",
    "SslMode",
    "TemporarilyUnavailable",
    "Transaction",
    "TransactionAborted",
    "TransientDatabaseError",
    "current_route_class",
    "route_class_scope",
    "scope_parameters",
]

MAX_READ_ATTEMPTS: Final = 2
"""Intentos por lectura: el original y un único reintento (PAT-NUC-RES-01)."""
READ_RETRY_DELAY_SECONDS: Final = 0.1
"""Espera antes del reintento de una lectura ``[objetivo propio]``."""
RETRY_AFTER_SECONDS: Final = 5
"""``retry_after_seconds`` de ``temporarily_unavailable`` ``[objetivo propio]``."""
COMMAND_TIMEOUT_MARGIN_SECONDS: Final = 1.0
"""Holgura del tope de cada comando en el cliente sobre ``statement_timeout``."""

_log = get_logger("shared.db")

_SET_SCOPE: Final = text(
    "SELECT set_config('vigia.organization_id', :organization_id, true), "
    "set_config('vigia.actor_kind', :actor_kind, true), "
    "set_config('vigia.concession_id', :concession_id, true)"
)
"""El ``SET LOCAL`` de las tres variables, con parámetros (NFR-NUC-19): una sola ida y vuelta."""

_CONNECTION_SQLSTATES: Final = frozenset({"57P01", "57P02", "57P03", "53300"})
"""``admin_shutdown``, ``crash_shutdown``, ``cannot_connect_now``, ``too_many_connections``;
más toda la clase ``08`` (``connection_exception``)."""
_LOCK_TIMEOUT_SQLSTATE: Final = "55P03"
"""``lock_not_available``: lo que produce ``lock_timeout``."""
_TRANSIENT_SQLSTATES: Final = frozenset({"57014", "40001", "40P01"})
"""``query_canceled`` (``statement_timeout``), ``serialization_failure``, ``deadlock_detected``."""


# --- Errores -----------------------------------------------------------------------------------


class TransientDatabaseError(Exception):
    """Fallo transitorio de la base traducido a un código de la interfaz."""

    code: str


class TemporarilyUnavailable(TransientDatabaseError):
    """La base no respondió a tiempo o se perdió la conexión (NFR-NUC-37)."""

    code = "temporarily_unavailable"

    def __init__(
        self,
        *,
        retry_after_seconds: int = RETRY_AFTER_SECONDS,
        commit_outcome_unknown: bool = False,
    ) -> None:
        super().__init__("base de datos temporalmente no disponible")
        self.retry_after_seconds = retry_after_seconds
        self.commit_outcome_unknown = commit_outcome_unknown
        """``True`` si el fallo llegó tras enviar el ``COMMIT``: pudo confirmarse."""


class ChainLockedTimeout(TransientDatabaseError):
    """La espera de un bloqueo superó ``lock_timeout`` (PAT-NUC-RES-08); transitorio."""

    code = "chain_locked_timeout"

    def __init__(self) -> None:
        super().__init__("espera de bloqueo agotada")


class TransactionAborted(Exception):
    """Una sentencia de la transacción falló y el llamador intentó seguir o confirmar.

    PostgreSQL convertiría ese ``COMMIT`` en un ``ROLLBACK`` silencioso: aquí se revierte y se
    avisa, para que nunca se dé por escrito lo que no se escribió (P5, BR-CTR-31).
    """

    def __init__(self) -> None:
        super().__init__("la transacción ya falló: se revirtió sin confirmar")


class _FailureKind(enum.Enum):
    CONNECTION = enum.auto()
    LOCK_TIMEOUT = enum.auto()
    TRANSIENT = enum.auto()
    OTHER = enum.auto()


def _sqlstate(error: BaseException) -> str | None:
    for candidate in (error, getattr(error, "orig", None), error.__cause__):
        value = getattr(candidate, "sqlstate", None)
        if isinstance(value, str):
            return value
    return None


def _classify(error: BaseException) -> _FailureKind:
    """Clase de fallo de ``error`` para decidir traducción y reintento."""
    sqlstate = _sqlstate(error)
    if sqlstate == _LOCK_TIMEOUT_SQLSTATE:
        return _FailureKind.LOCK_TIMEOUT
    if sqlstate in _TRANSIENT_SQLSTATES:
        return _FailureKind.TRANSIENT
    if sqlstate is not None and (sqlstate.startswith("08") or sqlstate in _CONNECTION_SQLSTATES):
        return _FailureKind.CONNECTION
    if isinstance(
        error,
        TimeoutError  # tope del cliente (asyncpg o ``asyncio.timeout``)
        | OSError  # conexión rechazada, reiniciada o de red
        | sa_exc.TimeoutError  # espera de pool agotada
        | sa_exc.DisconnectionError
        | sa_exc.InterfaceError  # conexión cerrada
        | asyncpg.exceptions.InterfaceError
        | asyncpg.exceptions.ConnectionDoesNotExistError,
    ):
        return _FailureKind.CONNECTION
    if isinstance(error, sa_exc.DBAPIError) and error.connection_invalidated:
        return _FailureKind.CONNECTION
    return _FailureKind.OTHER


def _translate(
    error: BaseException, *, after_commit: bool = False
) -> TransientDatabaseError | None:
    """Error de la interfaz para ``error``, o ``None`` si sale sin traducir.

    ``after_commit``: el fallo llegó al confirmar. Si el servidor respondió con un error (tiene
    SQLSTATE), la transacción no se confirmó; si no respondió, el resultado es desconocido.
    """
    kind = _classify(error)
    if kind is _FailureKind.LOCK_TIMEOUT:
        return ChainLockedTimeout()
    if kind is _FailureKind.TRANSIENT:
        return TemporarilyUnavailable()
    if kind is _FailureKind.CONNECTION:
        return TemporarilyUnavailable(commit_outcome_unknown=after_commit)
    if after_commit and _sqlstate(error) is None:
        return TemporarilyUnavailable(commit_outcome_unknown=True)
    return None


# --- Clase de ruta ------------------------------------------------------------------------------


class ProcessKind(enum.StrEnum):
    API = "api"
    WORKER = "worker"


class RouteClass(enum.StrEnum):
    """Clase de la petición en ``vigia-api`` (pendiente nº 37): elige el pool."""

    NODE = "node"
    PERSON = "person"


class PoolClass(enum.StrEnum):
    """Pool de conexiones; su valor es el atributo ``pool_class`` de las métricas."""

    NODE = "node"
    PERSON = "person"
    WORKER = "worker"


_route_class: ContextVar[RouteClass | None] = ContextVar("vigia_route_class", default=None)


def current_route_class() -> RouteClass:
    """Clase de ruta de la petición en curso; sin clase fijada, ``person``."""
    return _route_class.get() or RouteClass.PERSON


@contextlib.contextmanager
def route_class_scope(route_class: RouteClass) -> Iterator[None]:
    """Fija la clase de ruta mientras dura el bloque (lo usa el eslabón de TASK-134)."""
    if not isinstance(route_class, RouteClass):
        raise TypeError("route_class debe ser RouteClass")
    token: Token[RouteClass | None] = _route_class.set(route_class)
    try:
        yield
    finally:
        _route_class.reset(token)


# --- Configuración ------------------------------------------------------------------------------


class SslMode(enum.StrEnum):
    DISABLE = "disable"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


_POOL_CLASSES: Final[Mapping[ProcessKind, tuple[PoolClass, ...]]] = {
    ProcessKind.API: (PoolClass.NODE, PoolClass.PERSON),
    ProcessKind.WORKER: (PoolClass.WORKER,),
}
_STATEMENT_TIMEOUT_MS: Final[Mapping[ProcessKind, int]] = {
    ProcessKind.API: 10_000,
    ProcessKind.WORKER: 30_000,
}


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    """Parámetros del adaptador; los valores por defecto son los de NFR-NUC-36 y la adenda A-21."""

    url: str = field(repr=False)
    """``postgresql+asyncpg://…``; lleva la contraseña: nunca se registra."""
    process: ProcessKind
    sslmode: SslMode = SslMode.VERIFY_FULL
    ssl_root_cert: str | None = None
    """Certificado raíz de la autoridad (p. ej. el paquete de RDS) para ``verify-ca/full``."""
    connect_timeout_seconds: float = 5.0
    statement_timeout_ms: int | None = None
    """Por defecto 10 000 en ``api`` y 30 000 en ``worker``."""
    lock_timeout_ms: int = 2_000
    pool_timeout_seconds: float = 5.0
    node_pool_size: int = 10
    person_pool_size: int = 5
    worker_pool_size: int = 20

    def __post_init__(self) -> None:
        if not self.url.startswith("postgresql+asyncpg://"):
            raise ValueError("url debe usar el controlador postgresql+asyncpg")
        if not isinstance(self.process, ProcessKind) or not isinstance(self.sslmode, SslMode):
            raise TypeError("process y sslmode deben ser ProcessKind y SslMode")
        positive: tuple[float, ...] = (
            self.connect_timeout_seconds,
            self.effective_statement_timeout_ms,
            self.lock_timeout_ms,
            self.pool_timeout_seconds,
            *self.pool_sizes.values(),
        )
        if any(value <= 0 for value in positive):
            raise ValueError("tiempos de espera y tamaños de pool deben ser positivos")

    @property
    def effective_statement_timeout_ms(self) -> int:
        if self.statement_timeout_ms is not None:
            return self.statement_timeout_ms
        return _STATEMENT_TIMEOUT_MS[self.process]

    @property
    def command_timeout_seconds(self) -> float:
        """Tope de cada comando en el cliente: ``statement_timeout`` más la holgura."""
        return self.effective_statement_timeout_ms / 1000 + COMMAND_TIMEOUT_MARGIN_SECONDS

    @property
    def attempt_timeout_seconds(self) -> float:
        """Tope de la apertura de una transacción y de cada intento de lectura."""
        return self.connect_timeout_seconds + self.command_timeout_seconds

    @property
    def read_timeout_seconds(self) -> float:
        """Tope de una lectura completa: dos intentos y la espera entre ellos."""
        return MAX_READ_ATTEMPTS * self.attempt_timeout_seconds + READ_RETRY_DELAY_SECONDS

    @property
    def pool_sizes(self) -> Mapping[PoolClass, int]:
        sizes = {
            PoolClass.NODE: self.node_pool_size,
            PoolClass.PERSON: self.person_pool_size,
            PoolClass.WORKER: self.worker_pool_size,
        }
        return {pool_class: sizes[pool_class] for pool_class in _POOL_CLASSES[self.process]}


def _ssl_argument(settings: DatabaseSettings) -> ssl.SSLContext | bool:
    if settings.sslmode is SslMode.DISABLE:
        return False
    context = ssl.create_default_context(cafile=settings.ssl_root_cert)
    if settings.sslmode is SslMode.REQUIRE:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif settings.sslmode is SslMode.VERIFY_CA:
        context.check_hostname = False
    return context


# --- Puertos del pool (el adaptador de SQLAlchemy y los dobles de prueba) -----------------------


class ConnectionPort(Protocol):
    """Una conexión sacada del pool."""

    async def begin(self, *, read_only: bool) -> None: ...

    async def execute(
        self, statement: Executable, parameters: Mapping[str, Any] | None
    ) -> Result[Any]: ...

    async def commit(self) -> None: ...

    async def release(self) -> None:
        """Devuelve la conexión sana al pool (revierte lo que quede abierto)."""
        ...

    async def discard(self) -> None:
        """Descarta la conexión sin ir a la red (tras un fallo o una cancelación)."""
        ...


class PoolPort(Protocol):
    async def acquire(self) -> ConnectionPort: ...

    async def dispose(self) -> None: ...


class _EngineConnection:
    __slots__ = ("_connection",)

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def begin(self, *, read_only: bool) -> None:
        if read_only:
            await self._connection.execution_options(postgresql_readonly=True)
        await self._connection.begin()

    async def execute(
        self, statement: Executable, parameters: Mapping[str, Any] | None
    ) -> Result[Any]:
        return await self._connection.execute(statement, parameters)

    async def commit(self) -> None:
        await self._connection.commit()

    async def release(self) -> None:
        await self._connection.close()

    async def discard(self) -> None:
        try:
            await self._connection.invalidate()
        finally:
            await self._connection.close()


class _EnginePool:
    __slots__ = ("engine",)

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def acquire(self) -> ConnectionPort:
        connection = self.engine.connect()
        await connection.start()
        return _EngineConnection(connection)

    async def dispose(self) -> None:
        await self.engine.dispose()


def _create_engine(settings: DatabaseSettings, pool_class: PoolClass, size: int) -> AsyncEngine:
    return create_async_engine(
        settings.url,
        pool_size=size,
        max_overflow=0,
        pool_timeout=settings.pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.connect_timeout_seconds,
            "command_timeout": settings.command_timeout_seconds,
            "ssl": _ssl_argument(settings),
            "server_settings": {
                "statement_timeout": str(settings.effective_statement_timeout_ms),
                "lock_timeout": str(settings.lock_timeout_ms),
                "application_name": f"vigia-{settings.process.value}-{pool_class.value}",
            },
        },
    )


def _install_pool_metrics(
    engine: AsyncEngine, pool_class: PoolClass, size: int, metrics: PlatformMetrics
) -> None:
    attributes = {"pool_class": pool_class.value}
    metrics.db_pool_size.set(size, attributes)
    metrics.db_pool_in_use.set(0, attributes)
    pool = engine.sync_engine.pool
    if not isinstance(pool, QueuePool):
        return

    def update(*_: object) -> None:
        metrics.db_pool_in_use.set(pool.checkedout(), attributes)

    event.listen(pool, "checkout", update)
    event.listen(pool, "checkin", update)


def scope_parameters(context: ScopeContext) -> dict[str, str]:
    """Valores de las tres variables de sesión para ``context``."""
    return {
        "organization_id": str(context.organization_id),
        "actor_kind": context.actor.kind.value,
        "concession_id": "" if context.concession_id is None else str(context.concession_id),
    }


async def _discard_quietly(connection: ConnectionPort) -> None:
    with contextlib.suppress(Exception):
        await connection.discard()


async def _release_or_discard(connection: ConnectionPort) -> None:
    """Devuelve la conexión al pool; si ni eso responde, la descarta."""
    try:
        await connection.release()
    except Exception:
        await _discard_quietly(connection)


# --- Transacción y adaptador --------------------------------------------------------------------


class Transaction:
    """La transacción abierta por ``Database.transaction``: solo ejecuta sentencias."""

    __slots__ = ("_broken", "_connection", "_failed")

    def __init__(self, connection: ConnectionPort) -> None:
        self._connection = connection
        self._failed = False
        self._broken = False

    @property
    def failed(self) -> bool:
        """Alguna sentencia falló: la transacción ya no puede confirmarse."""
        return self._failed

    @property
    def broken(self) -> bool:
        """El fallo fue de conexión: la conexión se descarta sin ir a la red."""
        return self._broken

    async def execute(
        self, statement: Executable, parameters: Mapping[str, Any] | None = None
    ) -> Result[Any]:
        """Ejecuta ``statement`` con parámetros; traduce los fallos transitorios."""
        if self._failed:
            raise TransactionAborted()
        try:
            return await self._connection.execute(statement, parameters)
        except Exception as error:
            self._failed = True
            self._broken = _classify(error) is _FailureKind.CONNECTION
            translated = _translate(error)
            if translated is None:
                raise
            raise translated from error
        except BaseException:
            self._failed = self._broken = True
            raise


class Database:
    """El adaptador: un pool por clase de ruta (``api``) o uno solo (``worker``)."""

    def __init__(
        self,
        pools: Mapping[PoolClass, PoolPort],
        *,
        process: ProcessKind,
        attempt_timeout_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(process, ProcessKind):
            raise TypeError("process debe ser ProcessKind")
        if set(pools) != set(_POOL_CLASSES[process]):
            raise ValueError("los pools no corresponden al proceso")
        self._pools = dict(pools)
        self._process = process
        self._attempt_timeout = attempt_timeout_seconds
        self._sleep = sleep
        self._abandoned: set[asyncio.Future[Any]] = set()

    @classmethod
    def create(
        cls,
        settings: DatabaseSettings,
        *,
        metrics: PlatformMetrics | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> Database:
        """Crea los motores de ``settings.process`` con sus métricas de pool."""
        metrics = metrics if metrics is not None else get_metrics()
        pools: dict[PoolClass, PoolPort] = {}
        for pool_class, size in settings.pool_sizes.items():
            engine = _create_engine(settings, pool_class, size)
            _install_pool_metrics(engine, pool_class, size, metrics)
            pools[pool_class] = _EnginePool(engine)
        return cls(
            pools,
            process=settings.process,
            attempt_timeout_seconds=settings.attempt_timeout_seconds,
            sleep=sleep,
        )

    async def dispose(self) -> None:
        """Espera los intentos abandonados (acotados por sus propios topes) y cierra los pools."""
        while self._abandoned:
            await asyncio.gather(*self._abandoned, return_exceptions=True)
        for pool in self._pools.values():
            await pool.dispose()

    async def _bounded[T](
        self,
        work: Coroutine[Any, Any, T],
        *,
        on_abandon: Callable[[T], Awaitable[None]] | None = None,
    ) -> T:
        """Espera ``work`` como mucho el tope del intento, **sin cancelarlo** al vencer.

        Cancelar a SQLAlchemy a mitad de la comprobación previa o del cierre de una conexión
        rota la deja fuera del pool hasta que la recoge el recolector de basura. Por eso el
        intento corre en su propia tarea: al vencer el tope (o si cancelan al llamador) se
        abandona y termina solo, acotado por los tiempos de espera del controlador; si llega a
        entregar una conexión, ``on_abandon`` la devuelve.
        """
        task = asyncio.ensure_future(work)
        try:
            return await asyncio.wait_for(asyncio.shield(task), self._attempt_timeout)
        except BaseException:
            if not task.done():
                self._abandon(task, on_abandon)
            raise

    def _abandon[T](
        self, task: asyncio.Future[T], on_abandon: Callable[[T], Awaitable[None]] | None
    ) -> None:
        self._abandoned.add(task)

        def finished(done: asyncio.Future[T]) -> None:
            self._abandoned.discard(done)
            if done.cancelled() or done.exception() is not None or on_abandon is None:
                return
            cleanup = asyncio.ensure_future(on_abandon(done.result()))
            self._abandoned.add(cleanup)
            cleanup.add_done_callback(self._abandoned.discard)

        task.add_done_callback(finished)

    def _pool(self) -> PoolPort:
        if self._process is ProcessKind.WORKER:
            return self._pools[PoolClass.WORKER]
        return self._pools[PoolClass(current_route_class().value)]

    @staticmethod
    def _require_context(context: object) -> ScopeContext:
        if not isinstance(context, ScopeContext):
            _log.warning("operación de datos sin contexto rechazada")
            raise ContextAbsent()
        return context

    async def _open(
        self, pool: PoolPort, context: ScopeContext, *, read_only: bool
    ) -> ConnectionPort:
        """Saca una conexión, abre la transacción y fija las tres variables."""
        connection = await pool.acquire()
        try:
            await connection.begin(read_only=read_only)
            await connection.execute(_SET_SCOPE, scope_parameters(context))
        except BaseException:
            await _discard_quietly(connection)
            raise
        return connection

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]:
        """Transacción de escritura con el contexto fijado; nunca se reintenta.

        Lanza ``ContextAbsent`` aquí mismo, antes de pedir una conexión, si falta el contexto.
        """
        return self._transaction(self._require_context(context))

    @contextlib.asynccontextmanager
    async def _transaction(self, context: ScopeContext) -> AsyncIterator[Transaction]:
        try:
            connection = await self._bounded(
                self._open(self._pool(), context, read_only=False),
                on_abandon=_release_or_discard,
            )
        except Exception as error:
            translated = _translate(error)
            if translated is None:
                raise
            raise translated from error
        transaction = Transaction(connection)
        try:
            yield transaction
        except BaseException as error:
            if isinstance(error, Exception) and not transaction.broken:
                await _release_or_discard(connection)
            else:
                await _discard_quietly(connection)
            raise
        if transaction.failed:
            await _release_or_discard(connection)
            raise TransactionAborted()
        try:
            await connection.commit()
        except BaseException as error:
            await _discard_quietly(connection)
            if not isinstance(error, Exception):
                raise
            # Tras enviar el COMMIT nunca se reintenta (PR-NUC-41).
            translated = _translate(error, after_commit=True)
            if translated is None:
                raise
            raise translated from error
        await _release_or_discard(connection)

    async def read(
        self,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Row[Any]]:
        """Lectura en transacción ``READ ONLY``: un único reintento tras un fallo de conexión."""
        context = self._require_context(context)
        pool = self._pool()
        for attempt in range(1, MAX_READ_ATTEMPTS + 1):
            progress = _ReadProgress()
            try:
                return await self._bounded(
                    self._read_once(pool, context, statement, parameters, progress)
                )
            except TimeoutError as error:  # venció el tope del intento
                if progress.committing:
                    # El COMMIT ya se envió: nunca se reintenta (PR-NUC-41).
                    raise TemporarilyUnavailable(commit_outcome_unknown=True) from error
                cause: BaseException | None = error
            except _RetryableRead as failure:
                cause = failure.__cause__
            if attempt == MAX_READ_ATTEMPTS:
                raise TemporarilyUnavailable() from cause
            _log.warning(
                "lectura reintentada tras un fallo de conexión",
                attempt=attempt,
                correlation_id=context.correlation_id,
                organization_id=context.organization_id,
            )
            await self._sleep(READ_RETRY_DELAY_SECONDS)
        raise AssertionError("inalcanzable")  # pragma: no cover

    async def _read_once(
        self,
        pool: PoolPort,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None,
        progress: _ReadProgress,
    ) -> Sequence[Row[Any]]:
        connection: ConnectionPort | None = None
        try:
            connection = await pool.acquire()
            await connection.begin(read_only=True)
            await connection.execute(_SET_SCOPE, scope_parameters(context))
            rows = (await connection.execute(statement, parameters)).all()
            progress.committing = True
            await connection.commit()
        except Exception as error:
            kind = _classify(error)
            committing = progress.committing
            if connection is not None:
                if kind is _FailureKind.CONNECTION or committing:
                    await _discard_quietly(connection)
                else:
                    await _release_or_discard(connection)
            if kind is _FailureKind.CONNECTION and not committing:
                raise _RetryableRead() from error
            translated = _translate(error, after_commit=committing)
            if translated is None:
                raise
            raise translated from error
        except BaseException:
            if connection is not None:
                await _discard_quietly(connection)
            raise
        await _release_or_discard(connection)
        return rows


class _RetryableRead(Exception):
    """Fallo de conexión antes del ``COMMIT`` de una lectura: admite el único reintento."""


@dataclass(slots=True)
class _ReadProgress:
    """Hasta dónde llegó un intento de lectura (lo consulta el llamador si vence el tope)."""

    committing: bool = False
