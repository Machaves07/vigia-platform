"""PR-LAZ-46 (TASK-323; BR-LAZ-134 a 136; H-33): la adopción se cuenta por zona y día, sin
atribución a usuario, y la alerta sale **exactamente** con ``adoption_silence_days`` días
consecutivos sin actividad en una zona activa, y **nunca** en una zona no activa.

Para cualquier secuencia generada de actividad del lazo (``loop_activity_sequences``: unas zonas,
su día de activación o ninguno, y escrituras de actividad y de otros tipos, cada una con la
persona que la hizo), con la tarea corriendo cada mañana antes de la actividad del día:

- **Oráculo puro** (sin base, muchos ejemplos): la regla de la tarea (``streak.silence_streak`` y
  ``streak.adoption_due``, publicando una vez por día de inicio de racha) alerta en exactamente las
  mañanas que da un conteo independiente hacia atrás de los días completos silenciosos tras la
  activación, con ``silent_days == adoption_silence_days`` en cada una, y en ninguna zona que nunca
  se activó. Correr la regla dos veces cada mañana no añade nada.
- **Contra PostgreSQL 16** (marca ``integration``): la tarea real ``adoption_check`` (dos veces cada
  mañana, ``ProductiveZones`` sobre ``catalog.zone_gate_state``) y el gancho real de las escrituras
  (``write_activity``) dan las mismas alertas que el oráculo, un ``adoption_low`` por alerta con su
  ``zone_id`` y ``silent_days``; el contador tiene una fila por zona y día local con actividad, con
  la cuenta de cada clase y el total (``session_count``), ninguna por los tipos que no son
  actividad, ninguna columna de usuario y ningún identificador de las personas generadas.

Solo datos generados. Un solo reloj (retro 15): la tarea, el gancho y la activación usan el reloj
simulado del mundo; nada se compara con la hora de la base.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings

from tests.adoption_support import COUNTER_COLUMNS, AdoptionStack, adoption_stack
from tests.integration.conftest import PostgresEndpoint
from tests.properties.laz.strategies.adoption import (
    ActivitySequence,
    ZoneActivity,
    loop_activity_sequences,
)
from vigia_platform.accreditation.adoption.counter import LOOP_ACTIVITY_BY_RECORD_TYPE
from vigia_platform.accreditation.adoption.streak import adoption_due, silence_streak

DAY: Final = timedelta(days=1)
BASE: Final = date(2026, 1, 5)
"""Día 0 del oráculo puro (cualquier fecha: la regla solo resta días)."""
DB_EXAMPLES: Final = 5
"""Cada ejemplo con base crea una organización y recorre hasta ocho días de tarea y escrituras; el
perfil ``nightly`` no sube este número."""

type Alert = tuple[int, int]
"""``(índice de la zona, día de la mañana en que salió la alerta)``."""


def oracle(sequence: ActivitySequence) -> set[Alert]:
    """Conteo independiente: en la mañana ``d``, los días completos silenciosos consecutivos hacia
    atrás desde ayer, sin pasar del de la activación; alerta cuando son exactamente el umbral."""
    alerts: set[Alert] = set()
    for index, zone in enumerate(sequence.zones):
        if zone.activated_on is None:
            continue
        activity = sequence.activity_days(zone)
        for morning in range(sequence.days):
            if zone.activated_on >= morning:
                continue  # la activación es a mediodía: esa mañana la zona aún no está activa
            silent, day = 0, morning - 1
            while day > zone.activated_on and day not in activity:
                silent, day = silent + 1, day - 1
            if silent == sequence.threshold:
                alerts.add((index, morning))
    return alerts


def _rule(sequence: ActivitySequence, zone: ZoneActivity, morning: int) -> tuple[date, int] | None:
    """La regla de la tarea para ``zone`` en la mañana ``morning``: ``(inicio, días)`` si alerta."""
    if zone.activated_on is None or zone.activated_on >= morning:
        return None
    past = [day for day in sequence.activity_days(zone) if day < morning]
    streak = silence_streak(
        today=BASE + morning * DAY,
        active_since=BASE + zone.activated_on * DAY,
        last_activity_day=None if not past else BASE + max(past) * DAY,
    )
    if not adoption_due(streak, sequence.threshold):
        return None
    return streak.start, streak.silent_days


@given(sequence=loop_activity_sequences())
def test_pr_laz_46_the_rule_alerts_exactly_at_n_silent_days_in_active_zones(
    sequence: ActivitySequence,
) -> None:
    published: set[tuple[int, date]] = set()
    alerts: set[Alert] = set()
    for morning in range(sequence.days):
        for _run in range(2):  # dos ejecuciones la misma mañana
            for index, zone in enumerate(sequence.zones):
                due = _rule(sequence, zone, morning)
                if due is None or (index, due[0]) in published:
                    continue
                published.add((index, due[0]))
                alerts.add((index, morning))
                assert due[1] == sequence.threshold, (index, morning, due)
    assert alerts == oracle(sequence)
    never_active = {i for i, zone in enumerate(sequence.zones) if zone.activated_on is None}
    assert not {index for index, _ in alerts} & never_active


# --- contra PostgreSQL ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[AdoptionStack]:
    with adoption_stack(postgres_endpoint, "pr_laz_46_adoption") as built:
        yield built


def _expected_counts(sequence: ActivitySequence, zone: ZoneActivity) -> dict[int, Counter[str]]:
    counts: dict[int, Counter[str]] = {}
    for write in zone.writes:
        kind = LOOP_ACTIVITY_BY_RECORD_TYPE.get(write.record_type)
        if kind is not None:
            counts.setdefault(write.day, Counter())[kind.value] += 1
    return counts


def _uuids(row: dict[str, Any]) -> set[uuid.UUID]:
    return {value for value in row.values() if isinstance(value, uuid.UUID)}


@pytest.mark.integration
@settings(
    max_examples=DB_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(sequence=loop_activity_sequences(max_zones=3, max_threshold=3, max_days=8))
def test_pr_laz_46_the_task_and_the_counter_against_postgres(
    stack: AdoptionStack, sequence: ActivitySequence
) -> None:
    organization = stack.organization()
    stack.set_silence_days(organization, sequence.threshold)
    zones = [stack.site_zone(organization)]
    zones += [stack.add_zone(organization) for _ in sequence.zones[1:]]
    index_of = {zone.zone_id: index for index, zone in enumerate(zones)}
    first = stack.next_morning(organization)
    alerts: set[Alert] = set()
    for morning in range(sequence.days):
        stack.to_local(organization, first + morning * DAY, 1)
        for _run in range(2):
            for alert in stack.run_check(organization):
                assert alert.streak.silent_days == sequence.threshold, alert
                alerts.add((index_of[alert.zone_id], morning))
        stack.to_local(organization, first + morning * DAY, 12)
        for zone, plan in zip(zones, sequence.zones, strict=True):
            if plan.activated_on == morning:
                stack.activate(zone)
        stack.to_local(organization, first + morning * DAY, 13)
        for zone, plan in zip(zones, sequence.zones, strict=True):
            for write in plan.writes:
                if write.day == morning:
                    stack.tick()
                    stack.record(zone, write.record_type)

    assert alerts == oracle(sequence)
    events = stack.adoption_events(organization)
    assert len(events) == len(alerts)
    assert {(event["zone_id"], event["silent_days"]) for event in events} <= {
        (str(zone.zone_id), sequence.threshold) for zone in zones
    }
    actors = {write.actor_user_id for plan in sequence.zones for write in plan.writes}
    assert stack.columns("adoption_counter") == COUNTER_COLUMNS
    for zone, plan in zip(zones, sequence.zones, strict=True):
        rows = stack.counter_rows(zone)
        expected = _expected_counts(sequence, plan)
        assert {row["day"]: row["activity_counts"] for row in rows} == {
            first + day * DAY: dict(counts) for day, counts in expected.items()
        }
        for row in rows:
            assert row["session_count"] == sum(row["activity_counts"].values())
            assert not _uuids(row) & actors, "el contador guardó una persona"
            assert _uuids(row) <= {zone.organization_id, zone.plant_id, zone.zone_id}
