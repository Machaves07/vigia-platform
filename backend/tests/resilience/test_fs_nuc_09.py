"""FS-NUC-09 · Reloj del nodo desviado (PR-NUC-25 a 29; BR-NUC-69 a 73).

**Inyección**: un **nodo simulado con su reloj 10 minutos adelantado** (``node_clock.offset_ms``
declarado de 600 000 y todas sus marcas desplazadas) envía por ``EscritorExpediente``, con los
**generadores del kit de U-01**: hallazgos (``finding``) y un par de eventos de observabilidad
(``observability_event_pair``, apertura degradada y cierre) de la zona. La plataforma declara la
comunicación del nodo con su propio reloj (``node_communication_state_changed``: alcanzable y
después mudo). PostgreSQL de verdad como ``vigia_app``.

**Resultado esperado**: los registros del nodo se **aceptan** (la tolerancia de BR-CTR-08 sobre el
catálogo la aplica U-03 al ingerir; el expediente no rechaza por la marca del nodo y la conserva
tal cual); la **línea de tiempo** de la zona tiene los tramos de la capa del nodo con
``clock_basis = node``, sus extremos son las marcas **del nodo** (desplazadas) y llevan la
desviación declarada; los de la **capa de comunicación** van con el **reloj de la plataforma**
(``clock_basis = platform``, el mudo empieza en ``last_heartbeat_at`` de la plataforma); y la
línea de tiempo es una **partición exacta** del periodo: tramos contiguos, sin solapes, que cubren
``[a, b)`` y cuyo resumen suma ``b - a``.

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from vigia_contracts.conformance.generators import observability_event_pair, zone_catalog

from tests.factories import uuid7
from tests.identity_db import migrated_database, seed_identity
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_coverage_port import COVERAGE_TYPES
from tests.resilience.harness import scenario
from tests.resilience.load import all_clips, draw_example, kit_findings
from tests.writer_support import Place, WriterEnvironment, unit_context, writer_environment
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.ledger.domain.coverage import ClockBasis, CoverageLayer, CoveragePeriod
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

SKEW: Final = timedelta(minutes=10)
SKEW_MS: Final = 600_000
T0: Final = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
"""Inicio del periodo (marcas fijas: la línea de tiempo no depende de la hora de la prueba)."""
PERIOD: Final = CoveragePeriod(T0, T0 + timedelta(hours=2))


@dataclass
class Site:
    env: WriterEnvironment
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    user_id: uuid.UUID

    def run(self, awaitable: Any) -> Any:
        return self.env.loop.run(awaitable)


@pytest.fixture(scope="module")
def site(postgres_endpoint: PostgresEndpoint) -> Iterator[Site]:
    with (
        migrated_database(postgres_endpoint, "fs_nuc_09") as migrated,
        writer_environment(migrated, extra_types=COVERAGE_TYPES) as env,
    ):

        async def seed() -> Any:
            connection = await migrated.connect()
            try:
                return await seed_identity(connection)
            finally:
                await connection.close()

        tenant = env.loop.run(seed()).a
        yield Site(env, tenant.organization_id, tenant.plants[0].plant_id, tenant.user_id)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def _reader(organization_id: uuid.UUID) -> ScopeContext:
    actor = Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        display_name_snapshot="Coordinación SST sintética",
        unit=ActorUnit.U02,
        role_in_use=Role.COORDINATOR_SST,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.SESSION,
        allowed_scopes=[AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.ADMINISTRATOR)],
        correlation_id=uuid7(),
        session_id_hash=hashlib.sha256(os.urandom(8)).hexdigest(),
    )


async def _zone_with_node(site: Site, zone_id: uuid.UUID, node_id: uuid.UUID) -> None:
    """Zona, nodo y asignación desde antes del periodo (como superusuario, datos generados)."""
    connection = await site.env.migrated.connect()
    try:
        before = T0 - timedelta(days=1)
        await connection.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name,"
            " created_at, created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            site.organization_id,
            site.plant_id,
            f"ZS-{zone_id.hex[:8].upper()}",
            before,
            site.user_id,
        )
        await connection.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code,"
            " status, created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            node_id,
            site.organization_id,
            site.plant_id,
            f"NS-{node_id.hex[:8].upper()}",
            before,
        )
        await connection.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
            " plant_id, zone_id, node_id, assigned_at, unassigned_at, assigned_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, NULL, $7)",
            uuid.uuid4(),
            site.organization_id,
            site.plant_id,
            zone_id,
            node_id,
            before,
            site.user_id,
        )
    finally:
        await connection.close()


async def _project_communication(
    site: Site,
    node_id: uuid.UUID,
    state: str,
    since: datetime,
    heartbeat: datetime | None,
    record_id: uuid.UUID,
) -> None:
    """La proyección ``CommunicationState`` de la última declaración del nodo."""
    connection = await site.env.migrated.connect()
    try:
        await connection.execute(
            "INSERT INTO ledger.communication_state (node_id, organization_id, plant_id, state,"
            " since, last_heartbeat_at, source_record_id) VALUES ($1, $2, $3, $4, $5, $6, $7)"
            " ON CONFLICT (node_id) DO UPDATE SET state = EXCLUDED.state, since = EXCLUDED.since,"
            " last_heartbeat_at = EXCLUDED.last_heartbeat_at,"
            " source_record_id = EXCLUDED.source_record_id",
            node_id,
            site.organization_id,
            site.plant_id,
            state,
            since,
            heartbeat,
            record_id,
        )
    finally:
        await connection.close()


def _write(site: Site, record_type: str, document: dict[str, Any], **extra: Any) -> uuid.UUID:
    context = unit_context(site.organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
    receipt = site.run(site.env.writer.write(context, record_type, document, **extra))
    assert isinstance(receipt, Receipt), receipt
    return receipt.record_id


def _skewed_pair(seed: int, place: Place, opened_at: datetime, closed_at: datetime) -> list[Any]:
    """Un par del kit (sujeto ``zone``) con las marcas del nodo adelantadas 10 minutos."""
    strategy = (
        zone_catalog()
        .flatmap(observability_event_pair)
        .filter(lambda pair: pair[0]["subject"]["kind"] == "zone")
    )
    opened, closed = (dict(event) for event in draw_example(strategy, seed))
    opened_id, closed_id = uuid7(), uuid7()
    node_opened, node_closed = opened_at + SKEW, closed_at + SKEW
    for event, event_id, at in ((opened, opened_id, node_opened), (closed, closed_id, node_closed)):
        event.update(
            event_id=str(event_id),
            organization_id=str(place.organization_id),
            plant_id=str(place.plant_id),
            zone_id=str(place.zone_id),
            node_id=str(place.node_id),
            started_at=_stamp(node_opened),
            evidence=[],
            node_time={
                "started_at": _stamp(at),
                "ended_at": _stamp(at),
                "clock": {"synchronized": True, "offset_ms": SKEW_MS, "source": "ntp.local"},
            },
        )
        event.pop("receipt", None)
        event.pop("measurements", None)
    closed["opened_event_id"] = str(opened_id)
    closed["ended_at"] = _stamp(node_closed)
    return [opened, closed]


def test_fs_nuc_09_node_clock_skewed_ten_minutes(site: Site) -> None:
    with scenario(
        "FS-NUC-09",
        title="Reloj del nodo desviado",
        injection="nodo simulado con desviación de 10 min",
        expected=(
            "registros aceptados dentro de la tolerancia de BR-CTR-08; línea de tiempo con"
            " clock_basis = node; capa de comunicación con reloj de la plataforma; partición exacta"
        ),
    ) as run:
        zone_id, node_id = uuid.uuid4(), uuid.uuid4()
        site.run(_zone_with_node(site, zone_id, node_id))
        place = Place(site.organization_id, site.plant_id, zone_id, node_id)

        # Hallazgos del kit con el reloj del nodo 10 minutos adelantado: el expediente los acepta
        # y conserva la marca del nodo como la mandó.
        findings = kit_findings(run.random.getrandbits(32), place, run.random.randint(2, 4))
        for finding in findings:
            finding["node_time"]["clock"] = {
                "synchronized": True,
                "offset_ms": SKEW_MS,
                "source": "ntp.local",
            }
        for clip in all_clips(findings):
            site.env.storage.put(clip)
        finding_ids = [_write(site, "finding_received", finding) for finding in findings]

        # La plataforma, con su reloj: zona productiva y nodo alcanzable desde antes del periodo.
        before = T0 - timedelta(hours=1)
        gate = {
            "zone_id": str(zone_id),
            "plant_id": str(site.plant_id),
            "gate": "use",
            "status": "approved",
            "resulting_mode": "productive",
        }
        _write(site, "gate_state_changed", gate, occurred_at=before)
        reachable = _write(
            site,
            "node_communication_state_changed",
            {
                "node_id": str(node_id),
                "plant_id": str(site.plant_id),
                "state": "reachable",
                "since": _stamp(before),
            },
        )
        # El nodo: degradado de T0+20' a T0+50' en su reloj (T0+10' a T0+40' en el de la
        # plataforma); las marcas que llegan son las suyas.
        opened_at = T0 + timedelta(minutes=10 + run.random.randint(0, 10))
        closed_at = opened_at + timedelta(minutes=30)
        events = _skewed_pair(run.random.getrandbits(32), place, opened_at, closed_at)
        event_ids = [_write(site, "observability_event_received", event) for event in events]
        # La plataforma deja de oír al nodo: mudo desde su último latido, con su reloj.
        heartbeat = T0 + timedelta(minutes=90)
        mute = _write(
            site,
            "node_communication_state_changed",
            {
                "node_id": str(node_id),
                "plant_id": str(site.plant_id),
                "state": "mute",
                "since": _stamp(heartbeat + timedelta(minutes=3)),
                "last_heartbeat_at": _stamp(heartbeat),
            },
        )
        site.run(
            _project_communication(
                site, node_id, "mute", heartbeat + timedelta(minutes=3), heartbeat, mute
            )
        )

        coverage = CoverageService(database=site.env.database, audit=site.env.audit)
        timeline = site.run(
            coverage.linea_de_tiempo(_reader(site.organization_id), zone_id, PERIOD)
        )
        intervals = timeline.intervals
        node_layer = [i for i in intervals if i.layer is CoverageLayer.NODE_REPORT]
        platform_layer = [i for i in intervals if i.layer is CoverageLayer.PLATFORM_COMMUNICATION]
        run.observe(
            accepted_findings=len(finding_ids),
            accepted_events=len(event_ids),
            reachable_record=str(reachable),
            intervals=[
                {
                    "from": _stamp(i.starts_at),
                    "to": _stamp(i.ends_at),
                    "layer": i.layer.value,
                    "state": i.state.value,
                    "causes": list(i.causes),
                    "clock_basis": i.clock_basis.value,
                    "clock_offset_ms": i.clock_offset_ms,
                }
                for i in intervals
            ],
            summary_ms=timeline.summary.total_ms,
        )

        # Capa del nodo: su reloj y su desviación; el tramo degradado empieza en la marca del nodo.
        assert node_layer and all(i.clock_basis is ClockBasis.NODE for i in node_layer)
        degraded = [i for i in node_layer if i.state.value != "observable"]
        assert [i.starts_at for i in degraded] == [opened_at + SKEW]
        assert degraded[0].ends_at == closed_at + SKEW
        assert all(i.clock_offset_ms == SKEW_MS for i in timeline.node_intervals)
        # Capa de comunicación: el reloj de la plataforma; el mudo empieza en su último latido.
        assert platform_layer and all(i.clock_basis is ClockBasis.PLATFORM for i in platform_layer)
        assert any(
            i.starts_at == heartbeat and "no_communication" in i.causes for i in platform_layer
        )
        # Partición exacta de [a, b): contiguos, sin solapes, y el resumen suma b - a.
        assert intervals[0].starts_at == PERIOD.start and intervals[-1].ends_at == PERIOD.end
        for left, right in itertools.pairwise(intervals):
            assert left.ends_at == right.starts_at
        period_ms = int((PERIOD.end - PERIOD.start) / timedelta(milliseconds=1))
        assert timeline.summary.total_ms == period_ms
        assert sum(i.duration_ms for i in intervals) == period_ms
