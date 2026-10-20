"""La revisión mensual de TASK-347 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``monthly_review_stack`` se apoya en ``tests/loop_actions_support.py`` (el mundo de U-04 con
hallazgos ingresados por las rutas del contrato de U-03, el escritor con la política de texto
libre de producción y los servicios reales del lazo) y añade:

- ``SampleAuditService`` y ``CommitmentService`` reales sobre la misma base y el mismo escritor;
- una aplicación real (``create_app`` con la cadena fija de middleware y ``ContextAuthorizer``)
  con ``LoopHttp`` **y** ``AccreditationHttp`` en ``app.state``: las rutas del lazo y las de la
  acreditación responden con sus servicios reales (sin límite de tasa: no es el asunto);
- la siembra de un paquete mensual **sellado** con su muestra, directamente por SQL como
  superusuario (como ``tests/integration/test_monthly_package_routes.py``), pero sobre
  **clasificaciones reales**: cada elemento de la muestra apunta a una fila de
  ``loop.classification`` y a su registro ``classification`` del expediente, escritos por el
  ``ClassificationService`` real;
- las lecturas de lo que una operación no puede cambiar (registros ``classification`` con sus
  hashes, filas de ``loop.classification``, ``loop.finding_state`` y ``ledger.label``) y de lo
  que sí (``accreditation.commitment``, ``audit_sample_item``, registros nuevos).

Solo datos generados. Reloj simulado desde la hora de la base (retro 14); topes de 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import secrets
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization
from tests.ledger_database import set_organization
from tests.loop_actions_support import ActionsStack, Who, actions_stack
from tests.middleware_support import UnlimitedRateLimiter
from vigia_platform.accreditation.adapters.http.monthly_routes import MonthlyPackageReads
from vigia_platform.accreditation.adapters.http.services import (
    ACCREDITATION_STATE_KEY,
    AccreditationHttp,
)
from vigia_platform.accreditation.monthly.audit import SampleAuditService
from vigia_platform.accreditation.monthly.commitments import CommitmentService
from vigia_platform.accreditation.monthly.repository import (
    MonthlyPackageRepository,
    period_to_date,
)
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeLevel

__all__ = [
    "COMMITMENT_TEXT",
    "CONCESSION_HEADER",
    "MonthlyReviewStack",
    "SampleSeed",
    "monthly_review_stack",
]

SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
COMMITMENT_TEXT: Final = "Revisar la señalización del pasillo peatonal de la zona de carga"
_EMPTY_FINDINGS: Final = {
    "received": 0,
    "classified": 0,
    "confirmed": 0,
    "discarded": 0,
    "closed": 0,
    "overdue": 0,
    "unclosable": 0,
    "reopened": 0,
}


@dataclass(frozen=True)
class SampleSeed:
    """Un paquete sellado con su muestra: los elementos, en el orden de ``classifications``."""

    organization: LoopOrganization
    zone_id: uuid.UUID
    package_id: uuid.UUID
    sample_id: uuid.UUID
    item_ids: tuple[uuid.UUID, ...]
    classification_ids: tuple[uuid.UUID, ...]


@dataclass
class MonthlyReviewStack:
    actions: ActionsStack
    audit: SampleAuditService
    commitments: CommitmentService
    client: httpx.AsyncClient
    _periods: Iterator[str]

    # --- utilidades ---------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.actions.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.actions.fetch(sql, *args)

    def tick(self, seconds: float = 1.0) -> None:
        self.actions.tick(seconds)

    def now(self) -> Any:
        return self.actions.now()

    def uuid7(self) -> uuid.UUID:
        return self.actions.world.stack.uuid7()

    def next_period(self) -> str:
        """Un mes distinto por paquete sembrado (``UNIQUE (organization_id, zone_id, period)``)."""
        return next(self._periods)

    def admin(self, organization_id: uuid.UUID, sql: str, *args: Any) -> None:
        """Una sentencia de superusuario dentro de ``set_organization``."""
        admin = self.actions.world.stack.authz.sessions.admin

        async def run() -> None:
            async with admin.transaction():
                await set_organization(admin, organization_id)
                await admin.execute(sql, *args)

        self.run(run())

    def service_like(self, cls: type[Any], **changes: Any) -> Any:
        """Otro servicio (p. ej. una subclase con gancho) sobre la misma base y escritor."""
        return self.actions.service_like(cls, **changes)

    # --- personas -----------------------------------------------------------------------------

    def user(
        self,
        organization: LoopOrganization,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> uuid.UUID:
        return self.actions.user(organization, role, level, scope)

    def member(
        self,
        organization: LoopOrganization,
        role: Role = Role.COORDINATOR_SST,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope: uuid.UUID | None = None,
    ) -> Who:
        return self.actions.member(organization, role, level, scope)

    def with_role(
        self,
        organization: LoopOrganization,
        who: Who,
        role: Role,
        level: ScopeLevel,
        scope: uuid.UUID | None,
    ) -> Who:
        """La misma persona con otra asignación más (y una sesión nueva que la recoge)."""
        authz = self.actions.world.stack.authz
        cookie, concession = who
        user_id = self.run(authz.contexts.context_from_session(cookie)).context.actor.id
        authz.assign(organization.organization_id, user_id, role, level, scope)
        return authz.open_session(organization.organization_id, user_id), concession

    def line_manager(self, organization: LoopOrganization, zone_id: uuid.UUID) -> Who:
        """Mando de línea con alcance de **una** zona."""
        return self.member(organization, Role.LINE_MANAGER, ScopeLevel.ZONE, zone_id)

    def installer(self, organization: LoopOrganization) -> Who:
        return self.actions.installer(organization)

    def context(self, who: Who) -> Any:
        return self.actions.context(who)

    # --- sitio --------------------------------------------------------------------------------

    def zone_in(self, organization: LoopOrganization) -> uuid.UUID:
        """Otra zona (sin nodo) de la planta del sitio de ``organization``."""
        zone_id = uuid.uuid4()
        self.actions.execute(
            "INSERT INTO identity.zone (zone_id, organization_id, plant_id, code, name, created_at,"
            " created_by) VALUES ($1, $2, $3, $4, 'Zona sintética', $5, $6)",
            zone_id,
            organization.organization_id,
            organization.plant_id,
            f"ZN-{secrets.token_hex(6).upper()}",
            self.now(),
            self.actions.world.stack.authz.operator_id,
        )
        return zone_id

    # --- clasificaciones reales ---------------------------------------------------------------

    def classified(self, organization: LoopOrganization) -> tuple[IngestedSubject, uuid.UUID]:
        """Un hallazgo confirmado por el ``ClassificationService`` real y su clasificación."""
        subject = self.actions.opened(organization)
        (row,) = self.fetch(
            "SELECT classification_id FROM loop.classification"
            " WHERE organization_id = $1 AND subject_id = $2",
            organization.organization_id,
            subject.subject_id,
        )
        return subject, uuid.UUID(str(row["classification_id"]))

    # --- siembra del paquete y la muestra -----------------------------------------------------

    def seed_package(
        self,
        organization: LoopOrganization,
        *,
        zone_id: uuid.UUID | None = None,
        state: str = "sealed",
        period: str | None = None,
    ) -> uuid.UUID:
        package_id = self.uuid7()
        sealed = state == "sealed"
        self.admin(
            organization.organization_id,
            "INSERT INTO accreditation.monthly_package (package_id, organization_id, plant_id,"
            " zone_id, period, state, generated_at, findings_summary, recurrence_summary,"
            " coverage_summary, commitments_from_previous_period, sealed_checkpoint_record_id,"
            " record_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, '[]'::jsonb, '{}'::jsonb,"
            " '[]'::jsonb, $9, $10)",
            package_id,
            organization.organization_id,
            organization.plant_id,
            zone_id or organization.zone_id,
            period_to_date(period or self.next_period()),
            state,
            self.now(),
            json.dumps(_EMPTY_FINDINGS),
            self.uuid7() if sealed else None,
            self.uuid7() if sealed else None,
        )
        return package_id

    def seed_sample(
        self,
        organization: LoopOrganization,
        classifications: Sequence[tuple[IngestedSubject, uuid.UUID]],
        *,
        state: str = "sealed",
        attach: bool = True,
    ) -> SampleSeed:
        """Un paquete con su muestra sobre ``classifications`` (todas de la zona del sitio).

        ``attach=False`` deja la muestra sin enlazar al paquete (la de una composición que perdió
        la carrera de sellado)."""
        package_id = self.seed_package(organization, state=state)
        sample_id = self.uuid7()
        reviewer = self.user(organization)
        item_ids = tuple(self.uuid7() for _ in classifications)
        count = len(classifications)
        self.admin(
            organization.organization_id,
            "INSERT INTO accreditation.audit_sample (sample_id, organization_id, plant_id, zone_id,"
            " package_id, population_size, sample_size, seed, strata, selected_at,"
            " reviewer_user_id) VALUES ($1, $2, $3, $4, $5, $6, $6, 12345, '[]'::jsonb, $7, $8)",
            sample_id,
            organization.organization_id,
            organization.plant_id,
            organization.zone_id,
            package_id,
            count,
            self.now(),
            reviewer,
        )
        for item_id, (subject, classification_id) in zip(item_ids, classifications, strict=True):
            self.admin(
                organization.organization_id,
                "INSERT INTO accreditation.audit_sample_item (sample_item_id, organization_id,"
                " plant_id, zone_id, sample_id, classification_id, subject_id)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                item_id,
                organization.organization_id,
                organization.plant_id,
                organization.zone_id,
                sample_id,
                classification_id,
                subject.subject_id,
            )
        if attach:
            self.admin(
                organization.organization_id,
                "UPDATE accreditation.monthly_package SET audit_sample_id = $1"
                " WHERE package_id = $2",
                sample_id,
                package_id,
            )
        return SampleSeed(
            organization=organization,
            zone_id=organization.zone_id,
            package_id=package_id,
            sample_id=sample_id,
            item_ids=item_ids,
            classification_ids=tuple(classification_id for _, classification_id in classifications),
        )

    # --- peticiones ---------------------------------------------------------------------------

    def send(
        self,
        who: Who,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.request(
                method,
                path,
                json=json_body,
                params=dict(params) if params else None,
                headers=headers,
            )
        )
        return response

    def sample_result(self, who: Who, item_id: uuid.UUID, body: Any) -> httpx.Response:
        return self.send(
            who, "POST", f"/accreditation/sample-items/{item_id}/result", json_body=body
        )

    def record_commitments(
        self, who: Who, package_id: uuid.UUID, entries: Sequence[Mapping[str, Any]]
    ) -> httpx.Response:
        return self.send(
            who,
            "POST",
            f"/accreditation/monthly-packages/{package_id}/commitments",
            json_body={"commitments": [dict(entry) for entry in entries]},
        )

    def update_commitment(self, who: Who, commitment_id: uuid.UUID, body: Any) -> httpx.Response:
        return self.send(
            who, "PATCH", f"/accreditation/commitments/{commitment_id}", json_body=body
        )

    def commitment(self, responsible: uuid.UUID, **changes: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "text": COMMITMENT_TEXT,
            "responsible_user_id": str(responsible),
            "due_date": "2026-12-15",
        }
        entry.update(changes)
        return entry

    # --- lo que no puede cambiar y lo que sí -------------------------------------------------

    def classification_records(self, organization_id: uuid.UUID) -> list[tuple[Any, ...]]:
        """Los registros ``classification`` de la organización, con sus hashes."""
        return [
            tuple(row)
            for row in self.fetch(
                "SELECT record_id, chain_sequence, content_hash, record_hash,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content"
                " FROM ledger.ledger_record WHERE organization_id = $1"
                " AND record_type = 'classification' ORDER BY chain_sequence",
                organization_id,
            )
        ]

    def loop_rows(self, organization_id: uuid.UUID) -> tuple[Any, ...]:
        """Las filas de la proyección de los hallazgos, del historial de clasificaciones y de las
        etiquetas: estado, clasificación y visibilidad."""
        return tuple(
            [tuple(row) for row in self.fetch(sql, organization_id)]
            for sql in (
                "SELECT * FROM loop.finding_state WHERE organization_id = $1"
                " ORDER BY finding_state_id",
                "SELECT * FROM loop.classification WHERE organization_id = $1"
                " ORDER BY classification_id",
                "SELECT * FROM ledger.label WHERE organization_id = $1 ORDER BY label_id",
                "SELECT * FROM loop.corrective_action WHERE organization_id = $1"
                " ORDER BY action_id",
            )
        )

    def record_type_counts(self, organization_id: uuid.UUID) -> dict[str, int]:
        """Cuántos registros de cada tipo tiene la organización (todas sus cadenas)."""
        return {
            str(row["record_type"]): int(row["n"])
            for row in self.fetch(
                "SELECT record_type, count(*) AS n FROM ledger.ledger_record"
                " WHERE organization_id = $1 GROUP BY record_type",
                organization_id,
            )
        }

    def records_of(self, organization_id: uuid.UUID, record_type: str) -> list[dict[str, Any]]:
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, chain_sequence, schema_version,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content"
                " FROM ledger.ledger_record WHERE organization_id = $1 AND record_type = $2"
                " ORDER BY chain_sequence",
                organization_id,
                record_type,
            )
        ]

    def results_of(self, seed: SampleSeed) -> list[dict[str, Any]]:
        """Los ``sample_audit_result`` de la muestra de ``seed`` (la base es de todo el módulo)."""
        return [
            record
            for record in self.records_of(seed.organization.organization_id, "sample_audit_result")
            if record["content"]["sample_id"] == str(seed.sample_id)
        ]

    def batches_of(self, organization_id: uuid.UUID, package_id: uuid.UUID) -> list[dict[str, Any]]:
        """Los ``commitments_recorded`` del paquete ``package_id``, en el orden de la cadena."""
        return [
            record
            for record in self.records_of(organization_id, "commitments_recorded")
            if record["content"]["package_id"] == str(package_id)
        ]

    def commitment_rows(self, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM accreditation.commitment WHERE organization_id = $1"
                " ORDER BY commitment_id",
                organization_id,
            )
        ]

    def item_rows(self, sample_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT * FROM accreditation.audit_sample_item WHERE sample_id = $1"
                " ORDER BY sample_item_id",
                sample_id,
            )
        ]

    def sample_row(self, sample_id: uuid.UUID) -> dict[str, Any]:
        (row,) = self.fetch(
            "SELECT * FROM accreditation.audit_sample WHERE sample_id = $1", sample_id
        )
        return dict(row)

    def events(self, organization_id: uuid.UUID, record_id: uuid.UUID | str) -> list[str]:
        return [
            str(row["event_name"])
            for row in self.fetch(
                "SELECT event_name FROM shared.outbox_event WHERE organization_id = $1"
                " AND payload ->> 'record_id' = $2 ORDER BY event_name",
                organization_id,
                str(record_id),
            )
        ]


class _NoSigner:
    """Estas pruebas no descargan paquetes: ninguna ruta llega a firmar una URL."""

    async def presign_attachment_get(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("las pruebas de la revisión mensual no firman descargas")


def _periods() -> Iterator[str]:
    """``2000-01``, ``2000-02``…: meses que ninguna composición real usaría."""
    index = 2000 * 12
    while True:
        yield f"{index // 12:04d}-{index % 12 + 1:02d}"
        index += 1


@contextlib.contextmanager
def monthly_review_stack(
    endpoint: PostgresEndpoint, prefix: str, *, pool: int = 8
) -> Iterator[MonthlyReviewStack]:
    with actions_stack(endpoint, prefix, pool=pool) as actions:
        authz = actions.world.stack.authz
        clock = authz.sessions.clock
        common: dict[str, Any] = {
            "database": actions.database,
            "writer": actions.writer,
            "authorizer": authz.authorizer,
            "clock": clock,
        }
        audit = SampleAuditService(**common)
        commitments = CommitmentService(**common)
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(actions.writer),
                    clock=clock,
                ),
                "rate_limiter": UnlimitedRateLimiter(),
                "state": {
                    LOOP_STATE_KEY: LoopHttp(
                        configuration=actions.configuration,
                        classification=actions.classification,
                        actions=actions.actions,
                        evidence=actions.evidence,
                        closure=actions.closure,
                        reopening=actions.reopening,
                    ),
                    ACCREDITATION_STATE_KEY: AccreditationHttp(
                        # Las lecturas del paquete (TASK-344), para ver la muestra revisada.
                        monthly_packages=MonthlyPackageReads(
                            database=actions.database,
                            repository=MonthlyPackageRepository(database=actions.database),
                            authorizer=authz.authorizer,
                            audit=AuditWriter(
                                database=actions.database,
                                clock=clock,
                                provider_organization_id=authz.provider_organization_id,
                            ),
                            storage=_NoSigner(),
                        ),
                        sample_audit=audit,
                        commitments=commitments,
                    ),
                },
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = MonthlyReviewStack(actions, audit, commitments, client, _periods())
        try:
            yield built
        finally:
            actions.run(client.aclose())
