"""Comprobaciones de despliegue de ``tools/deploy_checks.py`` (TASK-151 y TASK-234; §3.2 y A-27).

Sin AWS ni red: HTTP, la CLI ``aws``, los sockets, el runner de conformidad y el material de
certificados son dobles que registran lo que se les pide. Cada comprobación se prueba en verde y
en cada forma de fallo, y la regla de omisión en sus dos lados: omitida mientras falte su
requisito, fallida si el requisito ya existe y la comprobación no está escrita.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tools import deploy_checks
from tools.deploy_checks import (
    Certificate,
    ConformanceVerdict,
    Fixtures,
    Response,
    Result,
    Target,
    run_checks,
)

REPOSITORY = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
VERIFIER = "b" * 64
TOTP_URI = "otpauth://totp/Vigia:op?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Vigia"
TG_ARN = "arn:aws:elasticloadbalancing:us-east-1:111:targetgroup/tg-api-staging-7/abc"
LB_ARN = "arn:aws:elasticloadbalancing:us-east-1:111:loadbalancer/app/vigia-alb-app/def"
STORE_ARN = "arn:aws:elasticloadbalancing:us-east-1:111:truststore/vigia-node-trust-staging-7/xyz"
ZONE = "01900000-0000-7000-8000-000000000003"
VALID = Certificate("-----BEGIN CERTIFICATE-----\nvalid\n", "-----BEGIN KEY-----\nvalid\n")
REVOKED = Certificate("-----BEGIN CERTIFICATE-----\nrevoked\n", "-----BEGIN KEY-----\nrevoked\n")


def _target(**changes: Any) -> Target:
    base = Target(
        environment="staging-7",
        domain="vigia.example",
        verifier_sha256=VERIFIER,
        repository=REPOSITORY,
        now=NOW,
        watch_minutes=2,
        operator_email="operadora@staging-7.vigia.example",
    )
    return replace(base, **changes)


def _fixtures(**changes: Any) -> Fixtures:
    base = Fixtures(valid_certificate=VALID, revoked_certificate=REVOKED, synthetic_zone_id=ZONE)
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
        sleep=pauses.append,
    )
    numbers = [r.number for r in results]
    assert numbers == sorted(set(numbers)) == list(range(1, 14))
    return {r.number: r for r in results}


# --- Conjunto ---------------------------------------------------------------------------


def test_a_healthy_staging_has_no_failures() -> None:
    results = _run()
    assert {n for n, r in results.items() if r.status == "ok"} == {
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        10,
        11,
        12,
        13,
    }
    assert results[8].status == "skipped" and results[9].detail == "solo en pilot"
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


def test_the_u05_checks_fail_once_the_frontend_exists(tmp_path: Path) -> None:
    repository = _repository(tmp_path, "/api/nodes/enrollment", frontend=True)
    assert _run(_target(repository=repository))[8].status == "failed"
    pilot = _run(_target(repository=repository, environment="pilot", operator_email=None))
    assert pilot[9].status == "failed"


def test_the_current_repository_has_contract_routes_and_enrollment_but_no_frontend() -> None:
    # VIG-152 y TASK-223 publican rutas del contrato y VIG-151 el alta: las comprobaciones de
    # U-03 ya están escritas (TASK-234) y corren contra el material del entorno.
    target = _target()
    assert target.has_contract_routes() and target.has_enrollment()
    assert not target.has_frontend()
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
