"""Comprobaciones de despliegue de ``staging-<n>`` y ``pilot`` (TASK-151; §3.2 del despliegue).

Las corre ``release.yml`` tras desplegar ``staging-<n>`` y ``pilot``, y ``rollback.yml`` tras
revertir. Solo biblioteca estándar y la CLI ``aws`` del runner (con las credenciales temporales
de ``vigia-deploy``), así que corre con ``uv run --no-project`` sin instalar el backend.

Lista de §3.2 leída como fija la revisión de coherencia (nota T-07, U02-H-07) y ampliada por A-27:

1. ``GET /health/live`` por ``app.`` responde ``200``; por ``nodes.`` solo la conexión TCP.
2. ``HealthyHostCount`` de ``tg-api`` igual a las tareas deseadas de ``vigia-api`` (sondeo
   interno: una petición externa a ``/health/ready`` por ``app.`` recibe ``404``).
3. ``nodes.`` sin certificado: rechazo TLS. Con un certificado de prueba de la raíz del entorno
   (alta de U-03): ``401`` de la aplicación.
4. Suite de conformidad de U-01 contra ``nodes.`` con el runner del kit (TASK-230).
5. ``staging``: activación de la invitación del arranque, inicio de sesión con segundo factor y
   ``GET /me``. ``pilot``: ``GET /me`` con una sesión del dueño, que hace el dueño (``manual``).
6. ``/.well-known/vigia-checkpoint-keys`` y ``/.well-known/vigia-verifier`` responden y el hash del
   verificador es el del artefacto del release.
7. Ninguna alarma del despliegue en ``ALARM`` durante 15 minutos (la última, por su duración).
   Incluye las de U-04: se consultan todas las alarmas ``vigia-``, no una lista de U-02.
8. ``staging``: recorrido de Playwright (U-05). 9. ``pilot``: ``GET /`` y ``version.json`` (U-05).
10 a 13. U-03: certificado revocado en dos capas, catálogo firmado, alta por ``app.`` y rechazo
   por ``nodes.``, origen cruzado y ``PUT`` sin suma.

**Material de las comprobaciones de U-03** (TASK-234). Las nº 3 (segunda mitad), 10 y 11 necesitan
un **certificado de prueba** de la autoridad del entorno (uno vigente y uno revocado) y, la nº 11,
la zona sintética. El entorno los publica en el secreto ``vigia/<entorno>/deploy-checks`` de
Secrets Manager; si faltan, la comprobación sale ``skipped`` con el motivo (nunca en silencio).
La nº 4 corre el runner del kit (``vigia-conformance``) contra ``https://nodes.<dominio>``; si el
kit no está instalado, sale ``skipped``. La nº 13 pide la URL prefirmada a ``POST /documents``
(reutiliza la sesión de la nº 5) y comprueba el origen cruzado y la suma del ``PUT``.

**Omisión con motivo** (TASK-151: «las que dependen de rutas de U-03 se omiten con motivo hasta
que existan»): una comprobación cuyo requisito todavía no está en el repositorio (la ruta en
``openapi/app.yaml`` o el directorio ``frontend/``) sale ``skipped`` con el motivo. Si el requisito
ya existe y la comprobación todavía no está escrita, sale ``failed``: nunca se omite en silencio
algo que ya se puede comprobar.

**Evidencia local sin AWS** (VIG-352). ``--target`` apunta las comprobaciones al balanceador
``app.`` del mundo de U-05 (``frontend/tests/e2e/README.md``); ``--only 8`` o ``--only 9`` limita
la ejecución (las demás salen ``skipped`` con motivo). Con ``--target``:

- ``--fixtures-file`` sustituye al secreto ``vigia/<entorno>/deploy-checks``: el mundo escribe
  ahí, con la misma forma, una invitación temporal de la organización sintética. Se rechaza si no
  es un archivo regular sin permisos de grupo ni de otros; su contenido nunca se informa.
- ``--ca-file`` es la raíz efímera del balanceador: verifica el TLS de las peticiones HTTP y
  fija, para Chromium, solo la clave pública del certificado que esa raíz verificó.
- la nº 9 sustituye la comprobación ``vigia-app-index`` de Route 53 por una sonda local
  equivalente (``HTTPS_STR_MATCH``: ``app_version`` en los primeros 5120 bytes de
  ``/version.json``, respuesta 2xx o 3xx).

Salida: informe JSON (``--report``) y tabla en Markdown para ``$GITHUB_STEP_SUMMARY``
(``--summary``). Código 0 si ninguna falla; 1 si alguna falla; 2 por uso.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol

Status = Literal["ok", "failed", "skipped", "manual"]

ENROLLMENT_ROUTES: Final = ("/nodes/enrollment", "/api/nodes/enrollment")
"""Rutas de U-03 que habilitan las comprobaciones nº 3 (segunda mitad) y 10 a 13."""
CONTRACT_PREFIXES: Final = ("/api/nodes/",)
"""Rutas del contrato de U-01 servidas por la plataforma (``NODE_PREFIX``): habilitan la nº 4.
``/nodes/{node_id}/…`` son rutas de **personas** de SCR-07 (TASK-218), no del contrato."""
NOTICE_VERSION_FALLBACK: Final = "1"
ALARM_POLL_SECONDS: Final = 60
METRIC_WINDOW: Final = timedelta(minutes=5)
TIMEOUT_SECONDS: Final = 15
PLAYWRIGHT_TIMEOUT_SECONDS: Final = 150
"""Arranque de Chromium y el recorrido (tope de 90 s, con la espera de un paso TOTP nuevo)."""
CHECK_NUMBERS: Final = frozenset(range(1, 14))
APP_INDEX_PATH: Final = "/version.json"
APP_INDEX_SEARCH: Final = "app_version"
"""``vigia-app-index``: los mismos valores de ``infra/stacks/observability.py``."""
APP_INDEX_SEARCH_BYTES: Final = 5120
"""Route 53 busca la cadena de ``HTTPS_STR_MATCH`` solo en los primeros 5120 bytes."""
_PATH_LINE: Final = re.compile(r'^  "(/[^"]*)":\s*$')
_HEX64: Final = re.compile(r"^[0-9a-f]{64}$")
_STAGING: Final = re.compile(r"^staging-[1-9][0-9]{0,8}$")


@dataclass(frozen=True)
class Result:
    number: int
    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class Http(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response: ...


class Aws(Protocol):
    def __call__(self, *arguments: str) -> Any: ...


class Network(Protocol):
    def tcp_connects(self, host: str, port: int) -> bool: ...

    def tls_without_certificate_rejected(self, host: str, port: int) -> bool: ...

    def request_with_certificate(
        self,
        host: str,
        port: int,
        method: str,
        path: str,
        certificate: Certificate,
    ) -> Response | None: ...

    def tls_with_certificate_rejected(
        self, host: str, port: int, certificate: Certificate
    ) -> bool: ...


@dataclass(frozen=True)
class Certificate:
    """Material de cliente (PEM) de un certificado de prueba de la autoridad del entorno."""

    certificate_pem: str
    key_pem: str


@dataclass(frozen=True)
class Fixtures:
    """Lo que el entorno publica para las comprobaciones de U-03 y U-05."""

    valid_certificate: Certificate | None = None
    revoked_certificate: Certificate | None = None
    synthetic_zone_id: str | None = None
    frontend: FrontendCredentials | None = None


@dataclass(frozen=True)
class FrontendCredentials:
    """Material de un solo uso para activar la invitación sintética desde Playwright.

    El enlace lleva el secreto de invitación. Nunca se incorpora al informe de comprobaciones ni a
    la salida del subproceso: solo se entrega en un archivo temporal con permisos privados.
    """

    invitation_link: str
    email: str


class FixtureSource(Protocol):
    def load(self, environment: str) -> Fixtures: ...


@dataclass(frozen=True)
class ConformanceVerdict:
    passed: bool
    detail: str


class Conformance(Protocol):
    def run(self, target_url: str) -> ConformanceVerdict: ...


@dataclass(frozen=True)
class PlaywrightVerdict:
    passed: bool
    detail: str = ""


class Playwright(Protocol):
    def run(
        self, target_url: str, credentials: FrontendCredentials
    ) -> PlaywrightVerdict | None: ...


# --- Adaptadores reales -----------------------------------------------------------------------


class UrllibHttp:
    """HTTPS con verificación de certificado y sesión por cookie (comprobación nº 5).

    ``ca_file`` (solo con ``--target``) sustituye a las raíces del sistema por la raíz efímera
    del mundo local: se sigue verificando la cadena y el nombre."""

    def __init__(self, ca_file: Path | None = None) -> None:
        context = ssl.create_default_context(cafile=None if ca_file is None else str(ca_file))
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context),
        )

    def request(
        self,
        method: str,
        url: str,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        if not url.startswith("https://"):
            raise ValueError(f"solo HTTPS: {url}")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - solo https
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as answer:
                return Response(answer.status, answer.read(), dict(answer.headers.items()))
        except urllib.error.HTTPError as error:
            return Response(error.code, error.read(), dict(error.headers.items()))


def aws_cli(*arguments: str) -> Any:
    """Llama a ``aws`` con salida JSON (credenciales temporales de ``vigia-deploy``)."""
    completed = subprocess.run(
        ["aws", *arguments, "--output", "json"],  # noqa: S607 - CLI del runner
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


class SocketNetwork:
    def tcp_connects(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT_SECONDS):
                return True
        except OSError:
            return False

    def tls_without_certificate_rejected(self, host: str, port: int) -> bool:
        """El balanceador con autenticación mutua corta la negociación sin certificado de
        cliente. Con TLS 1.3 el rechazo puede llegar en la primera lectura, así que se lee."""
        context = ssl.create_default_context()
        try:
            with (
                socket.create_connection((host, port), timeout=TIMEOUT_SECONDS) as raw,
                context.wrap_socket(raw, server_hostname=host) as tls,
            ):
                tls.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                return tls.recv(1) == b""
        except (ssl.SSLError, ConnectionResetError, BrokenPipeError):
            return True
        except OSError:
            return False

    def _client_context(self, certificate: Certificate) -> ssl.SSLContext:
        """Contexto con el certificado de cliente y sin verificar la cadena (autoridad propia)."""
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as chain:
            chain.write(certificate.certificate_pem)
            chain_path = chain.name
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as key:
            key.write(certificate.key_pem)
            key_path = key.name
        try:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.load_cert_chain(chain_path, key_path)
            return context
        finally:
            Path(chain_path).unlink(missing_ok=True)
            Path(key_path).unlink(missing_ok=True)

    def request_with_certificate(
        self,
        host: str,
        port: int,
        method: str,
        path: str,
        certificate: Certificate,
    ) -> Response | None:
        """Petición HTTP con el certificado de prueba; ``None`` si el saludo se rechaza."""
        context = self._client_context(certificate)
        try:
            with (
                socket.create_connection((host, port), timeout=TIMEOUT_SECONDS) as raw,
                context.wrap_socket(raw, server_hostname=host) as tls,
            ):
                tls.sendall(
                    (
                        f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
                    ).encode()
                )
                data = b""
                while True:
                    chunk = tls.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except (ssl.SSLError, ConnectionResetError, BrokenPipeError):
            return None
        except OSError:
            return None
        head, _, body = data.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        return Response(int(parts[1]), body)

    def tls_with_certificate_rejected(self, host: str, port: int, certificate: Certificate) -> bool:
        """Tras el barrido, el balanceador rechaza la conexión con el certificado revocado."""
        context = self._client_context(certificate)
        try:
            with (
                socket.create_connection((host, port), timeout=TIMEOUT_SECONDS) as raw,
                context.wrap_socket(raw, server_hostname=host) as tls,
            ):
                tls.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                return tls.recv(1) == b""
        except (ssl.SSLError, ConnectionResetError, BrokenPipeError):
            return True
        except OSError:
            return False


class SecretsFixtures:
    """Certificados de prueba y zona sintética que el entorno publica en Secrets Manager."""

    def __init__(self, aws: Aws) -> None:
        self._aws = aws

    def load(self, environment: str) -> Fixtures:
        try:
            secret = self._aws(
                "secretsmanager",
                "get-secret-value",
                "--secret-id",
                f"vigia/{environment}/deploy-checks",
            )
        except subprocess.CalledProcessError:
            return Fixtures()
        return _parsed_fixtures(secret.get("SecretString") or "{}")


class FileFixtures:
    """El material del secreto, leído de un archivo privado local (``--fixtures-file``).

    Solo para la evidencia local con ``--target``: lo escribe el mundo de U-05 dentro de su
    directorio ``0700``. ``private_file`` ya rechazó enlaces, no regulares y permisos abiertos.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, environment: str) -> Fixtures:
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return Fixtures()
        return _parsed_fixtures(text)


