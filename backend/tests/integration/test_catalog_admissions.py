"""Prueba de admisión de tres preguntas por HTTP contra PostgreSQL 16 real (TASK-207, LC-GOB-02).

La aplicación real (``create_app`` con la cadena fija de middleware, ``ScopeContexts`` y
``ContextAuthorizer``) sobre la base migrada como ``vigia_app``, con el ``AdmissionService`` real,
``EscritorExpediente`` y la política de texto libre con el validador mínimo de U-03 (A-45):

- **H-43**: con las tres respuestas afirmativas, ``201`` y la evaluación queda en
  ``catalog.family_admission`` y en ``standard_admission_test`` (cadena de la planta,
  ``source_key = admission_id``). Con alguna negativa, la evaluación **se confirma igual** con su
  ``failed_criterion`` (el primero falso) y después responde ``invalid_request`` con
  ``catalog_admission_rejected``; volver a intentarlo crea otra evaluación (BR-GOB-15).
- **BR-GOB-14**: con la familia ya admitida, ``conflict`` con ``catalog_family_already_admitted`` y
  nada se escribe; N peticiones simultáneas dejan exactamente una ``admitted`` y un registro (la
  garantía es el índice único parcial ``family_admission_admitted_once``).
- **BR-GOB-16**: ``admission_for`` y el listado solo ven la planta pedida en la organización del
  contexto; un administrador de planta no admite en otra planta.
- **G-6**: una familia fuera de la lista cerrada del contrato (``productivity``…), un parámetro de
  más o respuestas que no son booleanas responden ``invalid_request`` sin escribir nada; una
  justificación con marcado o que afirma intención, ``catalog_free_text_rejected``.

El estado HTTP de ``invalid_request`` es el de U-02 (``HTTP_STATUS``: 400) para todo
``ApiError``; ver la nota del PR sobre el «422» de interfaces §3.1.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
from vigia_contracts.models.enumerations import PredicateFamily

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.examples.test_ledger_routes import StubEvidenceStorage
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.adapters.postgres.admission_repository import (
    PostgresAdmissionRepository,
)
from vigia_platform.catalog.application.admission import AdmissionService
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeLevel
from vigia_platform.shared.db import Database, Transaction

pytestmark = pytest.mark.integration

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
YES: Final = {"standard": True, "remedy": True, "subject": True}
CONCURRENT: Final = 4
"""Peticiones simultáneas de la prueba concurrente (cabe en el pool de la base del servicio)."""
BARRIER_TIMEOUT_SECONDS: Final = 30.0


def _admission_type() -> Any:
    (definition,) = (d for d in CATALOG_RECORD_TYPES if d.record_type == "standard_admission_test")
    return definition


class BarrierRepository(PostgresAdmissionRepository):
    """Retiene cada alta tras su comprobación previa hasta que todas la han hecho.

    Así las ``CONCURRENT`` peticiones pasan **todas** la comprobación «¿ya hay una admitida?»
    antes de que la primera confirme: solo el índice único puede impedir la segunda admitida.
    """

    def __init__(self, database: Database, parties: int) -> None:
        super().__init__(database)
        self.barrier = asyncio.Barrier(parties)

    async def admitted_in(
        self, transaction: Transaction, plant_id: uuid.UUID, family: PredicateFamily
    ) -> bool:
        found = await super().admitted_in(transaction, plant_id, family)
        async with asyncio.timeout(BARRIER_TIMEOUT_SECONDS):
            await self.barrier.wait()
        return found


@dataclass
class Stack:
    """La base, el escritor y la política de texto libre del servicio."""

    authz: AuthzEnvironment
    database: Database
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry

    def build(self, repository: PostgresAdmissionRepository) -> tuple[AdmissionService, Any]:
        """Servicio y aplicación real con ``repository``."""
        authz = self.authz
        service = AdmissionService(
            repository=repository,
            database=self.database,
            writer=self.writer,
            authorizer=authz.authorizer,
            audit=authz.sessions.audit,
            free_text=self.free_text,
            clock=authz.sessions.clock,
        )
        app = World(clock=authz.sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(self.writer),
                    clock=authz.sessions.clock,
                ),
                "state": {CATALOG_STATE_KEY: CatalogHttp(admissions=service)},
            },
        )
        return service, app


@dataclass
class Admissions:
    stack: Stack
    client: httpx.AsyncClient
    service: AdmissionService

    @property
    def authz(self) -> AuthzEnvironment:
        return self.stack.authz

    @property
    def database(self) -> Database:
        return self.stack.database

    def build(self, repository: PostgresAdmissionRepository) -> tuple[AdmissionService, Any]:
        return self.stack.build(repository)

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    # --- Personas y peticiones -----------------------------------------------------------------

    def member(
        self,
        site: Site,
        role: Role = Role.ADMINISTRATOR,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> SessionCookie:
        user_id = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user_id, role, level, scope_id)
        cookie: SessionCookie = self.authz.open_session(site.organization_id, user_id)
        return cookie

    @staticmethod
    def headers(cookie: SessionCookie) -> dict[str, str]:
        return {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}

    def post(
        self,
        cookie: SessionCookie,
        plant_id: uuid.UUID,
        body: Any,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        response: httpx.Response = self.run(
            (client or self.client).post(
                f"/plants/{plant_id}/admissions", json=body, headers=self.headers(cookie)
            )
        )
        return response

    def get(self, cookie: SessionCookie, plant_id: uuid.UUID, **params: Any) -> httpx.Response:
        response: httpx.Response = self.run(
            self.client.get(
                f"/plants/{plant_id}/admissions", params=params, headers=self.headers(cookie)
            )
        )
        return response

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def rows(self, plant_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT admission_id, family, answers::text AS answers, justification_es, result,"
            " failed_criterion, evaluated_by, role_in_use, ledger_record_id"
            " FROM catalog.family_admission WHERE plant_id = $1"
            " ORDER BY evaluated_at, admission_id",
            plant_id,
        )

    def records(self, plant_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT record_id, plant_id, source_key, actor_role_in_use,"
            " ledger.vigia_bytes_to_jsonb(content) AS content FROM ledger.ledger_record"
            " WHERE record_type = 'standard_admission_test' AND plant_id = $1"
            " ORDER BY chain_sequence",
            plant_id,
        )


def _body(family: str = "coexistence", **answers: bool) -> dict[str, Any]:
    return {"family": family, "answers": {**YES, **answers}}


def _code(response: httpx.Response) -> tuple[int, str | None, str | None]:
    body = response.json()
    return response.status_code, body.get("code"), body.get("detail_code")


@pytest.fixture(scope="module")
def admissions(postgres_endpoint: PostgresEndpoint) -> Iterator[Admissions]:
    with authz_environment(postgres_endpoint, "catalog_admissions") as authz:
        sessions = authz.sessions
        # La RLS de las concesiones compara la vigencia con la hora de la base (nuc_0009): el
        # reloj simulado arranca en ella, y toda marca de la prueba sale de él (retro 14).
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(now["now"])
        registry = RecordTypeRegistry()
        for definition in (*U02_RECORD_TYPES, _admission_type()):
            registry.register(definition)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
            registry.seal()

        authz.run(synchronize())
        free_text = FreeTextPolicyRegistry()
        register_u03_free_text_validator(free_text)
        free_text.seal()
        # Base propia del servicio, con conexiones para todas las peticiones simultáneas.
        database = app_database(sessions.migrated, worker_pool_size=2 * CONCURRENT)
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(StubEvidenceStorage(), sessions.clock),
            outbox=sessions.outbox,
            clock=sessions.clock,
        )
        stack = Stack(authz, database, writer, free_text)
        service, app = stack.build(PostgresAdmissionRepository(database))
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=30.0
        )
        try:
            yield Admissions(stack, client, service)
        finally:
            authz.run(client.aclose())
            authz.run(database.dispose())


# --- H-43: las tres preguntas ------------------------------------------------------------------


def test_three_affirmative_answers_admit_and_leave_row_and_record(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    cookie = admissions.member(site)
    body = {**_body("guard_bypass"), "justification_es": "Hay estándar escrito de resguardos"}

    response = admissions.post(cookie, plant, body)

    assert response.status_code == 201, response.text
    view = response.json()
    assert (view["family"], view["result"], view["failed_criterion"]) == (
        "guard_bypass",
        "admitted",
        None,
    )
    assert view["answers"] == YES and view["role_in_use"] == "administrator"
    (row,) = admissions.rows(plant)
    (record,) = admissions.records(plant)
    assert str(row["admission_id"]) == view["admission_id"]
    assert (row["result"], row["failed_criterion"]) == ("admitted", None)
    assert json.loads(row["answers"]) == YES
    assert row["ledger_record_id"] == record["record_id"]
    assert str(row["evaluated_by"]) == view["evaluated_by"]
    # Cadena de la planta, source_key = admission_id, autor con su rol en uso (BR-GOB-15).
    assert record["plant_id"] == plant and record["source_key"] == view["admission_id"]
    assert record["actor_role_in_use"] == "administrator"
    content = json.loads(record["content"])
    assert content["result"] == "admitted" and "failed_criterion" not in content
    assert content["justification_es"] == "Hay estándar escrito de resguardos"
    listed = admissions.get(cookie, plant).json()
    assert [item["admission_id"] for item in listed["admissions"]] == [view["admission_id"]]


def test_a_rejection_is_committed_before_answering_and_a_retry_creates_another(
    admissions: Admissions,
) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    cookie = admissions.member(site)

    first = admissions.post(cookie, plant, _body(subject=False))
    second = admissions.post(cookie, plant, _body(subject=False))

    for response in (first, second):
        assert _code(response) == (400, "invalid_request", "catalog_admission_rejected")
        assert "failed_criterion" not in response.json()  # ApiError cerrado: va al registro
    rows = admissions.rows(plant)
    records = admissions.records(plant)
    assert len(rows) == 2 and len(records) == 2
    assert rows[0]["admission_id"] != rows[1]["admission_id"]
    assert [(r["result"], r["failed_criterion"]) for r in rows] == [("rejected", "subject")] * 2
    assert [json.loads(r["content"])["failed_criterion"] for r in records] == ["subject"] * 2
    assert {r["ledger_record_id"] for r in rows} == {r["record_id"] for r in records}
    listed = admissions.get(cookie, plant).json()["admissions"]
    assert [(i["result"], i["failed_criterion"]) for i in listed] == [("rejected", "subject")] * 2
    # Tras los rechazos, las tres afirmativas admiten (el rechazo no bloquea).
    assert admissions.post(cookie, plant, _body()).status_code == 201


@pytest.mark.parametrize(
    ("answers", "criterion"),
    [
        ({"standard": False, "remedy": False, "subject": False}, "standard"),
        ({"standard": False, "subject": False}, "standard"),
        ({"remedy": False, "subject": False}, "remedy"),
        ({"remedy": False}, "remedy"),
        ({"subject": False}, "subject"),
    ],
)
def test_the_recorded_failed_criterion_is_the_first_negative_answer(
    admissions: Admissions, answers: dict[str, bool], criterion: str
) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))

    response = admissions.post(admissions.member(site), plant, _body("dwell", **answers))

    assert _code(response) == (400, "invalid_request", "catalog_admission_rejected")
    (row,) = admissions.rows(plant)
    (record,) = admissions.records(plant)
    assert row["failed_criterion"] == criterion
    assert json.loads(record["content"])["failed_criterion"] == criterion


# --- BR-GOB-14: una sola vez por (planta, familia) ----------------------------------------------


@pytest.mark.parametrize("answers", [YES, {**YES, "remedy": False}])
def test_an_admitted_family_answers_conflict_and_writes_nothing(
    admissions: Admissions, answers: dict[str, bool]
) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    cookie = admissions.member(site)
    assert admissions.post(cookie, plant, _body("startup_transition")).status_code == 201
    before = (admissions.rows(plant), admissions.records(plant))

    response = admissions.post(cookie, plant, {"family": "startup_transition", "answers": answers})

    assert _code(response) == (409, "conflict", "catalog_family_already_admitted")
    assert (admissions.rows(plant), admissions.records(plant)) == before
    # Otra familia de la misma planta sí se evalúa.
    assert admissions.post(cookie, plant, _body("coexistence")).status_code == 201


def test_concurrent_admissions_leave_exactly_one_admitted_and_one_record(
    admissions: Admissions,
) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    cookies = [admissions.member(site) for _ in range(CONCURRENT)]
    repository = BarrierRepository(admissions.database, CONCURRENT)
    _, app = admissions.build(repository)

    async def race() -> list[httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        ) as client:
            return list(
                await asyncio.gather(
                    *(
                        client.post(
                            f"/plants/{plant}/admissions",
                            json=_body("coexistence"),
                            headers=admissions.headers(cookie),
                        )
                        for cookie in cookies
                    )
                )
            )

    responses = admissions.run(race())

    codes = sorted(_code(r)[0] for r in responses)
    assert codes == [201] + [409] * (CONCURRENT - 1), [r.text for r in responses]
    assert all(
        _code(r) == (409, "conflict", "catalog_family_already_admitted")
        for r in responses
        if r.status_code != 201
    )
    admitted = [r for r in admissions.rows(plant) if r["result"] == "admitted"]
    assert len(admitted) == 1
    (record,) = admissions.records(plant)
    assert admitted[0]["ledger_record_id"] == record["record_id"]


# --- BR-GOB-16: no se hereda entre plantas ni entre organizaciones ------------------------------


def test_admission_for_never_crosses_plants_or_organizations(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=2, zones_per_plant=1)
    other = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant_a, plant_b = list(site.plants)
    cookie = admissions.member(site)
    assert admissions.post(cookie, plant_a, _body("guard_bypass")).status_code == 201
    service = admissions.service
    own = unit_context(site.organization_id, ActorUnit.U03)
    foreign = unit_context(other.organization_id, ActorUnit.U03)
    family = PredicateFamily.GUARD_BYPASS

    found = admissions.run(service.admission_for(own, plant_a, family))
    other_plant = admissions.run(service.admission_for(own, plant_b, family))
    other_organization = admissions.run(service.admission_for(foreign, plant_a, family))
    other_family = admissions.run(service.admission_for(own, plant_a, PredicateFamily.DWELL))

    assert found is not None and found.plant_id == plant_a and found.family is family
    assert other_plant is None and other_organization is None and other_family is None
    # La planta B evalúa la misma familia por su cuenta (la comprobación previa filtra planta).
    assert admissions.post(cookie, plant_b, _body("guard_bypass")).status_code == 201
    assert admissions.run(service.admission_for(own, plant_b, family)) is not None


def test_the_statements_filter_the_organization_even_without_rls(admissions: Admissions) -> None:
    # Como superusuario la RLS no aplica: lo que separa las organizaciones es el filtro de
    # organización de cada sentencia (defensa en profundidad; sin él, esta prueba falla).
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    other = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    assert admissions.post(admissions.member(site), plant, _body("dwell")).status_code == 201
    migrated = admissions.authz.sessions.migrated
    database = app_database(migrated, url=migrated.as_role().sqlalchemy_url, worker_pool_size=1)
    repository = PostgresAdmissionRepository(database)
    own = unit_context(site.organization_id, ActorUnit.U03)
    foreign = unit_context(other.organization_id, ActorUnit.U03)
    family = PredicateFamily.DWELL

    async def page(context: Any) -> Any:
        async with database.transaction(context) as transaction:
            return await repository.page(transaction, plant, after=None, limit=10)

    try:
        assert admissions.run(repository.admitted(own, plant, family)) is not None
        assert len(admissions.run(page(own))) == 1
        assert admissions.run(repository.plant_exists(own, plant))
        assert admissions.run(repository.admitted(foreign, plant, family)) is None
        assert admissions.run(page(foreign)) == ()
        assert not admissions.run(repository.plant_exists(foreign, plant))
    finally:
        admissions.run(database.dispose())


def test_admission_for_ignores_rejected_evaluations(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    admissions.post(admissions.member(site), plant, _body("dwell", standard=False))
    context = unit_context(site.organization_id, ActorUnit.U03)

    found = admissions.run(admissions.service.admission_for(context, plant, PredicateFamily.DWELL))

    assert found is None


def test_the_listing_shows_only_the_requested_plant(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=2, zones_per_plant=1)
    plant_a, plant_b = list(site.plants)
    cookie = admissions.member(site)
    admissions.post(cookie, plant_a, _body("dwell"))

    listed_b = admissions.get(cookie, plant_b)

    assert listed_b.status_code == 200 and listed_b.json() == {"admissions": [], "next_after": None}
    assert len(admissions.get(cookie, plant_a).json()["admissions"]) == 1


def test_a_plant_administrator_admits_only_in_their_plant(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=2, zones_per_plant=1)
    plant_a, plant_b = list(site.plants)
    cookie = admissions.member(site, Role.ADMINISTRATOR, ScopeLevel.PLANT, plant_a)
    reader = admissions.member(site, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_a)

    allowed = admissions.post(cookie, plant_a, _body("coexistence"))
    elsewhere = admissions.post(cookie, plant_b, _body("coexistence"))
    missing = admissions.post(cookie, uuid.uuid4(), _body("coexistence"))

    assert allowed.status_code == 201, allowed.text
    assert _code(elsewhere) == _code(missing) == (404, "not_found", None)
    assert admissions.rows(plant_b) == []
    assert admissions.get(reader, plant_a).status_code == 200
    assert _code(admissions.get(reader, plant_b)) == (404, "not_found", None)


def test_without_catalog_manage_nobody_admits(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    for role in (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST):
        response = admissions.post(admissions.member(site, role), plant, _body())
        assert _code(response) == (404, "not_found", None), role
    assert admissions.rows(plant) == []


def test_another_organization_plant_answers_like_a_missing_one(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    other = admissions.authz.add_site(plants=1, zones_per_plant=1)
    foreign_plant = next(iter(other.plants))
    cookie = admissions.member(site)

    foreign = admissions.post(cookie, foreign_plant, _body())
    missing = admissions.post(cookie, uuid.uuid4(), _body())

    assert _code(foreign) == _code(missing) == (404, "not_found", None)
    assert _code(admissions.get(cookie, foreign_plant)) == (404, "not_found", None)
    assert admissions.rows(foreign_plant) == []


def test_under_concession_the_installer_reads_audited_and_never_admits(
    admissions: Admissions,
) -> None:
    authz = admissions.authz
    site = authz.add_site(plants=2, zones_per_plant=1)
    plant_a, plant_b = list(site.plants)
    assert admissions.post(admissions.member(site), plant_a, _body("dwell")).status_code == 201
    installer = authz.add_provider_user()
    concession = authz.add_concession(
        site.organization_id,
        installer,
        level=ScopeLevel.PLANT,
        scope_id=plant_a,
        granted_at=authz.now() - timedelta(hours=1),
    )
    cookie = authz.open_session(authz.provider_organization_id, installer)
    headers = {**admissions.headers(cookie), "X-Vigia-Concession": str(concession)}

    def send(method: str, plant: uuid.UUID, body: Any = None) -> httpx.Response:
        response: httpx.Response = admissions.run(
            admissions.client.request(
                method, f"/plants/{plant}/admissions", json=body, headers=headers
            )
        )
        return response

    listed = send("GET", plant_a)
    other_plant = send("GET", plant_b)
    admit = send("POST", plant_a, _body("coexistence"))

    assert listed.status_code == 200, listed.text
    assert [item["family"] for item in listed.json()["admissions"]] == ["dwell"]
    assert _code(other_plant) == (404, "not_found", None)
    assert _code(admit) == (404, "not_found", None)  # catalog.manage no está en su columna
    assert [r["family"] for r in admissions.rows(plant_a)] == ["dwell"]
    audited = admissions.fetch(
        "SELECT operation, scope_plant_id, result_count FROM shared.audit_entry"
        " WHERE organization_id = $1 AND actor_concession_id = $2 AND operation = 'catalog_read'",
        site.organization_id,
        concession,
    )
    assert [(r["scope_plant_id"], r["result_count"]) for r in audited] == [(plant_a, 1)]
    # Las lecturas de la propia organización no se auditan con este valor (A-50).
    admissions.get(admissions.member(site), plant_a)
    (count,) = admissions.fetch(
        "SELECT count(*) AS n FROM shared.audit_entry WHERE organization_id = $1"
        " AND operation = 'catalog_read'",
        site.organization_id,
    )
    assert count["n"] == 1


# --- G-6 y texto libre -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _body("productivity"),
        _body("heat_map"),
        _body("Coexistence"),
        {"family": "coexistence", "answers": {**YES, "standard": "true"}},
        {"family": "coexistence", "answers": {**YES, "subject": 1}},
        {"family": "coexistence", "answers": {"standard": True, "remedy": True}},
        {"family": "coexistence", "answers": YES, "override": True},
        {"family": "coexistence", "answers": {**YES, "skip": True}},
        {"family": "coexistence", "answers": YES, "result": "admitted"},
    ],
    ids=[
        "productivity",
        "heat-map",
        "case",
        "string-answer",
        "int-answer",
        "missing-answer",
        "override",
        "extra-answer",
        "result",
    ],
)
def test_g6_bodies_outside_the_contract_are_invalid_and_write_nothing(
    admissions: Admissions, body: Any
) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))

    response = admissions.post(admissions.member(site), plant, body)

    assert _code(response) == (400, "invalid_request", None)
    assert admissions.rows(plant) == [] and admissions.records(plant) == []


@pytest.mark.parametrize(
    "justification",
    [
        "<b>Hay estándar</b>",
        "&lt;script&gt;",
        "Hubo sabotaje en la línea",
        "Manipulación deliberada del resguardo",
        "SABOTAJE",
        "x" * 2001,
        "",
    ],
    ids=["markup", "entity", "intent", "deliberate", "upper", "too-long", "empty"],
)
def test_a_rejected_justification_answers_free_text_rejected_and_writes_nothing(
    admissions: Admissions, justification: str
) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    body = {**_body(), "justification_es": justification}

    response = admissions.post(admissions.member(site), plant, body)

    assert _code(response) == (400, "invalid_request", "catalog_free_text_rejected")
    assert admissions.rows(plant) == [] and admissions.records(plant) == []


def test_the_justification_is_stored_in_nfc(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    decomposed = "Estándar escrito y remedio de ingeniería"  # «í» descompuesta (NFD)

    response = admissions.post(
        admissions.member(site), plant, {**_body(), "justification_es": decomposed}
    )

    assert response.status_code == 201, response.text
    (row,) = admissions.rows(plant)
    (record,) = admissions.records(plant)
    assert row["justification_es"] == "Estándar escrito y remedio de ingeniería"
    assert json.loads(record["content"])["justification_es"] == row["justification_es"]


# --- Listado por páginas -----------------------------------------------------------------------


def test_the_listing_pages_by_key_newest_first(admissions: Admissions) -> None:
    site = admissions.authz.add_site(plants=1, zones_per_plant=1)
    plant = next(iter(site.plants))
    cookie = admissions.member(site)
    for _ in range(5):
        admissions.authz.sessions.clock.advance(1)
        admissions.post(cookie, plant, _body(remedy=False))

    seen: list[str] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if after is not None:
            params["after"] = after
        page = admissions.get(cookie, plant, **params).json()
        seen.extend(item["admission_id"] for item in page["admissions"])
        pages += 1
        after = page["next_after"]
        if after is None:
            break

    expected = [str(r["admission_id"]) for r in reversed(admissions.rows(plant))]
    assert seen == expected and pages == 3
    assert _code(admissions.get(cookie, plant, after="no-es-un-cursor!")) == (
        400,
        "invalid_request",
        None,
    )
    assert _code(admissions.get(cookie, plant, limit=201)) == (400, "invalid_request", None)
