"""Las tres rutas de ``/accreditation/monthly-packages*`` sobre PostgreSQL 16 real (TASK-344;
SUP-U05-24, 25 y 41; LC-LAZ-24; VIG-239).

Las filas selladas de ``accreditation.monthly_package``/``audit_sample``/``audit_sample_item`` se
siembran **directamente por SQL**, como superusuario (``tests.ledger_database.set_organization``,
igual que ``tests/export_support.py::ExportStack.add_evidence``): al escribir estas pruebas,
``MonthlyPackageComposer.compose_and_seal`` no puede sellar ningún paquete contra la base real
(dos fallos bloqueantes de ``repository.py``, documentados en el informe de la tarea y cubiertos
por ``tests/integration/test_monthly_package_task.py``), así que las rutas —puramente de
lectura— se prueban contra filas sembradas, no contra una composición real. El firmante de la
descarga es un doble determinista (``FakeSigner``): esta prueba no comprueba los bytes del
paquete, solo que la URL y la auditoría del permiso son correctas.

- ``GET /accreditation/monthly-packages``: 409 si no hay fila sellada; 200 con el resumen si la
  hay.
- ``GET .../{package_id}/sample``: los elementos de la muestra, todos sin revisar (TASK-347 no
  existe).
- ``GET .../{plant_id}/{period}/download``: una URL por zona en el alcance del actor **y con
  paquete sellado**; la zona fuera de alcance no aparece y no se firma ni se audita para ella
  (PR-LAZ-60); una entrada ``monthly_package_download_granted`` por URL emitida.
- ``404 not_found`` (nunca ``403`` ni el ``409`` de "no sellado") para un paquete o una zona que
  no existen o están fuera de alcance.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest

from tests.api_support import World as AppBuilder
from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.integration.conftest import PostgresEndpoint
from tests.ledger_database import set_organization
from tests.outbox_support import app_database
from vigia_platform.accreditation.adapters.http.monthly_routes import MonthlyPackageReads
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository, period_to_date
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeLevel
from vigia_platform.shared.db import Database
from vigia_platform.shared.storage import PresignedRequest

pytestmark = pytest.mark.integration

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
LOCK_TIMEOUT_MS = 60_000
_EMPTY_FINDINGS = {
    "received": 0,
    "classified": 0,
    "confirmed": 0,
    "discarded": 0,
    "closed": 0,
    "overdue": 0,
    "unclosable": 0,
    "reopened": 0,
}


class _NoProviderQueries:
    async def write_provider_query(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("ninguna persona de estas pruebas opera bajo concesión")


@dataclass
class FakeSigner:
    """``MonthlyPackageDownloadSigner`` determinista: no hay bytes reales que descargar."""

    clock: Any
    calls: list[str] = field(default_factory=list)

    async def presign_attachment_get(
        self, key: str, ttl: timedelta = timedelta(minutes=15), *, version_id: str | None = None
    ) -> PresignedRequest:
        self.calls.append(key)
        return PresignedRequest(
            method="GET",
            url=f"https://evidencia.prueba.test/{key}",
            headers={},
            expires_at=self.clock.now() + ttl,
        )


@dataclass
class World:
    authz: AuthzEnvironment
    database: Database
    repository: MonthlyPackageRepository
    signer: FakeSigner
    app: Any
    client: httpx.AsyncClient

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def admin_execute(self, organization_id: uuid.UUID, sql: str, *args: Any) -> None:
        """Una sentencia de superusuario dentro de ``set_organization`` (como ``seed_package``)."""
        admin = self.authz.sessions.admin

        async def run() -> None:
            async with admin.transaction():
                await set_organization(admin, organization_id)
                await admin.execute(sql, *args)

        self.run(run())

    def now(self) -> Any:
        return self.authz.now()

    # --- personas y peticiones -------------------------------------------------------------

    def site(self, plants: int = 1, zones: int = 2) -> Site:
        return self.authz.add_site(plants=plants, zones_per_plant=zones)

    def member(
        self,
        organization_id: uuid.UUID,
        *,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ZONE,
        scope: uuid.UUID | None = None,
    ) -> SessionCookie:
        user = self.authz.add_user(organization_id)
        self.authz.assign(organization_id, user, role, level, scope)
        return self.authz.open_session(organization_id, user)

    def send(
        self, cookie: SessionCookie, method: str, path: str, params: dict[str, str] | None = None
    ) -> httpx.Response:
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        response: httpx.Response = self.run(
            self.client.request(method, path, params=params, headers=headers)
        )
        return response

    # --- siembra directa (el compositor real no sella contra la base, ver docstring) ------

    def seed_package(
        self,
        *,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        period: str,
        state: str = "sealed",
        audit_sample_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        package_id = uuid.uuid4()
        admin = self.authz.sessions.admin

        async def insert() -> None:
            async with admin.transaction():
                await set_organization(admin, organization_id)
                await admin.execute(
                    "INSERT INTO accreditation.monthly_package (package_id, organization_id,"
                    " plant_id, zone_id, period, state, generated_at, findings_summary,"
                    " recurrence_summary, coverage_summary, commitments_from_previous_period,"
                    " audit_sample_id, sealed_checkpoint_record_id, record_id)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, '[]'::jsonb, '{}'::jsonb,"
                    " '[]'::jsonb, $9, $10, $11)",
                    package_id,
                    organization_id,
                    plant_id,
                    zone_id,
                    period_to_date(period),
                    state,
                    self.now(),
                    json.dumps(_EMPTY_FINDINGS),
                    audit_sample_id,
                    uuid.uuid4() if state == "sealed" else None,
                    uuid.uuid4() if state == "sealed" else None,
                )

        self.run(insert())
        return package_id

    def seed_sample(
        self,
        *,
        organization_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        package_id: uuid.UUID,
        count: int,
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """Una muestra sellada con ``count`` elementos, todos sin revisar (TASK-347)."""
        sample_id = uuid.uuid4()
        item_ids = [uuid.uuid4() for _ in range(count)]
        reviewer_user_id = self.authz.add_user(organization_id)
        admin = self.authz.sessions.admin
        strata = [
            {
                "family": "coexistence",
                "reason_category": "real_coexistence",
                "population": count,
                "drawn": count,
            }
        ]

        async def insert() -> None:
            async with admin.transaction():
                await set_organization(admin, organization_id)
                await admin.execute(
                    "INSERT INTO accreditation.audit_sample (sample_id, organization_id,"
                    " plant_id, zone_id, package_id, population_size, sample_size, seed, strata,"
                    " selected_at, reviewer_user_id)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $6, 12345, $7::jsonb, $8, $9)",
                    sample_id,
                    organization_id,
                    plant_id,
                    zone_id,
                    package_id,
                    count,
                    json.dumps(strata),
                    self.now(),
                    reviewer_user_id,
                )
                for item_id in item_ids:
                    classification_id = uuid.uuid4()
                    subject_id = uuid.uuid4()
                    # ``_SAMPLE_ITEMS`` lee la clasificación por INNER JOIN (TASK-320 rework 1,
                    # B4 de la revisión: «con su clasificación, motivo, autor, fecha»): hace
                    # falta una fila real de ``loop.classification``, no solo el id suelto.
                    await admin.execute(
                        "INSERT INTO loop.classification (classification_id, organization_id,"
                        " plant_id, zone_id, subject_kind, subject_id, record_id,"
                        " sequence_in_subject, outcome, reason_category, reason_text,"
                        " is_reclassification, reinforced_reason, effect_on_loop, classified_by,"
                        " classified_at, label_id, correlation_id)"
                        " VALUES ($1, $2, $3, $4, 'finding', $5, $6, 1, 'confirmed',"
                        " 'real_coexistence', 'Persona cruzó el pasillo señalizado', false, false,"
                        " 'loop_opened', $7::jsonb, $8, $9, $9)",
                        classification_id,
                        organization_id,
                        plant_id,
                        zone_id,
                        subject_id,
                        uuid.uuid4(),
                        json.dumps(
                            {
                                "user_id": str(reviewer_user_id),
                                "display_name": "Coordinador sintético",
                                "role_in_use": "coordinator_sst",
                            }
                        ),
                        self.now(),
                        uuid.uuid4(),
                    )
                    await admin.execute(
                        "INSERT INTO accreditation.audit_sample_item (sample_item_id,"
                        " organization_id, plant_id, zone_id, sample_id, classification_id,"
                        " subject_id) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        item_id,
                        organization_id,
                        plant_id,
                        zone_id,
                        sample_id,
                        classification_id,
                        subject_id,
                    )

        self.run(insert())
        return sample_id, item_ids

    def grants(self, package_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT actor_id, operation, outcome, resource_kind, resource_id, scope_zone_id"
            " FROM shared.audit_entry WHERE operation = 'monthly_package_download_granted'"
            " AND resource_id = $1 ORDER BY chain_sequence",
            package_id,
        )


@pytest.fixture(scope="module")
def world(postgres_endpoint: PostgresEndpoint) -> Iterator[World]:
    with authz_environment(postgres_endpoint, "monthly_routes") as authz:
        sessions = authz.sessions
        database = app_database(
            sessions.migrated, worker_pool_size=8, lock_timeout_ms=LOCK_TIMEOUT_MS
        )
        audit = AuditWriter(
            database=database,
            clock=sessions.clock,
            provider_organization_id=authz.provider_organization_id,
        )
        repository = MonthlyPackageRepository(database=database)
        signer = FakeSigner(clock=sessions.clock)
        reads = MonthlyPackageReads(
            database=database,
            repository=repository,
            authorizer=authz.authorizer,
            audit=audit,
            storage=signer,
        )
        app = AppBuilder(clock=sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=_NoProviderQueries(),
                    clock=sessions.clock,
                ),
                "state": {ACCREDITATION_STATE_KEY: AccreditationHttp(monthly_packages=reads)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        try:
            yield World(authz, database, repository, signer, app, client)
        finally:
            authz.run(client.aclose())
            authz.run(database.dispose())


# --- 1 y 2: disponibilidad del resumen ---------------------------------------------------------


def test_unsealed_period_answers_409_not_available(world: World) -> None:
    site = world.site()
    ((_, zones),) = site.plants.items()
    zone_id = zones[0]
    cookie = world.member(site.organization_id, level=ScopeLevel.ZONE, scope=zone_id)

    response = world.send(
        cookie,
        "GET",
        "/accreditation/monthly-packages",
        params={"zone_id": str(zone_id), "period": "2026-01"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail_code"] == "accreditation_monthly_package_not_available"
    assert response.headers["cache-control"] == "no-store"

    # Una fila ``generating`` (en curso) tampoco está disponible.
    world.seed_package(
        organization_id=site.organization_id,
        plant_id=next(iter(site.plants)),
        zone_id=zone_id,
        period="2026-02",
        state="generating",
    )
    response = world.send(
        cookie,
        "GET",
        "/accreditation/monthly-packages",
        params={"zone_id": str(zone_id), "period": "2026-02"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail_code"] == "accreditation_monthly_package_not_available"


def test_sealed_period_answers_200_with_the_summary(world: World) -> None:
    site = world.site()
    plant_id = next(iter(site.plants))
    ((_, zones),) = site.plants.items()
    zone_id = zones[0]
    cookie = world.member(site.organization_id, level=ScopeLevel.ZONE, scope=zone_id)
    package_id = world.seed_package(
        organization_id=site.organization_id, plant_id=plant_id, zone_id=zone_id, period="2026-03"
    )

    response = world.send(
        cookie,
        "GET",
        "/accreditation/monthly-packages",
        params={"zone_id": str(zone_id), "period": "2026-03"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["package_id"] == str(package_id)
    assert body["zone_id"] == str(zone_id)
    assert body["period"] == "2026-03"
    assert body["state"] == "sealed"
    assert body["findings_summary"] == _EMPTY_FINDINGS
    assert body["recurrence_summary"] == []
    assert body["commitments_from_previous_period"] == []


# --- 3: la muestra ------------------------------------------------------------------------------


def test_sample_items_are_all_unreviewed(world: World) -> None:
    site = world.site()
    plant_id = next(iter(site.plants))
    ((_, zones),) = site.plants.items()
    zone_id = zones[0]
    cookie = world.member(site.organization_id, level=ScopeLevel.ZONE, scope=zone_id)
    # El paquete primero (``audit_sample.package_id`` exige una fila ya sellada); la muestra
    # referencia su ``package_id`` real y después el paquete se corrige con el ``audit_sample_id``.
    package_id = world.seed_package(
        organization_id=site.organization_id, plant_id=plant_id, zone_id=zone_id, period="2026-04"
    )
    sample_id, item_ids = world.seed_sample(
        organization_id=site.organization_id,
        plant_id=plant_id,
        zone_id=zone_id,
        package_id=package_id,
        count=4,
    )
    world.admin_execute(
        site.organization_id,
        "UPDATE accreditation.monthly_package SET audit_sample_id = $1 WHERE package_id = $2",
        sample_id,
        package_id,
    )

    response = world.send(cookie, "GET", f"/accreditation/monthly-packages/{package_id}/sample")
    assert response.status_code == 200, response.text
    items = response.json()
    assert len(items) == 4
    assert {item["sample_item_id"] for item in items} == {str(item_id) for item_id in item_ids}
    for item in items:
        assert item["outcome"] is None
        assert item["comment"] is None
        assert item["reviewed_by"] is None
        assert item["reviewed_at"] is None
        assert "classification_id" in item and "subject_id" in item
        # TASK-320 rework 1 (B4 de la revisión): «con su clasificación, motivo, autor, fecha».
        assert item["classification_outcome"] == "confirmed"
        assert item["reason_category"] == "real_coexistence"
        assert item["classified_by_role"] == "coordinator_sst"
        assert item["classified_at"]


# --- 4: descarga por planta, filtrada por el alcance (PR-LAZ-60) --------------------------------


def test_plant_download_lists_only_the_in_scope_zone_and_audits_once(world: World) -> None:
    site = world.site(plants=1, zones=2)
    plant_id = next(iter(site.plants))
    ((_, zones),) = site.plants.items()
    in_scope, out_of_scope = zones
    cookie = world.member(site.organization_id, level=ScopeLevel.ZONE, scope=in_scope)

    in_package = world.seed_package(
        organization_id=site.organization_id, plant_id=plant_id, zone_id=in_scope, period="2026-05"
    )
    world.seed_package(
        organization_id=site.organization_id,
        plant_id=plant_id,
        zone_id=out_of_scope,
        period="2026-05",
    )

    response = world.send(
        cookie, "GET", f"/accreditation/monthly-packages/{plant_id}/2026-05/download"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert [zone["zone_id"] for zone in body["zones"]] == [str(in_scope)]
    assert body["zones"][0]["url"]

    grants = world.grants(in_package)
    assert len(grants) == 1, grants
    assert grants[0]["scope_zone_id"] == in_scope


# --- 5: fuera de alcance o inexistente → 404, nunca 403 ni 409 ----------------------------------


def test_missing_or_out_of_scope_answers_not_found(world: World) -> None:
    site = world.site()
    plant_id = next(iter(site.plants))
    ((_, zones),) = site.plants.items()
    zone_id = zones[0]
    cookie = world.member(site.organization_id, level=ScopeLevel.ZONE, scope=zone_id)

    other_site = world.site()
    ((_, other_zones),) = other_site.plants.items()
    other_zone = other_zones[0]
    other_package = world.seed_package(
        organization_id=other_site.organization_id,
        plant_id=next(iter(other_site.plants)),
        zone_id=other_zone,
        period="2026-07",
    )

    # Zona inexistente, y zona de otra organización: 404, no 409.
    for missing_zone in (uuid.uuid4(), other_zone):
        response = world.send(
            cookie,
            "GET",
            "/accreditation/monthly-packages",
            params={"zone_id": str(missing_zone), "period": "2026-07"},
        )
        assert response.status_code == 404, (missing_zone, response.text)
        assert response.json()["code"] == "not_found"

    # Paquete inexistente, y paquete real pero de una zona fuera de alcance: 404.
    for missing_package in (uuid.uuid4(), other_package):
        response = world.send(
            cookie, "GET", f"/accreditation/monthly-packages/{missing_package}/sample"
        )
        assert response.status_code == 404, (missing_package, response.text)
        assert response.json()["code"] == "not_found"

    # Y la propia, con la fila sellada, sigue respondiendo bien (no es un alcance roto).
    package_id = world.seed_package(
        organization_id=site.organization_id, plant_id=plant_id, zone_id=zone_id, period="2026-07"
    )
    response = world.send(
        cookie,
        "GET",
        "/accreditation/monthly-packages",
        params={"zone_id": str(zone_id), "period": "2026-07"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["package_id"] == str(package_id)
