"""Pila ``vigia-foundation`` (TASK-145): red, puntos privados, claves, alertas y presupuestos.

Infrastructure-design §3 (red), §7.1 y su nota U02-H-01 (claves), §2.2 (presupuestos), R13 en
§13 (una traducción de direcciones, ``nat_per_az``) y tabla D-8 de §2.1 (``staging-<n>``).
Cada prueba lee la plantilla sintetizada, igual que las reglas de ``template_rules.py``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from functools import cache
from typing import Any

import pytest

from config import EnvironmentConfig
from stacks.foundation import (
    ALERTS_EMAIL_PARAMETER,
    ECR_LAYER_BUCKET,
    KeyName,
    SecurityGroupName,
    budgets_enabled,
    deploy_role_name,
    waf_log_group_name,
)
from tests.conftest import Synthesized, synthesize
from tests.template_rules import (
    check_policy_wildcards,
    check_public_ingress,
    describe,
    properties,
    reference_target,
    render,
    resources,
)

JsonObject = Mapping[str, Any]
FIRST_DEPLOY_FLAGS = [(False, False), (True, False), (False, True), (True, True)]


def expected_foundation_resources(config: EnvironmentConfig) -> Counter[str]:
    """Recursos de ``vigia-foundation`` por despliegue (sin ``AWS::CDK::Metadata``)."""
    nats = 2 if config.nat_per_az else 1
    expected: Counter[str] = Counter(
        {
            "AWS::EC2::VPC": 1,
            "AWS::EC2::InternetGateway": 1,
            "AWS::EC2::VPCGatewayAttachment": 1,
            "AWS::EC2::Subnet": 6,
            "AWS::EC2::RouteTable": 6,
            "AWS::EC2::SubnetRouteTableAssociation": 6,
            # Dos por Internet (públicas) y dos por la traducción (aplicación).
            "AWS::EC2::Route": 4,
            "AWS::EC2::EIP": nats,
            "AWS::EC2::NatGateway": nats,
            "AWS::EC2::FlowLog": 1,
            "AWS::Logs::LogGroup": 1,
            "AWS::EC2::VPCEndpoint": 4,
            "AWS::EC2::SecurityGroup": 5,
            "AWS::EC2::SecurityGroupIngress": 6,
            "AWS::EC2::SecurityGroupEgress": 6,
            # Registros de flujo y el proveedor que vacía el grupo por defecto de la VPC.
            "AWS::IAM::Role": 2,
            "AWS::IAM::Policy": 1,
            "AWS::Lambda::Function": 1,
            "Custom::VpcRestrictDefaultSG": 1,
            "AWS::KMS::Key": 7,
            "AWS::KMS::Alias": 7,
            "AWS::SNS::Topic": 1,
            # Eventos de la base de ``vigia-data`` y, en ``pilot``, presupuestos.
            "AWS::SNS::TopicPolicy": 1,
            "AWS::SNS::Subscription": 1,
            "AWS::SSM::Parameter": 1,
        }
    )
    if budgets_enabled(config):
        expected += Counter({"AWS::Budgets::Budget": 2})
    return expected


# --- Síntesis --------------------------------------------------------------------------


@cache
def _synth(**context: str) -> Synthesized:
    return synthesize(None, **context)


def _flags(first_deploy: bool, ca_rotation: bool) -> dict[str, str]:
    return {
        "first_deploy": "true" if first_deploy else "false",
        "ca_rotation": "true" if ca_rotation else "false",
    }


def _foundation(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("foundation")]


def _of_type(template: JsonObject, kind: str) -> Iterator[tuple[str, JsonObject]]:
    for logical_id, resource in resources(template):
        if resource["Type"] == kind:
            yield logical_id, resource


def _tags(resource: JsonObject) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in properties(resource).get("Tags", [])}


def _named(template: JsonObject, kind: str) -> dict[str, str]:
    """``Name`` de la etiqueta → identificador lógico, para un tipo de recurso."""
    return {_tags(r)["Name"]: logical_id for logical_id, r in _of_type(template, kind)}


def _groups(template: JsonObject) -> dict[str, str]:
    """Nombre del grupo de seguridad → identificador lógico."""
    return {
        str(properties(r)["GroupName"]): logical_id
        for logical_id, r in _of_type(template, "AWS::EC2::SecurityGroup")
    }


def _keys(template: JsonObject, config: EnvironmentConfig) -> dict[KeyName, JsonObject]:
    """Cada clave por su nombre, a partir del alias que la apunta."""
    targets = {
        str(properties(alias)["AliasName"]): reference_target(properties(alias)["TargetKeyId"])
        for _, alias in _of_type(template, "AWS::KMS::Alias")
    }
    all_resources = template["Resources"]
    return {
        name: all_resources[str(targets[f"alias/{config.resource_name(name.value)}"])]
        for name in KeyName
    }


def _statements(key: JsonObject) -> list[JsonObject]:
    statements: list[JsonObject] = properties(key)["KeyPolicy"]["Statement"]
    return statements


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else [value]


def _principal_roles(statement: JsonObject, template: JsonObject) -> list[str]:
    """Nombres de los roles de la condición ``aws:PrincipalArn`` de una sentencia."""
    arns = statement.get("Condition", {}).get("ArnEquals", {}).get("aws:PrincipalArn", [])
    names = []
    for arn in _as_list(arns):
        text = render(arn, template)
        match = re.fullmatch(r"arn:aws:iam::<AccountId>:role/([A-Za-z0-9<>_+=,.@-]+)", text)
        assert match, f"ARN de rol inesperado: {text}"
        names.append(match.group(1))
    return names


def _grants(key: JsonObject, template: JsonObject) -> dict[str, set[str]]:
    """Rol → acciones que le concede la política de la clave."""
    granted: dict[str, set[str]] = {}
    for statement in _statements(key):
        for role in _principal_roles(statement, template):
            granted.setdefault(role, set()).update(_as_list(statement["Action"]))
    return granted


@pytest.fixture(scope="module")
def pilot_foundation(pilot: Synthesized) -> JsonObject:
    return _foundation(pilot)


@pytest.fixture(scope="module")
def staging_foundation(staging: Synthesized) -> JsonObject:
    return _foundation(staging)


# --- Recursos de la pila ---------------------------------------------------------------


def test_foundation_has_its_expected_resources(deployment: Synthesized) -> None:
    template = _foundation(deployment)
    found = Counter(str(r["Type"]) for _, r in resources(template))
    assert found == expected_foundation_resources(deployment.config)


def test_nat_per_az_adds_the_second_translation() -> None:
    deployment = _synth(nat_per_az="true")
    template = _foundation(deployment)
    found = Counter(str(r["Type"]) for _, r in resources(template))
    assert found == expected_foundation_resources(deployment.config)
    assert found["AWS::EC2::NatGateway"] == 2


# --- Red (§3) --------------------------------------------------------------------------


def test_vpc_is_10_40_in_two_zones_with_dns(pilot_foundation: JsonObject) -> None:
    ((_, vpc),) = _of_type(pilot_foundation, "AWS::EC2::VPC")
    props = properties(vpc)
    assert props["CidrBlock"] == "10.40.0.0/16"
    assert props["EnableDnsHostnames"] is True
    assert props["EnableDnsSupport"] is True
    assert _tags(vpc)["Name"] == "vigia-vpc-pilot"


def test_staging_vpc_carries_its_deployment(staging_foundation: JsonObject) -> None:
    ((_, vpc),) = _of_type(staging_foundation, "AWS::EC2::VPC")
    assert _tags(vpc)["Name"] == "vigia-vpc-staging-7"


@pytest.mark.parametrize(
    ("group", "mask", "public"),
    [("public", 24, True), ("app", 22, False), ("data", 24, False)],
)
def test_subnets_follow_the_design_table(
    pilot_foundation: JsonObject, group: str, mask: int, public: bool
) -> None:
    subnets = _named(pilot_foundation, "AWS::EC2::Subnet")
    all_resources = pilot_foundation["Resources"]
    for letter in ("a", "b"):
        props = properties(all_resources[subnets[f"vigia-{group}-{letter}"]])
        assert props["AvailabilityZone"] == f"us-east-1{letter}"
        assert props["CidrBlock"].startswith("10.40.")
        assert props["CidrBlock"].endswith(f"/{mask}")
        assert props["MapPublicIpOnLaunch"] is public


def _routes_by_subnet(template: JsonObject) -> dict[str, list[JsonObject]]:
    """Nombre de subred → rutas de su tabla."""
    subnets = {v: k for k, v in _named(template, "AWS::EC2::Subnet").items()}
    table_of = {
        reference_target(properties(a)["SubnetId"]): reference_target(properties(a)["RouteTableId"])
        for _, a in _of_type(template, "AWS::EC2::SubnetRouteTableAssociation")
    }
    routes: dict[str, list[JsonObject]] = {name: [] for name in subnets.values()}
    for _, route in _of_type(template, "AWS::EC2::Route"):
        table = reference_target(properties(route)["RouteTableId"])
        for subnet_id, name in subnets.items():
            if table_of[subnet_id] == table:
                routes[name].append(properties(route))
    return routes


def _default_routes(routes: list[JsonObject]) -> list[JsonObject]:
    return [
        r
        for r in routes
        if r.get("DestinationCidrBlock") == "0.0.0.0/0"
        or r.get("DestinationIpv6CidrBlock") == "::/0"
    ]


def test_data_subnets_have_no_default_route(deployment: Synthesized) -> None:
    routes = _routes_by_subnet(_foundation(deployment))
    data = {name: r for name, r in routes.items() if name.startswith("vigia-data-")}
    assert len(data) == 2
    for name, subnet_routes in data.items():
        assert subnet_routes == [], name


def test_data_subnets_stay_isolated_with_nat_per_az() -> None:
    routes = _routes_by_subnet(_foundation(_synth(nat_per_az="true")))
    assert all(r == [] for name, r in routes.items() if name.startswith("vigia-data-"))


def _nat_names(template: JsonObject) -> dict[str, str]:
    return {v: k for k, v in _named(template, "AWS::EC2::NatGateway").items()}


def test_public_subnets_route_to_the_internet_gateway(pilot_foundation: JsonObject) -> None:
    for name, subnet_routes in _routes_by_subnet(pilot_foundation).items():
        if name.startswith("vigia-public-"):
            assert [reference_target(r.get("GatewayId")) for r in subnet_routes] == [
                next(k for k, _ in _of_type(pilot_foundation, "AWS::EC2::InternetGateway"))
            ], name


def test_one_translation_serves_both_application_subnets(pilot_foundation: JsonObject) -> None:
    """R13: una sola ``vigia-nat-a``, en ``us-east-1a``, para las dos zonas."""
    nats = _nat_names(pilot_foundation)
    assert sorted(nats.values()) == ["vigia-nat-a"]
    (nat_id,) = nats
    subnet = reference_target(properties(pilot_foundation["Resources"][nat_id])["SubnetId"])
    assert _tags(pilot_foundation["Resources"][subnet])["Name"] == "vigia-public-a"
    for name, subnet_routes in _routes_by_subnet(pilot_foundation).items():
        if name.startswith("vigia-app-"):
            assert [nats[str(reference_target(r["NatGatewayId"]))] for r in subnet_routes] == [
                "vigia-nat-a"
            ], name
            assert all("GatewayId" not in r for r in subnet_routes), name


def test_nat_per_az_routes_each_zone_through_its_own_translation() -> None:
    template = _foundation(_synth(nat_per_az="true"))
    nats = _nat_names(template)
    assert sorted(nats.values()) == ["vigia-nat-a", "vigia-nat-b"]
    for name, subnet_routes in _routes_by_subnet(template).items():
        if name.startswith("vigia-app-"):
            zone = name[-1]
            assert [nats[str(reference_target(r["NatGatewayId"]))] for r in subnet_routes] == [
                f"vigia-nat-{zone}"
            ], name


def test_flow_logs_capture_all_traffic_for_90_days(deployment: Synthesized) -> None:
    template = _foundation(deployment)
    config = deployment.config
    ((group_id, group),) = _of_type(template, "AWS::Logs::LogGroup")
    props = properties(group)
    assert props["LogGroupName"] == f"/vigia/{config.deployment}/vpc-flow"
    assert props["RetentionInDays"] == 90
    logs_key = _keys(template, config)[KeyName.LOGS]
    key_id = next(k for k, r in template["Resources"].items() if r is logs_key)
    assert reference_target(props["KmsKeyId"]) == key_id
    ((_, flow_log),) = _of_type(template, "AWS::EC2::FlowLog")
    flow = properties(flow_log)
    assert flow["TrafficType"] == "ALL"
    assert flow["ResourceType"] == "VPC"
    assert reference_target(flow["LogGroupName"]) == group_id
    expected_policy = "Delete" if config.ephemeral else "Retain"
    assert group["DeletionPolicy"] == expected_policy


def test_flow_logs_role_is_named_and_scoped(pilot_foundation: JsonObject) -> None:
    roles = {
        properties(r).get("RoleName"): r for _, r in _of_type(pilot_foundation, "AWS::IAM::Role")
    }
    trust = properties(roles["vigia-flow-logs"])["AssumeRolePolicyDocument"]["Statement"]
    assert [s["Principal"] for s in trust] == [{"Service": "vpc-flow-logs.amazonaws.com"}]
    assert trust[0]["Condition"] == {
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    }
    ((_, policy),) = _of_type(pilot_foundation, "AWS::IAM::Policy")
    (statement,) = properties(policy)["PolicyDocument"]["Statement"]
    assert set(statement["Action"]) == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
    }
    assert render(statement["Resource"], pilot_foundation).startswith("<GetAtt.VpcFlowLogs")


# --- Grupos de seguridad (§3) ----------------------------------------------------------


def test_no_ingress_from_the_internet(deployment: Synthesized) -> None:
    template = _foundation(deployment)
    assert check_public_ingress("vigia-foundation", template) == []
    for _, group in _of_type(template, "AWS::EC2::SecurityGroup"):
        assert properties(group).get("SecurityGroupIngress", []) == []
    for _, rule in _of_type(template, "AWS::EC2::SecurityGroupIngress"):
        props = properties(rule)
        assert "CidrIp" not in props and "CidrIpv6" not in props
        assert "SourceSecurityGroupId" in props


# Regla de un grupo: protocolo, puerto inicial, puerto final y origen o destino. Comparar el
# rango y el protocolo completos impide que 443-65535 o UDP/443 pasen por 443/TCP.
Rule = tuple[str, int, int, str]


def _rule(props: JsonObject, target: str) -> Rule:
    return (str(props["IpProtocol"]), int(props["FromPort"]), int(props["ToPort"]), target)


def _ingress(template: JsonObject) -> set[tuple[str, Rule]]:
    names = {v: k for k, v in _groups(template).items()}
    return {
        (
            names[str(reference_target(properties(r)["GroupId"]))],
            _rule(
                properties(r),
                names[str(reference_target(properties(r)["SourceSecurityGroupId"]))],
            ),
        )
        for _, r in _of_type(template, "AWS::EC2::SecurityGroupIngress")
    }


def test_ingress_follows_the_design_table(pilot_foundation: JsonObject) -> None:
    tasks = ("sg-api", "sg-worker", "sg-tasks")
    assert _ingress(pilot_foundation) == {
        *(("sg-db", ("tcp", 5432, 5432, source)) for source in tasks),
        *(("sg-endpoints", ("tcp", 443, 443, source)) for source in tasks),
    }


def _egress(template: JsonObject) -> dict[str, set[Rule]]:
    names = {v: k for k, v in _groups(template).items()}
    egress: dict[str, set[Rule]] = {name: set() for name in names.values()}
    for logical_id, group in _of_type(template, "AWS::EC2::SecurityGroup"):
        for rule in properties(group).get("SecurityGroupEgress", []):
            egress[names[logical_id]].add(_rule(rule, str(rule["CidrIp"])))
    for _, rule in _of_type(template, "AWS::EC2::SecurityGroupEgress"):
        props = properties(rule)
        egress[names[str(reference_target(props["GroupId"]))]].add(
            _rule(props, names[str(reference_target(props["DestinationSecurityGroupId"]))])
        )
    return egress


# Regla que CDK escribe cuando un grupo no tiene salida: no permite ningún tráfico.
_NO_EGRESS = {("icmp", 252, 86, "255.255.255.255/32")}


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"ToPort": 65535}, ("tcp", 443, 65535, "0.0.0.0/0")),
        ({"IpProtocol": "udp"}, ("udp", 443, 443, "0.0.0.0/0")),
    ],
    ids=["port-range", "udp"],
)
def test_egress_comparison_sees_protocol_and_port_range(
    pilot_foundation: JsonObject, changes: dict[str, object], rule: Rule
) -> None:
    """Seguimiento de VIG-27: ampliar el rango o cambiar el protocolo de la salida 443 hacia
    ``0.0.0.0/0`` deja de coincidir con la tabla de diseño."""
    template = json.loads(json.dumps(pilot_foundation))
    names = {v: k for k, v in _groups(template).items()}
    group_id = next(i for i, n in names.items() if n == "sg-api")
    rules = properties(template["Resources"][group_id])["SecurityGroupEgress"]
    public = next(r for r in rules if r["CidrIp"] == "0.0.0.0/0")
    public.update(changes)
    found = _egress(template)["sg-api"]
    assert rule in found
    assert ("tcp", 443, 443, "0.0.0.0/0") not in found


def test_egress_follows_the_design_table(pilot_foundation: JsonObject) -> None:
    task_egress = {
        ("tcp", 5432, 5432, "sg-db"),
        ("tcp", 443, 443, "sg-endpoints"),
        ("tcp", 443, 443, "0.0.0.0/0"),
    }
    assert _egress(pilot_foundation) == {
        "sg-api": task_egress,
        "sg-worker": task_egress,
        "sg-tasks": task_egress,
        "sg-db": _NO_EGRESS,
        "sg-endpoints": _NO_EGRESS,
    }


def test_load_balancer_groups_are_not_in_this_stack(pilot_foundation: JsonObject) -> None:
    """``sg-alb-app`` y ``sg-alb-nodes`` (entrada pública 443) son de ``vigia-edge``."""
    assert set(_groups(pilot_foundation)) == {name.value for name in SecurityGroupName}


def test_sg_worker_is_published_for_other_units(deployment: Synthesized) -> None:
    template = _foundation(deployment)
    ((_, parameter),) = _of_type(template, "AWS::SSM::Parameter")
    props = properties(parameter)
    assert props["Name"] == f"/vigia/{deployment.config.deployment}/sg-worker-id"
    assert props["Value"] == {"Fn::GetAtt": [_groups(template)["sg-worker"], "GroupId"]}


# --- Puntos privados (§3) --------------------------------------------------------------


def _endpoints(template: JsonObject) -> dict[str, JsonObject]:
    return {
        render(properties(r)["ServiceName"], template): properties(r)
        for _, r in _of_type(template, "AWS::EC2::VPCEndpoint")
    }


def test_s3_gateway_endpoint_serves_application_and_data(pilot_foundation: JsonObject) -> None:
    s3 = _endpoints(pilot_foundation)["com.amazonaws.<Region>.s3"]
    assert s3["VpcEndpointType"] == "Gateway"
    routes = _routes_by_subnet(pilot_foundation)
    assert set(routes)  # sanidad
    subnets = {v: k for k, v in _named(pilot_foundation, "AWS::EC2::Subnet").items()}
    tables = {
        subnets[str(reference_target(properties(a)["SubnetId"]))]: reference_target(
            properties(a)["RouteTableId"]
        )
        for _, a in _of_type(pilot_foundation, "AWS::EC2::SubnetRouteTableAssociation")
    }
    expected = {tables[f"vigia-{g}-{z}"] for g in ("app", "data") for z in ("a", "b")}
    assert {reference_target(t) for t in s3["RouteTableIds"]} == expected


def test_s3_endpoint_policy_is_limited_to_vigia_buckets(pilot_foundation: JsonObject) -> None:
    s3 = _endpoints(pilot_foundation)["com.amazonaws.<Region>.s3"]
    statements = s3["PolicyDocument"]["Statement"]
    vigia, ecr = statements
    assert [render(r, pilot_foundation) for r in vigia["Resource"]] == [
        "arn:aws:s3:::vigia-*",
        "arn:aws:s3:::vigia-*/*",
    ]
    assert vigia["Condition"] == {"StringEquals": {"s3:ResourceAccount": {"Ref": "AWS::AccountId"}}}
    assert not any("Delete" in action or "*" in action for action in vigia["Action"])
    assert ecr["Action"] == "s3:GetObject"
    assert ecr["Resource"] == f"arn:aws:s3:::{ECR_LAYER_BUCKET}/*"
    assert all(s["Effect"] == "Allow" for s in statements)


@pytest.mark.parametrize("service", ["secretsmanager", "kms", "logs"])
def test_interface_endpoints_in_both_zones_behind_sg_endpoints(
    pilot_foundation: JsonObject, service: str
) -> None:
    endpoint = _endpoints(pilot_foundation)[f"com.amazonaws.us-east-1.{service}"]
    assert endpoint["VpcEndpointType"] == "Interface"
    assert endpoint["PrivateDnsEnabled"] is True
    groups = [reference_target(g) for g in endpoint["SecurityGroupIds"]]
    assert groups == [_groups(pilot_foundation)["sg-endpoints"]]
    subnets = {v: k for k, v in _named(pilot_foundation, "AWS::EC2::Subnet").items()}
    assert sorted(subnets[str(reference_target(s))] for s in endpoint["SubnetIds"]) == [
        "vigia-app-a",
        "vigia-app-b",
    ]


# --- Claves KMS (§7.1) -----------------------------------------------------------------


def test_the_seven_keys_exist_with_their_aliases(deployment: Synthesized) -> None:
    template = _foundation(deployment)
    aliases = sorted(
        str(properties(a)["AliasName"]) for _, a in _of_type(template, "AWS::KMS::Alias")
    )
    suffix = deployment.config.name_suffix
    assert aliases == sorted(f"alias/vigia-{name.value}{suffix}" for name in KeyName)


@pytest.mark.parametrize(
    "name", [k for k in KeyName if k is not KeyName.NODE_CA], ids=lambda k: k.value
)
def test_symmetric_keys_rotate_every_year(deployment: Synthesized, name: KeyName) -> None:
    props = properties(_keys(_foundation(deployment), deployment.config)[name])
    assert props["KeySpec"] == "SYMMETRIC_DEFAULT"
    assert props["KeyUsage"] == "ENCRYPT_DECRYPT"
    assert props["EnableKeyRotation"] is True
    assert props["RotationPeriodInDays"] == 365
    assert props.get("MultiRegion") in (None, False)


def test_node_ca_is_a_p256_signing_key_without_rotation(deployment: Synthesized) -> None:
    props = properties(_keys(_foundation(deployment), deployment.config)[KeyName.NODE_CA])
    assert props["KeySpec"] == "ECC_NIST_P256"
    assert props["KeyUsage"] == "SIGN_VERIFY"
    assert not props.get("EnableKeyRotation")
    assert "RotationPeriodInDays" not in props


def test_key_policies_have_no_wildcards(deployment: Synthesized) -> None:
    """SECURITY-06 y el seguimiento de VIG-23: política explícita, sin ``kms:*`` ni
    principales o roles con comodín."""
    template = _foundation(deployment)
    violations = check_policy_wildcards("vigia-foundation", template)
    assert violations == [], describe(violations)
    for name, key in _keys(template, deployment.config).items():
        for statement in _statements(key):
            assert statement["Effect"] == "Allow"
            assert statement["Resource"] == "*"  # la propia clave
            assert statement.get("Sid"), name
            for action in _as_list(statement["Action"]):
                assert re.fullmatch(r"kms:[A-Za-z]+", action), (name, action)
            principal = statement["Principal"]
            assert principal in (
                {"AWS": {"Fn::Join": ["", ["arn:aws:iam::", {"Ref": "AWS::AccountId"}, ":root"]]}},
                {"Service": "logs.us-east-1.amazonaws.com"},
            ), (name, principal)
            # La cuenta como principal siempre va acotada por rol o por servicio.
            if "AWS" in principal:
                conditions = statement["Condition"]
                assert "ArnEquals" in conditions or (
                    conditions["StringEquals"].get("kms:ViaService")
                    and conditions["StringEquals"].get("kms:CallerAccount")
                ), (name, statement["Sid"])
            for role in _principal_roles(statement, template):
                assert "*" not in role and "?" not in role


def test_no_key_grants_data_use_to_the_account_without_a_service(
    pilot_foundation: JsonObject, pilot: Synthesized
) -> None:
    """Sin la sentencia por defecto de CDK: la cuenta nunca recibe uso de datos a secas."""
    for name, key in _keys(pilot_foundation, pilot.config).items():
        text = json.dumps(_statements(key))
        assert '"kms:*"' not in text, name


def test_key_policies_follow_the_design_table(
    pilot_foundation: JsonObject, pilot: Synthesized
) -> None:
    keys = _keys(pilot_foundation, pilot.config)
    cfn = "cdk-vigia-cfn-exec-role-<AccountId>-us-east-1"
    grants = {name: _grants(key, pilot_foundation) for name, key in keys.items()}
    admin_only = {"kms:DescribeKey", "kms:PutKeyPolicy", "kms:ScheduleKeyDeletion"}
    for name in KeyName:
        assert admin_only <= grants[name][cfn], name
        # CloudFormation solo usa datos de ``vigia-secrets``: por Secrets Manager y, para
        # ``vigia-node-trust``, la lectura de ``vigia-edge/ca/*`` por S3.
        data_use = {"kms:Decrypt", "kms:Sign", "kms:GenerateDataKey", "kms:Encrypt"}
        assert bool(grants[name][cfn] & data_use) is (name is KeyName.SECRETS), name
    creates_secrets = _statement(keys[KeyName.SECRETS], "CloudFormationCreatesSecrets")
    assert creates_secrets["Condition"]["StringEquals"] == {
        "kms:ViaService": "secretsmanager.us-east-1.amazonaws.com"
    }
    assert grants[KeyName.EVIDENCE].keys() - {cfn} == {
        "vigia-api-task",
        "vigia-worker-task",
        "vigia-restore",
    }
    assert grants[KeyName.EVIDENCE]["vigia-api-task"] == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert grants[KeyName.EVIDENCE]["vigia-restore"] == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert grants[KeyName.SECRETS].keys() - {cfn} == {
        "vigia-api-task",
        "vigia-worker-task",
        "vigia-admin-task",
        "vigia-migrate-task",
        "vigia-deploy",
    }
    assert grants[KeyName.SECRETS]["vigia-migrate-task"] == {"kms:Decrypt"}
    assert grants[KeyName.SECRETS]["vigia-admin-task"] == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert grants[KeyName.SECRETS]["vigia-deploy"] == {"kms:Decrypt"}
    assert grants[KeyName.ARCHIVE].keys() - {cfn} == {"vigia-worker-task", "vigia-restore"}
    assert grants[KeyName.ARCHIVE]["vigia-worker-task"] == {"kms:Encrypt", "kms:GenerateDataKey"}
    assert grants[KeyName.ARCHIVE]["vigia-restore"] == {"kms:Decrypt"}
    assert grants[KeyName.BACKUP].keys() - {cfn} == {"vigia-backup"}
    assert grants[KeyName.DB].keys() - {cfn} == {"vigia-backup"}
    assert grants[KeyName.LOGS].keys() == {cfn}
    assert grants[KeyName.NODE_CA].keys() - {cfn} == {"vigia-api-task", "vigia-worker-task"}
    assert grants[KeyName.NODE_CA]["vigia-api-task"] == {"kms:Sign", "kms:GetPublicKey"}
    assert grants[KeyName.NODE_CA]["vigia-worker-task"] == {"kms:Sign"}


def test_admin_statements_grant_no_data_use(
    pilot_foundation: JsonObject, pilot: Synthesized
) -> None:
    for name, key in _keys(pilot_foundation, pilot.config).items():
        (admin,) = [s for s in _statements(key) if s["Sid"] == "KeyAdministration"]
        actions = set(_as_list(admin["Action"]))
        assert not actions & {
            "kms:Encrypt",
            "kms:Decrypt",
            "kms:GenerateDataKey",
            "kms:Sign",
            "kms:ReEncryptFrom",
            "kms:CreateGrant",
        }, name
        rotation = {"kms:EnableKeyRotation", "kms:DisableKeyRotation"}
        assert (rotation <= actions) is (name is not KeyName.NODE_CA), name


def _statement(key: JsonObject, sid: str) -> JsonObject:
    (found,) = [s for s in _statements(key) if s["Sid"] == sid]
    return found


def test_s3_backed_keys_are_used_only_through_s3(
    pilot_foundation: JsonObject, pilot: Synthesized
) -> None:
    keys = _keys(pilot_foundation, pilot.config)
    via_s3 = {"kms:ViaService": "s3.us-east-1.amazonaws.com"}
    for name, sid in [
        (KeyName.EVIDENCE, "ApiAndWorkerThroughS3"),
        (KeyName.EVIDENCE, "RestoreDrillThroughS3"),
        (KeyName.ARCHIVE, "WorkerWritesThroughS3"),
        (KeyName.ARCHIVE, "RestoreReads"),
        (KeyName.SECRETS, "DeployReadsEdgeCa"),
    ]:
        assert _statement(keys[name], sid)["Condition"]["StringEquals"] == via_s3, (name, sid)


@pytest.mark.parametrize(
    ("context", "role", "bucket"),
    [
        ({}, "vigia-deploy", "vigia-edge-<AccountId>-us-east-1"),
        (
            {"environment": "staging-7"},
            "vigia-deploy",
            "vigia-edge-staging-7-<AccountId>-us-east-1",
        ),
        ({"instance": "acme"}, "vigia-deploy-acme", "vigia-edge-acme-<AccountId>-us-east-1"),
    ],
    ids=["pilot", "staging-7", "dedicated-acme"],
)
def test_deploy_decrypts_only_the_edge_ca_package(
    context: dict[str, str], role: str, bucket: str
) -> None:
    """Nota U02-H-01 (2): ``Decrypt`` acotado a la lectura de ``vigia-edge/ca/*`` por S3."""
    deployment = _synth(**context)
    template = _foundation(deployment)
    key = _keys(template, deployment.config)[KeyName.SECRETS]
    statement = _statement(key, "DeployReadsEdgeCa")
    assert statement["Action"] == "kms:Decrypt"
    assert _principal_roles(statement, template) == [role]
    condition = statement["Condition"]["StringLike"]["kms:EncryptionContext:aws:s3:arn"]
    assert render(condition, template) == f"arn:aws:s3:::{bucket}/ca/*"
    for other in _statements(key):
        if other is not statement:
            assert role not in _principal_roles(other, template)


def test_logs_key_is_limited_to_the_deployment_log_groups(deployment: Synthesized) -> None:
    template = _foundation(deployment)
    key = _keys(template, deployment.config)[KeyName.LOGS]
    statement = _statement(key, "LogsForVigiaLogGroups")
    arns = statement["Condition"]["ArnLike"]["kms:EncryptionContext:aws:logs:arn"]
    prefix = "arn:aws:logs:us-east-1:<AccountId>:log-group:"
    deployment_name = deployment.config.deployment
    # Los grupos del despliegue, el de los registros de PostgreSQL de su base (vigia-data) y,
    # donde hay cortafuegos, el de vigia-app-waf (vigia-edge, §9.2).
    waf = (
        [f"{prefix}aws-waf-logs-{deployment.config.resource_name('app')}"]
        if deployment.config.waf_enabled
        else []
    )
    assert [render(arn, template) for arn in arns] == [
        f"{prefix}/vigia/{deployment_name}/*",
        f"{prefix}/aws/rds/instance/vigia-{deployment_name}-db/*",
        *waf,
    ]


def test_waf_log_group_name_carries_the_service_prefix() -> None:
    assert waf_log_group_name(_synth().config) == "aws-waf-logs-vigia-app"
    assert waf_log_group_name(_synth(instance="acme").config) == "aws-waf-logs-vigia-app-acme"


def test_cloudformation_decrypts_only_the_edge_ca_package_through_s3(
    deployment: Synthesized,
) -> None:
    """``vigia-edge`` crea ``vigia-node-trust`` leyendo ``ca/root.pem`` con el rol de ejecución
    de CloudFormation: solo ``Decrypt``, solo por S3 y solo sobre ``vigia-edge/ca/*``."""
    template = _foundation(deployment)
    key = _keys(template, deployment.config)[KeyName.SECRETS]
    statement = _statement(key, "CloudFormationReadsEdgeCa")
    assert statement["Action"] == "kms:Decrypt"
    assert _principal_roles(statement, template) == [
        "cdk-vigia-cfn-exec-role-<AccountId>-us-east-1"
    ]
    assert statement["Condition"]["StringEquals"] == {
        "kms:ViaService": "s3.us-east-1.amazonaws.com"
    }
    condition = statement["Condition"]["StringLike"]["kms:EncryptionContext:aws:s3:arn"]
    bucket = deployment.config.bucket_name("edge", "<AccountId>")
    assert render(condition, template) == f"arn:aws:s3:::{bucket}/ca/*"


def test_secrets_key_serves_only_the_database_secrets_through_secrets_manager(
    deployment: Synthesized,
) -> None:
    """Rotación de ``db/app`` y ``db/migrate`` y secreto maestro de RDS (§7.2): solo por Secrets
    Manager, solo en la cuenta y solo para los secretos de la base del despliegue."""
    template = _foundation(deployment)
    key = _keys(template, deployment.config)[KeyName.SECRETS]
    statement = _statement(key, "DatabaseSecretsThroughSecretsManager")
    assert set(statement["Action"]) == {"kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"}
    conditions = statement["Condition"]
    assert conditions["StringEquals"] == {
        "kms:ViaService": "secretsmanager.us-east-1.amazonaws.com",
        "kms:CallerAccount": {"Ref": "AWS::AccountId"},
    }
    secrets = conditions["StringLike"]["kms:EncryptionContext:SecretARN"]
    prefix = "arn:aws:secretsmanager:us-east-1:<AccountId>:secret:"
    assert [render(arn, template) for arn in secrets] == [
        f"{prefix}vigia/{deployment.config.deployment}/db/*",
        f"{prefix}rds!db-*",
    ]


def test_node_ca_signing_is_limited_to_ecdsa_sha_256(
    pilot_foundation: JsonObject, pilot: Synthesized
) -> None:
    key = _keys(pilot_foundation, pilot.config)[KeyName.NODE_CA]
    for statement in _statements(key):
        if "kms:Sign" in _as_list(statement["Action"]):
            assert statement["Condition"]["StringEquals"] == {
                "kms:SigningAlgorithm": "ECDSA_SHA_256"
            }


@pytest.mark.parametrize(("first_deploy", "ca_rotation"), FIRST_DEPLOY_FLAGS)
@pytest.mark.parametrize("environment", ["pilot", "staging-7"])
def test_admin_task_signs_with_node_ca_only_during_bootstrap(
    environment: str, first_deploy: bool, ca_rotation: bool
) -> None:
    """Nota U02-H-01 (1): ``vigia-admin-task`` en ``vigia-node-ca`` solo con ``first_deploy`` o
    ``ca_rotation``; con los dos en ``false`` la política no lo menciona."""
    deployment = _synth(environment=environment, **_flags(first_deploy, ca_rotation))
    template = _foundation(deployment)
    key = _keys(template, deployment.config)[KeyName.NODE_CA]
    admin_role = deployment.config.resource_name("admin-task")
    mentioned = admin_role in json.dumps(_statements(key))
    assert mentioned is (first_deploy or ca_rotation)
    grants = _grants(key, template)
    if first_deploy or ca_rotation:
        assert grants[admin_role] == {"kms:Sign", "kms:GetPublicKey"}
    else:
        assert "admin-task" not in json.dumps(_statements(key))
    # Los permisos del arranque no cambian las demás claves.
    baseline = _synth(environment=environment, **_flags(False, False))
    others = _keys(_foundation(baseline), baseline.config)
    for name, other in _keys(template, deployment.config).items():
        if name is not KeyName.NODE_CA:
            assert _statements(other) == _statements(others[name]), name


def test_keys_are_retained_in_permanent_deployments(permanent_deployment: Synthesized) -> None:
    config = permanent_deployment.config
    for name, key in _keys(_foundation(permanent_deployment), config).items():
        assert key["DeletionPolicy"] == "Retain", name
        assert key["UpdateReplacePolicy"] == "Retain", name


def test_staging_keys_are_destroyed_with_a_seven_day_window(
    staging_foundation: JsonObject, staging: Synthesized
) -> None:
    """D-8: ``vigia-node-ca`` es la de la ejecución y todo el entorno se destruye."""
    for name, key in _keys(staging_foundation, staging.config).items():
        assert key["DeletionPolicy"] == "Delete", name
        assert properties(key)["PendingWindowInDays"] == 7, name


def test_staging_key_policies_name_the_staging_roles(
    staging_foundation: JsonObject, staging: Synthesized
) -> None:
    keys = _keys(staging_foundation, staging.config)
    roles = {
        role
        for key in keys.values()
        for statement in _statements(key)
        for role in _principal_roles(statement, staging_foundation)
    }
    assert roles == {
        "cdk-vigia-cfn-exec-role-<AccountId>-us-east-1",
        "vigia-api-task-staging-7",
        "vigia-worker-task-staging-7",
        "vigia-admin-task-staging-7",
        "vigia-migrate-task-staging-7",
        "vigia-backup-staging-7",
        "vigia-restore-staging-7",
        # Un solo rol de despliegue para los entornos ``pilot`` y ``staging`` de GitHub (§8).
        "vigia-deploy",
    }


# --- Alertas y presupuestos (§2.2) -----------------------------------------------------


def test_alerts_topic_with_owner_email_from_ssm(deployment: Synthesized) -> None:
    template = _foundation(deployment)
    ((topic_id, topic),) = _of_type(template, "AWS::SNS::Topic")
    assert properties(topic)["TopicName"] == deployment.config.resource_name("alerts")
    ((_, subscription),) = _of_type(template, "AWS::SNS::Subscription")
    props = properties(subscription)
    assert props["Protocol"] == "email"
    assert reference_target(props["TopicArn"]) == topic_id
    parameter = reference_target(props["Endpoint"])
    assert template["Parameters"][parameter]["Default"] == ALERTS_EMAIL_PARAMETER
    assert template["Parameters"][parameter]["Type"] == "AWS::SSM::Parameter::Value<String>"


def test_no_email_address_in_any_template(deployment: Synthesized) -> None:
    for name, template in deployment.templates.items():
        assert not re.search(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", json.dumps(template)
        ), name


def _budgets(template: JsonObject) -> dict[str, JsonObject]:
    return {
        str(properties(b)["Budget"]["BudgetName"]): properties(b)
        for _, b in _of_type(template, "AWS::Budgets::Budget")
    }


def test_pilot_has_the_two_budgets(pilot_foundation: JsonObject) -> None:
    found = _budgets(pilot_foundation)
    assert set(found) == {"vigia-monthly", "vigia-staging"}
    ((topic_id, _),) = _of_type(pilot_foundation, "AWS::SNS::Topic")

    def summary(budget: JsonObject) -> tuple[Any, ...]:
        data = budget["Budget"]
        thresholds = []
        for notification in budget["NotificationsWithSubscribers"]:
            n = notification["Notification"]
            assert (n["NotificationType"], n["ThresholdType"]) == ("ACTUAL", "PERCENTAGE")
            (subscriber,) = notification["Subscribers"]
            assert subscriber["SubscriptionType"] == "SNS"
            assert reference_target(subscriber["Address"]) == topic_id
            thresholds.append(n["Threshold"])
        return (
            data["BudgetType"],
            data["TimeUnit"],
            data["BudgetLimit"],
            sorted(thresholds),
        )

    assert summary(found["vigia-monthly"]) == (
        "COST",
        "MONTHLY",
        {"Amount": 400, "Unit": "USD"},
        [80, 100],
    )
    assert summary(found["vigia-staging"]) == (
        "COST",
        "MONTHLY",
        {"Amount": 40, "Unit": "USD"},
        [100],
    )
    project = {"Tags": {"Key": "project", "Values": ["vigia"], "MatchOptions": ["EQUALS"]}}
    assert found["vigia-monthly"]["Budget"]["FilterExpression"] == project
    assert found["vigia-staging"]["Budget"]["FilterExpression"] == {
        "And": [
            project,
            {
                "Not": {
                    "Tags": {"Key": "environment", "Values": ["pilot"], "MatchOptions": ["EQUALS"]}
                }
            },
        ]
    }


def _topic_statements(template: JsonObject) -> dict[str, JsonObject]:
    ((_, policy),) = _of_type(template, "AWS::SNS::TopicPolicy")
    return {s["Sid"]: s for s in properties(policy)["PolicyDocument"]["Statement"]}


def test_budgets_may_publish_to_the_alerts_topic(pilot_foundation: JsonObject) -> None:
    statement = _topic_statements(pilot_foundation)["BudgetsPublish"]
    assert statement["Principal"] == {"Service": "budgets.amazonaws.com"}
    assert statement["Action"] == "sns:Publish"
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    }


def test_rds_events_may_publish_to_the_alerts_topic(deployment: Synthesized) -> None:
    """Suscripción de eventos de la base (§6.1): con política propia en el tema, RDS necesita
    su permiso; solo ``sns:Publish`` y solo desde la cuenta."""
    template = _foundation(deployment)
    statements = _topic_statements(template)
    budgets = {"BudgetsPublish"} if budgets_enabled(deployment.config) else set()
    assert (
        set(statements)
        == {"RdsEventsPublish", "CloudWatchAlarmsPublish", "BackupJobFailedRulePublish"} | budgets
    )
    statement = statements["RdsEventsPublish"]
    assert statement["Principal"] == {"Service": "events.rds.amazonaws.com"}
    assert statement["Action"] == "sns:Publish"
    ((topic_id, _),) = _of_type(template, "AWS::SNS::Topic")
    assert reference_target(statement["Resource"]) == topic_id
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    }


def test_only_the_backup_rule_of_the_deployment_may_publish_from_events(
    deployment: Synthesized,
) -> None:
    """Regla ``backup-job-failed`` de ``vigia-observability`` (§9.4): EventBridge publica solo
    desde esa regla del despliegue, no desde cualquier regla de la cuenta."""
    template = _foundation(deployment)
    statement = _topic_statements(template)["BackupJobFailedRulePublish"]
    assert statement["Principal"] == {"Service": "events.amazonaws.com"}
    assert statement["Action"] == "sns:Publish"
    ((topic_id, _),) = _of_type(template, "AWS::SNS::Topic")
    assert reference_target(statement["Resource"]) == topic_id
    (source,) = statement["Condition"]["ArnEquals"].values()
    rule = deployment.config.resource_name("backup-job-failed")
    assert render(source, template) == f"arn:aws:events:us-east-1:<AccountId>:rule/{rule}"
    assert set(statement["Condition"]) == {"ArnEquals"}


def test_cloudwatch_alarms_may_publish_to_the_alerts_topic(deployment: Synthesized) -> None:
    """Aviso de bloqueos por tasa de ``vigia-edge`` (nº 11) y alarmas de §9.4: con política
    propia en el tema, CloudWatch necesita su permiso; solo ``sns:Publish`` desde la cuenta."""
    template = _foundation(deployment)
    statement = _topic_statements(template)["CloudWatchAlarmsPublish"]
    assert statement["Principal"] == {"Service": "cloudwatch.amazonaws.com"}
    assert statement["Action"] == "sns:Publish"
    ((topic_id, _),) = _of_type(template, "AWS::SNS::Topic")
    assert reference_target(statement["Resource"]) == topic_id
    assert statement["Condition"] == {
        "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
    }


@pytest.mark.parametrize(("first_deploy", "ca_rotation"), FIRST_DEPLOY_FLAGS)
def test_staging_synthesis_has_no_budgets(first_deploy: bool, ca_rotation: bool) -> None:
    """Los presupuestos son de la cuenta: ``staging-7`` no los contiene en ninguna pila."""
    deployment = _synth(environment="staging-7", **_flags(first_deploy, ca_rotation))
    for name, template in deployment.templates.items():
        assert list(_of_type(template, "AWS::Budgets::Budget")) == [], name
        assert "BudgetsPublish" not in json.dumps(template), name


def test_dedicated_instance_budgets_carry_its_name() -> None:
    template = _foundation(_synth(instance="acme"))
    assert set(_budgets(template)) == {"vigia-monthly-acme", "vigia-staging-acme"}


def test_budgets_are_only_for_pilot() -> None:
    assert budgets_enabled(_synth().config)
    assert budgets_enabled(_synth(instance="acme").config)
    assert not budgets_enabled(_synth(environment="staging-7").config)
    assert not budgets_enabled(_synth(environment="staging-7", instance="acme").config)


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({}, "vigia-deploy"),
        ({"environment": "staging-7"}, "vigia-deploy"),
        ({"instance": "acme"}, "vigia-deploy-acme"),
        ({"instance": "acme", "environment": "staging-7"}, "vigia-deploy-acme"),
    ],
)
def test_deploy_role_name(context: dict[str, str], expected: str) -> None:
    assert deploy_role_name(_synth(**context).config) == expected
