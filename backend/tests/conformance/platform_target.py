"""Objetivo de conformidad local: la plataforma real, su aprovisionamiento y la orden del kit
(TASK-230; NFR-GOB-15, 54, 61, 63 y 66; LC-GOB-19 y 22).

**Plataforma** (``platform_stack`` y ``InProcessApi``): la de producción, sin dobles en las rutas.

- PostgreSQL 16 recién migrado y LocalStack (S3, KMS y Secrets Manager) de testcontainers; las
  claves KMS ``vigia-node-ca`` y ``vigia-secrets``, los depósitos ``vigia-evidence``,
  ``vigia-edge`` y ``vigia-archive`` y los secretos de la base y de la clave del hash de origen
  del alta, todos efímeros (NFR-GOB-63, NFR-CTR-14);
- ``vigia-admin bootstrap`` (la orden de la imagen) crea la organización proveedora, las cinco
  claves de firma y la raíz de ``vigia-node-ca`` (``ca/root.pem`` en ``vigia-edge``), que es la
  autoridad de nodos efímera de la ejecución;
- ``InProcessApi``: ``compose_api_runtime`` + ``create_app`` con **todas** las unidades
  registradas y el servidor de ``vigia-api`` (``shared.api.main.build_server``), en un hilo con su
  bucle, con un ``MeterProvider`` de exportador en memoria (NFR-GOB-54). Los dos procesos de
  NFR-GOB-15 son la orden ``vigia-api`` de verdad (``api_process_environment``).

**Aprovisionamiento** (``provision``), por los servicios reales del ``AppRuntime`` (en el bucle
de la plataforma) con las personas que lo harían: la persona administradora del cliente crea la
planta y las zonas (``HierarchyService``), admite la familia (``AdmissionService``) y publica el
catálogo de cada zona (``CatalogPublicationService``); el instalador del proveedor, con su
concesión, declara el nodo (``NodeDeclarationService``), aprueba las compuertas
(``GateService.transition_gate``) y emite el código de alta (``EnrollmentCodeService``); el nodo
se da de alta por la ruta del contrato ``POST /api/nodes/enrollment`` detrás del balanceador
``app.``. La organización cliente, sus usuarios, sus sesiones, la concesión y la asignación
anterior de cada zona (``_previous_assignments``) se siembran por SQL, como en el resto de las
pruebas.

El nodo de prueba tiene dos zonas de una planta, una con la compuerta de uso aprobada y otra
pendiente; el segundo nodo es de otra organización (como el objetivo en proceso del kit). El
JSON de ``--provision``, los certificados y las claves de cliente se escriben en un directorio
temporal fuera del árbol; el código de alta solo existe en memoria durante el alta (PR-GOB-31).

**Tiempos.** El kit traslada cada registro para que termine un segundo antes de ahora y un tramo
dura como mucho ``max_segment_ms`` (60 s en estos catálogos), y la plataforma evalúa la
asignación, el catálogo y la compuerta en ``node_time.started_at`` con la tolerancia del reloj
(``fleet.domain.clock_tolerance``). Por eso cada zona lleva asignada al nodo ``ASSIGNED_SINCE``
y la suite no empieza hasta ``SUITE_LEAD`` después de ``in_force_since`` (catálogo y compuertas):
es una precondición con el reloj real, nunca un tope que decida un resultado.

**Almacén.** LocalStack queda detrás del balanceador local con TLS (``VIGIA_AWS_ENDPOINT_URL``
``https`` y ``AWS_CA_BUNDLE``): ``ClipUploadGrant.upload_url`` es ``https`` en el contrato y la
plataforma no entrega una concesión ``http`` (``clip_uploads.grant_document``).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import functools
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

import asyncpg
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from vigia_contracts.models.enumerations import CameraRoleInZone, GateStatus, PredicateFamily

from tests.factories import session_hash, uuid7
from tests.fleet_credentials_support import csr_pem, local_ip
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
    PostgresEndpoint,
    versioned_bucket,
)
from tests.runtime_support import breach_list, database_secret
from vigia_platform.catalog.adapters.http import CATALOG_STATE_KEY, CatalogHttp
from vigia_platform.catalog.application.admission import AdmissionRequest
from vigia_platform.catalog.domain.admission import AdmissionAnswers
from vigia_platform.catalog.domain.catalog_version import (
    InitialZoneParameters,
    NewStandard,
    StandardDraft,
)
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.zone_camera import ZoneCamera
from vigia_platform.fleet.adapters.http import FLEET_STATE_KEY, FleetHttp
from vigia_platform.identity.application.hierarchy import PlantSpec, ZoneSpec
from vigia_platform.identity.auth.sessions import (
    ABSOLUTE_TIMEOUT,
    IDLE_TIMEOUT,
    SessionCookie,
    new_session_cookie,
)
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.identity.domain.privacy_notice import CURRENT_PRIVACY_NOTICE_VERSION
from vigia_platform.shared.api.app import AppConfig, AppRuntime, create_app
from vigia_platform.shared.api.main import ApiServerConfig, build_server
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext
from vigia_platform.shared.observability.logging import JsonFormatter
from vigia_platform.shared.observability.metrics import PlatformMetrics
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig

__all__ = [
    "API_RUNTIME",
    "ASSIGNED_SINCE",
    "BACKEND",
    "BIN",
    "SUITE_LEAD",
    "InProcessApi",
    "PlatformStack",
    "Provisioned",
    "ProvisionedNode",
    "ProvisionedZone",
    "Provisioner",
    "conformance_command",
    "platform_stack",
    "run_conformance",
    "wait_ready",
]

T = TypeVar("T")

BACKEND: Final = Path(__file__).resolve().parents[2]
BIN: Final = Path(sys.executable).parent
STATIC: Final = BACKEND / "tests" / "fixtures" / "static"
ORIGIN: Final = "https://app.vigia.test"
NODES_BASE_URL: Final = "https://nodes.vigia.test/api/nodes"
"""``Endpoints.ingest_base_url`` de la configuración inicial (el kit usa ``--target``)."""
SENTINEL: Final = "health/ready-sentinel"
API_RUNTIME: Final = "vigia_platform.shared.runtime.api:build_api_runtime"
INGEST_PATH: Final = "/api/nodes"
ENROLLMENT_PATH: Final = "/api/nodes/enrollment"
BOOTSTRAP: Final = (
    "bootstrap",
    "--organization-code",
    "VIGIA-PROV",
    "--organization-name",
    "Proveedor sintético",
    "--operator-email",
    "operadora@example.test",
    "--operator-name",
    "Operadora sintética",
    "--yes",
)
WALL: Final = SystemClock()
"""Reloj real: la plataforma y el kit usan la hora del sistema (objetivo por URL)."""
ASSIGNED_SINCE: Final = dt.timedelta(days=400)
"""Antigüedad de la asignación previa de cada zona: más que la retención del nodo (30 días)."""
SUITE_LEAD: Final = dt.timedelta(seconds=65)
"""Desde ``in_force_since`` hasta la suite: el tramo más largo (60 s) ya empieza dentro."""
MAX_SEGMENT_MS: Final = 60_000
READY_SECONDS: Final = 120.0
SERVICE_SECONDS: Final = 120.0
"""Tope de una llamada de aprovisionamiento al bucle de la plataforma (nunca decide nada)."""
HTTP_SECONDS: Final = 60.0
REASON: Final = "Publicación sintética del catálogo de la zona de conformidad"
_AWS_VARIABLES: Final = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION",
    "AWS_CA_BUNDLE",
)


# --- Pila ---------------------------------------------------------------------------------------


@dataclass
class PlatformStack:
    """Base migrada, recursos de LocalStack y el entorno de ``vigia-api`` (sin secretos en claro
    salvo las credenciales fijas de LocalStack)."""

    migrated: MigratedDatabase
    localstack: LocalStackEndpoint
    environ: dict[str, str]
    directory: Path
    edge_bucket: str

    @property
    def provider_organization_id(self) -> uuid.UUID:
        return uuid.UUID(self.environ["VIGIA_PROVIDER_ORGANIZATION_ID"])

    def admin(self, statements: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        """Sentencias como superusuario en una transacción; devuelve las filas de la última."""

        async def go() -> list[Any]:
            connection: asyncpg.Connection = await self.migrated.connect()
            try:
                rows: list[Any] = []
                async with connection.transaction():
                    for sql, args in statements:
                        rows = list(await connection.fetch(sql, *args))
                return rows
            finally:
                await connection.close()

        return asyncio.run(go())

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.admin([(sql, args)])

    def node_ca_root(self) -> bytes:
        """``ca/root.pem`` de ``vigia-edge``: la raíz efímera de ``vigia-node-ca``."""
        s3 = self.localstack.aws_client("s3")
        body: bytes = s3.get_object(Bucket=self.edge_bucket, Key="ca/root.pem")["Body"].read()
        return body

    def run_admin(self, *argv: str, timeout: float = 180.0) -> tuple[int, str]:
        completed = subprocess.run(
            [str(BIN / "vigia-admin"), *argv],
            env=self.environ,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout + completed.stderr


def _result_line(output: str) -> dict[str, Any]:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            document = json.loads(line)
            if isinstance(document, dict) and "command" in document:
                return document
    raise AssertionError(f"vigia-admin sin línea de resultado:\n{output}")


@contextlib.contextmanager
def platform_stack(
    postgres: PostgresEndpoint,
    localstack: LocalStackEndpoint,
    directory: Path,
    *,
    aws_url: str,
    ca_bundle: Path,
) -> Iterator[PlatformStack]:
    """La pila de producción de ``vigia-api`` sobre contenedores, con ``bootstrap`` hecho.

    ``aws_url`` es LocalStack detrás del balanceador local con TLS (``ca_bundle``): las URL
    prefirmadas de las concesiones de clip son ``https``, como exige ``ClipUploadGrant``.
    """
    deployment = f"conformidad-{uuid.uuid4().hex[:8]}"
    kms = localstack.aws_client("kms")
    node_ca = kms.create_key(
        Description="vigia-node-ca efímera", KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256"
    )["KeyMetadata"]["KeyId"]
    secrets_key = kms.create_key(Description="vigia-secrets efímera")["KeyMetadata"]["KeyId"]
    s3 = localstack.aws_client("s3")
    manager = localstack.aws_client("secretsmanager")
    created: list[str] = []
    with (
        migrated_database(postgres, "conformidad") as migrated,
        versioned_bucket(s3, "vigia-evidence") as evidence,
        versioned_bucket(s3, "vigia-edge") as edge,
        versioned_bucket(s3, "vigia-archive") as archive,
    ):
        s3.put_object(Bucket=evidence, Key=SENTINEL, Body=b"centinela")
        endpoint = migrated.endpoint

        def secret(name: str, **value: Any) -> str:
            full = f"vigia/{deployment}/{name}"
            manager.create_secret(Name=full, **value)
            created.append(full)
            return full

        environ = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(directory),
            "AWS_ACCESS_KEY_ID": LOCALSTACK_ACCESS_KEY_ID,
            "AWS_SECRET_ACCESS_KEY": LOCALSTACK_SECRET_ACCESS_KEY,
            "AWS_REGION": localstack.region,
            "VIGIA_ENVIRONMENT": "test",
            "VIGIA_AWS_ENDPOINT_URL": aws_url,
            "AWS_CA_BUNDLE": str(ca_bundle),
            "PGSSLMODE": "disable",  # el contenedor local no tiene TLS
            "VIGIA_SIGNING_SECRET_PREFIX": f"vigia/{deployment}/signing/",
            "VIGIA_SECRETS_KEY_ARN": secrets_key,
            "VIGIA_NODE_CA_KEY_ARN": node_ca,
            "VIGIA_EVIDENCE_BUCKET": evidence,
            "VIGIA_ARCHIVE_BUCKET": archive,
            "VIGIA_EDGE_BUCKET": edge,
            "VIGIA_BOOTSTRAP_INVITATION_SECRET": f"vigia/{deployment}/bootstrap/invitation",
            "VIGIA_PUBLIC_ORIGIN": ORIGIN,
            "VIGIA_STATIC_DIR": str(STATIC),
            "VIGIA_BREACH_LIST_PATH": str(breach_list(directory)),
            "VIGIA_NODES_BASE_URL": NODES_BASE_URL,
            "VIGIA_API_RUNTIME": API_RUNTIME,
            "VIGIA_ADMIN_RUNTIME": "vigia_platform.shared.runtime.admin:build_admin_runtime",
        }
        environ["VIGIA_DB_APP_SECRET"] = secret(
            "db/app",
            SecretString=database_secret(
                host=endpoint.host,
                port=endpoint.port,
                dbname=migrated.database,
                password=migrated.app_password,
            ),
        )
        environ["VIGIA_ENROLLMENT_SOURCE_KEY_SECRET"] = secret(
            "enrollment/source-key", SecretBinary=secrets.token_bytes(32)
        )
        stack = PlatformStack(migrated, localstack, environ, directory, edge)
        code, output = stack.run_admin(*BOOTSTRAP)
        assert code == 0, output
        stack.environ["VIGIA_PROVIDER_ORGANIZATION_ID"] = _result_line(output)[
            "provider_organization_id"
        ]
        try:
            yield stack
        finally:
            for name in created:
                with contextlib.suppress(Exception):
                    manager.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


def api_process_environment(stack: PlatformStack, port: int) -> dict[str, str]:
    """El entorno de un proceso ``vigia-api`` de la pila en ``port`` (NFR-GOB-15)."""
    return {**stack.environ, "VIGIA_API_PORT": str(port)}


def wait_ready(base_url: str, *, timeout: float = READY_SECONDS, alive: Any = None) -> None:
    """Espera ``/health/ready`` 200 (precondición: el arranque supervisado terminó)."""
    deadline = WALL.monotonic() + timeout
    while WALL.monotonic() < deadline:
        if alive is not None and not alive():
            raise AssertionError(f"{base_url}: el proceso terminó antes de quedar listo")
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{base_url}/health/ready", timeout=10.0).status_code == 200:
                return
        time.sleep(0.5)
    raise AssertionError(f"{base_url}/health/ready no respondió 200 en {timeout:.0f} s")


# --- vigia-api en el proceso de la prueba -------------------------------------------------------


@dataclass
class InProcessApi:
    """``vigia-api`` con la raíz de composición de producción en un hilo con su propio bucle.

    Todo lo que toca la base (el servidor y el aprovisionamiento) corre en ese bucle (``call``):
    los grupos de conexiones de SQLAlchemy son del bucle que los creó.
    """

    environ: Mapping[str, str]
    reader: InMemoryMetricReader = field(default_factory=InMemoryMetricReader)
    runtime: AppRuntime | None = None
    port: int = 0
    _loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.new_event_loop)
    _thread: threading.Thread | None = None
    _server: Any = None
    _serving: Any = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def call(self, awaitable: Awaitable[T], *, timeout: float = SERVICE_SECONDS) -> T:
        async def wrapped() -> T:
            return await awaitable

        future = asyncio.run_coroutine_threadsafe(wrapped(), self._loop)
        return future.result(timeout=timeout)

    def start(self, port: int) -> None:
        self.port = port
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="vigia-api-en-proceso", daemon=True
        )
        self._thread.start()
        provider = MeterProvider(metric_readers=[self.reader])
        metrics = PlatformMetrics(provider.get_meter("vigia_platform.conformidad"))
        config = AppConfig.from_environ(self.environ)
        runtime_config = RuntimeConfig.from_environ(self.environ)

        async def compose() -> Any:
            self.runtime = await compose_api_runtime(config, runtime_config, metrics=metrics)
            app = create_app(config, runtime=self.runtime)
            self._server = build_server(app, ApiServerConfig(host="127.0.0.1", port=port))
            return app

        self.call(compose())
        self._serving = asyncio.run_coroutine_threadsafe(self._server.serve(), self._loop)
        wait_ready(self.url, alive=lambda: not self._serving.done())

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._server is not None:
            self._server.should_exit = True
        if self._serving is not None:
            with contextlib.suppress(Exception):
                self._serving.result(timeout=60)
        if self.runtime is not None:
            with contextlib.suppress(Exception):
                self.call(self.runtime.database.dispose())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=30)
        self._thread = None
        self._loop.close()

    def state(self, key: str) -> Any:
        assert self.runtime is not None
        return self.runtime.state[key]


@contextlib.contextmanager
def in_process_api(
    environ: Mapping[str, str], port: int, *, log_file: Path | None = None
) -> Iterator[InProcessApi]:
    """``InProcessApi`` con las credenciales de LocalStack en el entorno del proceso (boto3 las
    lee de ahí) durante su vida. Con ``log_file``, sus registros (JSON con la redacción de la
    plataforma, como los de los procesos ``vigia-api``) van a ese archivo."""
    saved = {name: os.environ.get(name) for name in _AWS_VARIABLES}
    for name in _AWS_VARIABLES:
        os.environ[name] = environ[name]
    logger = logging.getLogger("vigia")
    handler: logging.Handler | None = None
    if log_file is not None:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    api = InProcessApi(environ)
    try:
        api.start(port)
        yield api
    finally:
        api.stop()
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# --- Aprovisionamiento --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisionedZone:
    zone_id: uuid.UUID
    catalog: Mapping[str, Any]
    usage_approved: bool
    in_force_since: dt.datetime


@dataclass(frozen=True)
class ProvisionedNode:
    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zones: tuple[ProvisionedZone, ...]
    certificate_file: Path
    key_file: Path

    def document(self, base: Path) -> dict[str, Any]:
        """La entrada de este nodo en el JSON de ``--provision`` (rutas relativas a ``base``)."""
        return {
            "node_id": str(self.node_id),
            "organization_id": str(self.organization_id),
            "plant_id": str(self.plant_id),
            "certificate": self.certificate_file.relative_to(base).as_posix(),
            "key": self.key_file.relative_to(base).as_posix(),
            "zones": [
                {
                    "catalog": dict(zone.catalog),
                    "usage_approved": zone.usage_approved,
                    "in_force_since": _stamp(zone.in_force_since),
                }
                for zone in self.zones
            ],
        }


@dataclass(frozen=True)
class Provisioned:
    """Lo aprovisionado: los dos nodos, el JSON de ``--provision`` y la raíz del servidor."""

    primary: ProvisionedNode
    second: ProvisionedNode
    provision_file: Path
    verify_file: Path
    node_ca_file: Path
    ready_at: dt.datetime
    """Desde cuándo puede empezar la suite (``in_force_since`` + ``SUITE_LEAD``)."""
    enrollment_codes: tuple[str, ...] = field(repr=False, default=())
    """Los códigos de alta usados, solo en memoria: ninguna salida los contiene (PR-GOB-31)."""

    def leaks(self, *texts: str) -> list[str]:
        """Qué códigos de alta aparecen en ``texts`` (vacío si ninguno)."""
        return [code for code in self.enrollment_codes if any(code in text for text in texts)]

    def wait_until_ready(self) -> None:
        delay = (self.ready_at - WALL.now()).total_seconds()
        if delay > 0:
            time.sleep(delay)


def _stamp(moment: dt.datetime) -> str:
    utc = moment.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _ceil_ms(moment: dt.datetime) -> dt.datetime:
    """``moment`` redondeado al milisegundo siguiente (el informe y el kit van en ms)."""
    floor = moment.replace(microsecond=moment.microsecond // 1000 * 1000)
    return floor if floor == moment else floor + dt.timedelta(milliseconds=1)


def _camera(zone: uuid.UUID, index: int) -> ZoneCamera:
    return ZoneCamera(
        camera_id=uuid.uuid5(zone, f"camara-{index}"),
        code=f"CM-{index}",
        role_in_zone=CameraRoleInZone.PRIMARY if index == 0 else CameraRoleInZone.REDUNDANT,
        declared_min_fps=5.0,
        stream_reference=f"cam-{index}",
    )


def _draft() -> StandardDraft:
    return StandardDraft(
        family=PredicateFamily.COEXISTENCE,
        title_es="Coexistencia en la celda",
        declared_text="Nadie permanece en la celda mientras la máquina está energizada.",
        predicate={
            "all_of": [{"presence": True}, {"signal_role": "energy", "value": "asserted"}],
            "min_duration_ms": 0,
        },
    )


def _new_standard(zone: uuid.UUID) -> NewStandard:
    cameras = (_camera(zone, 0), _camera(zone, 1))
    return NewStandard(
        draft=_draft(),
        initial=InitialZoneParameters(
            cameras=cameras,
            required_count=1,
            required_camera_ids=(cameras[0].camera_id,),
            signals=(
                {
                    "signal_id": str(uuid.uuid5(zone, "senal-energia")),
                    "code": "SG-1",
                    "role": "energy",
                    "asserted_level": "high",
                    "source": {"reader": "plc-1", "channel": 1},
                    "description_es": "Energía de la prensa",
                },
            ),
            thresholds={"review": 0.4, "publication": 0.8},
            clip_window={"pre_seconds": 10, "post_seconds": 10},
            episode={"grouping_window_ms": 3000, "max_segment_ms": MAX_SEGMENT_MS},
        ),
    )


@dataclass
class _Site:
    """Una organización cliente a medio aprovisionar."""

    organization_id: uuid.UUID
    administrator: SessionCookie
    installer: SessionCookie
    concession_id: uuid.UUID
    zones_approved: tuple[bool, ...]
    plant_id: uuid.UUID | None = None
    zones: tuple[uuid.UUID, ...] = ()
    zone_codes: tuple[str, ...] = ()
    node_id: uuid.UUID | None = None
    declared_at: dt.datetime | None = None


@dataclass
class Provisioner:
    """Aprovisiona el nodo de prueba y el segundo nodo por los servicios reales (ver el módulo)."""

    stack: PlatformStack
    api: InProcessApi
    app_url: str
    """Base del balanceador ``app.`` (el alta va por la ruta del contrato)."""
    verify_file: Path
    directory: Path

    # --- Personas (siembra por SQL, como en el resto de las pruebas) ---------------------------

    @functools.cached_property
    def _operator(self) -> uuid.UUID:
        """La operadora que creó ``bootstrap``: autora de las altas sembradas."""
        (row,) = self.stack.fetch(
            "SELECT user_id FROM identity.user_account WHERE organization_id = $1"
            " ORDER BY created_at LIMIT 1",
            self.stack.provider_organization_id,
        )
        return uuid.UUID(str(row["user_id"]))

    def _user(self, organization: uuid.UUID, role: Role) -> tuple[uuid.UUID, list[Any]]:
        user = uuid.uuid4()
        now = WALL.now()
        return user, [
            (
                "INSERT INTO identity.user_account (user_id, organization_id, email,"
                " display_name, status, second_factor_required, second_factor_enrolled_at,"
                " created_at, privacy_notice_version_accepted)"
                " VALUES ($1, $2, $3, 'Persona sintética', 'active', false, NULL, $4, $5)",
                (
                    user,
                    organization,
                    f"persona-{secrets.token_hex(6)}@example.test",
                    now,
                    CURRENT_PRIVACY_NOTICE_VERSION,
                ),
            ),
            (
                "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id,"
                " role, scope_level, scope_id, assigned_at, assigned_by)"
                " VALUES ($1, $2, $3, $4, 'organization', $2, $5, $6)",
                (uuid7(), organization, user, role.value, now, self._operator),
            ),
        ]

    def _session(self, organization: uuid.UUID, user: uuid.UUID) -> tuple[SessionCookie, Any]:
        cookie = new_session_cookie(organization)
        now = WALL.now()
        return cookie, (
            "INSERT INTO identity.session (session_id_hash, user_id, organization_id, created_at,"
            " last_seen_at, idle_expires_at, absolute_expires_at, second_factor_verified,"
            " origin_hash) VALUES ($1, $2, $3, $4, $4, $5, $6, true, $7)",
            (
                cookie.session_id_hash,
                user,
                organization,
                now,
                now + IDLE_TIMEOUT,
                now + ABSOLUTE_TIMEOUT,
                session_hash(),
            ),
        )

    def _site(self, zones_approved: tuple[bool, ...]) -> _Site:
        organization = uuid.uuid4()
        provider = self.stack.provider_organization_id
        now = WALL.now()
        statements: list[Any] = [
            (
                "INSERT INTO identity.organization (organization_id, code, name, kind,"
                " created_at, created_by) VALUES ($1, $2, 'Cliente sintético', 'client', $3, $4)",
                (organization, f"ORG-{secrets.token_hex(4).upper()}", now, self._operator),
            )
        ]
        administrator, rows = self._user(organization, Role.ADMINISTRATOR)
        statements += rows
        installer, rows = self._user(provider, Role.PROVIDER_INSTALLER)
        statements += rows
        concession = uuid7()
        statements.append(
            (
                "INSERT INTO identity.provider_concession (concession_id, organization_id,"
                " provider_user_id, provider_organization_id, scope_level, scope_id, reason,"
                " granted_at, expires_at, status) VALUES ($1, $2, $3, $4, 'organization', $2,"
                " 'Puesta en marcha sintética del nodo', $5, $6, 'active')",
                (concession, organization, installer, provider, now, now + dt.timedelta(days=7)),
            )
        )
        admin_cookie, row = self._session(organization, administrator)
        statements.append(row)
        installer_cookie, row = self._session(provider, installer)
        statements.append(row)
        self.stack.admin(statements)
        return _Site(organization, admin_cookie, installer_cookie, concession, zones_approved)

    async def _context(self, cookie: SessionCookie, concession: uuid.UUID | None) -> ScopeContext:
        assert self.api.runtime is not None
        contexts = self.api.runtime.sessions
        assert contexts is not None
        scope = await contexts.context_from_session(cookie, concession_id=concession)
        context: ScopeContext = scope.context
        return context

    # --- Pasos por los servicios reales ---------------------------------------------------------

    def _declare(self, site: _Site) -> None:
        """Planta y zonas (administradora), admisión de la familia y nodo declarado."""
        assert self.api.runtime is not None and self.api.runtime.identity is not None
        hierarchy = self.api.runtime.identity.hierarchy
        assert hierarchy is not None
        catalog: CatalogHttp = self.api.state(CATALOG_STATE_KEY)
        fleet: FleetHttp = self.api.state(FLEET_STATE_KEY)

        async def go() -> None:
            administrator = await self._context(site.administrator, None)
            plant = await hierarchy.create_plant(
                administrator,
                PlantSpec(
                    code=f"PL-{secrets.token_hex(4).upper()}",
                    name="Planta sintética de conformidad",
                    country="CO",
                    data_region="us-east-1",
                    timezone="America/Bogota",
                ),
            )
            zones, codes = [], []
            for _ in site.zones_approved:
                administrator = await self._context(site.administrator, None)
                zone = await hierarchy.create_zone(
                    administrator,
                    plant.plant_id,
                    ZoneSpec(code=f"ZN-{secrets.token_hex(4).upper()}", name="Zona sintética"),
                )
                zones.append(zone.zone_id)
                codes.append(zone.code)
            site.zone_codes = tuple(codes)
            administrator = await self._context(site.administrator, None)
            await catalog.admissions.evaluate(
                administrator,
                plant.plant_id,
                AdmissionRequest(
                    family=PredicateFamily.COEXISTENCE,
                    answers=AdmissionAnswers(standard=True, remedy=True, subject=True),
                ),
            )
            installer = await self._context(site.installer, site.concession_id)
            declared = await fleet.declarations.declare(
                installer,
                plant.plant_id,
                code=f"ND-{secrets.token_hex(4).upper()}",
                zone_ids=zones,
            )
            site.plant_id, site.zones, site.node_id = plant.plant_id, tuple(zones), declared.node_id
            site.declared_at = declared.declared_at

        self.api.call(go())

    def _publish(self, site: _Site) -> list[tuple[uuid.UUID, Mapping[str, Any], dt.datetime]]:
        """Catálogo firmado de cada zona y compuertas: montaje aprobado; uso según la zona.

        Por los servicios reales del ``AppRuntime``: la administradora publica la versión 1 del
        catálogo (``CatalogPublicationService.publish_catalog_version``, con su registro
        ``catalog_version_published`` y el evento ``catalog_updated``) y el instalador, con
        ``commissioning.run`` sobre la zona, aprueba el montaje y, si toca, el uso
        (``GateService.transition_gate``, lo que harán el acta de alcance y el acuerdo de uso; su
        respaldo es un identificador sintético). Devuelve, por zona, el catálogo y desde cuándo
        rigen catálogo y compuertas.
        """
        catalog: CatalogHttp = self.api.state(CATALOG_STATE_KEY)
        gates = catalog.gates
        published: list[tuple[uuid.UUID, Mapping[str, Any], dt.datetime]] = []

        async def go() -> None:
            for zone, approved in zip(site.zones, site.zones_approved, strict=True):
                administrator = await self._context(site.administrator, None)
                version = await catalog.catalog.publish_catalog_version(
                    administrator, zone, _new_standard(zone), REASON
                )
                since = version.issued_at
                installer = await self._context(site.installer, site.concession_id)
                _, authorized = await gates.zone(installer, zone, PermissionKey.COMMISSIONING_RUN)
                writer = with_unit(authorized, ActorUnit.U03)
                for kind in [GateKind.MOUNTING] + ([GateKind.USAGE] if approved else []):

                    async def transition(
                        transaction: Any,
                        kind: GateKind = kind,
                        zone: uuid.UUID = zone,
                        writer: ScopeContext = writer,
                    ) -> Any:
                        return await gates.transition_gate(
                            transaction, writer, zone, kind, GateStatus.APPROVED, uuid.uuid4()
                        )

                    done = await gates.run(writer, transition)
                    since = max(since, done.interval.effective_from)
                published.append((zone, dict(version.payload), since))

        self.api.call(go())
        return published

    def _enroll(self, site: _Site) -> tuple[Path, Path, str]:
        """Código de alta (instalador) y alta por ``POST /api/nodes/enrollment`` en ``app.``.

        Devuelve el certificado, la clave y el código usado: el código solo vive en memoria, para
        comprobar que no aparece en ninguna salida (PR-GOB-31)."""
        fleet: FleetHttp = self.api.state(FLEET_STATE_KEY)
        assert site.node_id is not None

        async def issue() -> str:
            installer = await self._context(site.installer, site.concession_id)
            issued = await fleet.enrollment_codes.issue(installer, site.node_id)  # type: ignore[arg-type]
            code: str = issued.code
            return code

        client_key = ec.generate_private_key(ec.SECP256R1())
        node = str(site.node_id)
        code = self.api.call(issue())
        body = {
            "enrollment_code": code,
            "key_algorithm": "ecdsa_p256",
            "certificate_signing_request": csr_pem(node, key=client_key),
            "server_certificate_signing_request": csr_pem(node, names=[local_ip()]),
            "software_version": "1.0.0",
            "contract_version": _contract_version(),
            "hardware_fingerprint": secrets.token_hex(32),
            "requested_at": _stamp(WALL.now()),
        }
        response = httpx.post(
            self.app_url + ENROLLMENT_PATH,
            json=body,
            headers={"X-Vigia-Contract-Version": _contract_version()},
            verify=str(self.verify_file),
            timeout=HTTP_SECONDS,
        )
        assert response.status_code == 200, response.text
        certificate = x509.load_pem_x509_certificate(response.json()["certificate"].encode())
        certificate_file = self.directory / f"nodo-{node}.crt"
        key_file = self.directory / f"nodo-{node}.key"
        certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            client_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_file.chmod(0o600)
        return certificate_file, key_file, code

    def _previous_assignments(self, site: _Site) -> None:
        """Un intervalo de asignación anterior ``[ahora - ASSIGNED_SINCE, alta de la asignación)``
        por zona, añadido (``zone_node_assignment`` es de solo anexar): el nodo lleva más de la
        retención asignado a sus zonas, como un nodo en servicio. Sin él, un registro más antiguo
        que la retención sería ``node_zone_mismatch`` (asignación en el instante del hecho, paso 2)
        antes que ``timestamp_out_of_window``."""
        rows = self.stack.fetch(
            "SELECT organization_id, plant_id, zone_id, assigned_at"
            " FROM identity.zone_node_assignment WHERE node_id = $1",
            site.node_id,
        )
        assert len(rows) == len(site.zones)
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
                        site.node_id,
                        row["assigned_at"] - ASSIGNED_SINCE,
                        row["assigned_at"],
                        self._operator,
                    ),
                )
                for row in rows
            ]
        )

    def provision(self) -> Provisioned:
        primary_site = self._site((True, False))
        second_site = self._site((True,))
        for site in (primary_site, second_site):
            self._declare(site)
            self._previous_assignments(site)
        nodes = []
        codes: list[str] = []
        latest = WALL.now()
        for site in (primary_site, second_site):
            published = self._publish(site)
            certificate_file, key_file, code = self._enroll(site)
            codes.append(code)
            zones = tuple(
                ProvisionedZone(zone, catalog, approved, _ceil_ms(since))
                for (zone, catalog, since), approved in zip(
                    published, site.zones_approved, strict=True
                )
            )
            latest = max([latest, *(zone.in_force_since for zone in zones)])
            assert site.node_id is not None and site.plant_id is not None
            nodes.append(
                ProvisionedNode(
                    site.node_id,
                    site.organization_id,
                    site.plant_id,
                    zones,
                    certificate_file,
                    key_file,
                )
            )
        primary, second = nodes
        node_ca_file = self.directory / "vigia-node-ca.crt"
        node_ca_file.write_bytes(self.stack.node_ca_root())
        provision_file = self.directory / "provision.json"
        provision_file.write_text(
            json.dumps(
                {
                    "verify": self.verify_file.relative_to(self.directory).as_posix(),
                    "flood": False,
                    "primary": primary.document(self.directory),
                    "second": second.document(self.directory),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return Provisioned(
            primary,
            second,
            provision_file,
            self.verify_file,
            node_ca_file,
            latest + SUITE_LEAD,
            tuple(codes),
        )


def _contract_version() -> str:
    from vigia_contracts.versioning import CONTRACT_VERSION

    return str(CONTRACT_VERSION)


# --- La orden del kit ---------------------------------------------------------------------------


def conformance_command(
    *,
    nodes_url: str,
    app_url: str,
    provision_file: Path,
    profile: str,
    seed: int,
    report_file: Path,
    groups: Sequence[str] = (),
) -> list[str]:
    """``vigia-conformance run`` contra la plataforma por URL (la base de la ingesta y el alta)."""
    command = [
        str(BIN / "vigia-conformance"),
        "run",
        "--target",
        nodes_url + INGEST_PATH,
        "--enrollment-url",
        app_url + ENROLLMENT_PATH,
        "--provision",
        str(provision_file),
        "--profile",
        profile,
        "--seed",
        str(seed),
        "--report",
        str(report_file),
    ]
    for group in groups:
        command += ["--group", group]
    return command


@dataclass(frozen=True)
class ConformanceRun:
    code: int
    output: str
    report: dict[str, Any] | None


def run_conformance(command: Sequence[str], report_file: Path, *, timeout: float) -> ConformanceRun:
    """Ejecuta la orden (sin credenciales de AWS ni de la base en su entorno) hasta que termina."""
    environ = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("AWS_", "VIGIA_", "PG"))
    }
    completed = subprocess.run(
        list(command),
        env=environ,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    report = json.loads(report_file.read_text(encoding="utf-8")) if report_file.is_file() else None
    return ConformanceRun(completed.returncode, completed.stdout + completed.stderr, report)
