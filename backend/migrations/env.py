"""Entorno de Alembic de vigia-platform (TASK-106, LC-NUC-12, PAT-NUC-MAN-09).

Ejecuta la cadena única de migraciones con el controlador asíncrono ``asyncpg`` en **una sola
transacción**: si un eslabón falla, no queda nada a medias (PostgreSQL revierte también el DDL).

Conexión:

- ``config.attributes["connection_url"]``, si quien invoca a Alembic la pasa (pruebas);
- si no, las variables estándar de libpq (``PGHOST``, ``PGPORT``, ``PGUSER``, ``PGPASSWORD``,
  ``PGDATABASE``, ``PGSSLMODE``, ``PGSSLROOTCERT``), que asyncpg lee por sí mismo. Así lo
  invocan ``make migrate`` y la tarea ``vigia-migrate`` (con el rol ``vigia_migrate``; la
  primera vez, con el usuario maestro: ver ``nuc_0001``).

La tabla ``alembic_version`` vive en ``public`` con nombre calificado, sin depender de
``search_path``. El modo sin conexión (``--sql``) está deshabilitado: ``nuc_0001`` recibe
contraseñas y un volcado de SQL las dejaría escritas.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import URL, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

VERSION_TABLE_SCHEMA = "public"

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    # Registro de alembic.ini: progreso de Alembic; nunca el de sqlalchemy.engine en INFO, que
    # escribiría los parámetros de las sentencias (entre ellos, los verificadores de nuc_0001).
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _connection_url() -> str | URL:
    url: str | URL | None = config.attributes.get("connection_url")
    if url is not None:
        return url
    # Sin host, usuario ni base: asyncpg los toma de las variables PG* del entorno.
    return URL.create("postgresql+asyncpg")


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table_schema=VERSION_TABLE_SCHEMA,
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_connection_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit(
        "Modo sin conexión (--sql) deshabilitado: nuc_0001 recibe contraseñas de roles y el "
        "volcado de SQL las dejaría escritas. Ejecuta las migraciones contra la base."
    )
asyncio.run(_run_online())
