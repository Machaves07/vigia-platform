"""La restricción de ``changed_fields`` admite cada ``CatalogChangedField`` (TASK-594; gob_0047).

``catalog.zone_catalog_version.changed_fields`` lleva la restricción
``zone_catalog_version_changed_fields`` con la lista cerrada de valores (``gob_0017``, ampliada por
``gob_0047`` con ``process_context``). Si el enumerado del código crece sin migración, toda
publicación con el valor nuevo cae con ``CheckViolation`` (500). Esta prueba lee la cadena de
migraciones sin base (texto de cada eslabón, como ``tools/lint_migrations.py``) y exige que la
última definición de la restricción liste exactamente los valores del enumerado. Así la mutación
``m_sin_migracion_changed_fields`` (sin ``gob_0047``) se ve también sin Docker; la prueba de
integración ``test_migration_gob_0047_admits_process_context_and_keeps_the_rows`` la comprueba
contra PostgreSQL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from vigia_platform.catalog.domain.enums import CatalogChangedField

VERSIONS: Final = Path(__file__).resolve().parents[3] / "migrations" / "versions"
CONSTRAINT: Final = "zone_catalog_version_changed_fields"
_LIST: Final = re.compile(r"ARRAY\[\s*['\"]*\s*'standards'.*?\]", re.DOTALL)
_VALUE: Final = re.compile(r"'([a-z_]+)'")


def _chain() -> list[Path]:
    """Los eslabones en el orden de la cadena (``NNNN`` es su posición, MIG005)."""
    return sorted(
        VERSIONS.glob("[a-z][a-z][a-z]_[0-9][0-9][0-9][0-9]_*.py"), key=lambda p: p.name[4:8]
    )


def _last_definition() -> tuple[str, set[str]]:
    found: tuple[str, set[str]] | None = None
    for path in _chain():
        source = path.read_text(encoding="utf-8")
        if CONSTRAINT not in source:
            continue
        match = _LIST.search(source)
        assert match is not None, path.name
        found = (path.name, set(_VALUE.findall(match.group(0))))
    assert found is not None
    return found


def test_the_last_constraint_lists_exactly_the_changed_fields_of_the_code() -> None:
    name, values = _last_definition()
    assert values == {field.value for field in CatalogChangedField}, name
    assert name.startswith("gob_0047_")
