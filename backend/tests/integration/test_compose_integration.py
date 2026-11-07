"""La plataforma de integración persistente, de verdad (TASK-545, VIG-393; A-76 D3; A-89).

Levanta ``compose.integration.yml`` en el PC con la IP de ``lo`` (127.0.0.1) como «tailnet»,
corre ``local/integration/bootstrap.sh`` (dos veces), da de alta un nodo por ``app.``, lo hace
latir por ``nodes.`` a través de ``vigia-mtls-edge``, reinicia LocalStack, copia y restaura la
base y, al final, quita la clave de la autoridad para comprobar que la receta de reinicio falla
sin publicar otra autoridad. Las pruebas comparten un único despliegue y se ejecutan en el orden
del archivo: el despliegue tarda más de un minuto, y lo que prueban es justamente su continuidad.

Requisitos: Docker con Compose v2 y la imagen de ``backend/Dockerfile`` construida como
``vigia-platform:integration`` (o la que nombre ``VIGIA_PLATFORM_IMAGE``; ver el README de
``local/integration``). Sin la imagen, las pruebas se saltan **con ese motivo**: no hay forma de
construirla sin la clave de despliegue de ``vigia-contracts`` en el agente SSH.

Todo es sintético. El nodo simulado hace lo que el kit de ``vigia-contracts``: la alta por
``POST /api/nodes/enrollment`` con un código de un solo uso y el latido ``POST
/api/nodes/heartbeats`` con su certificado de cliente. Las personas leen el inventario por la ruta
``GET /fleet/nodes`` de ``app.`` con la cookie de una sesión de la administradora (el mismo servicio
que muestra SCR-07).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from vigia_contracts.versioning import CONTRACT_VERSION

from tests.factories import uuid7
from tests.fleet_credentials_support import csr_pem, local_ip
from vigia_platform.shared.clock import SystemClock

pytestmark = pytest.mark.integration

REPOSITORY: Final = Path(__file__).resolve().parents[3]
DOMAIN: Final = "vigia.test"
LOOPBACK: Final = "127.0.0.1"
IMAGE: Final = os.environ.get("VIGIA_PLATFORM_IMAGE", "vigia-platform:integration")
CLOCK: Final = SystemClock()
COMMAND_SECONDS: Final = 900.0
"""Tope de una orden de Docker o de un guion: nunca decide un resultado."""
HTTP_SECONDS: Final = 30.0
SETTLE_SECONDS: Final = 90.0
EDGE: Final = "vigia-mtls-edge"
SESSION_COOKIE: Final = "__Host-vigia_session"
DOCKER: Final = shutil.which("docker") or "docker"


def _stamp(moment: dt.datetime) -> str:
    utc = moment.astimezone(dt.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [DOCKER, *arguments],
        capture_output=True,
        text=True,
        timeout=120.0,
        check=check,
    )


def _unused_subnet() -> str:
    """Un /24 de 172.30.x que ninguna red de Docker use (otros agentes comparten el PC)."""
    names = _docker("network", "ls", "--quiet").stdout.split()
    used = ""
    if names:
        used = _docker(
            "network", "inspect", *names, "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}"
        ).stdout
    taken = set(re.findall(r"172\.30\.(\d+)\.", used))
    for _ in range(200):
        octet = 16 + secrets.randbelow(200)
        if str(octet) not in taken:
            return f"172.30.{octet}.0/24"
    raise RuntimeError("no hay una subred libre para la prueba")


@dataclass
class Deployment:
    """El despliegue de la prueba y lo que se hace con él (todo por ``docker compose``)."""

    project: str
    directory: Path
    env_file: Path
    app_port: int
    nodes_port: int
    environment: dict[str, str]
    edge_ca: Path | None = None
    outputs: list[str] = field(default_factory=list)

    def compose(
        self,
        *arguments: str,
        check: bool = True,
        stdin: str | None = None,
        timeout: float = COMMAND_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            [
                DOCKER,
                "compose",
                "-f",
                "compose.integration.yml",
                "--env-file",
                str(self.env_file),
                *arguments,
            ],
            cwd=REPOSITORY,
            env=self.environment,
            capture_output=True,
            text=True,
            input=stdin,
            timeout=timeout,
            check=False,
        )
        if check and completed.returncode != 0:
            raise AssertionError(
                f"docker compose {' '.join(arguments)}:\n{completed.stderr[-4000:]}"
            )
        return completed

    def script(self, name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(REPOSITORY / "local" / "integration" / name), *arguments],
            cwd=REPOSITORY,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=COMMAND_SECONDS,
            check=False,
        )

    def seed(self, *arguments: str) -> dict[str, Any]:
        """``seed.py`` en la imagen: la última línea JSON de su salida."""
        done = self.compose(
            "run", "--rm", "-T", "vigia-admin", "python", "/integration/seed.py", *arguments
        )
        return json.loads(done.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]

    def sql(self, statement: str) -> str:
        done = self.compose(
            "exec", "-T", "postgres", "psql", "--username=vigia", "--dbname=vigia",
            "--no-align", "--tuples-only", "--command", statement,
        )  # fmt: skip
        return done.stdout.strip()

    def inventory_counts(self) -> tuple[int, int]:
        nodes = int(self.sql("SELECT count(*) FROM identity.node_identity"))
        zones = int(self.sql("SELECT count(*) FROM identity.zone"))
        return nodes, zones

    def published_ports(self) -> dict[str, list[tuple[str, int]]]:
        """Servicio → (IP de publicación, puerto del anfitrión), de ``docker compose ps``."""
        done = self.compose("--profile", "app", "ps", "--all", "--format", "json")
        published: dict[str, list[tuple[str, int]]] = {}
        for line in done.stdout.splitlines():
            if not line.strip():
                continue
            container = json.loads(line)
            ports = [
                (str(item["URL"]), int(item["PublishedPort"]))
                for item in container.get("Publishers") or []
                if item.get("PublishedPort")
            ]
            published[container["Service"]] = ports
        return published

    def root_sha256(self) -> str:
        """Huella de ``ca/root.pem`` como la sirve el depósito (lectura anónima, red interna)."""
        program = (
            "import hashlib,urllib.request;"
            "print(hashlib.sha256(urllib.request.urlopen("
            "'http://localhost:4566/vigia-edge/ca/root.pem').read()).hexdigest())"
        )
        done = self.compose("exec", "-T", "localstack", "python3", "-c", program)
        return done.stdout.strip()

    def wait_healthy(self) -> None:
        self.compose(
            "--profile",
            "app",
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            str(int(SETTLE_SECONDS)),
        )


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Deployment]:
    if shutil.which("docker") is None:
        pytest.skip("sin Docker")
    if _docker("image", "inspect", IMAGE, check=False).returncode != 0:
        pytest.skip(
            f"falta la imagen {IMAGE}: constrúyela con backend/Dockerfile y la clave de despliegue "
            "de vigia-contracts en el agente SSH (local/integration/README.md); "
            "VIGIA_PLATFORM_IMAGE la nombra"
        )
    directory = tmp_path_factory.mktemp("integration")
    app_port, nodes_port = _free_port(), _free_port()
    env_file = directory / "integration.env"
    env_file.write_text(
        f"VIGIA_EDGE_BIND={LOOPBACK}\nVIGIA_EDGE_APP_PORT={app_port}\nVIGIA_EDGE_NODES_PORT={nodes_port}\n"
        f"VIGIA_PLATFORM_IMAGE={IMAGE}\nVIGIA_INTERNAL_SUBNET={_unused_subnet()}\n",
        encoding="utf-8",
    )
    project = f"vigia-it-{secrets.token_hex(3)}"
    environment = {
        **os.environ,
        "COMPOSE_PROJECT_NAME": project,
        "VIGIA_INTEGRATION_ENV": str(env_file),
        "VIGIA_INTEGRATION_CREDENTIALS": str(directory / "credentials" / "credentials.json"),
        "VIGIA_BACKUP_DIR": str(directory / "backups"),
    }
    result = Deployment(project, directory, env_file, app_port, nodes_port, environment)
    try:
        yield result
    finally:
        result.compose(
            "--profile",
            "app",
            "--profile",
            "tools",
            "down",
            "--volumes",
            "--remove-orphans",
            check=False,
        )


@dataclass
class Node:
    """El nodo simulado: lo que el kit guarda tras la alta."""

    node_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    certificate: Path
    key: Path


@pytest.fixture(scope="module")
def resolved_names() -> Iterator[None]:
    """``app.`` y ``nodes.`` resuelven a la IP de la prueba (server-casa: MagicDNS o /etc/hosts)."""
    real = socket.getaddrinfo

    def getaddrinfo(host: Any, *arguments: Any, **keywords: Any) -> Any:
        mapped = LOOPBACK if isinstance(host, str) and host.endswith(f".{DOMAIN}") else host
        return real(mapped, *arguments, **keywords)

    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = real  # type: ignore[assignment]


def tls_context(deployment: Deployment, node: Node | None = None) -> ssl.SSLContext:
    """Confía solo en la raíz del balanceador; con ``node``, presenta su certificado de cliente.

    El contexto se arma a mano: con ``httpx.Client(cert=...)`` y una ruta en ``verify`` el
    certificado de cliente no llegó al balanceador (``client_certificate: false`` en su registro).
    """
    assert deployment.edge_ca is not None
    context = ssl.create_default_context(cafile=str(deployment.edge_ca))
    if node is not None:
        context.load_cert_chain(str(node.certificate), str(node.key))
    return context


def app_url(deployment: Deployment, path: str = "") -> str:
    return f"https://app.{DOMAIN}:{deployment.app_port}{path}"


def nodes_url(deployment: Deployment, path: str = "") -> str:
    return f"https://nodes.{DOMAIN}:{deployment.nodes_port}{path}"


@pytest.fixture(scope="module")
def installed(deployment: Deployment) -> tuple[str, str]:
    """``bootstrap.sh --first-install`` y una segunda ejecución sin la marca: sus dos salidas."""
    first = deployment.script("bootstrap.sh", "--first-install")
    assert first.returncode == 0, first.stderr[-4000:]
    second = deployment.script("bootstrap.sh")
    assert second.returncode == 0, second.stderr[-4000:]
    deployment.compose("cp", f"{EDGE}:/edge/edge-ca.pem", str(deployment.directory / "edge-ca.pem"))
    deployment.edge_ca = deployment.directory / "edge-ca.pem"
    return first.stdout, second.stdout


def enroll_node(deployment: Deployment) -> Node:
    """Un código de alta (la persona instaladora) y la alta por ``POST /api/nodes/enrollment``."""
    issued = deployment.seed("issue-code")
    assert deployment.edge_ca is not None
    key = ec.generate_private_key(ec.SECP256R1())
    node = str(issued["node_id"])
    version = str(CONTRACT_VERSION)
    body = {
        "enrollment_code": issued["enrollment_code"],
        "key_algorithm": "ecdsa_p256",
        "certificate_signing_request": csr_pem(node, key=key),
        "server_certificate_signing_request": csr_pem(node, names=[local_ip()]),
        "software_version": "1.0.0",
        "contract_version": version,
        "hardware_fingerprint": secrets.token_hex(32),
        "requested_at": _stamp(CLOCK.now()),
    }
    response = httpx.post(
        app_url(deployment, "/api/nodes/enrollment"),
        json=body,
        headers={"X-Vigia-Contract-Version": version},
        verify=tls_context(deployment),
        timeout=HTTP_SECONDS,
    )
    assert response.status_code == 200, response.text
    certificate = x509.load_pem_x509_certificate(response.json()["certificate"].encode())
    certificate_file = deployment.directory / f"nodo-{node}.crt"
    key_file = deployment.directory / f"nodo-{node}.key"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.touch(mode=0o600)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return Node(
        uuid.UUID(node),
        uuid.UUID(str(issued["organization_id"])),
        uuid.UUID(str(issued["plant_id"])),
        uuid.UUID(str(issued["zone_id"])),
        certificate_file,
        key_file,
    )


def heartbeat(deployment: Deployment, node: Node) -> httpx.Response:
    """``POST /api/nodes/heartbeats`` por ``nodes.`` con el certificado de cliente del nodo."""
    assert deployment.edge_ca is not None
    version = str(CONTRACT_VERSION)
    body = {
        "heartbeat_id": str(uuid7()),
        "contract_version": version,
        "organization_id": str(node.organization_id),
        "plant_id": str(node.plant_id),
        "node_id": str(node.node_id),
        "sent_at": _stamp(CLOCK.now()),
        "node_clock": {"synchronized": True, "offset_ms": 12, "source": "ntp_local"},
        "software_version": "1.0.0",
        "model_version": "modelo-integracion-1",
        "uptime_seconds": 3600,
        # Las dos cámaras del catálogo de la zona (seed.py las deriva con uuid5 de la zona).
        "cameras": [
            {
                "camera_id": str(uuid.uuid5(node.zone_id, f"camara-{index}")),
                "connected": True,
                "measured_fps": 12.0,
                "observability_state": "observable",
                "declared_min_fps": 5.0,
            }
            for index in (0, 1)
        ],
        "zones": [
            {
                "zone_id": str(node.zone_id),
                "mode": "productive",
                "observability_state": "observable",
                "catalog_version": 1,
                "gate_state_valid_until": _stamp(CLOCK.now() + dt.timedelta(days=7)),
                "open_episodes": 0,
            }
        ],
        "signal_reader": {"available": True, "adapter": "modbus_rtu"},
        "local_queue": {"pending": 0, "dead_letter": [], "retained_sent": 0},
    }
    with httpx.Client(verify=tls_context(deployment, node), timeout=HTTP_SECONDS) as client:
        return client.post(
            nodes_url(deployment, "/api/nodes/heartbeats"),
            json=body,
            headers={"X-Vigia-Contract-Version": version},
        )


def eventually(check: Callable[[], bool], message: str) -> None:
    deadline = CLOCK.monotonic() + SETTLE_SECONDS
    while CLOCK.monotonic() < deadline:
        if check():
            return
        time.sleep(1.0)
    raise AssertionError(message)


def person_inventory(deployment: Deployment) -> list[Mapping[str, Any]]:
    """``GET /fleet/nodes`` por ``app.`` con la sesión de la administradora (lo que lee SCR-07)."""
    assert deployment.edge_ca is not None
    cookie = deployment.seed("session", "administrator")["cookie"]
    response = httpx.get(
        app_url(deployment, "/fleet/nodes"),
        headers={"Cookie": f"{SESSION_COOKIE}={cookie}"},
        verify=tls_context(deployment),
        timeout=HTTP_SECONDS,
    )
    assert response.status_code == 200, response.text
    return list(response.json()["nodes"])


@pytest.fixture(scope="module")
def enrolled(deployment: Deployment, installed: tuple[str, str], resolved_names: None) -> Node:
    return enroll_node(deployment)


# --- Criterio 1: levanta, idempotente, nada en 0.0.0.0 -------------------------------------------


def test_bootstrap_twice_gives_the_same_output(installed: tuple[str, str]) -> None:
    first, second = installed
    assert first == second
    assert first.rstrip().endswith("BOOTSTRAP_OK")
    fields = dict(line.split(": ", 1) for line in first.splitlines() if ": " in line)
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", fields["proveedora"]
    )
    assert re.fullmatch(r"[0-9a-f]{64}", fields["raiz_de_nodos"])


def test_only_the_edge_listens_and_only_on_the_tailnet_address(
    deployment: Deployment, installed: tuple[str, str]
) -> None:
    published = deployment.published_ports()
    assert sorted(published[EDGE]) == sorted(
        [(LOOPBACK, deployment.app_port), (LOOPBACK, deployment.nodes_port)]
    )
    for service, ports in published.items():
        if service != EDGE:
            assert ports == [], f"{service} publica {ports}"
    assert {"vigia-api-1", "vigia-api-2", "vigia-worker", "postgres", "localstack"} <= set(
        published
    )
    internal = _docker(
        "network", "inspect", f"{deployment.project}_interna", "--format", "{{.Internal}}"
    )
    assert internal.stdout.strip() == "true"


def test_the_people_api_lists_the_site_and_the_roles_have_credentials(
    deployment: Deployment, installed: tuple[str, str], resolved_names: None
) -> None:
    assert deployment.edge_ca is not None
    ready = httpx.get(
        app_url(deployment, "/health/ready"), verify=tls_context(deployment), timeout=HTTP_SECONDS
    )
    assert ready.json() == {"status": "ready"}
    credentials_file = Path(deployment.environment["VIGIA_INTEGRATION_CREDENTIALS"])
    assert credentials_file.stat().st_mode & 0o777 == 0o600
    credentials = json.loads(credentials_file.read_text(encoding="utf-8"))
    assert set(credentials) == {
        "administrator",
        "coordinator_sst",
        "copasst",
        "line_manager",
        "plant_manager",
        "provider_installer",
    }
    assert deployment.inventory_counts()[1] == 2


# --- Criterio 2: alta por app., latido por nodes. ------------------------------------------------


def test_a_node_enrolls_through_app_and_beats_through_nodes(
    deployment: Deployment, enrolled: Node
) -> None:
    response = heartbeat(deployment, enrolled)
    assert response.status_code == 200, response.text
    nodes = {str(item["node_id"]): item for item in person_inventory(deployment)}
    assert nodes[str(enrolled.node_id)]["communication_state"] == "reachable"


def test_a_call_without_a_node_certificate_is_not_a_node(
    deployment: Deployment, enrolled: Node
) -> None:
    assert deployment.edge_ca is not None
    response = httpx.post(
        nodes_url(deployment, "/api/nodes/heartbeats"),
        json={},
        headers={"X-Vigia-Contract-Version": str(CONTRACT_VERSION)},
        verify=tls_context(deployment),
        timeout=HTTP_SECONDS,
    )
    assert response.status_code in {401, 403}, response.text


# --- Criterio 3: reiniciar LocalStack no pierde la credencial del nodo ---------------------------


def test_restarting_localstack_keeps_the_node_credential_and_the_authority(
    deployment: Deployment, enrolled: Node, installed: tuple[str, str]
) -> None:
    root_before = deployment.root_sha256()
    program = (
        "import boto3;k=boto3.client('kms',endpoint_url='http://localhost:4566',region_name='us-east-1',"
        "aws_access_key_id='test',aws_secret_access_key='test');"
        "import sys;sys.stdout.write(k.encrypt(KeyId='alias/vigia-secrets',"
        "Plaintext=b'segundo-factor')"
        "['CiphertextBlob'].hex())"
    )
    ciphertext = deployment.compose(
        "exec", "-T", "localstack", "python3", "-c", program
    ).stdout.strip()
    time.sleep(12.0)  # la copia periódica (VIGIA_SAVE_SECONDS=10) ya vio lo último
    deployment.compose("restart", "localstack")
    deployment.wait_healthy()
    # La raíz de los nodos y el segundo factor de las personas sobreviven (misma clave KMS).
    assert deployment.root_sha256() == root_before
    decrypt = (
        "import boto3,sys;k=boto3.client('kms',endpoint_url='http://localhost:4566',region_name='us-east-1',"
        "aws_access_key_id='test',aws_secret_access_key='test');"
        "sys.stdout.write(k.decrypt(CiphertextBlob=bytes.fromhex(sys.argv[1]))['Plaintext'].decode())"
    )
    plain = deployment.compose(
        "exec", "-T", "localstack", "python3", "-c", decrypt, ciphertext
    ).stdout
    assert plain == "segundo-factor"
    # El mismo nodo, con su credencial de antes, sigue latiendo.
    eventually(
        lambda: heartbeat(deployment, enrolled).status_code == 200, "el nodo no volvió a latir"
    )
    # Y la receta de reinicio, sin --first-install, deja la misma autoridad.
    again = deployment.script("bootstrap.sh")
    assert again.returncode == 0, again.stderr[-4000:]
    assert again.stdout == installed[0], "la receta de reinicio cambió la raíz de los nodos"
    assert heartbeat(deployment, enrolled).status_code == 200


def test_the_signing_secrets_come_back_with_the_same_arn(
    deployment: Deployment, enrolled: Node
) -> None:
    """La base guarda el ARN de cada clave de firma: tras el reinicio ``vigia-api`` las halla."""
    rows = deployment.sql("SELECT count(*) FROM identity.signing_key WHERE status = 'active'")
    assert int(rows) == 5
    ready = deployment.compose(
        "exec",
        "-T",
        "vigia-api-1",
        "python",
        "-c",
        "import urllib.request;"
        "print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').status)",
    )
    assert ready.stdout.strip() == "200"


# --- Criterio 4: copia y restauración dejan el inventario igual ----------------------------------


def test_backup_and_restore_keep_the_fleet_inventory(
    deployment: Deployment, enrolled: Node
) -> None:
    before = deployment.inventory_counts()
    backup = deployment.script("backup.sh")
    assert backup.returncode == 0, backup.stderr[-4000:]
    dump = Path(backup.stdout.strip().splitlines()[-1])
    assert dump.stat().st_mode & 0o777 == 0o600
    # Dos copias a la vez (el cron y una a mano): cada una es suya y entera.
    with ThreadPoolExecutor(max_workers=2) as pool:
        twins = [
            done.result() for done in [pool.submit(deployment.script, "backup.sh") for _ in "ab"]
        ]
    assert [item.returncode for item in twins] == [0, 0], [item.stderr[-500:] for item in twins]
    paths = {Path(item.stdout.strip().splitlines()[-1]) for item in twins}
    assert len(paths) == 2 and dump not in paths, "dos copias a la vez se pisaron"
    for path in paths:
        assert path.stat().st_size > 0 and path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in dump.parent.iterdir() if p.name.startswith(".")) == []
    deployment.seed("issue-code")  # un nodo más después de la copia
    assert deployment.inventory_counts()[0] == before[0] + 1
    refused = deployment.script("restore.sh", str(dump))
    assert refused.returncode != 0, "restaurar sin --yes no debe hacer nada"
    assert deployment.inventory_counts()[0] == before[0] + 1
    restored = deployment.script("restore.sh", "--yes", str(dump))
    assert restored.returncode == 0, restored.stderr[-4000:]
    assert deployment.inventory_counts() == before
    # El nodo dado de alta antes de la copia sigue latiendo con su credencial.
    eventually(
        lambda: heartbeat(deployment, enrolled).status_code == 200,
        "el nodo no latió tras restaurar",
    )


# --- Requisito 4: sin la clave, la receta falla y no publica otra autoridad ----------------------


def test_without_the_authority_key_the_restart_recipe_fails_and_publishes_nothing(
    deployment: Deployment, enrolled: Node
) -> None:
    root_before = deployment.root_sha256()
    deployment.compose(
        "--profile", "app", "stop", EDGE, "vigia-api-1", "vigia-api-2", "vigia-worker"
    )
    deployment.compose("run", "--rm", "-T", "vigia-admin", "rm", "-f", "/ca/node-ca.pem")
    failed = deployment.script("bootstrap.sh")
    assert failed.returncode != 0
    assert "BOOTSTRAP_OK" not in failed.stdout
    assert "first-install" in failed.stderr
    assert deployment.root_sha256() == root_before, "publicó otra autoridad"
    absent = deployment.compose(
        "run", "--rm", "-T", "vigia-admin", "sh", "-c", "test ! -e /ca/node-ca.pem && echo ausente"
    )
    assert absent.stdout.strip() == "ausente", "la receta de reinicio creó una clave nueva"
    # Con la marca explícita, en cambio, es una decisión deliberada: no se prueba aquí.
