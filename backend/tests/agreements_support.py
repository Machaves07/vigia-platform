"""Entorno de las pruebas del acuerdo de uso, sus firmantes y la transparencia (TASK-212).

Sobre ``gates_world`` (servicios reales de ``catalog.gates`` como ``vigia_app``) añade los de
``catalog.agreements``: ``SignatoryPolicyService``, ``AgreementService`` (con
``HierarchyService.users_by_role_and_scope`` real) y ``TransparencyService``, y la aplicación real
(``World().app`` con la cadena fija de middleware y ``ContextAuthorizer``) con esas rutas y las de
compuertas instaladas en ``app.state``.

``Ready`` deja una zona a un paso de la aprobación: montaje aprobado por un acta de alcance real,
política de firmantes, política de planta y acta de comisionamiento cerrada (insertadas por
repositorio: su cierre real es de TASK-216), tres firmantes de la organización (``coordinator_sst``
y ``plant_manager`` de gestión, ``copasst`` de los trabajadores) y el acuerdo creado por el
instalador bajo concesión. Cada pieza se puede omitir para probar su guarda.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.authz_support import Site
from tests.gates_support import HOUR, GatesWorld, gates_world
from tests.identity_db import BASE_TIME
from tests.integration.conftest import PostgresEndpoint
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.plant_policy_repository import (
    PostgresPlantPolicyRepository,
)
from vigia_platform.catalog.application.agreements import (
    AgreementApproval,
    AgreementRequest,
    AgreementService,
    ConfirmationResult,
)
from vigia_platform.catalog.application.gates import GateService
from vigia_platform.catalog.application.signatory_policy import SignatoryPolicyService
from vigia_platform.catalog.application.transparency import TransparencyService
from vigia_platform.catalog.domain.agreements import UseAgreement
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.registry import RecordType
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
POLICY_ROLES: Final = (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST)
SIGNER_ROLES: Final = (Role.COORDINATOR_SST, Role.PLANT_MANAGER, Role.COPASST)


@dataclass
class Signer:
    """Un firmante de la organización: su usuario, su rol, su contexto y su sesión."""

    user_id: uuid.UUID
    role: Role
    context: ScopeContext
    cookie: SessionCookie


@dataclass
class Ready:
    """Una zona con todo lo que la aprobación exige (salvo lo que se pidió omitir)."""

    site: Site
    plant: uuid.UUID
    zone: uuid.UUID
    installer: ScopeContext
    signers: list[Signer]
    agreement: UseAgreement | None = None
    commissioning_record_id: uuid.UUID | None = None

    @property
    def copasst(self) -> Signer:
        return next(s for s in self.signers if s.role is Role.COPASST)


@dataclass
class AgreementsWorld:
    gates_world: GatesWorld
    repository: PostgresAgreementRepository = field(default_factory=PostgresAgreementRepository)
    agreements: AgreementService = field(init=False)
    signatory_policies: SignatoryPolicyService = field(init=False)
    transparency: TransparencyService = field(init=False)
    client: httpx.AsyncClient = field(init=False)

    def __post_init__(self) -> None:
        self.agreements = self.build_agreements()
        self.signatory_policies = self.build_signatory_policies()
        self.transparency = self.build_transparency()

    # --- Servicios -----------------------------------------------------------------------------

    @property
    def g(self) -> GatesWorld:
        return self.gates_world

    def build_agreements(
        self,
        gates: GateService | None = None,
        repository: PostgresAgreementRepository | None = None,
    ) -> AgreementService:
        g = self.g
        return AgreementService(
            repository=repository or self.repository,
            gates=gates or g.gates,
            policies=PostgresPlantPolicyRepository(g.database),
            documents=g.documents,
            identity=g.hierarchy,
            database=g.database,
            writer=g.writer,
            authorizer=g.authz.authorizer,
            clock=g.authz.sessions.clock,
        )

    def build_signatory_policies(self) -> SignatoryPolicyService:
        g = self.g
        return SignatoryPolicyService(
            repository=self.repository,
            plants=PostgresPlantPolicyRepository(g.database),
            database=g.database,
            authorizer=g.authz.authorizer,
            audit=g.authz.sessions.audit,
            clock=g.authz.sessions.clock,
        )

    def build_transparency(self) -> TransparencyService:
        g = self.g
        return TransparencyService(
            repository=self.repository,
            gates=g.gates,
            catalog=PostgresCatalogRepository(g.database),
            database=g.database,
            audit=g.authz.sessions.audit,
        )

    def install(self) -> None:
        g = self.g
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
                        gates=g.gates,
                        scope_records=g.records,
                        plant_policies=g.policies,
                        documents=g.documents,
                        signatory_policies=self.signatory_policies,
                        agreements=self.agreements,
                        transparency=self.transparency,
                    )
                },
            },
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.g.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.g.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.g.execute(sql, *args)

    def request(
        self,
        method: str,
        path: str,
        cookie: SessionCookie,
        json_body: Any = None,
        concession: uuid.UUID | None = None,
    ) -> httpx.Response:
        self.g.advance()
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.request(method, path, json=json_body, headers=headers)
        )
        return response

    # --- Personas ------------------------------------------------------------------------------

    def signer(
        self,
        site: Site,
        role: Role,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
        *extra: tuple[Role, ScopeLevel, uuid.UUID | None],
    ) -> Signer:
        authz = self.g.authz
        user_id = authz.add_user(site.organization_id)
        authz.assign(site.organization_id, user_id, role, level, scope_id)
        for other_role, other_level, other_scope in extra:
            authz.assign(site.organization_id, user_id, other_role, other_level, other_scope)
        cookie: SessionCookie = authz.open_session(site.organization_id, user_id)
        scope = self.run(authz.contexts.context_from_session(cookie))
        return Signer(user_id, role, scope.context, cookie)

    def installer_session(
        self,
        site: Site,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> tuple[ScopeContext, SessionCookie, uuid.UUID]:
        """Instalador con concesión: su contexto, su cookie y la concesión (para HTTP)."""
        authz = self.g.authz
        installer = authz.add_provider_user()
        concession = authz.add_concession(
            site.organization_id,
            installer,
            level=level,
            scope_id=scope_id,
            granted_at=authz.now() - HOUR,
        )
        cookie: SessionCookie = authz.open_session(authz.provider_organization_id, installer)
        scope = self.run(authz.contexts.context_from_session(cookie, concession_id=concession))
        return scope.context, cookie, concession

    # --- Lo que la aprobación exige ------------------------------------------------------------

    def signatory_policy(
        self, site: Site, plant: uuid.UUID, roles: Sequence[Role] = POLICY_ROLES, minimum: int = 3
    ) -> None:
        self.execute(
            "INSERT INTO catalog.plant_signatory_policy (plant_id, organization_id, required_roles,"
            " minimum, updated_by, updated_at) VALUES ($1, $2, $3, $4, $5, $6)"
            " ON CONFLICT (plant_id) DO UPDATE SET required_roles = EXCLUDED.required_roles,"
            " minimum = EXCLUDED.minimum",
            plant,
            site.organization_id,
            [role.value for role in roles],
            minimum,
            self.g.authz.operator_id,
            self.g.authz.now(),
        )

    def plant_policy(self, site: Site, plant: uuid.UUID) -> None:
        self.execute(
            "INSERT INTO catalog.plant_policy (policy_id, organization_id, plant_id, version,"
            " signed_at, signed_by_display_name, legal_opinion_reference, document_ref,"
            " criteria_summary_es, loaded_by, loaded_at, ledger_record_id)"
            " VALUES ($1, $2, $3, 1, $4, 'Firmante sintético', 'REF-1', '{}', 'Resumen sintético',"
            " $5, $4, $6)",
            uuid.uuid4(),
            site.organization_id,
            plant,
            BASE_TIME,
            self.g.authz.operator_id,
            uuid.uuid4(),
        )

    def commissioning_record(self, site: Site, plant: uuid.UUID, zone: uuid.UUID) -> uuid.UUID:
        """Acta de comisionamiento cerrada de la zona, por repositorio (su cierre: TASK-216)."""
        (node,) = self.fetch(
            "SELECT node_id FROM identity.zone_node_assignment WHERE zone_id = $1 LIMIT 1", zone
        )
        session_id, record_id = uuid.uuid4(), uuid.uuid4()
        self.execute(
            "INSERT INTO catalog.walk_test_session (session_id, organization_id, plant_id, zone_id,"
            " node_id, catalog_version, kind, status, passes_per_cell, matrix_rows, started_at,"
            " last_activity_at, closed_at, commissioning_record_id)"
            " VALUES ($1, $2, $3, $4, $5, 1, 'initial', 'closed', 3, '[]', $6, $6, $6, $7)",
            session_id,
            site.organization_id,
            plant,
            zone,
            node["node_id"],
            BASE_TIME,
            record_id,
        )
        self.execute(
            "INSERT INTO catalog.commissioning_record (commissioning_record_id, organization_id,"
            " plant_id, zone_id, session_id, catalog_version, matrix_results,"
            " false_negatives_total, false_alarm_rate_observed, false_alarm_threshold, latency,"
            " installer_measurements, cameras_measured, occlusion_summary, total_hours,"
            " steps_summary, signatures, closed_at, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, 1, '[]', 0, 0, 0, '{}', '{}', '[]', '[]', 0, '[]', '[]',"
            " $6, $7)",
            record_id,
            site.organization_id,
            plant,
            zone,
            session_id,
            BASE_TIME,
            uuid.uuid4(),
        )
        return record_id

    def mount(
        self,
        site: Site,
        plant: uuid.UUID,
        zone: uuid.UUID,
        installer: ScopeContext,
        catalog: Mapping[str, Any] | None = None,
    ) -> None:
        """Catálogo, nodo y montaje aprobado por un acta de alcance real."""
        cameras = self.g.equip(site, plant, zone, payload=catalog)
        self.g.file(installer, zone, self.g.scope_request(installer, plant, cameras))

    def ready(
        self,
        *,
        mounted: bool = True,
        record: bool = True,
        plant_policy: bool = True,
        signatory_policy: bool = True,
        create: bool = True,
        confirm: Sequence[Role] = SIGNER_ROLES,
        site: Site | None = None,
        catalog: Mapping[str, Any] | None = None,
    ) -> Ready:
        g = self.g
        site = site or g.site()
        ((plant, zone), *_) = site.zones()
        installer = g.installer(site)
        if mounted:
            self.mount(site, plant, zone, installer, catalog)
        else:
            g.equip(site, plant, zone, payload=catalog)
        ready = Ready(site, plant, zone, installer, [self.signer(site, r) for r in SIGNER_ROLES])
        if record:
            ready.commissioning_record_id = self.commissioning_record(site, plant, zone)
        if plant_policy:
            self.plant_policy(site, plant)
        if signatory_policy:
            self.signatory_policy(site, plant)
        if create:
            ready.agreement = self.create(ready)
            for signer in ready.signers:
                if signer.role in confirm:
                    self.confirm(signer.context, ready.agreement.agreement_id)
        return ready

    def request_of(
        self, ready: Ready, replaces: uuid.UUID | None = None, **changes: Any
    ) -> AgreementRequest:
        fields: dict[str, Any] = {
            "signatories": [(s.role, s.user_id) for s in ready.signers],
            "document_ref": None,
            "replaces_agreement_id": replaces,
        }
        fields.update(changes)
        return AgreementRequest(**fields)

    def create(self, ready: Ready, **changes: Any) -> UseAgreement:
        self.g.advance()
        agreement: UseAgreement = self.run(
            self.agreements.create(ready.installer, ready.zone, self.request_of(ready, **changes))
        )
        return agreement

    def confirm(self, context: ScopeContext, agreement_id: uuid.UUID) -> ConfirmationResult:
        self.g.advance()
        result: ConfirmationResult = self.run(self.agreements.confirm(context, agreement_id))
        return result

    def approve(
        self,
        context: ScopeContext,
        agreement_id: uuid.UUID,
        service: AgreementService | None = None,
    ) -> AgreementApproval:
        self.g.advance()
        approval: AgreementApproval = self.run(
            (service or self.agreements).approve(context, agreement_id)
        )
        return approval

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def agreement_row(self, agreement_id: uuid.UUID) -> Any:
        (row,) = self.fetch(
            "SELECT status, approved_at, approved_by, ledger_record_id, superseded_at, revoked_at,"
            " signatories::text AS signatories FROM catalog.use_agreement WHERE agreement_id = $1",
            agreement_id,
        )
        return row

    def confirmation_rows(self, agreement_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT user_id, role_in_use, confirmed_at, origin FROM catalog.agreement_confirmation"
            " WHERE agreement_id = $1 ORDER BY confirmed_at, user_id",
            agreement_id,
        )

    def usage_history(self, zone: uuid.UUID) -> list[Any]:
        return [row for row in self.g.history(zone) if row["gate"] == "usage"]

    def records(self, zone: uuid.UUID, record_type: str) -> list[Any]:
        return [
            {**dict(r), "content": json.loads(r["content"])}
            for r in self.g.records_of(zone)
            if r["record_type"] == record_type
        ]

    def events(self, plant: uuid.UUID, zone: uuid.UUID, name: str | None = None) -> list[Any]:
        return [
            {"event_name": r["event_name"], "payload": json.loads(r["payload"])}
            for r in self.g.events(plant, zone)
            if name is None or r["event_name"] == name
        ]

    def written(self, plant: uuid.UUID, zone: uuid.UUID) -> tuple[Any, ...]:
        """Compuertas, registros, eventos, acuerdos y confirmaciones de la zona."""
        agreements = self.fetch(
            "SELECT agreement_id, status, approved_at, superseded_at, revoked_at"
            " FROM catalog.use_agreement WHERE zone_id = $1 ORDER BY agreement_id",
            zone,
        )
        confirmations = self.fetch(
            "SELECT c.agreement_id, c.user_id FROM catalog.agreement_confirmation AS c"
            " JOIN catalog.use_agreement AS a USING (agreement_id) WHERE a.zone_id = $1"
            " ORDER BY c.agreement_id, c.user_id",
            zone,
        )
        return (
            self.g.written(plant, zone),
            [tuple(r) for r in agreements],
            [tuple(r) for r in confirmations],
        )


@contextmanager
def agreements_world(
    endpoint: PostgresEndpoint, prefix: str, extra_types: Sequence[RecordType] = ()
) -> Iterator[AgreementsWorld]:
    """El entorno de ``AgreementsWorld`` sobre una base migrada propia, con su cliente HTTP."""
    with gates_world(endpoint, prefix, extra_types) as g:
        world = AgreementsWorld(g)
        world.install()
        try:
            yield world
        finally:
            g.run(world.client.aclose())
