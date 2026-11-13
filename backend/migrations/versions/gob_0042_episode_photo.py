"""Foto del episodio de la sombra (TASK-572; A-112; BR-GOB-84; NFR-GOB-33).

Revisión gob_0042, aditiva: una tabla nueva.

``fleet.episode_photo`` guarda el registro ``episode_photo`` que el nodo presenta en
``commissioning`` por ``POST /api/nodes/episode-photos`` (v1.5.0 del contrato): uno por tramo, con
la foto difuminada del cuadro de mayor confianza o con el motivo de su ausencia. Es material de
puesta en marcha (QF10): **no** entra en el expediente ni en la cadena, así que no es de solo
anexar. La purga por vencimiento y al aprobar la compuerta de uso es de TASK-573, que añadirá el
``DELETE`` de ``vigia_app``.

- Clave ``(organization_id, photo_id)``: la idempotencia es por organización (BR-GOB-89). La
  ingesta inserta con ``ON CONFLICT DO NOTHING`` y compara ``canonical_sha256`` en la misma
  transacción (BR-GOB-84, paso 5).
- Columnas **anulables** de la imagen: ``camera_id``, ``frame_at``, ``width``, ``height``,
  ``size_bytes``, ``sha256``, ``blur_params_version``, ``image`` (``bytea``) e
  ``image_absent_reason``.
- ``CHECK`` de coherencia: imagen presente si y solo si el motivo es nulo; con imagen, también la
  cámara, el instante del cuadro, el ancho, el alto, el tamaño, la suma y la versión del
  difuminado; sin imagen, ninguno de los cinco de la imagen. Tamaño declarado ≤ 204 800; ancho y
  alto de 64 a 1280; sumas en hexadecimal de 64. Que los bytes midan ``size_bytes``, tengan esa
  suma, sean un JPEG con la marca y esas dimensiones lo comprueba la ingesta antes de confirmar
  (paso 6): una fila confirmada siempre lo cumple.
- ``expires_at = received_at + VIGIA_EPISODE_PHOTO_RETENTION_DAYS`` (de 1 a 90 días): la fila lo
  exige posterior a la recepción y como mucho 90 días después.
- ``FORCE ROW LEVEL SECURITY`` con ``organization_isolation`` y la RESTRICTIVE
  ``provider_concession_scope`` de las tablas de ``fleet`` (la proveedora la lee solo bajo
  concesión, A-69). ``vigia_app``: ``SELECT`` e ``INSERT``.

La imagen anterior no lee ni escribe la tabla nueva: arranca igual sobre este esquema
(NFR-NUC-14). ``MINIMUM_SCHEMA_VERSION`` sube a 42: el código de esta imagen sirve la ruta nueva.
"""

from __future__ import annotations

from typing import Final

from alembic import op

revision: str = "gob_0042"
down_revision: str | None = "laz_0041"
branch_labels: None = None
depends_on: None = None

TABLE: Final = "episode_photo"
"""Tabla nueva de ``fleet``."""

IMAGE_ABSENT_REASONS: Final = ("frame_unavailable", "too_large", "disk_pressure", "pending_cap")
"""Lista cerrada ``image_absent_reason`` del contrato v1.5.0."""

