"""${message}

Revisión ${up_revision}, sobre ${down_revision | comma,n}.

Solo hacia adelante (NFR-NUC-14, PAT-NUC-MAN-09): compatible con la imagen anterior (añadir antes
de usar, nunca renombrar en la misma etiqueta); nada de DROP TABLE, TRUNCATE ni DELETE sobre
tablas de solo anexar (``migrations/append_only.py``). Nombres de tabla siempre calificados con su
esquema. ``tools/lint_migrations.py`` lo comprueba.
"""

from __future__ import annotations

from alembic import op
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    raise NotImplementedError(
        "Solo hacia adelante (NFR-NUC-14): se revierte redesplegando la imagen anterior."
    )
