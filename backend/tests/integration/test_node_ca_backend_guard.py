"""El firmante de ``vigia-node-ca`` por archivo con procesos y contenedores reales (TASK-543; Q6).

- **Guarda**: ``vigia-api``, ``vigia-worker`` y ``vigia-admin`` con ``VIGIA_NODE_CA_BACKEND=file``
  en ``pilot`` no arrancan: ``node_ca_backend_forbidden``.
- **Persistencia** (criterio 1): en un LocalStack propio, ``bootstrap``, un alta que emite un
  certificado y **reinicio de LocalStack**. Con el backend ``file``, ``ca/root.pem`` se publica otra
  vez con la misma clave y el mismo certificado sigue validando; con ``kms``, la clave ya no existe
  y el certificado queda huérfano: es lo que motiva la tarea, y la prueba lo demuestra.
- **Sumideros** (criterio 3): ``vigia-admin bootstrap`` real; la clave no aparece en la salida, en
  los objetos del depósito, en los secretos ni en la base.
- ``vigia-api`` real queda lista con el backend ``file``; ``rotate-node-ca`` real publica el paquete
  de dos raíces.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from testcontainers.core.container import DockerContainer

from tests.admin_support import issue_node_certificate, public_der, validates
from tests.identity_db import migrated_database
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_ENVIRONMENT,
    LOCALSTACK_IMAGE,
    LOCALSTACK_PORT,
    LOCALSTACK_REGION,
    LOCALSTACK_SECRET_ACCESS_KEY,
    STARTUP_TIMEOUT_SECONDS,
    LocalStackEndpoint,
    PostgresEndpoint,
    _start,
    _wait_for_localstack,
    versioned_bucket,
)
from tests.integration.test_admin_bootstrap import (
    Env,
    _bootstrap,
    _secret_name,
    env,  # noqa: F401 - fixture
)
from tests.integration.test_production_runtime_boot import (
    BIN,
    BOOTSTRAP,
    Stack,
    _free_port,
    _json_line,
)
from tests.runtime_support import breach_list, database_secret
from vigia_platform.fleet.adapters.ca.file_signer import FileBackedKms, FileNodeCaSigner
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.node_ca import (
    ROOT_CERTIFICATE_KEY,
    NodeCaPublisher,
    read_bundle,
)
from vigia_platform.shared.secrets import (
    AwsCredentials,
    AwsSettings,
    KmsAdapter,
    SecretsUnavailable,
)
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

_CLOCK: Final = SystemClock()
KEY_ID: Final = "file:node-ca"
ENVIRONMENT: Final = "local"
RESTART_SECONDS: Final = 60
PROCESS_SECONDS: Final = 600.0
"""Tope de la prueba para ver el arranque o el final de un proceso: nunca decide el resultado (bajo
carga, importar la aplicación y agotar el plazo de arranque pasó de 180 s)."""


# --- La guarda -----------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["vigia-api", "vigia-worker", "vigia-admin"])
def test_each_process_refuses_the_file_backend_in_pilot(command: str, tmp_path: Path) -> None:
    """Antes de leer ningún secreto ni abrir la base: el aborto es de la configuración."""
    arguments = ("bootstrap", "--resume", "--dry-run") if command == "vigia-admin" else ()
    environ = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "VIGIA_ENVIRONMENT": "pilot",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": LOCALSTACK_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": LOCALSTACK_SECRET_ACCESS_KEY,
        "VIGIA_DB_APP_SECRET": "/".join(("vigia", "pilot", "db", "app")),
        "VIGIA_SIGNING_SECRET_PREFIX": "vigia/pilot/signing/",
        "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
        "VIGIA_NODE_CA_KEY_ARN": KEY_ID,
        "VIGIA_NODE_CA_BACKEND": "file",
        "VIGIA_NODE_CA_KEY_FILE": str(tmp_path / "node-ca.pem"),
        "VIGIA_PROVIDER_ORGANIZATION_ID": str(uuid.uuid4()),
        "VIGIA_EVIDENCE_BUCKET": "vigia-evidence",
        "VIGIA_ARCHIVE_BUCKET": "vigia-archive",
        "VIGIA_EDGE_BUCKET": "vigia-edge",
        "VIGIA_API_RUNTIME": "vigia_platform.shared.runtime.api:build_api_runtime",
        "VIGIA_WORKER_RUNTIME": "vigia_platform.shared.runtime.worker:build_worker_runtime",
        "VIGIA_ADMIN_RUNTIME": "vigia_platform.shared.runtime.admin:build_admin_runtime",
    }
    finished = subprocess.run(
        [str(BIN / command), *arguments],
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode != 0, finished.stdout + finished.stderr
    assert _json_line(finished.stdout + finished.stderr)["error"] == "node_ca_backend_forbidden"
    assert list(tmp_path.glob("*.pem")) == []


# --- Persistencia frente al reinicio de LocalStack -----------------------------------------------


class RestartableLocalStack:
    """Un LocalStack solo para esta prueba: reiniciarlo no afecta al de la sesión."""

    def __init__(self) -> None:
        def build() -> DockerContainer:
            container = DockerContainer(LOCALSTACK_IMAGE).with_exposed_ports(LOCALSTACK_PORT)
            for name, value in LOCALSTACK_ENVIRONMENT.items():
                container.with_env(name, value)
            return container

        self.container = _start(build)
        self.endpoint: LocalStackEndpoint | None = None

    def _port(self) -> int:
        """El puerto publicado: Docker Desktop tarda en mostrarlo, también tras un reinicio."""
        deadline = _CLOCK.monotonic() + STARTUP_TIMEOUT_SECONDS
        while True:
            try:
                return int(self.container.get_exposed_port(LOCALSTACK_PORT))
            except ConnectionError:
                if _CLOCK.monotonic() > deadline:
                    raise
                time.sleep(1.0)

    def wait_ready(self) -> LocalStackEndpoint:
        port = self._port()
        url = f"http://{self.container.get_container_host_ip()}:{port}"
        _wait_for_localstack(url, timeout=STARTUP_TIMEOUT_SECONDS)
        self.endpoint = LocalStackEndpoint(url=url)
        return self.endpoint

    def restart(self) -> LocalStackEndpoint:
        wrapped = self.container.get_wrapped_container()
        wrapped.restart(timeout=RESTART_SECONDS)
        wrapped.reload()
        return self.wait_ready()

    def stop(self) -> None:
        self.container.stop()


@pytest.fixture(scope="module")
def restartable() -> Iterator[RestartableLocalStack]:
    localstack = RestartableLocalStack()
    try:
        # Dentro del ``try``: si no llega a estar listo, el contenedor no se queda encendido.
        localstack.wait_ready()
        yield localstack
    finally:
        localstack.stop()


def _endpoint(localstack: RestartableLocalStack) -> LocalStackEndpoint:
    assert localstack.endpoint is not None
    return localstack.endpoint


def _new_edge_bucket(endpoint: LocalStackEndpoint) -> str:
    bucket = f"vigia-edge-{uuid.uuid4().hex[:12]}"
    s3 = endpoint.aws_client("s3")
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    return bucket


def _edge_storage(endpoint: LocalStackEndpoint, bucket: str) -> S3Storage:
    return S3Storage(endpoint.storage_settings(bucket), _CLOCK)


def _aws(endpoint: LocalStackEndpoint) -> AwsSettings:
    return AwsSettings(
        region=LOCALSTACK_REGION,
        endpoint_url=endpoint.url,
        credentials=AwsCredentials(LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY),
    )


def _publisher(
    endpoint: LocalStackEndpoint, bucket: str, signer: Any, environment: str = ENVIRONMENT
) -> NodeCaPublisher:
    return NodeCaPublisher(
        storage=_edge_storage(endpoint, bucket),
        kms=signer,
        environment=environment,
        random_bytes=os.urandom,
    )


def test_the_certificate_keeps_validating_after_a_localstack_restart_with_the_file_backend(
    restartable: RestartableLocalStack, tmp_path: Path
) -> None:
    key_file = tmp_path / "node-ca.pem"
    signer = FileNodeCaSigner(key_file, can_create=True)
    bucket = _new_edge_bucket(_endpoint(restartable))
    now = _CLOCK.now()
    published = asyncio.run(
        _publisher(_endpoint(restartable), bucket, signer).publish_root(KEY_ID, now=now)
    )
    leaf = asyncio.run(issue_node_certificate(signer, KEY_ID, published.root, now))
    at = now + timedelta(minutes=5)
    assert validates(leaf, published.bundle, at)

    restartable.restart()

    # LocalStack comunitario lo perdió todo: el depósito y ``ca/root.pem`` ya no están.
    s3 = _endpoint(restartable).aws_client("s3")
    assert bucket not in {item["Name"] for item in s3.list_buckets().get("Buckets", [])}
    # La clave sigue en su archivo. Un ``bootstrap`` repetido publica una raíz de la misma clave.
    again = _new_edge_bucket(_endpoint(restartable))
    republished = asyncio.run(
        _publisher(_endpoint(restartable), again, FileNodeCaSigner(key_file)).publish_root(
            KEY_ID, now=now
        )
    )
    assert public_der(republished.root.public_key()) == public_der(published.root.public_key())
    stored = s3.get_object(Bucket=again, Key=ROOT_CERTIFICATE_KEY)["Body"].read()
    assert validates(leaf, read_bundle(stored), at)


def test_the_kms_backend_loses_the_authority_on_the_same_restart(
    restartable: RestartableLocalStack,
) -> None:
    """La mutación del criterio 1: con ``kms``, el mismo reinicio deja huérfano el certificado."""
    kms_client = _endpoint(restartable).aws_client("kms")
    key_id = kms_client.create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256")["KeyMetadata"][
        "KeyId"
    ]
    kms = KmsAdapter(_aws(_endpoint(restartable)))
    bucket = _new_edge_bucket(_endpoint(restartable))
    now = _CLOCK.now()
    published = asyncio.run(
        _publisher(_endpoint(restartable), bucket, kms).publish_root(key_id, now=now)
    )
    leaf = asyncio.run(issue_node_certificate(kms, key_id, published.root, now))
    at = now + timedelta(minutes=5)
    assert validates(leaf, published.bundle, at)

    restartable.restart()

    kms_after = KmsAdapter(_aws(_endpoint(restartable)))
    with pytest.raises(SecretsUnavailable):
        asyncio.run(kms_after.get_public_key(key_id))
    created = (
        _endpoint(restartable)
        .aws_client("kms")
        .create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256")
    )
    again = _new_edge_bucket(_endpoint(restartable))
    republished = asyncio.run(
        _publisher(_endpoint(restartable), again, kms_after).publish_root(
            created["KeyMetadata"]["KeyId"], now=now
        )
    )
    assert not validates(leaf, republished.bundle, at)


# --- bootstrap real, sumideros y API real --------------------------------------------------------


def _stack_environ(
    directory: Path,
    localstack: LocalStackEndpoint,
    deployment: str,
    key_file: Path,
    secrets_key: str,
    buckets: dict[str, str],
) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(directory),
        "AWS_ACCESS_KEY_ID": LOCALSTACK_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": LOCALSTACK_SECRET_ACCESS_KEY,
        "VIGIA_ENVIRONMENT": ENVIRONMENT,
        "AWS_REGION": localstack.region,
        "VIGIA_AWS_ENDPOINT_URL": localstack.url,
        "PGSSLMODE": "disable",
        "VIGIA_SIGNING_SECRET_PREFIX": f"vigia/{deployment}/signing/",
        "VIGIA_SECRETS_KEY_ARN": secrets_key,
        "VIGIA_NODE_CA_BACKEND": "file",
        "VIGIA_NODE_CA_KEY_FILE": str(key_file),
        "VIGIA_NODE_CA_KEY_ARN": KEY_ID,
        "VIGIA_EVIDENCE_BUCKET": buckets["evidence"],
        "VIGIA_ARCHIVE_BUCKET": buckets["archive"],
        "VIGIA_EDGE_BUCKET": buckets["edge"],
        "VIGIA_BOOTSTRAP_INVITATION_SECRET": f"vigia/{deployment}/bootstrap/invitation",
        "VIGIA_PUBLIC_ORIGIN": "https://app.vigia.test",
        "VIGIA_STATIC_DIR": str(Path(__file__).resolve().parents[1] / "fixtures" / "static"),
        "VIGIA_BREACH_LIST_PATH": str(breach_list(directory)),
        "VIGIA_API_RUNTIME": "vigia_platform.shared.runtime.api:build_api_runtime",
        "VIGIA_WORKER_RUNTIME": "vigia_platform.shared.runtime.worker:build_worker_runtime",
        "VIGIA_ADMIN_RUNTIME": "vigia_platform.shared.runtime.admin:build_admin_runtime",
    }


@pytest.fixture(scope="module")
def file_stack(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Stack, Path, str]]:
    directory = tmp_path_factory.mktemp("firmante-archivo")
    deployment = f"archivo-{uuid.uuid4().hex[:8]}"
    key_file = directory / "node-ca.pem"
    secrets_key = localstack_endpoint.aws_client("kms").create_key(
        Description="vigia-secrets de prueba"
    )["KeyMetadata"]["KeyId"]
    s3 = localstack_endpoint.aws_client("s3")
    with (
        migrated_database(postgres_endpoint, "archivo") as migrated,
        versioned_bucket(s3, "vigia-evidence") as evidence,
        versioned_bucket(s3, "vigia-edge") as edge,
        versioned_bucket(s3, "vigia-archive") as archive,
    ):
        s3.put_object(Bucket=evidence, Key="health/ready-sentinel", Body=b"centinela")
        environ = _stack_environ(
            directory,
            localstack_endpoint,
            deployment,
            key_file,
            secrets_key,
            {"evidence": evidence, "edge": edge, "archive": archive},
        )
        built = Stack(migrated, localstack_endpoint, environ, archive, directory)
        endpoint = migrated.endpoint
        built.environ["VIGIA_DB_APP_SECRET"] = built.secret(
            "app",
            database_secret(
                host=endpoint.host,
                port=endpoint.port,
                dbname=migrated.database,
                password=migrated.app_password,
            ),
        )
        bootstrap = built.run("vigia-admin", *BOOTSTRAP)
        assert bootstrap.code == 0, bootstrap.output
        result = _json_line(bootstrap.output)
        built.environ["VIGIA_PROVIDER_ORGANIZATION_ID"] = result["provider_organization_id"]
        try:
            yield built, key_file, bootstrap.output
        finally:
            client = localstack_endpoint.aws_client("secretsmanager")
            for name in built.secrets_created:
                with contextlib.suppress(Exception):
                    client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


def _key_body_lines(key_file: Path) -> list[str]:
    return [line for line in key_file.read_text().splitlines() if not line.startswith("-----")]


def _logged_backend(output: str) -> set[str]:
    """Los ``code`` de las líneas de registro del firmante (nunca por subcadena del mensaje)."""
    codes: set[str] = set()
    for line in output.splitlines():
        with contextlib.suppress(ValueError):
            document = json.loads(line)
            if isinstance(document, dict) and document.get("code") in {"kms", "file"}:
                codes.add(str(document["code"]))
    return codes


def test_bootstrap_creates_the_key_and_publishes_its_root(
    file_stack: tuple[Stack, Path, str],
) -> None:
    stack, key_file, output = file_stack
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"
    s3 = stack.localstack.aws_client("s3")
    body = s3.get_object(Bucket=stack.environ["VIGIA_EDGE_BUCKET"], Key=ROOT_CERTIFICATE_KEY)
    (root,) = read_bundle(body["Body"].read())
    public = asyncio.run(FileNodeCaSigner(key_file).get_public_key(KEY_ID))
    assert public_der(root.public_key()) == public
    assert isinstance(root.public_key(), ec.EllipticCurvePublicKey)
    assert _logged_backend(output) == {"file"}


def test_the_private_key_reaches_no_sink(file_stack: tuple[Stack, Path, str]) -> None:
    stack, key_file, output = file_stack
    lines = _key_body_lines(key_file)
    assert lines
    sinks: dict[str, str] = {"salida de bootstrap": output}

    s3 = stack.localstack.aws_client("s3")
    for name in ("VIGIA_EDGE_BUCKET", "VIGIA_EVIDENCE_BUCKET", "VIGIA_ARCHIVE_BUCKET"):
        bucket = stack.environ[name]
        for item in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
            content = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            sinks[f"{bucket}/{item['Key']}"] = content.decode("latin-1")

    client = stack.localstack.aws_client("secretsmanager")
    for secret in client.list_secrets(MaxResults=100)["SecretList"]:
        if stack.deployment in secret["Name"]:
            value = client.get_secret_value(SecretId=secret["ARN"]).get("SecretString", "")
            sinks[f"secreto {secret['Name']}"] = value

    async def database_rows() -> dict[str, str]:
        connection = await asyncpg.connect(stack.migrated.as_role().dsn)
        try:
            tables = await connection.fetch(
                "SELECT n.nspname || '.' || c.relname AS name FROM pg_class AS c"
                " JOIN pg_namespace AS n ON n.oid = c.relnamespace"
                " WHERE c.relkind = 'r' AND n.nspname NOT IN"
                " ('pg_catalog', 'information_schema', 'pg_toast')"
            )
            rows: dict[str, str] = {}
            for table in tables:
                schema, name = table["name"].split(".", 1)
                found = await connection.fetch(
                    f'SELECT t::text AS row FROM "{schema}"."{name}" AS t'  # noqa: S608
                )
                rows[table["name"]] = "\n".join(row["row"] for row in found)
            return rows
        finally:
            await connection.close()

    sinks.update(asyncio.run(database_rows()))
    assert len(sinks) > 10
    leaks = [name for name, text in sinks.items() if any(line in text for line in lines)]
    assert leaks == []


def test_a_second_dry_run_reads_the_same_key(file_stack: tuple[Stack, Path, str]) -> None:
    stack, key_file, _ = file_stack
    before = key_file.read_bytes()
    finished = stack.run("vigia-admin", "bootstrap", "--resume", "--dry-run")
    assert finished.code == 0, finished.output
    assert key_file.read_bytes() == before


def _ready(port: int) -> bool:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health/ready", timeout=10.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def test_the_api_becomes_ready_with_the_file_backend(file_stack: tuple[Stack, Path, str]) -> None:
    stack, key_file, _ = file_stack
    port = _free_port()
    with stack.start("vigia-api", VIGIA_API_PORT=str(port)) as api:
        api.wait_for(lambda: _ready(port), "/health/ready 200", timeout=PROCESS_SECONDS)
        api.stop()
        output = api.output()
    assert _logged_backend(output) == {"file"}
    assert not any(line in output for line in _key_body_lines(key_file))


def test_the_api_without_the_key_file_does_not_become_ready_nor_creates_it(
    file_stack: tuple[Stack, Path, str], tmp_path: Path
) -> None:
    """Nunca una autoridad nueva por un volumen mal montado: la crea solo ``vigia-admin``."""
    stack, _, _ = file_stack
    missing = tmp_path / "node-ca.pem"
    port = _free_port()
    with stack.start(
        "vigia-api", VIGIA_API_PORT=str(port), VIGIA_NODE_CA_KEY_FILE=str(missing)
    ) as api:
        # Termina o queda lista, lo que ocurra antes: si queda lista, la prueba cae enseguida.
        api.wait_for(
            lambda: api.process.poll() is not None or _ready(port),
            "el proceso termina o queda listo",
            timeout=PROCESS_SECONDS,
        )
        assert not _ready(port), api.output()
        assert api.process.wait(timeout=PROCESS_SECONDS) != 0, api.output()
    assert list(tmp_path.iterdir()) == []


# --- rotate-node-ca real -------------------------------------------------------------------------


def test_rotate_node_ca_publishes_two_roots_with_the_file_backend(
    env: Env,  # noqa: F811 - la fixture de ``test_admin_bootstrap``
    tmp_path: Path,
) -> None:
    env.world.kms_factory = lambda aws: _file_kms(aws, tmp_path)
    env.world.node_ca_key_id = KEY_ID
    document = _bootstrap(env)
    provider_id = uuid.UUID(document["provider_organization_id"])
    (old_root,) = read_bundle(env.object(ROOT_CERTIFICATE_KEY))
    signer = FileNodeCaSigner(tmp_path / "node-ca.pem")
    now = env.world.clock.now()
    old_node = asyncio.run(issue_node_certificate(signer, KEY_ID, old_root, now))
    environ = env.world.environ(provider_id)
    operator_id = env.activate(provider_id, env.secret(_secret_name(env))["link"])
    rotate = ("rotate-node-ca", "--new-key-id", "file:node-ca-2", "--operator", str(operator_id))
    code, _, err = env.world.run(*rotate, "--dry-run", environ=environ)
    assert code == 0, err
    assert not (tmp_path / "node-ca-2.pem").exists()  # --dry-run no escribe la clave
    code, _, err = env.world.run(*rotate, "--yes", environ=environ)
    assert code == 0, err
    bundle = read_bundle(env.object(ROOT_CERTIFICATE_KEY))
    assert len(bundle) == 2 and bundle[0] == old_root
    assert oct((tmp_path / "node-ca-2.pem").stat().st_mode & 0o777) == "0o600"
    new_node = asyncio.run(issue_node_certificate(signer, "file:node-ca-2", bundle[1], now))
    at = now + timedelta(minutes=5)
    assert validates(old_node, bundle, at)
    assert validates(new_node, bundle, at)
    assert not validates(old_node, (bundle[1],), at)


def _file_kms(aws: AwsSettings, directory: Path) -> KmsAdapter:
    return FileBackedKms(aws, FileNodeCaSigner(directory / "node-ca.pem", can_create=True))
