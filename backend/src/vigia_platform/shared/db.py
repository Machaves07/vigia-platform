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
de pool 5 s. En el cliente, cada paso de la petición tiene su tope y nunca espera más:

- **tope de comando** = ``statement_timeout`` + 1 s (11 s; 31 s en el worker): cada sentencia
  de ``Transaction.execute``, el ``COMMIT`` y la devolución de la conexión al pool;
- **tope del intento** = conexión + tope de comando (16 s): la apertura de la transacción
  (sacar conexión, ``BEGIN`` y ``SET LOCAL``) y cada intento de lectura completo;
- **tope de una lectura** = dos topes del intento + 100 ms (32,1 s por defecto; 21,1 s medidos
  con la base pausada), ``[objetivo propio]`` derivado de NFR-NUC-36 que la cadena de
  middleware (TASK-134) debe tener en cuenta en el tiempo de la petición.

Al vencer un tope, el llamador recibe el error de inmediato y el paso se abandona **sin
cancelarlo** (cancelar a SQLAlchemy a mitad del cierre de una conexión rota la deja fuera del
pool): se corta el transporte de su conexión (``terminate`` de asyncpg, sin red), el paso
colgado falla enseguida y la conexión se retira del pool en segundo plano. Una sentencia que
vence da ``TemporarilyUnavailable``; un ``COMMIT`` que vence, además ``commit_outcome_unknown``.
Lo único que puede seguir esperando al servidor es una comprobación previa en curso dentro del
pool; ``dispose`` espera a lo abandonado como mucho el tope del intento y cancela el resto.
Comprobación previa de cada conexión (``pool_pre_ping``) y ``sslmode=verify-full`` por defecto.

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

    def terminate(self) -> None:
        """Corta ya el transporte del controlador, sin red ni espera (servidor colgado)."""
        ...


class PoolPort(Protocol):
    async def acquire(self) -> ConnectionPort: ...

    async def dispose(self) -> None: ...


class _EngineConnection:
    __slots__ = ("_connection", "_driver")

    def __init__(self, connection: AsyncConnection, driver: Any) -> None:
        self._connection = connection
        self._driver = driver
        """La ``asyncpg.Connection`` de debajo, para cortarla sin pasar por SQLAlchemy."""

    def terminate(self) -> None:
        # ``terminate`` de asyncpg cierra el transporte y cancela sus peticiones de
        # cancelación pendientes: lo que dejaba colgado el cierre elegante de SQLAlchemy.
        with contextlib.suppress(Exception):
            self._driver.terminate()

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
        # Primero se corta el transporte: así la invalidación de SQLAlchemy no espera a un
        # servidor que no responde.
        self.terminate()
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
        try:
            raw = await connection.get_raw_connection()
        except BaseException:
            await connection.close()
            raise
        return _EngineConnection(connection, raw.driver_connection)

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


