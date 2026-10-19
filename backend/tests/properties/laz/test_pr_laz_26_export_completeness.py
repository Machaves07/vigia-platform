"""PR-LAZ-26: el paquete lleva **exactamente** los hallazgos del alcance (TASK-318; BR-LAZ-88, 89
y 94; C-PLA-29).

**Propiedad** (``exported_packages``: dos organizaciones con su planta y de una a tres zonas,
expedientes del lazo generados con la máquina de §4.1, un periodo y un subconjunto de zonas de la
organización ``a``): el paquete real, leído de vuelta de sus volúmenes ZIP, contiene exactamente
los hallazgos que da el **oráculo por fuerza bruta** (plegar todo el expediente de las dos
organizaciones con ``fold`` y quedarse con los de la organización, las zonas y el periodo
pedidos), con su estado; **todos** los estados, también descartados, suspendidos e incerrables, y
los de evidencia marcada; ninguno de la organización ``b`` ni de una zona no pedida. Los conteos
del manifiesto coinciden con el contenido y suman ``finding_count``.

Además, ejemplos de los bordes: un periodo sin hallazgos (el paquete existe y lo dice con ceros),
el periodo semiabierto (un hallazgo justo en ``period_to`` no entra; uno justo en
``period_from``, sí), y ``evidence_flagged`` solo con una verificación terminada antes de
``generated_at`` (BR-LAZ-94).

Solo datos generados.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings

from tests.export_contents_support import compose_world, oracle_findings
from tests.properties.laz.strategies.export import ExportWorld, exported_packages
from vigia_platform.ledger.application.evidence_read import MarkerResult
from vigia_platform.loop.domain.enums import FindingLifecycleState

S = FindingLifecycleState

_SETTINGS: Any = settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


def _package_findings(composed: Any) -> dict[uuid.UUID, dict[str, Any]]:
    found: dict[uuid.UUID, dict[str, Any]] = {}
    for path in composed.artifacts:
        if path.startswith("findings/"):
            for finding in composed.json(path)["findings"]:
                subject = uuid.UUID(finding["subject_id"])
                assert subject not in found, "un hallazgo aparece dos veces"
                found[subject] = finding
    return found


@_SETTINGS
@given(world=exported_packages())
def test_pr_laz_26_the_package_holds_exactly_the_findings_of_the_scope(world: ExportWorld) -> None:
    composed = compose_world(world)
    expected = oracle_findings(world, world.job)
    found = _package_findings(composed)
    assert set(found) == set(expected)
    for subject, row in expected.items():
        finding = found[subject]
        assert finding["lifecycle_state"] == row.lifecycle_state
        assert uuid.UUID(finding["zone_id"]) in set(world.job.zone_ids)
        assert row.organization_id == world.a
    # Ninguno de la otra organización ni de otra zona (por construcción del oráculo y aquí).
    others = {
        record.record_id
        for record in world.records
        if record.organization_id == world.b or record.scope_zone_id not in set(world.job.zone_ids)
    }
    for finding in found.values():
        assert not {uuid.UUID(r["record_id"]) for r in finding["records"]} & others
    # Los conteos del manifiesto coinciden con el contenido.
    manifest = composed.manifest
    states = [finding["lifecycle_state"] for finding in found.values()]
    assert manifest["finding_count"] == len(found)
    assert manifest["state_counts"] == {state.value: states.count(state.value) for state in S}
    open_states = {S.RECEIVED, S.IN_REVIEW, S.OPEN, S.ACTION_ASSIGNED, S.OVERDUE}
    assert manifest["open_finding_count"] == sum(states.count(s.value) for s in open_states)
    assert manifest["closed_finding_count"] == states.count(S.CLOSED.value)
    assert manifest["unclosable_count"] == states.count(S.UNCLOSABLE.value)
    assert manifest["discarded_count"] == states.count(S.DISCARDED.value)
    assert manifest["suspended_count"] == states.count(S.SUSPENDED_BY_RECLASSIFICATION.value)
    assert (
        manifest["open_finding_count"]
        + manifest["closed_finding_count"]
        + manifest["unclosable_count"]
        + manifest["discarded_count"]
        + manifest["suspended_count"]
        == manifest["finding_count"]
    )
    flagged = [finding for finding in found.values() if finding["evidence_flagged"]]
    assert manifest["evidence_flagged_count"] == len(flagged)
    assert manifest["organization"]["organization_id"] == str(world.a)
    assert [zone["zone_id"] for zone in manifest["zones"]] == sorted(
        str(zone) for zone in world.job.zone_ids
    )
    # El registro export_produced copia esos conteos.
    assert composed.package.finding_counts == {
        "finding_count": manifest["finding_count"],
        "open_finding_count": manifest["open_finding_count"],
        "closed_finding_count": manifest["closed_finding_count"],
        "unclosable_count": manifest["unclosable_count"],
        "discarded_count": manifest["discarded_count"],
    }


@_SETTINGS
@given(world=exported_packages())
def test_pr_laz_26_evidence_flagged_is_included_and_explained(world: ExportWorld) -> None:
    """BR-LAZ-94: un hallazgo con un clip de marca rota (verificada antes de ``generated_at``)
    sigue en el paquete con ``evidence_flagged``; una rotura posterior aún no cuenta."""
    composed = compose_world(world)
    found = _package_findings(composed)
    generated_at = world.job.generated_at
    for subject, finding in found.items():
        evidences = {
            uuid.UUID(evidence)
            for record in finding["records"]
            for evidence in record["evidence_ids"]
        }
        broken = any(
            world.markers[e].result is MarkerResult.BROKEN
            and world.markers[e].marker_verified_at is not None
            and world.markers[e].marker_verified_at < generated_at  # type: ignore[operator]
            for e in evidences
        )
        assert finding["evidence_flagged"] is broken, subject
    assert composed.manifest["evidence_flagged_notice_es"]


def _with_period(world: ExportWorld, **changes: Any) -> ExportWorld:
    return dataclasses.replace(world, job=world.job_with(**changes))


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(world=exported_packages())
def test_the_period_is_half_open_on_the_anchor_record(world: ExportWorld) -> None:
    """Un hallazgo cuyo primer registro de U-03 cae justo en ``period_from`` entra; justo en
    ``period_to``, no."""
    anchors = sorted(
        (
            record
            for record in world.records
            if record.organization_id == world.a
            and record.record_type in ("finding_received", "detection_for_review_received")
            and record.scope_zone_id in set(world.job.zone_ids)
        ),
        key=lambda record: record.chain_sequence,
    )
    if not anchors:
        return
    pivot = anchors[len(anchors) // 2].received_at
    starting = _with_period(world, period_from=pivot, period_to=pivot + timedelta(days=60))
    ending = _with_period(world, period_from=pivot - timedelta(days=60), period_to=pivot)
    for candidate in (starting, ending):
        expected = oracle_findings(candidate, candidate.job)
        assert set(_package_findings(compose_world(candidate))) == set(expected)


@settings(max_examples=5, deadline=None)
@given(world=exported_packages(max_steps=0))
def test_a_period_without_findings_is_still_a_package_with_zero_counts(world: ExportWorld) -> None:
    composed = compose_world(world)
    manifest = composed.manifest
    assert manifest["finding_count"] == 0
    assert all(value == 0 for value in manifest["state_counts"].values())
    assert manifest["coverage_summary"], "la cobertura del periodo va igual"
