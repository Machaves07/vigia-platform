"""Foto del hallazgo en ``fleet.episode_photo``: migración y reglas de la base (TASK-588; A-128).

Contra PostgreSQL 16, con bases propias migradas por ``alembic`` como proceso aparte:

- **Migración** ``gob_0045`` sobre una base en ``laz_0044`` (la cabeza anterior, con fotos de la
  sombra de ``v1.5.0``): ``alembic upgrade head`` deja cada fila como
  ``commissioning``/``0``/``peak`` con ``record_id`` nulo y **ningún otro valor cambia**
  (comparación fila a fila). La imagen anterior sigue arrancando: su versión mínima (43) no supera
  la del esquema y su ``INSERT`` (sin las columnas nuevas) deja la fila en ``commissioning``.
- **CHECK por modo**: la base rechaza ``productive`` sin ``record_id``, ``commissioning`` con
  ``record_id``, ``photo_index = 21``, ``0``/``burst`` y ``1``/``peak``, ``commissioning`` sin
  ``expires_at`` o a más de 90 días y ``productive`` a más de 1 825 días; admite los bordes
  (``m_sin_check_de_modo``).
- **Índice único** de ``productive`` por ``(zona, episodio, tramo, índice)``: la segunda falla; dos
  ``commissioning`` iguales siguen permitidas.

Solo datos generados: identificadores aleatorios, sin imagen (``image_absent_reason``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import pytest

from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_roles import HEAD_SCHEMA_VERSION, run_alembic
from vigia_platform.shared.migration_credentials import (
    APP_PASSWORD_VARIABLE,
    MIGRATE_PASSWORD_VARIABLE,
)
from vigia_platform.shared.schema_version import MINIMUM_SCHEMA_VERSION

pytestmark = pytest.mark.integration

PREVIOUS_HEAD: Final = "laz_0044"
"""La cabeza de la cadena antes de ``gob_0045``."""
PREVIOUS_MINIMUM: Final = 43
"""``MINIMUM_SCHEMA_VERSION`` de la imagen anterior (``gob_0043``)."""
NOW: Final = dt.datetime(2026, 11, 23, 9, 30, tzinfo=dt.UTC)
MS: Final = dt.timedelta(milliseconds=1)
DAY: Final = dt.timedelta(days=1)
CHECK_VIOLATION: Final = "23514"
UNIQUE_VIOLATION: Final = "23505"

_V150_INSERT: Final = (
    "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id, node_id,"
    " episode_id, segment_index, classified_as, family, standard_id, standard_version,"
    " max_confidence, started_at, ended_at, received_at, expires_at, image_absent_reason,"
    " canonical_sha256, platform_record_id) VALUES ($1, $2, $3, $4, $5, $6, $7, 'finding',"
    " 'coexistence', $8, 1, 0.9, $9, $9, $9, $10, 'frame_unavailable', $11, $12)"
)
"""El ``INSERT`` de la imagen anterior (``gob_0042``): sin las columnas de ``gob_0045``."""

_MODE_INSERT: Final = (
    "INSERT INTO fleet.episode_photo (organization_id, photo_id, plant_id, zone_id, node_id,"
    " episode_id, segment_index, classified_as, family, standard_id, standard_version,"
    " max_confidence, started_at, ended_at, received_at, expires_at, image_absent_reason,"
    " canonical_sha256, platform_record_id, zone_mode, record_id, photo_index, photo_role)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, 'finding', 'coexistence', $8, 1, 0.9, $9, $9, $9, $10,"
    " 'frame_unavailable', $11, $12, $13, $14, $15, $16)"
)


@dataclass(frozen=True)
class Slot:
    """Zona, episodio y tramo de una foto (lo que comparte el índice único)."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    episode_id: uuid.UUID
    segment_index: int = 0

    @classmethod
    def new(cls) -> Slot:
        return cls(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


def _base(slot: Slot, photo_id: uuid.UUID, expires: dt.datetime | None) -> list[Any]:
    return [
        slot.organization_id,
        photo_id,
        slot.plant_id,
        slot.zone_id,
        uuid.uuid4(),
        slot.episode_id,
        slot.segment_index,
        uuid.uuid4(),
        NOW,
        expires,
        photo_id.hex * 2,
        uuid.uuid4(),
    ]


def _mode_row(
    slot: Slot,
    *,
    mode: str,
    record: bool | None = None,
    index: int = 0,
    role: str = "peak",
    expires: dt.datetime | None,
) -> list[Any]:
    with_record = mode == "productive" if record is None else record
    return [
        *_base(slot, uuid.uuid4(), expires),
        mode,
        uuid.uuid4() if with_record else None,
        index,
        role,
    ]


async def _connect(migrated: MigratedDatabase) -> Any:
    return await migrated.connect()


async def _sqlstate(migrated: MigratedDatabase, *rows: list[Any]) -> tuple[str, str | None]:
    """``ok`` o el SQLSTATE y la restricción del primer ``INSERT`` que falla (todo se revierte)."""
    connection = await _connect(migrated)
    try:
        transaction = connection.transaction()
        await transaction.start()
        try:
            for row in rows:
                await connection.execute(_MODE_INSERT, *row)
        except asyncpg.PostgresError as error:
            return str(error.sqlstate), getattr(error, "constraint_name", None)
        finally:
            await transaction.rollback()
    finally:
        await connection.close()
    return "ok", None


def _alembic(migrated: MigratedDatabase, revision: str) -> None:
    result = run_alembic(
        migrated.endpoint,
        migrated.database,
        {
            APP_PASSWORD_VARIABLE: migrated.app_password,
            MIGRATE_PASSWORD_VARIABLE: migrated.migrate_password,
        },
        "upgrade",
        revision,
    )
    assert result.returncode == 0, result.stderr


async def _admin(endpoint: PostgresEndpoint, statement: str) -> None:
    connection = await asyncpg.connect(
        host=endpoint.host,
        port=endpoint.port,
        user=endpoint.user,
        password=endpoint.password,
        database=endpoint.database,
    )
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


async def _rows(migrated: MigratedDatabase) -> dict[uuid.UUID, dict[str, Any]]:
    connection = await _connect(migrated)
    try:
        found = await connection.fetch("SELECT * FROM fleet.episode_photo")
    finally:
        await connection.close()
    return {uuid.UUID(str(row["photo_id"])): dict(row) for row in found}


async def _execute(migrated: MigratedDatabase, sql: str, *args: Any) -> Any:
    connection = await _connect(migrated)
    try:
        return await connection.fetchval(sql, *args)
    finally:
        await connection.close()


# --- Migración ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Upgraded:
    migrated: MigratedDatabase
    before: dict[uuid.UUID, dict[str, Any]]
    after: dict[uuid.UUID, dict[str, Any]]


@pytest.fixture(scope="module")
def upgraded(postgres_endpoint: PostgresEndpoint) -> Iterator[Upgraded]:
    name = f"vigia_gob_0045_{secrets.token_hex(4)}"
    asyncio.run(_admin(postgres_endpoint, f"CREATE DATABASE {name}"))
    migrated = MigratedDatabase(
        postgres_endpoint, name, secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    )
    try:
        _alembic(migrated, PREVIOUS_HEAD)

        async def seed() -> None:
            connection = await _connect(migrated)
            try:
                for index in range(3):
                    slot = dataclasses.replace(Slot.new(), segment_index=index)
                    expires = NOW + (index + 1) * 10 * DAY
                    await connection.execute(_V150_INSERT, *_base(slot, uuid.uuid4(), expires))
            finally:
                await connection.close()

        asyncio.run(seed())
        before = asyncio.run(_rows(migrated))
        _alembic(migrated, "head")
        after = asyncio.run(_rows(migrated))
        yield Upgraded(migrated, before, after)
    finally:
        asyncio.run(_admin(postgres_endpoint, f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


NEW_COLUMNS: Final = {
    "zone_mode": "commissioning",
    "record_id": None,
    "photo_index": 0,
    "photo_role": "peak",
}


def test_migration_gob_0045_leaves_every_v150_row_commissioning_and_unchanged(
    upgraded: Upgraded,
) -> None:
    assert len(upgraded.before) == 3
    assert set(upgraded.after) == set(upgraded.before)
    for photo_id, old in upgraded.before.items():
        new = upgraded.after[photo_id]
        assert {key: new[key] for key in NEW_COLUMNS} == NEW_COLUMNS
        assert {key: value for key, value in new.items() if key not in NEW_COLUMNS} == old


def test_migration_gob_0045_keeps_the_previous_image_working(upgraded: Upgraded) -> None:
    """La imagen anterior arranca (su mínimo no supera la versión del esquema) y su ``INSERT`` sin
    las columnas nuevas deja una foto de ``commissioning``; esta imagen exige ``gob_0045``."""
    version = asyncio.run(_execute(upgraded.migrated, "SELECT shared.vigia_schema_version()"))
    assert version == HEAD_SCHEMA_VERSION
    assert PREVIOUS_MINIMUM <= MINIMUM_SCHEMA_VERSION <= version
    photo_id = uuid.uuid4()

    async def old_insert() -> dict[str, Any]:
        connection = await _connect(upgraded.migrated)
        try:
            await connection.execute(_V150_INSERT, *_base(Slot.new(), photo_id, NOW + 30 * DAY))
            row = await connection.fetchrow(
                "SELECT zone_mode, record_id, photo_index, photo_role FROM fleet.episode_photo"
                " WHERE photo_id = $1",
                photo_id,
            )
        finally:
            await connection.close()
        return dict(row)

    assert asyncio.run(old_insert()) == NEW_COLUMNS


# --- CHECK por modo e índice único ----------------------------------------------------------------


@pytest.fixture(scope="module")
def database(postgres_endpoint: PostgresEndpoint) -> Iterator[MigratedDatabase]:
    with migrated_database(postgres_endpoint, "vigia_finding_photo_rules") as migrated:
        yield migrated


REJECTED: Final = {
    "productive-sin-record_id": (
        {"mode": "productive", "record": False, "expires": None},
        "episode_photo_record_by_mode",
    ),
    "commissioning-con-record_id": (
        {"mode": "commissioning", "record": True, "expires": NOW + DAY},
        "episode_photo_record_by_mode",
    ),
    "indice-21": (
        {"mode": "productive", "index": 21, "role": "burst", "expires": None},
        "episode_photo_photo_index_range",
    ),
    "indice-0-burst": (
        {"mode": "productive", "index": 0, "role": "burst", "expires": None},
        "episode_photo_role_of_index",
    ),
    "indice-1-peak": (
        {"mode": "productive", "index": 1, "role": "peak", "expires": None},
        "episode_photo_role_of_index",
    ),
    "commissioning-sin-vencimiento": (
        {"mode": "commissioning", "expires": None},
        "episode_photo_expiry_by_mode",
    ),
    "commissioning-90-dias-y-1-ms": (
        {"mode": "commissioning", "expires": NOW + 90 * DAY + MS},
        "episode_photo_expiry_by_mode",
    ),
    "productive-1825-dias-y-1-ms": (
        {"mode": "productive", "expires": NOW + 1825 * DAY + MS},
        "episode_photo_expiry_by_mode",
    ),
    "modo-desconocido": (
        {"mode": "no_capture", "record": False, "expires": NOW + DAY},
        "episode_photo_zone_mode_values",
    ),
}


@pytest.mark.parametrize("case", list(REJECTED), ids=list(REJECTED))
def test_the_database_rejects_each_row_outside_its_mode(
    database: MigratedDatabase, case: str
) -> None:
    options, constraint = REJECTED[case]
    row = _mode_row(Slot.new(), **options)
    assert asyncio.run(_sqlstate(database, row)) == (CHECK_VIOLATION, constraint)


ACCEPTED: Final = {
    "productive-sin-vencimiento": {"mode": "productive", "expires": None},
    "productive-1825-dias": {"mode": "productive", "expires": NOW + 1825 * DAY},
    "productive-rafaga-20": {"mode": "productive", "index": 20, "role": "burst", "expires": None},
    "commissioning-90-dias": {"mode": "commissioning", "expires": NOW + 90 * DAY},
}


@pytest.mark.parametrize("case", list(ACCEPTED), ids=list(ACCEPTED))
def test_the_database_accepts_the_edges_of_each_mode(database: MigratedDatabase, case: str) -> None:
    row = _mode_row(Slot.new(), **ACCEPTED[case])
    assert asyncio.run(_sqlstate(database, row)) == ("ok", None)


def test_one_productive_photo_per_slot_and_index_while_commissioning_repeats(
    database: MigratedDatabase,
) -> None:
    slot = Slot.new()
    first = _mode_row(slot, mode="productive", expires=None)
    second = _mode_row(slot, mode="productive", expires=None)
    assert asyncio.run(_sqlstate(database, first, second)) == (
        UNIQUE_VIOLATION,
        "episode_photo_productive_slot",
    )
    # Otro índice del mismo tramo sí entra (la ráfaga de la fase 2).
    burst = _mode_row(slot, mode="productive", index=1, role="burst", expires=None)
    assert asyncio.run(_sqlstate(database, first, burst)) == ("ok", None)
    # Dos de puesta en marcha del mismo tramo siguen permitidas, como hasta ahora.
    shadow = _mode_row(slot, mode="commissioning", expires=NOW + DAY)
    again = _mode_row(slot, mode="commissioning", expires=NOW + DAY)
    assert asyncio.run(_sqlstate(database, shadow, again, first)) == ("ok", None)
