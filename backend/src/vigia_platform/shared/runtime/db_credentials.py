"""Credencial de ``vigia_app`` desde Secrets Manager y su relectura tras una rotación.

El secreto que nombra ``VIGIA_DB_APP_SECRET`` (``vigia/<despliegue>/db/app``, ``infra/stacks/
data.py``) tiene el formato de rotación de RDS: un objeto JSON con ``host``, ``port``,
``dbname``, ``username`` y ``password``. ``DatabaseCredentials`` lo lee al construir el proceso y
es la ``CredentialSource`` de ``shared.db``: cada conexión nueva sale con la contraseña vigente,
y cuando PostgreSQL la rechaza tras una rotación (runbook 6.6) el pool llama a ``renew``.

**Una sola relectura por rotación.** Una ráfaga de conexiones que fallan a la vez (20
transacciones simultáneas justo después de la rotación) produce **una** lectura del secreto:
``renew`` relee bajo un candado y solo si la credencial rechazada sigue siendo la vigente; las
demás aperturas esperan al candado, ven la credencial nueva y reconectan sin volver a leer.

``LazyDatabase`` difiere todo esto hasta la primera operación de datos: ``vigia-admin`` la usa
para que una orden sin base (``restore-audit-partition``) no lea el secreto ni intente conectar
(runbook 6.5, comentario de VIG-98 del 2026-10-03).

Ningún mensaje, registro ni ``repr`` contiene la contraseña ni el contenido del secreto.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]
from sqlalchemy import URL
from sqlalchemy.engine import Row
from sqlalchemy.sql import Executable

from vigia_platform.shared.context import ContextAbsent, ScopeContext, repository
from vigia_platform.shared.db import Database, DatabaseHealth, ProcessKind, Transaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.secrets import (
    AwsSettings,
    Dependency,
    SecretNotFound,
    SecretsUnavailable,
)

__all__ = [
    "AwsSecretStringReader",
    "DatabaseCredentials",
    "DatabaseSecret",
    "DatabaseSecretInvalid",
    "LazyDatabase",
    "SecretStringReader",
    "database_url",
    "parse_database_secret",
]

_log = get_logger("shared.runtime.db_credentials")

_DRIVER: Final = "postgresql+asyncpg"
_MAX_SECRET_CHARACTERS: Final = 65_536
_FIELDS: Final = ("host", "port", "dbname", "username", "password")


class DatabaseSecretInvalid(ValueError):
    """El secreto no tiene la forma de RDS; el mensaje nombra el campo, nunca un valor."""


@dataclass(frozen=True, slots=True)
class DatabaseSecret:
    """Destino y credencial de ``vigia_app``; la contraseña nunca sale en un ``repr``."""

    host: str
    port: int
    dbname: str
    username: str
    password: str = field(repr=False)

    @property
    def credential(self) -> tuple[str, str]:
        return self.username, self.password


def parse_database_secret(text: object) -> DatabaseSecret:
    """El ``SecretString`` de RDS como ``DatabaseSecret``; ``DatabaseSecretInvalid`` si no vale."""
    if not isinstance(text, str) or not 0 < len(text) <= _MAX_SECRET_CHARACTERS:
        raise DatabaseSecretInvalid("el secreto de la base no es texto")
    try:
        document = json.loads(text)
    except ValueError:
        raise DatabaseSecretInvalid("el secreto de la base no es un objeto JSON") from None
    if not isinstance(document, dict):
        raise DatabaseSecretInvalid("el secreto de la base no es un objeto JSON")
    for name in _FIELDS:
        value = document.get(name)
        if name == "port":
            valid = (isinstance(value, int) and not isinstance(value, bool)) or (
                isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 5
            )
        else:
            valid = isinstance(value, str) and 0 < len(value) <= 1024 and value.isprintable()
        if not valid:
            raise DatabaseSecretInvalid(f"al secreto de la base le falta el campo {name!r}")
    port = int(str(document["port"]))
    if not 0 < port < 65536:
        raise DatabaseSecretInvalid("al secreto de la base le falta el campo 'port'")
    return DatabaseSecret(
        host=document["host"],
        port=port,
        dbname=document["dbname"],
        username=document["username"],
        password=document["password"],
    )


def database_url(secret: DatabaseSecret) -> str:
    """``postgresql+asyncpg://host:puerto/base`` **sin** usuario ni contraseña: la credencial
    la pone el pool en cada conexión (``CredentialSource``)."""
    url = URL.create(_DRIVER, host=secret.host, port=secret.port, database=secret.dbname)
    return url.render_as_string(hide_password=False)


# --- Lectura del secreto ------------------------------------------------------------------------


class SecretStringReader(Protocol):
    """El ``SecretString`` de un secreto por nombre o ARN."""

    async def read(self, secret_id: str) -> str: ...


class AwsSecretStringReader:
    """``SecretStringReader`` sobre boto3 con el tope de ``AwsSettings`` (PAT-NUC-RES-03).

    Un secreto inexistente da ``SecretNotFound``; cualquier otro fallo de AWS (red, tiempo de
    espera, acceso negado), ``SecretsUnavailable``. Sin caché: cada lectura va al servicio, y solo
    ocurre al arrancar y tras una rotación.
    """

    def __init__(
        self,
        settings: AwsSettings,
        *,
        client: Any | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._settings = settings
        self._client = client if client is not None else settings.make_client("secretsmanager")
        self._executor = executor

    def __repr__(self) -> str:
        return "AwsSecretStringReader()"

    async def read(self, secret_id: str) -> str:
        def fetch() -> str:
            try:
                response = self._client.get_secret_value(SecretId=secret_id)
            except botocore_exceptions.ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code == "ResourceNotFoundException":
                    raise SecretNotFound from None
                raise
            value = response.get("SecretString")
            if not isinstance(value, str):
                raise DatabaseSecretInvalid("el secreto de la base no es texto")
            return value

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, fetch)
        try:
            async with asyncio.timeout(self._settings.call_timeout_seconds):
                return await future
        except (SecretNotFound, DatabaseSecretInvalid):
            raise
        except (TimeoutError, botocore_exceptions.BotoCoreError, botocore_exceptions.ClientError):
            raise SecretsUnavailable(Dependency.SECRETS_MANAGER, "get_secret") from None


# --- Credencial con relectura -------------------------------------------------------------------


class DatabaseCredentials:
    """``CredentialSource`` de ``shared.db`` sobre el secreto ``db/app``."""

    def __init__(self, reader: SecretStringReader, secret_id: str, secret: DatabaseSecret) -> None:
        self._reader = reader
        self._secret_id = secret_id
        self._secret = secret
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "DatabaseCredentials()"

    @classmethod
    async def load(cls, reader: SecretStringReader, secret_id: str) -> DatabaseCredentials:
        """La primera lectura, al construir el proceso."""
        return cls(reader, secret_id, parse_database_secret(await reader.read(secret_id)))

    @property
    def secret(self) -> DatabaseSecret:
        return self._secret

    def current(self) -> tuple[str, str]:
        return self._secret.credential

    async def renew(self, rejected: tuple[str, str]) -> None:
        """Relee el secreto si ``rejected`` sigue siendo la credencial vigente.

        La exclusión hace que una ráfaga de rechazos simultáneos lea el secreto una sola vez: la
        primera apertura relee; las demás, al obtener el candado, ya ven otra credencial.
        """
        async with self._lock:
            if self._secret.credential != rejected:
                return
            fresh = parse_database_secret(await self._reader.read(self._secret_id))
            if fresh.credential == rejected:
                # El secreto aún no cambió (rotación a medias): la apertura fallará y el
                # llamador recibirá el error; la próxima volverá a intentar la relectura.
                _log.warning("la credencial releída es la misma que la base rechazó")
                return
            self._secret = fresh
            _log.info("credencial de la base releída tras una rotación")


# --- Base perezosa -------------------------------------------------------------------------------


@repository
class LazyDatabase:
    """``shared.db.Database`` que se construye en la primera operación de datos.

    Hasta entonces no lee el secreto ni abre conexiones: una orden de ``vigia-admin`` que no usa
    la base termina sin tocarla. La guarda de contexto corre antes que la construcción.
    """

    def __init__(self, open_database: Callable[[], Awaitable[Database]]) -> None:
        self._open = open_database
        self._database: Database | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"LazyDatabase(opened={self._database is not None})"

    @property
    def opened(self) -> bool:
        """``True`` si alguna operación construyó ya la base."""
        return self._database is not None

    @property
    def process(self) -> ProcessKind:
        return ProcessKind.WORKER

    async def _get(self) -> Database:
        async with self._lock:
            if self._database is None:
                self._database = await self._open()
            return self._database

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]:
        if not isinstance(context, ScopeContext):
            raise ContextAbsent()
        return self._transaction(context)

    @contextlib.asynccontextmanager
    async def _transaction(self, context: ScopeContext) -> AsyncIterator[Transaction]:
        database = await self._get()
        async with database.transaction(context) as transaction:
            yield transaction

    async def read(
        self,
        context: ScopeContext,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
    ) -> Sequence[Row[Any]]:
        if not isinstance(context, ScopeContext):
            raise ContextAbsent()
        return await (await self._get()).read(context, statement, parameters)

    async def health(self, *, timeout_seconds: float) -> DatabaseHealth:
        return await (await self._get()).health(timeout_seconds=timeout_seconds)

    async def dispose(self) -> None:
        if self._database is not None:
            await self._database.dispose()
