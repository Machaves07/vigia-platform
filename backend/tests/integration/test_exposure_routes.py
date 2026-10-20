"""``GET /accreditation/exposure`` sobre PostgreSQL 16 real como ``vigia_app`` (TASK-322;
SUP-U05-31; LC-LAZ-28; PAT-LAZ-REN-01; BR-LAZ-118 a 123).

Con hallazgos ingresados por la ruta del nodo, proyectados y clasificados por el escritor real
(``tests/exposure_support.py``), los puertos reales de U-03 y una línea de tiempo guionada:

- las tres cifras (confirmados y pendientes aparte), la línea de tiempo del mismo periodo, la
  distribución horaria y el texto fijo; ``Cache-Control: no-store``; una fila guardada;
- una zona unipersonal agrega sus cubos a su ventana, también si cambia después del cálculo;
- un hueco de cobertura o una revocación de la compuerta de uso no aumentan los confirmados;
- **mes en curso**: lo guardado se sirve dentro de 15 minutos con la misma huella; una escritura
  nueva de la planta o el paso de 15 minutos obligan a recalcular;
- **mes cerrado**: se materializa una vez (``materialized_at`` ≥ fin, sin ``recomputed_at``), se
  sirve sin recalcular y ningún valor del mes en curso lo sobrescribe;
- **concurrencia**: dos primeros cálculos simultáneos del mismo periodo dejan una sola fila y
  ninguno falla (``INSERT … ON CONFLICT``);
- alcance: una zona de otra organización, una zona inexistente o una zona fuera de las
  asignaciones del actor responden ``not_found`` y no dejan fila; un mes que no ha empezado,
  ``invalid_request``; un parámetro de más, ``invalid_request``;
- con la ``CoveragePort`` real de U-02, la cobertura del indicador es la de su línea de tiempo.

Solo datos generados; reloj simulado desde la hora de la base (retro 14).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from vigia_contracts.models.enumerations import GateStatus

from tests.exposure_support import (
    TIMEZONE,
    ExposureStack,
    GatedRepository,
    ScriptedCoverage,
    exposure_stack,
)
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.accreditation.exposure.indicator import USAGE_NOTICE_ES
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.loop.calendar import local_date
from vigia_platform.loop.domain.enums import ExposureStatus
from vigia_platform.shared.context import Role, ScopeLevel

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExposureStack]:
    with exposure_stack(postgres_endpoint, "exposure_routes") as built:
        yield built


@pytest.fixture
def coverage(stack: ExposureStack) -> Iterator[ScriptedCoverage]:
    stack.coverage.gaps.clear()
    yield stack.coverage
    stack.coverage.gaps.clear()


def _params(zone_id: uuid.UUID, period: str) -> dict[str, str]:
    return {"zone_id": str(zone_id), "period": period}


def test_the_three_figures_the_timeline_and_the_notice(
    stack: ExposureStack, coverage: ScriptedCoverage
) -> None:
    organization = stack.world.organization()
    coordinator = stack.member(organization)
    now = stack.now()
    started = now - timedelta(minutes=20)
    stack.finding(organization, started=started, duration=timedelta(seconds=30))
    stack.finding(
        organization,
        started=now - timedelta(minutes=10),
        duration=timedelta(seconds=12),
        confirmed=False,
    )
    period = stack.period()
    start, end = stack.bounds(period)

    response = stack.get(coordinator, _params(organization.zone_id, period))

    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["confirmed_minutes"] == 0.5
    assert body["pending_classification_minutes"] == 0.2
    period_ms = (end - start) // timedelta(milliseconds=1)
    observed_ms = (stack.now() - start) // timedelta(milliseconds=1)
    assert body["observable_coverage_percent"] == observed_ms * 100 / period_ms
    assert body["status"] == ("partial" if observed_ms * 100 < 80 * period_ms else "computed")
    assert (body["partial_reason_es"] is not None) == (body["status"] == "partial")
    assert body["usage_notice_es"] == USAGE_NOTICE_ES
    timeline = body["coverage_timeline"]
    assert timeline["summary"]["total_ms"] == period_ms
    assert timeline["summary"]["observable_ms"] == observed_ms
    assert timeline["intervals"][-1]["causes"] == ["period_not_elapsed"]
    buckets = body["hourly_distribution"]
    assert {bucket["width_minutes"] for bucket in buckets} == {60}
    assert sum(bucket["confirmed_minutes"] for bucket in buckets) == 0.5
    local = started.astimezone(ZoneInfo(TIMEZONE))
    (hit,) = [bucket for bucket in buckets if bucket["confirmed_minutes"]]
    assert hit["start_minute_of_day"] == local.hour * 60
    (row,) = stack.rows(organization.zone_id)
    assert row["period"] == local_date(start, TIMEZONE)
    assert row["recomputed_at"] == stack.now()
    assert row["materialized_at"] < end


def test_a_single_occupancy_zone_aggregates_its_buckets_even_after_the_computation(
    stack: ExposureStack, coverage: ScriptedCoverage
) -> None:
    organization = stack.world.organization()
    coordinator = stack.member(organization)
    stack.finding(
        organization, started=stack.now() - timedelta(minutes=20), duration=timedelta(seconds=30)
    )
    params = _params(organization.zone_id, stack.period())
    first = stack.get(coordinator, params)
    assert {b["width_minutes"] for b in first.json()["hourly_distribution"]} == {60}

    stack.set_single_occupancy(organization, 240)
    second = stack.get(coordinator, params)

    assert second.status_code == 200, second.text
    buckets = second.json()["hourly_distribution"]
    assert {bucket["width_minutes"] for bucket in buckets} == {240}
    assert [bucket["start_minute_of_day"] for bucket in buckets] == [0, 240, 480, 720, 960, 1200]
    assert sum(bucket["confirmed_minutes"] for bucket in buckets) == 0.5


def test_a_coverage_gap_or_a_revoked_gate_never_increases_the_confirmed_minutes(
    stack: ExposureStack, coverage: ScriptedCoverage
) -> None:
    organization = stack.world.organization()
    context = stack.context(organization)
    started = stack.now() - timedelta(minutes=20)
    stack.finding(organization, started=started, duration=timedelta(seconds=30))
    period = stack.period()

    full = stack.run(stack.service().indicator(context, organization.zone_id, period))
    assert full.confirmed_minutes == 0.5

    coverage.gaps.append((started + timedelta(seconds=10), started + timedelta(seconds=20)))
    stack.advance(16 * 60)
    gap = stack.run(stack.service().indicator(context, organization.zone_id, period))
    assert gap.confirmed_minutes == 20 / 60
    coverage.gaps.clear()

    revoked = RevokedGates(organization.organization_id, started + timedelta(seconds=15))
    service = stack.service()
    service._gates = revoked  # el puerto de U-03 con una revocación a mitad del episodio
    stack.advance(16 * 60)
    stopped = stack.run(service.indicator(context, organization.zone_id, period))
    assert stopped.confirmed_minutes == 15 / 60
    assert revoked.calls == 1
    annotations = (stopped.coverage_timeline or {})["annotations"]
    assert [note["kind"] for note in annotations] == ["usage_gate_revoked"]


class RevokedGates:
    """``GateQueryPort.gate_history`` con la compuerta de uso aprobada hasta ``since`` y revocada
    desde entonces, y la de **montaje** revocada en todo el periodo: solo la revocación de la de
    uso deja de acumular."""

    def __init__(self, organization_id: uuid.UUID, since: Any) -> None:
        self.organization_id = organization_id
        self.since = since
        self.calls = 0

    def _interval(
        self, zone_id: uuid.UUID, gate: GateKind, status: GateStatus, start: Any, end: Any
    ) -> GateInterval:
        return GateInterval(
            organization_id=self.organization_id,
            plant_id=uuid.uuid4(),
            zone_id=zone_id,
            gate=gate,
            status=status,
            effective_from=start,
            effective_until=end,
            decided_by=uuid.uuid4(),
            reason_es="Decisión sintética de la gobernanza",
            record_id=uuid.uuid4(),
            ledger_record_id=uuid.uuid4(),
        )

    async def gate_history(
        self, context: Any, zone_id: uuid.UUID, start: Any, end: Any
    ) -> tuple[GateInterval, ...]:
        self.calls += 1
        return (
            self._interval(zone_id, GateKind.USAGE, GateStatus.APPROVED, start, self.since),
            self._interval(zone_id, GateKind.USAGE, GateStatus.REVOKED, self.since, None),
            self._interval(zone_id, GateKind.MOUNTING, GateStatus.REVOKED, start, None),
        )


def test_the_current_month_is_reused_for_15_minutes_unless_the_plant_writes(
    stack: ExposureStack, coverage: ScriptedCoverage
) -> None:
    organization = stack.world.organization()
    coordinator = stack.member(organization)
    stack.finding(
        organization, started=stack.now() - timedelta(minutes=30), duration=timedelta(seconds=30)
    )
    params = _params(organization.zone_id, stack.period())
    first = stack.get(coordinator, params).json()
    calls = coverage.calls

    stack.advance(60)
    reused = stack.get(coordinator, params).json()
    assert reused == first
    assert coverage.calls == calls

    # Una escritura nueva de la planta (otro hallazgo confirmado) cambia la huella.
    stack.finding(
        organization, started=stack.now() - timedelta(minutes=5), duration=timedelta(seconds=30)
    )
    after_write = stack.get(coordinator, params).json()
    assert after_write["confirmed_minutes"] == 1.0
    assert after_write["recomputed_at"] != first["recomputed_at"]
    assert coverage.calls == calls + 1

    # Sin escrituras, la validez de 15 minutos caduca sola.
    stack.advance(14 * 60)
    assert stack.get(coordinator, params).json()["recomputed_at"] == after_write["recomputed_at"]
    stack.advance(61)
    expired = stack.get(coordinator, params).json()
    assert expired["recomputed_at"] != after_write["recomputed_at"]
    assert expired["confirmed_minutes"] == 1.0
    assert len(stack.rows(organization.zone_id)) == 1


def test_two_simultaneous_first_computations_leave_one_row(
    stack: ExposureStack, coverage: ScriptedCoverage
) -> None:
    organization = stack.world.organization()
    context = stack.context(organization)
    stack.finding(
        organization, started=stack.now() - timedelta(minutes=20), duration=timedelta(seconds=30)
    )
    period = stack.period()
    repository = GatedRepository(2)
    service = stack.service(repository=repository)

    async def both() -> list[Any]:
        return list(
            await asyncio.gather(
                service.indicator(context, organization.zone_id, period),
                service.indicator(context, organization.zone_id, period),
            )
        )

    first, second = stack.run(both())

    rows = stack.rows(organization.zone_id)
    assert len(rows) == 1
    assert first.exposure_id == second.exposure_id == rows[0]["exposure_id"]
    assert first.confirmed_minutes == second.confirmed_minutes == 0.5


def test_episodes_of_another_zone_of_the_same_plant_never_count(
    stack: ExposureStack, coverage: ScriptedCoverage
) -> None:
    organization = stack.world.organization()
    context = stack.context(organization)
    finding = stack.finding(
        organization, started=stack.now() - timedelta(minutes=20), duration=timedelta(seconds=30)
    )
    sibling = stack.zone_without_catalog(organization)
    stack.clone_episode(finding, sibling)
    stack.clone_episode(finding, sibling)

    indicator = stack.run(stack.service().indicator(context, organization.zone_id, stack.period()))

    assert indicator.confirmed_minutes == 0.5
    assert sum(bucket.confirmed_ms for bucket in indicator.hourly_distribution) == 30_000


def test_scope_not_found_and_invalid_requests(
    stack: ExposureStack, coverage: ScriptedCoverage
) -> None:
    organization = stack.world.organization()
    other = stack.world.organization()
    coordinator = stack.member(organization)
    period = stack.period()

    foreign = stack.get(coordinator, _params(other.zone_id, period))
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["code"] == "not_found"
    assert stack.rows(other.zone_id) == []
    unknown = stack.get(coordinator, _params(uuid.uuid4(), period))
    assert unknown.status_code == 404

    # Un coordinador de **otra zona de la misma planta** no ve esta.
    sibling = stack.zone_without_catalog(organization)
    narrow = stack.member(organization, Role.COORDINATOR_SST, ScopeLevel.ZONE, sibling)
    outside = stack.get(narrow, _params(organization.zone_id, period))
    assert outside.status_code == 404, outside.text
    assert stack.rows(organization.zone_id) == []
    line_manager = stack.member(organization, Role.LINE_MANAGER)
    denied = stack.get(line_manager, _params(organization.zone_id, period))
    assert denied.status_code == 404, denied.text
    assert denied.json()["code"] == "not_found"

    _, end = stack.bounds(period)
    future = stack.period(end + timedelta(days=1))
    early = stack.get(coordinator, _params(organization.zone_id, future))
    assert early.status_code == 400, early.text
    assert early.json()["code"] == "invalid_request"
    assert early.json().get("detail_code") is None
    extra = stack.get(coordinator, {**_params(organization.zone_id, period), "shift": "a"})
    assert extra.status_code == 400
    malformed = stack.get(coordinator, _params(organization.zone_id, "2026-13"))
    assert malformed.status_code == 400
    assert stack.rows(organization.zone_id) == []


def test_with_the_real_coverage_port_the_percent_is_its_timeline(stack: ExposureStack) -> None:
    organization = stack.world.organization()
    service = stack.service()
    service._coverage = stack.real_coverage()
    indicator = stack.run(
        service.indicator(stack.context(organization), organization.zone_id, stack.period())
    )
    timeline = indicator.coverage_timeline or {}
    summary = timeline["summary"]
    assert indicator.observable_coverage_percent == (
        summary["observable_ms"] * 100 / summary["total_ms"]
    )
    assert summary["total_ms"] == sum(i["duration_ms"] for i in timeline["intervals"])
    assert indicator.status in {ExposureStatus.COMPUTED, ExposureStatus.PARTIAL}
    assert len(closed_rows := stack.rows(organization.zone_id)) == 1
    assert closed_rows[0]["observable_coverage_percent"] == indicator.observable_coverage_percent


@pytest.fixture
def closed(postgres_endpoint: PostgresEndpoint) -> Iterator[ExposureStack]:
    with exposure_stack(postgres_endpoint, "exposure_closed_month") as built:
        yield built


def test_a_closed_month_is_materialized_once_and_never_overwritten_by_the_current_one(
    closed: ExposureStack,
) -> None:
    organization = closed.world.a
    closed.finding(
        organization, started=closed.now() - timedelta(minutes=20), duration=timedelta(seconds=30)
    )
    period = closed.period()
    _, end = closed.bounds(period)
    closed.advance((end - closed.now()).total_seconds() + 3600)
    coordinator = closed.member(organization)
    params = _params(organization.zone_id, period)

    response = closed.get(coordinator, params)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confirmed_minutes"] == 0.5
    assert body["observable_coverage_percent"] == 100.0
    assert body["status"] == "computed"
    assert body["recomputed_at"] is None
    (row,) = closed.rows(organization.zone_id)
    assert row["materialized_at"] >= end
    calls = closed.coverage.calls

    closed.advance(3 * 3600)
    coordinator = closed.member(organization)  # la sesión anterior caducó por inactividad
    assert closed.get(coordinator, params).json() == body
    assert closed.coverage.calls == calls

    # Un valor del mes en curso que llega tarde no pisa el definitivo.
    values = {
        "exposure_id": uuid.uuid4(),
        "plant_id": organization.plant_id,
        "zone_id": organization.zone_id,
        "period": row["period"],
        "status": "partial",
        "confirmed_minutes": 99.0,
        "pending_classification_minutes": 0.0,
        "observable_coverage_percent": 1.0,
        "coverage_timeline_ref": {},
        "hourly_distribution": [],
        "partial_reason_es": "Valor tardío del mes en curso",
        "usage_notice_es": USAGE_NOTICE_ES,
        "computed_at": end - timedelta(minutes=1),
        "recomputed_at": end - timedelta(minutes=1),
        "inputs_digest": "0" * 64,
    }
    repository = closed.service()._repository

    async def late() -> Any:
        async with closed.database.transaction(closed.context(organization)) as transaction:
            return await repository.save(
                transaction, values=values, definitive=False, period_end=end
            )

    assert closed.run(late()) is None
    (after,) = closed.rows(organization.zone_id)
    assert after == row

    # La zona pasa a unipersonal: el mes materializado se vuelve a materializar con cubos de su
    # ventana, nunca se sirve con cubos más finos.
    closed.set_single_occupancy(organization, 120)
    coordinator = closed.member(organization)
    aggregated = closed.get(coordinator, params).json()
    assert {bucket["width_minutes"] for bucket in aggregated["hourly_distribution"]} == {120}
    assert aggregated["confirmed_minutes"] == 0.5
    assert aggregated["recomputed_at"] is None
    (rematerialized,) = closed.rows(organization.zone_id)
    assert rematerialized["materialized_at"] > row["materialized_at"]
