"""Seguridad a nivel de fila y categoría de retención de las tablas de U-04 (TASK-301).

Ayudante compartido de las migraciones ``laz_``: ``laz_0029`` lo usa para ``loop`` y las de
``accreditation`` (TASK-338) y ``notifications`` (TASK-339) lo reutilizan sin reescribirlo. Solo
devuelve texto SQL; no ejecuta nada ni importa nada de la aplicación.

**Políticas** (PAT-LAZ-SEG-03, adenda A-46 y A-69). Toda tabla de U-04 lleva ``organization_id``:

- ``organization_isolation`` (PERMISSIVE, forzada con ``FORCE ROW LEVEL SECURITY``): solo las
  filas de ``shared.vigia_current_organization()``, la organización del ``SET LOCAL`` de U-02;
- ``provider_concession_scope`` (RESTRICTIVE) en toda tabla con planta o zona:
  ``identity.rls_provider_scope_allows(organization_id, plant_id)`` de ``nuc_0009``, que exige la
  concesión de ``vigia.concession_id`` **vigente** (activa, sin revocar, ya concedida, sin vencer)
  y su planta;
- ``provider_context_denied`` (RESTRICTIVE, A-69) en las tablas de solo organización: con un
  contexto de proveedor (``identity.rls_provider_context()``: actor del proveedor o
  ``vigia.concession_id`` presente, fallo cerrado), ninguna fila visible ni escribible. Una tabla
  de solo organización que además lleva planta (``assistant_request``,
  ``loop_projection_checkpoint``) tiene las dos RESTRICTIVE: la de A-69 ya no deja nada.

**Retención** (NFR-LAZ-08 y 34, PAT-LAZ-ESC-04): cada tabla declara **exactamente una**
categoría en su ``COMMENT ON TABLE`` con la marca ``retention_category=<categoría>``.
``retention_violations`` es la metapropiedad que la comprueba sobre los comentarios que lee
``tests/unit/test_laz_retention_categories.py`` de la base.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

RETENTION_CATEGORIES: Final = (
    "ledger_projection",
    "operational_state",
    "operational_state_with_retention",
)
"""Las tres categorías: proyección reconstruible del expediente (sin caducidad), estado operativo
sin retención y estado operativo con retención (la única que una tarea de purga podrá tocar)."""

U04_SCHEMAS: Final = ("loop", "accreditation", "notifications")
"""Los esquemas de U-04 (nota U04-H-17 de ``domain-entities.md`` §2.14)."""

_RETENTION_TAG: Final = re.compile(r"retention_category=([a-z_]+)")


def retention_tag(category: str) -> str:
    """La marca de categoría que va en el comentario de la tabla."""
    if category not in RETENTION_CATEGORIES:
        raise ValueError(f"categoría de retención desconocida: {category}")
    return f"retention_category={category}"


def retention_violations(comments: Mapping[str, str | None]) -> list[str]:
    """Tablas sin categoría, con más de una o con una desconocida (``tabla`` → comentario)."""
    violations: list[str] = []
    for table, comment in sorted(comments.items()):
        found = _RETENTION_TAG.findall(comment or "")
        if not found:
            violations.append(f"{table}: sin categoría de retención")
        elif len(found) > 1:
            violations.append(f"{table}: {len(found)} categorías de retención ({', '.join(found)})")
        elif found[0] not in RETENTION_CATEGORIES:
            violations.append(f"{table}: categoría de retención desconocida ({found[0]})")
    return violations


def row_security(
    schema: str, table: str, *, plant_column: str | None, organization_only: bool
) -> list[str]:
    """RLS forzada y las políticas de la tabla.

    ``plant_column``: la columna de planta que acota la concesión (``None`` si la tabla no tiene
    planta). ``organization_only``: la tabla es de solo organización según A-69 y no deja nada a un
    contexto de proveedor. Una tabla sin planta tiene que ser de solo organización: si no, un
    proveedor la vería entera.
    """
    if plant_column is None and not organization_only:
        raise ValueError(f"{schema}.{table}: sin planta, la tabla es de solo organización (A-69)")
    name = f"{schema}.{table}"
    statements = [
        f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY organization_isolation ON {name} AS PERMISSIVE FOR ALL TO PUBLIC"
        " USING (organization_id = shared.vigia_current_organization())"
        " WITH CHECK (organization_id = shared.vigia_current_organization())",
    ]
    if plant_column is not None:
        statements.append(
            f"CREATE POLICY provider_concession_scope ON {name} AS RESTRICTIVE FOR ALL TO PUBLIC"
            f" USING (identity.rls_provider_scope_allows(organization_id, {plant_column}))"
            f" WITH CHECK (identity.rls_provider_scope_allows(organization_id, {plant_column}))"
        )
    if organization_only:
        statements.append(
            f"CREATE POLICY provider_context_denied ON {name} AS RESTRICTIVE FOR ALL TO PUBLIC"
            " USING (NOT identity.rls_provider_context())"
            " WITH CHECK (NOT identity.rls_provider_context())"
        )
    return statements
