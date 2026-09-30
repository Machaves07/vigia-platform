"""Cadena según el alcance y guarda de los puntos de control en el encadenado (TASK-117).

Revisión nuc_0005 (LC-NUC-14, BR-NUC-45 y 53). Dos cambios en ``ledger.vigia_chain_link()``, que se
sustituye entera (``CREATE OR REPLACE``; el disparador de ``ledger.ledger_record`` y el de
``shared.audit_entry`` siguen apuntando a ella):

- **Nivel de cadena con ``chain_follows_scope``** (BR-NUC-45). ``ledger.record_type`` gana la
  columna ``chain_follows_scope`` (por omisión falsa), la misma declaración de
  ``RecordType.chain_follows_scope``: los tipos cuya cadena depende del alcance
  (``provider_concession_*``, ``provider_query``, ``checkpoint``) entran en la cadena de la
  planta si llevan planta y en la de organización si no. Hasta ahora el disparador exigía
  ``plant_id IS NOT NULL`` si y solo si el tipo era de nivel planta, y un punto de control de
  planta fallaba con un ``23514`` sin nombre. Ahora un tipo de planta exige planta, uno de
  organización la rechaza salvo que siga al alcance, y el rechazo lleva la restricción
  ``ledger_record_chain_level`` para que el escritor lo traduzca a ``content_invalid``.
  Las filas ya registradas de esos cinco tipos de U-02 se marcan aquí; el registro de tipos
  exige después que la columna coincida con el código.
- **Guarda de cobertura del punto de control** (BR-NUC-53). Con la exclusión de la cabeza ya
  tomada, un registro ``checkpoint`` (o una entrada de auditoría ``checkpoint``, cuyo contenido
  va en ``filters``) solo se encadena si ``covered_sequence`` y ``covered_hash`` son exactamente
  la secuencia y el hash de la cabeza: el registro anterior. Si otra escritura se adelantó entre
  la lectura de la cabeza y el ``INSERT``, el disparador responde ``check_violation`` con la
  restricción ``ledger_record_checkpoint_coverage`` y no queda nada; ``ledger.chain.checkpoints``
  relee la cabeza, vuelve a firmar y reintenta. Así la base nunca guarda un punto de control que
  el verificador daría por roto (``checkpoint_coverage``).
"""

from __future__ import annotations

from alembic import op

revision: str = "nuc_0005"
down_revision: str | None = "nuc_0004"
branch_labels: None = None
depends_on: None = None

_RECORD_TYPE = (
    """
    ALTER TABLE ledger.record_type
        ADD COLUMN chain_follows_scope boolean NOT NULL DEFAULT false
    """,
    """
    COMMENT ON COLUMN ledger.record_type.chain_follows_scope IS
        'El tipo entra en la cadena de la planta si su alcance lleva planta y en la de '
        'chain_level si no (BR-NUC-45)'
    """,
    # Los tipos de U-02 declarados con chain_follows_scope (ledger.record_types.u02).
    """
    UPDATE ledger.record_type SET chain_follows_scope = true
    WHERE record_type IN ('provider_concession_granted', 'provider_concession_revoked',
                          'provider_concession_expired', 'provider_query', 'checkpoint')
      AND chain_level = 'organization'
    """,
)

