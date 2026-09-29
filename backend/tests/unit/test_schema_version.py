"""Versión mínima del esquema al arrancar (TASK-106, PAT-NUC-MAN-09, NFR-NUC-14).

Con un doble de conexión: la comparación con la versión mínima, los bordes (igual, uno menos,
nula, función inexistente) y que la imagen nunca exige una versión que la cadena no tiene. Con
PostgreSQL real, en ``tests/integration/test_roles.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection

from tools.lint_migrations import DEFAULT_VERSIONS, REVISION_PATTERN, check_directory, load_registry
from vigia_platform.shared.schema_version import (
    MINIMUM_SCHEMA_VERSION,
    SchemaTooOld,
    ensure_minimum_schema_version,
    read_schema_version,
)


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value


class _Connection:
    def __init__(self, value: object = None, error: BaseException | None = None) -> None:
        self.value = value
        self.error = error
        self.statements: list[str] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        if self.error is not None:
            raise self.error
        return _Result(self.value)


def _ensure(connection: _Connection, minimum: int = MINIMUM_SCHEMA_VERSION) -> int:
    return asyncio.run(ensure_minimum_schema_version(cast(AsyncConnection, connection), minimum))


def test_head_satisfies_the_image_minimum() -> None:
    """La imagen nunca exige un esquema que la cadena no produce."""
    assert check_directory(DEFAULT_VERSIONS, load_registry()) == []
    numbers = [
        int(match["number"])
        for path in DEFAULT_VERSIONS.glob("*.py")
        if (match := REVISION_PATTERN.fullmatch("_".join(path.stem.split("_")[:2])))
    ]
    assert 1 <= MINIMUM_SCHEMA_VERSION <= max(numbers)


@pytest.mark.parametrize("version", [MINIMUM_SCHEMA_VERSION, MINIMUM_SCHEMA_VERSION + 5])
def test_version_at_or_above_the_minimum_passes(version: int) -> None:
    connection = _Connection(version)
    assert _ensure(connection) == version
    assert connection.statements == ["SELECT shared.vigia_schema_version()"]


@pytest.mark.parametrize(("version", "minimum"), [(0, 1), (1, 2), (41, 42), (None, 1)])
def test_older_or_unknown_schema_refuses_to_start(version: int | None, minimum: int) -> None:
    with pytest.raises(SchemaTooOld) as caught:
        _ensure(_Connection(version), minimum)
    assert (caught.value.found, caught.value.minimum) == (version, minimum)
    assert "alembic upgrade head" in str(caught.value)


@pytest.mark.parametrize("sqlstate", ["42883", "3F000", "42P01"])
def test_unmigrated_database_refuses_to_start(sqlstate: str) -> None:
    error = sa_exc.ProgrammingError("SELECT", {}, _Orig(sqlstate))
    with pytest.raises(SchemaTooOld) as caught:
        _ensure(_Connection(error=error))
    assert caught.value.found is None
    assert "sin migrar" in str(caught.value)


def test_other_database_errors_are_not_masked() -> None:
    error = sa_exc.OperationalError("SELECT", {}, _Orig("08006"))
    with pytest.raises(sa_exc.OperationalError):
        _ensure(_Connection(error=error))


@pytest.mark.parametrize("value", ["1", 1.0, True])
def test_non_integer_version_is_a_type_error(value: object) -> None:
    with pytest.raises(TypeError):
        asyncio.run(read_schema_version(cast(AsyncConnection, _Connection(value))))
