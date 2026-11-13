"""Entorno de la ingesta de la foto del episodio (TASK-572) sobre PostgreSQL 16 real, como
``vigia_app``.

Se apoya en ``fleet_ingest_support.ingest_stack`` (la base migrada, los sitios con su nodo dado de
alta, su certificado, su catálogo y su compuerta de uso) y añade:

- ``PhotoInstance``: una instancia de ``vigia-api`` con la ``NodeApiGate`` real, solo la ruta
  ``POST /api/nodes/episode-photos`` y la operación que compone la raíz
  (``episode_photo_operation`` sobre ``EpisodePhotoService`` y ``PostgresEpisodePhotoStore``), con
  métricas en memoria para leerlas;
- ``mounting``: la compuerta de **montaje** de la zona aprobada o revocada desde un instante;
- ``photo`` y ``rows``: una presentación válida de la zona y lo que quedó en
  ``fleet.episode_photo``.

Reloj simulado desde la hora de la base (retro 14), el mismo para la verificación previa y el
servicio (un solo reloj inyectado). Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from tests.api_support import World
from tests.episode_photo_support import submission
from tests.fleet_ingest_support import LOCK_TIMEOUT_MS, IngestSite, IngestStack
from tests.heartbeat_support import UnlimitedLimiter
from tests.node_api_support import VERSION, alb_headers, node_unit
from vigia_platform.fleet.adapters.postgres.episode_photo_store import PostgresEpisodePhotoStore
from vigia_platform.fleet.application.episode_photos import (
    EpisodePhotoDependencies,
    EpisodePhotoService,
)
from vigia_platform.node_api.identity import NodeIdentity, PostgresNodeContextStore
from vigia_platform.node_api.limits import NodeRateLimits
from vigia_platform.node_api.observability import NodeResponses
from vigia_platform.node_api.router import NodeApiGate
from vigia_platform.node_api.routes.episode_photos import episode_photo_operation
from vigia_platform.shared.api.declarations import NODE_GATE_STATE_KEY, NodeRoute
from vigia_platform.shared.db import Database
from vigia_platform.shared.observability.metrics import MetricName, PlatformMetrics

__all__ = ["LOCK_TIMEOUT_MS", "PATH", "PhotoInstance", "PhotoStack", "metric_points"]

PATH: Final = NodeRoute.EPISODE_PHOTO.path
ROUTES: Final = (NodeRoute.EPISODE_PHOTO,)


@dataclass
class PhotoInstance:
    database: Database
    service: EpisodePhotoService
    gate: NodeApiGate
    app: Any
    client: httpx.AsyncClient
    reader: InMemoryMetricReader


@dataclass
class PhotoStack:
    """La pila de la ingesta con la ruta de la foto del episodio."""

    ingest: IngestStack
    retention_days: int = 30
    primary: PhotoInstance = field(init=False)
    _extra: list[PhotoInstance] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.primary = self.instance(self.ingest.primary.database)

    # --- Utilidades ----------------------------------------------------------------------------

    def run(self, awaitable: Any) -> Any:
        return self.ingest.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.ingest.fetch(sql, *args)

    def execute(self, sql: str, *args: Any) -> None:
        self.ingest.execute(sql, *args)

    def now(self) -> dt.datetime:
        return self.ingest.now()

    # --- Instancias ----------------------------------------------------------------------------

    def instance(self, database: Database | None = None) -> PhotoInstance:
        ingest = self.ingest
        database = database if database is not None else ingest.database()
        clock = ingest.authz.sessions.clock
        reader = InMemoryMetricReader()
        metrics = PlatformMetrics(MeterProvider(metric_readers=[reader]).get_meter("pruebas"))
        service = EpisodePhotoService(
            EpisodePhotoDependencies(
                database=database,
                clock=clock,
                store=PostgresEpisodePhotoStore(database),
                retention_days=self.retention_days,
            )
        )
        gate = NodeApiGate(
            identity=NodeIdentity(
                contexts=ingest.authz.contexts, store=PostgresNodeContextStore(database)
            ),
            limits=NodeRateLimits(UnlimitedLimiter(clock)),
            clock=clock,
            responses=NodeResponses(clock),
            operations={NodeRoute.EPISODE_PHOTO: episode_photo_operation(service, metrics=metrics)},
        )
        app = World(clock=clock).app(
            units=(node_unit(ROUTES),), runtime={"state": {NODE_GATE_STATE_KEY: gate}}
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=120.0
        )
        built = PhotoInstance(database, service, gate, app, client, reader)
        self._extra.append(built)
        return built

    async def release(self) -> None:
        for built in self._extra:
            if built is not self.primary:
                await built.client.aclose()
                if built.database is not self.ingest.primary.database:
                    await built.database.dispose()
        self._extra = [self.primary]

    # --- Siembra -------------------------------------------------------------------------------

    def site(self, *, mounting: bool = True, **options: Any) -> IngestSite:
        site = self.ingest.site(**options)
        if mounting:
            self.mounting(site, approved=True, at=self.now() - dt.timedelta(days=400))
        return site

    def mounting(self, site: IngestSite, *, approved: bool, at: dt.datetime) -> None:
        """La compuerta de montaje pasa a ``approved`` o ``revoked`` en ``at``."""
        self.execute(
            "UPDATE catalog.gate_state_history SET effective_until = $2"
            " WHERE zone_id = $1 AND gate = 'mounting' AND effective_until IS NULL",
            site.zone_id,
            at,
        )
        self.execute(
            "INSERT INTO catalog.gate_state_history (organization_id, plant_id, zone_id, gate,"
            " status, effective_from, decided_by, reason_es, ledger_record_id, record_id)"
            " VALUES ($1, $2, $3, 'mounting', $4, $5, $6, $7, $8, $9)",
            site.organization_id,
            site.plant_id,
            site.zone_id,
            "approved" if approved else "revoked",
            at,
            self.ingest.authz.operator_id,
            None if approved else "Retirada sintética del montaje de la zona",
            uuid.uuid4(),
            uuid.uuid4(),
        )

    # --- Presentaciones ------------------------------------------------------------------------

    def photo(self, site: IngestSite, **options: Any) -> dict[str, Any]:
        """Una ``EpisodePhotoSubmission`` válida de ``site`` (imagen sintética por defecto)."""
        arguments: dict[str, Any] = {
            "organization_id": site.organization_id,
            "plant_id": site.plant_id,
            "zone_id": site.zone_id,
            "node_id": site.node_id,
            "standard_id": site.standard_id,
            "camera_id": site.camera_id,
            "started": self.now() - dt.timedelta(minutes=10),
        }
        arguments.update(options)
        return submission(**arguments)

    def headers(self, site: IngestSite, document: Mapping[str, Any]) -> dict[str, str]:
        return {
            **alb_headers(site.certificate),
            "X-Vigia-Contract-Version": VERSION,
            "Idempotency-Key": str(document.get("photo_id", "")),
            "Content-Type": "application/json",
        }

    async def send(
        self,
        site: IngestSite,
        document: Mapping[str, Any] | bytes,
        instance: PhotoInstance | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        client = (instance or self.primary).client
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        sent = (
            dict(headers)
            if headers is not None
            else self.headers(site, {} if isinstance(document, bytes) else document)
        )
        response: httpx.Response = await client.post(PATH, content=body, headers=sent)
        return response

    def post(self, site: IngestSite, document: Mapping[str, Any] | bytes, **options: Any) -> Any:
        return self.run(self.send(site, document, **options))

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def rows(self, organization_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.fetch(
                "SELECT photo_id, plant_id, zone_id, node_id, camera_id, frame_at, width, height,"
                " size_bytes, sha256, blur_params_version, image, image_absent_reason,"
                " canonical_sha256, platform_record_id, received_at, expires_at, started_at,"
                " ended_at FROM fleet.episode_photo WHERE organization_id = $1"
                " ORDER BY received_at, photo_id",
                organization_id,
            )
        ]


def metric_points(reader: InMemoryMetricReader) -> list[tuple[dict[str, Any], float]]:
    """``(atributos, valor)`` de cada punto de ``episode_photo_received_total``."""
    data = reader.get_metrics_data()
    points: list[tuple[dict[str, Any], float]] = []
    if data is None:
        return points
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == MetricName.EPISODE_PHOTO_RECEIVED_TOTAL.value:
                    points.extend(
                        (dict(point.attributes or {}), float(point.value))
                        for point in metric.data.data_points
                        if isinstance(point, NumberDataPoint)
                    )
    return points