class _Abandoned:
    """Marca de que un paso se abandonó: su conexión se retira en segundo plano."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = False

    def set(self) -> None:
        self.value = True


# --- Transacción y adaptador --------------------------------------------------------------------


class Transaction:
    """La transacción abierta por ``Database.transaction``: solo ejecuta sentencias."""

    __slots__ = ("_broken", "_connection", "_context", "_database", "_failed", "_retired")

    def __init__(
        self, connection: ConnectionPort, database: Database, context: ScopeContext
    ) -> None:
        self._connection = connection
        self._database = database
        self._context = context
        self._failed = False
        self._broken = False
        self._retired = _Abandoned()

    @property
    def context(self) -> ScopeContext:
        """El contexto con el que se abrió: el de las variables fijadas con ``SET LOCAL``."""
        return self._context

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
        """Ejecuta ``statement`` con parámetros y tope de comando; traduce los fallos transitorios.

        Si el servidor no responde dentro de ``command_timeout_seconds``, la sentencia se
        abandona (sin cancelarla), la conexión se corta y se retira en segundo plano, y aquí se
        lanza ``TemporarilyUnavailable``. Nunca se reintenta.
        """
        if self._failed:
            raise TransactionAborted()
        try:
            return await self._database._within(
                self._connection.execute(statement, parameters),
                self._database._command_timeout,
                connection=self._connection,
                abandoned=self._retired,
            )
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
        command_timeout_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(process, ProcessKind):
            raise TypeError("process debe ser ProcessKind")
        if set(pools) != set(_POOL_CLASSES[process]):
            raise ValueError("los pools no corresponden al proceso")
        self._pools = dict(pools)
        self._process = process
        self._attempt_timeout = attempt_timeout_seconds
        self._command_timeout = command_timeout_seconds
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
            command_timeout_seconds=settings.command_timeout_seconds,
            sleep=sleep,
        )

    async def _drain(self, timeout: float | None = None) -> None:
        """Espera a los pasos abandonados como mucho ``timeout`` (por defecto, el del intento)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self._attempt_timeout if timeout is None else timeout)
        while self._abandoned:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.wait(set(self._abandoned), timeout=remaining)

    async def dispose(self) -> None:
        """Cierra los pools; con la base caída, en un tiempo acotado.

        Espera a los pasos abandonados como mucho el tope del intento; los que sigan pendientes
        (p. ej. una comprobación previa contra un servidor colgado) se cancelan: en el apagado
        ya no importa devolver su conexión, y ``engine.dispose`` cierra las del pool.
        """
        await self._drain()
        for pending in list(self._abandoned):
            pending.cancel()
        if self._abandoned:
            await asyncio.wait(set(self._abandoned), timeout=self._attempt_timeout)
        for pool in self._pools.values():
            await pool.dispose()

    async def _within[T](
        self,
        work: Coroutine[Any, Any, T],
        timeout: float,
        *,
        connection: ConnectionPort | None = None,
        abandoned: _Abandoned | None = None,
        after: Callable[[asyncio.Future[T]], Awaitable[None]] | None = None,
    ) -> T:
        """Espera ``work`` como mucho ``timeout``, **sin cancelarlo** al vencer.

        Cancelar a SQLAlchemy a mitad de la comprobación previa o del cierre de una conexión
        rota la deja fuera del pool hasta que la recoge el recolector de basura. Por eso el paso
        corre en su propia tarea. Al vencer el tope (o si cancelan al llamador), el llamador
        recibe el error de inmediato y el paso se abandona:

        - si hay ``connection``, se corta su transporte (``terminate``): el paso colgado falla
          enseguida y la conexión se retira del pool en segundo plano;
        - ``abandoned`` queda marcado, para que quien llama no vuelva a tocar esa conexión;
        - ``after`` corre cuando el paso termina (p. ej. devolver una conexión entregada tarde).
        """
        task = asyncio.ensure_future(work)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except BaseException:
            if not task.done():
                if abandoned is not None:
                    abandoned.set()
                if connection is not None:
                    connection.terminate()
                    retire = connection

                    async def discard(_: asyncio.Future[T]) -> None:
                        await _discard_quietly(retire)

                    after = after or discard
                self._abandon(task, after)
            raise

    def _abandon[T](
        self,
        task: asyncio.Future[T],
        after: Callable[[asyncio.Future[T]], Awaitable[None]] | None,
    ) -> None:
        self._abandoned.add(task)

        def finished(done: asyncio.Future[T]) -> None:
            self._abandoned.discard(done)
            if not done.cancelled():
                done.exception()  # recogida: un paso abandonado no deja avisos sin leer
            if after is None:
                return
            cleanup = asyncio.ensure_future(after(done))
            self._abandoned.add(cleanup)
            cleanup.add_done_callback(self._abandoned.discard)

        task.add_done_callback(finished)

    async def _release(self, connection: ConnectionPort) -> None:
        """Devuelve la conexión al pool con tope de comando; si no responde, la retira."""
        abandoned = _Abandoned()
        try:
            await self._within(
                connection.release(),
                self._command_timeout,
                connection=connection,
                abandoned=abandoned,
            )
        except BaseException:
            if not abandoned.value:
                await _discard_quietly(connection)

    async def _release_late(self, done: asyncio.Future[ConnectionPort]) -> None:
        """Una apertura abandonada que acabó entregando conexión: se devuelve al pool."""
        if not done.cancelled() and done.exception() is None:
            await self._release(done.result())

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
        self, pool: PoolPort, context: ScopeContext, attempt: _Attempt
    ) -> ConnectionPort:
        """Saca una conexión, abre la transacción de escritura y fija las tres variables."""
        connection = attempt.connection = await pool.acquire()
        try:
            await connection.begin(read_only=False)
            await connection.execute(_SET_SCOPE, scope_parameters(context))
        except BaseException:
            attempt.connection = None
            await _discard_quietly(connection)
            raise
        attempt.connection = None  # desde aquí la conexión es de quien la pidió
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
        attempt = _Attempt()
        abandoned = _Abandoned()
        try:
            try:
                connection = await self._within(
                    self._open(self._pool(), context, attempt),
                    self._attempt_timeout,
                    abandoned=abandoned,
                    after=self._release_late,
                )
            finally:
                # Apertura abandonada a mitad de BEGIN o del SET LOCAL: se corta la conexión
                # para que la apertura colgada falle enseguida y la retire ella misma.
                if abandoned.value and attempt.connection is not None:
                    attempt.connection.terminate()
        except Exception as error:
            translated = _translate(error)
            if translated is None:
                raise
            raise translated from error
        transaction = Transaction(connection, self, context)
        try:
            yield transaction
        except BaseException as error:
            await self._close_after_error(connection, transaction, error)
            raise
        if transaction.failed:
            await self._close_after_error(connection, transaction, None)
            raise TransactionAborted()
        try:
            await self._within(
                connection.commit(),
                self._command_timeout,
                connection=connection,
                abandoned=transaction._retired,
            )
        except BaseException as error:
            if not transaction._retired.value:
                await _discard_quietly(connection)
            if not isinstance(error, Exception):
                raise
            # Tras enviar el COMMIT nunca se reintenta (PR-NUC-41); si venció el tope, el
            # resultado es desconocido (TimeoutError se traduce como fallo de conexión).
            translated = _translate(error, after_commit=True)
            if translated is None:
                raise
            raise translated from error
        await self._release(connection)

    async def _close_after_error(
        self, connection: ConnectionPort, transaction: Transaction, error: BaseException | None
    ) -> None:
        """Cierra la transacción que no se confirma, sin esperar a un servidor que no responde.

        Si un paso se abandonó, la conexión ya se retira en segundo plano. Si hubo fallo de
        conexión o una cancelación, se descarta sin ir a la red. Si no, se revierte devolviéndola
        al pool con tope de comando.
        """
        if transaction._retired.value:
            return
        if transaction.broken or (error is not None and not isinstance(error, Exception)):
            await _discard_quietly(connection)
        else:
            await self._release(connection)

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
            progress = _Attempt()
            try:
                return await self._attempt_read(pool, context, statement, parameters, progress)
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

    async def _attempt_read(
        self,
        pool: PoolPort,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None,
        progress: _Attempt,
    ) -> Sequence[Row[Any]]:
        """Un intento de lectura con el tope del intento.

        Si vence con una conexión ya entregada, se corta su transporte: el intento colgado falla
        enseguida y él mismo la retira del pool (no queda esperando al servidor).
        """
        abandoned = _Abandoned()
        try:
            return await self._within(
                self._read_once(pool, context, statement, parameters, progress),
                self._attempt_timeout,
                abandoned=abandoned,
            )
        finally:
            if abandoned.value and progress.connection is not None:
                progress.connection.terminate()

    async def _read_once(
        self,
        pool: PoolPort,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None,
        progress: _Attempt,
    ) -> Sequence[Row[Any]]:
        connection: ConnectionPort | None = None
        try:
            connection = progress.connection = await pool.acquire()
            await connection.begin(read_only=True)
            await connection.execute(_SET_SCOPE, scope_parameters(context))
            rows = (await connection.execute(statement, parameters)).all()
            progress.committing = True
            await connection.commit()
        except Exception as error:
            kind = _classify(error)
            committing = progress.committing
            if connection is not None:
                progress.connection = None
                if kind is _FailureKind.CONNECTION or committing:
                    await _discard_quietly(connection)
                else:
                    await self._release(connection)
            if kind is _FailureKind.CONNECTION and not committing:
                raise _RetryableRead() from error
            translated = _translate(error, after_commit=committing)
            if translated is None:
                raise
            raise translated from error
        except BaseException:
            if connection is not None:
                progress.connection = None
                await _discard_quietly(connection)
            raise
        # La devolución tiene su propio tope (``_release``): el llamador ya no la corta.
        progress.connection = None
        await self._release(connection)
        return rows


class _RetryableRead(Exception):
    """Fallo de conexión antes del ``COMMIT`` de una lectura: admite el único reintento."""


@dataclass(slots=True)
class _Attempt:
    """Hasta dónde llegó un intento (apertura o lectura); lo consulta el llamador si vence."""

    committing: bool = False
    connection: ConnectionPort | None = None
    """La conexión en uso por el intento, mientras la tenga; se corta si el intento se abandona."""
