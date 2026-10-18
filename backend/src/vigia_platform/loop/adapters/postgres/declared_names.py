"""Vocabulario declarado de una organización sobre PostgreSQL (LC-LAZ-16; TASK-340).

``PostgresDeclaredNamesStore`` es el ``DeclaredNamesStore`` de ``cache.DeclaredNamesCache``: los
nombres de las plantas y zonas de la organización, que eximen al patrón de nombre propio de
``person_attribution`` («Prensa Norte», «Línea Krones»). No abre conexiones: lee con el lector de
filas de la escritura (``FreeTextPreparation.rows``), con su contexto, así que la seguridad a nivel
de fila de ``identity.plant`` e ``identity.zone`` decide lo que ve; lo que no ve no exime, y el
texto se valida más estricto, nunca más laxo.

Mientras no exista la lista que la organización declara (TASK-350, equipos y máquinas), el
vocabulario es el de la jerarquía de U-02 que el catálogo de zonas de U-03 usa.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import text

from vigia_platform.ledger.free_text import RowReader

__all__ = ["PostgresDeclaredNamesStore"]

_NAMES: Final = text(
    "SELECT name FROM identity.plant WHERE organization_id = :organization_id"
    " UNION SELECT name FROM identity.zone WHERE organization_id = :organization_id"
)


class PostgresDeclaredNamesStore:
    """``DeclaredNamesStore`` de PostgreSQL."""

    async def names(self, organization_id: uuid.UUID, rows: RowReader) -> tuple[str, ...]:
        found = await rows(_NAMES, {"organization_id": organization_id})
        return tuple(str(row.name) for row in found)
