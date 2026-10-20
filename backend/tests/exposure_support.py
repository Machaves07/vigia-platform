"""El indicador de exposición de TASK-322 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``exposure_stack`` construye, sobre el mundo de U-04 (``tests/laz_support.py``: hallazgos
ingresados por las rutas del contrato de U-03 y proyectados, catálogo publicado con su señal
``energy`` y compuerta de uso aprobada), el ``ExposureService`` real con los puertos reales de
U-03 (``CatalogQueryPort`` y ``GateQueryPort``) y la aplicación real (``create_app`` con la cadena
fija de middleware y ``ContextAuthorizer``) con ``AccreditationHttp`` en ``app.state``.

La línea de tiempo de U-02 es, por defecto, ``ScriptedCoverage``: observable en todo lo pedido
salvo los huecos que la prueba declara (``not_observable``), para que las cifras dependan solo de
lo que la prueba siembra. ``real_coverage`` cambia al ``CoverageService`` real.

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14); si ese instante cae cerca de un borde del mes local de la planta, el reloj avanza
(``_away_from_month_borders``) para que el periodo en curso no cambie a mitad de prueba. Topes de
la base: 60 s (retro 15).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, LoopWorld, loop_world
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.exposure.repository import ExposureRepository, StoredExposure
from vigia_platform.accreditation.exposure.service import ExposureService
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.coverage import CoverageService, CoverageTimeline
from vigia_platform.ledger.domain.coverage import (
    ClockBasis,
    CoverageInterval,
    CoverageLayer,
    CoveragePeriod,
    CoverageState,
    CoverageSummary,
)
from vigia_platform.loop.calendar import month_bounds, period_of
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database, Transaction

__all__ = ["PATH", "ExposureStack", "ScriptedCoverage", "Who", "exposure_stack"]

PATH: Final = "/accreditation/exposure"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
TIMEZONE: Final = "America/Bogota"
"""La de las plantas de ``tests/authz_support.py``."""
_MS: Final = timedelta(milliseconds=1)

Who = SessionCookie


@dataclass
class ScriptedCoverage:
    """``CoveragePort.linea_de_tiempo`` guionado: observable salvo ``gaps`` (no observables)."""

    gaps: list[tuple[datetime, datetime]] = field(default_factory=list)
    calls: int = 0

    async def linea_de_tiempo(
        self, context: ScopeContext, zone_id: uuid.UUID, period: CoveragePeriod
    ) -> CoverageTimeline:
        self.calls += 1
        cuts = sorted(
            (max(start, period.start), min(end, period.end))
            for start, end in self.gaps
            if start < period.end and end > period.start
        )
        intervals: list[CoverageInterval] = []
        cursor = period.start
        for start, end in cuts:
            if start > cursor:
                intervals.append(_interval(cursor, start, CoverageState.OBSERVABLE))
            if end > max(start, cursor):
                intervals.append(_interval(max(start, cursor), end, CoverageState.NOT_OBSERVABLE))
            cursor = max(cursor, end)
        if cursor < period.end:
            intervals.append(_interval(cursor, period.end, CoverageState.OBSERVABLE))
        observable = sum(i.duration_ms for i in intervals if i.state is CoverageState.OBSERVABLE)
        hidden = sum(i.duration_ms for i in intervals if i.state is not CoverageState.OBSERVABLE)
        return CoverageTimeline(
            organization_id=context.organization_id,
            plant_id=uuid.UUID(int=0),
            zone_id=zone_id,
            period=period,
            intervals=tuple(intervals),
            node_intervals=(),
            summary=CoverageSummary(observable_ms=observable, not_observable_ms=hidden),
        )


def _interval(start: datetime, end: datetime, state: CoverageState) -> CoverageInterval:
    return CoverageInterval(
        starts_at=start,
        ends_at=end,
        layer=CoverageLayer.NODE_REPORT,
        state=state,
        causes=() if state is CoverageState.OBSERVABLE else ("camera_offline",),
        subject=None,
        source_record_ids=(),
        clock_basis=ClockBasis.PLATFORM,
    )


class GatedRepository(ExposureRepository):
    """``ExposureRepository`` cuyo ``save`` espera a que lleguen ``parties`` guardados a la vez:
    fuerza que dos cálculos del mismo periodo escriban la fila simultáneamente."""

    def __init__(self, parties: int) -> None:
        self.barrier = asyncio.Barrier(parties)

    async def save(
        self,
        transaction: Transaction,
        *,
        values: Any,
        definitive: bool,
        period_end: datetime,
    ) -> StoredExposure | None:
        await asyncio.wait_for(self.barrier.wait(), timeout=60)
        return await super().save(
            transaction, values=values, definitive=definitive, period_end=period_end
        )


@dataclass
class ExposureStack:
    world: LoopWorld
    database: Database
    coverage: ScriptedCoverage
    client: httpx.AsyncClient
    http: AccreditationHttp

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.world.stack.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.world.stack.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.world.stack.execute(sql, *args)

    def now(self) -> datetime:
        return self.world.stack.now()

    def advance(self, seconds: float) -> None:
        self.world.stack.tick(seconds)

    def period(self, moment: datetime | None = None) -> str:
        """El mes ``AAAA-MM`` de ``moment`` (por defecto, ahora) en la zona de la planta."""
        return period_of(moment if moment is not None else self.now(), TIMEZONE)

    def bounds(self, period: str) -> tuple[datetime, datetime]:
        return month_bounds(TIMEZONE, period)

    def service(self, *, repository: ExposureRepository | None = None) -> ExposureService:
        """Otro ``ExposureService`` sobre la misma base, la misma cobertura y el mismo reloj."""
        authz = self.world.stack.authz
        ports = catalog_query_ports(self.database)
        return ExposureService(
            database=self.database,
            authorizer=authz.authorizer,
            catalog=ports.catalog,
            gates=ports.gates,
            coverage=self.coverage,
            settings=PostgresLoopConfigurationRepository(self.database),
            clock=authz.sessions.clock,
            repository=repository,
        )

    def real_coverage(self) -> CoverageService:
        authz = self.world.stack.authz
        return CoverageService(
            database=self.database, audit=authz.sessions.audit, clock=authz.sessions.clock
        )

    # --- personas -----------------------------------------------------------------------------

    def member(
        self,
        organization: LoopOrganization,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> Who:
        authz = self.world.stack.authz
        user = authz.add_user(organization.organization_id)
        authz.assign(organization.organization_id, user, role, level, scope)
        cookie: SessionCookie = authz.open_session(organization.organization_id, user)
        return cookie

    def context(self, organization: LoopOrganization) -> ScopeContext:
        return self.world.context(organization)

    # --- siembra ------------------------------------------------------------------------------

    def finding(
        self,
        organization: LoopOrganization,
        *,
        started: datetime,
        duration: timedelta,
        confirmed: bool = True,
        **changes: Any,
    ) -> IngestedSubject:
        """Un hallazgo ingresado y proyectado; con ``confirmed``, clasificado ``confirmed`` por el
        escritor real (si no, queda pendiente de clasificar)."""
        finding = self.world.ingest_finding(
            organization, started=started, duration=duration, **changes
        )
        step = self.world.project(finding)
        assert step.applied, step
        if confirmed:
            content = self.world.classification_content(finding)
            receipt, applied = self.world.write(organization, "classification", content)
            assert applied is not None and applied.applied, (receipt, applied)
        return finding

    def zone_without_catalog(self, organization: LoopOrganization) -> uuid.UUID:
        """Otra zona de la planta del sitio, sin catálogo publicado ni nodo."""
        zone_id = uuid.uuid4()
        self.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
            " created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            organization.organization_id,
            organization.plant_id,
            f"ZN-{uuid.uuid4().hex[:12].upper()}",
            self.now(),
            self.world.stack.authz.operator_id,
        )
        return zone_id

    def set_signal_roles(self, organization: LoopOrganization, role: str) -> None:
        """Una versión nueva del catálogo de la zona del sitio con todas sus señales en ``role``
        (como ``LoopWorld.set_single_occupancy``: la anterior queda sustituida en este instante);
        el reloj avanza un segundo después, para que la versión ya rija al consultar."""
        self.advance(1)
        now = self.now()
        (current,) = self.fetch(
            "SELECT catalog_version FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 AND superseded_at IS NULL",
            organization.zone_id,
        )
        version = int(current["catalog_version"])
        self.execute(
            "UPDATE catalog.zone_catalog_version SET superseded_at = $3"
            " WHERE zone_id = $1 AND catalog_version = $2",
            organization.zone_id,
            version,
            now,
        )
        self.execute(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, aggregation_window_minutes, ledger_record_id)"
            " SELECT organization_id, plant_id, zone_id, catalog_version + 1, $3, issued_by,"
            " role_in_use, reason_es, ARRAY['signals'],"
            " jsonb_set(jsonb_set(payload, '{version}', to_jsonb(catalog_version + 1)),"
            " '{signals}', COALESCE((SELECT jsonb_agg(jsonb_set(s, '{role}', to_jsonb($4::text)))"
            " FROM jsonb_array_elements(payload -> 'signals') AS s), '[]'::jsonb)),"
            " jsonb_set(envelope, '{payload,version}', to_jsonb(catalog_version + 1)),"
            " single_occupancy, aggregation_window_minutes, $5 FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 AND catalog_version = $2",
            organization.zone_id,
            version,
            now,
            role,
            uuid.uuid4(),
        )
        self.advance(1)

    def set_single_occupancy(self, organization: LoopOrganization, window: int) -> None:
        """La zona del sitio pasa a unipersonal con ``window`` (y el reloj avanza un segundo)."""
        self.world.set_single_occupancy(organization, True, window)
        self.advance(1)

    def clone_episode(self, finding: IngestedSubject, zone_id: uuid.UUID) -> uuid.UUID:
        """Copia el episodio de ``finding`` y su fila de estado a ``zone_id`` (otra zona de la
        misma planta), como superusuario; devuelve el episodio nuevo."""
        episode = self.world.episode(finding)
        assert episode is not None
        episode_id = uuid.uuid4()
        values = {**episode, "episode_id": episode_id, "zone_id": zone_id}
        names = list(values)
        placeholders = ", ".join(f"${index}" for index in range(1, len(names) + 1))
        self.execute(
            f"INSERT INTO loop.episode ({', '.join(names)}) VALUES ({placeholders})",  # noqa: S608
            *values.values(),
        )
        state = self.world.finding_state(finding)
        assert state is not None
        values = {
            **state,
            "finding_state_id": uuid.uuid4(),
            "subject_id": episode_id,
            "zone_id": zone_id,
        }
        names = list(values)
        placeholders = ", ".join(f"${index}" for index in range(1, len(names) + 1))
        self.execute(
            f"INSERT INTO loop.finding_state ({', '.join(names)})"  # noqa: S608
            f" VALUES ({placeholders})",
            *values.values(),
        )
        return episode_id

    def rows(self, zone_id: uuid.UUID) -> list[dict[str, Any]]:
        """Las filas de ``accreditation.exposure_indicator`` de la zona (como superusuario)."""
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM accreditation.exposure_indicator WHERE zone_id = $1 ORDER BY period",
                zone_id,
            )
        ]

    # --- peticiones ---------------------------------------------------------------------------

    def get(self, who: Who, params: Any) -> httpx.Response:
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={who.value}"}
        response: httpx.Response = self.run(self.client.get(PATH, params=params, headers=headers))
        return response


def _away_from_month_borders(world: LoopWorld) -> None:
    """Las pruebas siembran episodios unos minutos antes de ahora y avanzan el reloj hasta media
    hora: lejos de los dos bordes del mes local, el periodo en curso no cambia a mitad de prueba.
    Si faltan menos de 2 horas para el fin del mes, el reloj salta a la segunda hora del siguiente;
    si el mes empezó hace menos de 1 hora, avanza 1 hora."""
    now = world.stack.now()
    start, end = month_bounds(TIMEZONE, period_of(now, TIMEZONE))
    if end - now < timedelta(hours=2):
        world.stack.tick((end - now).total_seconds() + 3600)
    elif now - start < timedelta(hours=1):
        world.stack.tick(3600)


@contextlib.contextmanager
def exposure_stack(endpoint: PostgresEndpoint, prefix: str) -> Iterator[ExposureStack]:
    with loop_world(endpoint, prefix) as world:
        _away_from_month_borders(world)
        stack = world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = stack.database()
        coverage = ScriptedCoverage()
        ports = catalog_query_ports(database)
        http = AccreditationHttp(
            exposure=ExposureService(
                database=database,
                authorizer=authz.authorizer,
                catalog=ports.catalog,
                gates=ports.gates,
                coverage=coverage,
                settings=PostgresLoopConfigurationRepository(database),
                clock=clock,
            )
        )
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(stack.primary.writer),
                    clock=clock,
                ),
                "state": {ACCREDITATION_STATE_KEY: http},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = ExposureStack(world, database, coverage, client, http)
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())


def ms(value: timedelta) -> int:
    return value // _MS
