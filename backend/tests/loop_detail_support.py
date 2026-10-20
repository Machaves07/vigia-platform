"""El detalle y el historial de TASK-342 sobre PostgreSQL 16 real como ``vigia_app`` y por HTTP.

``detail_stack`` construye, sobre la pila de la clasificación
(``tests/loop_classification_support``: mundo de U-04, escritor con la política de texto libre
de U-04 y ``ClassificationService`` real), el ``FindingDetailService`` real con
``LectorExpediente`` y ``EvidencePort`` reales de U-02 y los puertos reales de U-03 y de cobertura
**envueltos** en ``DetailPorts``: el doble instrumentado de ``tests/laz_support.py``
(``InstrumentedPorts``) que cuenta cada llamada por operación y que, con ``down``, simula que una
operación no responde (``PortUnavailable``). La aplicación real lleva ``LoopHttp`` con la
clasificación y el detalle.

``DetailStack`` amplía ``ClassificationStack`` con:

- ``declare_standard``: la fila de ``catalog.declared_standard_version`` del estándar del sitio
  (el mundo de la ingesta publica el catálogo sin su tabla de estándares);
- ``later_catalog``: una versión posterior del catálogo que sustituye la versión del estándar;
- ``occupancy``: la marca unipersonal de la fila del hallazgo en la proyección;
- ``detail`` y ``history``: las dos peticiones; ``audit_reads``: las entradas de auditoría de
  lectura del expediente; ``record``: un registro como superusuario.

``DetailEvidenceStore`` es el almacén de evidencias de la ingesta con ``presign_get``, ``down``
(el almacén caído: ``StorageUnavailable``) y ``unreadable`` (claves cuyo objeto no está: ese clip
es ilegible, ``EvidenceUnreadable``). ``received_with_two_clips`` ingresa un hallazgo con dos.

Solo datos generados. Las marcas salen del reloj simulado, que arranca en la hora de la base
(retro 14). Topes de las esperas de la base: 60 s (retro 15).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
from vigia_contracts.models.enumerations import GateStatus

from tests.api_support import World
from tests.fleet_ingest_support import EvidenceStore
from tests.integration.conftest import PostgresEndpoint
from tests.laz_support import (
    IngestedSubject,
    InstrumentedPorts,
    LoopOrganization,
    PortUnavailable,
    RealPorts,
    loop_world,
)
from tests.loop_classification_support import (
    SAME_ORIGIN,
    ClassificationStack,
    Who,
    classification_free_text,
)
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.fleet.application.inventory_read import FleetInventory
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.domain.coverage import CoverageStatus
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.loop.adapters.http.services import LOOP_STATE_KEY, LoopHttp
from vigia_platform.loop.application.classification import ClassificationService
from vigia_platform.loop.application.detail import FindingDetailService
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.signing.keys import format_timestamp
from vigia_platform.shared.storage import (
    PRESIGN_GET_MAX_TTL,
    ObjectHead,
    PresignedRequest,
    StorageUnavailable,
)

__all__ = [
    "DETAIL",
    "HISTORY",
    "STANDARD_TEXT",
    "U03_OPERATIONS",
    "DetailEvidenceStore",
    "DetailPorts",
    "DetailStack",
    "detail_stack",
]

DETAIL: Final = "/loop/findings/{finding_state_id}"
HISTORY: Final = "/loop/findings/{finding_state_id}/classifications"
STANDARD_TITLE: Final = "Zona de prensa"
STANDARD_TEXT: Final = "Ninguna persona dentro del perímetro con la máquina energizada."
LATER_TEXT: Final = "Ninguna persona dentro del perímetro ampliado con la prensa energizada."
U03_OPERATIONS: Final = frozenset({"state_at", "standard_version", "nodes_by_zone"})
"""Las operaciones de U-03 que el detalle invoca; ``estado_en`` es de U-02."""


@dataclass
class DetailPorts(InstrumentedPorts):
    """``InstrumentedPorts`` sobre los puertos reales con las operaciones de ``down`` caídas.

    Cuenta también la llamada que falla: el presupuesto de NFR-LAZ-03 es de llamadas, no de
    respuestas."""

    down: set[str] = field(default_factory=set)
    gate_status: GateStatus | str | None = "real"
    """``"real"``: la respuesta del puerto; ``None``: sin intervalo en el instante; un estado: el
    intervalo real con ese estado (la compuerta de uso no aprobada en el instante del hecho)."""

    def reset(self) -> None:
        self.down.clear()
        self.calls.clear()
        self.gate_status = "real"

    def _failed(self, operation: str) -> bool:
        if operation in self.down:
            self._count(operation)
            return True
        return False

    async def state_at(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, at: datetime
    ) -> GateInterval | None:
        if self._failed("state_at"):
            raise PortUnavailable("state_at")
        found = await super().state_at(context, zone_id, gate, at)
        if self.gate_status == "real":
            return found
        if self.gate_status is None or found is None:
            return None
        return dataclasses.replace(found, status=GateStatus(self.gate_status))

    async def estado_en(
        self, context: ScopeContext, zone_id: uuid.UUID, instant: datetime
    ) -> CoverageStatus:
        if self._failed("estado_en"):
            raise PortUnavailable("estado_en")
        return await super().estado_en(context, zone_id, instant)

    async def standard_version(self, *args: Any) -> Any:
        if self._failed("standard_version"):
            raise PortUnavailable("standard_version")
        self._count("standard_version")
        return await self.wrapped.catalog.standard_version(*args)

    async def nodes_by_zone(self, *args: Any) -> Any:
        if self._failed("nodes_by_zone"):
            raise PortUnavailable("nodes_by_zone")
        self._count("nodes_by_zone")
        return await self.wrapped.fleet.nodes_by_zone(*args)

    @property
    def u03_calls(self) -> int:
        return sum(count for name, count in self.calls.items() if name in U03_OPERATIONS)


@dataclass
class DetailEvidenceStore:
    """El almacén de la ingesta con ``presign_get`` y caída a voluntad (``down``)."""

    store: EvidenceStore
    clock: Clock
    down: bool = False
    unreadable: set[str] = field(default_factory=set)
    """Claves cuyo objeto no está (``head_object`` → ``None``): ese clip queda ilegible."""
    presigned: list[str] = field(default_factory=list)

    async def head_object(self, key: str) -> ObjectHead | None:
        if self.down:
            raise StorageUnavailable("head_object")
        if key in self.unreadable:
            return None
        return await self.store.head_object(key)

    async def presign_get(
        self, key: str, ttl: timedelta = PRESIGN_GET_MAX_TTL, *, version_id: str | None = None
    ) -> PresignedRequest:
        if self.down:
            raise StorageUnavailable("presign_get")
        self.presigned.append(key)
        return PresignedRequest(
            method="GET",
            url=f"https://evidencias.example/{uuid.uuid4()}?version={version_id}",
            headers={},
            expires_at=self.clock.now() + ttl,
        )


@dataclass
class DetailStack(ClassificationStack):
    detail_service: FindingDetailService = field(kw_only=True)
    ports: DetailPorts = field(kw_only=True)
    evidence_store: DetailEvidenceStore = field(kw_only=True)

    # --- siembra ------------------------------------------------------------------------------

    def declare_standard(
        self,
        organization: LoopOrganization,
        *,
        version: int = 1,
        catalog_version: int = 1,
        text: str = STANDARD_TEXT,
        retired_in: int | None = None,
    ) -> None:
        """La versión ``version`` del estándar del sitio, nacida en ``catalog_version``."""
        site = organization.site
        self.world.stack.execute(
            "INSERT INTO catalog.declared_standard_version (organization_id, plant_id, zone_id,"
            " standard_id, version, family, title_es, declared_text, declared_by, effective_from,"
            " predicate, catalog_version, retired_in_catalog_version, reason_es)"
            " SELECT organization_id, plant_id, zone_id, $4, $5, 'coexistence', $6, $7, $8::jsonb,"
            " issued_at, '{}', catalog_version, $9, 'Catálogo sintético de la ingesta'"
            " FROM catalog.zone_catalog_version"
            " WHERE organization_id = $1 AND plant_id = $2 AND zone_id = $3"
            " AND catalog_version = $10",
            site.organization_id,
            site.plant_id,
            site.zone_id,
            site.standard_id,
            version,
            STANDARD_TITLE,
            text,
            json.dumps(
                {
                    "user_id": str(self.world.stack.authz.operator_id),
                    "display_name": "Coordinación SST sintética",
                    "role": "coordinator_sst",
                }
            ),
            retired_in,
            catalog_version,
        )

    def later_catalog(self, organization: LoopOrganization) -> datetime:
        """La versión 2 del catálogo con la versión 2 del estándar, que sustituye a la 1; la
        versión 1 queda retirada en el catálogo 2. Devuelve el ``issued_at`` del catálogo 2."""
        stack = self.world.stack
        stack.tick()
        issued_at = stack.now()
        stack.publish_catalog(organization.site, issued_at, version=2)
        self.declare_standard(organization, version=1, catalog_version=1, retired_in=2)
        self.declare_standard(organization, version=2, catalog_version=2, text=LATER_TEXT)
        return issued_at

    def received_with_two_clips(self) -> IngestedSubject:
        """Un hallazgo de la organización A con dos clips de la cámara de origen (``start`` y
        ``end``, la forma de dos clips de BR-CTR-39 partiendo el clip ``full`` por la mitad), los
        dos en el almacén y con su concesión, ingresado y proyectado (``received``)."""
        organization = self.world.a
        site = organization.site
        ingest = self.world.stack
        document = ingest.finding(site)
        first = document["cameras"][0]["clips"][0]
        starts_at = datetime.fromisoformat(first["starts_at"])
        ends_at = datetime.fromisoformat(first["ends_at"])
        middle = starts_at + (ends_at - starts_at) / 2
        second = ingest._clip(site, site.zone_id, starts_at, ends_at)
        for clip, segment, start, end in (
            (first, "start", starts_at, middle),
            (second, "end", middle, ends_at),
        ):
            clip["segment"] = segment
            clip["starts_at"] = format_timestamp(start)
            clip["ends_at"] = format_timestamp(end)
            clip["duration_ms"] = int((end - start).total_seconds() * 1000)
        ingest._prepare_clip(site, second, store=True, grant=True)
        document["cameras"][0]["clips"].append(second)
        finding = self.world._ingest(organization, IngestKind.FINDING, document)
        assert self.world.project(finding).transition_id == "T01"
        return finding

    def occupancy(self, finding_state_id: uuid.UUID, single: bool, window: int = 60) -> None:
        """La marca unipersonal de la fila del hallazgo (como superusuario). Sin respuesta de
        ``single_occupancy`` al proyectar, la fila nace unipersonal (fallo hacia la privacidad):
        las pruebas que comparan marcas exactas la desmarcan."""
        self.world.stack.execute(
            "UPDATE loop.finding_state SET single_occupancy_zone = $2,"
            " aggregation_window_minutes = $3 WHERE finding_state_id = $1",
            finding_state_id,
            single,
            window,
        )

    # --- peticiones ---------------------------------------------------------------------------

    def get(self, who: Who, path: str) -> httpx.Response:
        cookie, concession = who
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers["X-Vigia-Concession"] = str(concession)
        response: httpx.Response = self.run(self.client.get(path, headers=headers))
        return response

    def detail(self, who: Who, finding_state_id: uuid.UUID | str) -> httpx.Response:
        return self.get(who, DETAIL.format(finding_state_id=finding_state_id))

    def history(self, who: Who, finding_state_id: uuid.UUID | str) -> httpx.Response:
        return self.get(who, HISTORY.format(finding_state_id=finding_state_id))

    # --- lecturas -----------------------------------------------------------------------------

    def audit_reads(self, organization: LoopOrganization) -> list[dict[str, Any]]:
        """Las entradas ``ledger_detail_read`` de la organización, en orden de la cadena."""
        return [
            dict(row)
            for row in self.fetch(
                "SELECT operation, outcome, actor_id, scope_zone_id, resource_kind, resource_id,"
                " result_count FROM shared.audit_entry"
                " WHERE organization_id = $1 AND operation = 'ledger_detail_read'"
                " ORDER BY chain_sequence",
                organization.organization_id,
            )
        ]

    def record(self, record_id: uuid.UUID) -> dict[str, Any]:
        """El registro del expediente con su contenido y sus hashes (como superusuario)."""
        (row,) = self.fetch(
            "SELECT record_id, record_type, chain_sequence, content_hash, record_hash,"
            " ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
            " WHERE record_id = $1",
            record_id,
        )
        return {**dict(row), "content": json.loads(row["content"])}

    def record_count(self, organization: LoopOrganization) -> int:
        (row,) = self.fetch(
            "SELECT count(*) AS n FROM ledger.ledger_record WHERE organization_id = $1",
            organization.organization_id,
        )
        return int(row["n"])


@contextlib.contextmanager
def detail_stack(endpoint: PostgresEndpoint, prefix: str) -> Iterator[DetailStack]:
    with loop_world(endpoint, prefix) as world:
        stack = world.stack
        authz = stack.authz
        clock = authz.sessions.clock
        audit = authz.sessions.audit
        database = stack.database()
        writer = EscritorExpediente(
            database=database,
            registry=stack.registry,
            free_text=classification_free_text(),
            evidence=EvidenceVerifier(stack.storage, clock),
            outbox=stack.outbox,
            clock=clock,
        )
        classification = ClassificationService(
            database=database, writer=writer, authorizer=authz.authorizer, clock=clock
        )
        queries = catalog_query_ports(database)
        ports = DetailPorts(
            wrapped=RealPorts(
                catalog=queries.catalog,
                gates=queries.gates,
                coverage=CoverageService(database=database, audit=audit, clock=clock),
                fleet=FleetInventory(
                    database=database,
                    authorizer=authz.authorizer,
                    audit=audit,
                    clock=clock,
                    provider_organization_id=authz.provider_organization_id,
                ),
            )
        )
        evidence_store = DetailEvidenceStore(stack.storage, clock)
        detail = FindingDetailService(
            database=database,
            authorizer=authz.authorizer,
            reader=LectorExpediente(database=database, audit=audit, clock=clock),
            evidence=EvidenceService(database=database, audit=audit, storage=evidence_store),
            catalog=ports,
            gates=ports,
            fleet=ports,
            coverage=ports,
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
                "state": {LOOP_STATE_KEY: LoopHttp(classification=classification, detail=detail)},
            },
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=60.0
        )
        built = DetailStack(
            world,
            database,
            writer,
            classification,
            client,
            detail_service=detail,
            ports=ports,
            evidence_store=evidence_store,
        )
        try:
            yield built
        finally:
            stack.run(client.aclose())
            stack.run(database.dispose())
