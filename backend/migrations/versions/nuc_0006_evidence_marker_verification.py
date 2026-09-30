"""Verificación diferida de la marca del contenedor de las evidencias (TASK-121, pendiente nº 21).

Revisión nuc_0006 (LC-NUC-15 parte 2; NFR-NUC-33, PAT-NUC-MAN-04; adenda A-14). Aditiva:

- ``ledger.evidence`` gana tres columnas **fuera del sobre encadenado** (la evidencia no entra en
  el hash de ningún registro): ``marker_verification_result`` (``pending``, ``intact`` o
  ``broken``; por omisión ``pending``), ``marker_verified_at`` y ``container_marker_sampled_at``
  (nota fechada del 2026-09-23 de ``domain-entities.md`` §3.5: nombres distintos del
  ``verified_at`` de la verificación en la escritura). Una evidencia ``pending`` no tiene marcas;
  una verificada tiene las dos.
- El disparador de solo anexar de ``ledger.evidence`` (``evidence_append_only_row``) se sustituye
  por ``evidence_marker_transition``: ``DELETE`` sigue prohibido y un ``UPDATE`` solo pasa si es la
  **única** transición ``pending → intact | broken``, con sus dos marcas, y ninguna otra columna
  cambia. Una segunda escritura sobre una evidencia ya verificada falla (``restrict_violation``),
  también para un superusuario (``ENABLE ALWAYS`` en la tabla y en cada partición).
- ``ledger.evidence_sample_run``: una fila por organización y día muestreado con la **semilla
  registrada** de la selección, la población y el tamaño de la muestra; de solo anexar, con
  seguridad a nivel de fila. Con la semilla y la población la muestra se vuelve a calcular igual.

**Privilegios**: ``vigia_app`` puede actualizar **solo** las tres columnas nuevas de
``ledger.evidence`` (privilegio por columna) y lee e inserta en ``evidence_sample_run``.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0006"
down_revision: str | None = "nuc_0005"
branch_labels: None = None
depends_on: None = None

_HEX64 = "'^[0-9a-f]{64}$'"

_EVIDENCE_COLUMNS = (
    """
    ALTER TABLE ledger.evidence
        ADD COLUMN marker_verification_result text NOT NULL DEFAULT 'pending'
            CONSTRAINT evidence_marker_verification_result
                CHECK (marker_verification_result IN ('pending', 'intact', 'broken')),
        ADD COLUMN marker_verified_at timestamptz,
        ADD COLUMN container_marker_sampled_at timestamptz,
        ADD CONSTRAINT evidence_marker_marks CHECK (
            (marker_verification_result = 'pending'
                AND marker_verified_at IS NULL AND container_marker_sampled_at IS NULL)
            OR (marker_verification_result <> 'pending'
                AND marker_verified_at IS NOT NULL AND container_marker_sampled_at IS NOT NULL)
        )
    """,
    """
    COMMENT ON COLUMN ledger.evidence.marker_verification_result IS
        'Resultado de la verificación diferida de la marca del contenedor (pendiente nº 21); '
        'fuera del sobre encadenado; una sola transición pending → intact | broken'
    """,
    """
    COMMENT ON COLUMN ledger.evidence.marker_verified_at IS
        'Momento en que la muestra diaria escribió marker_verification_result'
    """,
    """
    COMMENT ON COLUMN ledger.evidence.container_marker_sampled_at IS
        'Momento en que la muestra diaria leyó el contenedor (NFR-NUC-33)'
    """,
    """
    CREATE INDEX evidence_sample_day
        ON ledger.evidence (organization_id, verified_at)
        WHERE content_type = 'video/mp4'
    """,
)

# Sustituye al disparador genérico de solo anexar. SECURITY INVOKER: no necesita privilegios.
_MARKER_TRANSITION = (
    """
    CREATE FUNCTION ledger.vigia_evidence_marker_transition() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
    AS $$
    DECLARE
        marker_columns CONSTANT text[] := ARRAY[
            'marker_verification_result', 'marker_verified_at', 'container_marker_sampled_at'];
    BEGIN
        IF TG_OP = 'UPDATE'
           AND OLD.marker_verification_result = 'pending'
           AND NEW.marker_verification_result IN ('intact', 'broken')
           AND NEW.marker_verified_at IS NOT NULL
           AND NEW.container_marker_sampled_at IS NOT NULL
           AND (to_jsonb(NEW) - marker_columns) = (to_jsonb(OLD) - marker_columns) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'la tabla %.% es de solo anexar: % solo admite la transición única '
            'pending → intact | broken de la marca', TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
            USING ERRCODE = 'restrict_violation';
    END
    $$
    """,
    """
    COMMENT ON FUNCTION ledger.vigia_evidence_marker_transition() IS
        'Solo anexar en ledger.evidence salvo la transición única pending → intact | broken de '
        'la verificación diferida de la marca (pendiente nº 21, BR-NUC-43)'
    """,
    "REVOKE ALL ON FUNCTION ledger.vigia_evidence_marker_transition() FROM PUBLIC",
    # Quitarlo de la tabla padre lo quita de cada partición; el nuevo se clona en todas.
    "DROP TRIGGER evidence_append_only_row ON ledger.evidence",
    """
    CREATE TRIGGER evidence_marker_transition
        BEFORE UPDATE OR DELETE ON ledger.evidence
        FOR EACH ROW EXECUTE FUNCTION ledger.vigia_evidence_marker_transition()
    """,
    "ALTER TABLE ledger.evidence ENABLE ALWAYS TRIGGER evidence_marker_transition",
    # Las particiones existentes, una a una (las futuras las protege create_partitions con
    # shared.vigia_protect_append_only_partition, que pone ENABLE ALWAYS en todos sus disparadores).
    """
    DO $$
    DECLARE
        partition regclass;
    BEGIN
        FOR partition IN
            SELECT inhrelid::regclass FROM pg_inherits
            WHERE inhparent = 'ledger.evidence'::regclass
        LOOP
            EXECUTE format(
                'ALTER TABLE %s ENABLE ALWAYS TRIGGER evidence_marker_transition', partition);
        END LOOP;
    END
    $$
    """,
)

_SAMPLE_RUN = (
    f"""
    CREATE TABLE ledger.evidence_sample_run (
        organization_id uuid NOT NULL,
        sample_day date NOT NULL,
        seed text NOT NULL
            CONSTRAINT evidence_sample_run_seed CHECK (seed ~ {_HEX64}),
        population integer NOT NULL
            CONSTRAINT evidence_sample_run_population CHECK (population >= 0),
        sample_size integer NOT NULL
            CONSTRAINT evidence_sample_run_sample_size
                CHECK (sample_size >= 0 AND sample_size <= population),
        started_at timestamptz NOT NULL,
        CONSTRAINT evidence_sample_run_pkey PRIMARY KEY (organization_id, sample_day)
    )
    """,
    """
    COMMENT ON TABLE ledger.evidence_sample_run IS
        'Muestra diaria de evidencias (PAT-NUC-MAN-04): semilla registrada, población y tamaño '
        'por organización y día; solo anexar'
    """,
    "ALTER TABLE ledger.evidence_sample_run ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE ledger.evidence_sample_run FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY organization_isolation ON ledger.evidence_sample_run
        USING (organization_id = shared.vigia_current_organization())
        WITH CHECK (organization_id = shared.vigia_current_organization())
    """,
    """
    CREATE TRIGGER evidence_sample_run_append_only_row
        BEFORE UPDATE OR DELETE ON ledger.evidence_sample_run
        FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()
    """,
    """
    CREATE TRIGGER evidence_sample_run_append_only_truncate
        BEFORE TRUNCATE ON ledger.evidence_sample_run
        FOR EACH STATEMENT EXECUTE FUNCTION shared.vigia_reject_mutation()
    """,
    "ALTER TABLE ledger.evidence_sample_run ENABLE ALWAYS TRIGGER "
    "evidence_sample_run_append_only_row",
    "ALTER TABLE ledger.evidence_sample_run ENABLE ALWAYS TRIGGER "
    "evidence_sample_run_append_only_truncate",
)

_APP_GRANTS = (
    """
    GRANT UPDATE (marker_verification_result, marker_verified_at, container_marker_sampled_at)
        ON ledger.evidence TO vigia_app
    """,
    "GRANT SELECT, INSERT ON ledger.evidence_sample_run TO vigia_app",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_EVIDENCE_COLUMNS, *_MARKER_TRANSITION, *_SAMPLE_RUN, *_APP_GRANTS):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
