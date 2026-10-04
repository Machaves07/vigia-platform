"""Arranque de los tres procesos con la raíz de composición de producción (VIG-137, A-52).

Las órdenes de la imagen (``vigia-api``, ``vigia-worker`` y ``vigia-admin``, los puntos de entrada
de ``pyproject.toml``) corren como **subprocesos** con ``VIGIA_*_RUNTIME`` apuntando a
``vigia_platform.shared.runtime`` y solo variables de entorno (ninguna contraseña), contra
PostgreSQL 16 recién migrado (testcontainers) y LocalStack (S3, KMS y Secrets Manager):

1. ``vigia-admin bootstrap`` crea la organización proveedora, las cinco claves de firma y la raíz
   de ``vigia-node-ca`` con la raíz real (base como ``vigia_app`` por el secreto
   ``VIGIA_DB_APP_SECRET``).
2. ``vigia-api`` arranca y ``/health/ready`` responde 200 dentro del plazo de arranque
   (PAT-NUC-RES-02: base, claves, clave de datos, registros y centinela). Sus registros no
   contienen la contraseña de ``vigia_app``.
3. ``vigia-worker`` arranca, deja en ``shared.periodic_task`` las 11 tareas de U-02 y en
   ``shared.consumer`` sus 2 consumidores (se borran antes de arrancarlo: los escribe él) y se
   para en orden con 0 tras ``SIGTERM``.
4. ``vigia-admin bootstrap --resume --dry-run`` arranca con la raíz real (lee la base, no escribe).
5. ``vigia-admin restore-audit-partition`` con la base inaccesible (un secreto que no existe, o uno
   que apunta a un servidor que no responde) no lee el secreto ni intenta conectar: termina en la
   verificación del archivo descargado de ``vigia-archive``.
6. Sin ``VIGIA_DB_APP_SECRET``, con un secreto inexistente o con uno sin la forma de RDS, cada
   proceso sale con su código de configuración (``STARTUP_FAILURE_EXIT_CODE`` en la API y el
   worker; 1, ``config_invalid``, en ``vigia-admin``, donde 3 es «sin confirmación»), la salida
   nombra la variable y la contraseña del secreto no aparece.
7. **Arranque de una unidad añadida al registro** (``tests.runtime_support.PROBE_UNIT``): los
   sincronizadores del ``AppRuntime`` de producción dejan su tipo de registro, su evento, su
   consumidor y su tarea en la base, sin editar el constructor (en otra base: la unidad de prueba
   no debe quedar en la de los procesos).

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
    PostgresEndpoint,
    versioned_bucket,
)
from tests.runtime_support import (
    PROBE_CONSUMER,
    PROBE_EVENT,
    PROBE_RECORD_TYPE,
    PROBE_TASK,
    SECRET_PASSWORD,
    FakeReader,
    breach_list,
    database_secret,
    probe_labels,
    runtime_environ,
    with_probe_unit,
)
from vigia_platform.shared.api.app import STARTUP_FAILURE_EXIT_CODE, AppConfig
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.runtime.api import compose_api_runtime
from vigia_platform.shared.runtime.config import RuntimeConfig

pytestmark = pytest.mark.integration

_CLOCK: Final = SystemClock()
BIN: Final = Path(sys.executable).parent
STATIC: Final = Path(__file__).resolve().parents[1] / "fixtures" / "static"
ORIGIN: Final = "https://app.vigia.test"
SENTINEL: Final = "health/ready-sentinel"
BOOT_SECONDS: Final = 120.0
"""Tope de la prueba para ver ``ready``: el plazo de arranque del proceso es de 60 s."""
STOP_SECONDS: Final = 150.0
"""Tope de la prueba para la parada ordenada (el worker espera hasta 115 s)."""
U02_TASKS: Final = 11
U02_CONSUMERS: Final = 2
UNREACHABLE_HOST: Final = "192.0.2.1"  # TEST-NET-1 (RFC 5737): nunca responde
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@dataclass
class Finished:
    code: int
    output: str


@dataclass
class Stack:
    migrated: MigratedDatabase
    localstack: LocalStackEndpoint
    environ: dict[str, str]
    archive_bucket: str
    directory: Path
    secrets_created: list[str] = field(default_factory=list)

    @property
    def deployment(self) -> str:
        return self.environ["VIGIA_SIGNING_SECRET_PREFIX"].split("/")[1]

    def secret(self, suffix: str, value: str) -> str:
        """Crea ``vigia/<despliegue>/db/<suffix>`` con ``value`` y devuelve su nombre."""
        name = f"vigia/{self.deployment}/db/{suffix}"
        self.localstack.aws_client("secretsmanager").create_secret(Name=name, SecretString=value)
        self.secrets_created.append(name)
        return name

    def env(self, **changes: str | None) -> dict[str, str]:
        values: dict[str, str | None] = {**self.environ, **changes}
        return {key: value for key, value in values.items() if value is not None}

    def run(
        self, command: str, *argv: str, timeout: float = 120.0, **changes: str | None
    ) -> Finished:
        """Ejecuta la orden hasta que termina; salida estándar y de errores juntas."""
        completed = subprocess.run(
            [str(BIN / command), *argv],
            env=self.env(**changes),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return Finished(completed.returncode, completed.stdout + completed.stderr)

    @contextlib.contextmanager
    def start(self, command: str, **changes: str | None) -> Iterator[Process]:
        log = self.directory / f"{command}-{uuid.uuid4().hex[:8]}.log"
        with log.open("w", encoding="utf-8") as sink:
            process = subprocess.Popen(
                [str(BIN / command)],
                env=self.env(**changes),
                stdout=sink,
                stderr=subprocess.STDOUT,
            )
        running = Process(process, log)
        try:
            yield running
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        async def go() -> list[Any]:
            connection = await self.migrated.connect()
            try:
                return list(await connection.fetch(sql, *args))
            finally:
                await connection.close()

        return asyncio.run(go())


@dataclass
class Process:
    process: subprocess.Popen[bytes]
    log: Path

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def wait_for(self, predicate: Any, what: str, timeout: float = BOOT_SECONDS) -> None:
        deadline = _CLOCK.monotonic() + timeout
        while _CLOCK.monotonic() < deadline:
            if predicate():
                return
            if self.process.poll() is not None:
                pytest.fail(
                    f"{what}: el proceso salió con {self.process.returncode}\n{self.output()}"
                )
            time.sleep(0.5)
        pytest.fail(f"{what}: no ocurrió en {timeout} s\n{self.output()}")

    def stop(self) -> int:
        self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=STOP_SECONDS)


def _json_line(output: str) -> dict[str, Any]:
    """La línea JSON de resultado de ``vigia-admin`` (la que lleva ``command`` o ``error``)."""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{"):
            document = json.loads(line)
            if isinstance(document, dict) and ("command" in document or "error" in document):
                return document
    raise AssertionError(f"sin línea de resultado:\n{output}")


@pytest.fixture(scope="module")
def stack(
    postgres_endpoint: PostgresEndpoint,
    localstack_endpoint: LocalStackEndpoint,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Stack]:
    directory = tmp_path_factory.mktemp("arranque")
    deployment = f"arranque-{uuid.uuid4().hex[:8]}"
    kms = localstack_endpoint.aws_client("kms")
    node_ca = kms.create_key(
        Description="vigia-node-ca de prueba", KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256"
    )["KeyMetadata"]["KeyId"]
    secrets_key = kms.create_key(Description="vigia-secrets de prueba")["KeyMetadata"]["KeyId"]
    s3 = localstack_endpoint.aws_client("s3")
    with (
        migrated_database(postgres_endpoint, "arranque") as migrated,
        versioned_bucket(s3, "vigia-evidence") as evidence,
        versioned_bucket(s3, "vigia-edge") as edge,
        versioned_bucket(s3, "vigia-archive") as archive,
    ):
        s3.put_object(Bucket=evidence, Key=SENTINEL, Body=b"centinela")
        environ = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(directory),
            "AWS_ACCESS_KEY_ID": LOCALSTACK_ACCESS_KEY_ID,
            "AWS_SECRET_ACCESS_KEY": LOCALSTACK_SECRET_ACCESS_KEY,
            "VIGIA_ENVIRONMENT": "test",
            "AWS_REGION": localstack_endpoint.region,
            "VIGIA_AWS_ENDPOINT_URL": localstack_endpoint.url,
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
            "VIGIA_API_RUNTIME": "vigia_platform.shared.runtime.api:build_api_runtime",
            "VIGIA_WORKER_RUNTIME": "vigia_platform.shared.runtime.worker:build_worker_runtime",
            "VIGIA_ADMIN_RUNTIME": "vigia_platform.shared.runtime.admin:build_admin_runtime",
        }
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
            yield built
        finally:
            client = localstack_endpoint.aws_client("secretsmanager")
            for name in built.secrets_created:
                with contextlib.suppress(Exception):
                    client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


# --- 1 y 2: vigia-api ----------------------------------------------------------------------------


def test_api_boots_with_the_production_root_and_becomes_ready(stack: Stack) -> None:
    port = _free_port()
    with stack.start("vigia-api", VIGIA_API_PORT=str(port)) as api:

        def ready() -> bool:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health/ready", timeout=10.0)
            except httpx.HTTPError:
                return False
            return response.status_code == 200

        api.wait_for(ready, "/health/ready 200")
        code = api.stop()
        output = api.output()

    assert code == 0, output
    assert "arranque completo" in output
    assert stack.migrated.app_password not in output


# --- 3: vigia-worker -----------------------------------------------------------------------------


def test_worker_boots_registers_u02_and_stops_in_order(stack: Stack) -> None:
    # El arranque de bootstrap ya sincronizó el catálogo: se borra para que lo escriba el worker.
    stack.fetch("DELETE FROM shared.periodic_task")
    stack.fetch("DELETE FROM shared.consumer")
    port = _free_port()
    with stack.start("vigia-worker", VIGIA_WORKER_HEALTH_PORT=str(port)) as worker:
        worker.wait_for(lambda: "worker en marcha" in worker.output(), "worker en marcha")
        live = httpx.get(f"http://127.0.0.1:{port}/health/live", timeout=10.0)
        tasks = stack.fetch("SELECT task_name, unit FROM shared.periodic_task ORDER BY task_name")
        consumers = stack.fetch("SELECT consumer_name, unit FROM shared.consumer")
        code = worker.stop()
        output = worker.output()

    assert live.status_code == 200
    assert len([row for row in tasks if row["unit"] == "U-02"]) == U02_TASKS
    assert len([row for row in consumers if row["unit"] == "U-02"]) == U02_CONSUMERS
    assert code == 0, output
    assert "parada ordenada completa" in output
    assert stack.migrated.app_password not in output


# --- 4 y 5: vigia-admin --------------------------------------------------------------------------


def test_admin_dry_run_boots_with_the_production_root(stack: Stack) -> None:
    finished = stack.run("vigia-admin", "bootstrap", "--resume", "--dry-run")
    assert finished.code == 0, finished.output
    result = _json_line(finished.output)
    assert result["dry_run"] is True
    assert result["provider_organization_id"] == stack.environ["VIGIA_PROVIDER_ORGANIZATION_ID"]
    assert result["operator_status"] == "invited"  # lo leyó de la base
    assert stack.migrated.app_password not in finished.output


@pytest.mark.parametrize("database", ["inexistente", "inalcanzable"])
def test_restore_audit_partition_never_touches_an_unreachable_database(
    stack: Stack, database: str
) -> None:
    if database == "inexistente":
        secret = f"vigia/{stack.deployment}/db/no-existe"
    else:
        secret = stack.secret(
            f"inalcanzable-{uuid.uuid4().hex[:6]}",
            database_secret(host=UNREACHABLE_HOST, password=SECRET_PASSWORD),
        )
    key = f"audit/{uuid.uuid4().hex}.zip"
    data = b"no es un archivo de auditoria"
    stack.localstack.aws_client("s3").put_object(Bucket=stack.archive_bucket, Key=key, Body=data)
    wrong = hashlib.sha256(b"otro contenido").hexdigest()
    started = _CLOCK.monotonic()
    dry = stack.run(
        "vigia-admin",
        "restore-audit-partition",
        key,
        "--sha256",
        wrong,
        "--output",
        str(stack.directory / f"salida-{uuid.uuid4().hex[:6]}"),
        "--dry-run",
        VIGIA_DB_APP_SECRET=secret,
    )
    real = stack.run(
        "vigia-admin",
        "restore-audit-partition",
        key,
        "--sha256",
        wrong,
        "--output",
        str(stack.directory / f"salida-{uuid.uuid4().hex[:6]}"),
        VIGIA_DB_APP_SECRET=secret,
    )
    elapsed = _CLOCK.monotonic() - started

    assert dry.code == 0, dry.output
    assert _json_line(dry.output)["dry_run"] is True
    # La orden real descargó el archivo y lo rechazó al verificarlo: nunca pasó por la base (un
    # secreto inexistente habría dado config_invalid; uno inalcanzable, temporarily_unavailable).
    assert real.code == 4, real.output
    assert _json_line(real.output)["error"] == "archive_invalid"
    assert "VIGIA_DB_APP_SECRET" not in real.output
    assert SECRET_PASSWORD not in dry.output + real.output
    assert elapsed < 60  # sin la espera de conexión a un servidor que no responde


# --- 6: fallos de configuración ------------------------------------------------------------------


def _admin_dry_run(stack: Stack, **changes: str | None) -> Finished:
    return stack.run("vigia-admin", "bootstrap", "--resume", "--dry-run", **changes)


@pytest.mark.parametrize("command", ["vigia-api", "vigia-worker", "vigia-admin"])
@pytest.mark.parametrize("case", ["sin-variable", "inexistente", "sin-forma-rds"])
def test_a_missing_or_bad_database_secret_stops_each_process(
    stack: Stack, command: str, case: str
) -> None:
    if case == "sin-variable":
        secret: str | None = None
    elif case == "inexistente":
        secret = f"vigia/{stack.deployment}/db/no-existe-{uuid.uuid4().hex[:6]}"
    else:
        secret = stack.secret(
            f"incompleto-{uuid.uuid4().hex[:6]}",
            database_secret(host=None, password=SECRET_PASSWORD),
        )
    if command == "vigia-admin":
        finished = _admin_dry_run(stack, VIGIA_DB_APP_SECRET=secret)
        expected = 1  # configuración (en vigia-admin, 3 es «sin confirmación»)
    else:
        changes = {
            "VIGIA_DB_APP_SECRET": secret,
            "VIGIA_API_PORT": str(_free_port()),
            "VIGIA_WORKER_HEALTH_PORT": str(_free_port()),
        }
        finished = stack.run(command, **changes)
        expected = STARTUP_FAILURE_EXIT_CODE

    assert finished.code == expected, finished.output
    assert "VIGIA_DB_APP_SECRET" in finished.output
    assert SECRET_PASSWORD not in finished.output
    assert stack.migrated.app_password not in finished.output
    if command == "vigia-admin":
        assert _json_line(finished.output)["error"] == "config_invalid"


# --- 7: arranque de una unidad añadida al registro -----------------------------------------------


def test_a_test_unit_reaches_the_database_at_startup(
    postgres_endpoint: PostgresEndpoint,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with_probe_unit(monkeypatch)
    with migrated_database(postgres_endpoint, "unidad") as migrated:
        endpoint = migrated.endpoint
        reader = FakeReader(
            database_secret(
                host=endpoint.host,
                port=endpoint.port,
                dbname=migrated.database,
                password=migrated.app_password,
            )
        )
        runtime = RuntimeConfig.from_environ(
            runtime_environ(VIGIA_BREACH_LIST_PATH=str(breach_list(tmp_path)))
        )
        config = AppConfig(
            environment="test",
            data_key_id="alias/vigia-secrets",
            static_dir=STATIC,
            public_origin=ORIGIN,
            labels_path=probe_labels(tmp_path),
        )

        async def startup() -> dict[str, list[str]]:
            built = await compose_api_runtime(config, runtime, reader=reader)
            try:
                for synchronize in built.registries:  # lo que hace el arranque supervisado
                    await synchronize()
            finally:
                await built.database.dispose()
            connection = await migrated.connect()
            try:
                queries = {
                    "record_type": "SELECT record_type AS name FROM ledger.record_type",
                    "event_type": "SELECT event_name AS name FROM shared.event_type",
                    "consumer": "SELECT consumer_name AS name FROM shared.consumer",
                    "periodic_task": "SELECT task_name AS name FROM shared.periodic_task",
                }
                return {
                    table: [row["name"] for row in await connection.fetch(sql)]
                    for table, sql in queries.items()
                }
            finally:
                await connection.close()

        rows = asyncio.run(startup())

    assert PROBE_RECORD_TYPE in rows["record_type"]
    assert PROBE_EVENT in rows["event_type"]
    assert PROBE_CONSUMER in rows["consumer"]
    assert PROBE_TASK in rows["periodic_task"]
    assert len(rows["periodic_task"]) == U02_TASKS + 1
    assert reader.reads == 1
