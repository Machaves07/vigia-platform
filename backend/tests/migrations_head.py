"""Cabeza de la cadena de ``migrations/versions/`` para las pruebas que migran una base.

El lint MIG005 garantiza que el ``NNNN`` de cada revisión es su posición en la cadena, así que la
mayor es la cabeza y su número es lo que devuelve ``shared.vigia_schema_version()``. Así las
pruebas de ``nuc_0001`` no se rompen con cada migración nueva.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
_REVISION_FILE = re.compile(r"^((?:nuc|gob|laz)_([0-9]{4}))_.*\.py$")


def _head() -> tuple[str, int]:
    revisions = [
        (int(match[2]), match[1])
        for path in VERSIONS.iterdir()
        if (match := _REVISION_FILE.match(path.name))
    ]
    number, revision = max(revisions)
    return revision, number


HEAD_REVISION, HEAD_VERSION = _head()
