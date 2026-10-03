"""Rutas del expediente, evidencias, etiquetas, cobertura, integridad, vista en vivo, auditoría,
claves públicas y operación, de extremo a extremo (TASK-137; ``business-logic-model.md`` §10.2).

La aplicación real (``create_app`` con la cadena fija de middleware y las rutas de
``platform_units()``) contra PostgreSQL 16 real como ``vigia_app``: sesiones y contextos reales,
``ContextAuthorizer`` y ``Authorizer`` con su auditoría, ``EscritorExpediente`` con los tipos de
U-02 y los de U-03 que lee la cobertura, servicio de firma real con las cinco claves, bandeja real.
Doble solo para el almacén de evidencias (el real con LocalStack lo prueba
``tests/integration/test_evidence_sample.py``).

- **Criterio 1** (PR-NUC-37): cada ruta nueva declara su clave de la matriz o está en la lista
  pública, y el catálogo real arranca; el permiso según el tipo consultado deja al administrador
  solo la vista de integridad (RF-PLA-11).
- **Criterio 2**: ``GET /.well-known/vigia-verifier`` devuelve el SHA-256 del artefacto
  ``tools/vigia_verify.py`` que genera ``tools/build_verifier.py`` (VIG-54).
- **Criterio 3**: 32 días de cobertura responden ``period_too_long`` con su mensaje en español;
  31 días exactos responden.
- **H-38**: un periodo con hueco de comunicación aparece como su propio tramo ``no_communication``,
  nunca omitido; el resumen suma el periodo exacto.
- **H-36**: el último punto de control de ``GET /integrity/checkpoints``, leído como registro con
  ``GET /ledger/records/{id}``, sirve de «paquete anterior» al verificador sin red con las claves de
  ``/.well-known/vigia-checkpoint-keys``; un prefijo alterado o un ancla falsificada no pasan.
- **Claves ``checkpoint`` retiradas** siguen publicadas tras rotar (por la ruta de operación) y
  vencer.
- Guardas de alcance: zona, planta, organización y concesión, cada una con su prueba que falla si
  se quita el filtro; ``provider_query`` bajo concesión (BR-NUC-38); ``authorization_denied``
  repetido (NFR-NUC-28); ráfaga de vista en vivo (VIG-80); validación estricta de parámetros.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.api_support import World
from tests.authz_support import Site
from tests.factories import uuid7
from tests.integration.conftest import PostgresEndpoint
from tests.integration.test_coverage_port import COVERAGE_TYPES
from tests.ledger_database import evidence_values, insert_evidence, set_organization
from tests.live_view_support import (
    LIVE_VIEW_URL,
    LiveViewEnvironment,
    live_view_environment,
    node_verifies,
)
from tests.signing_support import ENVIRONMENT
from tests.verifier_packages import PackageChain, row_to_entry, write_package
from tests.writer_support import save_record_types, unit_context
from tools.build_verifier import build, sha256_text
from vigia_platform.identity.adapters.authz_store import (
    DENIED_REPEATED_THRESHOLD,
    LedgerProviderQueryLedger,
)
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.identity.authz.authorize import (
    Authorizer,
    Resource,
    ResourceNotFound,
    narrowed,
)
from vigia_platform.identity.authz.matrix import MATRIX, PermissionKey
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.adapters.http import LedgerHttp
from vigia_platform.ledger.adapters.http.records import INTEGRITY_RECORD_TYPES
from vigia_platform.ledger.adapters.integrity_store import SqlIntegrityResults, SqlIntegrityStore
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import EvidenceService
from vigia_platform.ledger.application.integrity_requests import (
    INTEGRITY_VERIFICATION_REQUESTED,
    IntegrityOnDemandConsumer,
    IntegrityRequests,
)
from vigia_platform.ledger.application.labels import LabelService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import EscritorExpediente, Receipt
from vigia_platform.ledger.chain.checkpoints import (
    CheckpointChain,
    CheckpointContent,
    CheckpointPublicKey,
    CheckpointService,
    verify_checkpoint,
)
from vigia_platform.ledger.chain.package_verifier import verify_package
from vigia_platform.ledger.chain.verify import IntegrityService
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.adapters.http import PlatformHttp
from vigia_platform.shared.api.app import platform_permissions
from vigia_platform.shared.api.declarations import UnauthenticatedRoute, iter_declared_routes
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, AllowedScope, Role, ScopeLevel
from vigia_platform.shared.outbox.publish import OutboxEvent
from vigia_platform.shared.outbox.replay import DeadLetterReplay
from vigia_platform.shared.signing.keys import SigningPurpose
from vigia_platform.shared.signing.service import SigningService
from vigia_platform.shared.storage import ChecksumType, ObjectHead, PresignedRequest

pytestmark = pytest.mark.integration

BACKEND: Final = Path(__file__).resolve().parents[2]
VERIFIER: Final = BACKEND / "tools" / "vigia_verify.py"
ROUTES_DIRECTORIES: Final = (
    BACKEND / "src" / "vigia_platform" / "ledger" / "adapters" / "http",
    BACKEND / "src" / "vigia_platform" / "shared" / "adapters" / "http",
)
ORIGIN: Final = "https://app.vigia.test"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
LABELS: Final = PlatformLabels.load()
T0: Final = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
"""Inicio del periodo de cobertura (las marcas del contenido no dependen del reloj)."""
HOUR: Final = timedelta(hours=1)
CLIP: Final = b"clip sintetico: bytes que el almacen dice tener"

NEW_ROUTES: Final = {
    ("GET", "/ledger/records"): PermissionKey.INTEGRITY_VERIFY,
    ("GET", "/ledger/records/{record_id}"): PermissionKey.INTEGRITY_VERIFY,
    ("POST", "/evidence/{evidence_id}/read-url"): PermissionKey.EVIDENCE_READ,
    ("GET", "/labels"): PermissionKey.LABELS_READ,
    ("GET", "/zones/{zone_id}/coverage"): PermissionKey.COVERAGE_READ,
    ("GET", "/zones/{zone_id}/coverage/at"): PermissionKey.COVERAGE_READ,
    ("POST", "/integrity/verify"): PermissionKey.INTEGRITY_VERIFY,
    ("GET", "/integrity/results"): PermissionKey.INTEGRITY_VERIFY,
    ("GET", "/integrity/checkpoints"): PermissionKey.INTEGRITY_VERIFY,
    ("POST", "/zones/{zone_id}/live-view-token"): PermissionKey.LIVE_VIEW_OPEN,
    ("GET", "/audit/entries"): PermissionKey.AUDIT_READ,
    (
        "POST",
        "/platform/dead-letter/{event_id}/{consumer}/replay",
    ): PermissionKey.PLATFORM_DEAD_LETTER_REPLAY,
    ("POST", "/platform/keys/{purpose}/rotate"): PermissionKey.PLATFORM_KEYS_ROTATE,
}
"""Las rutas de TASK-137 con la clave que declaran (§10.2)."""
NEW_PUBLIC_ROUTES: Final = {
    ("GET", "/.well-known/vigia-checkpoint-keys"): UnauthenticatedRoute.CHECKPOINT_KEYS,
    ("GET", "/.well-known/vigia-verifier"): UnauthenticatedRoute.VERIFIER_HASH,
}


# --- Dobles y entorno ---


@dataclass
class StubEvidenceStorage:
    """El depósito de evidencias: ``HEAD`` con la suma de ``CLIP`` y URL prefirmada sintética."""

    version_id: str | None = "v-sintetica-1"
    presigned: list[str] = field(default_factory=list)

    async def head_object(self, key: str) -> ObjectHead | None:
        return ObjectHead(
            key=key,
            size_bytes=len(CLIP),
            checksum_sha256=base64.b64encode(hashlib.sha256(CLIP).digest()).decode("ascii"),
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type="video/mp4",
            metadata={"vigia-anonymized": "1"},
            version_id=self.version_id,
        )

    async def presign_get(
        self, key: str, ttl: timedelta = timedelta(minutes=5), *, version_id: str | None = None
    ) -> PresignedRequest:
        self.presigned.append(key)
        return PresignedRequest(
            method="GET",
            url=f"https://almacen.vigia.test/{key}?versionId={version_id}&X-Amz-Expires=300",
            headers={},
            expires_at=datetime(2026, 9, 29, 10, 35, tzinfo=UTC),
        )


@dataclass
class Routes:
    env: LiveViewEnvironment
    client: httpx.AsyncClient
    writer: EscritorExpediente
    checkpoints: CheckpointService
    verifier: IntegrityService
    storage: StubEvidenceStorage
    app: Any
    signing: SigningService

    @property
    def provider(self) -> uuid.UUID:
        return self.env.authz.provider_organization_id

    def run(self, awaitable: Any) -> Any:
        return self.env.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.env.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.env.execute(sql, *args)

    def call(
        self,
        method: str,
        path: str,
        *,
        cookie: Any = None,
        params: Any = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        sent = dict(SAME_ORIGIN)
        if cookie is not None:
            sent["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value}"
        sent.update(headers or {})
        response: httpx.Response = self.run(
            self.client.request(
                method, path, params=params, json=json_body, content=content, headers=sent
            )
        )
        return response

    # --- Personas -----------------------------------------------------------------------------

    def person(
        self,
        organization_id: uuid.UUID,
        role: Role,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> Any:
        """La cookie de una persona nueva con ``role`` sobre el alcance dado."""
        user_id = self.env.user_with_role(organization_id, role, level, scope_id)
        return self.env.authz.open_session(organization_id, user_id)

    def operator(self) -> Any:
        authz = self.env.authz
        return authz.open_session(authz.provider_organization_id, authz.operator_id)

    # --- Expediente ---------------------------------------------------------------------------

    def write(
        self,
        organization_id: uuid.UUID,
        record_type: str,
        document: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> uuid.UUID:
        context = unit_context(organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM)
        receipt = self.run(
            self.writer.write(context, record_type, document, occurred_at=occurred_at)
        )
        assert isinstance(receipt, Receipt), receipt
        return receipt.record_id

    def gate(self, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID, at: datetime) -> uuid.UUID:
        return self.write(
            site.organization_id,
            "gate_state_changed",
            {
                "zone_id": str(zone_id),
                "plant_id": str(plant_id),
                "gate": "use",
                "status": "approved",
                "resulting_mode": "productive",
            },
            occurred_at=at,
        )

    def checkpoint(self, organization_id: uuid.UUID, plant_id: uuid.UUID) -> Any:
        context = unit_context(organization_id, ActorUnit.U02, kind=ActorKind.SYSTEM)
        (result,) = self.run(
            self.checkpoints.write_checkpoints_now(context, [CheckpointChain.plant(plant_id)])
        )
        return result.checkpoint

    def audit_entries(self, organization_id: uuid.UUID, operation: str) -> list[Any]:
        return self.fetch(
            "SELECT * FROM shared.audit_entry WHERE organization_id = $1 AND operation = $2"
            " ORDER BY chain_sequence",
            organization_id,
            operation,
        )

    def events(self, organization_id: uuid.UUID, name: str) -> list[Any]:
        return self.fetch(
            "SELECT * FROM shared.outbox_event WHERE organization_id = $1 AND event_name = $2"
            " ORDER BY publish_seq",
            organization_id,
            name,
        )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


@pytest.fixture(scope="module")
def routes(postgres_endpoint: PostgresEndpoint) -> Iterator[Routes]:
    # El reloj simulado arranca en la hora de la base antes del alta de las claves de firma: la
    # prueba bajo concesión (RLS con now()) firma con claves vigentes sea cual sea la fecha real
    # (VIG-135). Las demás usan marcas fijas (T0) o relativas al reloj.
    with live_view_environment(postgres_endpoint, "ledger_routes", at_database_time=True) as env:
        authz = env.authz
        sessions = authz.sessions
        registry = RecordTypeRegistry()
        for definition in (*U02_RECORD_TYPES, *COVERAGE_TYPES):
            registry.register(definition)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
            registry.seal()

        env.run(synchronize())
        storage = StubEvidenceStorage()
        writer = EscritorExpediente(
            database=sessions.database,
            registry=registry,
            free_text=FreeTextPolicyRegistry(),
            evidence=EvidenceVerifier(storage, env.clock),
            outbox=sessions.outbox,
            clock=env.clock,
        )
        provider = authz.provider_organization_id
        # El servicio de firma de la proveedora sembrada (el del entorno de vista en vivo es de
        # otra organización sintética): mismas claves, mismo gestor, mismo almacén de claves.
        signing = SigningService(
            provider_organization_id=provider,
            store=env.store,
            secrets=env.secrets,
            events=env.events,
            clock=env.clock,
            environment=ENVIRONMENT,
        )
        env.run(signing.start())
        checkpoints = CheckpointService(
            store=SqlCheckpointStore(
                database=sessions.database,
                writer=writer,
                audit=sessions.audit,
                outbox=sessions.outbox,
            ),
            signer=signing,
            clock=env.clock,
        )
        verifier = IntegrityService(
            store=SqlIntegrityStore(
                database=sessions.database, audit=sessions.audit, outbox=sessions.outbox
            ),
            keys=checkpoints,
            clock=env.clock,
        )
        ledger = LedgerHttp(
            reader=LectorExpediente(database=sessions.database, audit=sessions.audit),
            evidence=EvidenceService(
                database=sessions.database, audit=sessions.audit, storage=storage
            ),
            labels=LabelService(database=sessions.database, audit=sessions.audit),
            coverage=CoverageService(database=sessions.database, audit=sessions.audit),
            integrity_results=SqlIntegrityResults(database=sessions.database),
            integrity_requests=IntegrityRequests(
                database=sessions.database, outbox=sessions.outbox, clock=env.clock
            ),
            checkpoints=checkpoints,
            live_view=env.service(),
            authorizer=authz.authorizer,
            provider_organization_id=provider,
        )
        platform = PlatformHttp(
            signing=signing,
            dead_letter=DeadLetterReplay(
                database=sessions.database,
                authorizer=authz.authorizer,
                audit=sessions.audit,
                clock=env.clock,
            ),
            operators=authz.contexts,
            authorizer=authz.authorizer,
            audit=sessions.audit,
            provider_organization_id=provider,
        )
        app = World(clock=env.clock).app(
            units=None,
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=provider,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=env.clock,
                ),
                "ledger": ledger,
                "platform": platform,
            },
            public_origin=ORIGIN,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=30.0
        )
        try:
            yield Routes(env, client, writer, checkpoints, verifier, storage, app, signing)
        finally:
            env.run(client.aclose())


def _error(response: httpx.Response, code: str, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    assert body["message_es"] == LABELS.label("api_error_code", code)
    assert set(body) <= {
        "code",
        "detail_code",
        "message_es",
        "correlation_id",
        "retry_after_seconds",
    }
    return body


# --- Criterio 1: declaraciones (PR-NUC-37) ---


def test_every_new_route_declares_its_matrix_key_or_is_public(routes: Routes) -> None:
    declared: dict[tuple[str, str], Any] = {}
    for route in iter_declared_routes(routes.app.routes):
        for method in route.methods - {"HEAD"}:
            declared[(method, route.path)] = route.declarations
    keys = platform_permissions()
    for (method, path), key in NEW_ROUTES.items():
        (declaration,) = declared[(method, path)]
        assert declaration.permission == key.value and key.value in keys, (method, path)
        assert declaration.unauthenticated is None
    for (method, path), entry in NEW_PUBLIC_ROUTES.items():
        (declaration,) = declared[(method, path)]
        assert declaration.unauthenticated is entry and declaration.permission is None


def test_the_ledger_route_key_is_the_broader_of_the_two(routes: Routes) -> None:
    # La ruta declara integrity.verify: todo rol con findings.read la tiene, así que la vista de
    # hallazgos nunca queda detrás de una denegación por ruta.
    assert all(
        PermissionKey.INTEGRITY_VERIFY in keys
        for keys in MATRIX.values()
        if PermissionKey.FINDINGS_READ in keys
    )
    assert frozenset({"checkpoint", "key_rotated", "key_set_published"}) == INTEGRITY_RECORD_TYPES


def test_no_route_exposes_by_source(routes: Routes) -> None:
    # Seguimiento de VIG-59: by_source (la idempotencia de U-03) no audita ni filtra por alcance;
    # ninguna ruta de este PR lo llama ni lo nombra.
    for directory in ROUTES_DIRECTORIES:
        for path in directory.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {
                node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute | ast.Name)
            }
            assert "by_source" not in names, path


# --- Criterio 2: hash del verificador ---


def test_the_verifier_route_publishes_the_hash_of_the_generated_artifact(routes: Routes) -> None:
    response = routes.call("GET", "/.well-known/vigia-verifier")
    assert response.status_code == 200, response.text
    body = response.json()
    artifact = VERIFIER.read_bytes()
    assert body == {
        "name": "vigia_verify.py",
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "size_bytes": len(artifact),
        "format_version": 1,
    }
    # El artefacto versionado es exactamente el que genera tools/build_verifier.py (VIG-54).
    assert body["sha256"] == sha256_text(build())
    assert response.headers["Cache-Control"] == "public, max-age=300"


# --- Criterio 3 y H-38: cobertura ---


def _observability(
    site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID, node_id: uuid.UUID, started: datetime
) -> dict[str, Any]:
    return {
        "event_id": str(uuid7()),
        "contract_version": "1.0.0",
        "organization_id": str(site.organization_id),
        "plant_id": str(plant_id),
        "zone_id": str(zone_id),
        "node_id": str(node_id),
        "subject": {"kind": "zone"},
        "phase": "opened",
        "state": "observable",
        "causes": [],
        "started_at": _stamp(started),
        "node_time": {
            "started_at": _stamp(started),
            "ended_at": _stamp(started),
            "clock": {"synchronized": True, "offset_ms": 0, "source": "ntp.local"},
        },
        "evidence": [],
        "software_version": "1.0.0",
    }


def _communication(
    routes: Routes,
    site: Site,
    plant_id: uuid.UUID,
    node_id: uuid.UUID,
    state: str,
    since: datetime,
    last_heartbeat: datetime | None = None,
) -> uuid.UUID:
    document: dict[str, Any] = {
        "node_id": str(node_id),
        "plant_id": str(plant_id),
        "state": state,
        "since": _stamp(since),
    }
    if last_heartbeat is not None:
        document["last_heartbeat_at"] = _stamp(last_heartbeat)
    return routes.write(site.organization_id, "node_communication_state_changed", document)


@dataclass(frozen=True)
class CoveredZone:
    site: Site
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    node_id: uuid.UUID


def _zone_with_a_communication_gap(routes: Routes) -> CoveredZone:
    """Zona productiva y observada desde ``T0 - 1 h``; el nodo enmudece entre ``T0 + 1 h`` (su
    último latido) y ``T0 + 3 h``."""
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    ((plant_id, zone_id),) = site.zones()
    node_id = routes.env.add_node(site.organization_id, plant_id, url=LIVE_VIEW_URL)
    routes.env.assign_node(site.organization_id, plant_id, zone_id, node_id, at=T0 - HOUR)
    routes.gate(site, plant_id, zone_id, T0 - HOUR)
    _communication(routes, site, plant_id, node_id, "reachable", T0 - HOUR)
    routes.write(
        site.organization_id,
        "observability_event_received",
        _observability(site, plant_id, zone_id, node_id, T0 - HOUR),
    )
    _communication(routes, site, plant_id, node_id, "mute", T0 + 2 * HOUR, T0 + HOUR)
    last = _communication(routes, site, plant_id, node_id, "reachable", T0 + 3 * HOUR)
    routes.execute(
        "INSERT INTO ledger.communication_state (node_id, organization_id, plant_id, state, since,"
        " last_heartbeat_at, source_record_id) VALUES ($1, $2, $3, 'reachable', $4, NULL, $5)",
        node_id,
        site.organization_id,
        plant_id,
        T0 + 3 * HOUR,
        last,
    )
    return CoveredZone(site, plant_id, zone_id, node_id)


def test_h_38_a_communication_gap_is_shown_as_its_own_interval(routes: Routes) -> None:
    zone = _zone_with_a_communication_gap(routes)
    cookie = routes.person(zone.site.organization_id, Role.COPASST, ScopeLevel.ZONE, zone.zone_id)
    response = routes.call(
        "GET",
        f"/zones/{zone.zone_id}/coverage",
        cookie=cookie,
        params={"from": _stamp(T0), "to": _stamp(T0 + 4 * HOUR)},
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["period"] == {"from": _stamp(T0), "to": _stamp(T0 + 4 * HOUR)}
    intervals = [
        (i["starts_at"], i["ends_at"], i["state"], i["causes"], i["layer"])
        for i in body["intervals"]
    ]
    assert intervals == [
        (_stamp(T0), _stamp(T0 + HOUR), "observable", [], "node_report"),
        (
            _stamp(T0 + HOUR),
            _stamp(T0 + 3 * HOUR),
            "not_observable",
            ["no_communication"],
            "platform_communication",
        ),
        (_stamp(T0 + 3 * HOUR), _stamp(T0 + 4 * HOUR), "observable", [], "node_report"),
    ]
    summary = body["summary"]
    assert summary["observable_ms"] == 2 * 3_600_000
    assert summary["no_communication_ms"] == 2 * 3_600_000
    assert sum(summary.values()) == 4 * 3_600_000
    # Ningún valor dice «despejada» ni «segura» (P2).
    assert "clear" not in response.text and "safe" not in response.text

    at = routes.call(
        "GET",
        f"/zones/{zone.zone_id}/coverage/at",
        cookie=cookie,
        params={"instant": _stamp(T0 + 2 * HOUR)},
    )
    assert at.status_code == 200, at.text
    assert (at.json()["state"], at.json()["causes"]) == ("not_observable", ["no_communication"])
    # Cada consulta válida queda auditada (BR-NUC-59).
    entries = routes.audit_entries(zone.site.organization_id, "coverage_read")
    assert [(e["scope_zone_id"], e["result_count"]) for e in entries] == [
        (zone.zone_id, 3),
        (zone.zone_id, 1),
    ]


def test_a_32_day_period_is_period_too_long_in_spanish_and_31_days_answer(routes: Routes) -> None:
    zone = _zone_with_a_communication_gap(routes)
    organization_id = zone.site.organization_id
    cookie = routes.person(organization_id, Role.COORDINATOR_SST)
    path = f"/zones/{zone.zone_id}/coverage"
    before = len(routes.audit_entries(organization_id, "coverage_read"))
    for days, extra in ((32, timedelta()), (31, timedelta(milliseconds=1))):
        response = routes.call(
            "GET",
            path,
            cookie=cookie,
            params={"from": _stamp(T0), "to": _stamp(T0 + timedelta(days=days) + extra)},
        )
        body = _error(response, "period_too_long", 400)
        assert body["message_es"].strip()
        assert "periodo" in body["message_es"].lower()
    # Ni consulta ni auditoría para un periodo demasiado largo.
    assert len(routes.audit_entries(organization_id, "coverage_read")) == before
    exact = routes.call(
        "GET", path, cookie=cookie, params={"from": _stamp(T0), "to": _stamp(T0 + timedelta(31))}
    )
    assert exact.status_code == 200, exact.text
    assert sum(exact.json()["summary"].values()) == 31 * 86_400_000


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-09-28T00:00:00.000", "to": "2026-09-28T01:00:00.000Z"},  # sin zona
        {"from": "2026-09-28", "to": "2026-09-29"},  # fecha sola
        {"from": "1790000000", "to": "1790003600"},  # número
        {"from": "2026-09-28T00:00:00.000001Z", "to": "2026-09-28T01:00:00Z"},  # por debajo de ms
        {"from": "2026-09-28T01:00:00Z", "to": "2026-09-28T00:00:00Z"},  # al revés
        {"from": "2026-09-28T00:00:00Z", "to": "2026-09-28T00:00:00Z"},  # vacío
        {"from": "2026-09-28T00:00:00Z"},  # falta to
        {"from": "2026-09-28T00:00:00Z", "to": "2026-09-28T01:00:00Z", "zona": "x"},  # desconocido
    ],
)
def test_invalid_coverage_periods_are_invalid_request(
    routes: Routes, params: dict[str, str]
) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    ((_, zone_id),) = site.zones()
    cookie = routes.person(site.organization_id, Role.COORDINATOR_SST)
    response = routes.call("GET", f"/zones/{zone_id}/coverage", cookie=cookie, params=params)
    _error(response, "invalid_request", 400)


def test_mixed_timezone_offsets_name_the_same_instants(routes: Routes) -> None:
    zone = _zone_with_a_communication_gap(routes)
    cookie = routes.person(zone.site.organization_id, Role.COORDINATOR_SST)
    utc = routes.call(
        "GET",
        f"/zones/{zone.zone_id}/coverage",
        cookie=cookie,
        params={"from": _stamp(T0), "to": _stamp(T0 + 4 * HOUR)},
    )
    bogota = routes.call(
        "GET",
        f"/zones/{zone.zone_id}/coverage",
        cookie=cookie,
        params={"from": "2026-09-27T19:00:00.000-05:00", "to": "2026-09-28T09:30:00+05:30"},
    )
    assert utc.status_code == bogota.status_code == 200
    assert utc.json()["intervals"] == bogota.json()["intervals"]


def test_coverage_scope_guards_zone_plant_and_organization(routes: Routes) -> None:
    site = routes.env.add_site(plants=2, zones_per_plant=2)
    zones = site.zones()
    (plant_a, zone_a1), (_, zone_a2), (plant_b, zone_b1), _ = zones
    params = {"from": _stamp(T0), "to": _stamp(T0 + HOUR)}
    zone_person = routes.person(site.organization_id, Role.COPASST, ScopeLevel.ZONE, zone_a1)
    plant_person = routes.person(site.organization_id, Role.LINE_MANAGER, ScopeLevel.PLANT, plant_a)
    other = routes.env.add_site(plants=1, zones_per_plant=1)
    stranger = routes.person(other.organization_id, Role.COORDINATOR_SST)
    allowed = {
        (zone_person, zone_a1): 200,
        (zone_person, zone_a2): 404,  # otra zona de la misma planta
        (plant_person, zone_a2): 200,
        (plant_person, zone_b1): 404,  # otra planta
        (stranger, zone_a1): 404,  # otra organización
    }
    for (cookie, zone_id), status in allowed.items():
        response = routes.call("GET", f"/zones/{zone_id}/coverage", cookie=cookie, params=params)
        assert response.status_code == status, (zone_id, response.text)
        if status == 404:
            _error(response, "not_found", 404)
    assert plant_b != plant_a


# --- Expediente: permiso según el tipo y alcance ---


def test_the_administrator_only_sees_the_integrity_view(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    ((plant_id, zone_id),) = site.zones()
    gate = routes.gate(site, plant_id, zone_id, T0)
    checkpoint = routes.checkpoint(site.organization_id, plant_id)
    admin = routes.person(site.organization_id, Role.ADMINISTRATOR)
    coordinator = routes.person(site.organization_id, Role.COORDINATOR_SST)

    # Coordinación SST: vista de hallazgos, cualquier tipo.
    listed = routes.call("GET", "/ledger/records", cookie=coordinator)
    assert listed.status_code == 200, listed.text
    assert {item["record_id"] for item in listed.json()["items"]} >= {
        str(gate),
        str(checkpoint.entry_id),
    }
    assert routes.call("GET", f"/ledger/records/{gate}", cookie=coordinator).status_code == 200

    # Administración: sin findings.read, ni la lista ni el detalle de un registro de U-03.
    before = len(routes.audit_entries(site.organization_id, "authorization_denied"))
    _error(routes.call("GET", "/ledger/records", cookie=admin), "not_found", 404)
    _error(
        routes.call(
            "GET",
            "/ledger/records",
            cookie=admin,
            params=[("record_type", "checkpoint"), ("record_type", "gate_state_changed")],
        ),
        "not_found",
        404,
    )
    _error(routes.call("GET", f"/ledger/records/{gate}", cookie=admin), "not_found", 404)
    after = routes.audit_entries(site.organization_id, "authorization_denied")
    assert len(after) == before + 2
    assert {json.loads(bytes(e["filters"]))["permission_key"] for e in after[before:]} == {
        "findings.read"
    }
    # …pero sí la vista de integridad: puntos de control y su detalle.
    integrity = routes.call(
        "GET", "/ledger/records", cookie=admin, params={"record_type": "checkpoint"}
    )
    assert integrity.status_code == 200, integrity.text
    assert [item["record_type"] for item in integrity.json()["items"]] == ["checkpoint"]
    detail = routes.call("GET", f"/ledger/records/{checkpoint.entry_id}", cookie=admin)
    assert detail.status_code == 200 and detail.json()["record_type"] == "checkpoint"


def test_records_scope_guards_zone_and_plant(routes: Routes) -> None:
    site = routes.env.add_site(plants=2, zones_per_plant=2)
    (plant_a, zone_a1), (_, zone_a2), (plant_b, zone_b1), _ = site.zones()
    in_zone = routes.gate(site, plant_a, zone_a1, T0)
    other_zone = routes.gate(site, plant_a, zone_a2, T0)
    other_plant = routes.gate(site, plant_b, zone_b1, T0)
    zone_coordinator = routes.person(
        site.organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_a1
    )
    plant_manager = routes.person(
        site.organization_id, Role.PLANT_MANAGER, ScopeLevel.PLANT, plant_a
    )
    listed = routes.call("GET", "/ledger/records", cookie=zone_coordinator)
    assert {i["record_id"] for i in listed.json()["items"]} == {str(in_zone)}
    for record_id, status in ((in_zone, 200), (other_zone, 404), (other_plant, 404)):
        response = routes.call("GET", f"/ledger/records/{record_id}", cookie=zone_coordinator)
        assert response.status_code == status
    plant_listed = routes.call("GET", "/ledger/records", cookie=plant_manager)
    assert {i["record_id"] for i in plant_listed.json()["items"]} == {
        str(in_zone),
        str(other_zone),
    }


def test_a_coordinator_of_one_plant_and_manager_of_another_reads_audit_only_where_allowed(
    routes: Routes,
) -> None:
    # Guarda del contexto reducido: audit.read lo da plant_manager (planta B), no la coordinación
    # SST (planta A). Sin reducir, LectorExpediente vería también la planta A.
    site = routes.env.add_site(plants=2, zones_per_plant=1)
    (plant_a, _), (plant_b, _) = site.zones()
    user_id = routes.env.user_with_role(
        site.organization_id, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_a
    )
    routes.env.authz.assign(
        site.organization_id, user_id, Role.PLANT_MANAGER, ScopeLevel.PLANT, plant_b
    )
    cookie = routes.env.authz.open_session(site.organization_id, user_id)
    for plant_id in (plant_a, plant_b):
        reader = routes.person(
            site.organization_id, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_id
        )
        routes.call("GET", "/ledger/records", cookie=reader)
    response = routes.call(
        "GET", "/audit/entries", cookie=cookie, params={"operation": "ledger_read"}
    )
    assert response.status_code == 200, response.text
    plants = {item["scope"]["plant_id"] for item in response.json()["items"]}
    assert plants == {str(plant_b)}


def test_a_zone_actors_reads_are_visible_to_the_plant_manager(routes: Routes) -> None:
    # Seguimiento de VIG-59: la entrada ledger_read sin filtros lleva la planta (y la zona) del
    # actor de zona; el plant_manager de esa planta la ve en GET /audit/entries.
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    ((plant_id, zone_id),) = site.zones()
    reader = routes.person(site.organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id)
    assert routes.call("GET", "/ledger/records", cookie=reader).status_code == 200
    (entry,) = routes.audit_entries(site.organization_id, "ledger_read")
    assert (entry["scope_plant_id"], entry["scope_zone_id"]) == (plant_id, zone_id)
    manager = routes.person(site.organization_id, Role.PLANT_MANAGER, ScopeLevel.PLANT, plant_id)
    response = routes.call(
        "GET", "/audit/entries", cookie=manager, params={"operation": "ledger_read"}
    )
    assert response.status_code == 200, response.text
    assert str(entry["entry_id"]) in {item["entry_id"] for item in response.json()["items"]}
    # La coordinación SST no tiene audit.read.
    _error(routes.call("GET", "/audit/entries", cookie=reader), "not_found", 404)


def test_records_are_paged_by_key_without_repeating_or_skipping(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    ((plant_id, zone_id),) = site.zones()
    written = {routes.gate(site, plant_id, zone_id, T0 + i * HOUR) for i in range(5)}
    cookie = routes.person(site.organization_id, Role.COORDINATOR_SST)
    seen: list[str] = []
    params: dict[str, str] = {"page_size": "2", "record_type": "gate_state_changed"}
    for _ in range(5):
        page = routes.call("GET", "/ledger/records", cookie=cookie, params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        seen += [item["record_id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
        params = {
            **params,
            "after_received_at": cursor["received_at"],
            "after_record_id": cursor["record_id"],
        }
    assert sorted(seen) == sorted(str(r) for r in written) and len(seen) == len(set(seen))


@pytest.mark.parametrize(
    "params",
    [
        [("page_size", "0")],
        [("page_size", "201")],
        [("page_size", "1"), ("page_size", "2")],  # repetido
        [("record_type", "Gate")],
        [("record_type", "gate_state_changed")] * 33,  # más de 32 tipos
        [("plant_id", "no-es-uuid")],
        [("received_from", "ayer")],
        [("after_received_at", "2026-09-28T00:00:00Z")],  # media clave
        [("filtro", "x")],  # desconocido
    ],
)
def test_invalid_record_queries_are_invalid_request(
    routes: Routes, params: list[tuple[str, str]]
) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    cookie = routes.person(site.organization_id, Role.COORDINATOR_SST)
    _error(
        routes.call("GET", "/ledger/records", cookie=cookie, params=params), "invalid_request", 400
    )


# --- Evidencias y etiquetas ---


def _evidence(routes: Routes, site: Site, plant_id: uuid.UUID, zone_id: uuid.UUID) -> uuid.UUID:
    """Una evidencia ya verificada de un registro de la zona (fila directa, datos generados)."""
    record_id = routes.gate(site, plant_id, zone_id, T0)
    values = evidence_values(site.organization_id, plant_id, record_id, T0)
    values.update(
        zone_id=zone_id,
        storage_key=f"org/{site.organization_id}/plant/{plant_id}/zone/{zone_id}/clip.mp4",
        sha256=hashlib.sha256(CLIP).hexdigest(),
        size_bytes=len(CLIP),
    )
    admin = routes.env.authz.sessions.admin

    async def insert() -> None:
        async with admin.transaction():
            await set_organization(admin, site.organization_id)
            await insert_evidence(admin, values)

    routes.run(insert())
    evidence_id: uuid.UUID = values["evidence_id"]
    return evidence_id


def test_evidence_read_url_is_granted_audited_and_fails_closed(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    ((plant_id, zone_id),) = site.zones()
    evidence_id = _evidence(routes, site, plant_id, zone_id)
    coordinator = routes.person(
        site.organization_id, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_id
    )
    response = routes.call("POST", f"/evidence/{evidence_id}/read-url", cookie=coordinator)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"evidence_id", "url", "expires_at"}
    assert body["evidence_id"] == str(evidence_id) and "versionId=v-sintetica-1" in body["url"]
    assert response.headers["Cache-Control"] == "no-store"
    (entry,) = routes.audit_entries(site.organization_id, "evidence_read_granted")
    assert (entry["outcome"], entry["resource_id"]) == ("success", evidence_id)

    # El administrador no lee evidencias (RF-PLA-11); otra zona tampoco.
    admin = routes.person(site.organization_id, Role.ADMINISTRATOR)
    _error(routes.call("POST", f"/evidence/{evidence_id}/read-url", cookie=admin), "not_found", 404)
    other = routes.env.add_site(plants=1, zones_per_plant=1)
    stranger = routes.person(other.organization_id, Role.COORDINATOR_SST)
    _error(
        routes.call("POST", f"/evidence/{evidence_id}/read-url", cookie=stranger), "not_found", 404
    )
    _error(
        routes.call("POST", f"/evidence/{uuid.uuid4()}/read-url", cookie=coordinator),
        "not_found",
        404,
    )
    # Sin versión en el almacén, no se firma nada (seguimiento de VIG-65): conflict.
    routes.storage.version_id = None
    try:
        signed = len(routes.storage.presigned)
        _error(
            routes.call("POST", f"/evidence/{evidence_id}/read-url", cookie=coordinator),
            "conflict",
            409,
        )
        assert len(routes.storage.presigned) == signed
    finally:
        routes.storage.version_id = "v-sintetica-1"


def test_labels_are_queried_by_period_and_audited(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    coordinator = routes.person(site.organization_id, Role.COORDINATOR_SST)
    params = {"from": _stamp(T0), "to": _stamp(T0 + HOUR)}
    response = routes.call("GET", "/labels", cookie=coordinator, params=params)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}
    (entry,) = routes.audit_entries(site.organization_id, "label_read")
    assert entry["outcome"] == "success"
    _error(
        routes.call("GET", "/labels", cookie=coordinator, params={"from": _stamp(T0)}),
        "invalid_request",
        400,
    )
    admin = routes.person(site.organization_id, Role.ADMINISTRATOR)
    _error(routes.call("GET", "/labels", cookie=admin, params=params), "not_found", 404)


# --- Vista en vivo ---


def _zone_with_node(routes: Routes) -> tuple[Site, uuid.UUID, uuid.UUID, uuid.UUID]:
    site = routes.env.add_site(plants=1, zones_per_plant=2)
    (plant_id, zone_id), (_, empty_zone) = site.zones()
    node_id = routes.env.add_node(site.organization_id, plant_id, url=LIVE_VIEW_URL)
    routes.env.assign_node(site.organization_id, plant_id, zone_id, node_id)
    return site, zone_id, empty_zone, node_id


def test_live_view_token_is_issued_to_copasst_and_verified_by_the_node(routes: Routes) -> None:
    site, zone_id, empty_zone, node_id = _zone_with_node(routes)
    copasst = routes.person(site.organization_id, Role.COPASST, ScopeLevel.ZONE, zone_id)
    response = routes.call("POST", f"/zones/{zone_id}/live-view-token", cookie=copasst)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"token", "live_view_local_url", "expires_at"}
    assert body["live_view_local_url"] == LIVE_VIEW_URL
    claims = node_verifies(
        body["token"], node_id, routes.env.node_key_set(), routes.env.clock.now()
    )
    assert claims is not None and claims.role.value == "copasst"
    assert response.headers["Cache-Control"] == "no-store"
    _error(
        routes.call("POST", f"/zones/{empty_zone}/live-view-token", cookie=copasst),
        "not_found",
        404,
    )  # la otra zona no es de su alcance
    coordinator = routes.person(site.organization_id, Role.COORDINATOR_SST)
    _error(
        routes.call("POST", f"/zones/{empty_zone}/live-view-token", cookie=coordinator),
        "zone_without_node",
        409,
    )
    line = routes.person(site.organization_id, Role.LINE_MANAGER, ScopeLevel.ZONE, zone_id)
    _error(routes.call("POST", f"/zones/{zone_id}/live-view-token", cookie=line), "not_found", 404)


def test_a_burst_waiting_for_the_users_lock_is_rate_limited(routes: Routes) -> None:
    # Seguimiento de VIG-80: otra emisión del mismo usuario tiene la exclusión; al vencer
    # lock_timeout la ruta responde rate_limited (reintento en 1 s), no temporarily_unavailable.
    site, zone_id, _, _ = _zone_with_node(routes)
    user_id = routes.env.user_with_role(site.organization_id, Role.COORDINATOR_SST)
    cookie = routes.env.authz.open_session(site.organization_id, user_id)
    admin = routes.env.authz.sessions.admin

    async def burst() -> httpx.Response:
        async with admin.transaction():
            await admin.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('live_view_token|' || $1, 0))",
                str(user_id),
            )
            return await routes.client.post(
                f"/zones/{zone_id}/live-view-token",
                headers={**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"},
            )

    response = routes.run(burst())
    body = _error(response, "rate_limited", 429)
    assert body["retry_after_seconds"] == 1 and response.headers["Retry-After"] == "1"
    # Sin la exclusión ajena, la siguiente emisión sale.
    again = routes.call("POST", f"/zones/{zone_id}/live-view-token", cookie=cookie)
    assert again.status_code == 200, again.text


def test_the_thirty_first_token_in_ten_minutes_is_rate_limited(routes: Routes) -> None:
    site, zone_id, _, _ = _zone_with_node(routes)
    cookie = routes.person(site.organization_id, Role.ADMINISTRATOR)
    for _ in range(30):
        assert (
            routes.call("POST", f"/zones/{zone_id}/live-view-token", cookie=cookie).status_code
            == 200
        )
    body = _error(
        routes.call("POST", f"/zones/{zone_id}/live-view-token", cookie=cookie), "rate_limited", 429
    )
    assert 1 <= body["retry_after_seconds"] <= 600


# --- Concesión del proveedor (BR-NUC-37, 38) ---


def test_under_concession_coverage_and_live_view_write_provider_query(routes: Routes) -> None:
    # Un solo reloj (VIG-135): la RLS de la concesión mira el now() de la base, y las claves de
    # firma del entorno nacieron a esa hora.
    with routes.env.on_database_time():
        _under_concession_coverage_and_live_view(routes)


def _under_concession_coverage_and_live_view(routes: Routes) -> None:
    site, zone_id, _, _ = _zone_with_node(routes)
    authz = routes.env.authz
    installer = authz.add_provider_user()
    concession = authz.add_concession(site.organization_id, installer)
    cookie = authz.open_session(authz.provider_organization_id, installer)
    headers = {"X-Vigia-Concession": str(concession)}
    coverage = routes.call(
        "GET",
        f"/zones/{zone_id}/coverage",
        cookie=cookie,
        headers=headers,
        params={"from": _stamp(T0), "to": _stamp(T0 + HOUR)},
    )
    assert coverage.status_code == 200, coverage.text
    token = routes.call("POST", f"/zones/{zone_id}/live-view-token", cookie=cookie, headers=headers)
    assert token.status_code == 200, token.text
    queries = routes.fetch(
        "SELECT content_json FROM ledger.ledger_record WHERE organization_id = $1"
        " AND record_type = 'provider_query' ORDER BY chain_sequence",
        site.organization_id,
    )
    documents = [json.loads(row["content_json"]) for row in queries]
    assert [(d["operation"], d["method"], d["resource"]) for d in documents] == [
        ("read", "GET", "/zones/{zone_id}/coverage"),
        ("write", "POST", "/zones/{zone_id}/live-view-token"),
    ]
    assert {d["concession_id"] for d in documents} == {str(concession)}
    # Lo que no es de su columna (BR-NUC-37): ni expediente, ni auditoría, ni etiquetas.
    for method, path in (("GET", "/ledger/records"), ("GET", "/audit/entries")):
        _error(routes.call(method, path, cookie=cookie, headers=headers), "not_found", 404)
    # Sin concesión, en la proveedora, la zona del cliente no existe.
    _error(
        routes.call(
            "GET",
            f"/zones/{zone_id}/coverage",
            cookie=cookie,
            params={"from": _stamp(T0), "to": _stamp(T0 + HOUR)},
        ),
        "not_found",
        404,
    )


# --- Integridad: a demanda en el worker, resultados y puntos de control ---


def test_on_demand_verification_runs_in_the_worker_once_per_request(routes: Routes) -> None:
    site = routes.env.add_site(plants=2, zones_per_plant=1)
    (plant_a, zone_a), (plant_b, _) = site.zones()
    routes.gate(site, plant_a, zone_a, T0)
    coordinator = routes.person(site.organization_id, Role.COORDINATOR_SST)
    accepted = routes.call(
        "POST",
        "/integrity/verify",
        cookie=coordinator,
        json_body={"kind": "ledger", "plant_id": str(plant_a)},
    )
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["status"] == "queued" and body["chain"] == {
        "kind": "ledger",
        "plant_id": str(plant_a),
    }
    (event,) = routes.events(site.organization_id, INTEGRITY_VERIFICATION_REQUESTED)
    assert str(event["event_id"]) == body["request_id"]
    payload = json.loads(event["payload"])
    assert payload["chain_kind"] == "ledger" and payload["plant_id"] == str(plant_a)

    # El worker: el consumidor con el contexto del evento; una reentrega no verifica otra vez.
    outbox_event = OutboxEvent(
        event_id=uuid.UUID(str(event["event_id"])),
        organization_id=site.organization_id,
        plant_id=plant_a,
        event_name=INTEGRITY_VERIFICATION_REQUESTED,
        partition_key=event["partition_key"],
        ledger_sequence=None,
        payload=payload,
        correlation_id=uuid.UUID(str(event["correlation_id"])),
        created_at=event["created_at"],
    )
    consumer = IntegrityOnDemandConsumer(routes.verifier)
    contexts = routes.env.authz.contexts
    database = routes.env.authz.sessions.database

    async def deliver() -> None:
        async with database.transaction(contexts.context_from_event(outbox_event)) as transaction:
            await consumer(outbox_event, transaction)

    routes.run(deliver())
    routes.run(deliver())
    results = [
        json.loads(bytes(e["filters"]))
        for e in routes.audit_entries(site.organization_id, "integrity_verification")
    ]
    assert [(r["mode"], r["result"], r["plant_id"]) for r in results] == [
        ("on_demand", "intact", str(plant_a))
    ]
    listed = routes.call("GET", "/integrity/results", cookie=coordinator)
    assert listed.status_code == 200, listed.text
    (result,) = listed.json()["results"]
    assert (result["mode"], result["result"], result["chain"]["plant_id"]) == (
        "on_demand",
        "intact",
        str(plant_a),
    )

    # Cadenas que no existen o fuera de alcance: not_found; cuerpos inválidos: invalid_request.
    _error(
        routes.call(
            "POST",
            "/integrity/verify",
            cookie=coordinator,
            json_body={"kind": "ledger", "plant_id": str(plant_b)},
        ),
        "not_found",
        404,
    )  # la planta B no tiene cadena todavía
    plant_coordinator = routes.person(
        site.organization_id, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_b
    )
    _error(
        routes.call(
            "POST",
            "/integrity/verify",
            cookie=plant_coordinator,
            json_body={"kind": "ledger", "plant_id": str(plant_a)},
        ),
        "not_found",
        404,
    )
    assert routes.call("GET", "/integrity/results", cookie=plant_coordinator).json() == {
        "results": []
    }


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "audit", "plant_id": str(uuid.UUID(int=1))},
        {"kind": "ledger", "plant_id": "no-es-uuid"},
        {"kind": "ledgers"},
        {"kind": None},
        {"kind": "ledger", "extra": True},
        {"kind": 1},
        [],
        {},
    ],
)
def test_invalid_verification_bodies_are_invalid_request(routes: Routes, body: Any) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    cookie = routes.person(site.organization_id, Role.COORDINATOR_SST)
    response = routes.call("POST", "/integrity/verify", cookie=cookie, json_body=body)
    _error(response, "invalid_request", 400)
    assert "Traceback" not in response.text and "pydantic" not in response.text


def test_oversized_and_broken_verification_bodies(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    cookie = routes.person(site.organization_id, Role.COORDINATOR_SST)
    too_big = b'{"kind": "ledger", "x": "' + b"a" * (1024 * 1024) + b'"}'
    _error(
        routes.call(
            "POST",
            "/integrity/verify",
            cookie=cookie,
            content=too_big,
            headers={"Content-Type": "application/json"},
        ),
        "payload_too_large",
        413,
    )
    _error(
        routes.call(
            "POST",
            "/integrity/verify",
            cookie=cookie,
            content=b'{"kind": ',
            headers={"Content-Type": "application/json"},
        ),
        "invalid_request",
        400,
    )


def _keys(routes: Routes) -> list[dict[str, str]]:
    response = routes.call("GET", "/.well-known/vigia-checkpoint-keys")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["purpose"] == "checkpoint"
    keys: list[dict[str, str]] = body["keys"]
    return keys


def test_h_36_verification_with_the_previous_checkpoint(routes: Routes, tmp_path: Path) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    ((plant_id, zone_id),) = site.zones()
    for i in range(3):
        routes.gate(site, plant_id, zone_id, T0 + i * HOUR)
    first = routes.checkpoint(site.organization_id, plant_id)
    coordinator = routes.person(site.organization_id, Role.COORDINATOR_SST)

    # 1. El cliente guarda el último punto de control y lo comprueba con la clave publicada.
    listed = routes.call("GET", "/integrity/checkpoints", cookie=coordinator)
    assert listed.status_code == 200, listed.text
    (published,) = [
        c for c in listed.json()["checkpoints"] if c["chain"]["plant_id"] == str(plant_id)
    ]
    assert published["entry_id"] == str(first.entry_id)
    keys = _keys(routes)
    public = {key["key_id"]: base64.b64decode(key["public_key"]) for key in keys}
    content = CheckpointContent.from_json(
        {
            k: published[k]
            for k in ("covered_sequence", "covered_hash", "taken_at", "key_id", "signature")
        }
    )
    assert verify_checkpoint(site.organization_id, CheckpointChain.plant(plant_id), content, public)
    record = routes.call("GET", f"/ledger/records/{first.entry_id}", cookie=coordinator).json()
    previous = tmp_path / "anterior.json"
    package_keys = (
        "record_id", "organization_id", "plant_id", "chain_sequence", "record_type",
        "schema_version", "actor", "scope", "correlation_id", "received_at", "content",
        "content_hash", "previous_hash", "record_hash",
    )  # fmt: skip
    previous.write_text(json.dumps({k: record[k] for k in package_keys}), encoding="utf-8")

    # 2. Un mes después, el paquete nuevo (más registros y otro punto de control).
    for i in range(3, 5):
        routes.gate(site, plant_id, zone_id, T0 + i * HOUR)
    routes.checkpoint(site.organization_id, plant_id)
    rows = routes.fetch(
        "SELECT * FROM ledger.ledger_record WHERE organization_id = $1 AND plant_id = $2"
        " ORDER BY chain_sequence",
        site.organization_id,
        plant_id,
    )
    (head,) = routes.fetch(
        "SELECT * FROM ledger.chain_head WHERE organization_id = $1 AND plant_id = $2",
        site.organization_id,
        plant_id,
    )
    chain = PackageChain(
        "ledger",
        str(plant_id),
        "chains/ledger-plant.jsonl",
        [row_to_entry("ledger", row) for row in rows],
        1,
        head["last_sequence"],
        head["last_hash"],
    )
    manifest = {
        "format": "vigia-package",
        "format_version": 1,
        "organization_id": str(site.organization_id),
        "chains": [chain.manifest()],
        "checkpoint_keys": [{"key_id": k["key_id"], "public_key": k["public_key"]} for k in keys],
    }
    package = tmp_path / "paquete"
    package.mkdir()
    write_package(package, site.organization_id, [chain], [], manifest=manifest)
    report = verify_package(package, [previous])
    assert report.intact, report
    assert [p.status for p in report.previous] == ["matched"]

    # 3. Un ancla falsificada (otro hash cubierto) no se acepta como paquete anterior.
    forged = dict(record)
    forged["content"] = {**record["content"], "covered_hash": "0" * 64}
    tampered = tmp_path / "falsificado.json"
    tampered.write_text(json.dumps({k: forged[k] for k in package_keys}), encoding="utf-8")
    assert not verify_package(package, [tampered]).intact

    # 4. Un registro alterado del prefijo rompe el paquete en su secuencia.
    target = package / "chains" / "ledger-plant.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    altered = json.loads(lines[1])
    altered["content"]["resulting_mode"] = "commissioning"
    lines[1] = json.dumps(altered, ensure_ascii=False)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    broken = verify_package(package, [previous])
    assert not broken.intact


def test_checkpoints_and_results_are_filtered_by_chain_scope(routes: Routes) -> None:
    site = routes.env.add_site(plants=2, zones_per_plant=1)
    (plant_a, zone_a), (plant_b, zone_b) = site.zones()
    routes.gate(site, plant_a, zone_a, T0)
    routes.gate(site, plant_b, zone_b, T0)
    routes.checkpoint(site.organization_id, plant_a)
    routes.checkpoint(site.organization_id, plant_b)
    plant_coordinator = routes.person(
        site.organization_id, Role.COORDINATOR_SST, ScopeLevel.PLANT, plant_a
    )
    seen = routes.call("GET", "/integrity/checkpoints", cookie=plant_coordinator).json()
    assert {c["chain"]["plant_id"] for c in seen["checkpoints"]} == {str(plant_a)}
    admin = routes.person(site.organization_id, Role.ADMINISTRATOR)
    everything = routes.call("GET", "/integrity/checkpoints", cookie=admin).json()
    assert {c["chain"]["plant_id"] for c in everything["checkpoints"]} >= {
        str(plant_a),
        str(plant_b),
    }
    copasst = routes.person(site.organization_id, Role.COPASST)
    _error(routes.call("GET", "/integrity/checkpoints", cookie=copasst), "not_found", 404)


# --- Claves públicas y operación ---


def test_rotated_and_retired_checkpoint_keys_stay_published(routes: Routes) -> None:
    before = {key["key_id"] for key in _keys(routes)}
    operator = routes.operator()
    rotated = routes.call("POST", "/platform/keys/checkpoint/rotate", cookie=operator)
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["purpose"] == "checkpoint" and body["previous_key_id"] in before
    assert "private" not in rotated.text and "secret" not in rotated.text
    entries = routes.audit_entries(routes.provider, "key_rotated")
    assert json.loads(bytes(entries[-1]["filters"]))["key_id"] == body["key_id"]

    # La clave anterior vence y se retira: sigue publicada (BR-NUC-55).
    clock = routes.env.clock
    now = clock.now()
    clock.set(now + timedelta(days=400))
    try:
        routes.run(routes.signing.retire_expired())
    finally:
        clock.set(now)
    published = {key["key_id"]: key["status"] for key in _keys(routes)}
    assert published[body["previous_key_id"]] == "retired"
    assert published[body["key_id"]] == "active"
    assert before <= set(published)
    assert all(set(key) == {"key_id", "algorithm", "public_key", "status", "valid_from",
                            "valid_until"} for key in _keys(routes))  # fmt: skip


def test_platform_operations_are_only_for_the_operator(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    for role in (Role.ADMINISTRATOR, Role.COORDINATOR_SST):
        cookie = routes.person(site.organization_id, role)
        _error(
            routes.call("POST", "/platform/keys/checkpoint/rotate", cookie=cookie), "not_found", 404
        )
        _error(
            routes.call(
                "POST", f"/platform/dead-letter/{uuid.uuid4()}/alert_metrics/replay", cookie=cookie
            ),
            "not_found",
            404,
        )
    installer = routes.env.authz.add_provider_user()
    cookie = routes.env.authz.open_session(routes.provider, installer)
    _error(routes.call("POST", "/platform/keys/gate/rotate", cookie=cookie), "not_found", 404)
    operator = routes.operator()
    _error(
        routes.call("POST", "/platform/keys/no_existe/rotate", cookie=operator),
        "invalid_request",
        400,
    )
    # El propio operador, bajo una concesión sobre un cliente, tampoco opera la plataforma.
    authz = routes.env.authz
    concession = authz.add_concession(site.organization_id, authz.operator_id)
    headers = {"X-Vigia-Concession": str(concession)}
    for path in (
        "/platform/keys/checkpoint/rotate",
        f"/platform/dead-letter/{uuid.uuid4()}/alert_metrics/replay",
    ):
        _error(routes.call("POST", path, cookie=operator, headers=headers), "not_found", 404)


def test_dead_letter_replay_by_the_operator_keeps_the_event_id(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    event_id, consumer = uuid7(), "route_probe_consumer"
    routes.execute(
        "INSERT INTO shared.consumer (consumer_name, unit, subscribed_events)"
        " VALUES ($1, 'U-02', '{security_alert}') ON CONFLICT DO NOTHING",
        consumer,
    )
    routes.execute(
        "INSERT INTO shared.outbox_event (event_id, organization_id, event_name, payload,"
        " correlation_id, created_at) VALUES ($1, $2, 'security_alert', $3, $4, $5)",
        event_id,
        site.organization_id,
        json.dumps({"alert_kind": "context_absent_attempt", "occurred_at": _stamp(T0)}),
        uuid7(),
        T0,
    )
    routes.execute(
        "INSERT INTO shared.outbox_delivery (event_id, consumer_name, organization_id, status,"
        " attempts, next_attempt_at, last_error_code) VALUES ($1, $2, $3, 'dead_letter', 8, $4,"
        " 'handler_failed')",
        event_id,
        consumer,
        site.organization_id,
        T0,
    )
    routes.execute(
        "INSERT INTO shared.dead_letter (event_id, consumer_name, organization_id, failed_at,"
        " attempts, last_error_code) VALUES ($1, $2, $3, $4, 8, 'handler_failed')",
        event_id,
        consumer,
        site.organization_id,
        T0,
    )
    operator = routes.operator()
    path = f"/platform/dead-letter/{event_id}/{consumer}/replay"
    response = routes.call("POST", path, cookie=operator)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "event_id": str(event_id),
        "consumer_name": consumer,
        "organization_id": str(site.organization_id),
        "status": "pending",
    }
    (delivery,) = routes.fetch(
        "SELECT status, attempts FROM shared.outbox_delivery WHERE event_id = $1", event_id
    )
    assert (delivery["status"], delivery["attempts"]) == ("pending", 0)
    assert len(routes.fetch("SELECT 1 FROM shared.dead_letter WHERE event_id = $1", event_id)) == 1
    replayed = routes.audit_entries(routes.provider, "dead_letter_replayed")
    assert replayed[-1]["resource_id"] == event_id
    # Ya no está en la cola muerta: not_found.
    _error(routes.call("POST", path, cookie=operator), "not_found", 404)


# --- authorization_denied repetido (NFR-NUC-28; seguimiento de VIG-77) ---


def test_the_twenty_first_denial_in_ten_minutes_raises_one_security_alert(routes: Routes) -> None:
    site = routes.env.add_site(plants=1, zones_per_plant=1)
    user_id = routes.env.user_with_role(site.organization_id, Role.COPASST)
    cookie = routes.env.authz.open_session(site.organization_id, user_id)

    def alerts() -> list[Any]:
        return [
            json.loads(row["payload"])
            for row in routes.events(site.organization_id, "security_alert")
            if json.loads(row["payload"])["alert_kind"] == "authorization_denied_repeated"
        ]

    for _ in range(DENIED_REPEATED_THRESHOLD):
        _error(routes.call("GET", "/audit/entries", cookie=cookie), "not_found", 404)
    assert alerts() == []
    _error(routes.call("GET", "/audit/entries", cookie=cookie), "not_found", 404)
    (alert,) = alerts()
    assert (alert["resource_kind"], alert["resource_id"]) == ("user", str(user_id))
    _error(routes.call("GET", "/audit/entries", cookie=cookie), "not_found", 404)
    assert len(alerts()) == 1  # una alerta por cruce del umbral, no una por denegación


# --- Autorización por recurso: la auditoría caída sigue siendo not_found (VIG-78) ---


class _AuditDown:
    async def authorization_denied(self, context: Any, key: Any, resource: Any) -> None:
        raise ConnectionError("base caída")


def test_resource_authorization_denies_not_found_even_if_the_audit_fails() -> None:
    from tests.authz_support import sealed_context

    organization_id = uuid.uuid4()
    context = sealed_context(
        organization_id,
        [AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.COPASST)],
    )
    authorizer = Authorizer(audit=_AuditDown(), provider_organization_id=uuid.uuid4())
    with pytest.raises(ResourceNotFound):
        asyncio.run(
            authorizer.authorize(
                context, PermissionKey.AUDIT_READ, Resource.organization(organization_id)
            )
        )


def test_narrowed_keeps_only_the_assignments_with_the_key() -> None:
    from tests.authz_support import sealed_context

    organization_id, plant_a, plant_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    context = sealed_context(
        organization_id,
        [
            AllowedScope(ScopeLevel.PLANT, plant_a, Role.COORDINATOR_SST),
            AllowedScope(ScopeLevel.PLANT, plant_b, Role.PLANT_MANAGER),
        ],
    )
    provider = uuid.uuid4()
    audit = narrowed(context, PermissionKey.AUDIT_READ, provider_organization_id=provider)
    assert audit is not None
    assert [s.scope_id for s in audit.allowed_scopes] == [plant_b]
    assert audit.actor.role_in_use is Role.PLANT_MANAGER
    assert (
        narrowed(context, PermissionKey.PLATFORM_KEYS_ROTATE, provider_organization_id=provider)
        is None
    )
    evidence = narrowed(context, PermissionKey.EVIDENCE_READ, provider_organization_id=provider)
    assert evidence is not None and len(evidence.allowed_scopes) == 2


def test_the_checkpoint_public_key_document_never_has_a_secret_reference(routes: Routes) -> None:
    records = routes.signing.public_keys(SigningPurpose.CHECKPOINT)
    for key in _keys(routes):
        assert set(key) == {
            "key_id",
            "algorithm",
            "public_key",
            "status",
            "valid_from",
            "valid_until",
        }
    assert {CheckpointPublicKey.of(r).key_id for r in records} == {
        k["key_id"] for k in _keys(routes)
    }
