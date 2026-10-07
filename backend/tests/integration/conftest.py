"""Fixtures de integración: PostgreSQL 16 y LocalStack (TASK-103, LC-NUC-36, PAT-NUC-MAN-07).

Las pruebas de integración usan los **mismos contenedores** que ``docker-compose.yml``: las
imágenes de abajo son las del archivo, fijadas por digest, y
``tests/unit/test_local_environment.py`` comprueba que no se separan.

Dos modos, elegidos con la variable de entorno ``VIGIA_TEST_USE_COMPOSE``:

- sin la variable (o con ``0``): cada sesión de pytest levanta sus propios contenedores con
  testcontainers y los elimina al terminar;
- ``VIGIA_TEST_USE_COMPOSE=1``: las pruebas usan el entorno ya levantado con
  ``docker compose up -d`` (más rápido al iterar). Los puertos se leen de las mismas variables
  que usa el archivo (``VIGIA_LOCAL_POSTGRES_PORT``, ``VIGIA_LOCAL_LOCALSTACK_PORT``).

Si Docker no está disponible, o el entorno de compose no responde, la fixture **falla** con un
mensaje en español: una prueba de integración nunca pasa sin ejecutar nada.

Las fixtures son de sesión y cualquier prueba las reutiliza. Se declaran en ``tests/conftest.py``
con los generadores de este módulo, para que las propiedades de ``tests/properties/`` que
necesitan la base (TASK-108) compartan el mismo contenedor que ``tests/integration/``:

- ``postgres_endpoint`` (``postgres_endpoint_session``): dónde conectarse a PostgreSQL 16
  (``PostgresEndpoint``);
- ``localstack_endpoint`` (``localstack_endpoint_session``): dónde llamar a S3, KMS y Secrets
  Manager (``LocalStackEndpoint``), con clientes de boto3 que ya llevan tiempos de espera
  (PAT-NUC-RES-03, regla ``VIG003``).

Solo datos generados (NFR-CTR-43). Las credenciales son las fijas del entorno local.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import quote

import boto3  # type: ignore[import-untyped]
import httpx
import pytest
from botocore.config import Config  # type: ignore[import-untyped]

from vigia_platform.shared.storage import AddressingStyle, StorageCredentials, StorageSettings

if TYPE_CHECKING:
    from testcontainers.core.container import DockerContainer

POSTGRES_IMAGE = (
    "postgres:16@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a09590fad07b2246bfa4b54"
)
LOCALSTACK_IMAGE = (
    "localstack/localstack:4.14.0"
    "@sha256:3ebc37595918b8accb852f8048fef2aff047d465167edd655528065b07bc364a"
)

USE_COMPOSE_VARIABLE = "VIGIA_TEST_USE_COMPOSE"

POSTGRES_USER = "vigia"
POSTGRES_PASSWORD = "vigia_local"  # noqa: S105 - credencial fija del entorno local, no un secreto
POSTGRES_DATABASE = "vigia"
POSTGRES_PORT = 5432

LOCALSTACK_PORT = 4566
LOCALSTACK_REGION = "us-east-1"
LOCALSTACK_SERVICES = ("s3", "kms", "secretsmanager")
LOCALSTACK_ENVIRONMENT = {
    "SERVICES": ",".join(LOCALSTACK_SERVICES),
    "EAGER_SERVICE_LOADING": "1",
    "AWS_DEFAULT_REGION": LOCALSTACK_REGION,
    "DISABLE_EVENTS": "1",
    "SKIP_SSL_CERT_DOWNLOAD": "1",
    # LocalStack no valida firmas por defecto: sin esto aceptaría un ``PUT`` prefirmado sin la
    # cabecera de suma que la URL firmó (TASK-112).
    "S3_SKIP_SIGNATURE_VALIDATION": "0",
}
LOCALSTACK_ACCESS_KEY_ID = "test"
LOCALSTACK_SECRET_ACCESS_KEY = "test"  # noqa: S105 - LocalStack acepta cualquier valor

STARTUP_TIMEOUT_SECONDS = 120.0
PROBE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class PostgresEndpoint:
    """Datos de conexión a PostgreSQL 16 del entorno de pruebas."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str

    @property
    def dsn(self) -> str:
        """Cadena ``postgresql://`` para asyncpg o libpq."""
        return (
            f"postgresql://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{quote(self.database, safe='')}"
        )

    @property
    def sqlalchemy_url(self) -> str:
        """URL de SQLAlchemy con el controlador asíncrono ``asyncpg``."""
        return self.dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@dataclass(frozen=True, slots=True)
class LocalStackEndpoint:
    """Punto de acceso de LocalStack (S3, KMS y Secrets Manager) del entorno de pruebas."""

    url: str
    region: str = LOCALSTACK_REGION

    def aws_client(self, service_name: str) -> Any:
        """Cliente de boto3 contra LocalStack con tiempos de espera y sin reintentos.

        Firma SigV4 y S3 con direcciones por ruta, como las URL prefirmadas de la plataforma.
        """
        config = Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 1, "mode": "standard"},
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )
        return boto3.client(
            service_name,
            endpoint_url=self.url,
            region_name=self.region,
            aws_access_key_id=LOCALSTACK_ACCESS_KEY_ID,
            aws_secret_access_key=LOCALSTACK_SECRET_ACCESS_KEY,
            config=config,
        )

    def storage_settings(self, bucket: str) -> StorageSettings:
        """``StorageSettings`` de ``shared.storage`` contra LocalStack (direcciones por ruta)."""
        return StorageSettings(
            bucket=bucket,
            endpoint_url=self.url,
            region=self.region,
            addressing_style=AddressingStyle.PATH,
            credentials=StorageCredentials(LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY),
        )


