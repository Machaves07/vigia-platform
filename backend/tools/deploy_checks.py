"""Comprobaciones de despliegue de ``staging-<n>`` y ``pilot`` (TASK-151; §3.2 del despliegue).

Las corre ``release.yml`` tras desplegar ``staging-<n>`` y ``pilot``, y ``rollback.yml`` tras
revertir. Solo biblioteca estándar y la CLI ``aws`` del runner (con las credenciales temporales
de ``vigia-deploy``), así que corre con ``uv run --no-project`` sin instalar el backend.

Lista de §3.2 leída como fija la revisión de coherencia (nota T-07, U02-H-07) y ampliada por A-27:

1. ``GET /health/live`` por ``app.`` responde ``200``; por ``nodes.`` solo la conexión TCP.
2. ``HealthyHostCount`` de ``tg-api`` igual a las tareas deseadas de ``vigia-api`` (sondeo
   interno: una petición externa a ``/health/ready`` por ``app.`` recibe ``404``).
3. ``nodes.`` sin certificado: rechazo TLS. Con un certificado de prueba de la raíz del entorno:
   ``401`` de la aplicación. La segunda mitad necesita el alta de U-03 para emitir el certificado.
4. Suite de conformidad de U-01 contra ``nodes.``: necesita las rutas del contrato (U-03).
5. ``staging``: activación de la invitación del arranque, inicio de sesión con segundo factor y
   ``GET /me``. ``pilot``: ``GET /me`` con una sesión del dueño, que hace el dueño (``manual``).
6. ``/.well-known/vigia-checkpoint-keys`` y ``/.well-known/vigia-verifier`` responden y el hash del
   verificador es el del artefacto del release.
7. Ninguna alarma del despliegue en ``ALARM`` durante 15 minutos (la última, por su duración).
8. ``staging``: recorrido de Playwright (U-05). 9. ``pilot``: ``GET /`` y ``version.json`` (U-05).
10 a 13. U-03: certificado revocado en dos capas, catálogo firmado, alta por ``app.`` y rechazo
   por ``nodes.``, origen cruzado y ``PUT`` sin suma.

**Omisión con motivo** (TASK-151: «las que dependen de rutas de U-03 se omiten con motivo hasta
que existan»): una comprobación cuyo requisito todavía no está en el repositorio (la ruta en
``openapi/app.yaml`` o el directorio ``frontend/``) sale ``skipped`` con el motivo. Si el requisito
ya existe y la comprobación todavía no está escrita, sale ``failed``: nunca se omite en silencio
algo que ya se puede comprobar.

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
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
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
CONTRACT_PREFIXES: Final = ("/nodes/", "/api/nodes/")
"""Rutas del contrato de U-01 servidas por la plataforma: habilitan la nº 4."""
NOTICE_VERSION_FALLBACK: Final = "1"
ALARM_POLL_SECONDS: Final = 60
METRIC_WINDOW: Final = timedelta(minutes=5)
TIMEOUT_SECONDS: Final = 15
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


# --- Adaptadores reales -----------------------------------------------------------------------


class UrllibHttp:
    """HTTPS con verificación de certificado y sesión por cookie (comprobación nº 5)."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
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


# --- Contexto -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    environment: str
    domain: str
    verifier_sha256: str
    repository: Path
    now: datetime
    watch_minutes: int = 15
    operator_email: str | None = None
    """Correo del primer operador que el arranque de ``staging-<n>`` invitó (nº 5)."""

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


def _pending(number: int, name: str, available: bool, reason: str) -> Result:
    """Omitida mientras falte su requisito; fallida si ya existe y no está escrita."""
    if not available:
        return Result(number, name, "skipped", reason)
    return Result(
        number,
        name,
        "failed",
        "el requisito ya existe en el repositorio y la comprobación no está escrita: "
        "escríbela en tools/deploy_checks.py antes de desplegar",
    )


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


