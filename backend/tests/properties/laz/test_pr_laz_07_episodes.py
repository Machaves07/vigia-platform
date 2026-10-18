"""PR-LAZ-07: el episodio es la unidad de gestión (BR-LAZ-13 a 17 y 155; TASK-305).

Sobre ``episodes_with_segments`` (tramos de un episodio del kit de U-01, con huecos y en
cualquier orden de llegada):

- **un elemento por episodio**: una sola fila de ``FindingState`` y una de ``Episode``, sea cual
  sea el número de tramos;
- **tier máximo** de los tramos presentes; **estándar del primer tramo** que llegó, nunca
  reevaluado con la versión de uno posterior (H-16);
- **duración = suma** de los tramos presentes (``episode_duration_ms`` y
  ``condition_duration_ms``), y los **tramos faltantes declarados** (BR-LAZ-16 y 155);
- **tramo tardío**: si el hallazgo ya está clasificado cuando llega un tramo, el tramo se adjunta,
  el lazo **no se reabre**, no se crea elemento y el pliegue pide el registro
  ``late_segment_attached``; aplicarlo (T21) no cambia el estado.
"""

from __future__ import annotations

import dataclasses
import uuid

from hypothesis import given
from hypothesis import strategies as st

from tests.properties.laz.strategies.projection import (
    EpisodeScenario,
    episodes_with_segments,
    stamp,
)
from vigia_platform.loop.domain.enums import FindingLifecycleState
from vigia_platform.loop.projection.fold import PlantProjection, ProjectionInput, fold

S = FindingLifecycleState


def _project(scenario: EpisodeScenario) -> PlantProjection:
    (projection,) = fold(scenario.inputs).values()
    return projection


@given(scenario=episodes_with_segments())
def test_one_element_per_episode_with_max_tier_and_first_standard(
    scenario: EpisodeScenario,
) -> None:
    projection = _project(scenario)
    assert list(projection.episodes) == [scenario.episode_id]
    assert list(projection.findings) == [scenario.episode_id]
    episode = projection.episodes[scenario.episode_id]
    finding = projection.findings[scenario.episode_id]
    tiers = {segment.tier for segment in scenario.segments}
    expected_tier = "tier_1" if "tier_1" in tiers else "tier_2"
    assert episode.tier == finding.tier == expected_tier
    first = scenario.segments[0]
    assert episode.standard_version == finding.standard_version == first.standard_version
    assert finding.anchor_record_id == finding.finding_state_id == first.record_id
    assert finding.subject_kind == "finding"
    assert finding.lifecycle_state == S.RECEIVED


@given(scenario=episodes_with_segments())
def test_duration_is_the_sum_of_present_segments_and_gaps_are_declared(
    scenario: EpisodeScenario,
) -> None:
    episode = _project(scenario).episodes[scenario.episode_id]
    finding = _project(scenario).findings[scenario.episode_id]
    assert episode.total_episode_duration_ms == sum(
        segment.episode_duration_ms for segment in scenario.segments
    )
    assert episode.total_condition_duration_ms == sum(
        segment.condition_duration_ms for segment in scenario.segments
    )
    assert episode.segment_count == len(scenario.segments)
    assert episode.expected_segment_count == scenario.expected_count
    assert episode.missing_segment_indexes == scenario.missing
    assert finding.missing_segments_declared is bool(scenario.missing)
    by_index = sorted(scenario.segments, key=lambda segment: segment.segment_index)
    assert episode.segment_record_ids == tuple(segment.record_id for segment in by_index)
    clip_segments = {clip["segment_index"] for clip in episode.clip_references}
    assert clip_segments == {segment.segment_index for segment in scenario.segments}


def _classification(
    scenario: EpisodeScenario, sequence: int, received_at: object
) -> ProjectionInput:
    document = {
        "subject_kind": "finding",
        "subject_id": str(scenario.episode_id),
        "outcome": "confirmed",
        "reason_category": "real_coexistence",
        "reinforced_reason": False,
        "classified_at": stamp(received_at),  # type: ignore[arg-type]
    }
    return ProjectionInput(
        record_id=uuid.uuid5(scenario.episode_id, "classification"),
        organization_id=scenario.organization_id,
        plant_id=scenario.plant_id,
        chain_sequence=sequence,
        record_type="classification",
        received_at=received_at,  # type: ignore[arg-type]
        document=document,
    )


@given(scenario=episodes_with_segments(), data=st.data())
def test_a_late_segment_never_reopens_nor_creates_an_element(
    scenario: EpisodeScenario, data: st.DataObject
) -> None:
    if len(scenario.inputs) < 2:
        return
    cut = data.draw(st.integers(1, len(scenario.inputs) - 1))
    before, after = scenario.inputs[:cut], scenario.inputs[cut:]
    projection = PlantProjection(scenario.organization_id, scenario.plant_id)
    for item in before:
        projection.apply(item)
    classified_at = before[-1].received_at
    projection.apply(_classification(scenario, cut, classified_at))
    assert projection.findings[scenario.episode_id].state is S.OPEN
    sequence = cut + 1
    for item in after:
        result = projection.apply(dataclasses.replace(item, chain_sequence=sequence))
        sequence += 1
        assert result.late_segment is not None
        assert result.late_segment.segment_record_id == item.record_id
        assert result.late_segment.episode_state_at_arrival is S.OPEN
        assert projection.findings[scenario.episode_id].state is S.OPEN
        assert list(projection.findings) == [scenario.episode_id]
        notice = result.late_segment
        late = ProjectionInput(
            record_id=uuid.uuid5(item.record_id, "late"),
            organization_id=scenario.organization_id,
            plant_id=scenario.plant_id,
            chain_sequence=sequence,
            record_type="late_segment_attached",
            received_at=item.received_at,
            document={
                "segment_record_id": str(notice.segment_record_id),
                "episode_id": str(notice.episode_id),
                "segment_index": notice.segment_index,
                "received_at": stamp(notice.received_at),
                "episode_state_at_arrival": notice.episode_state_at_arrival.value,
                "reopened_loop": False,
                "delay_ms": notice.delay_ms,
            },
        )
        sequence += 1
        attached = projection.apply(late)
        assert attached.transition_id == "T21"
        assert projection.findings[scenario.episode_id].state is S.OPEN
        assert projection.findings[scenario.episode_id].classification_count == 1
    episode = projection.episodes[scenario.episode_id]
    assert episode.segment_count == len(scenario.segments)
    assert episode.total_episode_duration_ms == sum(
        s.episode_duration_ms for s in scenario.segments
    )
