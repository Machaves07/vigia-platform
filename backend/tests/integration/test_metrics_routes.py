"""SUP-U05-30 con vigia_app, RLS, expediente real y Clock compartido (datos sintéticos)."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from tests.api_support import World
from tests.exposure_support import ExposureStack, exposure_stack
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import LoopOrganization
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.metrics.model import ProgramMetricSnapshot
from vigia_platform.accreditation.metrics.repository import MetricsRepository
from vigia_platform.accreditation.metrics.service import ProgramMetricsService
from vigia_platform.accreditation.metrics.task import MetricsMaterialize
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ScopeLevel
from vigia_platform.shared.db import Transaction

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExposureStack]:
    with exposure_stack(postgres_endpoint, "program_metrics") as built:
        # El Clock simulado queda por delante de las recepciones que fija el disparador.
        # No se compara nunca con un segundo reloj para medir vigencia de la caché.
        built.advance(3600)
        yield built


def service(
    stack: ExposureStack, repository: MetricsRepository | None = None
) -> ProgramMetricsService:
    return ProgramMetricsService(
        database=stack.database,
        authorizer=stack.world.stack.authz.authorizer,
        exposure=stack.service(),
        clock=stack.world.stack.authz.sessions.clock,
        repository=repository,
    )


def get(stack: ExposureStack, who: Any, params: dict[str, Any]) -> httpx.Response:
    authz = stack.world.stack.authz
    app = World(clock=authz.sessions.clock).app(
        runtime={
            "sessions": authz.contexts,
            "authorizer": ContextAuthorizer(
                audit=authz.audit,
                provider_organization_id=authz.provider_organization_id,
                provider_queries=LedgerProviderQueryLedger(stack.world.stack.primary.writer),
                clock=authz.sessions.clock,
            ),
            "state": {ACCREDITATION_STATE_KEY: AccreditationHttp(metrics=service(stack))},
        }
    )

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        ) as client:
            return await client.get(
                "/accreditation/metrics",
                params=params,
                headers={
                    "Sec-Fetch-Site": "same-origin",
                    "Cookie": f"{SESSION_COOKIE_NAME}={who.value}",
                },
            )

    return stack.run(request())


def rows(stack: ExposureStack, zone_id: uuid.UUID) -> list[Any]:
    return stack.fetch(
        "SELECT * FROM accreditation.program_metric_snapshot WHERE zone_id=$1", zone_id
    )


def test_route_seven_values_and_per_metric_not_computable_within_200(stack: ExposureStack) -> None:
    org = stack.world.organization()
    who = stack.member(org)
    stack.finding(org, started=stack.now() - timedelta(minutes=10), duration=timedelta(seconds=1))
    response = get(stack, who, {"zone_id": str(org.zone_id), "period": stack.period()})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    snapshot = response.json()["zones"][0]
    assert snapshot["zone_id"] == str(org.zone_id)
    assert len(snapshot["values"]) == 7
    assert snapshot["recomputed_at"].endswith(".000Z") or snapshot["recomputed_at"].endswith("Z")
    assert len(rows(stack, org.zone_id)) == 1
    # Otra zona sin catálogo no se convierte en exposición cero.
    zone = stack.zone_without_catalog(org)
    missing = get(stack, who, {"zone_id": str(zone), "period": stack.period()})
    assert missing.status_code == 200, missing.text
    values = {m["key"]: m for m in missing.json()["zones"][0]["values"]}
    assert values["exposure"]["status"] == "not_computable"
    assert values["exposure"]["value"] is None
    assert values["exposure"]["detail_code"] == "accreditation_metric_not_computable"


@pytest.mark.parametrize("field", ["person_id", "user_id", "team_id", "shift", "group_by"])
def test_route_rejects_every_extra_dimension(stack: ExposureStack, field: str) -> None:
    org = stack.world.organization()
    response = get(
        stack,
        stack.member(org),
        {"zone_id": str(org.zone_id), "period": stack.period(), field: "x"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "invalid_request"


def test_authorization_zone_and_organization_fail_closed(stack: ExposureStack) -> None:
    a, b = stack.world.organization(), stack.world.organization()
    second = stack.zone_without_catalog(a)
    own_zone = stack.member(a, level=ScopeLevel.ZONE, scope=a.zone_id)
    for zone in (second, b.zone_id, uuid.UUID(int=999)):
        response = get(stack, own_zone, {"zone_id": str(zone), "period": stack.period()})
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
        assert not rows(stack, zone)
    # Tener una zona no otorga la planta entera.
    denied = get(stack, own_zone, {"plant_id": str(a.plant_id), "period": stack.period()})
    assert denied.status_code == 404


def test_plant_lists_only_its_zones_and_the_zone_filter_is_exact(stack: ExposureStack) -> None:
    org = stack.world.organization()
    other_plant = stack.world.add_plant(org)
    other_zone = uuid.uuid4()
    stack.execute(
        "INSERT INTO identity.zone (organization_id,plant_id,zone_id,code,name,created_at,"
        " created_by)"
        " VALUES ($1,$2,$3,'Z-OTRA','Zona sintética',$4,$5)",
        org.organization_id,
        other_plant,
        other_zone,
        stack.now(),
        stack.world.stack.authz.operator_id,
    )
    second = stack.zone_without_catalog(org)
    who = stack.member(org)
    plant = get(stack, who, {"plant_id": str(org.plant_id), "period": stack.period()})
    assert plant.status_code == 200, plant.text
    assert {z["zone_id"] for z in plant.json()["zones"]} == {
        str(org.zone_id),
        str(org.site.other_zone_id),
        str(second),
    }
    zone = get(stack, who, {"zone_id": str(second), "period": stack.period()})
    assert [z["zone_id"] for z in zone.json()["zones"]] == [str(second)]


def test_repository_does_not_read_records_of_another_zone_of_the_same_plant(
    stack: ExposureStack,
) -> None:
    org = stack.world.organization()
    subject = stack.finding(
        org, started=stack.now() - timedelta(minutes=10), duration=timedelta(seconds=1)
    )
    other = LoopOrganization(dataclasses.replace(org.site, zone_id=org.site.other_zone_id))
    document = stack.world.classification_content(
        subject,
        outcome="false_positive",
        reason_category="no_person_present",
        sequence=2,
        previous_outcome="confirmed",
    )
    document["zone_id"] = str(other.zone_id)
    receipt, _ = stack.world.write(other, "classification", document, apply=False)
    assert isinstance(receipt, Receipt), receipt

    async def read() -> Any:
        async with stack.database.transaction(stack.context(org)) as tx:
            return await MetricsRepository().records(
                tx,
                plant_id=org.plant_id,
                zone_id=org.zone_id,
                head=await MetricsRepository().head(tx, org.plant_id),
                period_end=stack.bounds(stack.period())[1],
            )

    records = stack.run(read())
    assert records
    assert receipt.record_id not in {r.record_id for r in records}


def test_current_month_cache_expires_at_15_minutes_and_on_a_new_record(
    stack: ExposureStack,
) -> None:
    org = stack.world.organization()
    stack.finding(org, started=stack.now() - timedelta(minutes=10), duration=timedelta(seconds=1))
    repository = CountingRepository()
    svc = service(stack, repository)
    context = stack.context(org)
    period = stack.period()
    first = stack.run(svc.snapshot(context, org.zone_id, period))
    stack.advance(899)
    assert stack.run(svc.snapshot(context, org.zone_id, period)) == first
    assert repository.reads == 1
    stack.advance(1)
    expired = stack.run(svc.snapshot(context, org.zone_id, period))
    assert expired.recomputed_at != first.recomputed_at
    assert repository.reads == 2
    stack.finding(org, started=stack.now() - timedelta(minutes=5), duration=timedelta(seconds=1))
    changed = stack.run(svc.snapshot(context, org.zone_id, period))
    assert changed.inputs_digest != expired.inputs_digest
    assert repository.reads == 3
    assert {v.key: v.value for v in changed.values}["classified_in_time"] == 1.0


class CountingRepository(MetricsRepository):
    def __init__(self) -> None:
        self.reads = 0

    async def records(self, transaction: Transaction, **kwargs: Any) -> Any:
        self.reads += 1
        return await super().records(transaction, **kwargs)


class ConcurrentRepository(MetricsRepository):
    def __init__(self) -> None:
        self.barrier = asyncio.Barrier(2)
        self.writes = 0

    async def save(
        self, transaction: Transaction, snapshot: ProgramMetricSnapshot, **kwargs: Any
    ) -> ProgramMetricSnapshot | None:
        await self.barrier.wait()
        result = await super().save(transaction, snapshot, **kwargs)
        self.writes += result is not None
        return result


@pytest.mark.parametrize("_repeat", range(3))
def test_concurrent_materialization_writes_once_and_retries_after_a_crash(
    stack: ExposureStack,
    _repeat: int,
) -> None:
    org = stack.world.organization()
    zone = stack.zone_without_catalog(org)
    period = stack.period()
    _, end = stack.bounds(period)
    stack.advance((end - stack.now()).total_seconds())
    repository = ConcurrentRepository()
    svc = service(stack, repository)
    context = stack.context(org)

    async def both() -> list[ProgramMetricSnapshot]:
        return list(
            await asyncio.gather(
                svc.snapshot(context, zone, period), svc.snapshot(context, zone, period)
            )
        )

    first, second = stack.run(both())
    assert repository.writes == 1
    assert first == second
    assert first.recomputed_at is None
    assert len(rows(stack, zone)) == 1
    # Caída después del COMMIT y antes de acusar recibo: otro proceso retoma lo confirmado.
    resumed = stack.run(service(stack).snapshot(context, zone, period))
    assert resumed == first


def test_current_read_matches_the_month_close_task_and_stale_cache_cannot_replace_it(
    stack: ExposureStack,
) -> None:
    org = stack.world.organization()
    stack.finding(org, started=stack.now() - timedelta(minutes=10), duration=timedelta(seconds=1))
    context = stack.context(org)
    period = stack.period()
    _, end = stack.bounds(period)
    stack.advance((end - stack.now()).total_seconds() - 0.001)
    current = stack.run(service(stack).snapshot(context, org.zone_id, period))
    stack.advance(0.001)
    task = MetricsMaterialize(service=service(stack), clock=stack.world.stack.authz.sessions.clock)

    async def close() -> None:
        async with stack.database.transaction(context) as tx:
            await task.run(tx)

    stack.run(close())
    final = stack.run(service(stack).snapshot(context, org.zone_id, period))
    assert current.values == final.values
    assert final.recomputed_at is None
    stale = current.model_copy(update={"inputs_digest": "0" * 64})

    async def delayed() -> Any:
        async with stack.database.transaction(context) as tx:
            return await MetricsRepository().save(
                tx, stale, timezone="America/Bogota", expired_before=stack.now()
            )

    assert stack.run(delayed()) is None
    assert stack.run(service(stack).snapshot(context, org.zone_id, period)) == final


def test_failed_transaction_leaves_no_snapshot_and_the_retry_recovers(stack: ExposureStack) -> None:
    org = stack.world.organization()

    class FailedRepository(MetricsRepository):
        async def save(
            self, transaction: Transaction, snapshot: ProgramMetricSnapshot, **kwargs: Any
        ) -> Any:
            await super().save(transaction, snapshot, **kwargs)
            raise RuntimeError("caída sintética antes de COMMIT")

    with pytest.raises(RuntimeError, match="caída sintética"):
        stack.run(
            service(stack, FailedRepository()).snapshot(
                stack.context(org), org.zone_id, stack.period()
            )
        )
    assert not rows(stack, org.zone_id)
    stack.run(service(stack).snapshot(stack.context(org), org.zone_id, stack.period()))
    assert len(rows(stack, org.zone_id)) == 1


@pytest.mark.parametrize("_repeat", range(3))
def test_concurrent_stale_cache_and_final_never_rewrite_a_final(
    stack: ExposureStack, _repeat: int
) -> None:
    org = stack.world.organization()
    period = stack.period()
    _, end = stack.bounds(period)
    stack.advance((end - stack.now()).total_seconds())
    final = stack.run(service(stack).snapshot(stack.context(org), org.zone_id, period))
    stale = final.model_copy(
        update={"recomputed_at": end - timedelta(seconds=1), "inputs_digest": "0" * 64}
    )
    repository = ConcurrentRepository()

    async def write(snapshot: ProgramMetricSnapshot) -> Any:
        async with stack.database.transaction(stack.context(org)) as tx:
            return await repository.save(
                tx, snapshot, timezone="America/Bogota", expired_before=end
            )

    async def both() -> Any:
        return await asyncio.gather(write(final), write(stale))

    assert stack.run(both()) == [None, None]
    assert repository.writes == 0
    assert stack.run(service(stack).snapshot(stack.context(org), org.zone_id, period)) == final


def test_task_only_materializes_its_organization(stack: ExposureStack) -> None:
    a, b = stack.world.organization(), stack.world.organization()
    start, _ = stack.bounds(stack.period())
    created = start - timedelta(days=2)
    stack.execute(
        "UPDATE identity.zone SET created_at=$1 WHERE zone_id=ANY($2::uuid[])",
        created,
        [a.zone_id, b.zone_id],
    )
    task = MetricsMaterialize(service=service(stack), clock=stack.world.stack.authz.sessions.clock)

    async def run() -> None:
        async with stack.database.transaction(stack.context(a)) as tx:
            await task.run(tx)

    stack.run(run())
    assert rows(stack, a.zone_id)
    assert not rows(stack, b.zone_id)
    before = rows(stack, a.zone_id)
    stack.run(run())
    assert rows(stack, a.zone_id) == before
