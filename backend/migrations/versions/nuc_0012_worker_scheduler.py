"""Planificador de ``vigia-worker`` (TASK-130, LC-NUC-24; PAT-NUC-RES-05, ESC-04; BR-NUC-81).

Revisión nuc_0012. Lo que el planificador de tareas periódicas y el proceso de trabajo necesitan:

- ``shared.periodic_task``: avance de la ejecución en curso y último éxito.

  - ``progress_run_at``: el ``next_run_at`` que venció cuando se tomó la tarea; identifica la
    ejecución. ``progress_organization_id``: la última organización terminada (con éxito o con
    fallo registrado) de esa ejecución; las organizaciones se recorren en orden de
    ``organization_id``, así que quien retoma la tarea tras la caída de otro proceso empieza
    por la siguiente. ``progress_failures``: organizaciones con fallo en la ejecución.
  - ``last_success_at``: fin de la última ejecución sin fallos (métrica
    ``periodic_task_last_success_age_seconds``).

  El planificador avanza el cursor **dentro de la transacción de cada organización** y solo si
  sigue teniendo el arrendamiento (``lease_owner`` suyo y ``lease_until`` sin vencer): esa
  actualización es la valla; si no actualiza nada, la transacción se deshace entera. Es un
  registro global: solo identificadores, sin datos de cliente.
- ``shared.vigia_active_organizations()``: con ``FORCE ROW LEVEL SECURITY`` el proceso no ve
  ``identity.organization`` sin una organización fijada, y una tarea itera todas. Función
  ``SECURITY DEFINER`` de ``vigia_migrate`` con ``search_path`` fijo que devuelve **solo
  identificadores** de las organizaciones ``active``, en orden; solo con
  ``vigia.actor_kind = system`` (el contexto del sistema del proceso de trabajo). La política
  ``periodic_iteration`` deja leer a ``vigia_migrate`` solo mientras ``vigia.periodic_iteration``
  vale ``on``, y eso ocurre únicamente dentro de la función, como ``outbox_dispatch`` de nuc_0010.
- ``shared.vigia_outbox_oldest_pending(consumer)``: ``created_at`` de la entrega abierta más
  vieja del consumidor (métrica ``outbox_oldest_pending_age_seconds``, que escala el worker);
  solo con actor ``system``.
- ``shared.vigia_outbox_due_heads``: igual que en nuc_0010, pero ahora exige también actor
  ``system``, como ``vigia_outbox_replay`` exige ``operator`` (revisión de VIG-77).
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0012"
down_revision: str | None = "nuc_0011"
branch_labels: None = None
depends_on: None = None

_ITERATION_FLAG = "pg_catalog.current_setting('vigia.periodic_iteration', true) = 'on'"

_COLUMNS = (
    """
    ALTER TABLE shared.periodic_task
        ADD COLUMN last_success_at timestamptz,
        ADD COLUMN progress_run_at timestamptz,
        ADD COLUMN progress_organization_id uuid,
        ADD COLUMN progress_failures integer NOT NULL DEFAULT 0
            CONSTRAINT periodic_task_progress_failures CHECK (progress_failures >= 0),
        ADD CONSTRAINT periodic_task_progress_run
            CHECK (progress_run_at IS NOT NULL OR progress_organization_id IS NULL)
    """,
    """
    COMMENT ON COLUMN shared.periodic_task.progress_run_at IS
        'next_run_at de la ejecución en curso o la última (TASK-130)'
    """,
    """
    COMMENT ON COLUMN shared.periodic_task.progress_organization_id IS
        'Última organización terminada de progress_run_at, en orden de organization_id'
    """,
)

_POLICIES = (
    f"""
    CREATE POLICY periodic_iteration ON identity.organization
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING ({_ITERATION_FLAG})
    """,
    """
    COMMENT ON POLICY periodic_iteration ON identity.organization IS
        'Solo dentro de shared.vigia_active_organizations (vigia.periodic_iteration = on)'
    """,
)

_FUNCTIONS = (
    """
    CREATE FUNCTION shared.vigia_active_organizations()
        RETURNS TABLE (organization_id uuid)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    #variable_conflict use_column
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.periodic_iteration', 'on', true);
        RETURN QUERY
            SELECT organization.organization_id
            FROM identity.organization AS organization
            WHERE organization.status = 'active'
            ORDER BY organization.organization_id;
        PERFORM pg_catalog.set_config('vigia.periodic_iteration', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_active_organizations() IS
        'Organizaciones activas, solo identificadores y solo con actor system (TASK-130)'
    """,
    """
    CREATE FUNCTION shared.vigia_outbox_oldest_pending(target_consumer text)
        RETURNS timestamptz
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        oldest timestamptz;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RETURN NULL;
        END IF;
        PERFORM pg_catalog.set_config('vigia.outbox_dispatch', 'on', true);
        SELECT min(event.created_at) INTO oldest
            FROM shared.outbox_delivery AS delivery
            JOIN shared.outbox_event AS event ON event.event_id = delivery.event_id
            WHERE delivery.consumer_name = target_consumer
                AND delivery.status IN ('pending', 'retrying');
        PERFORM pg_catalog.set_config('vigia.outbox_dispatch', '', true);
        RETURN oldest;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_outbox_oldest_pending(text) IS
        'created_at de la entrega abierta más vieja del consumidor, solo con actor system'
    """,
    # El cuerpo de nuc_0010 con la comprobación del actor delante.
    """
    CREATE OR REPLACE FUNCTION shared.vigia_outbox_due_heads(
        target_consumer text, due_at timestamptz, max_heads integer
    )
        RETURNS TABLE (
            organization_id uuid, partition_key text, event_id uuid, correlation_id uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    #variable_conflict use_column
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'system' THEN
            RETURN;
        END IF;
        PERFORM pg_catalog.set_config('vigia.outbox_dispatch', 'on', true);
        RETURN QUERY
            SELECT head.organization_id, head.partition_key, head.event_id, head.correlation_id
            FROM (
                SELECT DISTINCT ON (event.partition_key)
                    event.organization_id, event.partition_key, event.event_id,
                    event.correlation_id, event.created_at, event.ledger_sequence,
                    event.publish_seq, delivery.next_attempt_at
                FROM shared.outbox_delivery AS delivery
                JOIN shared.outbox_event AS event ON event.event_id = delivery.event_id
                WHERE delivery.consumer_name = target_consumer
                    AND delivery.status IN ('pending', 'retrying')
                ORDER BY event.partition_key, event.created_at, event.ledger_sequence,
                    event.publish_seq
            ) AS head
            WHERE head.next_attempt_at <= due_at
            ORDER BY head.created_at, head.ledger_sequence, head.publish_seq
            LIMIT greatest(least(max_heads, 1000), 0);
        PERFORM pg_catalog.set_config('vigia.outbox_dispatch', '', true);
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_outbox_due_heads(text, timestamptz, integer) IS
        'Cabeza vencida de cada partición, solo identificadores, solo con actor system (TASK-130)'
    """,
    "REVOKE ALL ON FUNCTION shared.vigia_active_organizations() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION shared.vigia_active_organizations() TO vigia_app",
    "REVOKE ALL ON FUNCTION shared.vigia_outbox_oldest_pending(text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION shared.vigia_outbox_oldest_pending(text) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0010: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_COLUMNS, *_POLICIES, *_FUNCTIONS):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
