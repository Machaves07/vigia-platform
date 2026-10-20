"""``accreditation.adoption``: contador, ``adoption_check`` y SUP-U05-32 (TASK-323; LC-LAZ-29;
BR-LAZ-134 a 136; PAT-LAZ-RES-05; H-33).

Sobre PostgreSQL 16 real como ``vigia_app``, con los servicios reales del lazo (que alimentan el
contador por ``LoopProjectionStore.write_and_apply``), la tarea real en el contexto de iteración
periódica del planificador, ``ProductiveZones`` sobre la proyección de compuertas de U-03 y la
aplicación real para ``GET /accreditation/adoption`` (``adoption_stack``):

- **Registro**: ``adoption_check`` está en ``ACCREDITATION_TASKS`` y en ``U04_TASKS``, diaria y por
  organización, y se registra con ``unit = U-04``.
- **Contador** (BR-LAZ-134 y 135): clasificar, asignar una acción, añadir evidencia y cerrar suman
  por zona y **día local de la planta** (una escritura a las 21:00 de Bogotá cuenta en su día, no
  en el de UTC), sin ninguna columna ni valor de usuario; lo que escribe el nodo (U-03) no cuenta;
  una escritura revertida no deja cuenta; veinte actividades a la vez no pierden ninguna (tabla de
  garantías del PR).
- **Alerta** (BR-LAZ-136): con el umbral de la planta, una zona activa sin actividad no alerta con
  ``N - 1`` días y alerta **una vez** al cumplir ``N`` (con su carga); repetir el mismo día o el
  siguiente no publica otra; una actividad abre otra racha que vuelve a alertar a los ``N`` días;
  el día de la activación no cuenta; una zona no activa (sin compuertas, en puesta en marcha o con
  el uso revocado) nunca alerta; tras una caída, la primera ejecución publica una vez con los días
  acumulados; ``ProductiveZones`` toma la última de las dos aprobaciones.
- **Una sola vez a la vez** (tabla de garantías del PR): dos ejecuciones simultáneas, las dos ya
  dentro de su transacción antes de mirar la marca, dejan una marca y un evento, en tres rondas.
- **Organización**: la tarea solo marca y publica en la de su transacción.
- **Ruta** (SUP-U05-32): planta y zona, actividad por día y clase, días de silencio, umbral,
  ``no-store``; ``not_found`` para lo ajeno o fuera de alcance; ``invalid_request`` para filtros
  inválidos.

Solo datos generados. Un solo reloj (retro 15): todo instante sale del reloj simulado del mundo.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from tests.adoption_support import (
    ALERT_COLUMNS,
    COUNTER_COLUMNS,
    AdoptionStack,
    Zone,
    adoption_stack,
)
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopOrganization
from vigia_platform.accreditation.adoption.task import (
    ADOPTION_CHECK,
    ADOPTION_CHECK_SCHEDULE,
    ProductiveZones,
)
from vigia_platform.accreditation.registration import ACCREDITATION_TASKS
from vigia_platform.loop.registration import U04_TASKS
from vigia_platform.shared.context import ActorUnit, Role, ScopeLevel
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import Schedule, ScheduleKind, TaskIteration
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

DAY: Final = timedelta(days=1)
ROUNDS: Final = 3
CONCURRENT_RECORDS: Final = 20
BARRIER_SECONDS: Final = 60.0
"""Tope de la barrera de la prueba concurrente (no decide el resultado)."""
_NOT_FOUND: Final = (404, "not_found")
_INVALID: Final = (400, "invalid_request")


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[AdoptionStack]:
    with adoption_stack(postgres_endpoint, "adoption_task") as built:
        yield built


@pytest.fixture(autouse=True)
def _release(stack: AdoptionStack) -> Iterator[None]:
    yield
    world = stack.actions.world.stack
    world.run(world.release())


def _error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["code"]


async def _together(stack: AdoptionStack, zone: Zone) -> None:
    await asyncio.gather(
        *(stack.record_async(zone, "classification") for _ in range(CONCURRENT_RECORDS))
    )


async def _two_runs(stack: AdoptionStack, organization: LoopOrganization, check: Any) -> list[Any]:
    return list(
        await asyncio.gather(
            stack.check_async(organization, check), stack.check_async(organization, check)
        )
    )


def _silent_world(stack: AdoptionStack, threshold: int) -> tuple[LoopOrganization, Zone, datetime]:
    """Una organización con su zona activada hoy a las 10:00 locales y el umbral dado."""
    organization = stack.organization()
    stack.set_silence_days(organization, threshold)
    stack.next_morning(organization, hour=10)
    zone = stack.site_zone(organization)
    return organization, zone, stack.activate(zone)


# --- registro -------------------------------------------------------------------------------------


def test_the_task_is_daily_per_organization_in_the_u04_manifest(stack: AdoptionStack) -> None:
    expected = (ADOPTION_CHECK_SCHEDULE, TaskIteration.PER_ORGANIZATION)
    assert ACCREDITATION_TASKS[ADOPTION_CHECK] == expected
    assert U04_TASKS[ADOPTION_CHECK] == expected
    assert Schedule(ScheduleKind.DAILY, 5 * 3600) == ADOPTION_CHECK_SCHEDULE
    task = stack.task
    assert (task.task_name, task.unit, task.iteration, task.schedule) == (
        ADOPTION_CHECK,
        ActorUnit.U04,
        TaskIteration.PER_ORGANIZATION,
        ADOPTION_CHECK_SCHEDULE,
    )


# --- contador -------------------------------------------------------------------------------------


def test_loop_writes_count_by_zone_and_local_day_without_any_user(stack: AdoptionStack) -> None:
    actions = stack.actions
    organization = stack.organization()
    zone = stack.site_zone(organization)
    # 21:00 en Bogotá (UTC-5) ya es el día siguiente en UTC: cuenta el día local de la planta.
    day = stack.next_morning(organization, hour=21)
    coordinator = actions.member(organization)
    assignee = actions.user(organization)
    finding = actions.received(organization)
    assert stack.counter_rows(zone) == [], "lo que escribe el nodo no es actividad de la zona"

    actions.classify(finding)
    response = actions.assign(coordinator, finding, actions.action_body(assignee))
    assert response.status_code == 201, response.text
    action_id = response.json()["action_id"]
    response = actions.add_evidence(coordinator, action_id, actions.evidence_body())
    assert response.status_code == 201, response.text
    response = actions.close(coordinator, finding, actions.closure_body())
    assert response.status_code == 201, response.text

    (row,) = stack.counter_rows(zone)
    assert row["day"] == day
    assert stack.now().astimezone(UTC).date() == day + DAY, "en UTC ya es el día siguiente"
    assert row["activity_counts"] == {
        "finding_classified": 1,
        "action_managed": 2,
        "finding_closed": 1,
    }
    assert row["session_count"] == 4
    # La hora del reloj inyectado del escritor en el cierre (nada avanza el reloj después).
    assert row["last_activity_at"] == stack.now()
    # Ninguna columna ni ningún valor de usuario (H-33, P3).
    assert stack.columns("adoption_counter") == COUNTER_COLUMNS
    assert assignee not in {value for value in row.values() if isinstance(value, uuid.UUID)}
    assert set(row["activity_counts"]) <= {"finding_classified", "action_managed", "finding_closed"}


def test_a_rolled_back_write_leaves_no_count(stack: AdoptionStack) -> None:
    world = stack.actions.world
    organization = stack.organization()
    zone = stack.site_zone(organization)
    finding = stack.actions.received(organization)
    receipt, step = world.write(
        organization, "classification", world.classification_content(finding), rollback=True
    )
    assert step is not None and step.applied, receipt
    assert stack.counter_rows(zone) == []
    receipt, step = world.write(
        organization, "classification", world.classification_content(finding)
    )
    assert step is not None and step.applied, receipt
    (row,) = stack.counter_rows(zone)
    assert (row["activity_counts"], row["session_count"]) == ({"finding_classified": 1}, 1)


def test_system_records_do_not_count(stack: AdoptionStack) -> None:
    organization = stack.organization()
    zone = stack.site_zone(organization)
    assert stack.record(zone, "action_overdue") is None
    assert stack.record(zone, "loop_configuration_changed") is None
    assert stack.record(zone, "finding_received") is None
    assert stack.counter_rows(zone) == []
    assert stack.record(zone, "review_resolution") is not None
    (row,) = stack.counter_rows(zone)
    assert row["activity_counts"] == {"queue_resolved": 1}


def test_concurrent_activities_never_lose_a_count(stack: AdoptionStack) -> None:
    """Garantía: el incremento lo hace la base sobre la fila (``ON CONFLICT DO UPDATE``)."""
    for _ in range(ROUNDS):
        organization = stack.organization()
        zone = stack.site_zone(organization)
        stack.next_morning(organization, hour=12)
        stack.run(_together(stack, zone))
        (row,) = stack.counter_rows(zone)
        assert row["activity_counts"] == {"finding_classified": CONCURRENT_RECORDS}
        assert row["session_count"] == CONCURRENT_RECORDS


# --- alerta ---------------------------------------------------------------------------------------


def test_the_alert_borders_once_per_streak_and_a_new_streak(stack: AdoptionStack) -> None:
    organization, zone, _activated = _silent_world(stack, 3)
    # Días completos tras la activación: el de la activación no cuenta.
    for silent in (0, 1, 2):
        stack.next_morning(organization)
        assert stack.run_check(organization) == (), silent
    first_start = stack.local_today(organization) - 2 * DAY

    stack.next_morning(organization)
    (alert,) = stack.run_check(organization)
    assert (alert.zone_id, alert.streak.start, alert.streak.silent_days) == (
        zone.zone_id,
        first_start,
        3,
    )
    assert stack.adoption_events(organization) == [
        {
            "plant_id": organization.plant_id,
            "zone_id": str(zone.zone_id),
            "silent_days": 3,
            "last_activity_at": None,
        }
    ]
    (mark,) = stack.alert_rows(organization)
    assert set(mark) == ALERT_COLUMNS
    assert (mark["streak_start"], mark["silent_days"], mark["threshold_days"]) == (
        first_start,
        3,
        3,
    )
    assert mark["evaluated_day"] == stack.local_today(organization)
    assert mark["emitted_at"] <= stack.now()

    # La misma racha: ni el mismo día ni los siguientes vuelven a publicar.
    stack.tick(3600)
    assert stack.run_check(organization) == ()
    for _ in range(2):
        stack.next_morning(organization)
        assert stack.run_check(organization) == ()
    assert len(stack.adoption_events(organization)) == 1

    # Una actividad abre otra racha, que alerta de nuevo al cumplir el umbral.
    stack.to_local(organization, stack.local_today(organization), 15)
    stack.record(zone, "action_assigned")
    active_at = stack.now()
    for _ in range(3):
        stack.next_morning(organization)
        assert stack.run_check(organization) == ()
    stack.next_morning(organization)
    (second,) = stack.run_check(organization)
    assert second.streak.start == stack.local_today(organization) - 3 * DAY
    assert second.streak.start > first_start
    events = stack.adoption_events(organization)
    assert len(events) == 2
    assert events[1] == {
        "plant_id": organization.plant_id,
        "zone_id": str(zone.zone_id),
        "silent_days": 3,
        "last_activity_at": format_timestamp(active_at),
    }


def test_activity_on_the_evaluated_day_or_yesterday_keeps_the_zone_quiet(
    stack: AdoptionStack,
) -> None:
    organization, zone, _ = _silent_world(stack, 1)
    stack.record(zone, "closure")
    stack.next_morning(organization)
    # Ayer hubo actividad: la racha empieza hoy, que no ha terminado.
    assert stack.run_check(organization) == ()
    stack.to_local(organization, stack.local_today(organization), 9)
    stack.record(zone, "classification")
    assert stack.run_check(organization) == ()
    stack.next_morning(organization)
    assert stack.run_check(organization) == ()
    stack.next_morning(organization)
    (alert,) = stack.run_check(organization)
    assert alert.streak.silent_days == 1


def test_a_zone_that_is_not_active_never_alerts(stack: AdoptionStack) -> None:
    organization = stack.organization()
    stack.set_silence_days(organization, 1)
    stack.next_morning(organization, hour=10)
    without_gates = stack.site_zone(organization)
    commissioning = stack.add_zone(organization)
    stack.commissioning(commissioning)
    revoked = stack.add_zone(organization)
    stack.activate(revoked)
    stack.tick(3600)
    stack.revoke_usage(revoked)
    for _ in range(5):
        stack.next_morning(organization)
        assert stack.run_check(organization) == ()
    assert stack.adoption_events(organization) == []
    assert stack.alert_rows(organization) == []
    assert (without_gates.zone_id, commissioning.zone_id) != (revoked.zone_id, revoked.zone_id)

    # Al activarse cuenta desde el día siguiente a la activación, no desde el último uso.
    stack.activate(revoked)
    assert stack.run_check(organization) == ()
    stack.next_morning(organization)
    assert stack.run_check(organization) == (), "el día de la activación no cuenta"
    stack.next_morning(organization)
    (alert,) = stack.run_check(organization)
    assert (alert.zone_id, alert.streak.silent_days) == (revoked.zone_id, 1)


def test_after_an_outage_the_first_run_publishes_once_with_the_days_so_far(
    stack: AdoptionStack,
) -> None:
    organization, zone, _ = _silent_world(stack, 2)
    for _ in range(6):
        stack.next_morning(organization)
    (alert,) = stack.run_check(organization)
    assert alert.streak.silent_days == 5
    assert stack.adoption_events(organization) == [
        {
            "plant_id": organization.plant_id,
            "zone_id": str(zone.zone_id),
            "silent_days": 5,
            "last_activity_at": None,
        }
    ]
    assert stack.run_check(organization) == ()


def test_the_default_threshold_is_fourteen_days(stack: AdoptionStack) -> None:
    organization = stack.organization()
    stack.next_morning(organization, hour=10)
    zone = stack.site_zone(organization)
    stack.activate(zone)
    for _ in range(14):
        stack.next_morning(organization)
        assert stack.run_check(organization) == ()
    stack.next_morning(organization)
    (alert,) = stack.run_check(organization)
    assert (alert.streak.silent_days, alert.threshold_days) == (14, 14)


def test_gate_activations_take_the_later_of_the_two_approvals(stack: AdoptionStack) -> None:
    organization = stack.organization()
    zone = stack.site_zone(organization)
    activations = stack.real_activations()
    context = stack.periodic_context(organization)

    async def active() -> Mapping[uuid.UUID, datetime]:
        async with stack.actions.database.transaction(context) as transaction:
            return await activations.active_zones(transaction, organization.plant_id)

    assert stack.run(active()) == {}
    stack.commissioning(zone)
    assert stack.run(active()) == {}
    stack.tick(60)
    activated = stack.activate(zone)
    assert stack.run(active()) == {zone.zone_id: activated}
    stack.tick(60)
    stack.revoke_usage(zone)
    assert stack.run(active()) == {}


class _Barrier:
    """``ZoneActivations`` que detiene cada ejecución, ya dentro de su transacción, hasta que las
    dos llegan: así las dos miran la marca con la otra todavía sin confirmar."""

    def __init__(self, activations: ProductiveZones, parties: int) -> None:
        self._activations = activations
        self._barrier = asyncio.Barrier(parties)

    async def active_zones(
        self, transaction: Transaction, plant_id: uuid.UUID
    ) -> Mapping[uuid.UUID, datetime]:
        found = await self._activations.active_zones(transaction, plant_id)
        await asyncio.wait_for(self._barrier.wait(), BARRIER_SECONDS)
        return found


def test_two_runs_at_once_publish_one_alert(stack: AdoptionStack) -> None:
    """Garantía: la marca con clave única por racha, insertada antes de publicar."""
    for _ in range(ROUNDS):
        organization, zone, _ = _silent_world(stack, 1)
        stack.next_morning(organization)
        stack.next_morning(organization)
        check = stack.check_like(_Barrier(stack.real_activations(), 2))
        results = stack.run(_two_runs(stack, organization, check))
        assert sorted(len(alerts) for alerts in results) == [0, 1]
        assert len(stack.adoption_events(organization)) == 1
        assert len(stack.alert_rows(organization)) == 1
        assert stack.adoption_events(organization)[0]["zone_id"] == str(zone.zone_id)


# --- organización -----------------------------------------------------------------------------


def test_the_task_only_marks_and_publishes_in_its_organization(stack: AdoptionStack) -> None:
    a, zone_a, _ = _silent_world(stack, 1)
    b = stack.organization()
    stack.set_silence_days(b, 1)
    zone_b = stack.site_zone(b)
    stack.activate(zone_b)
    stack.next_morning(a)
    stack.next_morning(a)
    (alert,) = stack.run_check(a)
    assert alert.zone_id == zone_a.zone_id
    assert stack.adoption_events(b) == [] and stack.alert_rows(b) == []
    assert [row["zone_id"] for row in stack.alert_rows(a)] == [zone_a.zone_id]
    (other,) = stack.run_check(b)
    assert other.zone_id == zone_b.zone_id
    assert len(stack.adoption_events(a)) == 1


# --- ruta -----------------------------------------------------------------------------------------


def test_the_route_reports_activity_silence_and_threshold(stack: AdoptionStack) -> None:
    organization, zone, _ = _silent_world(stack, 4)
    quiet = stack.add_zone(organization)
    first_day = stack.local_today(organization)
    stack.to_local(organization, first_day, 11)
    stack.record(zone, "classification")
    stack.record(zone, "classification")
    stack.record(zone, "closure")
    stack.next_morning(organization, hour=8)
    stack.record(zone, "root_cause_analysis")
    last = stack.now()
    for _ in range(2):
        stack.next_morning(organization)
    today = stack.local_today(organization)
    coordinator = stack.actions.member(organization)

    response = stack.get_adoption(
        coordinator,
        {"plant_id": str(organization.plant_id), "from": str(first_day), "to": str(today)},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["plant_id"], body["threshold_days"], body["evaluated_day"]) == (
        str(organization.plant_id),
        4,
        str(today),
    )
    assert body["usage_notice_es"].startswith("La adopción se mide por zona")
    zones = {item["zone_id"]: item for item in body["zones"]}
    assert set(zones) >= {str(zone.zone_id), str(quiet.zone_id)}
    active = zones[str(zone.zone_id)]
    zeros = {
        "inbox_opened": 0,
        "finding_classified": 0,
        "queue_resolved": 0,
        "analysis_advanced": 0,
        "action_managed": 0,
        "finding_closed": 0,
    }
    assert active == {
        "zone_id": str(zone.zone_id),
        "zone_active": True,
        "silent_days": 1,
        "streak_start": str(today - DAY),
        "last_activity_at": format_timestamp(last),
        "days": [
            {
                "day": str(first_day),
                "activity_counts": {**zeros, "finding_classified": 2, "finding_closed": 1},
                "activity_count": 3,
            },
            {
                "day": str(first_day + DAY),
                "activity_counts": {**zeros, "analysis_advanced": 1},
                "activity_count": 1,
            },
        ],
    }
    assert zones[str(quiet.zone_id)] == {
        "zone_id": str(quiet.zone_id),
        "zone_active": False,
        "silent_days": None,
        "streak_start": None,
        "last_activity_at": None,
        "days": [],
    }

    # Una zona: solo esa; el periodo acota los días.
    response = stack.get_adoption(
        coordinator, {"zone_id": str(zone.zone_id), "from": str(today), "to": str(today)}
    )
    assert response.status_code == 200, response.text
    (only,) = response.json()["zones"]
    assert (only["zone_id"], only["days"], only["silent_days"]) == (str(zone.zone_id), [], 1)


def test_the_route_answers_not_found_for_what_is_not_in_scope(stack: AdoptionStack) -> None:
    organization = stack.organization()
    other = stack.organization()
    zone = stack.site_zone(organization)
    sibling = stack.add_zone(organization)
    period = {"from": "2026-01-01", "to": "2026-01-31"}
    coordinator = stack.actions.member(organization)
    zone_only = stack.actions.member(
        organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone.zone_id
    )
    line = stack.actions.member(organization, Role.LINE_MANAGER)
    for who, params in (
        (coordinator, {"plant_id": str(other.plant_id)}),
        (coordinator, {"zone_id": str(other.zone_id)}),
        (coordinator, {"plant_id": str(uuid.uuid4())}),
        (coordinator, {"zone_id": str(uuid.uuid4())}),
        (zone_only, {"plant_id": str(organization.plant_id)}),
        (zone_only, {"zone_id": str(sibling.zone_id)}),
        (line, {"plant_id": str(organization.plant_id)}),
    ):
        response = stack.get_adoption(who, {**params, **period})
        assert _error(response) == _NOT_FOUND, (params, response.text)
    response = stack.get_adoption(zone_only, {"zone_id": str(zone.zone_id), **period})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-01-01", "to": "2026-01-31"},
        {"plant_id": "{plant}", "zone_id": "{zone}", "from": "2026-01-01", "to": "2026-01-31"},
        {"plant_id": "{plant}", "from": "2026-01-31", "to": "2026-01-01"},
        {"plant_id": "{plant}", "from": "2025-01-01", "to": "2026-01-02"},
        {"plant_id": "{plant}", "from": "2026-02-30", "to": "2026-03-01"},
        {"plant_id": "{plant}", "from": "2026-1-01", "to": "2026-03-01"},
        {"plant_id": "{plant}", "from": "2026-01-01"},
        {"plant_id": "{plant}", "from": "2026-01-01", "to": "2026-01-02", "user_id": "x"},
    ],
    ids=[
        "sin_planta_ni_zona",
        "planta_y_zona",
        "periodo_invertido",
        "periodo_de_367_dias",
        "fecha_inexistente",
        "fecha_mal_formada",
        "sin_fin",
        "parametro_de_persona",
    ],
)
def test_the_route_rejects_invalid_filters(stack: AdoptionStack, params: dict[str, str]) -> None:
    organization = stack.organization()
    replaced = {
        key: value.format(plant=organization.plant_id, zone=organization.zone_id)
        for key, value in params.items()
    }
    response = stack.get_adoption(stack.actions.member(organization), replaced)
    assert _error(response) == _INVALID, response.text


def test_the_longest_period_is_accepted(stack: AdoptionStack) -> None:
    organization = stack.organization()
    response = stack.get_adoption(
        stack.actions.member(organization),
        {"plant_id": str(organization.plant_id), "from": "2025-01-02", "to": "2026-01-02"},
    )
    assert response.status_code == 200, response.text
