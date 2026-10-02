"""Política de proveedor en el expediente, la auditoría y la bandeja (TASK-139, LC-NUC-12).

Revisión nuc_0015 (BR-NUC-01 nota del 2026-09-20, BR-NUC-04, PAT-NUC-SEG-01 capa 2; PR-NUC-52).
El diseño pide una **segunda política** de proveedor en toda tabla con planta o zona; hasta aquí
solo la tenían las de ``identity`` (``nuc_0004`` y ``nuc_0009``). Las de ``ledger`` y ``shared``
solo acotaban por organización: un contexto de proveedor con una concesión de **una planta**
veía, en cuanto un puerto omitiera el filtro de ``allowed_scopes``, el expediente, las
evidencias, las etiquetas, la comunicación, la auditoría y los eventos de **todas** las plantas
del cliente; y con la concesión revocada o vencida, todo lo de la organización. La prueba de
aislamiento de la aplicación (``tests/isolation/test_provider_policy_app.py``) lo encontró.

**Políticas RESTRICTIVE nuevas** (se suman con ``AND`` a ``organization_isolation``), con el
criterio de ``identity.rls_provider_scope_allows``: fuera de un contexto de proveedor no cambian
nada; dentro, solo con la concesión de ``vigia.concession_id`` vigente y dentro de su alcance
(organización entera, o la planta de la fila; una fila sin planta, solo con alcance de
organización):

- ``provider_concession_scope`` (``ALL``) en ``ledger.ledger_record`` (planta de la cadena o,
  en una cadena de organización, la del alcance del registro), ``ledger.evidence``,
  ``ledger.label`` y ``ledger.communication_state``. El ``provider_query`` que escribe el
  proveedor va a la cadena del alcance concedido, así que su alta (con ``RETURNING``) pasa.
- ``provider_concession_scope`` (solo ``SELECT``, solo ``vigia_app``) en ``ledger.chain_head``:
  ``vigia_app`` solo la lee; el disparador de encadenado (``SECURITY DEFINER`` de
  ``vigia_migrate``) la sigue bloqueando y avanzando para cualquier cadena.
- ``provider_concession_scope`` (solo ``SELECT``) en ``shared.audit_entry`` (planta del alcance de
  la entrada) y ``shared.outbox_event``. En la auditoría, además, el proveedor ve las entradas que
  escribió su propia concesión vigente (``actor_concession_id``): la denegación por ruta de un
  proveedor se audita sin planta y su ``RETURNING`` tiene que verla. La bandeja ya no inserta con
  ``RETURNING`` (``shared.outbox.publish``): la alerta de denegaciones repetidas de un proveedor
  con concesión de una planta va a la partición de la organización, que ese contexto no lee, y
  con ``RETURNING`` la denegación tampoco quedaría auditada. Las altas no cambian: la auditoría y
  la bandeja de una petición del proveedor se siguen escribiendo.

**La transacción que concede o cierra la concesión** (PAT-NUC-SEG-01: «una concesión revocada
corta el acceso en la transacción **siguiente**»). El alta y el cierre por el proveedor escriben
primero la fila de ``identity.provider_concession`` (la proyección) y después, en la misma
transacción, ``provider_concession_granted`` o ``provider_concession_revoked`` en la cadena del
cliente, con ``RETURNING``, y su evento. Con las políticas nuevas, ese registro ya no se vería:
la concesión recién revocada no está vigente, y la recién concedida puede tener un
``granted_at`` (la hora de la aplicación) unos milisegundos posterior al ``now()`` de la base.
``identity.rls_provider_scope_allows`` e ``identity.rls_concession_reaches`` cuentan por eso como
vigente, **solo dentro de la transacción que la escribió**, la concesión del contexto cuya fila
creó o modificó esa transacción (``xmin`` igual al identificador de la transacción en curso,
``pg_current_xact_id_if_assigned``); ``provider_concession_own`` deja ver esa misma fila. Desde
la transacción siguiente rige el criterio de siempre: revocada o vencida, nada. Un contexto de
proveedor no puede provocar esa condición sobre una concesión que no está vigente: no la puede
modificar (``provider_concession_close`` exige la concesión vigente) ni volver a crear (clave
primaria). Las búsquedas y el cierre no usan subtransacciones (``SAVEPOINT``), así que el
``xmin`` de la fila es el de la transacción principal.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0015"
down_revision: str | None = "nuc_0014"
branch_labels: None = None
depends_on: None = None

_CONTEXT_CONCESSION = "NULLIF(pg_catalog.current_setting('vigia.concession_id', true), '')::uuid"

_WRITTEN_HERE = "xmin = pg_catalog.pg_current_xact_id_if_assigned()::xid"
"""La fila la creó o la modificó la transacción en curso (nulo si aún no escribió nada)."""

_IN_FORCE = f"""(
                    (
                        concession.status = 'active'
                        AND concession.revoked_at IS NULL
                        AND concession.granted_at <= pg_catalog.now()
                        AND concession.expires_at > pg_catalog.now()
                    )
                    OR concession.{_WRITTEN_HERE}
                )"""
"""Vigente, o escrita por esta misma transacción (el alta o el cierre que la escriben)."""

_FUNCTIONS = (
    f"""
    CREATE OR REPLACE FUNCTION identity.rls_concession_reaches(
        row_organization_id uuid, row_plant_id uuid, any_scope boolean
    ) RETURNS boolean
        LANGUAGE sql
        STABLE
    AS $$
        SELECT EXISTS (
            SELECT
            FROM identity.provider_concession AS concession
            WHERE concession.concession_id = {_CONTEXT_CONCESSION}
                AND concession.organization_id = row_organization_id
                AND {_IN_FORCE}
                AND (
                    any_scope
                    OR concession.scope_level = 'organization'
                    OR (concession.scope_level = 'plant' AND concession.scope_id = row_plant_id)
                )
        )
    $$
    """,  # noqa: S608 - solo constantes del módulo, sin entrada externa
    f"""
    CREATE OR REPLACE FUNCTION identity.rls_provider_scope_allows(
        row_organization_id uuid, row_plant_id uuid
    )
        RETURNS boolean
        LANGUAGE sql
        STABLE
    AS $$
        SELECT NOT identity.rls_provider_context()
            OR EXISTS (
                SELECT
                FROM identity.provider_concession AS concession
                WHERE concession.concession_id = {_CONTEXT_CONCESSION}
                    AND concession.organization_id = row_organization_id
                    AND {_IN_FORCE}
                    AND (
                        concession.scope_level = 'organization'
                        OR (concession.scope_level = 'plant' AND concession.scope_id = row_plant_id)
                    )
            )
    $$
    """,  # noqa: S608 - solo constantes del módulo, sin entrada externa
)

_OWN_POLICY = f"""
ALTER POLICY provider_concession_own ON identity.provider_concession
    USING (
        NOT identity.rls_provider_context()
        OR (
            concession_id = {_CONTEXT_CONCESSION}
            AND status = 'active' AND revoked_at IS NULL
            AND granted_at <= pg_catalog.now() AND expires_at > pg_catalog.now()
        )
        OR (
            concession_id = {_CONTEXT_CONCESSION}
            AND status = 'revoked'
            AND revoked_by_side = 'provider'
        )
        OR (concession_id = {_CONTEXT_CONCESSION} AND {_WRITTEN_HERE})
    )
