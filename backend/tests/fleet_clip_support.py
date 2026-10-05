"""Entorno de las pruebas de integración de los clips del nodo (TASK-222; LC-GOB-13, 18).

PostgreSQL 16 real como ``vigia_app`` (``authz_environment``: sesiones, contextos y
``Authorizer`` reales) y un depósito **versionado** de LocalStack, como ``vigia-evidence``:

- ``CountingStorage``: delegado conmutable del ``StoragePort`` que cuenta cada operación; cualquier
  otra que ``head_object`` y ``presign_put`` (``get_object`` incluida) queda contada y **falla**:
  los clips se verifican solo por metadatos (NFR-GOB-09). ``Hung`` es el almacén que nunca
  responde (FS-GOB-01 en comportamiento).
- ``HttpsUrls``: la URL de LocalStack es ``http``; el contrato exige ``https`` en ``upload_url``.
  Solo para las pruebas de la ruta HTTP de la concesión, que no suben con esa URL.
- Servicios reales (``ClipGrantService``, ``ClipConfirmationService``, ``CommissioningClips`` y
  ``OrphanClipSweeper``) con topes holgados (60 s, retro 15): el tope corto (5 s de producción)
  solo en ``fragile_grants``, para las pruebas del almacén sin respuesta.
- La aplicación real de las rutas del contrato (``create_app`` con la cadena fija, la
  ``NodeApiGate`` real y ``PostgresNodeContextStore``) con los manejadores de TASK-222, y nodos
  dados de alta con su zona y su certificado de ``TestAuthority`` (``tests/node_api_db.py``).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
from vigia_contracts.models import api
from vigia_contracts.models.clip_upload_request import ClipUploadRequest

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, authz_environment
from tests.dispatch_support import metrics_with_reader
from tests.factories import uuid7
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.node_api_db import DbNode, insert_node, issue
from tests.node_api_support import VERSION, Probe, TestAuthority, alb_headers, node_app, node_gate
from tests.writer_support import unit_context
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.application.clip_confirmation import (
    ClipConfirmationService,
    CommissioningClips,
)
from vigia_platform.fleet.application.clip_grants import ClipGrantService
from vigia_platform.fleet.application.orphan_clips import OrphanClipSweeper
from vigia_platform.identity.auth.sessions import SessionCookie
from vigia_platform.identity.authz.context import NodeScope, PresentedNode
from vigia_platform.node_api.certificate_profile import serial_hex
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.routes.clip_confirmations import clip_confirmation_operation
from vigia_platform.node_api.routes.clip_uploads import clip_upload_operation
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.storage import ObjectHead, PresignedRequest, S3Storage, sha256_b64

__all__ = [
    "LONG_TIMEOUT_SECONDS",
    "ClipNode",
    "ClipWorld",
    "CountingStorage",
    "Hung",
    "clip_world",
    "video",
]

LONG_TIMEOUT_SECONDS: Final = 60.0
"""Topes de las fixtures (retro 15): nunca deciden una prueba que no trata del tope."""
HOUR: Final = timedelta(hours=1)
NODE_BASE_URL: Final = "https://nodes.vigia.test"
GRANT_PATH: Final = NodeRoute.CLIP_UPLOAD.path
CONFIRMATION_PATH: Final = "/api/nodes/clip-uploads/{clip_id}/confirmation"


def video(label: str, size: int = 2048) -> bytes:
    """Bytes sintéticos de un clip (nunca un video real)."""
    seed = hashlib.sha256(label.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


# --- Almacén ------------------------------------------------------------------------------------


class Hung:
    """Almacén que nunca responde."""

    async def head_object(self, key: str) -> ObjectHead | None:
        await asyncio.Event().wait()
        raise AssertionError("inalcanzable")  # pragma: no cover

    async def presign_put(self, *_: Any, **__: Any) -> PresignedRequest:
        await asyncio.Event().wait()
        raise AssertionError("inalcanzable")  # pragma: no cover


class CountingStorage:
    """Delegado conmutable que cuenta cada operación; cualquier otra que ``head_object`` y
    ``presign_put`` (``get_object`` incluida) queda contada y falla."""

    def __init__(self, target: Any) -> None:
        self.target = target
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def forbidden(*_: Any, **__: Any) -> Any:
            self.calls[name] += 1
            raise AssertionError(f"operación prohibida en clips: {name}")

        return forbidden

    async def head_object(self, key: str) -> ObjectHead | None:
        self.calls["head_object"] += 1
        result: ObjectHead | None = await self.target.head_object(key)
        return result

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = timedelta(minutes=15),
    ) -> PresignedRequest:
        self.calls["presign_put"] += 1
        presigned: PresignedRequest = await self.target.presign_put(
            key, content_type, checksum_sha256, required_headers, ttl
        )
        return presigned


class HttpsUrls:
    """``https`` en la URL firmada (el contrato lo exige); solo para la respuesta HTTP."""

    def __init__(self, target: Any) -> None:
        self.target = target

    async def head_object(self, key: str) -> ObjectHead | None:
        result: ObjectHead | None = await self.target.head_object(key)
        return result

    async def presign_put(self, *arguments: Any) -> PresignedRequest:
        presigned: PresignedRequest = await self.target.presign_put(*arguments)
        return replace(presigned, url=presigned.url.replace("http://", "https://", 1))


# --- Entorno ------------------------------------------------------------------------------------


@dataclass
class ClipNode:
    db: DbNode
    certificate: Any

    @property
    def headers(self) -> dict[str, str]:
        return {**alb_headers(self.certificate), "X-Vigia-Contract-Version": VERSION}

    @property
    def presented(self) -> PresentedNode:
        return PresentedNode(
            node_id=self.db.node_id,
            organization_id=self.db.organization_id,
            plant_id=self.db.plant_id,
            certificate_serial=serial_hex(self.certificate.serial_number),
        )


@dataclass
class ClipWorld:
    authz: AuthzEnvironment
    s3: Any
    bucket: str
    real: S3Storage
    storage: CountingStorage
    metrics: PlatformMetrics
    reader: Any
    grants: ClipGrantService
    fragile_grants: ClipGrantService
    """Tope de producción (5 s): solo para las pruebas del almacén sin respuesta."""
    confirmations: ClipConfirmationService
    listing: CommissioningClips
    sweeper: OrphanClipSweeper
    identity: NodeIdentity
    client: httpx.AsyncClient
    authority: TestAuthority

    # --- Básicos ------------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    @property
    def clock(self) -> Any:
        return self.authz.sessions.clock

    def now(self) -> datetime:
        return self.authz.now()

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    # --- Nodos y personas ---------------------------------------------------------------------

    def organization(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        """Organización nueva con una planta y su usuario: ``(org, planta, usuario)``."""
        site = self.authz.add_site(plants=1, zones_per_plant=1)
        (plant_id,) = site.plants
        user_id = self.authz.add_user(site.organization_id)
        return site.organization_id, plant_id, user_id

    def node(
        self,
        organization: tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None = None,
    ) -> ClipNode:
        """Nodo dado de alta con una zona propia asignada y su certificado vigente."""
        organization_id, plant_id, user_id = organization or self.organization()
        db = self.run(
            insert_node(self.authz.sessions.admin, organization_id, plant_id, user_id, self.now())
        )
        certificate, _ = self.run(issue(self.authz.sessions.admin, self.authority, db, self.now()))
        return ClipNode(db, certificate)

    def scope(self, node: ClipNode) -> NodeScope:
        scope: NodeScope = self.run(self.identity.resolve(node.presented, uuid7()))
        return scope

    def member(self, organization_id: uuid.UUID, role: Role = Role.COORDINATOR_SST) -> ScopeContext:
        user = self.authz.add_user(organization_id)
        self.authz.assign(organization_id, user, role)
        cookie: SessionCookie = self.authz.open_session(organization_id, user)
        scope = self.run(self.authz.contexts.context_from_session(cookie))
        context: ScopeContext = scope.context
        return context

    def installer(self, organization_id: uuid.UUID) -> ScopeContext:
        """Instalador del proveedor con una concesión vigente de toda la organización."""
        installer = self.authz.add_provider_user()
        concession = self.authz.add_concession(
            organization_id, installer, granted_at=self.now() - HOUR
        )
        cookie = self.authz.open_session(self.authz.provider_organization_id, installer)
        scope = self.run(self.authz.contexts.context_from_session(cookie, concession_id=concession))
        context: ScopeContext = scope.context
        return context

    def system(self, organization_id: uuid.UUID) -> ScopeContext:
        """Contexto de la iteración periódica de una organización (``mark_orphan_clips``)."""
        return unit_context(organization_id, ActorUnit.U03, kind=ActorKind.SYSTEM, role=None)

    # --- Peticiones ---------------------------------------------------------------------------

    def body(
        self,
        node: ClipNode,
        data: bytes,
        *,
        clip_id: uuid.UUID | None = None,
        purpose: str | None = "verification",
        zone_id: uuid.UUID | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            "clip_id": str(clip_id or uuid7()),
            "camera_id": str(uuid.uuid4()),
            "zone_id": str(zone_id or node.db.zone_id),
            "media_kind": "video",
            "content_type": "video/mp4",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "duration_ms": 10_000,
        }
        if purpose is not None:
            document["purpose"] = purpose
        document.update(changes)
        return document

    def request(self, body: Mapping[str, Any]) -> ClipUploadRequest:
        import json

        return api.parse_clip_upload_request(json.dumps(body).encode())

    def post_grant(self, node: ClipNode, body: Any) -> httpx.Response:
        response: httpx.Response = self.run(
            self.client.post(GRANT_PATH, json=body, headers=node.headers)
        )
        return response

    def post_confirmation(self, node: ClipNode, clip_id: uuid.UUID | str) -> httpx.Response:
        response: httpx.Response = self.run(
            self.client.post(CONFIRMATION_PATH.format(clip_id=clip_id), headers=node.headers)
        )
        return response

    def put(self, url: str, headers: Mapping[str, str], data: bytes) -> httpx.Response:
        return httpx.put(url, content=data, headers=dict(headers), timeout=LONG_TIMEOUT_SECONDS)

    def put_directly(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "video/mp4",
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """Un objeto escrito por otra vía en la clave (lo que la verificación debe rechazar)."""
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ChecksumSHA256=sha256_b64(data),
            Metadata=dict(metadata if metadata is not None else {"vigia-anonymized": "1"}),
        )

    def issue(self, node: ClipNode, data: bytes, **body: Any) -> Any:
        """Concesión por el servicio (URL de LocalStack utilizable)."""
        return self.run(
            self.grants.issue(self.scope(node), self.request(self.body(node, data, **body)))
        )

    def uploaded(self, node: ClipNode, data: bytes, **body: Any) -> Any:
        issued = self.issue(node, data, **body)
        response = self.put(issued.upload.url, issued.upload.headers, data)
        assert response.status_code == 200, response.text
        return issued

    # --- Filas --------------------------------------------------------------------------------

    def grant_row(self, clip_id: uuid.UUID) -> Any:
        rows = self.fetch("SELECT * FROM fleet.clip_upload_grant WHERE clip_id = $1", clip_id)
        return rows[0] if rows else None

    def clip_rows(self, clip_id: uuid.UUID) -> list[Any]:
        return self.fetch("SELECT * FROM fleet.verification_clip WHERE clip_id = $1", clip_id)


@contextlib.contextmanager
def clip_world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint, prefix: str
) -> Iterator[ClipWorld]:
    s3 = localstack_endpoint.aws_client("s3")
    with (
        authz_environment(postgres_endpoint, prefix) as authz,
        versioned_bucket(s3, "vigia-clips") as bucket,
    ):
        # La RLS de las concesiones compara con la hora de la base: el reloj simulado arranca en
        # ella (VIG-135) y todo lo que se compara sale de él.
        clock = authz.sessions.clock
        (row,) = authz.fetch("SELECT now() AS now")
        clock.set(row["now"])
        database = authz.sessions.database
        real = S3Storage(
            replace(
                localstack_endpoint.storage_settings(bucket),
                connect_timeout_seconds=LONG_TIMEOUT_SECONDS,
                read_timeout_seconds=LONG_TIMEOUT_SECONDS,
            ),
            clock,
        )
        storage = CountingStorage(real)
        metrics, reader = metrics_with_reader()

        def store(target: Any, seconds: float = LONG_TIMEOUT_SECONDS) -> ClipObjectStore:
            return ClipObjectStore(
                target, presign_timeout_seconds=seconds, head_timeout_seconds=seconds
            )

        grants = ClipGrantService(
            database=database, store=store(storage), clock=clock, metrics=metrics
        )
        fragile_grants = ClipGrantService(
            database=database, store=ClipObjectStore(storage), clock=clock, metrics=metrics
        )
        confirmations = ClipConfirmationService(
            database=database, store=store(storage), clock=clock, metrics=metrics
        )
        node_store = PostgresNodeContextStore(database)
        gate = node_gate(
            contexts=authz.contexts,
            store=node_store,
            clock=clock,
            probe=Probe(),
            operations={
                NodeRoute.CLIP_UPLOAD: clip_upload_operation(
                    ClipGrantService(
                        database=database,
                        store=store(HttpsUrls(storage)),
                        clock=clock,
                        metrics=metrics,
                    )
                ),
                NodeRoute.CLIP_CONFIRMATION: clip_confirmation_operation(confirmations),
            },
        )
        app = node_app(
            World(clock=clock), gate, routes=(NodeRoute.CLIP_UPLOAD, NodeRoute.CLIP_CONFIRMATION)
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=NODE_BASE_URL,
            timeout=LONG_TIMEOUT_SECONDS,
        )
        world = ClipWorld(
            authz=authz,
            s3=s3,
            bucket=bucket,
            real=real,
            storage=storage,
            metrics=metrics,
            reader=reader,
            grants=grants,
            fragile_grants=fragile_grants,
            confirmations=confirmations,
            listing=CommissioningClips(
                database=database,
                authorizer=authz.authorizer,
                audit=authz.sessions.audit,
                clock=clock,
            ),
            sweeper=OrphanClipSweeper(
                database=database, store=store(storage), clock=clock, metrics=metrics
            ),
            identity=NodeIdentity(contexts=authz.contexts, store=node_store),
            client=client,
            authority=TestAuthority(),
        )
        try:
            yield world
        finally:
            authz.run(client.aclose())
