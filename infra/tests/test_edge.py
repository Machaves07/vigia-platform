"""Pila ``vigia-edge`` (TASK-147): zona, certificados, balanceadores, almacén de confianza y
cortafuegos.

Infrastructure-design §4 con las notas de U-03 (nº 15 y 16/19) y las de 2026-09-23 (§4.3: salud,
``404`` de ``/health/ready``, ``tg-api-nodes``; §4.4: nº 11 y tamaño de cuerpo), la tabla D-8 de
§2.1, el orden corregido del primer despliegue (deployment-architecture §5, pasos 3 y 8) y la
adenda (A-19 y A-20). Cada prueba lee la plantilla sintetizada; las rutas de ``vigia-alb-app``
se comprueban además con un evaluador de sus reglas en el orden del balanceador.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from fnmatch import fnmatchcase
from functools import cache
from typing import Any

import pytest

from config import EnvironmentConfig, NodesTlsMode
from stacks.edge import MTLS_HEADERS, nodes_host, target_group_name
from tests.conftest import Synthesized, synthesize
from tests.template_rules import (
    check_ephemeral_teardown,
    check_policy_wildcards,
    describe,
    properties,
    reference_target,
    render,
    resources,
)

JsonObject = Mapping[str, Any]
ACCOUNT = "<AccountId>"
LOGS_BUCKET = re.compile(rf"^vigia-logs(-[a-z0-9-]+)?-{ACCOUNT}-us-east-1$")
TLS_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"
LOAD_BALANCER = "AWS::ElasticLoadBalancingV2::LoadBalancer"
LISTENER = "AWS::ElasticLoadBalancingV2::Listener"
LISTENER_RULE = "AWS::ElasticLoadBalancingV2::ListenerRule"
TARGET_GROUP = "AWS::ElasticLoadBalancingV2::TargetGroup"
TRUST_STORE = "AWS::ElasticLoadBalancingV2::TrustStore"
# Ventanas de evaluación que admite una regla de tasa y su límite mínimo (hipótesis (d) de R14
# en U-03).
WAF_RATE_WINDOWS = {60, 120, 300, 600}
WAF_RATE_MIN_LIMIT = 10


def expected_edge_resources(config: EnvironmentConfig) -> Counter[str]:
    """Recursos de ``vigia-edge`` por despliegue (sin ``AWS::CDK::Metadata``)."""
    passthrough = config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH
    mtls_listener = not passthrough and not config.first_deploy
    expected: Counter[str] = Counter(
        {
            "AWS::EC2::SecurityGroup": 2,
            "AWS::EC2::SecurityGroupIngress": 2,
            "AWS::EC2::SecurityGroupEgress": 2,
            TARGET_GROUP: 2,
            LOAD_BALANCER: 2,
            # La de personas; la de nodos, salvo sin raíz (``first_deploy`` con ``mtls``).
            LISTENER: 1 + (1 if passthrough or mtls_listener else 0),
            # Seis cabeceras, la alta, los nodos y ``/health/ready``; más la de ``nodes.``.
            LISTENER_RULE: 9 + (1 if mtls_listener else 0),
            # En la contingencia, TLS de ``nodes.`` lo termina la aplicación.
            "AWS::CertificateManager::Certificate": 1 if passthrough else 2,
            "AWS::Route53::RecordSet": 2,
        }
    )
    if mtls_listener:
        expected += Counter({TRUST_STORE: 1})
    if config.hosted_zone_owned:
        expected += Counter({"AWS::Route53::HostedZone": 1, "AWS::SSM::Parameter": 2})
    if config.waf_enabled:
        expected += Counter(
            {
                "AWS::WAFv2::WebACL": 1,
                "AWS::WAFv2::WebACLAssociation": 1,
                "AWS::WAFv2::LoggingConfiguration": 1,
                "AWS::Logs::LogGroup": 1,
                "AWS::CloudWatch::Alarm": 1,
            }
        )
    return expected


# --- Síntesis --------------------------------------------------------------------------


@cache
def _synth(**context: str) -> Synthesized:
    return synthesize(None, **context)


def _edge(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("edge")]


def _of_type(template: JsonObject, kind: str) -> Iterator[tuple[str, JsonObject]]:
    for logical_id, resource in resources(template):
        if resource["Type"] == kind:
            yield logical_id, resource


def _one(template: JsonObject, kind: str) -> tuple[str, JsonObject]:
    (found,) = _of_type(template, kind)
    return found


def _attributes(resource: JsonObject, key: str) -> dict[str, Any]:
    return {a["Key"]: a["Value"] for a in properties(resource).get(key, [])}


def _named(template: JsonObject, kind: str) -> dict[str, tuple[str, JsonObject]]:
    """Recursos de un tipo por su nombre físico (``Name``)."""
    return {
        render(properties(r)["Name"], template): (logical_id, r)
        for logical_id, r in _of_type(template, kind)
    }


def _load_balancer(template: JsonObject, name: str) -> tuple[str, JsonObject]:
    return _named(template, LOAD_BALANCER)[name]


def _listeners_of(template: JsonObject, load_balancer_id: str) -> list[tuple[str, JsonObject]]:
    return [
        (logical_id, r)
        for logical_id, r in _of_type(template, LISTENER)
        if reference_target(properties(r)["LoadBalancerArn"]) == load_balancer_id
    ]


def _rules_of(template: JsonObject, listener_id: str) -> list[JsonObject]:
    rules = [
        properties(r)
        for _, r in _of_type(template, LISTENER_RULE)
        if reference_target(properties(r)["ListenerArn"]) == listener_id
    ]
    return sorted(rules, key=lambda rule: int(rule["Priority"]))


def _group_ids(template: JsonObject) -> dict[str, str]:
    return {
        str(properties(r)["GroupName"]): logical_id
        for logical_id, r in _of_type(template, "AWS::EC2::SecurityGroup")
    }


def _app(deployment: Synthesized) -> tuple[JsonObject, str, str]:
    """Plantilla, escucha HTTPS de ``vigia-alb-app`` y su grupo ``tg-api``."""
    template = _edge(deployment)
    config = deployment.config
    alb_id, _ = _load_balancer(template, config.resource_name("alb-app"))
    ((listener_id, _),) = _listeners_of(template, alb_id)
    tg_id, _ = _named(template, TARGET_GROUP)[target_group_name(config, "tg-api")]
    return template, listener_id, tg_id


# --- Evaluador de las reglas de un balanceador de aplicación -------------------------------


def _matches(condition: JsonObject, path: str, headers: Mapping[str, str]) -> bool:
    """``path-pattern`` y ``http-header`` como los evalúa el balanceador: ``*`` y ``?``
    comodines, ruta sensible a mayúsculas, nombre de cabecera insensible."""
    if condition["Field"] == "path-pattern":
        values = condition["PathPatternConfig"]["Values"]
        return any(fnmatchcase(path, value) for value in values)
    if condition["Field"] == "http-header":
        config = condition["HttpHeaderConfig"]
        wanted = config["HttpHeaderName"].lower()
        present = {name.lower(): value for name, value in headers.items()}
        return wanted in present and any(
            fnmatchcase(present[wanted], value) for value in config["Values"]
        )
    raise AssertionError(f"condición no prevista: {condition['Field']}")


def _route(
    template: JsonObject, listener_id: str, path: str, headers: Mapping[str, str] | None = None
) -> tuple[str, Any]:
    """Acción de la primera regla que casa, en orden de prioridad; si ninguna, la de omisión."""
    headers = headers or {}
    listener = dict(template["Resources"][listener_id]["Properties"])
    for rule in _rules_of(template, listener_id):
        if all(_matches(c, path, headers) for c in rule["Conditions"]):
            (action,) = rule["Actions"]
            break
    else:
        (action,) = listener["DefaultActions"]
    if action["Type"] == "forward":
        return "forward", reference_target(action["TargetGroupArn"])
    return "fixed", int(action["FixedResponseConfig"]["StatusCode"])


# --- Criterio 1: registro de acceso y sin escucha 80 ----------------------------------------


def test_every_load_balancer_logs_access_to_vigia_logs(deployment: Synthesized) -> None:
    """SECURITY-02: ``alb/app/`` y ``alb/nodes/`` en el ``vigia-logs`` del despliegue."""
    template = _edge(deployment)
    config = deployment.config
    prefixes = {
        config.resource_name("alb-app"): "alb/app",
        config.resource_name("alb-nodes"): "alb/nodes",
        config.resource_name("nlb-nodes"): "alb/nodes",
    }
    found = _named(template, LOAD_BALANCER)
    assert len(found) == 2
    for name, (_, load_balancer) in found.items():
        attributes = _attributes(load_balancer, "LoadBalancerAttributes")
        assert attributes["access_logs.s3.enabled"] == "true", name
        bucket = render(attributes["access_logs.s3.bucket"], template)
        assert bucket == config.bucket_name("logs", ACCOUNT), name
        assert LOGS_BUCKET.match(bucket), name
        assert attributes["access_logs.s3.prefix"] == prefixes[name], name


def test_no_listener_on_port_80_and_only_443_is_public(deployment: Synthesized) -> None:
    template = _edge(deployment)
    listeners = list(_of_type(template, LISTENER))
    assert listeners
    for logical_id, listener in listeners:
        assert properties(listener)["Port"] == 443, logical_id
    for _, group in _of_type(template, "AWS::EC2::SecurityGroup"):
        ingress = properties(group)["SecurityGroupIngress"]
        assert [(r["IpProtocol"], r["FromPort"], r["ToPort"], r["CidrIp"]) for r in ingress] == [
            ("tcp", 443, 443, "0.0.0.0/0")
        ]
    assert not re.search(r'"(Port|FromPort|ToPort)": 80[,}]', json.dumps(template))


def test_https_listeners_use_the_tls_1_2_and_1_3_policy(deployment: Synthesized) -> None:
    template = _edge(deployment)
    for logical_id, listener in _of_type(template, LISTENER):
        props = properties(listener)
        if props["Protocol"] == "HTTPS":
            assert props["SslPolicy"] == TLS_POLICY, logical_id
            assert len(props["Certificates"]) == 1, logical_id
        else:
            assert deployment.config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH, logical_id
            assert props["Protocol"] == "TCP", logical_id


# --- Criterio 2: reglas de vigia-alb-app ----------------------------------------------------


def test_enrollment_rule_is_evaluated_before_the_nodes_not_found(pilot: Synthesized) -> None:
    """Nº 15: ``/api/nodes/enrollment`` exacto hacia ``tg-api`` con prioridad menor que el
    ``404`` fijo de ``/api/nodes/*``."""
    template, listener_id, tg_id = _app(pilot)
    rules = _rules_of(template, listener_id)

    def priority_of(paths: list[str]) -> int:
        (rule,) = [
            r
            for r in rules
            if r["Conditions"][0].get("PathPatternConfig", {}).get("Values") == paths
        ]
        return int(rule["Priority"])

    enrollment = priority_of(["/api/nodes/enrollment"])
    nodes = priority_of(["/api/nodes", "/api/nodes/*"])
    ready = priority_of(["/health/ready", "/health/ready/*"])
    assert enrollment < nodes
    (enrollment_rule,) = [r for r in rules if int(r["Priority"]) == enrollment]
    assert enrollment_rule["Actions"] == [{"TargetGroupArn": {"Ref": tg_id}, "Type": "forward"}]
    for priority in (nodes, ready):
        (rule,) = [r for r in rules if int(r["Priority"]) == priority]
        assert rule["Actions"] == [
            {
                "FixedResponseConfig": {"ContentType": "text/plain", "StatusCode": "404"},
                "Type": "fixed-response",
            }
        ]
    assert len({int(r["Priority"]) for r in rules}) == len(rules)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/nodes/enrollment", "tg-api"),
        # Bordes de la alta: solo la ruta exacta pasa.
        ("/api/nodes/enrollment/", 404),
        ("/api/nodes/enrollmentx", 404),
        ("/api/nodes/enrollment/extra", 404),
        ("/api/nodes/Enrollment", 404),
        ("/api/nodes/heartbeat", 404),
        ("/api/nodes/", 404),
        ("/api/nodes", 404),
        ("/api/nodes/a/b/c", 404),
        # La salud profunda no se ve desde Internet; la superficial sí.
        ("/health/ready", 404),
        ("/health/ready/", 404),
        ("/health/ready/deep", 404),
        ("/health/live", "tg-api"),
        # El resto de la aplicación y de la API de personas.
        ("/", "tg-api"),
        ("/api/sessions", "tg-api"),
        ("/api/nodesx", "tg-api"),
        ("/assets/index-abc.js", "tg-api"),
        ("/version.json", "tg-api"),
    ],
)
def test_app_routes_in_load_balancer_order(pilot: Synthesized, path: str, expected: object) -> None:
    template, listener_id, tg_id = _app(pilot)
    route = _route(template, listener_id, path)
    assert route == (("forward", tg_id) if expected == "tg-api" else ("fixed", expected))


@pytest.mark.parametrize("header", MTLS_HEADERS)
@pytest.mark.parametrize("path", ["/api/nodes/enrollment", "/", "/api/nodes/heartbeat"])
@pytest.mark.parametrize("value", ["CN=forged", "", "x" * 2048])
def test_app_rejects_forged_mtls_headers_on_every_route(
    pilot: Synthesized, header: str, path: str, value: str
) -> None:
    """Nadie hace llegar una cabecera ``X-Amzn-Mtls-*`` a la aplicación por ``app.``, ni en
    minúsculas ni vacía, y la regla va antes que la de la alta."""
    template, listener_id, _ = _app(pilot)
    assert _route(template, listener_id, path, {header: value}) == ("fixed", 400)
    assert _route(template, listener_id, path, {header.lower(): value}) == ("fixed", 400)


def test_mtls_header_rules_cover_every_header_the_load_balancer_adds(pilot: Synthesized) -> None:
    template, listener_id, _ = _app(pilot)
    rejected = [
        r["Conditions"][0]["HttpHeaderConfig"]["HttpHeaderName"]
        for r in _rules_of(template, listener_id)
        if r["Conditions"][0]["Field"] == "http-header"
    ]
    assert sorted(rejected) == sorted(MTLS_HEADERS)
    assert all(h.startswith("X-Amzn-Mtls-Clientcert") for h in rejected)
    # Las cinco del modo de verificación y la del paso directo (documentación del servicio).
    assert len(set(rejected)) == 6


def test_app_load_balancer_is_public_and_drops_invalid_headers(
    deployment: Synthesized,
) -> None:
    template = _edge(deployment)
    config = deployment.config
    _, alb = _load_balancer(template, config.resource_name("alb-app"))
    props = properties(alb)
    assert props["Scheme"] == "internet-facing"
    assert props["Type"] == "application"
    attributes = _attributes(alb, "LoadBalancerAttributes")
    assert attributes["routing.http.drop_invalid_header_fields.enabled"] == "true"
    # ``staging-<n>`` se destruye entero (D-8); los permanentes, protegidos (§4.2).
    assert attributes["deletion_protection.enabled"] == ("false" if config.ephemeral else "true")
    groups = _group_ids(template)
    assert [reference_target(g) for g in props["SecurityGroups"]] == [groups["sg-alb-app"]]


# --- Grupos de destino -----------------------------------------------------------------------


def test_api_target_group_checks_ready_and_drains_in_30_seconds(deployment: Synthesized) -> None:
    template = _edge(deployment)
    config = deployment.config
    _, tg = _named(template, TARGET_GROUP)[target_group_name(config, "tg-api")]
    props = properties(tg)
    assert (props["TargetType"], props["Protocol"], props["Port"]) == ("ip", "HTTP", 8000)
    assert (props["HealthCheckPath"], props["HealthCheckProtocol"]) == ("/health/ready", "HTTP")
    assert props["HealthCheckIntervalSeconds"] == 30
    assert props["HealthCheckTimeoutSeconds"] == 5
    assert props["HealthyThresholdCount"] == 2
    assert props["UnhealthyThresholdCount"] == 2
    assert props["Matcher"] == {"HttpCode": "200"}
    attributes = _attributes(tg, "TargetGroupAttributes")
    assert attributes["deregistration_delay.timeout_seconds"] == "30"
    assert attributes.get("stickiness.enabled", "false") == "false"


def test_nodes_have_their_own_target_group(deployment: Synthesized) -> None:
    """Hipótesis (g) de R14: un grupo no se comparte entre dos balanceadores."""
    template = _edge(deployment)
    config = deployment.config
    groups = _named(template, TARGET_GROUP)
    assert set(groups) == {
        target_group_name(config, "tg-api"),
        target_group_name(config, "tg-api-nodes"),
    }
    _, tg = groups[target_group_name(config, "tg-api-nodes")]
    props = properties(tg)
    attributes = _attributes(tg, "TargetGroupAttributes")
    assert attributes["deregistration_delay.timeout_seconds"] == "30"
    if config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH:
        assert (props["Protocol"], props["Port"], props["HealthCheckProtocol"]) == (
            "TCP",
            8443,
            "TCP",
        )
    else:
        assert (props["Protocol"], props["Port"]) == ("HTTP", 8000)
        assert (props["HealthCheckPath"], props["HealthCheckProtocol"]) == ("/health/live", "HTTP")
    assert props["TargetType"] == "ip"


def test_load_balancer_groups_reach_only_sg_api(deployment: Synthesized) -> None:
    """§3: ``sg-alb-*`` salen solo hacia ``sg-api`` y ``sg-api`` los admite solo a ellos, en
    8000 (8443 en la contingencia)."""
    template = _edge(deployment)
    nodes_port = 8443 if deployment.config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH else 8000
    ports = {"sg-alb-app": 8000, "sg-alb-nodes": nodes_port}
    groups = _group_ids(template)
    egress = {
        reference_target(properties(r)["GroupId"]): properties(r)
        for _, r in _of_type(template, "AWS::EC2::SecurityGroupEgress")
    }
    ingress = {
        reference_target(properties(r)["SourceSecurityGroupId"]): properties(r)
        for _, r in _of_type(template, "AWS::EC2::SecurityGroupIngress")
    }
    for name, port in ports.items():
        out, into = egress[groups[name]], ingress[groups[name]]
        assert (out["FromPort"], out["ToPort"], out["IpProtocol"]) == (port, port, "tcp")
        assert (into["FromPort"], into["ToPort"], into["IpProtocol"]) == (port, port, "tcp")
        # El mismo ``sg-api`` de ``vigia-foundation`` por las dos vías.
        assert "Fn::GetStackOutput" in out["DestinationSecurityGroupId"]
        assert into["GroupId"] == out["DestinationSecurityGroupId"]
    for _, group in _of_type(template, "AWS::EC2::SecurityGroup"):
        assert "SecurityGroupEgress" not in properties(group) or all(
            rule.get("CidrIp") != "0.0.0.0/0" for rule in properties(group)["SecurityGroupEgress"]
        )


# --- Criterio 3: primer despliegue en dos pasadas --------------------------------------------


@pytest.mark.parametrize(
    ("first_deploy", "ca_rotation"), [(True, False), (True, True)], ids=["first", "first-rotation"]
)
def test_first_deploy_has_no_trust_store_nor_mutual_authentication(
    first_deploy: bool, ca_rotation: bool
) -> None:
    """Paso 3: la raíz aún no existe, así que ni ``vigia-node-trust`` ni la escucha de ``nodes.``;
    el balanceador, su grupo, su certificado y su nombre sí (el paso 8 solo añade la escucha)."""
    deployment = _synth(
        first_deploy=str(first_deploy).lower(), ca_rotation=str(ca_rotation).lower()
    )
    template = _edge(deployment)
    assert list(_of_type(template, TRUST_STORE)) == []
    assert "MutualAuthentication" not in json.dumps(template)
    alb_id, _ = _load_balancer(template, "vigia-alb-nodes")
    assert _listeners_of(template, alb_id) == []
    assert target_group_name(deployment.config, "tg-api-nodes") in _named(template, TARGET_GROUP)
    certificates = [
        render(properties(c)["DomainName"], template)
        for _, c in _of_type(template, "AWS::CertificateManager::Certificate")
    ]
    assert any(name.startswith("nodes.") for name in certificates)


@pytest.mark.parametrize("context", [{}, {"ca_rotation": "true"}], ids=["pilot", "ca-rotation"])
def test_second_pass_creates_the_trust_store_from_the_published_root(
    context: dict[str, str],
) -> None:
    """Paso 8: ``vigia-node-trust`` con ``vigia-edge/ca/root.pem`` y la escucha 443 con
    autenticación mutua en modo de verificación que no ignora la caducidad."""
    deployment = _synth(first_deploy="false", **context)
    template = _edge(deployment)
    trust_id, trust = _one(template, TRUST_STORE)
    props = properties(trust)
    assert props["Name"] == "vigia-node-trust"
    assert render(props["CaCertificatesBundleS3Bucket"], template) == (
        f"vigia-edge-{ACCOUNT}-us-east-1"
    )
    assert props["CaCertificatesBundleS3Key"] == "ca/root.pem"
    # La lista de revocación la añade el worker (A-19), no la plantilla.
    assert "AWS::ElasticLoadBalancingV2::TrustStoreRevocation" not in json.dumps(template)

    alb_id, _ = _load_balancer(template, "vigia-alb-nodes")
    ((listener_id, listener),) = _listeners_of(template, alb_id)
    listener_props = properties(listener)
    assert listener_props["Protocol"] == "HTTPS"
    assert listener_props["MutualAuthentication"] == {
        "IgnoreClientCertificateExpiry": False,
        "Mode": "verify",
        "TrustStoreArn": {"Fn::GetAtt": [trust_id, "TrustStoreArn"]},
    }
    assert listener_props["DefaultActions"] == [
        {
            "FixedResponseConfig": {"ContentType": "text/plain", "StatusCode": "404"},
            "Type": "fixed-response",
        }
    ]
    tg_id, _ = _named(template, TARGET_GROUP)["tg-api-nodes"]
    assert _route(template, listener_id, "/api/nodes/heartbeat") == ("forward", tg_id)
    assert _route(template, listener_id, "/api/nodes/enrollment") == ("forward", tg_id)
    for path in ("/", "/api/nodes", "/health/ready", "/health/live", "/api/sessions"):
        assert _route(template, listener_id, path) == ("fixed", 404), path


def test_mutual_authentication_listener_exists_only_when_the_root_does(
    deployment: Synthesized,
) -> None:
    config = deployment.config
    template = _edge(deployment)
    expected = config.nodes_tls_mode is NodesTlsMode.MTLS and not config.first_deploy
    assert (len(list(_of_type(template, TRUST_STORE))) == 1) is expected
    assert ("MutualAuthentication" in json.dumps(template)) is expected


# --- Criterio 4: staging-7 sin zona ni cortafuegos ------------------------------------------


def test_staging_imports_the_zone_and_has_no_firewall(staging: Synthesized) -> None:
    template = _edge(staging)
    for kind in (
        "AWS::Route53::HostedZone",
        "AWS::WAFv2::WebACL",
        "AWS::WAFv2::WebACLAssociation",
        "AWS::WAFv2::LoggingConfiguration",
        "AWS::CloudWatch::Alarm",
        "AWS::Logs::LogGroup",
        "AWS::SSM::Parameter",
    ):
        assert list(_of_type(template, kind)) == [], kind
    defaults = {p["Default"] for p in template["Parameters"].values()}
    assert {"/vigia/pilot/zone-id", "/vigia/pilot/zone-name"} <= defaults
    violations = check_ephemeral_teardown("vigia-edge-staging-7", template, deployment="staging-7")
    assert violations == [], describe(violations)


def test_staging_names_are_created_and_deleted_with_the_environment(staging: Synthesized) -> None:
    """D-8: ``staging-<n>.<dominio>`` y ``staging-<n>-nodes.<dominio>`` en la zona de ``pilot``."""
    template = _edge(staging)
    records = sorted(
        render(properties(r)["Name"], template)
        for _, r in _of_type(template, "AWS::Route53::RecordSet")
    )
    zone_name = next(
        f"<Ref.{k}>"
        for k, p in template["Parameters"].items()
        if p["Default"] == "/vigia/pilot/zone-name"
    )
    assert records == [f"staging-7-nodes.{zone_name}.", f"staging-7.{zone_name}."]
    for _, record in _of_type(template, "AWS::Route53::RecordSet"):
        assert "DeletionPolicy" not in record
        assert render(properties(record)["HostedZoneId"], template).startswith("<Ref.SsmParameter")
    certificates = sorted(
        render(properties(c)["DomainName"], template)
        for _, c in _of_type(template, "AWS::CertificateManager::Certificate")
    )
    assert certificates == [f"staging-7-nodes.{zone_name}", f"staging-7.{zone_name}"]
    for name, (_, lb) in _named(template, LOAD_BALANCER).items():
        assert name.endswith("-staging-7"), name
        attributes = _attributes(lb, "LoadBalancerAttributes")
        assert attributes["deletion_protection.enabled"] == "false", name


def test_pilot_hosts_and_publishes_vigia_zone(permanent_deployment: Synthesized) -> None:
    template = _edge(permanent_deployment)
    config = permanent_deployment.config
    zone_id, zone = _one(template, "AWS::Route53::HostedZone")
    assert zone["DeletionPolicy"] == "Retain"
    assert properties(zone)["HostedZoneConfig"]["Comment"].startswith("vigia-zone")
    parameters = {
        str(properties(p)["Name"]): properties(p)["Value"]
        for _, p in _of_type(template, "AWS::SSM::Parameter")
    }
    deployment = config.deployment
    assert set(parameters) == {f"/vigia/{deployment}/zone-id", f"/vigia/{deployment}/zone-name"}
    assert parameters[f"/vigia/{deployment}/zone-id"] == {"Ref": zone_id}
    domain = next(k for k, p in template["Parameters"].items() if p["Default"] == "/vigia/domain")
    assert parameters[f"/vigia/{deployment}/zone-name"] == {"Ref": domain}
    records = sorted(
        render(properties(r)["Name"], template)
        for _, r in _of_type(template, "AWS::Route53::RecordSet")
    )
    assert records == [f"app.<Ref.{domain}>.", f"nodes.<Ref.{domain}>."]


def test_certificates_are_validated_by_dns_in_the_zone(deployment: Synthesized) -> None:
    template = _edge(deployment)
    certificates = list(_of_type(template, "AWS::CertificateManager::Certificate"))
    assert certificates
    for logical_id, certificate in certificates:
        props = properties(certificate)
        assert props["ValidationMethod"] == "DNS", logical_id
        (option,) = props["DomainValidationOptions"]
        assert option["DomainName"] == props["DomainName"], logical_id
        assert "HostedZoneId" in option, logical_id


@pytest.mark.parametrize(
    ("context", "app", "nodes"),
    [
        ({}, "app", "nodes"),
        ({"instance": "acme"}, "app", "nodes"),
        ({"environment": "staging-7"}, "staging-7", "staging-7-nodes"),
        ({"environment": "staging-1234567"}, "staging-1234567", "staging-1234567-nodes"),
    ],
)
def test_hosts_follow_the_environment(context: dict[str, str], app: str, nodes: str) -> None:
    from stacks.data import app_host

    config = _synth(**context).config
    assert (app_host(config), nodes_host(config)) == (app, nodes)


# --- Criterio 5: contingencia de R2 --------------------------------------------------------


@pytest.mark.parametrize("environment", ["pilot", "staging-7"])
def test_passthrough_changes_only_vigia_edge(environment: str) -> None:
    baseline = _synth(environment=environment)
    contingency = _synth(environment=environment, nodes_tls_mode="passthrough")
    assert contingency.stack_names == baseline.stack_names
    edge = baseline.config.stack_name("edge")
    for name in baseline.stack_names:
        same = json.dumps(contingency.templates[name], sort_keys=True) == json.dumps(
            baseline.templates[name], sort_keys=True
        )
        assert same is (name != edge), name


def test_passthrough_is_a_network_load_balancer_straight_to_the_application() -> None:
    deployment = _synth(nodes_tls_mode="passthrough")
    template = _edge(deployment)
    nlb_id, nlb = _load_balancer(template, "vigia-nlb-nodes")
    props = properties(nlb)
    assert (props["Type"], props["Scheme"]) == ("network", "internet-facing")
    groups = _group_ids(template)
    assert [reference_target(g) for g in props["SecurityGroups"]] == [groups["sg-alb-nodes"]]
    attributes = _attributes(nlb, "LoadBalancerAttributes")
    assert attributes["load_balancing.cross_zone.enabled"] == "true"
    assert attributes["deletion_protection.enabled"] == "true"
    ((_, listener),) = _listeners_of(template, nlb_id)
    listener_props = properties(listener)
    assert (listener_props["Protocol"], listener_props["Port"]) == ("TCP", 443)
    assert "Certificates" not in listener_props
    assert "MutualAuthentication" not in listener_props
    tg_id, _ = _named(template, TARGET_GROUP)["tg-api-nodes"]
    assert listener_props["DefaultActions"] == [
        {"TargetGroupArn": {"Ref": tg_id}, "Type": "forward"}
    ]
    assert list(_of_type(template, TRUST_STORE)) == []
    certificates = [
        render(properties(c)["DomainName"], template)
        for _, c in _of_type(template, "AWS::CertificateManager::Certificate")
    ]
    assert [c.split(".")[0] for c in certificates] == ["app"]
    # vigia-alb-app no cambia con la contingencia.
    pilot_app = _app(_synth())
    contingency_app = _app(deployment)
    assert _rules_of(pilot_app[0], pilot_app[1]) == _rules_of(
        contingency_app[0], contingency_app[1]
    )


# --- Cortafuegos (§4.4, nº 11 y nº 15) -----------------------------------------------------


def _web_acl(deployment: Synthesized) -> tuple[JsonObject, str, JsonObject]:
    template = _edge(deployment)
    acl_id, acl = _one(template, "AWS::WAFv2::WebACL")
    return template, acl_id, properties(acl)


def _waf_rules(props: JsonObject) -> dict[str, JsonObject]:
    return {rule["Name"]: rule for rule in props["Rules"]}


def test_firewall_exists_only_where_the_config_enables_it(deployment: Synthesized) -> None:
    template = _edge(deployment)
    enabled = deployment.config.waf_enabled
    assert enabled is (not deployment.config.ephemeral)
    assert (len(list(_of_type(template, "AWS::WAFv2::WebACL"))) == 1) is enabled


def test_firewall_protects_the_app_load_balancer(permanent_deployment: Synthesized) -> None:
    template, acl_id, props = _web_acl(permanent_deployment)
    config = permanent_deployment.config
    assert props["Name"] == config.resource_name("app-waf")
    assert props["Scope"] == "REGIONAL"
    assert props["DefaultAction"] == {"Allow": {}}
    _, association = _one(template, "AWS::WAFv2::WebACLAssociation")
    alb_id, _ = _load_balancer(template, config.resource_name("alb-app"))
    assert reference_target(properties(association)["ResourceArn"]) == alb_id
    assert reference_target(properties(association)["WebACLArn"]) == acl_id


def test_firewall_rate_rules_follow_11_and_15(pilot: Synthesized) -> None:
    _, _, props = _web_acl(pilot)
    rules = _waf_rules(props)
    general = rules["general-rate"]["Statement"]["RateBasedStatement"]
    assert (general["Limit"], general["EvaluationWindowSec"]) == (6000, 300)
    assert general["AggregateKeyType"] == "IP"
    assert "ScopeDownStatement" not in general
    enrollment = rules["enrollment-rate"]["Statement"]["RateBasedStatement"]
    assert (enrollment["Limit"], enrollment["EvaluationWindowSec"]) == (20, 600)
    assert enrollment["AggregateKeyType"] == "IP"
    assert enrollment["ScopeDownStatement"] == {
        "ByteMatchStatement": {
            "FieldToMatch": {"UriPath": {}},
            "PositionalConstraint": "EXACTLY",
            "SearchString": "/api/nodes/enrollment",
            "TextTransformations": [{"Priority": 0, "Type": "URL_DECODE"}],
        }
    }
    for name in ("general-rate", "enrollment-rate"):
        assert rules[name]["Action"] == {"Block": {}}, name
        statement = rules[name]["Statement"]["RateBasedStatement"]
        assert statement["EvaluationWindowSec"] in WAF_RATE_WINDOWS, name
        assert statement["Limit"] >= WAF_RATE_MIN_LIMIT, name
    # La de la alta, más estrecha, se evalúa antes que la general.
    assert rules["enrollment-rate"]["Priority"] < rules["general-rate"]["Priority"]


def test_firewall_managed_groups_block_except_the_body_size_rule(pilot: Synthesized) -> None:
    """§4.4 y nº 11: tres grupos gestionados en bloqueo; ``SizeRestrictions_BODY`` del conjunto
    común solo cuenta, para no bloquear los borradores de 8 000 caracteres de U-05."""
    _, _, props = _web_acl(pilot)
    rules = _waf_rules(props)
    managed = {
        name: rule
        for name, rule in rules.items()
        if "ManagedRuleGroupStatement" in rule["Statement"]
    }
    assert set(managed) == {
        "AWSManagedRulesCommonRuleSet",
        "AWSManagedRulesKnownBadInputsRuleSet",
        "AWSManagedRulesAmazonIpReputationList",
    }
    for name, rule in managed.items():
        assert rule["OverrideAction"] == {"None": {}}, name
        statement = rule["Statement"]["ManagedRuleGroupStatement"]
        assert (statement["VendorName"], statement["Name"]) == ("AWS", name)
        overrides = statement.get("RuleActionOverrides", [])
        if name == "AWSManagedRulesCommonRuleSet":
            assert overrides == [{"ActionToUse": {"Count": {}}, "Name": "SizeRestrictions_BODY"}]
        else:
            assert overrides == [], name
    priorities = [rule["Priority"] for rule in props["Rules"]]
    assert len(set(priorities)) == len(priorities)
    for rule in props["Rules"]:
        visibility = rule["VisibilityConfig"]
        assert visibility["CloudWatchMetricsEnabled"] is True
        assert visibility["MetricName"] == rule["Name"]


def test_firewall_logs_90_days_encrypted_with_vigia_logs(
    permanent_deployment: Synthesized,
) -> None:
    template, acl_id, _ = _web_acl(permanent_deployment)
    config = permanent_deployment.config
    group_id, group = _one(template, "AWS::Logs::LogGroup")
    props = properties(group)
    assert props["LogGroupName"] == f"aws-waf-logs-{config.resource_name('app')}"
    assert props["RetentionInDays"] == 90
    assert "Fn::GetStackOutput" in props["KmsKeyId"]
    assert group["DeletionPolicy"] == "Retain"
    _, logging = _one(template, "AWS::WAFv2::LoggingConfiguration")
    logging_props = properties(logging)
    assert reference_target(logging_props["ResourceArn"]) == acl_id
    (destination,) = logging_props["LogDestinationConfigs"]
    assert render(destination, template) == (
        f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:{props['LogGroupName']}"
    )
    assert group_id in logging["DependsOn"]


def test_rate_blocks_warn_vigia_alerts(permanent_deployment: Synthesized) -> None:
    """Nº 11: todo bloqueo de la regla de tasa general avisa a ``vigia-alerts``."""
    template, _, props = _web_acl(permanent_deployment)
    _, alarm = _one(template, "AWS::CloudWatch::Alarm")
    alarm_props = properties(alarm)
    assert alarm_props["Namespace"] == "AWS/WAFV2"
    assert alarm_props["MetricName"] == "BlockedRequests"
    dimensions = {d["Name"]: d["Value"] for d in alarm_props["Dimensions"]}
    assert dimensions == {"WebACL": props["Name"], "Region": "us-east-1", "Rule": "general-rate"}
    assert (alarm_props["Statistic"], alarm_props["Period"]) == ("Sum", 300)
    assert alarm_props["Threshold"] == 1
    assert alarm_props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert alarm_props["TreatMissingData"] == "notBreaching"
    (action,) = alarm_props["AlarmActions"]
    output = action["Fn::GetStackOutput"]
    config = permanent_deployment.config
    assert output["StackName"] == config.stack_name("foundation")
    foundation = permanent_deployment.templates[config.stack_name("foundation")]
    target = reference_target(foundation["Outputs"][output["OutputName"]]["Value"])
    assert foundation["Resources"][target]["Type"] == "AWS::SNS::Topic"


# --- Registros de acceso en el depósito propio (staging e instancia dedicada) -------------


@pytest.mark.parametrize("context", [{"environment": "staging-7"}, {"instance": "acme"}])
def test_owned_logs_bucket_accepts_load_balancer_delivery(context: dict[str, str]) -> None:
    """``vigia-data`` concede la entrega en ``alb/app/`` y ``alb/nodes/``; ``vigia-edge`` importa
    el depósito por nombre, así que la política no cambia con ``nodes_tls_mode``."""
    deployment = _synth(**context)
    data = deployment.templates[deployment.config.stack_name("data")]
    logs_name = deployment.config.bucket_name("logs", ACCOUNT)
    (logs_id,) = [
        logical_id
        for logical_id, r in _of_type(data, "AWS::S3::Bucket")
        if render(properties(r)["BucketName"], data) == logs_name
    ]
    (policy,) = [
        properties(p)["PolicyDocument"]["Statement"]
        for _, p in _of_type(data, "AWS::S3::BucketPolicy")
        if reference_target(properties(p)["Bucket"]) == logs_id
    ]
    statements = {s["Sid"]: s for s in policy if "Sid" in s}
    elb = statements["LoadBalancerLogDelivery"]
    assert render(elb["Principal"]["AWS"], data) == "arn:aws:iam::127311923021:root"
    assert elb["Action"] == "s3:PutObject"
    expected = [
        f"arn:<Partition>:s3:::{logs_name}/alb/app/AWSLogs/{ACCOUNT}/*",
        f"arn:<Partition>:s3:::{logs_name}/alb/nodes/AWSLogs/{ACCOUNT}/*",
    ]
    assert [render(r, data) for r in elb["Resource"]] == expected
    network = statements["NetworkLoadBalancerLogDelivery"]
    assert network["Principal"] == {"Service": "delivery.logs.amazonaws.com"}
    assert [render(r, data) for r in network["Resource"]] == expected
    assert network["Condition"]["StringEquals"]["s3:x-amz-acl"] == "bucket-owner-full-control"
    acl_check = statements["NetworkLoadBalancerLogAclCheck"]
    assert acl_check["Action"] == "s3:GetBucketAcl"
    assert render(acl_check["Resource"], data) == f"arn:<Partition>:s3:::{logs_name}"
    violations = check_policy_wildcards("vigia-data", data)
    assert violations == [], describe(violations)


# --- Nombres ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (
            {},
            {"vigia-alb-app", "vigia-alb-nodes", "tg-api", "tg-api-nodes", "vigia-node-trust"},
        ),
        (
            {"environment": "staging-7"},
            {
                "vigia-alb-app-staging-7",
                "vigia-alb-nodes-staging-7",
                "tg-api-staging-7",
                "tg-api-nodes-staging-7",
                "vigia-node-trust-staging-7",
            },
        ),
        (
            {"instance": "acme"},
            {
                "vigia-alb-app-acme",
                "vigia-alb-nodes-acme",
                "tg-api-acme",
                "tg-api-nodes-acme",
                "vigia-node-trust-acme",
            },
        ),
        (
            {"instance": "abcdefghij01234"},
            {
                "vigia-alb-app-abcdefghij01234",
                "vigia-alb-nodes-abcdefghij01234",
                "tg-api-abcdefghij01234",
                "tg-api-nodes-abcdefghij01234",
                "vigia-node-trust-abcdefghij01234",
            },
        ),
    ],
    ids=["pilot", "staging-7", "dedicated", "longest-instance"],
)
def test_load_balancing_names_carry_the_suffix_and_fit_32(
    context: dict[str, str], expected: set[str]
) -> None:
    template = _edge(_synth(**context))
    names = {
        name
        for kind in (LOAD_BALANCER, TARGET_GROUP, TRUST_STORE)
        for name in _named(template, kind)
    }
    assert names == expected
    assert all(len(name) <= 32 for name in names)
