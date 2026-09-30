"""Versión mínima del esquema que exige esta imagen (PAT-NUC-MAN-09, NFR-NUC-14, RESILIENCY-04).

La tarea ``vigia-migrate`` migra la base antes de arrancar la imagen nueva; la reversión es
redesplegar la imagen anterior, que arranca sobre el esquema ya migrado. Por eso cada imagen
declara la versión mínima que necesita y **no arranca** sobre un esquema más viejo:

- ``shared.vigia_schema_version()`` devuelve el número del último eslabón aplicado de la cadena
  (``nuc_0001`` → 1; lo lee de ``alembic_version``) o ``NULL`` si la tabla no tiene exactamente
  una fila con ese formato;
- ``ensure_minimum_schema_version(connection)`` lo compara con ``MINIMUM_SCHEMA_VERSION`` y lanza
  ``SchemaTooOld`` si es menor, nulo o si la función no existe (base sin migrar). Lo llaman las
  comprobaciones de arranque de ``vigia-api`` y ``vigia-worker`` (TASK-133, TASK-130) y
  ``/health/ready``.

Sube ``MINIMUM_SCHEMA_VERSION`` la migración cuyo esquema empiece a usar el código de la imagen,
en el mismo cambio; ``tests/unit/test_schema_version.py`` exige que nunca supere la cabeza de la
cadena. Es un dato del esquema, no de clientes: la consulta no lleva ``ScopeContext``.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "MINIMUM_SCHEMA_VERSION",
    "SchemaTooOld",
    "ensure_minimum_schema_version",
    "read_schema_version",
]

MINIMUM_SCHEMA_VERSION: Final = 6
"""Versión mínima del esquema que exige el código de esta imagen: ``nuc_0006`` (las columnas de
la verificación diferida de la marca en ``ledger.evidence`` y ``ledger.evidence_sample_run``)."""

_READ_VERSION: Final = text("SELECT shared.vigia_schema_version()")
_MISSING_SQLSTATES: Final = frozenset({"3F000", "42883", "42P01"})
"""``invalid_schema_name``, ``undefined_function``, ``undefined_table``: base sin migrar."""


class SchemaTooOld(RuntimeError):
    """El esquema es anterior al que exige la imagen: el proceso no debe arrancar."""

    def __init__(self, *, found: int | None, minimum: int) -> None:
        shown = "sin migrar" if found is None else str(found)
        super().__init__(
            f"el esquema de la base ({shown}) es anterior al mínimo que exige esta imagen "
            f"({minimum}): ejecuta las migraciones (alembic upgrade head) antes de arrancar"
        )
        self.found = found
        self.minimum = minimum


def _sqlstate(error: BaseException) -> str | None:
    for candidate in (error, getattr(error, "orig", None), error.__cause__):
        value = getattr(candidate, "sqlstate", None)
        if isinstance(value, str):
            return value
    return None


async def read_schema_version(connection: AsyncConnection) -> int | None:
    """Versión aplicada del esquema, o ``None`` si la base no está migrada.

    Si la función no existe, la transacción de ``connection`` queda abortada: úsese una conexión
    dedicada a esta comprobación.
    """
    try:
        result = await connection.execute(_READ_VERSION)
    except sa_exc.DBAPIError as error:
        if _sqlstate(error) in _MISSING_SQLSTATES:
            return None
        raise
    version = result.scalar_one()
    if version is None:
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        raise TypeError("shared.vigia_schema_version() debe devolver un entero")
    return version


async def ensure_minimum_schema_version(
    connection: AsyncConnection, minimum: int = MINIMUM_SCHEMA_VERSION
) -> int:
    """Devuelve la versión aplicada; lanza ``SchemaTooOld`` si es menor que ``minimum``."""
    version = await read_schema_version(connection)
    if version is None or version < minimum:
        raise SchemaTooOld(found=version, minimum=minimum)
    return version
