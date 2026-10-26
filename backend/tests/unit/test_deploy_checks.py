"""Comprobaciones de despliegue de ``tools/deploy_checks.py`` (TASK-151 y TASK-234; §3.2 y A-27).

Sin AWS ni red: HTTP, la CLI ``aws``, los sockets, el runner de conformidad y el material de
certificados son dobles que registran lo que se les pide. Cada comprobación se prueba en verde y
en cada forma de fallo, y la regla de omisión en sus dos lados: omitida mientras falte su
requisito, fallida si el requisito ya existe y la comprobación no está escrita.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.conformance.mtls_proxy import server_tls
from tools import deploy_checks
from tools.deploy_checks import (
    Certificate,
    ConformanceVerdict,
    Fixtures,
    FrontendCredentials,
    PlaywrightVerdict,
    Response,
    Result,
    Target,
    run_checks,
)
from vigia_platform.shared.clock import SystemClock

REPOSITORY = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
VERIFIER = "b" * 64
APP_VERSION = "1.2.3"
TOTP_URI = "otpauth://totp/Vigia:op?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Vigia"
TG_ARN = "arn:aws:elasticloadbalancing:us-east-1:111:targetgroup/tg-api-staging-7/abc"
LB_ARN = "arn:aws:elasticloadbalancing:us-east-1:111:loadbalancer/app/vigia-alb-app/def"
STORE_ARN = "arn:aws:elasticloadbalancing:us-east-1:111:truststore/vigia-node-trust-staging-7/xyz"
ZONE = "01900000-0000-7000-8000-000000000003"
VALID = Certificate("-----BEGIN CERTIFICATE-----\nvalid\n", "-----BEGIN KEY-----\nvalid\n")
REVOKED = Certificate("-----BEGIN CERTIFICATE-----\nrevoked\n", "-----BEGIN KEY-----\nrevoked\n")
FRONTEND = FrontendCredentials(
    invitation_link="https://staging-7.vigia.example/invitacion#synthetic-invitation",
    email="operadora@staging-7.vigia.example",
)


def _target(**changes: Any) -> Target:
    base = Target(
        environment="staging-7",
        domain="vigia.example",
        verifier_sha256=VERIFIER,
        repository=REPOSITORY,
        now=NOW,
        watch_minutes=2,
        operator_email="operadora@staging-7.vigia.example",
        app_version=APP_VERSION,
    )
    return replace(base, **changes)


def _fixtures(**changes: Any) -> Fixtures:
    base = Fixtures(
        valid_certificate=VALID,
        revoked_certificate=REVOKED,
        synthetic_zone_id=ZONE,
        frontend=FRONTEND,
    )
    return replace(base, **changes)


def _json(status: int, body: object) -> Response:
    return Response(status, json.dumps(body).encode())


@dataclass
class FakeHttp:
    routes: dict[tuple[str, str], Callable[[object | None], Response]] = field(default_factory=dict)
    calls: list[tuple[str, str, object | None, dict[str, str]]] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append((method, "/" + path, body, headers or {}))
        handler = self.routes.get((method, "/" + path.split("/accept")[0].rsplit("/", 1)[0]))
        handler = self.routes.get((method, "/" + path), handler)
        if handler is None:
            return Response(404, b"{}")
        return handler(body)


def _healthy_http() -> FakeHttp:
    http = FakeHttp()
    http.routes[("GET", "/")] = lambda _: Response(
        200, b'<main id="vigia-app"></main><script type="module" src="/assets/app-123.js"></script>'
    )
    http.routes[("GET", "/version.json")] = lambda _: _json(200, {"app_version": APP_VERSION})
    http.routes[("GET", "/assets/app-123.js")] = lambda _: Response(
        200, b"console.log('synthetic')", {"Cache-Control": "public, max-age=31536000, immutable"}
    )
    http.routes[("GET", "/health/live")] = lambda _: _json(200, {"status": "live"})
    http.routes[("GET", "/.well-known/vigia-checkpoint-keys")] = lambda _: _json(
        200, {"keys": [{"key_id": "vigia-catalog-2026"}]}
    )
    http.routes[("GET", "/.well-known/vigia-verifier")] = lambda _: _json(
        200, {"name": "vigia_verify.py", "sha256": VERIFIER, "size_bytes": 1, "format_version": 1}
    )

    def accept(body: object | None) -> Response:
        assert isinstance(body, dict)
        if body["step"] == "begin":
            enrollment = {"provisioning_uri": TOTP_URI, "qr_svg": "", "recovery_codes": []}
            return _json(
                200, {"status": "pending", "enrollment": enrollment, "notice": {"version": "3"}}
            )
        return _json(200, {"status": "activated"})

    http.routes[("POST", "/invitations")] = accept
    http.routes[("POST", "/auth/login")] = lambda _: _json(
        200, {"status": "second_factor_required"}
    )
    http.routes[("POST", "/auth/second-factor")] = lambda _: _json(200, {"status": "authenticated"})
    http.routes[("GET", "/me")] = lambda _: _json(200, {"user_id": "x"})
    http.routes[("POST", "/api/nodes/enrollment")] = lambda _: _json(
        422, {"code": "schema_invalid"}
    )
    return http


@dataclass
class FakeAws:
    desired: int = 2
    healthy: list[int] = field(default_factory=lambda: [1, 2])
    alarms: list[list[str]] = field(default_factory=list)
    revoked_entries: int = 1
    revocation_polls: list[list[dict[str, int]]] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, *arguments: str) -> Any:
        self.calls.append(arguments)
        service, operation = arguments[0], arguments[1]
        if (service, operation) == ("ecs", "describe-services"):
            return {"services": [{"desiredCount": self.desired}]}
        if (service, operation) == ("elbv2", "describe-target-groups"):
            return {"TargetGroups": [{"TargetGroupArn": TG_ARN, "LoadBalancerArns": [LB_ARN]}]}
        if (service, operation) == ("elbv2", "describe-trust-stores"):
            return {"TrustStores": [{"TrustStoreArn": STORE_ARN}]}
        if (service, operation) == ("elbv2", "describe-trust-store-revocations"):
            if self.revocation_polls:
                return {"TrustStoreRevocations": self.revocation_polls.pop(0)}
            return {"TrustStoreRevocations": [{"NumberOfRevokedEntries": self.revoked_entries}]}
        if (service, operation) == ("cloudwatch", "get-metric-statistics"):
            if "revocation_list_entries" in arguments:
                return {
                    "Datapoints": [
                        {
                            "Timestamp": "2026-10-03T11:59:00Z",
                            "Maximum": float(self.revoked_entries),
                        }
                    ]
                }
            points = [
                {"Timestamp": f"2026-10-03T11:5{index}:00Z", "Minimum": float(value)}
                for index, value in enumerate(self.healthy)
            ]
            return {"Datapoints": list(reversed(points))}
        if (service, operation) == ("cloudwatch", "describe-alarms"):
            firing = self.alarms.pop(0) if self.alarms else []
            return {"MetricAlarms": [{"AlarmName": name} for name in firing]}
        if (service, operation) == ("secretsmanager", "get-secret-value"):
            invitation = {"link": "https://staging-7.vigia.example/invitacion#tok_123"}
            return {"SecretString": json.dumps(invitation)}
        if (service, operation) == ("route53", "list-health-checks"):
            return {
                "HealthChecks": [
                    {
                        "Id": "app-index",
                        "HealthCheckConfig": {
                            "FullyQualifiedDomainName": "app.vigia.example",
                            "ResourcePath": "/version.json",
                            "Type": "HTTPS_STR_MATCH",
                            "SearchString": "app_version",
                        },
                    }
                ]
            }
        if (service, operation) == ("route53", "get-health-check-status"):
            return {"HealthCheckObservations": [{"StatusReport": {"Status": "Success"}}]}
        raise AssertionError(f"llamada inesperada: {arguments}")


@dataclass
class FakeNetwork:
    tcp: bool = True
    rejected: bool = True
    revoked_rejected: bool = True
    certificate_responses: dict[tuple[str, str, str], Response] = field(default_factory=dict)
    certificate_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def tcp_connects(self, host: str, port: int) -> bool:
        return self.tcp

    def tls_without_certificate_rejected(self, host: str, port: int) -> bool:
        return self.rejected

    def request_with_certificate(
        self, host: str, port: int, method: str, path: str, certificate: Certificate
    ) -> Response | None:
        self.certificate_calls.append((certificate.certificate_pem, method, path))
        return self.certificate_responses.get((certificate.certificate_pem, method, path))

    def tls_with_certificate_rejected(self, host: str, port: int, certificate: Certificate) -> bool:
        return self.revoked_rejected


@dataclass
class FakeConformance:
    passed: bool = True
    detail: str = "la suite del kit pasa contra nodes."
    calls: list[str] = field(default_factory=list)

    def run(self, target_url: str) -> ConformanceVerdict:
        self.calls.append(target_url)
        return ConformanceVerdict(self.passed, self.detail)


@dataclass
class FakePlaywright:
    available: bool = True
    passed: bool = True
    calls: list[tuple[str, FrontendCredentials]] = field(default_factory=list)

    def run(self, target_url: str, credentials: FrontendCredentials) -> PlaywrightVerdict | None:
        self.calls.append((target_url, credentials))
        return PlaywrightVerdict(self.passed) if self.available else None


def _healthy_network() -> FakeNetwork:
    network = FakeNetwork()
    network.certificate_responses[(VALID.certificate_pem, "GET", "/api/nodes/heartbeats")] = _json(
        401, {"code": "node_not_enrolled"}
    )
    network.certificate_responses[
        (VALID.certificate_pem, "GET", f"/api/nodes/zones/{ZONE}/catalog")
    ] = _json(200, {"key_id": "vigia-catalog-2026", "payload": {}, "signature": "x"})
    network.certificate_responses[(REVOKED.certificate_pem, "GET", "/api/nodes/heartbeats")] = (
        _json(403, {"code": "node_revoked"})
    )
    return network


def _healthy_checksum_http() -> FakeHttp:
    http = _healthy_http()
    upload = "https://vigia-evidence.s3.amazonaws.com/documents/scope.pdf"
    http.routes[("POST", "/documents")] = lambda _: _json(200, {"upload_url": upload})
    http.routes[("OPTIONS", "/documents/scope.pdf")] = lambda _: Response(
        200,
        b"",
        {
            "Access-Control-Allow-Methods": "PUT",
            "Access-Control-Allow-Headers": "x-amz-checksum-sha256, content-type",
        },
    )
    http.routes[("PUT", "/documents/scope.pdf")] = lambda _: _json(403, {"code": "AccessDenied"})
    return http


def _run(
    target: Target | None = None,
    http: FakeHttp | None = None,
    aws: FakeAws | None = None,
    network: FakeNetwork | None = None,
    fixtures: Fixtures | None = None,
    conformance: FakeConformance | None = None,
    playwright: FakePlaywright | None = None,
    sleeps: list[float] | None = None,
) -> dict[int, Result]:
    pauses = sleeps if sleeps is not None else []
    results = run_checks(
        target or _target(),
        http=http or _healthy_checksum_http(),
        aws=aws or FakeAws(),
        network=network or _healthy_network(),
        fixtures=_fixtures() if fixtures is None else fixtures,
        conformance=conformance or FakeConformance(),
        playwright=playwright or FakePlaywright(),
        sleep=pauses.append,
    )
    numbers = [r.number for r in results]
    assert numbers == sorted(set(numbers)) == list(range(1, 14))
    return {r.number: r for r in results}


# --- Conjunto ---------------------------------------------------------------------------


def test_a_healthy_staging_has_no_failures_and_runs_the_u05_check() -> None:
    results = _run()
    assert {n for n, r in results.items() if r.status == "ok"} == {
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        10,
        11,
        12,
        13,
    }
    assert results[9].status == "skipped" and results[9].detail == "solo en pilot"
    assert {n for n, r in results.items() if r.status == "failed"} == set()


def test_a_repository_without_u03_routes_skips_the_u03_checks_with_a_reason(tmp_path: Path) -> None:
    # Un repositorio sin rutas del contrato ni interfaz (el de antes de VIG-152 y TASK-223).
    results = _run(_target(repository=_repository(tmp_path)))
    assert {n for n, r in results.items() if r.status == "ok"} == {1, 2, 5, 6, 7}
    assert {n for n, r in results.items() if r.status == "failed"} == set()
    for number in (3, 4, 8, 10, 11, 12, 13):
        assert results[number].status == "skipped" and results[number].detail
    assert "U-03" in results[4].detail and "U-05" in results[8].detail
    assert results[9].detail == "solo en pilot"


def test_without_environment_fixtures_the_u03_checks_skip_with_a_reason() -> None:
    results = _run(fixtures=Fixtures())
    assert results[3].status == "skipped" and "certificado de prueba" in results[3].detail
    assert results[10].status == "skipped" and "certificado revocado" in results[10].detail
    assert results[11].status == "skipped" and "zona sintética" in results[11].detail
    assert results[12].status == "ok"  # la ruta ya existe: se comprueba sin certificado
    assert results[13].status == "ok"


def test_pilot_uses_its_own_hosts_and_leaves_the_owner_session_manual() -> None:
    http = _healthy_http()
    aws = FakeAws()
    results = _run(_target(environment="pilot", operator_email=None), http=http, aws=aws)
    assert results[5].status == "manual"
    assert results[13].detail == "solo en staging"
    assert results[8].detail == "solo en staging"
    assert results[9].status == "ok"
    assert (
        "ecs",
        "describe-services",
        "--cluster",
        "vigia-pilot",
        "--services",
        "vigia-api",
    ) in aws.calls
    assert ("elbv2", "describe-target-groups", "--names", "tg-api") in aws.calls
    assert _target(environment="pilot").app_url == "https://app.vigia.example"
    assert _target(environment="pilot").nodes_host == "nodes.vigia.example"
    assert _target().app_url == "https://staging-7.vigia.example"
    assert _target().nodes_host == "staging-7-nodes.vigia.example"


def test_an_unexpected_error_fails_that_check_and_the_others_still_run() -> None:
    def broken(_: object | None) -> Response:
        raise ConnectionError("sin red")

    http = _healthy_http()
    http.routes[("GET", "/health/live")] = broken
    results = _run(http=http)
    assert results[1].status == "failed" and "ConnectionError" in results[1].detail
    assert results[6].status == "ok" and results[7].status == "ok"


# --- 1 y 3: health/live y autenticación mutua -------------------------------------------


def test_live_fails_on_a_non_200_or_without_tcp_to_nodes() -> None:
    http = _healthy_http()
    http.routes[("GET", "/health/live")] = lambda _: _json(503, {})
    assert _run(http=http)[1].status == "failed"
    assert _run(network=FakeNetwork(tcp=False))[1].status == "failed"


def test_nodes_accepting_tls_without_a_certificate_fails() -> None:
    result = _run(network=replace(_healthy_network(), rejected=False))[3]
    assert result.status == "failed" and "sin certificado" in result.detail


def test_mutual_tls_accepts_a_test_certificate_that_returns_401() -> None:
    assert _run()[3].status == "ok"
    assert _run(network=_healthy_network())[3].detail.startswith("con certificado de prueba")


def test_mutual_tls_fails_when_the_certificate_is_not_accepted_or_answers_another_status() -> None:
    rejected = _healthy_network()
    rejected.certificate_responses.pop((VALID.certificate_pem, "GET", "/api/nodes/heartbeats"))
    assert _run(network=rejected)[3].status == "failed"
    wrong = _healthy_network()
    wrong.certificate_responses[(VALID.certificate_pem, "GET", "/api/nodes/heartbeats")] = _json(
        200, {}
    )
    assert _run(network=wrong)[3].status == "failed"


# --- 2: HealthyHostCount --------------------------------------------------------------------


def test_healthy_hosts_compares_the_latest_point_with_the_desired_tasks() -> None:
    aws = FakeAws(desired=2, healthy=[2, 2, 1])
    assert _run(aws=aws)[2].status == "failed"
    metric = next(
        c
        for c in aws.calls
        if c[:2] == ("cloudwatch", "get-metric-statistics") and "HealthyHostCount" in c
    )
    assert "Name=TargetGroup,Value=targetgroup/tg-api-staging-7/abc" in metric
    assert "Name=LoadBalancer,Value=app/vigia-alb-app/def" in metric
    assert "2026-10-03T11:55:00+00:00" in metric and "2026-10-03T12:00:00+00:00" in metric


@pytest.mark.parametrize(("desired", "healthy"), [(0, [0]), (2, []), (3, [2])])
def test_healthy_hosts_fails_without_tasks_data_or_enough_hosts(
    desired: int, healthy: list[int]
) -> None:
    assert _run(aws=FakeAws(desired=desired, healthy=healthy))[2].status == "failed"


# --- 4: conformidad --------------------------------------------------------------------------


def test_conformance_runs_the_kit_against_the_nodes_host() -> None:
    conformance = FakeConformance()
    assert _run(conformance=conformance)[4].status == "ok"
    assert conformance.calls == ["https://staging-7-nodes.vigia.example"]


def test_conformance_fails_when_the_kit_fails() -> None:
    result = _run(conformance=FakeConformance(passed=False, detail="una comprobación falló"))[4]
    assert result.status == "failed" and "una comprobación falló" in result.detail


def test_conformance_skips_without_contract_routes(tmp_path: Path) -> None:
    assert _run(_target(repository=_repository(tmp_path)))[4].status == "skipped"


# --- 5: inicio de sesión ---------------------------------------------------------------------


def test_totp_matches_the_rfc_6238_vector() -> None:
    assert deploy_checks._totp(TOTP_URI, datetime(1970, 1, 1, 0, 0, 59, tzinfo=UTC)) == "287082"


def test_session_activates_logs_in_with_the_second_factor_and_reads_me() -> None:
    http = _healthy_http()
    assert _run(http=http)[5].status == "ok"
    posts = [
        c
        for c in http.calls
        if c[0] == "POST" and (c[1].startswith("/invitations") or c[1].startswith("/auth"))
    ]
    assert [c[1] for c in posts] == [
        "/invitations/tok_123/accept",
        "/invitations/tok_123/accept",
        "/auth/login",
        "/auth/second-factor",
    ]
    for _, _, _, headers in posts:
        assert headers == {
            "Origin": "https://staging-7.vigia.example",
            "Sec-Fetch-Site": "same-origin",
        }
    complete, login = posts[1][2], posts[2][2]
    assert isinstance(complete, dict) and isinstance(login, dict)
    assert complete["notice_version"] == "3"
    assert complete["password"] == login["password"] and len(str(login["password"])) >= 32
    assert login["email"] == "operadora@staging-7.vigia.example"
    assert str(complete["second_factor_code"]).isdigit()


@pytest.mark.parametrize(
    ("route", "answer"),
    [
        (("POST", "/invitations"), _json(410, {})),
        (("POST", "/auth/login"), _json(401, {})),
        (("POST", "/auth/second-factor"), _json(200, {"status": "second_factor_required"})),
        (("GET", "/me"), _json(401, {})),
    ],
)
def test_session_fails_at_any_step(route: tuple[str, str], answer: Response) -> None:
    http = _healthy_http()
    http.routes[route] = lambda _: answer
    assert _run(http=http)[5].status == "failed"


def test_session_without_the_operator_email_fails() -> None:
    assert _run(_target(operator_email=None))[5].status == "failed"


# --- 6: well-known --------------------------------------------------------------------------


def test_a_different_verifier_hash_fails() -> None:
    result = _run(_target(verifier_sha256="c" * 64))[6]
    assert result.status == "failed" and VERIFIER in result.detail


def test_a_missing_checkpoint_key_set_fails() -> None:
    http = _healthy_http()
    http.routes[("GET", "/.well-known/vigia-checkpoint-keys")] = lambda _: _json(500, {})
    assert _run(http=http)[6].status == "failed"


# --- 7: alarmas -------------------------------------------------------------------------------


def test_alarms_are_polled_every_minute_for_the_whole_window() -> None:
    sleeps: list[float] = []
    aws = FakeAws()
    assert _run(_target(watch_minutes=15), aws=aws, sleeps=sleeps)[7].status == "ok"
    assert sleeps == [60] * 15
    assert sum(1 for c in aws.calls if c[:2] == ("cloudwatch", "describe-alarms")) == 16


def test_an_own_alarm_fails_at_the_minute_it_fires() -> None:
    aws = FakeAws(alarms=[[], ["vigia-api-5xx-staging-7"]])
    result = _run(aws=aws)[7]
    assert result.status == "failed" and "minuto 1" in result.detail


def test_alarms_of_other_deployments_are_ignored() -> None:
    staging = FakeAws(alarms=[["vigia-api-5xx", "vigia-api-5xx-staging-8"]])
    assert _run(aws=staging)[7].status == "ok"
    pilot = FakeAws(alarms=[["vigia-api-5xx-staging-7"]])
    assert _run(_target(environment="pilot"), aws=pilot)[7].status == "ok"
    pilot_own = FakeAws(alarms=[["vigia-api-5xx"]])
    assert _run(_target(environment="pilot"), aws=pilot_own)[7].status == "failed"


@pytest.mark.parametrize(
    "alarm",
    [
        "loop-dead-letter",
        "assistant-unavailable",
        "export-stuck",
        "export-failed",
        "loop-projection-divergence",
        "periodic-task-stale-review-reminders",
        "assistant-token-budget",
        "assistant-rate-margin",
        "email-bounce-rate",
        "email-events-dlq",
        "email-events-queue-age",
        "email-send-quota-high",
        "email-bounce-rate-warning",
        "email-complaint-rate-warning",
        "email-complaint-rate",
    ],
)
def test_u04_alarm_at_the_last_minute_of_pilot_fails(alarm: str) -> None:
    pauses: list[float] = []
    aws = FakeAws(alarms=[[] for _ in range(15)] + [[f"vigia-{alarm}"]])
    result = deploy_checks.check_alarms(
        _target(environment="pilot", watch_minutes=15), aws, pauses.append
    )
    assert result.status == "failed"
    assert pauses == [60] * 15
    assert "minuto 15" in result.detail
    assert all(call[-2:] == ("--alarm-name-prefix", "vigia-") for call in aws.calls)


def test_a_composite_u04_alarm_also_blocks_pilot() -> None:
    def aws(*arguments: str) -> Any:
        return {"CompositeAlarms": [{"AlarmName": "vigia-loop-degraded"}]}

    result = deploy_checks.check_alarms(_target(environment="pilot"), aws, lambda _: None)
    assert result.status == "failed"


# --- 8 y 9: aplicación de página única --------------------------------------------------------


def test_playwright_runs_against_staging_with_the_private_frontend_fixture() -> None:
    playwright = FakePlaywright()
    result = _run(playwright=playwright)[8]
    assert result.status == "ok"
    assert playwright.calls == [("https://staging-7.vigia.example", FRONTEND)]


def test_playwright_fails_without_copying_a_credential_to_the_report() -> None:
    secret = FrontendCredentials(
        invitation_link="https://staging-7.vigia.example/invitacion#secret-synthetic",
        email="secret@example.test",
    )
    result = _run(fixtures=_fixtures(frontend=secret), playwright=FakePlaywright(passed=False))[8]
    rendered = deploy_checks.summary(_target(), [result])
    assert result.status == "failed"
    assert secret.invitation_link not in rendered and secret.email not in rendered


def test_playwright_skips_without_the_secret_or_the_installed_runner() -> None:
    missing_secret = _run(fixtures=_fixtures(frontend=None))[8]
    assert missing_secret.status == "skipped" and "secreto" in missing_secret.detail
    unavailable = _run(playwright=FakePlaywright(available=False))[8]
    assert unavailable.status == "skipped" and "no está instalado" in unavailable.detail


def test_local_playwright_never_prints_or_keeps_the_private_credential_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = tmp_path / "frontend" / "node_modules" / "@playwright" / "test" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    secret = FrontendCredentials(
        invitation_link="https://staging-7.vigia.example/invitacion#secret-synthetic",
        email="secret@example.test",
    )
    seen: dict[str, str] = {}

    def fake_run(arguments: list[str], **kwargs: Any) -> CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        private_path = Path(environment["VIGIA_DEPLOY_CHECK_CREDENTIALS_FILE"])
        seen["path"] = str(private_path)
        assert private_path.read_text(encoding="utf-8")
        assert os.stat(private_path).st_mode & 0o777 == 0o600
        assert secret.invitation_link not in " ".join(arguments)
        assert kwargs["timeout"] == deploy_checks.PLAYWRIGHT_TIMEOUT_SECONDS
        return CompletedProcess(arguments, 1, stdout=secret.invitation_link, stderr=secret.email)

    monkeypatch.setattr(deploy_checks.shutil, "which", lambda _: "/usr/bin/node")
    monkeypatch.setattr(deploy_checks.subprocess, "run", fake_run)
    verdict = deploy_checks.LocalPlaywright(tmp_path).run("https://staging-7.vigia.example", secret)
    assert verdict is not None and not verdict.passed
    assert verdict.detail == "el proceso terminó con código 1"
    captured = capsys.readouterr()
    assert secret.invitation_link not in captured.out + captured.err
    assert secret.email not in captured.out + captured.err
    assert not Path(seen["path"]).exists()


def test_local_playwright_fails_safely_when_its_process_exceeds_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = tmp_path / "frontend" / "node_modules" / "@playwright" / "test" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    secret = FrontendCredentials(
        invitation_link="https://staging-7.vigia.example/invitacion#secret-synthetic",
        email="secret@example.test",
    )
    seen: dict[str, str] = {}

    def fake_run(arguments: list[str], **kwargs: Any) -> CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        private_path = Path(environment["VIGIA_DEPLOY_CHECK_CREDENTIALS_FILE"])
        seen["path"] = str(private_path)
        assert kwargs["timeout"] == deploy_checks.PLAYWRIGHT_TIMEOUT_SECONDS
        raise deploy_checks.subprocess.TimeoutExpired(
            arguments,
            deploy_checks.PLAYWRIGHT_TIMEOUT_SECONDS,
            output=secret.invitation_link,
            stderr=secret.email,
        )

    monkeypatch.setattr(deploy_checks.shutil, "which", lambda _: "/usr/bin/node")
    monkeypatch.setattr(deploy_checks.subprocess, "run", fake_run)
    verdict = deploy_checks.LocalPlaywright(tmp_path).run("https://staging-7.vigia.example", secret)
    assert verdict == PlaywrightVerdict(False, "el proceso agotó el límite de 90 s")
    captured = capsys.readouterr()
    assert secret.invitation_link not in captured.out + captured.err
    assert secret.email not in captured.out + captured.err
    assert not Path(seen["path"]).exists()


def test_pilot_index_checks_marker_version_asset_and_route53_health() -> None:
    aws = FakeAws()
    result = _run(_target(environment="pilot", operator_email=None), aws=aws)[9]
    assert result.status == "ok"
    assert ("route53", "list-health-checks") in aws.calls
    assert ("route53", "get-health-check-status", "--health-check-id", "app-index") in aws.calls


@pytest.mark.parametrize(
    "route",
    [
        ("GET", "/"),
        ("GET", "/version.json"),
        ("GET", "/assets/app-123.js"),
    ],
)
def test_pilot_index_fails_when_an_http_resource_is_invalid(route: tuple[str, str]) -> None:
    http = _healthy_http()
    http.routes[route] = lambda _: Response(500, b"{}")
    assert _run(_target(environment="pilot", operator_email=None), http=http)[9].status == "failed"


def test_pilot_index_rejects_a_version_distinct_from_the_release() -> None:
    http = _healthy_http()
    http.routes[("GET", "/version.json")] = lambda _: _json(200, {"app_version": "other"})
    result = _run(_target(environment="pilot", operator_email=None), http=http)[9]
    assert result.status == "failed" and "release" in result.detail


def test_pilot_index_fails_when_the_index_has_no_vigia_app_marker() -> None:
    http = _healthy_http()
    http.routes[("GET", "/")] = lambda _: Response(
        200, b'<main></main><script type="module" src="/assets/app-123.js"></script>'
    )
    result = _run(_target(environment="pilot", operator_email=None), http=http)[9]
    assert result.status == "failed" and "marcador vigia-app" in result.detail


def test_pilot_index_fails_when_the_initial_asset_is_not_immutable() -> None:
    http = _healthy_http()
    http.routes[("GET", "/assets/app-123.js")] = lambda _: Response(
        200, b"console.log('synthetic')", {"Cache-Control": "public, max-age=31536000"}
    )
    result = _run(_target(environment="pilot", operator_email=None), http=http)[9]
    assert result.status == "failed" and "immutable" in result.detail


def test_pilot_index_fails_if_route53_does_not_report_success() -> None:
    class UnhealthyAws(FakeAws):
        def __call__(self, *arguments: str) -> Any:
            if arguments[:2] == ("route53", "get-health-check-status"):
                return {"HealthCheckObservations": [{"StatusReport": {"Status": "Failure"}}]}
            return super().__call__(*arguments)

    result = _run(_target(environment="pilot", operator_email=None), aws=UnhealthyAws())[9]
    assert result.status == "failed"


def test_pilot_index_skips_without_the_frontend(tmp_path: Path) -> None:
    repository = _repository(tmp_path, "/api/nodes/enrollment")
    result = _run(_target(repository=repository, environment="pilot", operator_email=None))[9]
    assert result.status == "skipped" and "no tiene frontend" in result.detail


# --- 10: certificado revocado en dos capas ----------------------------------------------------


def test_revoked_certificate_checks_the_app_the_store_and_the_load_balancer() -> None:
    aws = FakeAws(revoked_entries=3)
    assert _run(aws=aws)[10].status == "ok"
    assert ("elbv2", "describe-trust-stores", "--names", "vigia-node-trust-staging-7") in aws.calls
    metric = next(
        c
        for c in aws.calls
        if c[:2] == ("cloudwatch", "get-metric-statistics") and "revocation_list_entries" in c
    )
    assert "Vigia/Platform" in metric


def test_revoked_certificate_polls_the_store_until_it_matches() -> None:
    sleeps: list[float] = []
    aws = FakeAws(revoked_entries=3, revocation_polls=[[], [{"NumberOfRevokedEntries": 3}]])
    assert _run(_target(watch_minutes=0), aws=aws, sleeps=sleeps)[10].status == "ok"
    assert sleeps == [60]


def test_revoked_certificate_fails_on_the_first_layer() -> None:
    network = _healthy_network()
    network.certificate_responses[(REVOKED.certificate_pem, "GET", "/api/nodes/heartbeats")] = (
        _json(200, {})
    )
    assert _run(network=network)[10].status == "failed"
    other_code = _healthy_network()
    other_code.certificate_responses[(REVOKED.certificate_pem, "GET", "/api/nodes/heartbeats")] = (
        _json(403, {"code": "node_not_enrolled"})
    )
    assert _run(network=other_code)[10].status == "failed"


def test_revoked_certificate_fails_when_the_store_does_not_match() -> None:
    aws = FakeAws(revoked_entries=9, revocation_polls=[[{"NumberOfRevokedEntries": 3}]] * 3)
    assert _run(aws=aws)[10].status == "failed"


def test_revoked_certificate_fails_when_the_balancer_still_accepts_it() -> None:
    network = replace(_healthy_network(), revoked_rejected=False)
    assert _run(network=network)[10].status == "failed"


# --- 11: catálogo firmado ---------------------------------------------------------------------


def test_signed_catalog_verifies_the_key_against_the_published_set() -> None:
    assert _run()[11].status == "ok"


def test_signed_catalog_fails_when_the_key_is_not_published() -> None:
    network = _healthy_network()
    network.certificate_responses[
        (VALID.certificate_pem, "GET", f"/api/nodes/zones/{ZONE}/catalog")
    ] = _json(200, {"key_id": "otra", "payload": {}})
    assert _run(network=network)[11].status == "failed"


def test_signed_catalog_fails_when_the_route_rejects() -> None:
    network = _healthy_network()
    network.certificate_responses[
        (VALID.certificate_pem, "GET", f"/api/nodes/zones/{ZONE}/catalog")
    ] = _json(403, {"code": "node_revoked"})
    assert _run(network=network)[11].status == "failed"


# --- 12: alta por app. y rechazo por nodes. ---------------------------------------------------


def test_enrollment_reachable_expects_a_4xx_from_the_app_and_tls_rejection() -> None:
    assert _run()[12].status == "ok"
    network = replace(_healthy_network(), rejected=False)
    assert _run(network=network)[12].status == "failed"
    http = _healthy_http()
    http.routes[("POST", "/api/nodes/enrollment")] = lambda _: _json(200, {})
    assert _run(http=http)[12].status == "failed"


# --- 13: origen cruzado y PUT sin suma --------------------------------------------------------


def test_cross_origin_and_checksum_passes_with_cors_and_a_rejected_put() -> None:
    assert _run(http=_healthy_checksum_http())[13].status == "ok"


def test_cross_origin_fails_without_the_cors_headers() -> None:
    http = _healthy_checksum_http()
    http.routes[("OPTIONS", "/documents/scope.pdf")] = lambda _: Response(200, b"", {})
    assert _run(http=http)[13].status == "failed"


def test_cross_origin_fails_when_the_put_without_checksum_is_accepted() -> None:
    http = _healthy_checksum_http()
    http.routes[("PUT", "/documents/scope.pdf")] = lambda _: _json(200, {})
    assert _run(http=http)[13].status == "failed"


def test_cross_origin_skips_in_pilot() -> None:
    assert _run(_target(environment="pilot", operator_email=None))[13].status == "skipped"


# --- Omisión con motivo: nunca en silencio cuando el requisito ya existe -------------------


def _repository(tmp_path: Path, *paths: str, frontend: bool = False) -> Path:
    openapi = tmp_path / "backend" / "openapi"
    openapi.mkdir(parents=True)
    lines = ["paths:", '  "/health/live":', "    get: {}"]
    lines += [line for path in paths for line in (f'  "{path}":', "    post: {}")]
    (openapi / "app.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if frontend:
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_the_u05_checks_run_when_the_frontend_exists(tmp_path: Path) -> None:
    repository = _repository(tmp_path, "/api/nodes/enrollment", frontend=True)
    assert _run(_target(repository=repository))[8].status == "ok"
    pilot = _run(_target(repository=repository, environment="pilot", operator_email=None))
    assert pilot[9].status == "ok"


def test_the_current_repository_has_contract_routes_enrollment_and_frontend() -> None:
    # VIG-152 y TASK-223 publican rutas del contrato y VIG-151 el alta: las comprobaciones de
    # U-03 ya están escritas (TASK-234) y corren contra el material del entorno. VIG-277 crea
    # frontend/package.json (U-05): sus comprobaciones nº 8 y 9 las escribe VIG-319.
    target = _target()
    assert target.has_contract_routes() and target.has_enrollment()
    assert target.has_frontend()
    assert "/health/live" in target.openapi_paths()


# --- Adaptadores y argumentos ----------------------------------------------------------------


def test_secrets_fixtures_reads_the_material_and_tolerates_a_missing_secret() -> None:
    def aws(*arguments: str) -> Any:
        if arguments[1] == "get-secret-value":
            return {
                "SecretString": json.dumps(
                    {
                        "valid": {"certificate_pem": "cert", "key_pem": "key"},
                        "revoked": {"certificate_pem": "rc", "key_pem": "rk"},
                        "synthetic_zone_id": ZONE,
                    }
                )
            }
        raise AssertionError(arguments)

    loaded = deploy_checks.SecretsFixtures(aws).load("staging-7")
    assert loaded.valid_certificate == Certificate("cert", "key")
    assert loaded.revoked_certificate == Certificate("rc", "rk")
    assert loaded.synthetic_zone_id == ZONE

    def missing(*arguments: str) -> Any:
        raise deploy_checks.subprocess.CalledProcessError(1, arguments)

    assert deploy_checks.SecretsFixtures(missing).load("staging-7") == Fixtures()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--environment", "staging-07", "--domain", "d", "--verifier-sha256", VERIFIER],
        ["--environment", "prod", "--domain", "d", "--verifier-sha256", VERIFIER],
        ["--environment", "pilot", "--domain", "d", "--verifier-sha256", "B" * 64],
        ["--environment", "pilot", "--domain", "d", "--verifier-sha256", "b" * 63],
    ],
)
def test_the_command_rejects_malformed_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        deploy_checks.main(arguments)
    assert raised.value.code == 2


def test_the_summary_is_a_markdown_table() -> None:
    results = [Result(1, "a", "ok", "bien"), Result(2, "b", "skipped", "motivo")]
    text = deploy_checks.summary(_target(), results)
    assert "| 1 | a | ok | bien |" in text and "| 2 | b | skipped | motivo |" in text


# --- Evidencia local con --target (VIG-352) ----------------------------------------------------

LOCAL = "https://127.0.0.1:8443"
SEARCH_END_IN_BODY = len('{"p": "') + len('", "') + len("app_version")
"""Bytes del cuerpo de la sonda que no son relleno, hasta el final de ``app_version``."""


def _local_pilot(**changes: Any) -> Target:
    return _target(environment="pilot", operator_email=None, target_url=LOCAL, **changes)


def test_a_local_target_uses_the_local_index_probe_and_never_route53() -> None:
    aws = FakeAws()
    http = _healthy_http()
    result = _run(_local_pilot(), http=http, aws=aws)[9]
    assert result.status == "ok" and "sonda local" in result.detail
    assert not [call for call in aws.calls if call[0] == "route53"]
    probes = [call for call in http.calls if call[1] == "/version.json"]
    # La sonda repite el recurso de Route 53 sin cabeceras de navegación.
    assert len(probes) == 2 and probes[-1][3] == {}


def test_without_a_local_target_route53_is_still_required() -> None:
    aws = FakeAws()
    _run(_target(environment="pilot", operator_email=None), aws=aws)
    assert ("route53", "list-health-checks") in aws.calls


@pytest.mark.parametrize(("end", "status"), [(5120, "ok"), (5121, "failed")])
def test_the_local_probe_only_searches_the_first_5120_bytes(end: int, status: str) -> None:
    http = _healthy_http()
    body = '{"p": "' + "x" * (end - SEARCH_END_IN_BODY) + '", "app_version": "1.2.3"}'
    assert body.index("app_version") + len("app_version") == end
    http.routes[("GET", "/version.json")] = lambda _: Response(200, body.encode())
    result = _run(_local_pilot(), http=http)[9]
    assert result.status == status
    if status == "failed":
        assert "sonda local" in result.detail


@pytest.mark.parametrize(
    ("code", "healthy"), [(199, False), (200, True), (302, True), (399, True), (400, False)]
)
def test_the_local_probe_accepts_2xx_and_3xx_like_route53(code: int, healthy: bool) -> None:
    http = FakeHttp()
    http.routes[("GET", "/version.json")] = lambda _: _json(code, {"app_version": APP_VERSION})
    assert deploy_checks.local_app_index_probe(_local_pilot(), http) is healthy


def _selected(target: Target, aws: FakeAws, playwright: FakePlaywright, only: int) -> list[Result]:
    return run_checks(
        target,
        http=_healthy_http(),
        aws=aws,
        network=_healthy_network(),
        fixtures=_fixtures(),
        conformance=FakeConformance(),
        playwright=playwright,
        sleep=lambda _: None,
        only=frozenset({only}),
    )


def test_only_runs_the_selected_checks_and_skips_the_others_with_a_reason() -> None:
    playwright = FakePlaywright()
    by_number = {
        r.number: r for r in _selected(_target(target_url=LOCAL), FakeAws(), playwright, 8)
    }
    assert sorted(by_number) == list(range(1, 14))
    assert by_number[8].status == "ok"
    for number in set(range(1, 14)) - {8}:
        assert by_number[number].status == "skipped"
        assert by_number[number].detail == "no seleccionada (--only)"
    assert playwright.calls == [(LOCAL, FRONTEND)]


def test_only_never_calls_aws_for_the_unselected_checks() -> None:
    aws = FakeAws()
    results = {r.number: r for r in _selected(_local_pilot(), aws, FakePlaywright(), 9)}
    assert results[9].status == "ok"
    assert aws.calls == []


def _private(path: Path, document: object, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(mode)
    return path


def test_file_fixtures_read_the_secret_shape(tmp_path: Path) -> None:
    path = _private(
        tmp_path / "deploy-checks.json",
        {
            "frontend": {"invitation_link": FRONTEND.invitation_link, "email": FRONTEND.email},
            "synthetic_zone_id": ZONE,
            "unknown": {"kept": "aside"},
        },
    )
    loaded = deploy_checks.FileFixtures(path).load("staging-7")
    assert loaded == Fixtures(frontend=FRONTEND, synthetic_zone_id=ZONE)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no es json",
        "[]",
        '"texto"',
        "NaN",
        "[" * 100_000 + "]" * 100_000,
        '{"frontend": {"invitation_link": 1, "email": "a@b.c"}, "synthetic_zone_id": 7}',
        '{"frontend": {"invitation_link": "", "email": "a@b.c"}}',
        '{"frontend": "enlace"}',
    ],
    ids=["vacio", "texto", "lista", "cadena", "nan", "anidado", "tipos", "vacios", "plano"],
)
def test_file_fixtures_leave_everything_without_material_on_garbage(
    tmp_path: Path, text: str
) -> None:
    path = tmp_path / "deploy-checks.json"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    assert deploy_checks.FileFixtures(path).load("staging-7") == Fixtures()


def test_file_fixtures_tolerate_a_missing_or_binary_file(tmp_path: Path) -> None:
    assert deploy_checks.FileFixtures(tmp_path / "ausente.json").load("staging-7") == Fixtures()
    binary = tmp_path / "binario.json"
    binary.write_bytes(b"\xff\xfe\x00")
    assert deploy_checks.FileFixtures(binary).load("staging-7") == Fixtures()


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o610, 0o601, 0o660, 0o644])
def test_the_fixtures_file_must_be_private(tmp_path: Path, mode: int) -> None:
    path = _private(tmp_path / "deploy-checks.json", {}, mode)
    with pytest.raises(deploy_checks.argparse.ArgumentTypeError, match="privado"):
        deploy_checks.private_file(str(path))
    path.chmod(0o600)
    assert deploy_checks.private_file(str(path)) == path
    path.chmod(0o400)
    assert deploy_checks.private_file(str(path)) == path


def test_the_fixtures_file_rejects_links_directories_and_missing_paths(tmp_path: Path) -> None:
    real = _private(tmp_path / "real.json", {})
    link = tmp_path / "enlace.json"
    link.symlink_to(real)
    for value in (link, tmp_path, tmp_path / "ausente.json"):
        with pytest.raises(deploy_checks.argparse.ArgumentTypeError):
            deploy_checks.private_file(str(value))


def _arguments(*extra: str) -> list[str]:
    required = ["--environment", "staging-7", "--domain", "vigia.test"]
    return [*required, "--verifier-sha256", VERIFIER, *extra]


@pytest.mark.parametrize(
    "extra",
    [
        ("--only", "0"),
        ("--only", "14"),
        ("--only", "8,x"),
        ("--only", ""),
        ("--only", "8,,9"),
        ("--fixtures-file", "/no/existe.json"),
        ("--target", LOCAL, "--ca-file", "/no/existe.pem"),
    ],
)
def test_the_local_options_reject_malformed_values(extra: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as raised:
        deploy_checks.main(_arguments(*extra))
    assert raised.value.code == 2


def test_local_material_is_only_accepted_with_a_target(tmp_path: Path) -> None:
    fixtures = _private(tmp_path / "deploy-checks.json", {})
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    for extra in (("--fixtures-file", str(fixtures)), ("--ca-file", str(ca))):
        with pytest.raises(SystemExit) as raised:
            deploy_checks.main(_arguments(*extra))
        assert raised.value.code == 2


def test_the_command_runs_a_local_check_from_the_private_file_without_revealing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = FrontendCredentials(
        invitation_link=f"{LOCAL}/invitations#token-sintetico-{os.getpid()}",
        email="deploy-check@e2e.example.test",
    )
    fixtures = _private(
        tmp_path / "deploy-checks.json",
        {"frontend": {"invitation_link": secret.invitation_link, "email": secret.email}},
    )
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    class RecordingPlaywright:
        def __init__(self, repository: Path, ca_file: Path | None = None) -> None:
            seen["ca_file"] = ca_file

        def run(self, target_url: str, credentials: FrontendCredentials) -> PlaywrightVerdict:
            seen["run"] = (target_url, credentials)
            return PlaywrightVerdict(True)

    def no_aws(*arguments: str) -> Any:
        raise AssertionError(f"sin AWS en local: {arguments}")

    monkeypatch.setattr(deploy_checks, "LocalPlaywright", RecordingPlaywright)
    monkeypatch.setattr(deploy_checks, "UrllibHttp", lambda ca_file=None: _healthy_http())
    monkeypatch.setattr(deploy_checks, "aws_cli", no_aws)
    report = tmp_path / "report.json"
    summary = tmp_path / "summary.md"
    code = deploy_checks.main(
        _arguments(
            "--target", LOCAL + "/", "--fixtures-file", str(fixtures), "--ca-file", str(ca),
            "--only", "8", "--report", str(report), "--summary", str(summary),
        )
    )  # fmt: skip
    assert code == 0
    assert seen == {"ca_file": ca, "run": (LOCAL, secret)}
    results = {item["number"]: item for item in json.loads(report.read_text(encoding="utf-8"))}
    assert results[8]["status"] == "ok"
    captured = capsys.readouterr()
    written = captured.out + captured.err + report.read_text("utf-8") + summary.read_text("utf-8")
    token = secret.invitation_link.rsplit("#", 1)[-1]
    for value in (secret.invitation_link, token, secret.email):
        assert value not in written


def _server_certificate(directory: Path) -> tuple[bytes, Path, Path, Path]:
    """Raíz efímera y certificado de servidor del balanceador local (como el mundo de U-05)."""
    directory.mkdir()
    tls = server_tls(directory, SystemClock().now())
    leaf = x509.load_pem_x509_certificate(tls.certificate_file.read_bytes())
    return (
        leaf.public_bytes(serialization.Encoding.DER),
        tls.ca_file,
        tls.certificate_file,
        tls.key_file,
    )


def _expected_pin(certificate_der: bytes) -> str:
    spki = (
        x509.load_der_x509_certificate(certificate_der)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


def test_spki_pin_is_the_sha256_of_the_subject_public_key_info(tmp_path: Path) -> None:
    certificate, *_ = _server_certificate(tmp_path / "tls")
    assert deploy_checks.spki_sha256(certificate) == _expected_pin(certificate)


def test_spki_pin_of_an_rsa_certificate_with_long_form_lengths() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rsa.vigia.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(7)
        .not_valid_before(NOW)
        .not_valid_after(NOW + timedelta(days=1))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.DER)
    )
    assert deploy_checks.spki_sha256(certificate) == _expected_pin(certificate)


@pytest.mark.parametrize("cut", [0, 1, 2, 10, 200])
def test_spki_pin_rejects_a_truncated_certificate(tmp_path: Path, cut: int) -> None:
    certificate, *_ = _server_certificate(tmp_path / "tls")
    with pytest.raises(ValueError):
        deploy_checks.spki_sha256(certificate[:cut])


@contextlib.contextmanager
def _serve_once(certificate: Path, key: Path) -> Iterator[int]:
    """Un saludo TLS en 127.0.0.1; el cliente decide si confía en el certificado."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certificate, key)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(60)

    def serve() -> None:
        with contextlib.suppress(OSError):
            connection, _ = listener.accept()
            with context.wrap_socket(connection, server_side=True):
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        thread.join(timeout=60)
        listener.close()