def check_mutual_tls(target: Target, network: Network) -> Result:
    name = "autenticación mutua en nodes."
    if not network.tls_without_certificate_rejected(target.nodes_host, 443):
        return Result(3, name, "failed", "nodes. aceptó una conexión TLS sin certificado")
    if target.has_enrollment():
        return _pending(3, name, True, "")
    return Result(
        3,
        name,
        "skipped",
        "sin certificado: rechazo TLS comprobado. Con certificado de prueba: omitida hasta el alta "
        "de U-03 (/api/nodes/enrollment), que es quien emite certificados de la raíz",
    )


def check_conformance(target: Target) -> Result:
    return _pending(
        4,
        "conformidad de U-01 contra nodes.",
        target.has_contract_routes(),
        "omitida: la plataforma todavía no expone las rutas del contrato (llegan con U-03)",
    )


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


def check_frontend(target: Target) -> list[Result]:
    reason = "omitida: el repositorio no tiene frontend/ (U-05)"
    playwright = "recorrido de Playwright con la organización sintética"
    index = "GET / con vigia-app, version.json y paquete immutable"
    if target.staging:
        return [
            _pending(8, playwright, target.has_frontend(), reason),
            Result(9, index, "skipped", "solo en pilot"),
        ]
    return [
        Result(8, playwright, "skipped", "solo en staging"),
        _pending(9, index, target.has_frontend(), reason),
    ]


def check_fleet(target: Target) -> list[Result]:
    available = target.has_enrollment()
    reason = "omitida: la ruta de alta de U-03 (/api/nodes/enrollment) todavía no existe"
    results = [
        _pending(10, "certificado revocado rechazado en dos capas", available, reason),
        _pending(11, "catálogo firmado con certificado vigente", available, reason),
        _pending(12, "alta por app. y rechazo sin certificado por nodes.", available, reason),
    ]
    origin = "origen cruzado y PUT sin suma rechazado"
    if target.staging:
        results.append(_pending(13, origin, available, reason))
    else:
        results.append(Result(13, origin, "skipped", "solo en staging"))
    return results


def run_checks(
    target: Target,
    *,
    http: Http,
    aws: Aws,
    network: Network,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] | None = None,
) -> list[Result]:
    """Las trece comprobaciones; la nº 7 al final por sus 15 minutos. Un error inesperado de una
    comprobación la deja ``failed`` con el error, sin impedir las demás."""
    clock = now or (lambda: target.now)

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
        (3, "autenticación mutua", lambda: check_mutual_tls(target, network)),
        (4, "conformidad", lambda: check_conformance(target)),
        (5, "inicio de sesión", lambda: check_session(target, http, aws, clock)),
        (6, "well-known", lambda: check_well_known(target, http)),
        (8, "frontend", lambda: check_frontend(target)),
        (10, "U-03", lambda: check_fleet(target)),
        (7, "alarmas", lambda: check_alarms(target, aws, sleep)),
    ]
    results = [result for number, name, check in steps for result in guarded(number, name, check)]
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comprobaciones de despliegue (§3.2).")
    parser.add_argument("--environment", required=True, type=_environment)
    parser.add_argument("--domain", required=True, help="dominio del producto (P5)")
    parser.add_argument("--verifier-sha256", required=True, type=_sha256)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--watch-minutes", type=int, default=15)
    parser.add_argument("--operator-email", help="staging: correo del operador del arranque")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    target = Target(
        environment=args.environment,
        domain=args.domain,
        verifier_sha256=args.verifier_sha256,
        repository=args.repository,
        now=datetime.now(UTC),  # noqa: TID251 - herramienta de la canalización, no del núcleo
        watch_minutes=args.watch_minutes,
        operator_email=args.operator_email,
    )
    results = run_checks(
        target,
        http=UrllibHttp(),
        aws=aws_cli,
        network=SocketNetwork(),
        now=lambda: datetime.now(UTC),  # noqa: TID251
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
