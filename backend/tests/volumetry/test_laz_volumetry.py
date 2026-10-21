"""Volumetría de U-04 (TASK-353; NFR-LAZ-06 y 09; A-65): la generación y las mediciones de
``tests/volumetry/laz_scale.py`` con los perfiles de ``tests/load/laz_profiles.py``.

- ``ci`` (2 zonas, 7 días y 20 000 avisos de un destinatario): la prueba de integración del
  generador y de lo funcional.
- ``nightly`` (30 zonas, un año a 77 episodios por zona y día, extrapolado a 300 zonas, y **un
  millón** de avisos): solo con ``--hypothesis-profile=nightly``. El trabajo ``volumetria`` de
  ``nightly.yml`` lo corre hoy por ``generate_scale_data.py --part all`` (que incluye ``laz``); la
  edición del flujo para llamar a esta prueba es de TASK-333.

**Bloquea lo funcional**: la paginación por clave de ``GET /notifications`` recorre todos los
avisos sin perder ni repetir ninguno, en orden, antes y después del archivado; el archivado de lo
leído a los 60 días no borra ninguna fila (recuento antes y después) y archiva exactamente lo leído
hace más de 60 días; cada tabla de NFR-LAZ-06 tiene filas y bytes medidos. **Tendencia**: las
cifras de ``loop_rows_per_day`` y ``loop_bytes_per_table`` frente a la tabla de NFR-LAZ-06 van al
informe ``volumetry-laz-<perfil>.json`` y a la salida, y no deciden el resultado.

Solo datos generados.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.load.laz_profiles import NFR_LAZ_06, VOLUMETRY_PROFILES, VolumetryProfile
from tests.load.report import report_directory
from tests.volumetry.laz_scale import MEASURED_TABLES, report_json, run_laz

pytestmark = pytest.mark.integration


def _check(report: dict[str, Any], profile: VolumetryProfile) -> None:
    checks = report["checks"]
    assert all(checks.values()), {name: ok for name, ok in checks.items() if not ok}
    pagination = report["pagination"]
    assert pagination["seen_before_archive"] == pagination["rows"] == profile.notifications
    archive = report["archive"]
    assert archive["rows_after"] == archive["rows_before"]
    assert 0 < archive["archived"] < archive["rows_before"]
    assert pagination["seen_after_archive_default"] == archive["rows_before"] - archive["archived"]
    # Las dos métricas, por tabla, y la comparación con cada fila de la tabla de NFR-LAZ-06.
    assert set(report["loop_rows_per_day"]) == set(report["loop_bytes_per_table"])
    assert set(report["loop_rows_per_day"]) == set(MEASURED_TABLES)
    assert all(report["loop_rows_per_day"][table] > 0 for table in MEASURED_TABLES)
    assert all(report["loop_bytes_per_table"][table] > 0 for table in MEASURED_TABLES)
    groups = report["nfr_laz_06"]["groups"]
    assert [group["group"] for group in groups] == [group.label for group in NFR_LAZ_06]


def _run(
    postgres_endpoint: PostgresEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
    profile: VolumetryProfile,
) -> None:
    report = run_laz(postgres_endpoint, profile)
    directory = report_directory(tmp_path_factory.getbasetemp())
    path = directory / f"volumetry-laz-{profile.name}.json"
    path.write_text(report_json(report), encoding="utf-8")
    print(f"\nNFR-LAZ-06 y 09 ({profile.name}): {report_json(report)}\ninforme: {path}")
    _check(report, profile)


def test_laz_volumetry_ci(
    postgres_endpoint: PostgresEndpoint, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _run(postgres_endpoint, tmp_path_factory, VOLUMETRY_PROFILES["ci"])


@pytest.mark.nightly
def test_laz_volumetry_nightly_one_year_and_a_million_notifications(
    postgres_endpoint: PostgresEndpoint, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _run(postgres_endpoint, tmp_path_factory, VOLUMETRY_PROFILES["nightly"])
