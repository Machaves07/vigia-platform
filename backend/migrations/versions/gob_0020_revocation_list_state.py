"""Estado global de la lista de revocación de ``vigia-node-ca`` (TASK-218; D-7, PAT-GOB-RES-02).

Revisión gob_0020. La revocación de un nodo (capa 1, BR-GOB-66) deja en **su misma transacción**
la marca única de la lista de revocación: ``dirty_generation`` sube en uno y ``dirty_since``
guarda el primer instante sin publicar. ``regenerate_revocation_list`` (TASK-220, contexto de
operador) lee la marca, publica ``ca/crl.pem`` y deja ``published_generation`` en la generación
que publicó (si entretanto hubo otra revocación, la marca sigue sucia): nunca espera nadie al
almacén de confianza.

``fleet.revocation_list_state`` es **una fila global** sin ``organization_id`` ni dato de cliente
(contadores, instantes, la versión del objeto y el número de entradas). Es la excepción
documentada a la seguridad a nivel de fila de ``fleet``: la revocación la hace un instalador bajo
concesión, a veces de **una planta**, y la marca ``operator_only`` de
``revocation_list_publication`` (gob_0018) no la deja escribir desde ese contexto. La marca por
organización (``revocation_list_dirty``) queda solo como métrica (nota T-07 de §3.1).

``vigia_app``: ``SELECT`` y ``UPDATE`` de las columnas de estado; nunca ``INSERT`` ni ``DELETE``
(la fila se siembra aquí). No cambia ninguna tabla, función ni política existente: la imagen
anterior sigue igual sobre este esquema.
"""

from __future__ import annotations

from alembic import op

revision: str = "gob_0020"
down_revision: str | None = "gob_0019"
branch_labels: None = None
depends_on: None = None

TABLE = "revocation_list_state"
STATE_COLUMNS = (
    "dirty_generation",
    "dirty_since",
    "published_generation",
    "published_at",
    "object_version_id",
    "next_update",
    "entries",
)
"""Las columnas que ``vigia_app`` puede actualizar (todas salvo la clave de la fila única)."""

_STATEMENTS = (
    f"""
    CREATE TABLE fleet.{TABLE} (
        singleton boolean PRIMARY KEY DEFAULT true
            CONSTRAINT revocation_list_state_singleton CHECK (singleton),
        dirty_generation bigint NOT NULL DEFAULT 0
            CONSTRAINT revocation_list_state_dirty_generation CHECK (dirty_generation >= 0),
        dirty_since timestamptz,
        published_generation bigint NOT NULL DEFAULT 0
            CONSTRAINT revocation_list_state_published_generation CHECK (
                published_generation BETWEEN 0 AND dirty_generation
            ),
        published_at timestamptz,
        object_version_id text
            CONSTRAINT revocation_list_state_object_version_id_length
                CHECK (char_length(object_version_id) BETWEEN 1 AND 1024),
        next_update timestamptz,
        entries integer NOT NULL DEFAULT 0
            CONSTRAINT revocation_list_state_entries CHECK (entries >= 0),
        CONSTRAINT revocation_list_state_dirty_since
            CHECK (dirty_since IS NULL OR dirty_generation > published_generation),
        CONSTRAINT revocation_list_state_published
            CHECK ((published_at IS NULL) = (object_version_id IS NULL))
    )
    """,
    f"INSERT INTO fleet.{TABLE} (singleton) VALUES (true)",  # noqa: S608 - solo la constante
    f"""
    COMMENT ON TABLE fleet.{TABLE} IS
        'Marca única y estado de publicación de la lista de revocación global (D-7); fila global '
        'sin datos de cliente ni seguridad a nivel de fila (TASK-218)'
    """,
    f"REVOKE ALL ON fleet.{TABLE} FROM PUBLIC",
    f"GRANT SELECT ON fleet.{TABLE} TO vigia_app",
    f"GRANT UPDATE ({', '.join(STATE_COLUMNS)}) ON fleet.{TABLE} TO vigia_app",
)


def upgrade() -> None:
    # Como en gob_0018 y gob_0019: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