def test_the_pin_comes_only_from_a_certificate_the_local_root_verified(tmp_path: Path) -> None:
    certificate, ca, leaf, key = _server_certificate(tmp_path / "a")
    with _serve_once(leaf, key) as port:
        pin = deploy_checks.verified_certificate_pin(f"https://127.0.0.1:{port}", ca)
    assert pin == _expected_pin(certificate)
    _, other_ca, _, _ = _server_certificate(tmp_path / "b")
    with _serve_once(leaf, key) as port, pytest.raises(ssl.SSLCertVerificationError):
        deploy_checks.verified_certificate_pin(f"https://127.0.0.1:{port}", other_ca)


def _fake_cli(tmp_path: Path) -> None:
    cli = tmp_path / "frontend" / "node_modules" / "@playwright" / "test" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()


def test_local_playwright_pins_the_verified_key_only_with_a_ca_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(tmp_path)
    environments: list[dict[str, str]] = []

    def fake_run(arguments: list[str], **kwargs: Any) -> CompletedProcess[str]:
        environments.append(dict(kwargs["env"]))
        return CompletedProcess(arguments, 0, stdout="", stderr="")

    pin = "A" * 43 + "="
    monkeypatch.setattr(deploy_checks.shutil, "which", lambda _: "/usr/bin/node")
    monkeypatch.setattr(deploy_checks.subprocess, "run", fake_run)
    monkeypatch.setattr(deploy_checks, "verified_certificate_pin", lambda url, ca: pin)
    # Una huella heredada del entorno nunca llega al navegador.
    monkeypatch.setenv("VIGIA_DEPLOY_CHECK_SPKI", "B" * 43 + "=")
    verdict = deploy_checks.LocalPlaywright(tmp_path, tmp_path / "ca.pem").run(LOCAL, FRONTEND)
    assert verdict == PlaywrightVerdict(True, "el proceso terminó con código 0")
    assert deploy_checks.LocalPlaywright(tmp_path).run(LOCAL, FRONTEND) is not None
    assert environments[0]["VIGIA_DEPLOY_CHECK_SPKI"] == pin
    assert "VIGIA_DEPLOY_CHECK_SPKI" not in environments[1]


def test_local_playwright_fails_without_running_when_the_target_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_cli(tmp_path)

    def unverified(url: str, ca: Path) -> str:
        raise ssl.SSLCertVerificationError("certificate verify failed")

    def never(*arguments: Any, **kwargs: Any) -> Any:
        raise AssertionError("no debe lanzar Playwright")

    monkeypatch.setattr(deploy_checks.shutil, "which", lambda _: "/usr/bin/node")
    monkeypatch.setattr(deploy_checks.subprocess, "run", never)
    monkeypatch.setattr(deploy_checks, "verified_certificate_pin", unverified)
    verdict = deploy_checks.LocalPlaywright(tmp_path, tmp_path / "ca.pem").run(LOCAL, FRONTEND)
    assert verdict == PlaywrightVerdict(False, "el destino no presentó un certificado de --ca-file")