@contextlib.contextmanager
def versioned_bucket(s3: Any, prefix: str) -> Iterator[str]:
    """Depósito propio con versionado, como ``vigia-evidence``; se vacía y se borra al salir."""
    name = f"{prefix}-{uuid.uuid4().hex[:16]}"
    s3.create_bucket(Bucket=name)
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    try:
        yield name
    finally:
        for upload in s3.list_multipart_uploads(Bucket=name).get("Uploads", []):
            s3.abort_multipart_upload(Bucket=name, Key=upload["Key"], UploadId=upload["UploadId"])
        # Paginado: más de 1000 versiones (la conformidad ``nightly``) no caben en una página.
        for listing in s3.get_paginator("list_object_versions").paginate(Bucket=name):
            for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
                s3.delete_object(Bucket=name, Key=item["Key"], VersionId=item["VersionId"])
        s3.delete_bucket(Bucket=name)


def use_compose() -> bool:
    """``True`` si ``VIGIA_TEST_USE_COMPOSE`` pide usar el ``docker compose`` ya levantado."""
    value = os.environ.get(USE_COMPOSE_VARIABLE, "").strip().lower()
    if value in ("", "0", "false", "no"):
        return False
    if value in ("1", "true", "yes", "si", "sí"):
        return True
    pytest.fail(
        f"{USE_COMPOSE_VARIABLE}={value!r} no es válido: usa 1 para el entorno de docker compose "
        "o 0 (o nada) para testcontainers.",
        pytrace=False,
    )


def _compose_port(variable: str, default: int) -> int:
    raw = os.environ.get(variable, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        pytest.fail(f"{variable}={raw!r} no es un puerto válido.", pytrace=False)
    return int(raw)


def _localstack_services_running(url: str) -> bool:
    """``True`` si LocalStack responde y S3, KMS y Secrets Manager están en marcha."""
    try:
        response = httpx.get(f"{url}/_localstack/health", timeout=PROBE_TIMEOUT_SECONDS)
        services = response.json().get("services", {})
    except (httpx.HTTPError, ValueError):
        return False
    return all(services.get(name) == "running" for name in LOCALSTACK_SERVICES)


def _wait_for_localstack(url: str, *, timeout: float) -> None:
    attempts = max(1, int(timeout))
    for _ in range(attempts):
        if _localstack_services_running(url):
            return
        time.sleep(1)
    pytest.fail(
        f"LocalStack en {url} no tiene {', '.join(LOCALSTACK_SERVICES)} en marcha tras "
        f"{attempts} s."
    )


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _start[C: DockerContainer](build: Callable[[], C]) -> C:
    """Crea y arranca un contenedor; sin Docker, la prueba falla con un mensaje en español."""
    container: C | None = None
    try:
        container = build()
        container.start()
    except Exception as error:  # docker.errors.DockerException y fallos de arranque
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop()
        pytest.fail(
            "Las pruebas de integración necesitan Docker en ejecución (testcontainers con "
            f"PostgreSQL 16 y LocalStack). Detalle: {type(error).__name__}: {error}",
            pytrace=False,
        )
    return container


def _fail_without_compose(service: str, where: str) -> NoReturn:
    pytest.fail(
        f"{USE_COMPOSE_VARIABLE}=1 pero {service} no responde en {where}: levanta el entorno con "
        "`docker compose up -d` desde la raíz del repositorio.",
        pytrace=False,
    )


def postgres_endpoint_session() -> Iterator[PostgresEndpoint]:
    """PostgreSQL 16 con la base ``vigia``: contenedor propio o el de ``docker compose``."""
    if use_compose():
        endpoint = PostgresEndpoint(
            host="127.0.0.1",
            port=_compose_port("VIGIA_LOCAL_POSTGRES_PORT", POSTGRES_PORT),
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DATABASE,
        )
        if not _tcp_reachable(endpoint.host, endpoint.port):
            _fail_without_compose("PostgreSQL", f"{endpoint.host}:{endpoint.port}")
        yield endpoint
        return

    from testcontainers.community.postgres import PostgresContainer

    def build() -> PostgresContainer:
        container = PostgresContainer(
            image=POSTGRES_IMAGE,
            username=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            dbname=POSTGRES_DATABASE,
            driver=None,
        )
        return container.with_env("TZ", "UTC").with_env("PGTZ", "UTC")

    container = _start(build)
    try:
        yield PostgresEndpoint(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(POSTGRES_PORT)),
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
            database=POSTGRES_DATABASE,
        )
    finally:
        container.stop()


def localstack_endpoint_session() -> Iterator[LocalStackEndpoint]:
    """LocalStack comunitario con S3, KMS y Secrets Manager en marcha."""
    if use_compose():
        port = _compose_port("VIGIA_LOCAL_LOCALSTACK_PORT", LOCALSTACK_PORT)
        endpoint = LocalStackEndpoint(url=f"http://127.0.0.1:{port}")
        if not _localstack_services_running(endpoint.url):
            _fail_without_compose("LocalStack (S3, KMS y Secrets Manager)", endpoint.url)
        yield endpoint
        return

    from testcontainers.core.container import DockerContainer

    def build() -> DockerContainer:
        container = DockerContainer(LOCALSTACK_IMAGE).with_exposed_ports(LOCALSTACK_PORT)
        for name, value in LOCALSTACK_ENVIRONMENT.items():
            container.with_env(name, value)
        return container

    container = _start(build)
    try:
        url = (
            f"http://{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(LOCALSTACK_PORT)}"
        )
        _wait_for_localstack(url, timeout=STARTUP_TIMEOUT_SECONDS)
        yield LocalStackEndpoint(url=url)
    finally:
        container.stop()