_CHAIN_LINK = (
    """
    CREATE OR REPLACE FUNCTION ledger.vigia_chain_link() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
    AS $$
    DECLARE
        chain_kind CONSTANT text := TG_ARGV[0];
        routed_at timestamptz;
        chain_plant uuid;
        expected_level text;
        follows_scope boolean;
        head ledger.chain_head;
        taken_at timestamptz;
        checkpoint jsonb;
        is_checkpoint boolean := false;
    BEGIN
        IF chain_kind NOT IN ('ledger', 'audit') THEN
            RAISE EXCEPTION 'vigia_chain_link: tipo de cadena desconocido %', chain_kind;
        END IF;
        IF NEW.organization_id IS DISTINCT FROM shared.vigia_current_organization() THEN
            RAISE EXCEPTION 'la fila no es de la organización del contexto de alcance'
                USING ERRCODE = 'insufficient_privilege';
        END IF;

        IF chain_kind = 'ledger' THEN
            SELECT chain_level, chain_follows_scope INTO expected_level, follows_scope
            FROM ledger.record_type WHERE record_type = NEW.record_type;
            -- BR-NUC-45: un tipo de planta exige planta; uno de organización la rechaza salvo
            -- que siga al alcance. Un tipo inexistente lo rechaza después la clave foránea.
            IF (expected_level = 'plant' AND NEW.plant_id IS NULL)
               OR (expected_level = 'organization' AND NEW.plant_id IS NOT NULL
                   AND NOT follows_scope) THEN
                RAISE EXCEPTION 'el tipo % es de cadena % y plant_id no concuerda',
                    NEW.record_type, expected_level
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'ledger_record_chain_level';
            END IF;
            chain_plant := NEW.plant_id;
            routed_at := NEW.received_at;
            IF NEW.record_type = 'checkpoint' THEN
                is_checkpoint := true;
                checkpoint := ledger.vigia_bytes_to_jsonb(NEW.content);
            END IF;
        ELSE
            chain_plant := NULL;
            routed_at := NEW.occurred_at;
            IF NEW.operation = 'checkpoint' THEN
                is_checkpoint := true;
                checkpoint := ledger.vigia_bytes_to_jsonb(NEW.filters);
            END IF;
        END IF;

        -- Exclusión sobre la cabeza (BR-NUC-47); si no existe, se crea con el hash de génesis.
        SELECT * INTO head FROM ledger.chain_head
        WHERE organization_id = NEW.organization_id AND kind = chain_kind
          AND plant_id IS NOT DISTINCT FROM chain_plant
        FOR UPDATE;
        IF NOT FOUND THEN
            INSERT INTO ledger.chain_head
                (organization_id, plant_id, kind, last_sequence, last_hash, updated_at)
            VALUES (
                NEW.organization_id, chain_plant, chain_kind, 0,
                encode(public.digest(convert_to(
                    'vigia:genesis:' || NEW.organization_id::text || ':'
                        || coalesce(chain_plant::text, 'organization'),
                    'UTF8'), 'sha256'), 'hex'),
                '-infinity'
            )
            ON CONFLICT ON CONSTRAINT chain_head_chain DO NOTHING;
            SELECT * INTO STRICT head FROM ledger.chain_head
            WHERE organization_id = NEW.organization_id AND kind = chain_kind
              AND plant_id IS NOT DISTINCT FROM chain_plant
            FOR UPDATE;
        END IF;

        -- BR-NUC-53: el punto de control cubre exactamente el registro anterior, comprobado con
        -- la exclusión tomada. Si otra escritura se adelantó, se rechaza y el llamador reintenta.
        IF is_checkpoint AND (
            checkpoint IS NULL
            OR jsonb_typeof(checkpoint) IS DISTINCT FROM 'object'
            OR jsonb_typeof(checkpoint -> 'covered_sequence') IS DISTINCT FROM 'number'
            OR (checkpoint ->> 'covered_sequence') IS DISTINCT FROM head.last_sequence::text
            OR (checkpoint ->> 'covered_hash') IS DISTINCT FROM head.last_hash
        ) THEN
            RAISE EXCEPTION 'el punto de control no cubre la cabeza de la cadena'
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'ledger_record_checkpoint_coverage';
        END IF;

        -- La marca se toma con la exclusión ya obtenida y nunca retrocede en la cadena.
        taken_at := greatest(date_trunc('milliseconds', clock_timestamp()), head.updated_at);
        IF routed_at IS NULL
           OR date_trunc('month', routed_at AT TIME ZONE 'UTC')
              <> date_trunc('month', taken_at AT TIME ZONE 'UTC') THEN
            RAISE EXCEPTION 'la marca de la cadena cambió de mes mientras se esperaba la '
                'exclusión; reintenta la escritura'
                USING ERRCODE = 'serialization_failure';
        END IF;

        NEW.chain_sequence := head.last_sequence + 1;
        NEW.previous_hash := head.last_hash;
        -- El identificador se reclama sin ON CONFLICT antes de avanzar la cabeza: si se repite,
        -- 23505 aborta la sentencia y la cabeza no avanza por una fila que no se insertaría.
        IF chain_kind = 'ledger' THEN
            NEW.received_at := taken_at;
            INSERT INTO ledger.record_identity (record_id, organization_id, received_at)
            VALUES (NEW.record_id, NEW.organization_id, NEW.received_at);
            NEW.content_hash := encode(public.digest(NEW.content, 'sha256'), 'hex');
            NEW.record_hash := encode(public.digest(
                ledger.vigia_canonical_envelope(NEW) || convert_to(NEW.previous_hash, 'UTF8'),
                'sha256'), 'hex');
            UPDATE ledger.chain_head
            SET last_sequence = NEW.chain_sequence, last_hash = NEW.record_hash,
                updated_at = taken_at
            WHERE organization_id = NEW.organization_id AND kind = chain_kind
              AND plant_id IS NOT DISTINCT FROM chain_plant;
            IF NEW.source_key IS NOT NULL THEN
                INSERT INTO ledger.record_source_key
                    (organization_id, record_type, source_key, record_id, received_at,
                     record_hash)
                VALUES (NEW.organization_id, NEW.record_type, NEW.source_key, NEW.record_id,
                        NEW.received_at, NEW.record_hash);
            END IF;
        ELSE
            NEW.occurred_at := taken_at;
            INSERT INTO shared.audit_entry_identity (entry_id, organization_id, occurred_at)
            VALUES (NEW.entry_id, NEW.organization_id, NEW.occurred_at);
            NEW.filters_hash := encode(public.digest(NEW.filters, 'sha256'), 'hex');
            NEW.entry_hash := encode(public.digest(
                shared.vigia_canonical_audit_envelope(NEW)
                    || convert_to(NEW.previous_hash, 'UTF8'),
                'sha256'), 'hex');
            UPDATE ledger.chain_head
            SET last_sequence = NEW.chain_sequence, last_hash = NEW.entry_hash,
                updated_at = taken_at
            WHERE organization_id = NEW.organization_id AND kind = chain_kind
              AND plant_id IS NULL;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    COMMENT ON FUNCTION ledger.vigia_chain_link() IS
        'Encadenado en la base (BR-NUC-45, 46, 47, 53, 60): nivel de cadena, exclusión, cobertura '
        'del punto de control, secuencia, marca y hashes'
    """,
    "REVOKE ALL ON FUNCTION ledger.vigia_chain_link() FROM PUBLIC",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in (*_RECORD_TYPE, *_CHAIN_LINK):
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