"""

_IN_SCOPE = "identity.rls_provider_scope_allows(organization_id, {plant})"

_FULL_SCOPE = {
    "ledger.ledger_record": "COALESCE(plant_id, scope_plant_id)",
    "ledger.evidence": "plant_id",
    "ledger.label": "plant_id",
    "ledger.communication_state": "plant_id",
}
"""Tablas que el proveedor lee y puede escribir dentro de su alcance, con la planta de la fila."""

_OWN_CONCESSION = (
    f"(actor_concession_id = {_CONTEXT_CONCESSION}"
    " AND identity.rls_concession_reaches(organization_id, NULL, true))"
)
"""La entrada la escribió la concesión del contexto, que sigue vigente."""


def _policies() -> list[str]:
    statements = []
    for table, plant in _FULL_SCOPE.items():
        check = _IN_SCOPE.format(plant=plant)
        statements.append(
            f"CREATE POLICY provider_concession_scope ON {table}"
            f" AS RESTRICTIVE FOR ALL TO PUBLIC USING ({check}) WITH CHECK ({check})"
        )
    statements += [
        "CREATE POLICY provider_concession_scope ON ledger.chain_head"
        " AS RESTRICTIVE FOR SELECT TO vigia_app"
        f" USING ({_IN_SCOPE.format(plant='plant_id')})",
        "CREATE POLICY provider_concession_scope ON shared.audit_entry"
        " AS RESTRICTIVE FOR SELECT TO PUBLIC"
        f" USING ({_IN_SCOPE.format(plant='scope_plant_id')} OR {_OWN_CONCESSION})",
        "CREATE POLICY provider_concession_scope ON shared.outbox_event"
        " AS RESTRICTIVE FOR SELECT TO PUBLIC"
        f" USING ({_IN_SCOPE.format(plant='plant_id')})",
    ]
    return statements


_COMMENTS = (
    """
    COMMENT ON POLICY provider_concession_scope ON ledger.ledger_record IS
        'Con un contexto de proveedor, solo la concesión vigente y su alcance (PR-NUC-52)'
    """,
)


def upgrade() -> None:
    # Como en nuc_0009 y nuc_0014: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_FUNCTIONS, _OWN_POLICY, *_policies(), *_COMMENTS):
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