def _parsed_fixtures(text: str) -> Fixtures:
    """Material con la forma del secreto; un documento ilegible deja todo sin material."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return Fixtures()
    if not isinstance(data, dict):
        return Fixtures()
    zone = data.get("synthetic_zone_id")
    return Fixtures(
        valid_certificate=_certificate(data.get("valid")),
        revoked_certificate=_certificate(data.get("revoked")),
        synthetic_zone_id=zone if isinstance(zone, str) and zone else None,
        frontend=_frontend_credentials(data.get("frontend")),
    )


def _certificate(entry: object) -> Certificate | None:
    if not isinstance(entry, dict):
        return None
    chain = entry.get("certificate_pem")
    key = entry.get("key_pem")
    if not isinstance(chain, str) or not isinstance(key, str) or not chain or not key:
        return None
    return Certificate(certificate_pem=chain, key_pem=key)


def _frontend_credentials(entry: object) -> FrontendCredentials | None:
    if not isinstance(entry, dict):
        return None
    link = entry.get("invitation_link")
    email = entry.get("email")
    if not isinstance(link, str) or not isinstance(email, str) or not link or not email:
        return None
    return FrontendCredentials(invitation_link=link, email=email)


class KitConformance:
    """Runner del kit de conformidad de U-01 (``vigia-conformance``, TASK-230)."""

    def run(self, target_url: str) -> ConformanceVerdict:
        executable = shutil.which("vigia-conformance")
        if executable is None:
            return ConformanceVerdict(
                passed=False,
                detail="el kit vigia-conformance no está instalado en el runner (TASK-230)",
            )
        completed = subprocess.run(
            [executable, "run", "--target", target_url, "--profile", "ci"],
            capture_output=True,
            text=True,
            check=False,
        )
        detail = (completed.stdout + completed.stderr).strip().splitlines()
        summary = detail[-1] if detail else ""
        return ConformanceVerdict(passed=completed.returncode == 0, detail=summary)


def _der_element(data: bytes, offset: int) -> tuple[int, int]:
    """Inicio del contenido y fin del elemento DER que empieza en ``offset``."""
    length = data[offset + 1]
    start = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if not 0 < count <= 4:
            raise ValueError("longitud DER no admitida")
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    end = start + length
    if end > len(data):
        raise ValueError("elemento DER truncado")
    return start, end


def spki_sha256(certificate_der: bytes) -> str:
    """SHA-256 en base64 de ``subjectPublicKeyInfo`` (formato de Chromium para fijar claves).

    ``Certificate ::= SEQUENCE { tbsCertificate, ... }`` y ``tbsCertificate`` es
    ``[0] version`` opcional, serie, algoritmo, emisor, validez, sujeto y ``subjectPublicKeyInfo``.
    """
    try:
        tbs, _ = _der_element(certificate_der, 0)
        position, _ = _der_element(certificate_der, tbs)
        if certificate_der[position] == 0xA0:
            position = _der_element(certificate_der, position)[1]
        for _skipped in range(5):
            position = _der_element(certificate_der, position)[1]
        if certificate_der[position] != 0x30:
            raise ValueError("subjectPublicKeyInfo no es una secuencia")
        spki = certificate_der[position : _der_element(certificate_der, position)[1]]
    except IndexError:
        raise ValueError("certificado DER truncado") from None
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def verified_certificate_pin(target_url: str, ca_file: Path) -> str:
    """Huella de la clave del certificado que presenta ``target_url``, verificado con ``ca_file``.

    Chromium no admite una raíz propia sin tocar el almacén del usuario; en cambio acepta
    ``--ignore-certificate-errors-spki-list`` para una clave concreta. Solo se fija la clave de
    un certificado que la raíz efímera ya verificó (cadena y nombre)."""
    parsed = urllib.parse.urlparse(target_url)
    host = parsed.hostname or ""
    context = ssl.create_default_context(cafile=str(ca_file))
    with (
        socket.create_connection((host, parsed.port or 443), timeout=TIMEOUT_SECONDS) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        certificate = tls.getpeercert(binary_form=True)
    if not certificate:
        raise ssl.SSLError("el destino no presentó certificado")
    return spki_sha256(certificate)


class LocalPlaywright:
    """Ejecuta solo el recorrido de despliegue cuando el árbol ya tiene Playwright instalado.

    Con ``ca_file`` (mundo local, ``--target``) Chromium acepta solo la clave del certificado
    que esa raíz verificó (``VIGIA_DEPLOY_CHECK_SPKI``); sin ella, el almacén del sistema."""

    def __init__(self, repository: Path, ca_file: Path | None = None) -> None:
        self._frontend = repository / "frontend"
        self._ca_file = ca_file

    def run(self, target_url: str, credentials: FrontendCredentials) -> PlaywrightVerdict | None:
        node = shutil.which("node")
        cli = self._frontend / "node_modules" / "@playwright" / "test" / "cli.js"
        if node is None or not cli.is_file():
            return None
        pin: dict[str, str] = {}
        if self._ca_file is not None:
            try:
                pin["VIGIA_DEPLOY_CHECK_SPKI"] = verified_certificate_pin(target_url, self._ca_file)
            except (OSError, ValueError):
                return PlaywrightVerdict(
                    passed=False, detail="el destino no presentó un certificado de --ca-file"
                )
        descriptor, credentials_path = tempfile.mkstemp(
            prefix="vigia-deploy-check-", suffix=".json"
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "invitation_link": credentials.invitation_link,
                        "email": credentials.email,
                    },
                    handle,
                )
            environment = {
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key != "VIGIA_DEPLOY_CHECK_SPKI"
                },
                **pin,
                "VIGIA_DEPLOY_CHECK_TARGET": target_url,
                "VIGIA_DEPLOY_CHECK_CREDENTIALS_FILE": credentials_path,
            }
            completed = subprocess.run(
                [
                    node,
                    str(cli),
                    "test",
                    "--config",
                    "tests/e2e/deploy_check_staging.config.ts",
                ],
                cwd=self._frontend,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
                timeout=PLAYWRIGHT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return PlaywrightVerdict(
                passed=False,
                detail=f"el proceso agotó el límite de {PLAYWRIGHT_TIMEOUT_SECONDS} s",
            )
        finally:
            Path(credentials_path).unlink(missing_ok=True)
        # stdout y stderr pueden contener valores escritos por una dependencia;
        # nunca llegan al informe.
        return PlaywrightVerdict(
            passed=completed.returncode == 0,
            detail=f"el proceso terminó con código {completed.returncode}",
        )


# --- Contexto -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    environment: str
    domain: str
    verifier_sha256: str
    repository: Path
    now: datetime
    watch_minutes: int = 15
    operator_email: str | None = None  # Correo del primer operador invitado (nº 5).
    app_version: str | None = None
    target_url: str | None = None  # Origen HTTPS alternativo para el recorrido local.

    @property
    def staging(self) -> bool:
        return self.environment != "pilot"

    @property
    def suffix(self) -> str:
        return f"-{self.environment}" if self.staging else ""

    @property
    def app_host(self) -> str:
        return f"{self.environment}.{self.domain}" if self.staging else f"app.{self.domain}"

    @property
    def nodes_host(self) -> str:
        return f"{self.environment}-nodes.{self.domain}" if self.staging else f"nodes.{self.domain}"

    @property
    def app_url(self) -> str:
        if self.target_url is not None:
            return self.target_url
        return f"https://{self.app_host}"

    @property
    def cluster(self) -> str:
        return f"vigia-{self.environment}"

    def openapi_paths(self) -> frozenset[str]:
        text = (self.repository / "backend" / "openapi" / "app.yaml").read_text(encoding="utf-8")
        return frozenset(m[1] for line in text.splitlines() if (m := _PATH_LINE.match(line)))

    def has_enrollment(self) -> bool:
        return any(route in self.openapi_paths() for route in ENROLLMENT_ROUTES)

    def has_contract_routes(self) -> bool:
        return any(path.startswith(CONTRACT_PREFIXES) for path in self.openapi_paths())

    def has_frontend(self) -> bool:
        return (self.repository / "frontend" / "package.json").is_file()


# --- Comprobaciones ----------------------------------------------------------------------------


def check_live(target: Target, http: Http, network: Network) -> Result:
    name = "health/live por app. y TCP por nodes."
    answer = http.request("GET", f"{target.app_url}/health/live")
    if answer.status != 200:
        return Result(1, name, "failed", f"GET /health/live por app. respondió {answer.status}")
    if not network.tcp_connects(target.nodes_host, 443):
        return Result(1, name, "failed", f"sin conexión TCP con {target.nodes_host}:443")
    return Result(1, name, "ok", "app. 200; nodes. acepta la conexión TCP")


def check_healthy_hosts(target: Target, aws: Aws) -> Result:
    name = "HealthyHostCount de tg-api igual a las tareas de vigia-api"
    services = aws(
        "ecs", "describe-services", "--cluster", target.cluster, "--services", "vigia-api"
    )
    found = services.get("services") or []
    if not found:
        return Result(2, name, "failed", f"vigia-api no existe en {target.cluster}")
    desired = int(found[0]["desiredCount"])
    groups = aws("elbv2", "describe-target-groups", "--names", f"tg-api{target.suffix}")
    group = (groups.get("TargetGroups") or [{}])[0]
    balancers = group.get("LoadBalancerArns") or []
    if not group or not balancers:
        return Result(2, name, "failed", f"tg-api{target.suffix} sin balanceador asociado")
    dimension_group = group["TargetGroupArn"].split(":", 5)[5]
    dimension_balancer = balancers[0].split(":", 5)[5].removeprefix("loadbalancer/")
    metric = aws(
        "cloudwatch", "get-metric-statistics",
        "--namespace", "AWS/ApplicationELB", "--metric-name", "HealthyHostCount",
        "--dimensions", f"Name=TargetGroup,Value={dimension_group}",
        f"Name=LoadBalancer,Value={dimension_balancer}",
        "--start-time", (target.now - METRIC_WINDOW).isoformat(),
        "--end-time", target.now.isoformat(),
        "--period", "60", "--statistics", "Minimum",
    )  # fmt: skip
    points = sorted(metric.get("Datapoints") or [], key=lambda point: str(point["Timestamp"]))
    if not points:
        return Result(2, name, "failed", "sin datos de HealthyHostCount en los últimos 5 minutos")
    healthy = int(points[-1]["Minimum"])
    if desired < 1 or healthy != desired:
        return Result(2, name, "failed", f"HealthyHostCount {healthy}; tareas deseadas {desired}")
    return Result(2, name, "ok", f"{healthy} de {desired} tareas sanas")


def check_mutual_tls(target: Target, network: Network, fixtures: Fixtures) -> Result:
    name = "autenticación mutua en nodes."
    if not network.tls_without_certificate_rejected(target.nodes_host, 443):
        return Result(3, name, "failed", "nodes. aceptó una conexión TLS sin certificado")
    if not target.has_enrollment():
        return Result(
            3,
            name,
            "skipped",
            "sin certificado: rechazo TLS comprobado. Con certificado de prueba: omitida hasta"
            " el alta de U-03 (/api/nodes/enrollment), que emite certificados de la raíz",
        )
    if fixtures.valid_certificate is None:
        return Result(
            3,
            name,
            "skipped",
            "sin certificado de prueba del entorno (secreto vigia/<entorno>/deploy-checks)",
        )
    answer = network.request_with_certificate(
        target.nodes_host, 443, "GET", "/api/nodes/heartbeats", fixtures.valid_certificate
    )
    if answer is None:
        return Result(3, name, "failed", "nodes. rechazó el certificado de prueba en el saludo TLS")
    if answer.status == 401:
        return Result(3, name, "ok", "con certificado de prueba: 401 de la aplicación")
    return Result(
        3, name, "failed", f"con el certificado de prueba la aplicación respondió {answer.status}"
    )


def check_conformance(target: Target, conformance: Conformance) -> Result:
    name = "conformidad de U-01 contra nodes."
    if not target.has_contract_routes():
        return Result(
            4,
            name,
            "skipped",
            "omitida: la plataforma todavía no expone las rutas del contrato (llegan con U-03)",
        )
    verdict = conformance.run(f"https://{target.nodes_host}")
    if verdict.passed:
        return Result(4, name, "ok", verdict.detail or "la suite del kit pasa contra nodes.")
    return Result(4, name, "failed", verdict.detail or "la suite del kit falla contra nodes.")


def _totp(provisioning_uri: str, now: datetime) -> str:
    """Código TOTP (RFC 6238, SHA-1, 6 cifras, 30 s) de un ``otpauth://`` recién emitido."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(provisioning_uri).query)
    secret = query["secret"][0].upper()
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digits = int(query.get("digits", ["6"])[0])
    period = int(query.get("period", ["30"])[0])
    counter = struct.pack(">Q", int(now.timestamp()) // period)
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def check_session(target: Target, http: Http, aws: Aws, now: Callable[[], datetime]) -> Result:
    name = "inicio de sesión de prueba"
    if not target.staging:
        return Result(5, name, "manual", "pilot: el dueño comprueba GET /me con su sesión")
    if not target.operator_email:
        return Result(5, name, "failed", "staging sin --operator-email del arranque")
    secret = aws(
        "secretsmanager", "get-secret-value",
        "--secret-id", f"vigia/{target.environment}/bootstrap/invitation",
    )  # fmt: skip
    invitation = json.loads(secret["SecretString"])
    email = target.operator_email
    token = str(invitation["link"]).rsplit("#", 1)[-1]
    headers = {"Origin": target.app_url, "Sec-Fetch-Site": "same-origin"}
    accept = f"{target.app_url}/invitations/{urllib.parse.quote(token, safe='')}/accept"
    begun = http.request("POST", accept, {"step": "begin"}, headers)
    if begun.status != 200:
        return Result(5, name, "failed", f"activación (begin) respondió {begun.status}")
    state = begun.json()
    enrollment = state.get("enrollment") or {}
    notice = (state.get("notice") or {}).get("version") or NOTICE_VERSION_FALLBACK
    password = secrets.token_urlsafe(32)
    uri = enrollment.get("provisioning_uri")
    body: dict[str, object] = {"step": "complete", "password": password, "notice_version": notice}
    if uri:
        body["second_factor_code"] = _totp(uri, now())
    completed = http.request("POST", accept, body, headers)
    if completed.status != 200 or completed.json().get("status") != "activated":
        return Result(5, name, "failed", f"activación (complete) respondió {completed.status}")
    login = http.request(
        "POST", f"{target.app_url}/auth/login", {"email": email, "password": password}, headers
    )
    status = login.json().get("status") if login.status == 200 else None
    if status == "second_factor_required" and uri:
        login = http.request(
            "POST", f"{target.app_url}/auth/second-factor", {"code": _totp(uri, now())}, headers
        )
        status = login.json().get("status") if login.status == 200 else None
    if status != "authenticated":
        return Result(5, name, "failed", f"inicio de sesión: HTTP {login.status}, estado {status}")
    me = http.request("GET", f"{target.app_url}/me", headers=headers)
    if me.status != 200:
        return Result(5, name, "failed", f"GET /me respondió {me.status}")
    return Result(5, name, "ok", "invitación activada, sesión iniciada y GET /me 200")


def check_well_known(target: Target, http: Http) -> Result:
    name = "claves de puntos de control y hash del verificador"
    keys = http.request("GET", f"{target.app_url}/.well-known/vigia-checkpoint-keys")
    if keys.status != 200:
        return Result(6, name, "failed", f"vigia-checkpoint-keys respondió {keys.status}")
    keys.json()
    verifier = http.request("GET", f"{target.app_url}/.well-known/vigia-verifier")
    if verifier.status != 200:
        return Result(6, name, "failed", f"vigia-verifier respondió {verifier.status}")
    published = verifier.json().get("sha256")
    if published != target.verifier_sha256:
        return Result(
            6, name, "failed", f"hash publicado {published}; artefacto {target.verifier_sha256}"
        )
    return Result(6, name, "ok", f"verificador {published}")


def _own_alarm(target: Target, alarm: str) -> bool:
    if target.staging:
        return alarm.endswith(target.suffix)
    return "-staging-" not in alarm


def check_alarms(target: Target, aws: Aws, sleep: Callable[[float], None]) -> Result:
    name = f"ninguna alarma en ALARM durante {target.watch_minutes} minutos"
    for poll in range(target.watch_minutes + 1):
        if poll:
            sleep(ALARM_POLL_SECONDS)
        answer = aws(
            "cloudwatch",
            "describe-alarms",
            "--state-value",
            "ALARM",
            "--alarm-name-prefix",
            "vigia-",
        )
        firing = sorted(
            alarm["AlarmName"]
            for alarm in (answer.get("MetricAlarms") or []) + (answer.get("CompositeAlarms") or [])
            if _own_alarm(target, alarm["AlarmName"])
        )
        if firing:
            return Result(7, name, "failed", f"en ALARM al minuto {poll}: {', '.join(firing)}")
    return Result(7, name, "ok", f"{target.watch_minutes + 1} sondeos sin alarmas en ALARM")


def check_playwright(target: Target, fixtures: Fixtures, playwright: Playwright) -> Result:
    name = "recorrido de Playwright con la organización sintética"
    if not target.staging:
        return Result(8, name, "skipped", "solo en staging")
    if not target.has_frontend():
        return Result(8, name, "skipped", "omitida: el repositorio no tiene frontend/ (U-05)")
    if fixtures.frontend is None:
        return Result(
            8,
            name,
            "skipped",
            "sin invitación y correo de prueba del entorno (secreto vigia/<entorno>/deploy-checks"
            " o --fixtures-file)",
        )
    verdict = playwright.run(target.app_url, fixtures.frontend)
    if verdict is None:
        return Result(8, name, "skipped", "Playwright no está instalado en el runner")
    if not verdict.passed:
        detail = verdict.detail or "el proceso no informó detalle"
        return Result(8, name, "failed", f"el recorrido de Playwright falló: {detail}")
    return Result(8, name, "ok", "recorrido y política CSP/Trusted Types sin violaciones")


def _header(headers: dict[str, str], name: str) -> str:
    return next((value for key, value in headers.items() if key.lower() == name.lower()), "")


def _initial_asset(index: bytes) -> str | None:
    match = re.search(rb'<script[^>]+src=["\'](/assets/[^"\']+)["\']', index, re.IGNORECASE)
    return None if match is None else match.group(1).decode("ascii", errors="strict")


def _app_index_health_check(target: Target, aws: Aws) -> bool:
    checks = aws("route53", "list-health-checks").get("HealthChecks") or []
    matching = [
        check
        for check in checks
        if check.get("HealthCheckConfig", {}).get("FullyQualifiedDomainName") == target.app_host
        and check.get("HealthCheckConfig", {}).get("ResourcePath") == APP_INDEX_PATH
        and check.get("HealthCheckConfig", {}).get("Type") == "HTTPS_STR_MATCH"
        and check.get("HealthCheckConfig", {}).get("SearchString") == APP_INDEX_SEARCH
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("Id"), str):
        return False
    observations = (
        aws("route53", "get-health-check-status", "--health-check-id", matching[0]["Id"]).get(
            "HealthCheckObservations"
        )
        or []
    )
    statuses = [item.get("StatusReport", {}).get("Status") for item in observations]
    return bool(statuses) and all(status == "Success" for status in statuses)


def local_app_index_probe(target: Target, http: Http) -> bool:
    """Sonda local equivalente a ``vigia-app-index`` (``HTTPS_STR_MATCH`` de Route 53).

    Como Route 53: ``GET`` sin cabeceras de navegación, respuesta 2xx o 3xx y la cadena en los
    primeros ``APP_INDEX_SEARCH_BYTES`` bytes del cuerpo."""
    answer = http.request("GET", f"{target.app_url}{APP_INDEX_PATH}")
    head = answer.body[:APP_INDEX_SEARCH_BYTES]
    return 200 <= answer.status < 400 and APP_INDEX_SEARCH.encode("ascii") in head


def check_app_index(target: Target, http: Http, aws: Aws) -> Result:
    name = "GET / con vigia-app, version.json y paquete immutable"
    if target.staging:
        return Result(9, name, "skipped", "solo en pilot")
    if not target.has_frontend():
        return Result(9, name, "skipped", "omitida: el repositorio no tiene frontend/ (U-05)")
    if target.app_version is None:
        return Result(9, name, "failed", "sin versión de release (--app-version o VERSION)")
    index = http.request("GET", f"{target.app_url}/", headers={"Accept": "text/html"})
    if index.status != 200 or b"vigia-app" not in index.body:
        return Result(
            9, name, "failed", f"GET / respondió {index.status} sin el marcador vigia-app"
        )
    version = http.request("GET", f"{target.app_url}/version.json")
    if version.status != 200:
        return Result(9, name, "failed", f"GET /version.json respondió {version.status}")
    try:
        app_version = version.json().get("app_version")
    except (AttributeError, ValueError, UnicodeDecodeError):
        return Result(9, name, "failed", "GET /version.json no devolvió un objeto JSON")
    if app_version != target.app_version:
        return Result(
            9,
            name,
            "failed",
            f"version.json publicó {app_version!r}; release {target.app_version!r}",
        )
    asset = _initial_asset(index.body)
    if asset is None:
        return Result(9, name, "failed", "GET / no declaró el paquete inicial bajo /assets/")
    package = http.request("GET", f"{target.app_url}{asset}")
    cache_control = _header(package.headers, "Cache-Control").lower()
    if package.status != 200 or "immutable" not in cache_control:
        return Result(9, name, "failed", f"GET {asset} no respondió 200 immutable")
    if target.target_url is not None:
        # Destino local: no hay Route 53; la sonda equivalente consulta el mismo recurso.
        if not local_app_index_probe(target, http):
            return Result(9, name, "failed", "la sonda local equivalente a vigia-app-index falló")
        return Result(
            9,
            name,
            "ok",
            "marcador, versión, paquete immutable y sonda local equivalente a vigia-app-index",
        )
    if not _app_index_health_check(target, aws):
        return Result(9, name, "failed", "la comprobación vigia-app-index no está en OK")
    return Result(9, name, "ok", "marcador, versión, paquete immutable y vigia-app-index en OK")


def _rejection_code(answer: Response) -> str:
    try:
        body = answer.json()
    except (ValueError, UnicodeDecodeError):
        return ""
    return str(body.get("code") or "") if isinstance(body, dict) else ""


def _trust_store_arn(target: Target, aws: Aws) -> str | None:
    answer = aws("elbv2", "describe-trust-stores", "--names", f"vigia-node-trust{target.suffix}")
    stores = answer.get("TrustStores") or []
    if not stores:
        return None
    return str(stores[0].get("TrustStoreArn") or "") or None


def _revoked_entries(target: Target, aws: Aws) -> int | None:
    """El conteo global de la lista (métrica ``revocation_list_entries`` de ``Vigia/Platform``)."""
    answer = aws(
        "cloudwatch", "get-metric-statistics",
        "--namespace", "Vigia/Platform", "--metric-name", "revocation_list_entries",
        "--start-time", (target.now - METRIC_WINDOW).isoformat(),
        "--end-time", target.now.isoformat(),
        "--period", "60", "--statistics", "Maximum",
    )  # fmt: skip
    points = sorted(answer.get("Datapoints") or [], key=lambda point: str(point["Timestamp"]))
    if not points:
        return None
    return int(points[-1]["Maximum"])


def check_revoked_certificate(
    target: Target, network: Network, aws: Aws, fixtures: Fixtures, sleep: Callable[[float], None]
) -> Result:
    name = "certificado revocado rechazado en dos capas"
    reason = "omitida: la ruta de alta de U-03 (/api/nodes/enrollment) todavía no existe"
    if not target.has_enrollment():
        return Result(10, name, "skipped", reason)
    if fixtures.revoked_certificate is None:
        return Result(
            10,
            name,
            "skipped",
            "sin certificado revocado de prueba del entorno (secreto"
            " vigia/<entorno>/deploy-checks)",
        )
    answer = network.request_with_certificate(
        target.nodes_host, 443, "GET", "/api/nodes/heartbeats", fixtures.revoked_certificate
    )
    if answer is None:
        return Result(
            10,
            name,
            "failed",
            "nodes. rechazó el certificado revocado en el saludo TLS antes de la aplicación",
        )
    if answer.status != 403:
        return Result(
            10, name, "failed", f"la aplicación respondió {answer.status} al certificado revocado"
        )
    code = _rejection_code(answer)
    if code != "node_revoked":
        return Result(10, name, "failed", f"la aplicación respondió {code!r}, no node_revoked")
    arn = _trust_store_arn(target, aws)
    if arn is None:
        return Result(
            10, name, "failed", f"sin almacén de confianza vigia-node-trust{target.suffix}"
        )
    expected = _revoked_entries(target, aws)
    entries: int | None = None
    for poll in range(2):
        if poll:
            sleep(ALARM_POLL_SECONDS)
        answer_store = aws("elbv2", "describe-trust-store-revocations", "--trust-store-arn", arn)
        revocations = answer_store.get("TrustStoreRevocations") or []
        entries = sum(int(item.get("NumberOfRevokedEntries") or 0) for item in revocations) or None
        if entries is not None and (expected is None or entries == expected):
            break
    else:
        return Result(
            10,
            name,
            "failed",
            f"el almacén de confianza tiene {entries} revocadas; la base, {expected}",
        )
    if not network.tls_with_certificate_rejected(
        target.nodes_host, 443, fixtures.revoked_certificate
    ):
        return Result(
            10, name, "failed", "el balanceador aceptó el certificado revocado tras el barrido"
        )
    return Result(
        10,
        name,
        "ok",
        f"node_revoked de la aplicación y {entries} revocadas en el almacén de confianza",
    )


def check_signed_catalog(
    target: Target, http: Http, network: Network, fixtures: Fixtures
) -> Result:
    name = "catálogo firmado con certificado vigente"
    reason = "omitida: la ruta de alta de U-03 (/api/nodes/enrollment) todavía no existe"
    if not target.has_enrollment():
        return Result(11, name, "skipped", reason)
    if fixtures.valid_certificate is None or fixtures.synthetic_zone_id is None:
        return Result(
            11,
            name,
            "skipped",
            "sin certificado vigente o zona sintética del entorno (secreto"
            " vigia/<entorno>/deploy-checks)",
        )
    path = f"/api/nodes/zones/{fixtures.synthetic_zone_id}/catalog"
    answer = network.request_with_certificate(
        target.nodes_host, 443, "GET", path, fixtures.valid_certificate
    )
    if answer is None or answer.status != 200:
        status = "saludo TLS rechazado" if answer is None else f"HTTP {answer.status}"
        return Result(11, name, "failed", f"el catálogo de la zona sintética respondió {status}")
    envelope = answer.json()
    keys = http.request("GET", f"{target.app_url}/.well-known/vigia-checkpoint-keys")
    if keys.status != 200:
        return Result(11, name, "failed", f"vigia-checkpoint-keys respondió {keys.status}")
    published = {str(key.get("key_id")) for key in keys.json().get("keys", [])}
    key_id = str(envelope.get("key_id") or "")
    if not key_id or key_id not in published:
        return Result(
            11, name, "failed", f"el sobre cita la clave {key_id!r}, ausente del conjunto publicado"
        )
    return Result(11, name, "ok", f"sobre del catálogo firmado con la clave {key_id}")


def check_enrollment_reachable(target: Target, http: Http, network: Network) -> Result:
    name = "alta por app. y rechazo sin certificado por nodes."
    reason = "omitida: la ruta de alta de U-03 (/api/nodes/enrollment) todavía no existe"
    if not target.has_enrollment():
        return Result(12, name, "skipped", reason)
    answer = http.request("POST", f"{target.app_url}/api/nodes/enrollment")
    if not 400 <= answer.status < 500:
        return Result(
            12,
            name,
            "failed",
            f"POST /api/nodes/enrollment sin cuerpo por app. respondió {answer.status}"
            " (se espera 4xx)",
        )
    if not network.tls_without_certificate_rejected(target.nodes_host, 443):
        return Result(12, name, "failed", "nodes. aceptó la conexión TLS sin certificado")
    return Result(12, name, "ok", f"app. {answer.status} y nodes. rechaza sin certificado")


def check_cross_origin_and_checksum(target: Target, http: Http) -> Result:
    name = "origen cruzado y PUT sin suma rechazado"
    if not target.staging:
        return Result(13, name, "skipped", "solo en staging")
    if not target.has_enrollment():
        return Result(
            13,
            name,
            "skipped",
            "omitida: la ruta de alta de U-03 (/api/nodes/enrollment) todavía no existe",
        )
    grant = http.request(
        "POST",
        f"{target.app_url}/documents",
        {"document_kind": "scope_minutes", "content_type": "application/pdf", "size_bytes": 1},
    )
    if grant.status != 200:
        return Result(13, name, "failed", f"POST /documents respondió {grant.status}")
    upload_url = str(grant.json().get("upload_url") or "")
    if not upload_url.startswith("https://"):
        return Result(13, name, "failed", "POST /documents no devolvió una URL de subida")
    preflight = http.request(
        "OPTIONS",
        upload_url,
        headers={
            "Origin": target.app_url,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "x-amz-checksum-sha256",
        },
    )
    allowed = preflight.headers.get("Access-Control-Allow-Headers", "")
    methods = preflight.headers.get("Access-Control-Allow-Methods", "")
    if "PUT" not in methods.upper() or "x-amz-checksum-sha256" not in allowed.lower():
        return Result(
            13,
            name,
            "failed",
            f"el preflight no autoriza PUT con x-amz-checksum-sha256 ({methods!r}; {allowed!r})",
        )
    put = http.request("PUT", upload_url, b"vigia", headers={"Origin": target.app_url})
    if put.status < 400:
        return Result(
            13,
            name,
            "failed",
            f"el depósito aceptó un PUT sin x-amz-checksum-sha256 ({put.status})",
        )
    return Result(13, name, "ok", f"preflight con CORS y PUT sin suma rechazado ({put.status})")


def run_checks(
    target: Target,
    *,
    http: Http,
    aws: Aws,
    network: Network,
    fixtures: Fixtures | None = None,
    conformance: Conformance | None = None,
    playwright: Playwright | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
    only: frozenset[int] | None = None,
) -> list[Result]:
    """Las trece comprobaciones; la nº 7 al final por sus 15 minutos. Un error inesperado de una
    comprobación la deja ``failed`` con el error, sin impedir las demás. Con ``only`` (``--only``)
    las no seleccionadas no se ejecutan y salen ``skipped`` con ese motivo."""
    clock = now or (lambda: target.now)
    material = fixtures or Fixtures()
    kit = conformance or KitConformance()
    browser = playwright or LocalPlaywright(target.repository)

    def guarded(
        number: int, name: str, check: Callable[[], Result | list[Result]]
    ) -> Iterator[Result]:
        try:
            outcome = check()
        except Exception as error:  # se informa y se sigue con las demás
            yield Result(number, name, "failed", f"{type(error).__name__}: {error}")
            return
        yield from outcome if isinstance(outcome, list) else [outcome]

    steps: list[tuple[int, str, Callable[[], Result | list[Result]]]] = [
        (1, "health/live", lambda: check_live(target, http, network)),
        (2, "HealthyHostCount", lambda: check_healthy_hosts(target, aws)),
        (3, "autenticación mutua", lambda: check_mutual_tls(target, network, material)),
        (4, "conformidad", lambda: check_conformance(target, kit)),
        (5, "inicio de sesión", lambda: check_session(target, http, aws, clock)),
        (6, "well-known", lambda: check_well_known(target, http)),
        (8, "frontend", lambda: check_playwright(target, material, browser)),
        (9, "frontend", lambda: check_app_index(target, http, aws)),
        (10, "U-03", lambda: check_revoked_certificate(target, network, aws, material, sleep)),
        (11, "U-03", lambda: check_signed_catalog(target, http, network, material)),
        (12, "U-03", lambda: check_enrollment_reachable(target, http, network)),
        (13, "U-03", lambda: check_cross_origin_and_checksum(target, http)),
        (7, "alarmas", lambda: check_alarms(target, aws, sleep)),
    ]
    results = [
        result
        for number, name, check in steps
        for result in (
            guarded(number, name, check)
            if only is None or number in only
            else [Result(number, name, "skipped", "no seleccionada (--only)")]
        )
    ]
    return sorted(results, key=lambda result: result.number)


def summary(target: Target, results: Sequence[Result]) -> str:
    lines = [
        f"### Comprobaciones de despliegue · `{target.environment}`",
        "",
        "| Nº | Comprobación | Resultado | Detalle |",
        "|---:|---|---|---|",
    ]
    lines += [f"| {r.number} | {r.name} | {r.status} | {r.detail} |" for r in results]
    return "\n".join(lines) + "\n"


def _environment(value: str) -> str:
    if value != "pilot" and not _STAGING.match(value):
        raise argparse.ArgumentTypeError("'pilot' o 'staging-<n>'")
    return value


def _sha256(value: str) -> str:
    if not _HEX64.match(value):
        raise argparse.ArgumentTypeError("64 cifras hexadecimales en minúscula")
    return value


def _target_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError("URL HTTPS de origen, sin ruta ni consulta")
    return value.rstrip("/")


def _only(value: str) -> frozenset[int]:
    try:
        numbers = frozenset(int(item) for item in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("números de comprobación separados por comas") from None
    if not numbers or not numbers <= CHECK_NUMBERS:
        raise argparse.ArgumentTypeError("números de comprobación entre 1 y 13")
    return numbers


def private_file(value: str) -> Path:
    """Archivo regular, sin enlace simbólico y sin permisos de grupo ni de otros."""
    path = Path(value)
    try:
        status = path.lstat()
    except OSError:
        raise argparse.ArgumentTypeError("el archivo no existe o no se puede leer") from None
    if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
        raise argparse.ArgumentTypeError("debe ser un archivo regular privado (0600)")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comprobaciones de despliegue (§3.2).")
    parser.add_argument("--environment", required=True, type=_environment)
    parser.add_argument("--domain", required=True, help="dominio del producto (P5)")
    parser.add_argument("--verifier-sha256", required=True, type=_sha256)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--watch-minutes", type=int, default=15)
    parser.add_argument("--operator-email", help="staging: correo del operador del arranque")
    parser.add_argument(
        "--app-version", default=os.environ.get("VERSION"), help="versión de la release"
    )
    parser.add_argument(
        "--target", type=_target_url, help="origen HTTPS alternativo para el recorrido local"
    )
    parser.add_argument(
        "--fixtures-file",
        type=private_file,
        help="con --target: material del secreto deploy-checks en un archivo privado local",
    )
    parser.add_argument(
        "--ca-file", type=Path, help="con --target: raíz TLS efímera del destino local"
    )
    parser.add_argument("--only", type=_only, help="solo estas comprobaciones, p. ej. 8,9")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    if args.target is None and (args.fixtures_file is not None or args.ca_file is not None):
        parser.error("--fixtures-file y --ca-file solo se admiten con --target")
    if args.ca_file is not None and not args.ca_file.is_file():
        parser.error("--ca-file no es un archivo")
    target = Target(
        environment=args.environment,
        domain=args.domain,
        verifier_sha256=args.verifier_sha256,
        repository=args.repository,
        now=datetime.now(UTC),  # noqa: TID251 - herramienta de la canalización, no del núcleo
        watch_minutes=args.watch_minutes,
        operator_email=args.operator_email,
        app_version=args.app_version,
        target_url=args.target,
    )
    source: FixtureSource = (
        SecretsFixtures(aws_cli) if args.fixtures_file is None else FileFixtures(args.fixtures_file)
    )
    results = run_checks(
        target,
        http=UrllibHttp(args.ca_file),
        aws=aws_cli,
        network=SocketNetwork(),
        fixtures=source.load(args.environment),
        conformance=KitConformance(),
        playwright=LocalPlaywright(args.repository, args.ca_file),
        now=lambda: datetime.now(UTC),  # noqa: TID251
        only=args.only,
    )
    if args.report:
        args.report.write_text(
            json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    text = summary(target, results)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.write(text)
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
