"""Prueba de oclusión sobre PostgreSQL 16 real (TASK-215, LC-GOB-07; BR-GOB-41 a 43, NFR-GOB-44).

Servicios reales como ``vigia_app`` (``tests/walk_test_support.py``) con ``OcclusionService``,
``LectorExpediente`` real y la aplicación real para las respuestas HTTP. Los eventos de
observabilidad se escriben por ``EscritorExpediente`` como ``observability_event_received`` con
el contenido del contrato (la ingesta es de TASK-221).

**Relojes.** ``received_at`` es la hora de la base al escribir el evento; la ventana de cada
prueba se calcula **desde esa marca** (``R - 10 s``, ``R - 4 min``, ``R - 6 min``), así que la
recepción relativa a ``ended_at`` no depende de cuánto se haya adelantado el reloj simulado. El
reloj simulado de la oclusión es propio: cada caso lo pone en la hora de la base (``catch_up``) y
lo adelanta (``advance``); el de sesiones, claves y concesiones no se toca.

- **NFR-GOB-44**: recibido a los 10 s y a los 4 min de ``ended_at``, ``verified``; a los 6 min,
  ``failed`` con ``no_observability_events_in_window``.
- **Alcance de nodo**: sus copias en varias zonas no cuentan ni impiden ``verified``.
- **Concurrencia** (barreras, ningún tope de pared decide): N reevaluaciones simultáneas dejan una
  resolución y un ``occlusion_test_result``; N altas simultáneas de la misma cámara dejan una
  prueba; una declaración y una reevaluación a la vez nunca se interbloquean.
- **Declaración**: solo antes de ``deadline`` y sin eventos contados; vencida y sin eventos queda
  ``failed`` y se admite otra prueba.
- **Guarda de alcance**: los eventos de otra zona o de otra organización no cuentan.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest

from tests.agreements_support import CONCESSION_HEADER, SAME_ORIGIN
from tests.api_support import World
from tests.authz_support import Site
from tests.integration.conftest import PostgresEndpoint
from tests.walk_test_support import Mounted, WalkTestWorld, standards, walk_test_world
from tests.writer_support import unit_context
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.occlusion_repository import (
    PostgresOcclusionRepository,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.application.occlusion import OcclusionService
from vigia_platform.catalog.application.walk_test import WalkTestConflict
from vigia_platform.catalog.domain.enums import OcclusionVerification
from vigia_platform.catalog.domain.occlusion import OcclusionTest
from vigia_platform.catalog.domain.walk_test import WalkTestSession
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.fleet.record_types import FLEET_RECORD_TYPES
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ActorKind, ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp

pytestmark = pytest.mark.integration

CONCURRENT: Final = 5
"""Transacciones a la vez (el pool de prueba tiene 8 conexiones)."""
BARRIER_SECONDS: Final = 60.0
"""Tope de la barrera (retro 15: nunca decide el resultado, solo evita colgar la corrida)."""
REASON: Final = "El nodo no envió eventos mientras se tapaba la cámara"
EXTRA_TYPES: Final = (
    *(d for d in CATALOG_RECORD_TYPES if d.record_type == "occlusion_test_result"),
    *(d for d in FLEET_RECORD_TYPES if d.record_type == "observability_event_received"),
)


# --- Entorno -------------------------------------------------------------------------------------


@dataclass
class Zone:
    """Una zona montada con su sesión de walk-test abierta y sus cámaras."""

    mounted: Mounted
    session: WalkTestSession
    cameras: tuple[uuid.UUID, ...]
    node: uuid.UUID

    @property
    def site(self) -> Site:
        return self.mounted.site


class OcclusionWorld:
    def __init__(self, walk: WalkTestWorld) -> None:
        self.walk = walk
        g = walk.a.g
        # Reloj propio de la oclusión y de la sesión de walk-test: cada prueba lo pone en la hora
        # de la base (``catch_up``) y lo adelanta; el de sesiones y concesiones no se toca.
        self.clock = SimulatedClock(self.db_now())
        self.reader = LectorExpediente(database=g.database, audit=g.authz.sessions.audit)
        self.service = self.build()
        walk.service = walk.build(occlusions=self.service, clock=self.clock)
        self.client = self._install()

    def build(self, **changes: Any) -> OcclusionService:
        g = self.walk.a.g
        fields: dict[str, Any] = {
            "repository": PostgresOcclusionRepository(),
            "sessions": PostgresWalkTestRepository(),
            "catalog": PostgresCatalogRepository(g.database),
            "gates": g.gates,
            "reader": self.reader,
            "database": g.database,
            "writer": g.writer,
            "free_text": g.free_text,
            "clock": self.clock,
        }
        fields.update(changes)
        return OcclusionService(**fields)

    def _install(self) -> httpx.AsyncClient:
        g = self.walk.a.g
        authz = g.authz
        app = World(clock=authz.sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(g.writer),
                    clock=authz.sessions.clock,
                ),
                "state": {
                    CATALOG_STATE_KEY: CatalogHttp(
                        gates=g.gates, walk_tests=self.walk.service, occlusions=self.service
                    )
                },
            },
        )
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.walk.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.walk.fetch(sql, *args)

    def db_now(self) -> datetime:
        (row,) = self.fetch("SELECT clock_timestamp() AS now")
        now: datetime = row["now"]
        return now

    def catch_up(self) -> None:
        """El reloj de la oclusión, en la hora de la base más un segundo (al empezar cada caso)."""
        self.clock.set(self.db_now() + timedelta(seconds=1))

    def advance(self, by: timedelta) -> None:
        self.clock.advance(by.total_seconds())

    def request(self, method: str, path: str, mounted: Mounted, body: Any = None) -> httpx.Response:
        headers = {
            **SAME_ORIGIN,
            "Cookie": f"{SESSION_COOKIE_NAME}={mounted.cookie.value}",
            CONCESSION_HEADER: str(mounted.concession),
        }
        response: httpx.Response = self.run(
            self.client.request(method, path, json=body, headers=headers)
        )
        return response

    # --- Zonas y eventos -----------------------------------------------------------------------

    def site(self, zones: int = 1) -> Site:
        return self.walk.a.g.site(zones=zones)

    def zone(
        self,
        *,
        cameras: int = 2,
        required: int = 0,
        required_count: int = 1,
        site: Site | None = None,
        index: int = 0,
    ) -> Zone:
        """Zona ``index`` de ``site`` con ``cameras`` cámaras (las ``required`` primeras
        requeridas) y su sesión de walk-test abierta."""
        a = self.walk.a
        g = a.g
        site = site or g.site()
        plant, zone_id = site.zones()[index]
        camera_ids = tuple(uuid.uuid4() for _ in range(cameras))
        catalog = {
            "standards": standards(1),
            "cameras": [{"camera_id": str(c)} for c in camera_ids],
            "minimum_coverage": {
                "required_count": required_count,
                "required_camera_ids": [str(c) for c in camera_ids[:required]],
            },
        }
        installer, cookie, concession = a.installer_session(site)
        a.mount(site, plant, zone_id, installer, catalog)
        mounted = Mounted(site, plant, zone_id, installer, cookie, concession, catalog)
        session = self.walk.open(mounted)
        (node,) = self.fetch(
            "SELECT node_id FROM identity.zone_node_assignment"
            " WHERE zone_id = $1 AND unassigned_at IS NULL",
            zone_id,
        )
        return Zone(mounted, session, camera_ids, uuid.UUID(str(node["node_id"])))

    def observe(
        self,
        zone: Zone,
        *,
        started_at: datetime,
        kind: str = "camera",
        camera: uuid.UUID | None = None,
        state: str = "degraded",
        causes: tuple[str, ...] = ("obstruction",),
        node: uuid.UUID | None = None,
    ) -> Receipt:
        """``observability_event_received`` de la zona con el contenido del contrato; devuelve su
        recibo."""
        organization = zone.site.organization_id
        subject: dict[str, Any] = {"kind": kind}
        if kind == "camera":
            subject["camera_id"] = str(camera or zone.cameras[0])
        stamp = format_timestamp(started_at)
        content = {
            "event_id": str(uuid7(self.clock)),
            "contract_version": "1.0.0",
            "organization_id": str(organization),
            "plant_id": str(zone.mounted.plant),
            "zone_id": str(zone.mounted.zone),
            "node_id": str(node or zone.node),
            "subject": subject,
            "phase": "opened",
            "state": state,
            "causes": list(causes),
            "started_at": stamp,
            "node_time": {
                "started_at": stamp,
                "ended_at": stamp,
                "clock": {"synchronized": True, "offset_ms": 0, "source": "ntp.local"},
            },
            "evidence": [],
            "software_version": "1.0.0",
            "receipt": {
                "platform_record_id": str(uuid.uuid4()),
                "received_at": stamp,
                "status": "accepted",
            },
        }
        context = unit_context(organization, ActorUnit.U03, kind=ActorKind.SYSTEM)
        receipt = self.run(
            self.walk.a.g.writer.write(context, "observability_event_received", content)
        )
        assert isinstance(receipt, Receipt), receipt
        return receipt

    # --- Pruebas -------------------------------------------------------------------------------

    def post(
        self,
        zone: Zone,
        camera: uuid.UUID,
        ended_at: datetime,
        *,
        length: timedelta = timedelta(seconds=20),
        reason: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "camera_id": str(camera),
            "started_at": format_timestamp(ended_at - length),
            "ended_at": format_timestamp(ended_at),
        }
        if reason is not None:
            body["declared_reason_es"] = reason
        return self.request(
            "POST", f"/walk-tests/{zone.session.session_id}/occlusion-tests", zone.mounted, body
        )

    def current(self, zone: Zone) -> list[dict[str, Any]]:
        response = self.request(
            "GET", f"/zones/{zone.mounted.zone}/walk-tests/current", zone.mounted
        )
        body = _ok(response)
        tests: list[dict[str, Any]] = body["session"]["occlusion_tests"]
        return tests

    def reevaluate(self, zone: Zone, now: datetime | None = None) -> tuple[OcclusionTest, ...]:
        tests: tuple[OcclusionTest, ...] = self.run(
            self.service.reevaluate_pending(
                zone.mounted.installer, zone.session, now or self.clock.now()
            )
        )
        return tests

    def rows(self, zone: Zone) -> list[Any]:
        return self.fetch(
            "SELECT test_id, camera_id, verification, correlated_event_ids, declared_reason_es,"
            " ledger_record_id FROM catalog.occlusion_test WHERE session_id = $1 ORDER BY test_id",
            zone.session.session_id,
        )

    def results(self, zone: Zone) -> list[dict[str, Any]]:
        return [
            {**dict(r), "content": json.loads(r["content"])}
            for r in self.walk.a.g.records_of(zone.mounted.zone)
            if r["record_type"] == "occlusion_test_result"
        ]


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[OcclusionWorld]:
    with walk_test_world(postgres_endpoint, "occlusion", EXTRA_TYPES) as walk:
        world = OcclusionWorld(walk)
        try:
            yield world
        finally:
            walk.run(world.client.aclose())


def _ok(response: httpx.Response, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    return body


def _error(response: httpx.Response, status: int, code: str, detail: str | None = None) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["code"], body.get("detail_code")) == (code, detail), body


class _Barrier:
    """Reúne ``parties`` corrutinas antes de la fase que se quiere hacer chocar."""

    def __init__(self, parties: int) -> None:
        self._barrier = asyncio.Barrier(parties)

    async def wait(self) -> None:
        async with asyncio.timeout(BARRIER_SECONDS):
            await self._barrier.wait()


# --- NFR-GOB-44 ----------------------------------------------------------------------------------


@pytest.mark.parametrize("after_end", [timedelta(seconds=10), timedelta(minutes=4)])
def test_nfr_gob_44_a_camera_event_received_in_time_verifies(
    world: OcclusionWorld, after_end: timedelta
) -> None:
    zone = world.zone()
    camera = zone.cameras[1]
    world.catch_up()
    # El nodo vio la oclusión 10 s antes del fin de la ventana; la plataforma lo recibe en R.
    approx = world.db_now()
    receipt = world.observe(
        zone, started_at=approx - after_end - timedelta(seconds=10), camera=camera
    )
    ended_at = receipt.received_at - after_end
    body = _ok(world.post(zone, camera, ended_at), 201)
    assert body["deadline"] == format_timestamp(ended_at + timedelta(minutes=5))
    # Zona redundante: el silencio de la zona solo se afirma vencida la fecha límite (P2).
    assert world.clock.now() <= ended_at + timedelta(minutes=5)
    assert (body["verification"], body["correlated_event_ids"]) == ("pending", [])
    world.advance(timedelta(minutes=6))
    (test,) = world.current(zone)
    assert test["verification"] == "verified", test
    assert len(test["correlated_event_ids"]) == 1
    (result,) = world.results(zone)
    assert result["source_key"] == test["test_id"]
    assert result["content"]["verification"] == "verified"
    assert result["content"]["correlated_event_ids"] == test["correlated_event_ids"]


def test_nfr_gob_44_a_camera_event_received_at_six_minutes_fails(world: OcclusionWorld) -> None:
    zone = world.zone()
    camera = zone.cameras[1]
    world.catch_up()
    approx = world.db_now()
    receipt = world.observe(
        zone, started_at=approx - timedelta(minutes=6, seconds=10), camera=camera
    )
    ended_at = receipt.received_at - timedelta(minutes=6)
    world.catch_up()
    body = _ok(world.post(zone, camera, ended_at), 201)
    assert body["verification"] == "failed", body
    assert body["failure_reason"] == "no_observability_events_in_window"
    assert body["correlated_event_ids"] == []
    (result,) = world.results(zone)
    assert (result["content"]["verification"], result["content"]["correlated_event_ids"]) == (
        "failed",
        [],
    )


def test_a_required_camera_verifies_on_the_first_look_once_the_zone_drops(
    world: OcclusionWorld,
) -> None:
    zone = world.zone(required=1)
    camera = zone.cameras[0]
    world.catch_up()
    approx = world.db_now()
    world.observe(zone, started_at=approx - timedelta(seconds=15), camera=camera)
    receipt = world.observe(
        zone,
        started_at=approx - timedelta(seconds=14),
        kind="zone",
        state="not_observable",
        causes=("obstruction",),
    )
    body = _ok(world.post(zone, camera, receipt.received_at - timedelta(seconds=5)), 201)
    assert body["verification"] == "verified", body
    assert len(body["correlated_event_ids"]) == 2


# --- Alcance de nodo -----------------------------------------------------------------------------


def test_node_scope_copies_in_several_zones_neither_count_nor_block_verified(
    world: OcclusionWorld,
) -> None:
    site = world.site(zones=2)
    zone = world.zone(site=site)
    other = world.zone(site=site, index=1)
    camera = zone.cameras[1]
    world.catch_up()
    approx = world.db_now()
    started = approx - timedelta(seconds=12)
    # El reinicio y el reloj del nodo de la zona, una copia por zona (pendiente nº 40).
    for target in (zone, other):
        for kind, causes in (("zone", ("node_restart",)), ("clock", ("clock_unsynchronized",))):
            world.observe(
                target,
                started_at=started,
                kind=kind,
                state="not_observable",
                causes=causes,
                node=zone.node,
            )
    receipt = world.observe(zone, started_at=started, camera=camera)
    ended_at = receipt.received_at - timedelta(seconds=5)
    _ok(world.post(zone, camera, ended_at), 201)
    world.advance(timedelta(minutes=6))
    (test,) = world.current(zone)
    assert test["verification"] == "verified", test
    # Solo el evento de la cámara: ni el reinicio ni el reloj están entre los correlacionados.
    assert len(test["correlated_event_ids"]) == 1


def test_node_scope_events_alone_are_no_camera_event(world: OcclusionWorld) -> None:
    zone = world.zone()
    camera = zone.cameras[1]
    world.catch_up()
    approx = world.db_now()
    receipt = world.observe(
        zone,
        started_at=approx - timedelta(seconds=12),
        kind="zone",
        state="not_observable",
        causes=("node_restart",),
    )
    _ok(world.post(zone, camera, receipt.received_at - timedelta(seconds=5)), 201)
    world.advance(timedelta(minutes=6))
    (test,) = world.current(zone)
    assert (test["verification"], test["failure_reason"]) == (
        "failed",
        "no_observability_events_in_window",
    )


# --- Guarda de alcance ---------------------------------------------------------------------------


def test_events_of_another_zone_or_organization_are_not_counted(world: OcclusionWorld) -> None:
    site = world.site(zones=2)
    zone = world.zone(site=site)
    neighbour = world.zone(site=site, index=1)
    foreign = world.zone()
    assert neighbour.site.organization_id == zone.site.organization_id
    assert foreign.site.organization_id != zone.site.organization_id
    camera = zone.cameras[1]
    world.catch_up()
    approx = world.db_now()
    started = approx - timedelta(seconds=12)
    # El mismo camera_id, pero en la zona vecina de la misma organización y en otra organización.
    world.observe(neighbour, started_at=started, camera=camera)
    receipt = world.observe(foreign, started_at=started, camera=camera)
    _ok(world.post(zone, camera, receipt.received_at - timedelta(seconds=5)), 201)
    world.advance(timedelta(minutes=6))
    (test,) = world.current(zone)
    assert (test["verification"], test["failure_reason"]) == (
        "failed",
        "no_observability_events_in_window",
    ), test


# --- Declaración ---------------------------------------------------------------------------------


def test_a_declaration_resolves_the_pending_test_and_the_record_says_declared(
    world: OcclusionWorld,
) -> None:
    zone = world.zone()
    camera = zone.cameras[1]
    world.catch_up()
    ended_at = world.clock.now() - timedelta(seconds=5)
    created = _ok(world.post(zone, camera, ended_at), 201)
    assert created["verification"] == "pending"
    declared = _ok(world.post(zone, camera, ended_at, reason=REASON), 201)
    assert declared["test_id"] == created["test_id"]  # sin crear otra
    assert (declared["verification"], declared["declared_reason_es"]) == ("declared", REASON)
    (row,) = world.rows(zone)
    assert (row["verification"], row["declared_reason_es"]) == ("declared", REASON)
    (result,) = world.results(zone)
    assert result["content"]["verification"] == "declared"
    assert result["content"]["declared_reason_es"] == REASON
    # El acta la distingue de verified, y una prueba nueva ya no se admite.
    (test,) = world.current(zone)
    assert test["verification"] == "declared"
    _error(world.post(zone, camera, ended_at), 409, "conflict")


def test_a_declaration_with_counted_events_is_not_admitted(world: OcclusionWorld) -> None:
    zone = world.zone()
    camera = zone.cameras[1]
    world.catch_up()
    # Ventana y evento con el reloj simulado (que nunca va por detrás de la base): la recepción
    # cae antes de la fecha límite y la prueba sigue pending al declarar.
    ended_at = world.clock.now() - timedelta(seconds=5)
    world.observe(zone, started_at=ended_at - timedelta(seconds=5), camera=camera)
    _ok(world.post(zone, camera, ended_at), 201)
    _error(world.post(zone, camera, ended_at, reason=REASON), 409, "conflict")
    (row,) = world.rows(zone)
    assert row["verification"] == "pending"
    assert world.results(zone) == []


def test_silence_past_the_deadline_fails_and_admits_a_new_test(world: OcclusionWorld) -> None:
    zone = world.zone()
    camera = zone.cameras[0]
    world.catch_up()
    ended_at = world.clock.now() - timedelta(seconds=5)
    _ok(world.post(zone, camera, ended_at), 201)
    world.advance(timedelta(minutes=6))
    # Vencida la fecha límite, la declaración ya no se admite (y deja la prueba failed).
    _error(world.post(zone, camera, ended_at, reason=REASON), 409, "conflict")
    (first,) = world.current(zone)
    assert (first["verification"], first["failure_reason"]) == (
        "failed",
        "no_observability_events_in_window",
    )
    # Tras un failed se admite una prueba nueva, y con motivo se declara (FS-GOB-06).
    retried = _ok(world.post(zone, camera, world.clock.now() - timedelta(seconds=5)), 201)
    assert retried["verification"] == "pending"
    declared = _ok(
        world.post(zone, camera, world.clock.now() - timedelta(seconds=5), reason=REASON), 201
    )
    assert (declared["test_id"], declared["verification"]) == (retried["test_id"], "declared")
    tests = world.current(zone)
    assert [t["verification"] for t in tests] == ["failed", "declared"]
    assert len(world.results(zone)) == 2


def test_a_new_test_while_one_is_pending_is_conflict(world: OcclusionWorld) -> None:
    zone = world.zone()
    world.catch_up()
    ended_at = world.clock.now() - timedelta(seconds=5)
    _ok(world.post(zone, zone.cameras[0], ended_at), 201)
    _error(world.post(zone, zone.cameras[0], ended_at), 409, "conflict")
    assert len(world.rows(zone)) == 1


# --- Entradas ------------------------------------------------------------------------------------


def test_a_camera_outside_the_catalog_or_a_bad_window_is_invalid_request(
    world: OcclusionWorld,
) -> None:
    zone = world.zone()
    world.catch_up()
    now = world.clock.now()
    _error(world.post(zone, uuid.uuid4(), now - timedelta(seconds=5)), 400, "invalid_request")
    _error(world.post(zone, zone.cameras[0], now + timedelta(minutes=1)), 400, "invalid_request")
    _error(
        world.post(zone, zone.cameras[0], now, length=timedelta(hours=1, seconds=1)),
        400,
        "invalid_request",
    )
    _error(
        world.post(zone, zone.cameras[0], now - timedelta(seconds=5), reason="   ...   "),
        400,
        "invalid_request",
        "catalog_free_text_rejected",
    )
    assert world.rows(zone) == []


def test_an_unknown_session_is_not_found(world: OcclusionWorld) -> None:
    zone = world.zone()
    world.catch_up()
    response = world.request(
        "POST",
        f"/walk-tests/{uuid7(world.clock)}/occlusion-tests",
        zone.mounted,
        {
            "camera_id": str(zone.cameras[0]),
            "started_at": format_timestamp(world.clock.now() - timedelta(seconds=30)),
            "ended_at": format_timestamp(world.clock.now() - timedelta(seconds=5)),
        },
    )
    _error(response, 404, "not_found")


# --- Concurrencia --------------------------------------------------------------------------------


class _MeetBeforeTestLock(PostgresOcclusionRepository):
    """Todas las reevaluaciones decidieron antes de que ninguna tome el candado de la prueba."""

    def __init__(self, barrier: _Barrier) -> None:
        self._meet = barrier

    async def lock(self, transaction: Transaction, test_id: uuid.UUID) -> OcclusionTest | None:
        await self._meet.wait()
        return await super().lock(transaction, test_id)


def test_concurrent_reevaluations_leave_one_resolution_and_one_record(
    world: OcclusionWorld,
) -> None:
    zone = world.zone()
    camera = zone.cameras[1]
    world.catch_up()
    approx = world.db_now()
    receipt = world.observe(zone, started_at=approx - timedelta(seconds=12), camera=camera)
    _ok(world.post(zone, camera, receipt.received_at - timedelta(seconds=5)), 201)
    racing = world.build(repository=_MeetBeforeTestLock(_Barrier(CONCURRENT)))
    late = world.clock.now() + timedelta(minutes=6)

    async def race() -> list[Any]:
        return await asyncio.gather(
            *(
                racing.reevaluate_pending(zone.mounted.installer, zone.session, late)
                for _ in range(CONCURRENT)
            ),
            return_exceptions=True,
        )

    outcomes = world.run(race())
    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert errors == [], errors
    verifications = {tuple(t.verification for t in tests) for tests in outcomes}
    assert verifications == {(OcclusionVerification.VERIFIED,)}
    (row,) = world.rows(zone)
    assert row["verification"] == "verified"
    (result,) = world.results(zone)
    assert result["record_id"] == row["ledger_record_id"]


class _MeetBeforeSessionLock(PostgresWalkTestRepository):
    """Todas las altas pasaron sus comprobaciones previas antes de tomar el candado de la sesión."""

    def __init__(self, barrier: _Barrier) -> None:
        self._meet = barrier

    async def lock_session(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> WalkTestSession | None:
        await self._meet.wait()
        return await super().lock_session(transaction, session_id)


def test_concurrent_new_tests_of_one_camera_leave_one_test(world: OcclusionWorld) -> None:
    zone = world.zone()
    camera = zone.cameras[0]
    world.catch_up()
    ended_at = world.clock.now() - timedelta(seconds=5)
    racing = world.build(sessions=_MeetBeforeSessionLock(_Barrier(CONCURRENT)))

    async def race() -> list[Any]:
        return await asyncio.gather(
            *(
                racing.record(
                    zone.mounted.installer,
                    zone.session.session_id,
                    camera,
                    ended_at - timedelta(seconds=20),
                    ended_at,
                )
                for _ in range(CONCURRENT)
            ),
            return_exceptions=True,
        )

    outcomes = world.run(race())
    created = [o for o in outcomes if isinstance(o, OcclusionTest)]
    others = [o for o in outcomes if not isinstance(o, OcclusionTest)]
    assert len(created) == 1, outcomes
    assert all(isinstance(o, WalkTestConflict) for o in others), others
    assert len(world.rows(zone)) == 1


class _HoldFirstLock:
    """Cada operación, al obtener su **primer** candado, espera a que la otra tenga el suyo: así
    una declaración (sesión → prueba) y una reevaluación (prueba) se cruzan de verdad."""

    def __init__(self) -> None:
        self.barrier = _Barrier(2)
        self.holding: set[asyncio.Task[Any]] = set()

    async def first(self) -> None:
        task = asyncio.current_task()
        assert task is not None
        if task in self.holding:
            return
        self.holding.add(task)
        await self.barrier.wait()


class _HoldingSessions(PostgresWalkTestRepository):
    def __init__(self, hold: _HoldFirstLock) -> None:
        self._hold = hold

    async def lock_session(
        self, transaction: Transaction, session_id: uuid.UUID
    ) -> WalkTestSession | None:
        session = await super().lock_session(transaction, session_id)
        await self._hold.first()
        return session


class _HoldingTests(PostgresOcclusionRepository):
    def __init__(self, hold: _HoldFirstLock) -> None:
        self._hold = hold

    async def lock(self, transaction: Transaction, test_id: uuid.UUID) -> OcclusionTest | None:
        test = await super().lock(transaction, test_id)
        await self._hold.first()
        return test


def test_a_declaration_and_a_reevaluation_at_once_never_deadlock(world: OcclusionWorld) -> None:
    zone = world.zone()
    camera = zone.cameras[1]
    world.catch_up()
    ended_at = world.clock.now() - timedelta(seconds=5)
    created = _ok(world.post(zone, camera, ended_at), 201)
    hold = _HoldFirstLock()
    racing = world.build(sessions=_HoldingSessions(hold), repository=_HoldingTests(hold))
    deadline = datetime.fromisoformat(created["deadline"])

    async def race() -> list[Any]:
        return await asyncio.gather(
            racing.record(
                zone.mounted.installer,
                zone.session.session_id,
                camera,
                ended_at - timedelta(seconds=20),
                ended_at,
                REASON,
            ),
            # La reevaluación mira vencida la fecha límite: quiere dejarla failed.
            racing.reevaluate_pending(
                zone.mounted.installer, zone.session, deadline + timedelta(seconds=1)
            ),
            return_exceptions=True,
        )

    declaration, reevaluation = world.run(race())
    # Ningún interbloqueo ni transitorio: una de las dos resuelve y la otra lo respeta.
    assert not isinstance(reevaluation, BaseException), reevaluation
    assert isinstance(declaration, OcclusionTest | WalkTestConflict), declaration
    (row,) = world.rows(zone)
    assert row["verification"] in ("declared", "failed")
    if isinstance(declaration, OcclusionTest):
        assert row["verification"] == "declared"
    assert len(world.results(zone)) == 1


# --- Lectura de la sesión ------------------------------------------------------------------------


def test_get_current_lists_the_tests_and_audits_the_ledger_read(world: OcclusionWorld) -> None:
    zone = world.zone()
    world.catch_up()
    ended_at = world.clock.now() - timedelta(seconds=5)
    created = _ok(world.post(zone, zone.cameras[0], ended_at), 201)
    (listed,) = world.current(zone)
    assert listed == created
    trail = world.fetch(
        "SELECT count(*) AS reads FROM shared.audit_entry"
        " WHERE operation = 'ledger_read' AND scope_zone_id = $1",
        zone.mounted.zone,
    )
    assert trail[0]["reads"] >= 1
