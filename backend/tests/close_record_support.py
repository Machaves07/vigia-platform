"""Entorno de las pruebas del cierre del acta y de la reejecución (TASK-216, LC-GOB-08 y 09).

Sobre ``walk_test_world`` (servicios reales como ``vigia_app``, montaje con un acta de alcance
real, nodo asignado) añade ``CloseRecordService``, ``ExposureService``,
``RegressionRerunService``, ``RegressionService`` y ``OcclusionService`` reales, con el **mismo
reloj** que las sesiones (sus marcas y las de los clips se comparan con él), y la aplicación real
con todas esas rutas en ``app.state``.

Lo que el cierre lee de otras tareas se siembra por SQL, como pide TASK-216 («las pruebas crean el
``VerificationClip`` y su concesión por repositorio»): pases, pruebas de oclusión resueltas, clips
de verificación con su concesión, inventario de cámaras y regresiones. ``ClipStore`` es el doble
del depósito: responde ``head_object`` con los metadatos que se le den, cuenta las llamadas,
puede caerse (``StorageUnavailable``) y **cuenta las de ``get_object``**, que nunca debe haber.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx

from tests.agreements_support import CONCESSION_HEADER, SAME_ORIGIN
from tests.api_support import World
from tests.authz_support import Site
from tests.integration.conftest import PostgresEndpoint
from tests.walk_test_support import Mounted, WalkTestWorld, walk_test_world
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.commissioning_record_repository import (
    PostgresCommissioningRecordRepository,
)
from vigia_platform.catalog.adapters.postgres.occlusion_repository import (
    PostgresOcclusionRepository,
)
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.application.close_record import (
    Baseline,
    CloseRecordService,
    CloseRequest,
)
from vigia_platform.catalog.application.exposure import ExposureService
from vigia_platform.catalog.application.occlusion import OcclusionService
from vigia_platform.catalog.application.regression import RegressionService
from vigia_platform.catalog.application.regression_rerun import RegressionRerunService
from vigia_platform.catalog.domain.commissioning_record import CommissioningRecord
from vigia_platform.catalog.domain.walk_test import WalkTestSession
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.fleet.adapters.postgres.commissioning_queries import (
    PostgresCommissioningQueries,
)
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.fleet.record_types import FLEET_RECORD_TYPES
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import Role, ScopeContext, ScopeLevel
from vigia_platform.shared.storage import (
    ChecksumType,
    ObjectHead,
    PresignedRequest,
    StorageUnavailable,
)

__all__ = [
    "ACCEPTANCE",
    "ClipStore",
    "CloseWorld",
    "Zone",
    "close_world",
]

ACCEPTANCE: Final = "Las falsas alarmas se deben al reflejo del portón y se aceptan"
DECLARED: Final = "El nodo no envió eventos mientras se tapaba la cámara"
CLOSE_TYPES: Final = (
    "walk_test_result",
    "walk_test_regression_marked",
    "walk_test_regression_cleared",
    "occlusion_test_result",
)
EXTRA_TYPES: Final = (
    # Las dos versiones de walk_test_result, en orden (la 2 amplía la 1).
    *(d for d in CATALOG_RECORD_TYPES if d.record_type in CLOSE_TYPES),
    *(d for d in FLEET_RECORD_TYPES if d.record_type == "observability_event_received"),
)


# --- Doble del depósito --------------------------------------------------------------------------


@dataclass
class ClipStore:
    """``vigia-evidence`` para los clips: ``head_object`` con los metadatos sembrados.

    ``get_object`` y ``presign_put`` no deben llamarse nunca desde el cierre: se cuentan.
    """

    objects: dict[str, tuple[bytes, Mapping[str, str]]] = field(default_factory=dict)
    heads: int = 0
    gets: int = 0
    down: bool = False

    def put(self, key: str, data: bytes, anonymized: str | None = "1") -> None:
        metadata = {} if anonymized is None else {"vigia-anonymized": anonymized}
        self.objects[key] = (data, metadata)

    async def head_object(self, key: str) -> ObjectHead | None:
        self.heads += 1
        if self.down:
            raise StorageUnavailable("head_object")
        stored = self.objects.get(key)
        if stored is None:
            return None
        data, metadata = stored
        return ObjectHead(
            key=key,
            size_bytes=len(data),
            checksum_sha256=base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type="video/mp4",
            metadata=dict(metadata),
            version_id="v-sintetica-1",
        )

    async def get_object(self, key: str) -> bytes:
        self.gets += 1
        raise AssertionError("el cierre nunca descarga un clip (PAT-GOB-REN-05)")

    async def presign_put(self, *args: Any, **kwargs: Any) -> PresignedRequest:
        raise AssertionError("el cierre no firma subidas")


# --- Zona lista para cerrar ----------------------------------------------------------------------


@dataclass
class Zone:
    """Una zona montada con nodo, su catálogo, sus cámaras y una sesión abierta."""

    mounted: Mounted
    session: WalkTestSession
    node: uuid.UUID
    cameras: tuple[uuid.UUID, ...]
    signer: uuid.UUID

    @property
    def site(self) -> Site:
        return self.mounted.site

    @property
    def zone_id(self) -> uuid.UUID:
        return self.mounted.zone

    @property
    def installer(self) -> ScopeContext:
        return self.mounted.installer


class CloseWorld:
    def __init__(self, walk: WalkTestWorld) -> None:
        self.walk = walk
        g = walk.a.g
        self.clock = g.authz.sessions.clock
        self.store = ClipStore()
        self.reader = LectorExpediente(database=g.database, audit=g.authz.sessions.audit)
        self.occlusions = OcclusionService(
            repository=PostgresOcclusionRepository(),
            sessions=PostgresWalkTestRepository(),
            catalog=PostgresCatalogRepository(g.database),
            gates=g.gates,
            reader=self.reader,
            database=g.database,
            writer=g.writer,
            free_text=g.free_text,
            clock=self.clock,
        )
        walk.service = walk.build(occlusions=self.occlusions)
        self.marker = RegressionService(
            repository=PostgresRegressionRepository(g.database),
            catalog=PostgresCatalogRepository(g.database),
            database=g.database,
            writer=g.writer,
            authorizer=g.authz.authorizer,
            audit=g.authz.sessions.audit,
            free_text=g.free_text,
            clock=self.clock,
        )
        self.records = PostgresCommissioningRecordRepository()
        self.service = self.build()
        self.exposures = self.build_exposures()
        self.reruns = self.build_reruns()
        self.client = self._install()

    # --- Servicios -----------------------------------------------------------------------------

    def build(self, **changes: Any) -> CloseRecordService:
        g = self.walk.a.g
        fields: dict[str, Any] = {
            "repository": self.records,
            "sessions": PostgresWalkTestRepository(),
            "occlusion_tests": PostgresOcclusionRepository(),
            "occlusions": self.occlusions,
            "regressions": PostgresRegressionRepository(g.database),
            "catalog": PostgresCatalogRepository(g.database),
            "fleet": PostgresCommissioningQueries(),
            "clips": ClipObjectStore(self.store),  # type: ignore[arg-type]
            "gates": g.gates,
            "identity": g.hierarchy,
            "database": g.database,
            "writer": g.writer,
            "audit": g.authz.sessions.audit,
            "free_text": g.free_text,
            "clock": self.clock,
        }
        fields.update(changes)
        return CloseRecordService(**fields)

    def build_exposures(self, **changes: Any) -> ExposureService:
        g = self.walk.a.g
        fields: dict[str, Any] = {
            "repository": self.records,
            "sessions": PostgresWalkTestRepository(),
            "gates": g.gates,
            "database": g.database,
            "clock": self.clock,
        }
        fields.update(changes)
        return ExposureService(**fields)

    def build_reruns(self, walk_tests: Any = None) -> RegressionRerunService:
        g = self.walk.a.g
        return RegressionRerunService(
            walk_tests=walk_tests or self.walk.service,
            regressions=PostgresRegressionRepository(g.database),
            database=g.database,
        )

    def client_with(self, **services: Any) -> httpx.AsyncClient:
        """Otra aplicación real con los mismos servicios más ``services`` en ``CatalogHttp``
        (p. ej. ``record_documents``, TASK-217); quien la pide la cierra."""
        return self._install(**services)

    def _install(self, **services: Any) -> httpx.AsyncClient:
        g = self.walk.a.g
        authz = g.authz
        catalog: dict[str, Any] = {
            "gates": g.gates,
            "regression": self.marker,
            "walk_tests": self.walk.service,
            "occlusions": self.occlusions,
            "records": self.service,
            "exposures": self.exposures,
            "regression_reruns": self.reruns,
        }
        catalog.update(services)
        app = World(clock=authz.sessions.clock).app(
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(g.writer),
                    clock=authz.sessions.clock,
                ),
                "state": {CATALOG_STATE_KEY: CatalogHttp(**catalog)},
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

    def execute(self, sql: str, *args: Any) -> None:
        self.walk.execute(sql, *args)

    def advance(self, seconds: float = 1.0) -> None:
        self.walk.advance(seconds)

    def request(
        self,
        method: str,
        path: str,
        mounted: Mounted,
        body: Any = None,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        self.advance()
        headers = {
            **SAME_ORIGIN,
            "Cookie": f"{SESSION_COOKIE_NAME}={mounted.cookie.value}",
            CONCESSION_HEADER: str(mounted.concession),
        }
        response: httpx.Response = self.run(
            (client or self.client).request(method, path, json=body, headers=headers)
        )
        return response

    # --- Zonas ---------------------------------------------------------------------------------

    def catalog_of(self, zone_id: uuid.UUID) -> dict[str, Any]:
        (row,) = self.fetch(
            "SELECT payload::text AS payload FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 AND superseded_at IS NULL",
            zone_id,
        )
        payload: dict[str, Any] = json.loads(row["payload"])
        return payload

    def node_of(self, zone_id: uuid.UUID) -> uuid.UUID:
        (row,) = self.fetch(
            "SELECT node_id FROM identity.zone_node_assignment"
            " WHERE zone_id = $1 AND unassigned_at IS NULL",
            zone_id,
        )
        return uuid.UUID(str(row["node_id"]))

    def zone(
        self,
        *,
        standards: int = 2,
        passes_per_cell: int = 3,
        site: Site | None = None,
        index: int = 0,
        plant_concession: bool = False,
    ) -> Zone:
        """Zona montada (catálogo con ``standards`` estándares y dos cámaras, nodo, montaje
        aprobado), un firmante con rol sobre ella y la sesión ``initial`` abierta. Con
        ``plant_concession``, el instalador solo tiene concesión de la planta de la zona."""
        site = site or self.walk.a.g.site()
        plant = site.zones()[index][0]
        mounted = self.walk.mounted(
            count=standards,
            site=site,
            zone_index=index,
            installer_level=ScopeLevel.PLANT if plant_concession else ScopeLevel.ORGANIZATION,
            installer_scope=plant if plant_concession else None,
        )
        catalog = self.catalog_of(mounted.zone)
        cameras = tuple(uuid.UUID(str(c["camera_id"])) for c in catalog["cameras"])
        signer = self.walk.a.signer(mounted.site, Role.COORDINATOR_SST)
        session = self.walk.open(mounted, passes_per_cell)
        return Zone(mounted, session, self.node_of(mounted.zone), cameras, signer.user_id)

    def reopen_rerun(self, zone: Zone, passes_per_cell: int = 3) -> WalkTestSession:
        """La reejecución de la zona por el servicio (la regresión tiene que estar pending)."""
        self.advance()
        session: WalkTestSession = self.run(
            self.reruns.open(zone.installer, zone.zone_id, passes_per_cell)
        )
        zone.session = session
        return session

    # --- Siembras por SQL ----------------------------------------------------------------------

    def passes(
        self,
        zone: Zone,
        *,
        result: str = "detected",
        per_row: int | None = None,
        rows: Sequence[uuid.UUID] | None = None,
        evidence: uuid.UUID | None = None,
    ) -> list[uuid.UUID]:
        """``per_row`` pases (por defecto, los que exige la fila) de cada fila de la sesión."""
        session = zone.session
        recorded: list[uuid.UUID] = []
        moment = session.started_at
        targets = rows if rows is not None else [row.row_id for row in session.matrix_rows]
        required = {row.row_id: row.required_passes for row in session.matrix_rows}
        for row_id in targets:
            for _ in range(required[row_id] if per_row is None else per_row):
                pass_id = uuid.uuid4()
                moment += timedelta(milliseconds=1)
                self.execute(
                    "INSERT INTO catalog.walk_test_pass (pass_id, organization_id, plant_id,"
                    " session_id, row_id, result, evidence_ref, recorded_by, recorded_at)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                    pass_id,
                    session.organization_id,
                    session.plant_id,
                    session.session_id,
                    row_id,
                    result,
                    evidence,
                    uuid.UUID(str(zone.installer.actor.id)),
                    moment,
                )
                recorded.append(pass_id)
        return recorded

    def occlusion(
        self,
        zone: Zone,
        camera: uuid.UUID,
        verification: str = "declared",
        test_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Una prueba de oclusión ya resuelta de la cámara (su registro es de TASK-215)."""
        test = test_id or uuid.uuid4()
        session = zone.session
        ended = session.started_at + timedelta(seconds=30)
        self.execute(
            "INSERT INTO catalog.occlusion_test (test_id, organization_id, plant_id, session_id,"
            " camera_id, started_at, ended_at, deadline, verification, correlated_event_ids,"
            " declared_reason_es, recorded_by, ledger_record_id)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $7::timestamptz + interval '5 minutes', $8, $9,"
            " $10, $11, $12)",
            test,
            session.organization_id,
            session.plant_id,
            session.session_id,
            camera,
            ended - timedelta(seconds=20),
            ended,
            verification,
            [uuid.uuid4()] if verification == "verified" else None,
            DECLARED if verification == "declared" else None,
            uuid.UUID(str(zone.installer.actor.id)),
            uuid.uuid4(),
        )
        return test

    def occlusions_ok(self, zone: Zone) -> None:
        for camera in zone.cameras:
            self.occlusion(zone, camera)

    def clips(
        self,
        zone: Zone,
        count: int,
        *,
        anonymized: str | None = "1",
        stored: bool = True,
        served: bool = False,
        received_at: datetime | None = None,
        upload_ms: int = 400,
        zone_id: uuid.UUID | None = None,
        node: uuid.UUID | None = None,
    ) -> list[uuid.UUID]:
        """``count`` clips de verificación de la zona (o de ``zone_id``) con su concesión,
        recibidos en la ventana de la sesión; ``stored`` los deja en el depósito con el
        metadato ``anonymized``."""
        session = zone.session
        target = zone_id or zone.zone_id
        node_id = node or (zone.node if zone_id is None else self.node_of(target))
        start = received_at or session.started_at + timedelta(milliseconds=10)
        created: list[uuid.UUID] = []
        for index in range(count):
            clip_id = uuid.uuid4()
            data = f"clip sintético {clip_id}".encode()
            sha256 = hashlib.sha256(data).hexdigest()
            key = (
                f"org/{session.organization_id}/plant/{session.plant_id}/zone/{target}"
                f"/node/{node_id}/{clip_id}.mp4"
            )
            received = start + timedelta(milliseconds=index)
            issued = received - timedelta(milliseconds=upload_ms)
            self.execute(
                "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id,"
                " zone_id, node_id, purpose, storage_key, content_type, max_size_bytes,"
                " required_headers, issued_at, expires_at, status, used_at)"
                " VALUES ($1, $2, $3, $4, $5, 'verification', $6, 'video/mp4', $7, $8, $9,"
                " $9::timestamptz + interval '15 minutes', 'used', $10)",
                clip_id,
                session.organization_id,
                session.plant_id,
                target,
                node_id,
                key,
                len(data),
                json.dumps(
                    {
                        "x-amz-checksum-sha256": base64.b64encode(
                            hashlib.sha256(data).digest()
                        ).decode("ascii"),
                        "x-amz-meta-vigia-anonymized": "1",
                    }
                ),
                issued,
                received,
            )
            self.execute(
                "INSERT INTO fleet.verification_clip (clip_id, organization_id, plant_id,"
                " zone_id, node_id, received_at, sha256, first_served_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                clip_id,
                session.organization_id,
                session.plant_id,
                target,
                node_id,
                received,
                sha256,
                received + timedelta(milliseconds=150) if served else None,
            )
            if stored:
                self.store.put(key, data, anonymized)
            created.append(clip_id)
        return created

    def inventory(self, zone: Zone, camera: uuid.UUID, measured: float, declared: float) -> None:
        """La cámara tal como la dejaría el último latido aceptado (TASK-223)."""
        self.execute(
            "INSERT INTO fleet.camera_inventory (organization_id, plant_id, node_id, camera_id,"
            " connected, measured_fps, declared_min_fps, observability_state, updated_at)"
            " VALUES ($1, $2, $3, $4, true, $5, $6, 'observable', $7)",
            zone.session.organization_id,
            zone.session.plant_id,
            zone.node,
            camera,
            measured,
            declared,
            zone.session.started_at,
        )

    def regression(
        self,
        zone: Zone,
        rows: Sequence[uuid.UUID] | str = "all",
        cause: str = "framing_recaptured",
    ) -> uuid.UUID:
        """La regresión ``pending`` de la zona (fila sintética; la marca real es de TASK-209)."""
        record_id = uuid.uuid4()
        affected = json.dumps("all" if rows == "all" else [str(row) for row in rows])
        self.execute(
            "INSERT INTO catalog.walk_test_regression (zone_id, organization_id, plant_id, state,"
            " marked_at, cause, catalog_version, affected_row_ids, ledger_record_id)"
            " VALUES ($1, $2, $3, 'pending', $4, $5, $6, CAST($7 AS jsonb), $8)"
            " ON CONFLICT (zone_id) DO UPDATE SET state = 'pending',"
            " marked_at = EXCLUDED.marked_at,"
            " cause = EXCLUDED.cause, catalog_version = EXCLUDED.catalog_version,"
            " affected_row_ids = EXCLUDED.affected_row_ids,"
            " ledger_record_id = EXCLUDED.ledger_record_id, cleared_at = NULL,"
            " cleared_by_session_id = NULL",
            zone.zone_id,
            zone.site.organization_id,
            zone.mounted.plant,
            self.clock.now(),
            cause,
            1 if cause == "catalog_change" else None,
            affected,
            record_id,
        )
        return record_id

    def ready(self, zone: Zone, *, clips: int = 80) -> None:
        """Todo lo que el cierre exige: pases de cada fila, oclusiones declaradas y clips."""
        self.passes(zone)
        self.occlusions_ok(zone)
        self.clips(zone, clips)
        self.advance()

    # --- Cierre --------------------------------------------------------------------------------

    def body(
        self,
        zone: Zone,
        *,
        acceptance: str | None = None,
        signers: Sequence[uuid.UUID] | None = None,
        beacon: int = 180,
        baselines: bool = True,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            "signatures": [{"user_id": str(user)} for user in (signers or [zone.signer])],
            "installer_measurements": {
                "beacon_latency_ms_p95": beacon,
                "baselines": [
                    {
                        "camera_id": str(camera),
                        "zone_id": str(zone.zone_id),
                        "captured_at": "2026-10-01T08:00:00.000Z",
                    }
                    for camera in zone.cameras
                ]
                if baselines
                else [],
            },
        }
        if acceptance is not None:
            document["false_alarm_acceptance"] = {"reason_es": acceptance}
        return document

    def request_of(self, zone: Zone, **changes: Any) -> CloseRequest:
        document = self.body(zone, **changes)
        measurements = document["installer_measurements"]
        return CloseRequest(
            signatures=[uuid.UUID(s["user_id"]) for s in document["signatures"]],
            beacon_latency_ms_p95=measurements["beacon_latency_ms_p95"],
            baselines=[
                Baseline(
                    uuid.UUID(b["camera_id"]),
                    uuid.UUID(b["zone_id"]),
                    datetime.fromisoformat(b["captured_at"]),
                )
                for b in measurements["baselines"]
            ],
            false_alarm_reason_es=(document.get("false_alarm_acceptance") or {}).get("reason_es"),
        )

    def close(
        self, zone: Zone, service: CloseRecordService | None = None, **changes: Any
    ) -> CommissioningRecord:
        self.advance()
        record: CommissioningRecord = self.run(
            (service or self.service).close(
                zone.installer, zone.session.session_id, self.request_of(zone, **changes)
            )
        )
        return record

    def post_close(self, zone: Zone, **changes: Any) -> httpx.Response:
        return self.request(
            "POST",
            f"/walk-tests/{zone.session.session_id}/close",
            zone.mounted,
            self.body(zone, **changes),
        )

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def written(self, zone: Zone) -> tuple[Any, ...]:
        """Actas, registros, eventos, estado de la sesión, regresión y difuminados de la zona."""
        records = self.fetch(
            "SELECT count(*) AS n FROM catalog.commissioning_record WHERE zone_id = $1",
            zone.zone_id,
        )[0]["n"]
        ledger = [
            r["record_type"]
            for r in self.walk.a.g.records_of(zone.zone_id)
            if r["record_type"] in ("walk_test_result", "walk_test_regression_cleared")
        ]
        events = [r["event_name"] for r in self.walk.a.g.events(zone.mounted.plant, zone.zone_id)]
        (session,) = self.fetch(
            "SELECT status, closed_at, commissioning_record_id FROM catalog.walk_test_session"
            " WHERE session_id = $1",
            zone.session.session_id,
        )
        regression = self.fetch(
            "SELECT state, ledger_record_id, cleared_by_session_id"
            " FROM catalog.walk_test_regression WHERE zone_id = $1",
            zone.zone_id,
        )
        blurred = self.fetch(
            "SELECT count(*) AS n FROM fleet.verification_clip"
            " WHERE zone_id = $1 AND blur_check_result IS NOT NULL",
            zone.zone_id,
        )[0]["n"]
        return (
            records,
            ledger,
            events,
            tuple(session),
            [tuple(r) for r in regression],
            blurred,
        )

    def record_row(self, zone: Zone) -> Any:
        (row,) = self.fetch(
            "SELECT commissioning_record_id, false_alarm_rate_observed, false_alarm_threshold,"
            " false_alarm_acceptance::text AS acceptance, latency::text AS latency,"
            " cameras_measured::text AS cameras, occlusion_summary::text AS occlusions,"
            " matrix_results::text AS matrix, installer_measurements::text AS installer,"
            " signatures::text AS signatures, total_hours, ledger_record_id"
            " FROM catalog.commissioning_record WHERE session_id = $1",
            zone.session.session_id,
        )
        return row

    def results(self, zone: Zone, record_type: str = "walk_test_result") -> list[dict[str, Any]]:
        return [
            {**dict(r), "content": json.loads(r["content"])}
            for r in self.walk.a.g.records_of(zone.zone_id)
            if r["record_type"] == record_type
        ]

    def events(self, zone: Zone, name: str) -> list[dict[str, Any]]:
        return [
            json.loads(r["payload"])
            for r in self.walk.a.g.events(zone.mounted.plant, zone.zone_id)
            if r["event_name"] == name
        ]


@contextmanager
def close_world(endpoint: PostgresEndpoint, prefix: str) -> Iterator[CloseWorld]:
    """El entorno de ``CloseWorld`` sobre una base migrada propia, con su cliente HTTP."""
    with walk_test_world(endpoint, prefix, EXTRA_TYPES) as walk:
        world = CloseWorld(walk)
        try:
            yield world
        finally:
            walk.run(world.client.aclose())
