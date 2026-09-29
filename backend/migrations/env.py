"""Entorno de Alembic de vigia-platform (TASK-106, LC-NUC-12, PAT-NUC-MAN-09).

Ejecuta la cadena única de migraciones con el controlador asíncrono ``asyncpg`` en **una sola
transacción**: si un eslabón falla, no queda nada a medias (PostgreSQL revierte también el DDL).

La conexión y las contraseñas de los roles que crea ``nuc_0001`` salen de
``vigia_platform.shared.migration_credentials``: en AWS, de los secretos cuyos nombres recibe la
tarea ``vigia-migrate``; en local, de las variables de libpq (``PG*``) y de
``VIGIA_DB_APP_PASSWORD`` y ``VIGIA_DB_MIGRATE_PASSWORD`` (``make migrate``). Las contraseñas
llegan a las migraciones por ``config.attributes["role_passwords"]``, nunca por el registro.

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

from vigia_platform.shared.migration_credentials import resolve_migration_credentials

VERSION_TABLE_SCHEMA = "public"

config = context.config
if config.config_file_name is not None:
    # Registro de alembic.ini: progreso de Alembic; nunca el de sqlalchemy.engine en INFO, que
    # escribiría los parámetros de las sentencias (entre ellos, los verificadores de nuc_0001).
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table_schema=VERSION_TABLE_SCHEMA,
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online(url: URL) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
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
credentials = resolve_migration_credentials()
config.attributes["role_passwords"] = credentials.role_passwords
asyncio.run(_run_online(credentials.url))
