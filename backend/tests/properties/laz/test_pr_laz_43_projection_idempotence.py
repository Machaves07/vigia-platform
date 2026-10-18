"""PR-LAZ-43: la proyección del lazo es idempotente y no depende del orden entre particiones.

business-logic-model.md de U-04 §11 (TASK-305; BR-LAZ-146, 147 y 151). Sobre
``loop_projection_commands`` (registros de una a tres plantas que un expediente real podría
contener):

- **idempotencia**: aplicar dos veces seguidas el mismo registro o evento (el reintento y la
  doble entrega) deja la proyección igual que aplicarlo una vez; reaplicar después el último
  registro de cada sujeto, o cualquier tramo o detección ya aplicados, no cambia nada;
- **conmutatividad entre particiones**: cualquier permutación de la llegada produce la misma
  proyección (el pliegue ordena cada planta por ``chain_sequence``), y cada planta es la misma
  plegada sola que junto a las demás;
- **dentro de una partición manda ``chain_sequence``**: el mismo registro aplicado fuera de orden
  (la clasificación antes que su hallazgo) no se proyecta: es una incoherencia, nunca un estado
  inventado;
- el resumen de la partición, actualizado fila a fila, es el que se recalcula desde cero.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.projection import BASE_TIME, loop_projection_commands, stamp
from vigia_platform.loop.projection.digest import partition_digest
from vigia_platform.loop.projection.fold import (
    PlantProjection,
    ProjectionInconsistent,
    ProjectionInput,
    episode_term,
    finding_term,
    fold,
    subject_id_of,
)

Key = tuple[uuid.UUID, uuid.UUID]


def _states(items: Sequence[ProjectionInput]) -> dict[Key, object]:
    return {key: projection.state() for key, projection in fold(items).items()}


@given(items=loop_projection_commands())
def test_applying_each_record_or_event_twice_in_a_row_changes_nothing(
    items: list[ProjectionInput],
) -> None:
    doubled = [entry for item in items for entry in (item, item)]
    assert _states(doubled) == _states(items)


@given(items=loop_projection_commands(), data=st.data())
def test_reapplying_an_already_applied_record_changes_nothing(
    items: list[ProjectionInput], data: st.DataObject
) -> None:
    projections = fold(items)
    replayable = [
        item
        for item in items
        if item.record_type in ("finding_received", "detection_for_review_received")
    ]
    for projection in projections.values():
        replayable.extend(
            item
            for item in items
            if (item.organization_id, item.plant_id)
            == (projection.organization_id, projection.plant_id)
            and any(row.last_record_id == item.record_id for row in projection.findings.values())
        )
    if not replayable:
        return
    replay = data.draw(st.sampled_from(replayable))
    projection = projections[(replay.organization_id, replay.plant_id)]  # type: ignore[index]
    before = projection.state()
    result = projection.apply(replay)
    assert result.applied is False
    assert projection.state() == before


@given(items=loop_projection_commands(), data=st.data())
def test_any_arrival_order_across_partitions_gives_the_same_projection(
    items: list[ProjectionInput], data: st.DataObject
) -> None:
    shuffled = data.draw(st.permutations(items))
    assert _states(shuffled) == _states(items)


@given(items=loop_projection_commands())
def test_each_partition_folds_the_same_alone_as_with_the_others(
    items: list[ProjectionInput],
) -> None:
    together = _states(items)
    for key, state in together.items():
        alone = [item for item in items if (item.organization_id, item.plant_id) == key]
        assert _states(alone) == {key: state}


@given(items=loop_projection_commands())
def test_partition_digest_is_the_sum_of_its_rows(items: list[ProjectionInput]) -> None:
    for projection in fold(items).values():
        terms = [episode_term(row) for row in projection.episodes.values()]
        terms += [finding_term(row) for row in projection.findings.values()]
        assert projection.projection_digest == partition_digest(terms)
        for row in projection.findings.values():
            assert row.projection_digest == row.computed_digest()


@given(items=loop_projection_commands())
def test_within_a_partition_chain_sequence_rules(items: list[ProjectionInput]) -> None:
    """Un registro de U-04 aplicado antes que el registro que creó su sujeto no se proyecta."""
    for item in items:
        subject = subject_id_of(item)
        if item.record_type in ("finding_received", "detection_for_review_received") or not subject:
            continue
        projection = PlantProjection(item.organization_id, item.plant_id)  # type: ignore[arg-type]
        with pytest.raises(ProjectionInconsistent):
            projection.apply(item)
        return


def test_a_shuffled_sequence_number_reorders_the_fold() -> None:
    """Ejemplo: el orden de ``chain_sequence`` manda sobre el orden de la lista."""
    items = _example()
    renumbered = [
        dataclasses.replace(item, chain_sequence=len(items) - index)
        for index, item in enumerate(items)
    ]
    with pytest.raises(ProjectionInconsistent):
        fold(renumbered)
    assert _states(list(reversed(items))) == _states(items)


def test_an_event_already_processed_is_ignored_by_its_event_id() -> None:
    """BR-LAZ-151: la proyección ignora el evento repetido por ``event_id`` (BR-NUC-76)."""
    finding, classification = _example()
    event_id = uuid.UUID(int=99)
    projection = PlantProjection(finding.organization_id, finding.plant_id)  # type: ignore[arg-type]
    assert projection.apply(dataclasses.replace(finding, event_id=event_id)).applied
    before = projection.state()
    replayed = dataclasses.replace(classification, event_id=event_id)
    assert projection.apply(replayed).applied is False
    assert projection.state() == before


def _example() -> list[ProjectionInput]:
    """Un hallazgo y su clasificación, en orden de cadena."""
    organization, plant, zone, episode = (uuid.UUID(int=n) for n in (1, 2, 3, 4))
    finding = {
        "plant_id": str(plant),
        "zone_id": str(zone),
        "family": "coexistence",
        "tier": "tier_1",
        "standard": {"standard_id": str(uuid.UUID(int=5)), "version": 1},
        "node_time": {"started_at": stamp(BASE_TIME), "ended_at": stamp(BASE_TIME)},
        "observability_state": "observable",
        "max_confidence": 0.9,
        "episode_duration_ms": 0,
        "condition_duration_ms": 0,
        "cameras": [],
        "episode": {"episode_id": str(episode), "segment_index": 0, "continues": False},
    }
    classification = {
        "subject_kind": "finding",
        "subject_id": str(episode),
        "outcome": "confirmed",
        "reason_category": "real_coexistence",
        "reinforced_reason": False,
        "classified_at": stamp(BASE_TIME),
    }
    return [
        ProjectionInput(
            uuid.UUID(int=10), organization, plant, 1, "finding_received", BASE_TIME, finding
        ),
        ProjectionInput(
            uuid.UUID(int=11), organization, plant, 2, "classification", BASE_TIME, classification
        ),
    ]
