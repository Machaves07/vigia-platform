"""PR-LAZ-29: la línea de tiempo del paquete nunca omite un tramo y nada afirma que no ocurrió
nada (TASK-318; PAT-LAZ-REN-04; BR-LAZ-93, 96 y 97; H-38).

**Propiedad** (``exported_packages``): en el paquete real, leído de vuelta de sus volúmenes:

- la línea de tiempo de **cada** zona pedida es una partición exacta del periodo (contigua, sin
  solapes ni tramos vacíos) y su resumen suma exactamente la duración del periodo; el resumen del
  manifiesto es el mismo;
- cada tramo con acta de walk-test no vigente y cada revocación de la compuerta de uso que se
  solapan con el periodo están **declarados**: en ``regression_periods`` y ``revoked_gates`` del
  manifiesto (con la causa y el motivo de U-03) y como anotación en la línea de tiempo; con alguna
  revocación, el aviso fijo de revocación;
- el manifiesto lleva el aviso fijo de cobertura (``no se observó``) y **toda** frase propia del
  paquete es una de la lista cerrada ``FIXED_TEXTS_ES``, comparada por **igualdad** (nunca por
  subcadena, VIG-194). Los únicos textos libres son datos citados de otras unidades, en rutas
  cerradas: el nombre de la organización, la planta y las zonas, el título y el texto del
  estándar, el motivo de una revocación y el nombre del solicitante.

Solo datos generados.
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timedelta
from typing import Any, Final

from hypothesis import HealthCheck, given, settings

from tests.export_contents_support import compose_world, prose
from tests.properties.laz.strategies.export import ExportWorld, exported_packages
from vigia_platform.accreditation.manifest.model import (
    COVERAGE_NOTICE_ES,
    FIXED_TEXTS_ES,
    REVOCATION_NOTICE_ES,
)
from vigia_platform.shared.signing.keys import format_timestamp

_MS: Final = timedelta(milliseconds=1)

DATA_PATHS: Final = (
    re.compile(r"/organization/(name|code)"),
    re.compile(r"/plant/(name|code|country|timezone)"),
    re.compile(r"/zones/\d+/(name|code)"),
    re.compile(r"/zones/\d+/gate_intervals/\d+/reason_es"),
    re.compile(r"/revoked_gates/\d+/reason_es"),
    re.compile(r"/standards_cited/\d+/(title_es|declared_text)"),
    re.compile(r"/generated_by/display_name"),
)
"""Rutas cerradas de los textos que el manifiesto **cita** de otras unidades (no son suyos)."""


def _is_prose(path: str, value: str) -> bool:
    """Una frase en español del propio paquete: la clave termina en ``_es`` y no es un dato."""
    key = path.rsplit("/", 1)[-1]
    return key.endswith("_es") and not any(p.fullmatch(path) for p in DATA_PATHS)


def _overlaps(start: datetime, end: datetime | None, period: tuple[datetime, datetime]) -> bool:
    return start < period[1] and (end is None or end > period[0])


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(world=exported_packages())
def test_pr_laz_29_the_timeline_never_omits_a_stretch(world: ExportWorld) -> None:
    job = world.job
    composed = compose_world(world)
    period_ms = (job.period_to - job.period_from) // _MS
    summaries = {item["zone_id"]: item for item in composed.manifest["coverage_summary"]}
    assert set(summaries) == {str(zone) for zone in job.zone_ids}
    for zone in job.zone_ids:
        coverage = composed.json(f"coverage/zone-{zone}.json")
        intervals = coverage["intervals"]
        assert intervals, "un periodo no vacío siempre tiene tramos"
        assert intervals[0]["from"] == format_timestamp(job.period_from)
        assert intervals[-1]["to"] == format_timestamp(job.period_to)
        for before, after in itertools.pairwise(intervals):
            assert before["to"] == after["from"], "contigua y sin solapes"
        assert all(interval["duration_ms"] > 0 for interval in intervals)
        assert sum(interval["duration_ms"] for interval in intervals) == period_ms
        assert coverage["summary"]["total_ms"] == period_ms
        assert summaries[str(zone)]["total_ms"] == period_ms
        assert summaries[str(zone)]["interval_count"] == len(intervals)


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(world=exported_packages())
def test_pr_laz_29_regressions_and_revocations_are_declared(world: ExportWorld) -> None:
    job = world.job
    period = (job.period_from, job.period_to)
    composed = compose_world(world)
    manifest = composed.manifest
    declared_regressions = {
        (item["zone_id"], item["from"], item["cause"]) for item in manifest["regression_periods"]
    }
    declared_revocations = {(item["zone_id"], item["since"]) for item in manifest["revoked_gates"]}
    any_revoked = False
    for zone in job.zone_ids:
        notes = composed.json(f"coverage/zone-{zone}.json")["annotations"]
        kinds = {note["kind"] for note in notes}
        for marked, cleared, cause in world.regression[zone]:
            if _overlaps(marked, cleared, period):
                assert (str(zone), format_timestamp(marked), cause) in declared_regressions
                assert "walk_test_not_current" in kinds
        changes = world.usage_changes[zone]
        for index, (at, status, reason) in enumerate(changes):
            until = changes[index + 1][0] if index + 1 < len(changes) else None
            if status.value == "revoked" and _overlaps(at, until, period):
                any_revoked = True
                assert (str(zone), format_timestamp(at)) in declared_revocations
                assert "usage_gate_revoked" in kinds
                assert reason in {item["reason_es"] for item in manifest["revoked_gates"]}
    assert (manifest.get("revocation_notice_es") == REVOCATION_NOTICE_ES) is any_revoked
    # Una zona revocada se exporta con la advertencia, nunca se omite.
    assert {zone["zone_id"] for zone in manifest["zones"]} == {str(z) for z in job.zone_ids}


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(world=exported_packages())
def test_pr_laz_29_every_phrase_of_the_package_is_a_closed_fixed_text(world: ExportWorld) -> None:
    composed = compose_world(world)
    manifest = composed.manifest
    assert manifest["coverage_notice_es"] == COVERAGE_NOTICE_ES
    phrases = [(path, value) for path, value in prose(manifest) if _is_prose(path, value)]
    assert phrases, "el manifiesto lleva sus avisos"
    for path, value in phrases:
        assert value in FIXED_TEXTS_ES, path
    # Los artefactos de cobertura y de hallazgos no escriben ninguna frase propia.
    for path in composed.artifacts:
        if path.startswith(("coverage/", "checkpoints/", "keys/")):
            document: Any = composed.json(path)
            assert not [p for p, v in prose(document) if _is_prose(p, v)], path


def test_the_closed_list_has_no_duplicates_and_every_notice_is_in_it() -> None:
    assert len(set(FIXED_TEXTS_ES)) == len(FIXED_TEXTS_ES)
    assert COVERAGE_NOTICE_ES in FIXED_TEXTS_ES
    assert REVOCATION_NOTICE_ES in FIXED_TEXTS_ES


def test_a_phrase_outside_the_list_is_caught_by_equality_not_by_substring() -> None:
    """La comparación es por igualdad: un aviso que **contiene** un texto fijo y añade algo no
    pasa (una subcadena como «no se observó» no basta)."""
    tampered = COVERAGE_NOTICE_ES + " En esos tramos no ocurrió nada."
    assert tampered not in FIXED_TEXTS_ES
    assert _is_prose("/coverage_notice_es", tampered)
    assert not _is_prose("/zones/0/name", "Zona")
