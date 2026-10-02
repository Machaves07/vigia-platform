"""SECURITY-01, 02, 06 y 07 sobre la síntesis (infrastructure-design §2.3 y nota U02-H-06).

Primero, cada regla sobre cada pila de cada despliegue (``pilot``, ``pilot`` con
``first_deploy`` o ``ca_rotation``, ``staging-7`` e instancia dedicada). Después, recursos de
prueba que demuestran que cada regla falla nombrando el recurso y que las excepciones
declaradas, y solo ellas, pasan.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_backup as backup
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager

from tests.conftest import Synthesized, probe
from tests.template_rules import (
    SECURITY_RULES,
    Violation,
    check_load_balancer_access_logs,
    check_policy_wildcards,
    check_public_ingress,
    check_storage_encryption,
    describe,
)

Rule = Callable[[str, dict[str, Any]], list[Violation]]


def _run(rule: Rule, build: Callable[[Stack], object]) -> list[Violation]:
    return rule("vigia-probe", probe(build))


def _only(violations: list[Violation], logical_prefix: str) -> list[Violation]:
    return [v for v in violations if v.logical_id.startswith(logical_prefix)]


# --- Todas las pilas de todos los despliegues -----------------------------------------


@pytest.mark.parametrize("rule", SECURITY_RULES, ids=lambda rule: rule.__name__)
def test_every_stack_passes_the_security_rules(deployment: Synthesized, rule: Rule) -> None:
    violations = [v for name, t in deployment.templates.items() for v in rule(name, t)]
    assert not violations, describe(violations)


# --- SECURITY-01: cifrado con clave del cliente ---------------------------------------


def test_unencrypted_bucket_fails_naming_the_resource() -> None:
    violations = _run(
        check_storage_encryption,
        lambda stack: s3.Bucket(stack, "Unencrypted", bucket_name="vigia-probe-unencrypted"),
    )
    assert len(violations) == 1
    message = str(violations[0])
    assert "vigia-probe/Unencrypted" in message
    assert "vigia-probe-unencrypted" in message
    assert "SECURITY-01" in message


@pytest.mark.parametrize(
    "encryption",
    [s3.BucketEncryption.S3_MANAGED, s3.BucketEncryption.KMS_MANAGED],
    ids=["sse-s3", "kms-aws-managed"],
)
def test_bucket_without_customer_key_fails(encryption: s3.BucketEncryption) -> None:
    violations = _run(
        check_storage_encryption,
        lambda stack: s3.Bucket(
            stack, "Evidence", bucket_name="vigia-evidence-probe", encryption=encryption
        ),
    )
    assert [v.physical_name for v in violations] == ["vigia-evidence-probe"]


def test_bucket_with_customer_key_passes() -> None:
    def build(stack: Stack) -> None:
        key = kms.Key(stack, "EvidenceKey")
        s3.Bucket(stack, "Evidence", encryption_key=key)

    assert _run(check_storage_encryption, build) == []


@pytest.mark.parametrize(
    "name",
    [
        "vigia-logs-123456789012-us-east-1",
        "vigia-logs-staging-7-123456789012-us-east-1",
    ],
)
def test_declared_logs_bucket_with_sse_s3_passes(name: str) -> None:
    assert (
        _run(
            check_storage_encryption,
            lambda stack: s3.Bucket(
                stack, "Logs", bucket_name=name, encryption=s3.BucketEncryption.S3_MANAGED
            ),
        )
        == []
    )


def test_declared_logs_bucket_with_account_token_passes() -> None:
    """Forma real: la cuenta sin resolver (``vigia-logs-${AWS::AccountId}-us-east-1``)."""

    def build(stack: Stack) -> None:
        s3.Bucket(
            stack,
            "Logs",
            bucket_name=f"vigia-logs-{stack.account}-{stack.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

    assert _run(check_storage_encryption, build) == []


@pytest.mark.parametrize(
    ("name", "encryption"),
    [
        ("vigia-logs-123456789012-us-east-1", None),  # excepción: SSE-S3 explícito
        ("vigia-logs-123456789012-us-east-1", s3.BucketEncryption.KMS_MANAGED),
        ("vigia-logsx-123456789012-us-east-1", s3.BucketEncryption.S3_MANAGED),
        ("vigia-logs-123456789012-eu-west-1", s3.BucketEncryption.S3_MANAGED),
        ("vigia-evidence-123456789012-us-east-1", s3.BucketEncryption.S3_MANAGED),
    ],
    ids=["logs-default", "logs-aws-kms", "logsx", "logs-other-region", "evidence"],
)
def test_logs_exception_is_by_exact_name_and_sse_s3(
    name: str, encryption: s3.BucketEncryption | None
) -> None:
    violations = _run(
        check_storage_encryption,
        lambda stack: s3.Bucket(stack, "Bucket", bucket_name=name, encryption=encryption),
    )
    assert [v.physical_name for v in violations] == [name]


def test_declared_datasets_bucket_with_sse_s3_passes() -> None:
    """El conjunto sellado de U-01 conserva SSE-S3 al trasladarse (TASK-150), con la cuenta
    escrita o sin resolver (``vigia-datasets-${AWS::AccountId}-us-east-1``)."""

    def build(stack: Stack) -> None:
        for construct_id, name in (
            ("Literal", "vigia-datasets-123456789012-us-east-1"),
            ("Token", f"vigia-datasets-{stack.account}-{stack.region}"),
        ):
            s3.Bucket(
                stack, construct_id, bucket_name=name, encryption=s3.BucketEncryption.S3_MANAGED
            )

    assert _run(check_storage_encryption, build) == []


@pytest.mark.parametrize(
    ("name", "encryption"),
    [
        ("vigia-datasets-123456789012-us-east-1", None),
        ("vigia-datasets-123456789012-us-east-1", s3.BucketEncryption.KMS_MANAGED),
        # Solo en pilot compartido: ningún despliegue con sufijo tiene vigia-datasets (D-8).
        ("vigia-datasets-staging-7-123456789012-us-east-1", s3.BucketEncryption.S3_MANAGED),
        ("vigia-datasets-acme-123456789012-us-east-1", s3.BucketEncryption.S3_MANAGED),
        ("vigia-datasetsx-123456789012-us-east-1", s3.BucketEncryption.S3_MANAGED),
        ("vigia-datasets-123456789012-eu-west-1", s3.BucketEncryption.S3_MANAGED),
    ],
    ids=["default", "aws-kms", "staging", "dedicated", "datasetsx", "other-region"],
)
def test_datasets_exception_is_by_exact_name_and_sse_s3(
    name: str, encryption: s3.BucketEncryption | None
) -> None:
    violations = _run(
        check_storage_encryption,
        lambda stack: s3.Bucket(stack, "Bucket", bucket_name=name, encryption=encryption),
    )
    assert [v.physical_name for v in violations] == [name]


def test_declared_image_registry_with_service_aes_passes() -> None:
    def build(stack: Stack) -> None:
        ecr.Repository(stack, "Registry", repository_name="vigia-platform")
        ecr.Repository(
            stack,
            "RegistryExplicit",
            repository_name="vigia-platform",
            encryption=ecr.RepositoryEncryption.AES_256,
        )

    assert _run(check_storage_encryption, build) == []


@pytest.mark.parametrize("name", ["vigia-platform-2", "vigia-edge", "vigia-platfor"])
def test_other_image_registries_need_a_customer_key(name: str) -> None:
    violations = _run(
        check_storage_encryption,
        lambda stack: ecr.Repository(stack, "Registry", repository_name=name),
    )
    assert [v.physical_name for v in violations] == [name]


def test_other_storage_types_need_a_customer_key() -> None:
    def build(stack: Stack) -> None:
        key = kms.Key(stack, "Key")
        logs.LogGroup(stack, "PlainLogs", log_group_name="/vigia/probe/plain")
        logs.LogGroup(stack, "KeyedLogs", log_group_name="/vigia/probe/keyed", encryption_key=key)
        # S106: son nombres de secretos, no sus valores.
        secretsmanager.Secret(stack, "PlainSecret", secret_name="vigia/probe/plain")  # noqa: S106
        secretsmanager.Secret(
            stack,
            "KeyedSecret",
            secret_name="vigia/probe/keyed",  # noqa: S106
            encryption_key=key,
        )
        backup.BackupVault(stack, "PlainVault", backup_vault_name="vigia-probe-plain")
        backup.BackupVault(
            stack, "KeyedVault", backup_vault_name="vigia-probe-keyed", encryption_key=key
        )
        common: dict[str, Any] = {"engine": "postgres", "db_instance_class": "db.t4g.small"}
        rds.CfnDBInstance(stack, "PlainDb", storage_encrypted=False, **common)
        rds.CfnDBInstance(stack, "AwsKeyDb", storage_encrypted=True, **common)
        rds.CfnDBInstance(
            stack, "KeyedDb", storage_encrypted=True, kms_key_id=key.key_arn, **common
        )

    violations = _run(check_storage_encryption, build)
    failing = sorted({v.logical_id.rstrip("0123456789ABCDEF") for v in violations})
    assert failing == ["AwsKeyDb", "PlainDb", "PlainLogs", "PlainSecret", "PlainVault"], describe(
        violations
    )


# --- SECURITY-02: registro de acceso de los balanceadores -----------------------------


def _alb(stack: Stack, *, access_logs: bool) -> elbv2.ApplicationLoadBalancer:
    vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=0)
    alb = elbv2.ApplicationLoadBalancer(
        stack, "Alb", vpc=vpc, internet_facing=True, load_balancer_name="vigia-alb-probe"
    )
    if access_logs:
        logs_bucket = s3.Bucket.from_bucket_name(
            stack, "LogsBucket", f"vigia-logs-{stack.account}-{stack.region}"
        )
        alb.log_access_logs(logs_bucket, prefix="alb/app")
    return alb


def test_load_balancer_without_access_logs_fails_naming_the_resource() -> None:
    violations = _run(check_load_balancer_access_logs, lambda s: _alb(s, access_logs=False))
    assert [(v.physical_name, v.rule) for v in violations] == [("vigia-alb-probe", "SECURITY-02")]


def test_load_balancer_with_access_logs_passes() -> None:
    assert _run(check_load_balancer_access_logs, lambda s: _alb(s, access_logs=True)) == []


def test_load_balancer_access_log_delivery_policy_is_a_declared_wildcard() -> None:
    """La política de entrega de registros a ``vigia-logs/alb/app/*`` no es un comodín nuevo."""
    template = probe(lambda s: _alb(s, access_logs=True))
    assert check_policy_wildcards("vigia-probe", template) == []


# --- SECURITY-06: comodines en políticas ----------------------------------------------


def _role_with(*statements: iam.PolicyStatement) -> Callable[[Stack], None]:
    def build(stack: Stack) -> None:
        role = iam.Role(
            stack,
            "TaskRole",
            role_name="vigia-probe-task",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        for statement in statements:
            role.add_to_policy(statement)

    return build


def _evidence(stack: Stack, suffix: str) -> str:
    return f"arn:aws:s3:::vigia-evidence-{stack.account}-{stack.region}{suffix}"


@pytest.mark.parametrize(
    ("actions", "resource"),
    [
        (["s3:*"], "arn:aws:s3:::vigia-probe"),
        (["s3:Get*"], "arn:aws:s3:::vigia-probe"),
        (["s3:GetObject"], "*"),
        (["kms:Decrypt"], "*"),
        (["xray:PutTraceSegments", "s3:GetObject"], "*"),
        (["s3:GetObject"], "arn:aws:s3:::*"),
        (["s3:GetObject"], "arn:aws:s3:::vigia-evidence-123456789012-us-east-1/*"),
        (["s3:PutObject"], "arn:aws:s3:::vigia-evidence-123456789012-us-east-1/org/*/export/*"),
        (["secretsmanager:GetSecretValue"], "arn:aws:secretsmanager:us-east-1:1:secret:*"),
        (["logs:PutLogEvents"], "arn:aws:logs:us-east-1:1:log-group:*"),
    ],
)
def test_unlisted_wildcards_fail_naming_the_policy(actions: list[str], resource: str) -> None:
    violations = _run(
        check_policy_wildcards,
        _role_with(iam.PolicyStatement(actions=actions, resources=[resource])),
    )
    assert violations, "se esperaba un comodín no listado"
    assert all(v.rule == "SECURITY-06" for v in violations)
    assert any("TaskRole" in v.logical_id for v in violations), describe(violations)


@pytest.mark.parametrize(
    "actions",
    [
        ["ecr:GetAuthorizationToken"],
        ["xray:PutTraceSegments", "xray:PutTelemetryRecords"],
    ],
)
def test_actions_without_resource_level_permissions_may_use_star(actions: list[str]) -> None:
    statement = iam.PolicyStatement(actions=actions, resources=["*"])
    assert _run(check_policy_wildcards, _role_with(statement)) == []


def test_put_metric_data_needs_the_namespace_condition() -> None:
    bare = iam.PolicyStatement(actions=["cloudwatch:PutMetricData"], resources=["*"])
    conditioned = iam.PolicyStatement(
        actions=["cloudwatch:PutMetricData"],
        resources=["*"],
        conditions={"StringEquals": {"cloudwatch:namespace": "Vigia/Platform"}},
    )
    other = iam.PolicyStatement(
        actions=["cloudwatch:PutMetricData"],
        resources=["*"],
        conditions={"StringEquals": {"cloudwatch:namespace": "Other"}},
    )
    assert len(_run(check_policy_wildcards, _role_with(bare))) == 1
    assert _run(check_policy_wildcards, _role_with(conditioned)) == []
    assert len(_run(check_policy_wildcards, _role_with(other))) == 1


@pytest.mark.parametrize(
    ("actions", "suffix"),
    [
        (["s3:GetObject"], "/org/*"),
        (["s3:PutObject"], "/org/*/plant/*/zone/*/node/*"),
        (["s3:PutObject"], "/org/*/plant/*/documents/*"),
        (["s3:PutObject"], "/org/*/closure/*"),
    ],
)
def test_declared_evidence_prefixes_pass(actions: list[str], suffix: str) -> None:
    def build(stack: Stack) -> None:
        _role_with(iam.PolicyStatement(actions=actions, resources=[_evidence(stack, suffix)]))(
            stack
        )

    assert _run(check_policy_wildcards, build) == []


def test_declared_resources_with_explicit_statements_pass() -> None:
    """Forma que §8 pide: acciones explícitas sobre depósitos importados por nombre."""

    def build(stack: Stack) -> None:
        role = iam.Role(
            stack, "TaskRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )
        archive = s3.Bucket.from_bucket_name(
            stack, "Archive", f"vigia-archive-{stack.account}-{stack.region}"
        )
        edge = s3.Bucket.from_bucket_name(
            stack, "Edge", f"vigia-edge-{stack.account}-{stack.region}"
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[archive.arn_for_objects("*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"],
                resources=[edge.arn_for_objects("ca/*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{stack.region}:{stack.account}:secret:"
                    "vigia/pilot/signing/*"
                ],
            )
        )
        # Estas dos concesiones de CDK ya usan acciones explícitas.
        logs.LogGroup.from_log_group_name(stack, "Api", "/vigia/pilot/api").grant_write(role)
        secretsmanager.Secret.from_secret_name_v2(stack, "Db", "vigia/pilot/db/app").grant_read(
            role
        )

    template = probe(build)
    assert check_policy_wildcards("vigia-probe", template) == []


def test_cdk_bucket_grants_expand_to_unlisted_action_wildcards() -> None:
    """``grant_put`` y ``grant_read`` de S3 conceden ``s3:Abort*``, ``s3:GetObject*`` o
    ``s3:List*``: las pilas de TASK-146 y TASK-148 deben escribir sentencias explícitas."""

    def build(stack: Stack) -> None:
        role = iam.Role(
            stack, "TaskRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )
        archive = s3.Bucket.from_bucket_name(
            stack, "Archive", f"vigia-archive-{stack.account}-{stack.region}"
        )
        archive.grant_put(role)
        archive.grant_read(role)

    details = describe(_run(check_policy_wildcards, build))
    assert "s3:Abort*" in details
    assert "s3:GetObject*" in details


def test_negated_elements_and_public_principals_fail() -> None:
    def build(stack: Stack) -> None:
        role = iam.Role(
            stack, "TaskRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com")
        )
        role.add_to_policy(
            iam.PolicyStatement(not_actions=["s3:DeleteObject"], resources=["arn:aws:s3:::x"])
        )
        bucket = s3.Bucket(stack, "Shared", encryption_key=kms.Key(stack, "Key"))
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                principals=[iam.AnyPrincipal()],
                resources=[bucket.arn_for_objects("public.txt")],
            )
        )

    violations = _run(check_policy_wildcards, build)
    details = describe(violations)
    assert "NotAction" in details
    assert "principal público" in details


@pytest.mark.parametrize(
    "principal",
    ["*", {"AWS": "*"}, {"AWS": ["arn:aws:iam::123456789012:root", "*"]}],
    ids=["star", "aws-star", "aws-list-with-star"],
)
def test_public_principal_forms_are_detected(principal: object) -> None:
    template = {
        "Resources": {
            "Policy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "Bucket": "vigia-probe",
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": principal,
                                "Action": "s3:GetObject",
                                "Resource": "arn:aws:s3:::vigia-probe/public.txt",
                            }
                        ]
                    },
                },
            }
        }
    }
    violations = check_policy_wildcards("vigia-probe", template)
    assert [v.detail for v in violations] == ["PolicyDocument.Statement[0]: principal público '*'"]


def test_deny_statements_may_use_wildcards() -> None:
    """A-34: la denegación de ``s3:PutObject`` con ``NotResource`` restringe, no concede."""

    def build(stack: Stack) -> None:
        _role_with(
            iam.PolicyStatement(
                effect=iam.Effect.DENY,
                actions=["s3:PutObject"],
                not_resources=[_evidence(stack, "/org/*/closure/*")],
            ),
            iam.PolicyStatement(effect=iam.Effect.DENY, actions=["*"], resources=["*"]),
        )(stack)

    assert _run(check_policy_wildcards, build) == []


def test_default_key_policy_is_flagged() -> None:
    """La política por defecto de una clave KMS concede ``kms:*``: §7.1 exige principales y
    acciones explícitos, así que TASK-145 debe escribir la suya."""
    violations = _run(check_policy_wildcards, lambda stack: kms.Key(stack, "Key"))
    assert ["kms:*" in v.detail for v in violations] == [True]


def test_auto_delete_wildcards_are_accepted_only_for_the_cdk_provider() -> None:
    def build(stack: Stack) -> None:
        bucket = s3.Bucket(
            stack,
            "Evidence",
            encryption_key=kms.Key(stack, "Key"),
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        impostor = iam.Role(stack, "Impostor", assumed_by=iam.AccountRootPrincipal())
        bucket.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["s3:DeleteObject*"],
                principals=[impostor],
                resources=[bucket.arn_for_objects("*")],
            )
        )

    violations = _only(_run(check_policy_wildcards, build), "Evidence")
    assert len(violations) == 2, describe(violations)  # acción y recurso del impostor
    assert all("Statement[1]" in v.detail for v in violations)


# --- SECURITY-07: entrada desde Internet ----------------------------------------------


def test_https_on_a_load_balancer_passes() -> None:
    def build(stack: Stack) -> None:
        alb = _alb(stack, access_logs=True)
        alb.connections.allow_from_any_ipv4(ec2.Port.tcp(443))

    assert _run(check_public_ingress, build) == []


@pytest.mark.parametrize(
    "port",
    [ec2.Port.tcp(80), ec2.Port.tcp(8000), ec2.Port.tcp_range(443, 444), ec2.Port.all_traffic()],
    ids=["80", "8000", "443-444", "all"],
)
def test_other_public_ports_on_a_load_balancer_fail(port: ec2.Port) -> None:
    def build(stack: Stack) -> None:
        _alb(stack, access_logs=True).connections.allow_from_any_ipv4(port)

    violations = _run(check_public_ingress, build)
    assert len(violations) == 1, describe(violations)
    assert violations[0].rule == "SECURITY-07"
    assert "0.0.0.0/0" in violations[0].detail


def test_public_https_outside_a_load_balancer_fails_naming_the_group() -> None:
    def build(stack: Stack) -> None:
        vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=0)
        group = ec2.SecurityGroup(stack, "SgApi", vpc=vpc, security_group_name="sg-api")
        group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443))
        group.add_ingress_rule(ec2.Peer.any_ipv6(), ec2.Port.tcp(22))

    violations = _run(check_public_ingress, build)
    assert sorted(v.detail.split(" (")[0] for v in violations) == [
        "entrada desde 0.0.0.0/0",
        "entrada desde ::/0",
    ]
    assert {v.physical_name for v in violations} == {"sg-api"}


def test_numeric_tcp_protocol_is_recognised_on_a_load_balancer() -> None:
    def build(stack: Stack) -> None:
        alb = _alb(stack, access_logs=True)
        group = alb.connections.security_groups[0]
        ec2.CfnSecurityGroupIngress(
            stack,
            "HttpsNumeric",
            group_id=group.security_group_id,
            ip_protocol="6",
            from_port=443,
            to_port=443,
            cidr_ip="0.0.0.0/0",
        )

    assert _run(check_public_ingress, build) == []


def test_standalone_ingress_resource_is_checked() -> None:
    def build(stack: Stack) -> None:
        vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=0)
        group = ec2.SecurityGroup(stack, "SgDb", vpc=vpc)
        ec2.CfnSecurityGroupIngress(
            stack,
            "OpenDb",
            group_id=group.security_group_id,
            ip_protocol="tcp",
            from_port=5432,
            to_port=5432,
            cidr_ip="0.0.0.0/0",
        )

    violations = _run(check_public_ingress, build)
    assert [v.logical_id for v in violations] == ["OpenDb"]


def test_private_ingress_passes() -> None:
    def build(stack: Stack) -> None:
        vpc = ec2.Vpc(stack, "Vpc", max_azs=2, nat_gateways=0)
        api = ec2.SecurityGroup(stack, "SgApi", vpc=vpc)
        db = ec2.SecurityGroup(stack, "SgDb", vpc=vpc)
        db.add_ingress_rule(api, ec2.Port.tcp(5432))
        db.add_ingress_rule(ec2.Peer.ipv4("10.40.0.0/16"), ec2.Port.tcp(5432))

    assert _run(check_public_ingress, build) == []


def test_rules_do_not_flag_an_unrelated_duration() -> None:
    """Sanidad: un recurso sin almacenamiento, balanceador, política ni red no produce hallazgos."""

    def build(stack: Stack) -> None:
        logs.LogGroup(
            stack,
            "Keyed",
            encryption_key=kms.Key(stack, "LogsKey", pending_window=Duration.days(7)),
        )

    violations = [v for rule in SECURITY_RULES for v in _run(rule, build)]
    assert [v.detail for v in violations if "kms:*" not in v.detail] == []
