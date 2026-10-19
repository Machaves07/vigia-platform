"""Presupuesto de llamadas a U-03 de la exportación (TASK-318; NFR-LAZ-03; PAT-LAZ-REN-05).

Con ``InstrumentedPorts`` (el doble instrumentado de TASK-341) envolviendo los puertos del mundo
generado, el compositor real llama a U-03 **solo** con las formas por lote y por rango
(``single_occupancy_many``, ``standards_at_many``, ``gate_history`` y ``assignment_history``),
**ninguna** dentro de un bucle sobre elementos (el recuento no crece con los hallazgos: el mismo
alcance con 1 y con 25 hallazgos por zona da el mismo número de llamadas) y, para el **alcance
máximo** (20 zonas, 366 días, 10 estándares distintos por zona: 200 referencias), **42 ≤ 45**.
Con 201 referencias, ``standards_at_many`` se parte en dos lotes de 200 como mucho (43).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import timedelta
from typing import Final

import pytest

from tests.export_stream_support import MemoryExportStore, RecordingSink
from tests.laz_support import InstrumentedPorts, RealPorts
from tests.properties.laz.strategies.export import BASE, WorldPorts, job_world, world_context
from vigia_platform.accreditation.export.composer_port import ExportJob
from vigia_platform.accreditation.export.contents import ExportComposer, ExportPorts
from vigia_platform.accreditation.export.stream import ExportStream
from vigia_platform.catalog.domain.ports import MAX_STANDARD_REFS
from vigia_platform.loop.domain.enums import PackageForm

BUDGET: Final = 45
"""``[estimación propia]`` de PAT-LAZ-REN-05: «del orden de 45 llamadas» al alcance máximo."""
U03_BATCH: Final = frozenset(
    {"single_occupancy_many", "standards_at_many", "gate_history", "assignment_history"}
)
U02: Final = frozenset({"linea_de_tiempo"})
"""``CoveragePort`` es de U-02 (no cuenta para U-03): también va por tramos, no por elemento."""


def _job(zones: int, days: int) -> ExportJob:
    organization = uuid.uuid5(uuid.UUID(int=0x45), f"org{zones}{days}")
    return ExportJob(
        export_id=uuid.uuid5(organization, "export"),
        organization_id=organization,
        plant_id=uuid.uuid5(organization, "plant"),
        zone_ids=tuple(uuid.uuid5(organization, f"zone{index}") for index in range(zones)),
        period_from=BASE,
        period_to=BASE + timedelta(days=days),
        package_form=PackageForm.WITHOUT_CLIPS,
        requested_by={"user_id": str(uuid.UUID(int=1)), "display_name": "P", "role_in_use": None},
        correlation_id=uuid.UUID(int=2),
        attempt=1,
        generated_at=BASE + timedelta(days=days, hours=1),
        checkpoint_record_ids=(uuid.uuid5(organization, "checkpoint"),),
        context=world_context(organization),
    )


def _calls(job: ExportJob, findings: int, standards: int) -> Counter[str]:
    world = job_world(job, findings_per_zone=findings, standards_per_zone=standards)
    doubles = WorldPorts(world)
    counted = InstrumentedPorts(
        organization_id=job.organization_id,
        plant_id=job.plant_id,
        wrapped=RealPorts(catalog=doubles, gates=doubles, coverage=doubles, fleet=doubles),
    )
    ports = ExportPorts(
        reader=doubles,
        catalog=counted,
        gates=counted,
        fleet=counted,
        coverage=counted,
        markers=doubles,
        checkpoints=doubles,
        places=doubles,
        blobs=doubles,
        episodes=doubles,
        signer=doubles,
    )
    composer = ExportComposer(stream=ExportStream(store=MemoryExportStore()), ports=ports)
    package = asyncio.run(composer.compose(job, RecordingSink()))
    assert package.finding_counts["finding_count"] == findings * len(job.zone_ids)
    return counted.calls


def _u03(calls: Counter[str]) -> Counter[str]:
    return Counter({name: count for name, count in calls.items() if name not in U02})


def test_the_maximum_scope_stays_within_the_budget_with_batch_forms_only() -> None:
    job = _job(zones=20, days=366)
    calls = _calls(job, findings=12, standards=10)
    u03 = _u03(calls)
    assert set(u03) == U03_BATCH, calls
    assert u03 == Counter(
        single_occupancy_many=1, standards_at_many=1, gate_history=20, assignment_history=20
    )
    assert sum(u03.values()) == 42 <= BUDGET
    # La cobertura (U-02) va por tramos de 31 días: 12 por zona, nunca por hallazgo.
    assert calls["linea_de_tiempo"] == 20 * 12


@pytest.mark.parametrize("zones", [1, 7, 20])
def test_the_count_does_not_grow_with_the_findings(zones: int) -> None:
    """Ninguna llamada dentro de un bucle sobre elementos: 1 o 25 hallazgos por zona, igual."""
    job = _job(zones=zones, days=40)
    few, many = _calls(job, findings=1, standards=1), _calls(job, findings=25, standards=1)
    assert _u03(few) == _u03(many)
    assert few["linea_de_tiempo"] == many["linea_de_tiempo"] == 2 * zones


def test_more_than_200_references_go_in_batches_of_200() -> None:
    job = _job(zones=20, days=31)
    calls = _calls(job, findings=11, standards=11)  # 220 referencias distintas
    assert calls["standards_at_many"] == 2
    assert sum(_u03(calls).values()) == 43 <= BUDGET
    assert MAX_STANDARD_REFS == 200
