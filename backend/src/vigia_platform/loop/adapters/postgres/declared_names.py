"""Vocabulario declarado de una organización sobre PostgreSQL (LC-LAZ-16; TASK-340 y TASK-350).

``PostgresDeclaredNamesStore`` es el ``DeclaredNamesStore`` de ``cache.DeclaredNamesCache``: los
nombres que eximen al patrón de nombre propio de ``person_attribution`` («Prensa Norte», «Línea
Krones»):

- los de las plantas y zonas de la organización (la jerarquía de U-02 que el catálogo de zonas de
  U-03 usa);
- los que la organización declara en su lista de vocabulario (``declared_names`` de
  ``loop.blocked_vocabulary_list``, TASK-350): equipos, máquinas, normas y alias.

No abre conexiones: lee con el lector de filas de la escritura (``FreeTextPreparation.rows``), con
su contexto, así que la seguridad a nivel de fila de ``identity.plant``, ``identity.zone`` y
``loop.blocked_vocabulary_list`` (solo organización, A-69) decide lo que ve; lo que no ve no
exime, y el texto se valida más estricto, nunca más laxo. Toda lectura nombra la organización
(defensa en profundidad sobre la RLS).
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
    " UNION SELECT declared.item ->> 'value' FROM loop.blocked_vocabulary_list AS list"
    " CROSS JOIN LATERAL jsonb_array_elements(list.declared_names) AS declared(item)"
    " WHERE list.organization_id = :organization_id"
)


class PostgresDeclaredNamesStore:
    """``DeclaredNamesStore`` de PostgreSQL."""

    async def names(self, organization_id: uuid.UUID, rows: RowReader) -> tuple[str, ...]:
        found = await rows(_NAMES, {"organization_id": organization_id})
        return tuple(str(row.name) for row in found if row.name is not None)
