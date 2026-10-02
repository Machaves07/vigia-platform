"""Particiones y archivado de auditoría (TASK-131, LC-NUC-33; PAT-NUC-ESC-01, MAN-03).

Revisión nuc_0015. Funciones ``SECURITY DEFINER`` de ``vigia_migrate`` (el dueño de las tablas
particionadas) con ``search_path`` fijo, para que el proceso de trabajo (``vigia_app``) haga lo que
sus privilegios no le dejan: crear particiones, contar la partición por defecto, leer una partición
de auditoría entera (todas las organizaciones) y desprenderla. ``vigia_app`` solo puede
ejecutarlas, y cada una exige el actor del contexto (``vigia.actor_kind``, el ``SET LOCAL`` de
``shared.db``):

- ``shared.vigia_create_month_partitions(first_month, last_month)`` (actor ``system`` u
  ``operator``): crea, para ``ledger.ledger_record``, ``ledger.evidence`` y
  ``shared.audit_entry``, la partición mensual (UTC) de cada mes del intervalo que no exista, la
  protege con ``shared.vigia_protect_append_only_partition`` (nuc_0002: vaciarla falla y sus
  disparadores quedan con ``ENABLE ALWAYS``) y no concede nada sobre ella: se accede por la tabla
  padre, donde están las políticas. **Idempotente**: un nombre que ya existe (también una
  partición de auditoría ya desprendida) no se toca. Si la partición por defecto ya tiene filas de
  ese mes, ``CREATE TABLE ... PARTITION OF`` fallaría (PostgreSQL comprueba la partición por
  defecto) y esas filas no se pueden mover (``DELETE`` está bloqueado): el mes se salta con
  ``blocked``, para que la alarma de ``default_partition_rows`` lo haga visible. A lo sumo 120
  meses por llamada; ``lock_timeout`` de 5 s para no dejar en cola a los escritores. Las llamadas
  se **serializan** con un candado consultivo de transacción: dos a la vez (la tarea y la orden
  administrativa) se bloqueaban entre sí con ``deadlock detected``.
- ``shared.vigia_default_partition_rows()`` (``system`` u ``operator``): filas de cada partición
  por defecto (métrica ``default_partition_rows``, alarma con una sola fila).
- ``shared.vigia_audit_partition_summary(partition)`` y
  ``shared.vigia_audit_partition_rows(partition, after_organization, after_sequence, max_rows)``
  (solo ``system``): resumen por organización y lectura por lotes, en orden de organización y
  secuencia, de una partición **adjunta** de ``shared.audit_entry`` con nombre
  ``audit_entry_AAAA_MM``. Leen la partición directamente (la seguridad a nivel de fila está en la
  tabla padre): es la exportación del archivado, que cubre todas las organizaciones.
- ``shared.vigia_detach_audit_partition(partition, expected_entries, not_after)`` (solo
  ``system``): bloquea la partición, comprueba que tiene exactamente las entradas verificadas y
  ninguna posterior a ``not_after``, la desprende (``DETACH PARTITION``, sin ``CONCURRENTLY``
  para que vaya en la transacción del registro ``audit_partition_archived``) y deja la tabla
  desprendida de solo anexar: ``UPDATE`` y ``DELETE`` fallan (disparador con ``ENABLE ALWAYS``)
  y el de ``TRUNCATE`` se conserva. Nada se borra (P4).
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0015"
down_revision: str | None = "nuc_0014"
branch_labels: None = None
depends_on: None = None


_FUNCTIONS = (
    """
    CREATE FUNCTION shared.vigia_create_month_partitions(first_month date, last_month date)
        RETURNS TABLE (parent text, partition text, month date, created boolean, blocked boolean)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET lock_timeout = '5s'
    AS $$
    #variable_conflict use_variable
    DECLARE
        target record;
        current_month date;
        range_start timestamptz;
        range_end timestamptz;
        partition_name text;
        has_rows boolean;
    BEGIN
        IF coalesce(pg_catalog.current_setting('vigia.actor_kind', true), '')
            NOT IN ('system', 'operator')
        THEN
            RAISE EXCEPTION 'solo el sistema o un operador crea particiones'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF first_month IS NULL OR last_month IS NULL
            OR first_month <> date_trunc('month', first_month)::date
            OR last_month <> date_trunc('month', last_month)::date
            OR last_month < first_month
            OR last_month > (first_month + interval '119 months')::date
        THEN
            RAISE EXCEPTION 'intervalo de meses no válido'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        -- Una llamada a la vez hasta el final de la transacción (la tarea semanal y la orden
        -- vigia-admin create-partitions comparten esta función): sin el candado, dos llamadas
        -- simultáneas se bloquean entre sí al crear las mismas particiones (deadlock detected).
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('vigia_create_month_partitions', 0));
        FOR target IN
            SELECT * FROM (VALUES
                ('ledger', 'ledger_record', 'received_at'),
                ('ledger', 'evidence', 'verified_at'),
                ('shared', 'audit_entry', 'occurred_at')
            ) AS tables (schema_name, table_name, key_column)
        LOOP
            current_month := first_month;
            WHILE current_month <= last_month LOOP
                range_start := (current_month::timestamp AT TIME ZONE 'UTC');
                range_end := ((current_month + interval '1 month')::timestamp AT TIME ZONE 'UTC');
                partition_name := target.table_name || to_char(current_month, '"_"YYYY"_"MM');
                parent := target.schema_name || '.' || target.table_name;
                partition := target.schema_name || '.' || partition_name;
                month := current_month;
                created := false;
                blocked := false;
                IF to_regclass(format('%I.%I', target.schema_name, partition_name)) IS NULL THEN
                    EXECUTE format(
                        'SELECT EXISTS (SELECT 1 FROM %I.%I WHERE %I >= $1 AND %I < $2)',
                        target.schema_name, target.table_name || '_default',
                        target.key_column, target.key_column)
                        INTO has_rows USING range_start, range_end;
                    IF has_rows THEN
                        blocked := true;
                    ELSE
                        BEGIN
                            EXECUTE format(
                                'CREATE TABLE %I.%I PARTITION OF %I.%I'
                                ' FOR VALUES FROM (%L) TO (%L)',
                                target.schema_name, partition_name,
                                target.schema_name, target.table_name,
                                range_start, range_end);
                            PERFORM shared.vigia_protect_append_only_partition(
                                format('%I.%I', target.schema_name, partition_name)::regclass);
                            created := true;
                        EXCEPTION WHEN duplicate_table THEN
                            -- Segunda capa tras el candado: si aun así ya existe (creada fuera
                            -- de esta función), es lo que se pedía.
                            created := false;
                        END;
                    END IF;
                END IF;
                RETURN NEXT;
                current_month := (current_month + interval '1 month')::date;
            END LOOP;
        END LOOP;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_create_month_partitions(date, date) IS
        'Particiones mensuales que falten de ledger_record, evidence y audit_entry, protegidas; '
        'salta el mes con filas en la partición por defecto (TASK-131)'
    """,
    """
    CREATE FUNCTION shared.vigia_default_partition_rows()
        RETURNS TABLE (parent text, row_count bigint)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    BEGIN
        IF coalesce(pg_catalog.current_setting('vigia.actor_kind', true), '')
            NOT IN ('system', 'operator')
        THEN
            RAISE EXCEPTION 'solo el sistema o un operador lee la partición por defecto'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN QUERY
            SELECT 'ledger.ledger_record', count(*) FROM ledger.ledger_record_default
            UNION ALL
            SELECT 'ledger.evidence', count(*) FROM ledger.evidence_default
            UNION ALL
            SELECT 'shared.audit_entry', count(*) FROM shared.audit_entry_default;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_default_partition_rows() IS
        'Filas de cada partición por defecto (default_partition_rows, PAT-NUC-ESC-01)'
    """,
    # Una partición de auditoría adjunta y con el nombre del convenio, o un error: las funciones
    # de archivado no leen ni desprenden otra tabla.
    """
    CREATE FUNCTION shared.vigia_attached_audit_partition(partition_name text) RETURNS regclass
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        found regclass;
    BEGIN
        IF partition_name IS NULL OR partition_name !~ '^audit_entry_[0-9]{4}_(0[1-9]|1[0-2])$' THEN
            RAISE EXCEPTION 'nombre de partición de auditoría no válido'
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        SELECT inheritance.inhrelid::regclass INTO found
            FROM pg_inherits AS inheritance
            JOIN pg_class AS child ON child.oid = inheritance.inhrelid
            WHERE inheritance.inhparent = 'shared.audit_entry'::regclass
                AND child.relname = partition_name;
        IF found IS NULL THEN
            RAISE EXCEPTION 'la partición de auditoría no está adjunta'
                USING ERRCODE = 'undefined_table';
        END IF;
        RETURN found;
    END
    $$
    """,
    "REVOKE ALL ON FUNCTION shared.vigia_attached_audit_partition(text) FROM PUBLIC",
    """
    CREATE FUNCTION shared.vigia_audit_partition_summary(partition_name text)
        RETURNS TABLE (
            organization_id uuid, entries bigint, first_sequence bigint, last_sequence bigint
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        source regclass;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RAISE EXCEPTION 'solo el sistema lee una partición de auditoría entera'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        source := shared.vigia_attached_audit_partition(partition_name);
        RETURN QUERY EXECUTE format(
            'SELECT organization_id, count(*), min(chain_sequence), max(chain_sequence)'
            ' FROM %s GROUP BY organization_id ORDER BY organization_id', source);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_audit_partition_summary(text) IS
        'Entradas y secuencias por organización de una partición de auditoría adjunta (TASK-131)'
    """,
    """
    CREATE FUNCTION shared.vigia_audit_partition_rows(
        partition_name text, after_organization uuid, after_sequence bigint, max_rows integer
    )
        RETURNS SETOF shared.audit_entry
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        source regclass;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RAISE EXCEPTION 'solo el sistema lee una partición de auditoría entera'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        source := shared.vigia_attached_audit_partition(partition_name);
        RETURN QUERY EXECUTE format(
            'SELECT * FROM %s WHERE (organization_id, chain_sequence) > ($1, $2)'
            ' ORDER BY organization_id, chain_sequence LIMIT $3', source)
            USING coalesce(after_organization, '00000000-0000-0000-0000-000000000000'::uuid),
                coalesce(after_sequence, 0), greatest(least(max_rows, 10000), 1);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_audit_partition_rows(text, uuid, bigint, integer) IS
        'Lote de una partición de auditoría adjunta, en orden de organización y secuencia '
        '(exportación del archivado, TASK-131)'
    """,
    """
    CREATE FUNCTION shared.vigia_detach_audit_partition(
        partition_name text, expected_entries bigint, not_after timestamptz
    )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        SET lock_timeout = '5s'
    AS $$
    DECLARE
        source regclass;
        entries bigint;
        newest timestamptz;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RAISE EXCEPTION 'solo el sistema desprende una partición de auditoría'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        source := shared.vigia_attached_audit_partition(partition_name);
        -- El candado espera a los escritores en curso y cierra el paso a los nuevos: el recuento
        -- de abajo es el de lo que se desprende. Tras esperarlo, otra llamada pudo haberla
        -- desprendido ya: se comprueba de nuevo.
        EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE', source);
        IF shared.vigia_attached_audit_partition(partition_name) IS DISTINCT FROM source THEN
            RAISE EXCEPTION 'la partición de auditoría no está adjunta'
                USING ERRCODE = 'undefined_table';
        END IF;
        EXECUTE format('SELECT count(*), max(occurred_at) FROM %s', source)
            INTO entries, newest;
        IF entries IS DISTINCT FROM expected_entries
            OR not_after IS NULL
            OR (newest IS NOT NULL AND newest >= not_after)
        THEN
            RAISE EXCEPTION 'la partición no es la que se verificó'
                USING ERRCODE = 'object_not_in_prerequisite_state';
        END IF;
        EXECUTE format('ALTER TABLE shared.audit_entry DETACH PARTITION %s', source);
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = source AND tgname = 'archived_append_only_row'
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER archived_append_only_row BEFORE UPDATE OR DELETE ON %s '
                'FOR EACH ROW EXECUTE FUNCTION shared.vigia_reject_mutation()', source);
        END IF;
        EXECUTE format('ALTER TABLE %s ENABLE ALWAYS TRIGGER archived_append_only_row', source);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_detach_audit_partition(text, bigint, timestamptz) IS
        'Desprende una partición de auditoría ya archivada y verificada; la tabla desprendida '
        'sigue siendo de solo anexar (TASK-131, PAT-NUC-MAN-03)'
    """,
)

_GRANTS = tuple(
    statement
    for signature in (
        "shared.vigia_create_month_partitions(date, date)",
        "shared.vigia_default_partition_rows()",
        "shared.vigia_audit_partition_summary(text)",
        "shared.vigia_audit_partition_rows(text, uuid, bigint, integer)",
        "shared.vigia_detach_audit_partition(text, bigint, timestamptz)",
    )
    for statement in (
        f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC",
        f"GRANT EXECUTE ON FUNCTION {signature} TO vigia_app",
    )
)


def upgrade() -> None:
    # Como en nuc_0010 y nuc_0013: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_FUNCTIONS, *_GRANTS):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
