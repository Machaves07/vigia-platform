"""Fallo cerrado del compositor real, un doble por puerto (TASK-318; BR-LAZ-91 y 101).

Cada insumo obligatorio que falta termina en ``ExportInputUnavailable`` con su código **antes de
abrir ninguna subida**: el almacén en memoria no ve ni una subida por partes ni un objeto. Los
casos finos que la prueba de integración (``tests/integration/test_export_contents.py``) no
cubre: una clave pública publicada que no es la de la firma de un punto de control, un punto de
control fijado que no aparece, un estándar que en el instante del hecho tiene **otra** versión
que la citada y una zona sin compuerta legible. Sin la marca unipersonal no se falla: se agrega
(fallo cerrado hacia la privacidad).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import timedelta
from typing import Any

import pytest

from tests.export_stream_support import MemoryExportStore, RecordingSink
from tests.properties.laz.strategies.export import (
    BASE,
    ExportWorld,
    WorldPorts,
    job_world,
    world_context,
)
from vigia_platform.accreditation.export.composer_port import (
    CHECKPOINT_UNAVAILABLE,
    ExportInputUnavailable,
    ExportJob,
)
from vigia_platform.accreditation.export.contents import ExportComposer
from vigia_platform.accreditation.export.stream import ExportStream
from vigia_platform.loop.domain.enums import PackageForm


def _job() -> ExportJob:
    organization = uuid.uuid4()
    return ExportJob(
        export_id=uuid.uuid4(),
        organization_id=organization,
        plant_id=uuid.uuid4(),
        zone_ids=(uuid.uuid4(), uuid.uuid4()),
        period_from=BASE,
        period_to=BASE + timedelta(days=45),
        package_form=PackageForm.WITH_CLIPS,
        requested_by={"user_id": str(uuid.uuid4()), "display_name": "P", "role_in_use": None},
        correlation_id=uuid.uuid4(),
        attempt=1,
        generated_at=BASE + timedelta(days=46),
        checkpoint_record_ids=(uuid.uuid4(), uuid.uuid4()),
        context=world_context(organization),
    )


def _compose(world: ExportWorld, ports: WorldPorts | None = None) -> tuple[Any, MemoryExportStore]:
    store = MemoryExportStore()
    composer = ExportComposer(
        stream=ExportStream(store=store), ports=(ports or WorldPorts(world)).ports()
    )
    return asyncio.run(composer.compose(world.job, RecordingSink())), store


def _fails_closed(world: ExportWorld, code: str, ports: WorldPorts | None = None) -> None:
    store = MemoryExportStore()
    composer = ExportComposer(
        stream=ExportStream(store=store), ports=(ports or WorldPorts(world)).ports()
    )
    with pytest.raises(ExportInputUnavailable) as failure:
        asyncio.run(composer.compose(world.job, RecordingSink()))
    assert failure.value.failure_code == code
    assert store.uploads == {} and store.objects == {} and store.aborted == []


def test_the_world_composes_when_nothing_is_missing() -> None:
    package, store = _compose(job_world(_job(), findings_per_zone=2))
    assert package.finding_counts["finding_count"] == 4
    assert store.uploads == {} and store.objects


def test_a_checkpoint_signed_with_an_unpublished_key_fails_closed() -> None:
    world = job_world(_job())
    first = world.checkpoints[world.a][0]
    world.checkpoints[world.a][0] = dataclasses.replace(
        first, content=dataclasses.replace(first.content, key_id="checkpoint-unknown")
    )
    _fails_closed(world, CHECKPOINT_UNAVAILABLE)


def test_no_published_key_fails_closed() -> None:
    world = job_world(_job())
    _fails_closed(world, CHECKPOINT_UNAVAILABLE, WorldPorts(world, fail=frozenset({"keys"})))


def test_a_fixed_checkpoint_that_does_not_exist_fails_closed() -> None:
    world = job_world(_job())
    world.checkpoints[world.a].pop()
    _fails_closed(world, CHECKPOINT_UNAVAILABLE)


def test_no_fixed_checkpoint_at_all_fails_closed() -> None:
    world = job_world(dataclasses.replace(_job(), checkpoint_record_ids=()))
    _fails_closed(world, CHECKPOINT_UNAVAILABLE)


def test_another_version_of_the_standard_than_the_cited_one_fails_closed() -> None:
    """Nunca se cita otra versión que la evaluada (BR-LAZ-95): si en el instante del hecho rige
    otra, el texto del estándar citado no está disponible."""
    world = job_world(_job())
    world.standards = {key: version + 1 for key, version in world.standards.items()}
    _fails_closed(world, "loop_standard_text_unavailable")


def test_without_standards_coverage_or_gates_the_export_fails_closed() -> None:
    for fail, code in (
        ("standards", "loop_standard_text_unavailable"),
        ("coverage", "accreditation_coverage_unavailable"),
        ("gates", "loop_gate_state_unavailable"),
    ):
        world = job_world(_job())
        _fails_closed(world, code, WorldPorts(world, fail=frozenset({fail})))


def test_without_the_single_occupancy_mark_the_zones_are_aggregated() -> None:
    """SUP-U03-04: sin respuesta se asume unipersonal con la ventana por defecto."""
    world = job_world(_job())
    package, _ = _compose(world, WorldPorts(world, fail=frozenset({"occupancy"})))
    assert package.manifest["aggregated_zone_ids"] == sorted(str(z) for z in world.job.zone_ids)
    assert all(zone["aggregation_window_minutes"] == 60 for zone in package.manifest["zones"])


def test_without_the_assignment_history_it_is_declared_absent() -> None:
    world = job_world(_job())
    package, _ = _compose(world, WorldPorts(world, fail=frozenset({"assignments"})))
    for zone in package.manifest["zones"]:
        assert zone["node_assignments"] is None
        assert zone["node_assignments_absent_reason_es"]