_STATEMENTS = (
    f"""
    CREATE TABLE fleet.episode_photo (
        organization_id uuid NOT NULL,
        photo_id uuid NOT NULL,
        plant_id uuid NOT NULL,
        zone_id uuid NOT NULL,
        node_id uuid NOT NULL,
        episode_id uuid NOT NULL,
        segment_index integer NOT NULL
            CONSTRAINT episode_photo_segment_index CHECK (segment_index >= 0),
        classified_as text NOT NULL
            CONSTRAINT episode_photo_classified_as_values
            CHECK (classified_as IN ('finding', 'detection_for_review')),
        family text NOT NULL
            CONSTRAINT episode_photo_family_length CHECK (char_length(family) BETWEEN 1 AND 64),
        standard_id uuid NOT NULL,
        standard_version integer NOT NULL
            CONSTRAINT episode_photo_standard_version CHECK (standard_version >= 1),
        max_confidence double precision NOT NULL
            CONSTRAINT episode_photo_max_confidence CHECK (max_confidence BETWEEN 0 AND 1),
        started_at timestamptz NOT NULL,
        ended_at timestamptz NOT NULL,
        received_at timestamptz NOT NULL,
        expires_at timestamptz NOT NULL,
        camera_id uuid,
        frame_at timestamptz,
        width integer
            CONSTRAINT episode_photo_width CHECK (width BETWEEN 64 AND 1280),
        height integer
            CONSTRAINT episode_photo_height CHECK (height BETWEEN 64 AND 1280),
        size_bytes integer
            CONSTRAINT episode_photo_size_bytes CHECK (size_bytes BETWEEN 1 AND 204800),
        sha256 text
            CONSTRAINT episode_photo_sha256_format CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
        blur_params_version text
            CONSTRAINT episode_photo_blur_params_version_length
            CHECK (char_length(blur_params_version) BETWEEN 1 AND 64),
        image bytea,
        image_absent_reason text
            CONSTRAINT episode_photo_image_absent_reason_values
            CHECK (image_absent_reason IN ({", ".join(f"'{r}'" for r in IMAGE_ABSENT_REASONS)})),
        canonical_sha256 text NOT NULL
            CONSTRAINT episode_photo_canonical_sha256_format
            CHECK (canonical_sha256 ~ '^[0-9a-f]{{64}}$'),
        platform_record_id uuid NOT NULL,
        CONSTRAINT episode_photo_pkey PRIMARY KEY (organization_id, photo_id),
        CONSTRAINT episode_photo_interval CHECK (ended_at >= started_at),
        CONSTRAINT episode_photo_expiry
            CHECK (expires_at > received_at AND expires_at <= received_at + interval '90 days'),
        CONSTRAINT episode_photo_image_xor_reason
            CHECK ((image IS NULL) = (image_absent_reason IS NOT NULL)),
        CONSTRAINT episode_photo_image_facts CHECK (
            CASE WHEN image IS NULL
                THEN width IS NULL AND height IS NULL AND size_bytes IS NULL AND sha256 IS NULL
                    AND blur_params_version IS NULL
                ELSE camera_id IS NOT NULL AND frame_at IS NOT NULL AND width IS NOT NULL
                    AND height IS NOT NULL AND size_bytes IS NOT NULL AND sha256 IS NOT NULL
                    AND blur_params_version IS NOT NULL
            END
        )
    )
    """,
    """
    CREATE INDEX episode_photo_zone_received
        ON fleet.episode_photo (organization_id, plant_id, zone_id, received_at)
    """,
    "ALTER TABLE fleet.episode_photo ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE fleet.episode_photo FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY organization_isolation ON fleet.episode_photo
        AS PERMISSIVE FOR ALL TO PUBLIC
        USING (organization_id = shared.vigia_current_organization())
        WITH CHECK (organization_id = shared.vigia_current_organization())
    """,
    """
    CREATE POLICY provider_concession_scope ON fleet.episode_photo
        AS RESTRICTIVE FOR ALL TO PUBLIC
        USING (identity.rls_provider_scope_allows(organization_id, plant_id))
        WITH CHECK (identity.rls_provider_scope_allows(organization_id, plant_id))
    """,
    "GRANT SELECT, INSERT ON fleet.episode_photo TO vigia_app",
    "COMMENT ON TABLE fleet.episode_photo IS 'EpisodePhoto (TASK-572, A-112): foto difuminada del"
    " episodio de la sombra o motivo de su ausencia; material de puesta en marcha, fuera del"
    " expediente; vence a los 30 días (1 a 90); seguridad a nivel de fila forzada por"
    " organización y concesión de proveedor'",
    "COMMENT ON COLUMN fleet.episode_photo.canonical_sha256 IS 'SHA-256 de los bytes canónicos"
    " (RFC 8785) de la presentación: idempotencia (BR-GOB-89)'",
    "COMMENT ON COLUMN fleet.episode_photo.image IS 'JPEG difuminado con la marca"
    " vigia_anonymized=1 (A-112); nunca sale en registros, métricas ni trazas'",
)


def upgrade() -> None:
    op.execute("SET LOCAL ROLE vigia_migrate")
    for statement in _STATEMENTS:
        op.execute(statement)
    op.execute("RESET ROLE")


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
