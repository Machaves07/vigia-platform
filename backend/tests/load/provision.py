"""Plataforma y flota de los perfiles de carga (LC-GOB-22; TASK-231).

**Plataforma**: la del arnés de conformidad de TASK-230 (``tests.conformance.platform_target``:
PostgreSQL y LocalStack de testcontainers, ``vigia-admin bootstrap``, la raíz de composición de
producción en el proceso de la prueba con métricas en memoria y los balanceadores ``app.`` y
``nodes.``). Una diferencia: ``LoadApi`` cree la cabecera ``X-Forwarded-For`` que pone el
balanceador local (``forwarded_allow_ips`` de ``127.0.0.1``), como ``vigia-api`` detrás del
balanceador real. Sin ella todas las altas llegarían del mismo origen y el límite por origen
(5 cada 15 minutos, 20 al día; ``node_api.limits``) frenaría la flota en el sexto nodo.

**Flota** (``FleetProvisioner``), por los servicios reales del ``AppRuntime`` con las personas
que lo harían, como ``Provisioner`` de TASK-230: una organización cliente con su administradora y
un instalador del proveedor con concesión; ``plants`` plantas con sus zonas, la familia admitida
en cada planta, ``nodes`` nodos declarados con ``zones_per_node`` zonas cada uno, el catálogo de
cada zona publicado y sus dos compuertas aprobadas. Cada nodo se da de alta con su código por la
ruta del contrato ``POST /api/nodes/enrollment`` detrás de ``app.``, con la credencial del cliente
del nodo de U-01 (``NodeCredentials.enroll``) guardada en un ``FileCredentialStore`` del directorio
temporal (fuera del árbol, ``0600``). El código de alta solo vive en memoria (PR-GOB-31).

``fleet.json`` describe la flota para el proceso de los nodos (``tests.load.driver``): sin
códigos de alta ni claves, solo las rutas de las credenciales y las configuraciones iniciales.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import itertools
import json
import logging
import os
import secrets
import ssl
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
from opentelemetry.sdk.metrics import MeterProvider
from vigia_contracts.credentials import FileCredentialStore, NodeCredentials, NodeIdentity
from vigia_contracts.models.enumerations import GateStatus, PredicateFamily
from vigia_contracts.models.node_enrollment import NodeEnrollmentRequest
from vigia_contracts.models.tolerant.node_enrollment import NodeEnrollmentResponse
from vigia_contracts.models.tolerant.rejection_response import RejectionResponse
from vigia_contracts.transport import Client as TransportClient
from vigia_contracts.transport.api.credentials import post_enrollment
from vigia_contracts.transport.types import Response as TransportResponse
from vigia_contracts.versioning import CONTRACT_VERSION

from tests.conformance.platform_target import (
    _AWS_VARIABLES,
    ASSIGNED_SINCE,
    INGEST_PATH,
    REASON,
    SUITE_LEAD,
    WALL,
    InProcessApi,
    Provisioner,
    _ceil_ms,
    _new_standard,
    _Site,
    _stamp,
    wait_ready,
)
from tests.factories import uuid7
from tests.load.profiles import LoadProfile
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.application.admission import AdmissionRequest
from vigia_platform.catalog.domain.admission import AdmissionAnswers
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY, FleetHttp
from vigia_platform.identity.application.hierarchy import PlantSpec, ZoneSpec
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.shared.api.app import AppConfig, create_app
from vigia_platform.shared.api.main import ApiServerConfig, build_server
from vigia_platform.shared.context import ActorUnit, ScopeContext
from vigia_platform.shared.observability.logging import JsonFormatter
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig

__all__ = [
    "ConsoleSession",
    "Fleet",
    "FleetNode",
    "FleetProvisioner",
    "LoadApi",
    "load_api",
]

FLEET_FILE: Final = "fleet.json"
LOCAL_BALANCER: Final = ("127.0.0.1/32",)
"""El balanceador local de ``tests.conformance.mtls_proxy``: el único cuyo ``X-Forwarded-For``
cree ``LoadApi``."""
HTTP_SECONDS: Final = 60.0
LIVE_VIEW_HOST: Final = "10.0.0.5"
SOFTWARE_VERSION: Final = "1.0.0"
_ORIGINS: Final = itertools.count(1)
"""Un origen de red distinto por alta en toda la sesión (``10.x.y.z``): cada nodo es una máquina."""


# --- Plataforma -----------------------------------------------------------------------------------


@dataclass
class LoadApi(InProcessApi):
    """``InProcessApi`` que cree el ``X-Forwarded-For`` del balanceador local (ver el módulo)."""

    def start(self, port: int) -> None:
        self.port = port
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="vigia-api-carga", daemon=True
        )
        self._thread.start()
        provider = MeterProvider(metric_readers=[self.reader])
        metrics = PlatformMetrics(provider.get_meter("vigia_platform.carga"))
        config = AppConfig.from_environ(self.environ)
        runtime_config = RuntimeConfig.from_environ(self.environ)
        server = ApiServerConfig(host="127.0.0.1", port=port, forwarded_allow_ips=LOCAL_BALANCER)

        async def compose() -> None:
            self.runtime = await compose_api_runtime(config, runtime_config, metrics=metrics)
            self._server = build_server(create_app(config, runtime=self.runtime), server)

        self.call(compose())
        self._serving = asyncio.run_coroutine_threadsafe(self._server.serve(), self._loop)
        wait_ready(self.url, alive=lambda: not self._serving.done())


@contextlib.contextmanager
def load_api(environ: Mapping[str, str], port: int, *, log_file: Path) -> Iterator[LoadApi]:
    """``LoadApi`` con las credenciales de LocalStack en el entorno del proceso durante su vida y
    sus registros (JSON con la redacción de la plataforma) en ``log_file``."""
    saved = {name: os.environ.get(name) for name in _AWS_VARIABLES}
    for name in _AWS_VARIABLES:
        os.environ[name] = environ[name]
    logger = logging.getLogger("vigia")
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    api = LoadApi(environ)
    try:
        api.start(port)
        yield api
    finally:
        api.stop()
        logger.removeHandler(handler)
        handler.close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# --- Flota ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetNode:
    index: int
    node_id: uuid.UUID
    plant_id: uuid.UUID
    zone_ids: tuple[uuid.UUID, ...]
    credential_file: Path
    configuration_file: Path
    catalogs: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ConsoleSession:
    """La sesión del usuario de consola (el instalador del proveedor con su concesión)."""

    cookie_value: str = field(repr=False)
    """El valor de ``__Host-vigia_session``: un secreto, nunca en ``repr`` ni en un informe."""
    concession_id: uuid.UUID


@dataclass(frozen=True)
class Fleet:
    organization_id: uuid.UUID
    plant_ids: tuple[uuid.UUID, ...]
    nodes: tuple[FleetNode, ...]
    fleet_file: Path
    ready_at: dt.datetime
    console: ConsoleSession
    enrollment_codes: tuple[str, ...] = field(repr=False, default=())
    """Los códigos de alta usados, solo en memoria: ninguna salida los contiene (PR-GOB-31)."""
    record_ids: tuple[uuid.UUID, ...] = ()
    """Un acta de comisionamiento cerrada por planta, la que lee el cliente de consola."""

    @property
    def zone_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(zone for node in self.nodes for zone in node.zone_ids)

    def plant_of(self) -> dict[str, str]:
        """``node_id`` → ``plant_id`` (texto en minúsculas)."""
        return {str(node.node_id): str(node.plant_id) for node in self.nodes}

    def leaks(self, *texts: str) -> list[str]:
        """Qué códigos de alta aparecen en ``texts`` (vacío si ninguno)."""
        return [code for code in self.enrollment_codes if any(code in text for text in texts)]

    def wait_until_ready(self) -> None:
        delay = (self.ready_at - WALL.now()).total_seconds()
        if delay > 0:
            time.sleep(delay)


class _Enrollment:
    """``CredentialTransport`` síncrono de ``NodeCredentials.enroll``: ``POST /enrollment`` en el
    balanceador ``app.`` con el ``X-Forwarded-For`` del origen del nodo (lo pone el balanceador
    real). Guarda el cuerpo de la respuesta: de él sale la configuración inicial."""

    def __init__(self, base_url: str, verify: Path, origin: str) -> None:
        self._base_url = base_url
        self._verify = verify
        self._origin = origin
        self.body: bytes = b""

    def post_enrollment(
        self, body: NodeEnrollmentRequest
    ) -> TransportResponse[NodeEnrollmentResponse | RejectionResponse]:
        context = ssl.create_default_context(cafile=str(self._verify))
        with httpx.Client(
            base_url=self._base_url,
            verify=context,
            headers={"X-Forwarded-For": self._origin},
            timeout=HTTP_SECONDS,
        ) as http:
            client = TransportClient(self._base_url, self._base_url)
            client.set_enrollment_httpx_client(http)
            response = post_enrollment.sync_detailed(
                client=client, body=body, x_vigia_contract_version=str(CONTRACT_VERSION)
            )
        self.body = response.content
        return response

    def post_credential_rotation(self, body: Any) -> Any:
        raise NotImplementedError("la flota de carga solo se da de alta")


def _origin() -> str:
    number = next(_ORIGINS)
    return f"10.{(number >> 16) & 0xFF}.{(number >> 8) & 0xFF}.{number & 0xFF}"


@dataclass
class FleetProvisioner(Provisioner):
    """Aprovisiona una flota de carga por los servicios reales (ver el módulo)."""

    nodes_url: str = ""
    """Base del balanceador ``nodes.``: la ``ingest_base_url`` de los nodos."""

    def fleet(self, profile: LoadProfile) -> Fleet:
        site = self._site(())
        plants = self._hierarchy(site, profile)
        nodes = self._declare_nodes(site, profile, plants)
        self._assigned_before(nodes)
        since = WALL.now()
        catalogs: dict[uuid.UUID, Mapping[str, Any]] = {}
        for zone in (zone for _, _, zones in nodes for zone in zones):
            catalog, in_force = self._publish_zone(site, zone)
            catalogs[zone] = catalog
            since = max(since, in_force)
        directory = self.directory / f"flota-{profile.name}-{secrets.token_hex(4)}"
        directory.mkdir(mode=0o700)
        fleet_nodes: list[FleetNode] = []
        codes: list[str] = []
        for index, (node_id, plant_id, zones) in enumerate(nodes):
            credential, configuration, code = self._enroll_node(site, directory, node_id, plant_id)
            codes.append(code)
            fleet_nodes.append(
                FleetNode(
                    index,
                    node_id,
                    plant_id,
                    zones,
                    credential,
                    configuration,
                    tuple(catalogs[zone] for zone in zones),
                )
            )
        fleet_file = directory / "fleet.json"
        fleet_file.write_text(
            json.dumps(
                {
                    "organization_id": str(site.organization_id),
                    "ingest_base_url": self.nodes_url + INGEST_PATH,
                    "verify": str(self.verify_file),
                    "nodes": [
                        {
                            "index": node.index,
                            "node_id": str(node.node_id),
                            "organization_id": str(site.organization_id),
                            "plant_id": str(node.plant_id),
                            "credential": str(node.credential_file),
                            "configuration": str(node.configuration_file),
                            "catalogs": [dict(catalog) for catalog in node.catalogs],
                        }
                        for node in fleet_nodes
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return Fleet(
            site.organization_id,
            tuple(plants),
            tuple(fleet_nodes),
            fleet_file,
            _ceil_ms(since) + SUITE_LEAD,
            ConsoleSession(site.installer.value, site.concession_id),
            tuple(codes),
            self._commissioning_records(site, nodes),
        )

    # --- Pasos ---------------------------------------------------------------------------------

    def _commissioning_records(
        self, site: _Site, nodes: Sequence[tuple[uuid.UUID, uuid.UUID, tuple[uuid.UUID, ...]]]
    ) -> tuple[uuid.UUID, ...]:
        """Un acta cerrada por planta (la primera zona de su primer nodo), por SQL con la forma
        del acta, como ``tests/isolation/test_route_isolation.py``: lo que mide la consola es la
        lectura del acta estructurada (``GET /commissioning-records/{id}``), no su cierre, que
        tiene sus pruebas (``tests/integration/test_catalog_close_record.py``)."""
        now = WALL.now()
        latency = {
            "node_tranche": {
                "median_ms": None,
                "p95_ms": 180,
                "max_ms": None,
                "repetitions": None,
                "measured_by": "installer",
            },
            "platform_tranche": None,
            "exposure_tranche": None,
            "served_tranche": None,
            "not_measured": ["platform_tranche", "exposure_tranche", "served_tranche"],
            "indicative_sum_median_ms": None,
            "indicative_sum_p95_ms": 180,
            "indicative": True,
            "repetitions_counted": 100,
        }
        signature = {
            "user_id": str(uuid.uuid4()),
            "role_in_use": "coordinator_sst",
            "signed_at": _stamp(now),
        }
        firsts: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
        for node_id, plant_id, zones in nodes:
            firsts.setdefault(plant_id, (node_id, zones[0]))
        records: list[uuid.UUID] = []
        statements: list[tuple[str, Sequence[Any]]] = []
        for plant_id, (node_id, zone_id) in firsts.items():
            session_id, record_id = uuid7(), uuid7()
            records.append(record_id)
            statements += [
                (
                    "INSERT INTO catalog.walk_test_session (session_id, organization_id, plant_id,"
                    " zone_id, node_id, catalog_version, kind, status, passes_per_cell,"
                    " matrix_rows, started_at, last_activity_at, closed_at,"
                    " commissioning_record_id) VALUES ($1, $2, $3, $4, $5, 1, 'initial', 'closed',"
                    " 3, '[]', $6, $6, $6, $7)",
                    (session_id, site.organization_id, plant_id, zone_id, node_id, now, record_id),
                ),
                (
                    "INSERT INTO catalog.commissioning_record (commissioning_record_id,"
                    " organization_id, plant_id, zone_id, session_id, catalog_version,"
                    " matrix_results, false_negatives_total, false_alarm_rate_observed,"
                    " false_alarm_threshold, latency, installer_measurements, cameras_measured,"
                    " occlusion_summary, total_hours, steps_summary, signatures, closed_at,"
                    " ledger_record_id) VALUES ($1, $2, $3, $4, $5, 1, '[]', 0, 0, 0, $6, $7,"
                    " '[]', '[]', 0, '[]', $8, $9, $10)",
                    (
                        record_id,
                        site.organization_id,
                        plant_id,
                        zone_id,
                        session_id,
                        json.dumps(latency),
                        json.dumps({"beacon_latency_ms_p95": 180, "baselines": []}),
                        json.dumps([signature]),
                        now,
                        uuid7(),
                    ),
                ),
            ]
        self.stack.admin(statements)
        return tuple(records)

    def _hierarchy(self, site: _Site, profile: LoadProfile) -> list[uuid.UUID]:
        """Las plantas (administradora) con la familia de coexistencia admitida en cada una."""
        assert self.api.runtime is not None and self.api.runtime.identity is not None
        hierarchy = self.api.runtime.identity.hierarchy
        assert hierarchy is not None
        catalog: CatalogHttp = self.api.state(CATALOG_STATE_KEY)

        async def go() -> list[uuid.UUID]:
            plants: list[uuid.UUID] = []
            for _ in range(profile.plants):
                administrator = await self._context(site.administrator, None)
                plant = await hierarchy.create_plant(
                    administrator,
                    PlantSpec(
                        code=f"PL-{secrets.token_hex(4).upper()}",
                        name="Planta sintética de carga",
                        country="CO",
                        data_region="us-east-1",
                        timezone="America/Bogota",
                    ),
                )
                administrator = await self._context(site.administrator, None)
                await catalog.admissions.evaluate(
                    administrator,
                    plant.plant_id,
                    AdmissionRequest(
                        family=PredicateFamily.COEXISTENCE,
                        answers=AdmissionAnswers(standard=True, remedy=True, subject=True),
                    ),
                )
                plants.append(plant.plant_id)
            return plants

        return self.api.call(go(), timeout=600.0)

    def _declare_nodes(
        self, site: _Site, profile: LoadProfile, plants: Sequence[uuid.UUID]
    ) -> list[tuple[uuid.UUID, uuid.UUID, tuple[uuid.UUID, ...]]]:
        """Zonas (administradora) y nodos declarados con sus zonas (instalador con concesión)."""
        assert self.api.runtime is not None and self.api.runtime.identity is not None
        hierarchy = self.api.runtime.identity.hierarchy
        assert hierarchy is not None
        fleet: FleetHttp = self.api.state(FLEET_STATE_KEY)

        async def go() -> list[tuple[uuid.UUID, uuid.UUID, tuple[uuid.UUID, ...]]]:
            declared = []
            for index in range(profile.nodes):
                plant = plants[index // profile.nodes_per_plant]
                zones = []
                for _ in range(profile.zones_per_node):
                    administrator = await self._context(site.administrator, None)
                    zone = await hierarchy.create_zone(
                        administrator,
                        plant,
                        ZoneSpec(code=f"ZN-{secrets.token_hex(4).upper()}", name="Zona de carga"),
                    )
                    zones.append(zone.zone_id)
                installer = await self._context(site.installer, site.concession_id)
                node = await fleet.declarations.declare(
                    installer, plant, code=f"ND-{secrets.token_hex(4).upper()}", zone_ids=zones
                )
                declared.append((node.node_id, plant, tuple(zones)))
            return declared

        return self.api.call(go(), timeout=1_800.0)

    def _assigned_before(
        self, nodes: Sequence[tuple[uuid.UUID, uuid.UUID, tuple[uuid.UUID, ...]]]
    ) -> None:
        """La asignación anterior de cada zona, como ``Provisioner._previous_assignments``: el
        nodo lleva más que la retención asignado a sus zonas."""

        rows = self.stack.fetch(
            "SELECT organization_id, plant_id, zone_id, node_id, assigned_at"
            " FROM identity.zone_node_assignment WHERE node_id = ANY($1::uuid[])",
            [node for node, _, _ in nodes],
        )
        assert len(rows) == sum(len(zones) for _, _, zones in nodes)
        self.stack.admin(
            [
                (
                    "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id,"
                    " plant_id, zone_id, node_id, assigned_at, unassigned_at, assigned_by)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    (
                        uuid7(),
                        row["organization_id"],
                        row["plant_id"],
                        row["zone_id"],
                        row["node_id"],
                        row["assigned_at"] - ASSIGNED_SINCE,
                        row["assigned_at"],
                        self._operator,
                    ),
                )
                for row in rows
            ]
        )

    def _publish_zone(self, site: _Site, zone: uuid.UUID) -> tuple[Mapping[str, Any], dt.datetime]:
        """Versión 1 del catálogo (administradora) y compuertas de montaje y de uso aprobadas
        (instalador), como ``Provisioner._publish``; el catálogo y desde cuándo rige todo."""
        catalog: CatalogHttp = self.api.state(CATALOG_STATE_KEY)
        gates = catalog.gates

        async def go() -> tuple[Mapping[str, Any], dt.datetime]:
            administrator = await self._context(site.administrator, None)
            version = await catalog.catalog.publish_catalog_version(
                administrator, zone, _new_standard(zone), REASON
            )
            since = version.issued_at
            installer = await self._context(site.installer, site.concession_id)
            _, authorized = await gates.zone(installer, zone, PermissionKey.COMMISSIONING_RUN)
            writer = with_unit(authorized, ActorUnit.U03)
            for kind in (GateKind.MOUNTING, GateKind.USAGE):

                async def transition(
                    transaction: Any, kind: GateKind = kind, writer: ScopeContext = writer
                ) -> Any:
                    return await gates.transition_gate(
                        transaction, writer, zone, kind, GateStatus.APPROVED, uuid.uuid4()
                    )

                done = await gates.run(writer, transition)
                since = max(since, done.interval.effective_from)
            return dict(version.payload), since

        return self.api.call(go())

    def _enroll_node(
        self, site: _Site, directory: Path, node_id: uuid.UUID, plant_id: uuid.UUID
    ) -> tuple[Path, Path, str]:
        """Código de alta (instalador) y alta con ``NodeCredentials.enroll`` por la ruta del
        contrato; la credencial queda en su ``FileCredentialStore`` y la configuración inicial en
        un JSON. Devuelve las dos rutas y el código (solo en memoria)."""
        fleet: FleetHttp = self.api.state(FLEET_STATE_KEY)

        async def issue() -> str:
            installer = await self._context(site.installer, site.concession_id)
            issued = await fleet.enrollment_codes.issue(installer, node_id)
            code: str = issued.code
            return code

        code = self.api.call(issue())
        node_directory = directory / str(node_id)
        credential_file = node_directory / "credential.json"
        credentials = NodeCredentials(
            NodeIdentity(str(node_id), str(site.organization_id), str(plant_id)),
            FileCredentialStore(credential_file),
            WALL,
            live_view_host=LIVE_VIEW_HOST,
            software_version=SOFTWARE_VERSION,
        )
        transport = _Enrollment(self.app_url + INGEST_PATH, self.verify_file, _origin())
        credentials.enroll(code, secrets.token_hex(32), transport)
        configuration_file = node_directory / "initial-configuration.json"
        configuration = json.loads(transport.body)["initial_configuration"]
        configuration_file.write_text(json.dumps(configuration), encoding="utf-8")
        configuration_file.chmod(0o600)
        return credential_file, configuration_file, code
