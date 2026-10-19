"""El análisis de causa raíz de TASK-315 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``root_cause_stack`` construye, sobre el mundo de U-04 (``tests/laz_support.py``) y con la misma
política de texto libre que la clasificación (``classification_free_text``: el validador de U-03,
``blocked_vocabulary`` real y el **doble** de ``person_attribution``, que llega con TASK-340), el
``RootCauseService`` real con sus puertos reales (``CatalogQueryPort`` y ``FleetQueryPort`` de
U-03 y ``CoveragePort`` de U-02 sobre la misma base) y la aplicación real con ``LoopHttp``
(clasificación y causa raíz) en ``app.state``.

``RootCauseStack`` amplía ``ClassificationStack`` (personas, contextos, sujetos, registros) con
las peticiones de las tres rutas, los cuerpos válidos y las lecturas de lo que quedó escrito
(``loop.root_cause_analysis`` y los registros ``root_cause_analysis``).

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14). Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx

from tests.api_support import World
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import IngestedSubject, LoopOrganization, loop_world, register_u04
from tests.loop_classification_support import (
    CONCESSION_HEADER,
    SAME_ORIGIN,
    ClassificationStack,
    Who,
    classification_free_text,
)
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.root_cause import RootCauseService
from vigia_platform.loop.assistant.service import AssistantService
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.outbox.registries import OutboxCatalog

__all__ = [
    "CONCLUSION",
    "FACTOR",
    "LICENSE",
    "ROOT_STATEMENT",
    "SIGNATURE",
    "START",
    "STATEMENTS",
    "STEP",
    "RootCauseStack",
    "complete_signature",
    "root_cause_stack",
]

START: Final = "/loop/findings/{finding_state_id}/root-cause-analyses"
STEP: Final = "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}"
SIGNATURE: Final = "/loop/root-cause-analyses/{analysis_id}/signature"

STATEMENTS: Final = (
    "La carretilla cruzó el pasillo peatonal con la estación de carga energizada",
    "El pasillo de carga comparte el trazado con el acceso peatonal de la zona",
    "El plano de la zona no separa los flujos de carga y de paso peatonal",
    "La revisión del trazado no incluyó la ampliación de la estación de carga",
    "El procedimiento de cambios de trazado no exige revisar los flujos",
)
"""Cinco porqués neutros (20 a 1000 caracteres) que pasan la política y los validadores."""
FACTOR: Final = "Trazado del pasillo sin separación física"
ROOT_STATEMENT: Final = "El trazado de la zona mezcla los flujos de carga y de paso peatonal"
CONCLUSION: Final = "Separar físicamente los flujos con un resguardo fijo en el acceso de carga"
LICENSE: Final = "LIC-SST-0001"


def complete_signature(**changes: Any) -> dict[str, Any]:
    """Un cuerpo de firma completo y válido, con ``changes`` encima."""
    body: dict[str, Any] = {
        "professional_license": LICENSE,
        "root_cause_category": "engineering",
        "root_cause_statement": ROOT_STATEMENT,
        "conclusion": CONCLUSION,
        "cause_effect_branches": [
            {
                "branch_category": "engineering",
                "factors": [{"text": FACTOR, "evidence_refs": []}],
            }
        ],
    }
    body.update(changes)
    return body


@dataclass
class RootCauseStack(ClassificationStack):
    root_cause: RootCauseService
    free_text: FreeTextPolicyRegistry
    ports: dict[str, Any]
    """``catalog``, ``fleet`` y ``coverage``: los puertos reales del servicio."""
    assistant: AssistantService | None = None
    """``AssistantService`` (TASK-316) si la pila se pidió con ``assistant=True``."""

    def root_cause_like(self, **changes: Any) -> RootCauseService:
        """Otro servicio de causa raíz sobre la misma base (p. ej. una subclase de prueba)."""
        arguments: dict[str, Any] = {
            "database": self.database,
            "writer": self.writer,
            "authorizer": self.world.stack.authz.authorizer,
            "free_text": self.free_text,
            **self.ports,
            "clock": self.world.stack.authz.sessions.clock,
        }
        cls = changes.pop("cls", RootCauseService)
        arguments.update(changes)
        built: RootCauseService = cls(**arguments)
        return built

    # --- peticiones ---------------------------------------------------------------------------

    def request(self, who: Who, method: str, path: str, body: Any | None = None) -> httpx.Response:
        self.tick()
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.request(method, path, json=body, headers=headers)
        )
        return response

    def start(self, who: Who, finding_state_id: uuid.UUID | str) -> httpx.Response:
        return self.request(who, "POST", START.format(finding_state_id=finding_state_id))

    def step(
        self,
        who: Who,
        analysis_id: uuid.UUID | str,
        step_index: int | str,
        body: Any,
    ) -> httpx.Response:
        path = STEP.format(analysis_id=analysis_id, step_index=step_index)
        return self.request(who, "PUT", path, body)

    def signature(self, who: Who, analysis_id: uuid.UUID | str, body: Any) -> httpx.Response:
        return self.request(who, "POST", SIGNATURE.format(analysis_id=analysis_id), body)

    def started(self, who: Who, subject: IngestedSubject | None = None) -> dict[str, Any]:
        """Un análisis nuevo sobre ``subject`` (o un hallazgo recién ingresado): su cuerpo."""
        subject = subject or self.received()
        response = self.start(who, self.finding_state_id(subject))
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    def with_steps(self, who: Who, count: int, subject: IngestedSubject | None = None) -> str:
        """Un análisis con ``count`` porqués completos; devuelve ``analysis_id``."""
        analysis_id = str(self.started(who, subject)["analysis_id"])
        for index in range(1, count + 1):
            response = self.step(who, analysis_id, index, {"statement": STATEMENTS[index - 1]})
            assert response.status_code == 200, response.text
        return analysis_id

    # --- lo que quedó escrito ------------------------------------------------------------------

    def analysis_row(self, analysis_id: uuid.UUID | str) -> dict[str, Any]:
        (row,) = self.fetch(
            "SELECT * FROM loop.root_cause_analysis WHERE analysis_id = $1",
            uuid.UUID(str(analysis_id)),
        )
        found = dict(row)
        for name in ("zone_context", "why_steps", "cause_effect_branches", "signer"):
            if isinstance(found[name], str):
                found[name] = json.loads(found[name])
        return found

    def analysis_records(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        """Los registros ``root_cause_analysis`` de la organización, en orden de cadena."""
        return [
            {**dict(row), "content": json.loads(row["content"])}
            for row in self.fetch(
                "SELECT record_id, chain_sequence, content_hash,"
                " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
                " WHERE organization_id = $1 AND record_type = 'root_cause_analysis'"
                " ORDER BY chain_sequence",
                organization.organization_id,
            )
        ]

    def analysis_counts(self, organization: LoopOrganization) -> tuple[int, int, int]:
        """Análisis, registros ``root_cause_analysis`` y eventos de firma de la organización."""
        (row,) = self.fetch(
            "SELECT (SELECT count(*) FROM loop.root_cause_analysis WHERE organization_id = $1)"
            " AS a, (SELECT count(*) FROM ledger.ledger_record WHERE organization_id = $1"
            " AND record_type = 'root_cause_analysis') AS r, (SELECT count(*) FROM"
            " shared.outbox_event WHERE organization_id = $1"
            " AND event_name = 'root_cause_analysis_signed') AS e",
            organization.organization_id,
        )
        return int(row["a"]), int(row["r"]), int(row["e"])


STANDARD_TITLE: Final = "Zona de prensa"
STANDARD_TEXT: Final = "Ninguna persona dentro del perímetro con la máquina energizada."
"""El estándar del catálogo sintético de la ingesta (``IngestStack.catalog``)."""


def _declare_standard(stack: RootCauseStack, organization: LoopOrganization) -> None:
    """La fila de ``catalog.declared_standard_version`` del estándar del catálogo sintético.

    El mundo de la ingesta publica la versión del catálogo sin su tabla de estándares (la ingesta
    no la lee); ``CatalogQueryPort.standard_version`` sí la lee."""
    site = organization.site
    stack.world.stack.execute(
        "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
        " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
        " predicate, catalog_version, reason_es)"
        " SELECT organization_id, plant_id, zone_id, $4, 1, 'coexistence', $5, $6, $7::jsonb,"
        " issued_at, '{}', catalog_version, 'Catálogo sintético de la ingesta'"
        " FROM catalog.zone_catalog_version"
        " WHERE organization_id = $1 AND plant_id = $2 AND zone_id = $3 AND catalog_version = 1",
        site.organization_id,
        site.plant_id,
        site.zone_id,
        site.standard_id,
        STANDARD_TITLE,
        STANDARD_TEXT,
        json.dumps(
            {
                "user_id": str(stack.world.stack.authz.operator_id),
                "display_name": "Coordinación SST sintética",
                "role": "coordinator_sst",
            }
        ),
    )


@contextlib.contextmanager
def root_cause_stack(
    endpoint: PostgresEndpoint,
    prefix: str,
    *,
    pool: int = 8,
    register: Callable[[RecordTypeRegistry, OutboxCatalog], None] = register_u04,
    assistant: bool = False,
) -> Iterator[RootCauseStack]:
    """``register`` añade al catálogo lo que la prueba necesite antes de sellar (p. ej. el
    consumidor ``assistant_worker``); ``assistant`` instala ``AssistantService`` (TASK-316) en
    ``LoopHttp`` con la bandeja del mundo."""
    with loop_world(endpoint, prefix, register=register) as world:
        stack = world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        database = stack.database(worker_pool_size=pool)
        free_text = classification_free_text()
        writer = EscritorExpediente(
            database=database,
            registry=stack.registry,
            free_text=free_text,
            evidence=EvidenceVerifier(stack.storage, clock),
            outbox=stack.outbox,
            clock=clock,
        )
        classification = ClassificationService(
            database=database, writer=writer, authorizer=authz.authorizer, clock=clock
        )
        audit = AuditWriter(
            database=database, clock=clock, provider_organization_id=authz.provider_organization_id
        )
        ports: dict[str, Any] = {
            "catalog": catalog_query_ports(database).catalog,
            "fleet": FleetInventory(
                database=database,
                authorizer=authz.authorizer,
                audit=audit,
                clock=clock,
                provider_organization_id=authz.provider_organization_id,
            ),
            "coverage": CoverageService(database=database, audit=audit, clock=clock),
        }
        root_cause = RootCauseService(
            database=database,
            writer=writer,
            authorizer=authz.authorizer,
            free_text=free_text,
            clock=clock,
            **ports,
        )
        assistant_service = (
            AssistantService(
                database=database, authorizer=authz.authorizer, outbox=stack.outbox, clock=clock
            )
            if assistant
            else None
        )
        app = World(clock=clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=clock,
                ),
                "state": {
                    LOOP_STATE_KEY: LoopHttp(
                        classification=classification,
                        root_cause=root_cause,
                        assistant=assistant_service,
                    )
                },
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = RootCauseStack(
            world,
            database,
            writer,
            classification,
            client,
            root_cause,
            free_text,
            ports,
            assistant_service,
        )
        for organization in (world.a, world.b):
            _declare_standard(built, organization)
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())
