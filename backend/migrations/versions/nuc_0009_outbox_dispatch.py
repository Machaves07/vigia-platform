"""Despacho de la bandeja de salida (TASK-129, LC-NUC-23 parte 2; BR-NUC-76 a 82; PAT-NUC-ESC-05).

Revisión nuc_0009. Lo que el despachador y el reproceso necesitan sobre las tablas de nuc_0003:

- ``shared.outbox_event.publish_seq``: identidad ``bigint`` que la base asigna al insertar. Dos
  eventos de una partición publicados en el mismo milisegundo, sin ``ledger_sequence``, quedaban
  empatados; ``publish_seq`` crece con cada inserción (dentro de una transacción, en el orden de
  publicación) y hace **total** el orden de entrega ``(created_at, ledger_sequence, publish_seq)``
  (seguimiento 1 de VIG-47).
- ``shared.outbox_event.trace_id`` y ``span_id``: el tramo en curso al publicar (hexadecimal de
  W3C, los dos o ninguno). El despachador enlaza con él el tramo de la entrega (PAT-NUC-MAN-01).
- ``shared.vigia_outbox_due_heads(consumer, due_at, max_heads)``: con ``FORCE ROW LEVEL
  SECURITY`` el despachador no ve entregas sin una organización fijada, y las particiones
  pendientes son de muchas. Es una función ``SECURITY DEFINER`` de ``vigia_migrate`` con
  ``search_path`` fijo que devuelve **solo identificadores** de la cabeza de cada partición del
  consumidor (la entrega ``pending`` o ``retrying`` más antigua) si ya vence: organización,
  partición, ``event_id`` y ``correlation_id``. Nunca la carga. El despachador procesa cada una
  en su propia transacción, con el contexto de la organización del evento (BR-NUC-80).
- ``shared.vigia_outbox_replay(event_id, consumer, at)``: devuelve a ``pending`` con
  ``attempts = 0`` la entrega en ``dead_letter`` que tenga su fila en la cola muerta, y devuelve
  su organización (o nulo). Solo actúa con ``vigia.actor_kind = operator``; la autorización
  ``platform.dead_letter.replay`` la hace antes ``shared.outbox.replay``, y la auditoría
  ``dead_letter_replayed`` va en la misma transacción (BR-NUC-82). La cola muerta no se toca
  (solo anexar): el reproceso no borra ni reescribe nada de ella.
- Las políticas ``outbox_dispatch`` dejan a ``vigia_migrate`` leer ``outbox_event``,
  ``outbox_delivery`` y ``dead_letter`` y actualizar ``outbox_delivery`` **solo** mientras
  ``vigia.outbox_dispatch`` vale ``on``, y eso ocurre únicamente dentro de las dos funciones (la
  fijan al entrar y la vacían antes de salir), como ``login_lookup`` de nuc_0007. ``vigia_app``
  solo puede ejecutarlas; fijar él mismo la variable no le da nada, porque las políticas son solo
  para ``vigia_migrate``.
- ``outbox_delivery_consumer_due``: índice de las entregas abiertas por consumidor.
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0009"
down_revision: str | None = "nuc_0008"
branch_labels: None = None
depends_on: None = None

_DISPATCH_FLAG = "pg_catalog.current_setting('vigia.outbox_dispatch', true) = 'on'"

_COLUMNS = (
    """
    ALTER TABLE shared.outbox_event
        ADD COLUMN publish_seq bigint GENERATED ALWAYS AS IDENTITY,
        ADD COLUMN trace_id text
            CONSTRAINT outbox_event_trace_id CHECK (trace_id ~ '^[0-9a-f]{32}$'),
        ADD COLUMN span_id text
            CONSTRAINT outbox_event_span_id CHECK (span_id ~ '^[0-9a-f]{16}$'),
        ADD CONSTRAINT outbox_event_trace_link CHECK ((trace_id IS NULL) = (span_id IS NULL))
    """,
    """
    COMMENT ON COLUMN shared.outbox_event.publish_seq IS
        'Orden de inserción: desempata (created_at, ledger_sequence) dentro de la partición'
    """,
    """
    COMMENT ON COLUMN shared.outbox_event.trace_id IS
        'Traza W3C del tramo que publicó el evento (enlace con la entrega, PAT-NUC-MAN-01)'
    """,
    """
    CREATE INDEX outbox_delivery_consumer_due
        ON shared.outbox_delivery (consumer_name, next_attempt_at)
        WHERE status IN ('pending', 'retrying')
    """,
)

_POLICIES = (
    f"""
    CREATE POLICY outbox_dispatch ON shared.outbox_event
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING ({_DISPATCH_FLAG})
    """,
    f"""
    CREATE POLICY outbox_dispatch ON shared.outbox_delivery
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING ({_DISPATCH_FLAG})
    """,
    f"""
    CREATE POLICY outbox_dispatch_replay ON shared.outbox_delivery
        AS PERMISSIVE FOR UPDATE TO vigia_migrate
        USING ({_DISPATCH_FLAG})
        WITH CHECK ({_DISPATCH_FLAG})
    """,
    f"""
    CREATE POLICY outbox_dispatch ON shared.dead_letter
        AS PERMISSIVE FOR SELECT TO vigia_migrate
        USING ({_DISPATCH_FLAG})
    """,
)

# Como en nuc_0007: la variable se fija con set_config dentro y se vacía antes de salir.
_FUNCTIONS = (
    """
    CREATE FUNCTION shared.vigia_outbox_due_heads(
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
        'Cabeza vencida de cada partición del consumidor, solo identificadores (TASK-129)'
    """,
    """
    CREATE FUNCTION shared.vigia_outbox_replay(
        target_event uuid, target_consumer text, replay_at timestamptz
    )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        found uuid;
    BEGIN
        IF pg_catalog.current_setting('vigia.actor_kind', true) IS DISTINCT FROM 'operator' THEN
            RETURN NULL;
        END IF;
        PERFORM pg_catalog.set_config('vigia.outbox_dispatch', 'on', true);
        UPDATE shared.outbox_delivery AS delivery
            SET status = 'pending', attempts = 0, next_attempt_at = replay_at,
                last_error_code = NULL
            WHERE delivery.event_id = target_event
                AND delivery.consumer_name = target_consumer
                AND delivery.status = 'dead_letter'
                AND EXISTS (
                    SELECT 1 FROM shared.dead_letter AS letter
                    WHERE letter.event_id = delivery.event_id
                        AND letter.consumer_name = delivery.consumer_name
                )
            RETURNING delivery.organization_id INTO found;
        PERFORM pg_catalog.set_config('vigia.outbox_dispatch', '', true);
        RETURN found;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION shared.vigia_outbox_replay(uuid, text, timestamptz) IS
        'Reproceso de una entrega en cola muerta, solo con actor operador (BR-NUC-82, TASK-129)'
    """,
    "REVOKE ALL ON FUNCTION shared.vigia_outbox_due_heads(text, timestamptz, integer) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION shared.vigia_outbox_due_heads(text, timestamptz, integer)"
    " TO vigia_app",
    "REVOKE ALL ON FUNCTION shared.vigia_outbox_replay(uuid, text, timestamptz) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION shared.vigia_outbox_replay(uuid, text, timestamptz) TO vigia_app",
)


def upgrade() -> None:
    # Como en nuc_0003: todo lo creado es de vigia_migrate.
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_COLUMNS, *_POLICIES, *_FUNCTIONS):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
