"""La adopción por zona de TASK-323 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``adoption_stack`` construye, sobre ``actions_stack`` (TASK-311: hallazgos ingresados por la ruta
del contrato de U-03, clasificados, con acción, evidencia y cierre por los servicios reales, que
escriben por ``LoopProjectionStore.write_and_apply`` y con ello alimentan el contador), la tarea
real ``adoption_check`` (``AdoptionCheck`` con ``ProductiveZones`` sobre la proyección de
compuertas de U-03, la configuración del lazo y la bandeja real, en el contexto de iteración del
planificador, ``context_for_organization``), la consulta ``AdoptionReads`` y una aplicación real
(``create_app`` con la cadena fija de middleware y ``ContextAuthorizer``) con ``AccreditationHttp``
para ``GET /accreditation/adoption``.

``AdoptionStack`` da las zonas nuevas, su activación y su retirada (filas de
``catalog.zone_gate_state`` con las dos compuertas decididas), el reloj llevado a una hora local de
la planta, las ejecuciones de la tarea, la actividad registrada por el gancho y lo que quedó
escrito (contador, marcas y eventos ``adoption_low``).

Solo datos generados. Un solo reloj (retro 15): las marcas salen del reloj simulado del mundo, y
ninguna prueba compara con la hora de la base. Topes de las esperas de la base: 60 s.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx

from tests.api_support import World
from tests.laz_support import LoopOrganization
from tests.loop_actions_support import SAME_ORIGIN, ActionsStack, Who, actions_stack
from vigia_platform.accreditation.adapters.http.adoption_routes import PATH as ADOPTION_PATH
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.adoption.counter import write_activity
from vigia_platform.accreditation.adoption.reads import AdoptionReads
from vigia_platform.accreditation.adoption.task import (
    AdoptionAlert,
    AdoptionCheck,
    ProductiveZones,
    ZoneActivations,
    register_adoption_check,
)
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.domain.enums import LoopActivityKind
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = ["CONFIGURATION_REASON", "AdoptionStack", "Zone", "adoption_stack"]

CONFIGURATION_REASON: Final = "Ajuste del umbral de adopción acordado en el comité de seguridad"
DAY: Final = timedelta(days=1)
WEEK: Final = timedelta(days=7)
COUNTER_COLUMNS: Final = frozenset(
    {
        "organization_id",
        "plant_id",
        "zone_id",
        "day",
        "activity_counts",
        "session_count",
        "last_activity_at",
    }
)
"""Las columnas de ``accreditation.adoption_counter``: ninguna de usuario (H-33)."""
ALERT_COLUMNS: Final = frozenset(
    {
        "organization_id",
        "plant_id",
        "zone_id",
        "streak_start",
        "silent_days",
        "threshold_days",
        "evaluated_day",
        "emitted_at",
    }
)


@dataclass(frozen=True)
class Zone:
    """Una zona de la planta del sitio de una organización."""

    organization: LoopOrganization
    zone_id: uuid.UUID

    @property
    def organization_id(self) -> uuid.UUID:
        return self.organization.organization_id

    @property
    def plant_id(self) -> uuid.UUID:
        return self.organization.plant_id


@dataclass
class AdoptionStack:
    actions: ActionsStack
    check: AdoptionCheck
    task: PeriodicTask
    reads: AdoptionReads
    client: httpx.AsyncClient

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.actions.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.actions.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.actions.execute(sql, *args)

    def now(self) -> datetime:
        now: datetime = self.actions.now()
        return now

    def tick(self, seconds: float = 1.0) -> None:
        self.actions.tick(seconds)

    def timezone(self, organization: LoopOrganization) -> str:
        (row,) = self.fetch(
            "SELECT timezone FROM identity.plant WHERE plant_id = $1", organization.plant_id
        )
        return str(row["timezone"])

    def local_today(self, organization: LoopOrganization) -> date:
        return self.now().astimezone(ZoneInfo(self.timezone(organization))).date()

    def to_local(self, organization: LoopOrganization, day: date, hour: int) -> None:
        """Lleva el reloj hasta ``hour``:00 del día local ``day`` de la planta (nunca atrás)."""
        zone = ZoneInfo(self.timezone(organization))
        moment = datetime.combine(day, time(hour), tzinfo=zone)
        delta = (moment - self.now()).total_seconds()
        assert delta >= 0, (moment, self.now())
        if delta > 0:
            self.tick(delta)

    def next_morning(self, organization: LoopOrganization, hour: int = 1) -> date:
        """Lleva el reloj a ``hour``:00 del día local siguiente y devuelve ese día."""
        day = self.local_today(organization) + DAY
        self.to_local(organization, day, hour)
        return day

    # --- organizaciones, zonas y compuertas -----------------------------------------------------

    def organization(self) -> LoopOrganization:
        return self.actions.world.organization()

    def site_zone(self, organization: LoopOrganization) -> Zone:
        return Zone(organization, organization.zone_id)

    def add_zone(self, organization: LoopOrganization) -> Zone:
        zone_id = uuid.uuid4()
        self.actions.world.stack.authz.add_zone(
            organization.organization_id, organization.plant_id, zone_id
        )
        return Zone(organization, zone_id)

    def _gate_row(self, zone: Zone, usage_status: str, at: datetime) -> None:
        operator = self.actions.world.stack.authz.operator_id
        # El montaje se aprobó una hora antes que el uso: la activación es la última de las dos.
        mounting = {
            "status": "approved",
            "decided_at": format_timestamp(at - timedelta(hours=1)),
            "record_id": str(uuid.uuid4()),
            "decided_by": str(operator),
        }
        usage = {
            "status": usage_status,
            "decided_at": format_timestamp(at),
            "agreement_id": str(uuid.uuid4()),
            "decided_by": str(operator),
        }
        mode = "productive" if usage_status == "approved" else "commissioning"
        self.execute(
            "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
            " usage, resulting_mode, issued_at, envelope, valid_until)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, '{}', $8)"
            " ON CONFLICT (zone_id) DO UPDATE SET mounting = EXCLUDED.mounting,"
            " usage = EXCLUDED.usage, resulting_mode = EXCLUDED.resulting_mode,"
            " issued_at = EXCLUDED.issued_at, valid_until = EXCLUDED.valid_until",
            zone.zone_id,
            zone.organization_id,
            zone.plant_id,
            json.dumps(mounting),
            json.dumps(usage),
            mode,
            at,
            at + WEEK,
        )

    def activate(self, zone: Zone) -> datetime:
        """Montaje y uso aprobados ahora (la zona pasa a ``productive``); devuelve el instante."""
        at = self.now()
        self._gate_row(zone, "approved", at)
        return at

    def commissioning(self, zone: Zone) -> None:
        """Montaje aprobado y uso pendiente: la zona no está activa."""
        self.execute(
            "INSERT INTO catalog.zone_gate_state (zone_id, organization_id, plant_id, mounting,"
            " usage, resulting_mode, issued_at, envelope, valid_until)"
            " VALUES ($1, $2, $3, $4, '{\"status\": \"pending\"}', 'commissioning', $5, '{}', $6)",
            zone.zone_id,
            zone.organization_id,
            zone.plant_id,
            json.dumps(
                {
                    "status": "approved",
                    "decided_at": format_timestamp(self.now()),
                    "record_id": str(uuid.uuid4()),
                    "decided_by": str(self.actions.world.stack.authz.operator_id),
                }
            ),
            self.now(),
            self.now() + WEEK,
        )

    def revoke_usage(self, zone: Zone) -> None:
        """La compuerta de uso revocada: la zona deja de estar activa."""
        self._gate_row(zone, "revoked", self.now())

    # --- configuración ------------------------------------------------------------------------

    def manager(self, organization: LoopOrganization) -> Who:
        return self.actions.member(organization, Role.PLANT_MANAGER)

    def set_silence_days(self, organization: LoopOrganization, days: int) -> None:
        """``adoption_silence_days`` de la planta por ``loop.config`` (TASK-304)."""
        self.tick()
        self.run(
            self.actions.configuration.change(
                self.actions.context(self.manager(organization)),
                organization.plant_id,
                integers={"adoption_silence_days": days},
                business_calendar=None,
                reason=CONFIGURATION_REASON,
            )
        )

    # --- la tarea -----------------------------------------------------------------------------

    def periodic_context(self, organization: LoopOrganization) -> ScopeContext:
        """El contexto que el planificador da a la tarea en ``organization`` (BR-NUC-03)."""
        contexts = self.actions.world.stack.authz.contexts
        context: ScopeContext = contexts.context_for_organization(
            self.task, organization.organization_id
        )
        return context

    def check_like(self, activations: ZoneActivations) -> AdoptionCheck:
        """Otra ``AdoptionCheck`` sobre la misma base con otras activaciones (con ganchos)."""
        world = self.actions.world
        return AdoptionCheck(
            activations=activations,
            configuration=PostgresLoopConfigurationRepository(self.actions.database),
            outbox=world.stack.outbox,
            clock=world.stack.authz.sessions.clock,
        )

    def real_activations(self) -> ProductiveZones:
        return ProductiveZones()

    async def check_async(
        self, organization: LoopOrganization, check: AdoptionCheck | None = None
    ) -> tuple[AdoptionAlert, ...]:
        chosen = check if check is not None else self.check
        async with self.actions.database.transaction(
            self.periodic_context(organization)
        ) as transaction:
            return await chosen.run(transaction)

    def run_check(
        self, organization: LoopOrganization, check: AdoptionCheck | None = None
    ) -> tuple[AdoptionAlert, ...]:
        """Una ejecución de ``adoption_check`` en ``organization``, como la del planificador."""
        alerts: tuple[AdoptionAlert, ...] = self.run(self.check_async(organization, check))
        return alerts

    # --- la actividad -------------------------------------------------------------------------

    async def record_async(self, zone: Zone, record_type: str) -> LoopActivityKind | None:
        """El gancho de las escrituras (``write_activity``) en una transacción de la organización,
        como la de una escritura del lazo, con la hora del reloj."""
        context = self.actions.world.context(zone.organization)
        async with self.actions.database.transaction(context) as transaction:
            return await write_activity(
                transaction,
                record_type=record_type,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                subject_id=None,
                at=self.now(),
            )

    def record(self, zone: Zone, record_type: str = "classification") -> LoopActivityKind | None:
        kind: LoopActivityKind | None = self.run(self.record_async(zone, record_type))
        return kind

    # --- lo que quedó escrito ------------------------------------------------------------------

    def counter_rows(self, zone: Zone) -> list[dict[str, Any]]:
        rows = self.fetch(
            "SELECT organization_id, plant_id, zone_id, day, activity_counts::text AS counts,"
            " session_count, last_activity_at FROM accreditation.adoption_counter"
            " WHERE organization_id = $1 AND zone_id = $2 ORDER BY day",
            zone.organization_id,
            zone.zone_id,
        )
        return [
            {**dict(row), "activity_counts": json.loads(row["counts"]), "counts": None}
            for row in rows
        ]

    def alert_rows(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM accreditation.adoption_alert WHERE organization_id = $1"
                " ORDER BY zone_id, streak_start",
                organization.organization_id,
            )
        ]

    def adoption_events(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        """Los ``adoption_low`` de la organización (carga parseada y planta), en orden."""
        return [
            {"plant_id": row["plant_id"], **json.loads(row["payload"])}
            for row in self.fetch(
                "SELECT plant_id, payload::text AS payload FROM shared.outbox_event"
                " WHERE organization_id = $1 AND event_name = 'adoption_low'"
                " ORDER BY created_at, publish_seq",
                organization.organization_id,
            )
        ]

    def columns(self, table: str) -> frozenset[str]:
        rows = self.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'accreditation' AND table_name = $1",
            table,
        )
        return frozenset(str(row["column_name"]) for row in rows)

    # --- peticiones ---------------------------------------------------------------------------

    def get_adoption(self, who: Who, params: Mapping[str, Any]) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers["X-Vigia-Concession"] = str(concession)
        response: httpx.Response = self.run(
            self.client.get(ADOPTION_PATH, params=dict(params), headers=headers)
        )
        return response


@contextlib.contextmanager
def adoption_stack(endpoint: Any, prefix: str) -> Iterator[AdoptionStack]:
    with actions_stack(endpoint, prefix) as actions:
        stack = actions.world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = actions.database
        configuration = PostgresLoopConfigurationRepository(database)
        activations = ProductiveZones()
        check = AdoptionCheck(
            activations=activations,
            configuration=configuration,
            outbox=stack.outbox,
            clock=clock,
        )
        task = register_adoption_check(PeriodicTaskRegistry(), check)
        reads = AdoptionReads(
            database=database,
            authorizer=authz.authorizer,
            activations=activations,
            configuration=configuration,
            clock=clock,
        )
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(actions.writer),
                    clock=clock,
                ),
                "state": {ACCREDITATION_STATE_KEY: AccreditationHttp(adoption=reads)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        try:
            yield AdoptionStack(actions, check, task, reads, client)
        finally:
            stack.run(client.aclose())
