"""Pila ``vigia-compute`` (TASK-148): registro, clúster, servicios, tareas puntuales y roles.

Infrastructure-design §5, §8 y §9.1 con sus notas de 2026-09-23 (U02-H-01 permisos del arranque,
U02-H-04 forma de ``PutObject``, U02-H-10 volumen efímero), las notas nº 16 y nº 20, la nota de
``ca_rotation`` (deployment-architecture §6.4), el rol ``vigia-restore`` (§6.1) y la adenda
(A-21, A-23, A-34). Cada prueba lee la plantilla sintetizada; las referencias a otras pilas
(``Fn::GetStackOutput``) se resuelven contra la plantilla de la pila que las publica.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App

from config import ContextError, EnvironmentConfig, NodesTlsMode
from stacks.compute import (
    ADMIN_RUNTIME,
    API_RUNTIME,
    API_TUNING,
    COLLECTOR_CONFIG,
    EVIDENCE_UPLOAD_PREFIXES,
    NO_IMAGE_DIGEST,
    RUNTIME_VARIABLES,
    WORKER_RUNTIME,
    deploy_role_owned,
    read_image_digest,
    registry_owned,
    render_collector_config,
)
from tests.conftest import Synthesized, synthesize
from tests.template_rules import (
    NO_RESOURCE_LEVEL_ACTIONS,
    RESOURCE_WILDCARDS,
    check_policy_wildcards,
    describe,
    properties,
    reference_target,
    render,
    resources,
)

JsonObject = Mapping[str, Any]
ACCOUNT = "<AccountId>"
SERVICE = "AWS::ECS::Service"
TASK_DEFINITION = "AWS::ECS::TaskDefinition"
ROLE = "AWS::IAM::Role"
POLICY = "AWS::IAM::ManagedPolicy"
SCALABLE_TARGET = "AWS::ApplicationAutoScaling::ScalableTarget"
SCALING_POLICY = "AWS::ApplicationAutoScaling::ScalingPolicy"
DIGEST = "sha256:" + "ab" * 32


def expected_compute_resources(config: EnvironmentConfig) -> Counter[str]:
    """Recursos de ``vigia-compute`` por despliegue (sin ``AWS::CDK::Metadata``)."""
    roles = 6  # cinco de tarea (§8) y vigia-restore
    policies = 6
    expected: Counter[str] = Counter(
        {
            "AWS::ECS::Cluster": 1,
            SERVICE: 2,
            TASK_DEFINITION: 4,
            "AWS::SSM::Parameter": 1,
        }
    )
    if registry_owned(config):
        expected += Counter({"AWS::ECR::Repository": 1})
    if deploy_role_owned(config):
        roles += 1
        policies += 1
    if config.ephemeral:
        policies += 1  # vigia-deploy-staging-<n> sobre el vigia-deploy importado
    if config.first_deploy:
        policies += 1  # vigia-migrate-task-bootstrap
    if config.elevated_bootstrap:
        policies += 1  # vigia-admin-task-bootstrap
    if not config.first_deploy:
        # CPU y peticiones de vigia-api; CPU y los dos pasos de la bandeja de vigia-worker.
        expected += Counter({SCALABLE_TARGET: 2, SCALING_POLICY: 5, "AWS::CloudWatch::Alarm": 2})
    expected += Counter({ROLE: roles, POLICY: policies})
    return expected


# --- Síntesis y lectura ---------------------------------------------------------------


@cache
def _synth(**context: str) -> Synthesized:
    return synthesize(None, **context)


def _compute(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("compute")]


def _of_type(template: JsonObject, kind: str) -> Iterator[tuple[str, JsonObject]]:
    for logical_id, resource in resources(template):
        if resource["Type"] == kind:
            yield logical_id, resource


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _resolve(value: Any, deployment: Synthesized) -> tuple[str, Any]:
    """Sigue un ``Fn::GetStackOutput`` hasta la pila que lo publica: ``(pila, valor)``."""
    stack = deployment.config.stack_name("compute")
    while isinstance(value, Mapping) and "Fn::GetStackOutput" in value:
        output = value["Fn::GetStackOutput"]
        stack = output["StackName"]
        value = deployment.templates[stack]["Outputs"][output["OutputName"]]["Value"]
    return stack, value


def _resolved_resource(value: Any, deployment: Synthesized) -> JsonObject:
    stack, reference = _resolve(value, deployment)
    target = reference_target(reference)
    assert target, reference
    resource: JsonObject = deployment.templates[stack]["Resources"][target]
    return resource


def _service(deployment: Synthesized, name: str) -> tuple[str, JsonObject]:
    (found,) = [
        (logical_id, r)
        for logical_id, r in _of_type(_compute(deployment), SERVICE)
        if properties(r)["ServiceName"] == name
    ]
    return found


def _task_definition(deployment: Synthesized, family: str) -> JsonObject:
    template = _compute(deployment)
    expected = deployment.config.resource_name(family)
    (found,) = [
        r for _, r in _of_type(template, TASK_DEFINITION) if properties(r)["Family"] == expected
    ]
    return found


def _container(task_definition: JsonObject, name: str) -> JsonObject:
    containers: list[JsonObject] = properties(task_definition)["ContainerDefinitions"]
    (found,) = [c for c in containers if c["Name"] == name]
    return found


def _environment(container: JsonObject) -> dict[str, Any]:
    return {e["Name"]: e["Value"] for e in container.get("Environment", [])}


def _role(deployment: Synthesized, name: str) -> tuple[str, JsonObject]:
    template = _compute(deployment)
    (found,) = [
        (logical_id, r)
        for logical_id, r in _of_type(template, ROLE)
        if render(properties(r)["RoleName"], template) == name
    ]
    return found


def _role_statements(deployment: Synthesized, name: str) -> list[JsonObject]:
    """Sentencias de todas las políticas gestionadas del rol (§8: ninguna en línea)."""
    template = _compute(deployment)
    _, role = _role(deployment, name)
    assert "Policies" not in properties(role), name
    statements: list[JsonObject] = []
    for arn in properties(role).get("ManagedPolicyArns", []):
        policy = template["Resources"][reference_target(arn)]
        statements += _as_list(properties(policy)["PolicyDocument"]["Statement"])
    return statements


def _actions(statement: JsonObject) -> set[str]:
    return set(_as_list(statement.get("Action")))


def _rendered(values: Any, template: JsonObject) -> list[str]:
    return sorted(render(v, template) for v in _as_list(values))


def _granting(statements: list[JsonObject], action: str) -> list[JsonObject]:
    return [s for s in statements if s["Effect"] == "Allow" and action in _actions(s)]


def _upload_arns(bucket: str) -> list[str]:
    return sorted(f"arn:aws:s3:::{bucket}/{prefix}" for prefix in EVIDENCE_UPLOAD_PREFIXES)


def _bucket(deployment: Synthesized, usage: str) -> str:
    return deployment.config.bucket_name(usage, ACCOUNT)


def _stack_output(deployment: Synthesized, stack: str, attribute: str) -> Any:
    """Referencia que usa ``vigia-compute`` para un atributo de otra pila."""
    return next(
        output
        for output in _compute_outputs_used(deployment)
        if output["Fn::GetStackOutput"]["StackName"] == stack
        and attribute in output["Fn::GetStackOutput"]["OutputName"]
    )


def _compute_outputs_used(deployment: Synthesized) -> Iterator[JsonObject]:
    def walk(value: Any) -> Iterator[JsonObject]:
        if isinstance(value, Mapping):
            if "Fn::GetStackOutput" in value:
                yield value
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    yield from walk(_compute(deployment)["Resources"])


# --- Recursos por despliegue ----------------------------------------------------------


def test_each_deployment_has_its_compute_resources(deployment: Synthesized) -> None:
    found = Counter(r["Type"] for _, r in resources(_compute(deployment)))
    assert found == expected_compute_resources(deployment.config)


def test_compute_stack_passes_the_policy_rules(deployment: Synthesized) -> None:
    violations = check_policy_wildcards("vigia-compute", _compute(deployment))
    assert not violations, describe(violations)


# --- Criterio 1: escritura de vigia-api-task en vigia-evidence (A-34) -------------------


def test_api_task_policy_has_no_undeclared_wildcards(deployment: Synthesized) -> None:
    template = _compute(deployment)
    for statement in _role_statements(deployment, deployment.config.resource_name("api-task")):
        actions = _actions(statement)
        assert not any("*" in a or "?" in a for a in actions), statement
        if statement["Effect"] != "Allow":
            continue
        for raw in _as_list(statement["Resource"]):
            text = render(raw, template)
            if text == "*":
                assert actions <= NO_RESOURCE_LEVEL_ACTIONS, statement
            elif "*" in text or "?" in text:
                assert any(w.pattern.match(text) for w in RESOURCE_WILDCARDS), text


def test_api_task_writes_only_through_one_checksummed_put_object(deployment: Synthesized) -> None:
    template = _compute(deployment)
    statements = _role_statements(deployment, deployment.config.resource_name("api-task"))
    uploads = _upload_arns(_bucket(deployment, "evidence"))
    (allow,) = _granting(statements, "s3:PutObject")
    assert _actions(allow) == {"s3:PutObject"}
    assert _rendered(allow["Resource"], template) == uploads
    assert allow["Condition"] == {"Null": {"s3:x-amz-checksum-sha256": "false"}}
    (deny,) = [s for s in statements if s["Effect"] == "Deny"]
    assert _actions(deny) == {"s3:PutObject"}
    assert "Resource" not in deny
    assert _rendered(deny["NotResource"], template) == uploads
    # Ninguna otra acción de escritura o borrado en S3.
    s3_actions = {a for s in statements if s["Effect"] == "Allow" for a in _actions(s)}
    assert {a for a in s3_actions if a.startswith("s3:")} == {"s3:GetObject", "s3:PutObject"}


def test_api_task_has_no_encryption_condition(deployment: Synthesized) -> None:
    """A-34: la condición de cifrado se retira (el depósito cifra por defecto)."""
    statements = _role_statements(deployment, deployment.config.resource_name("api-task"))
    assert "s3:x-amz-server-side-encryption" not in str(statements)


def test_upload_prefixes_are_exactly_the_three_of_a_34() -> None:
    assert EVIDENCE_UPLOAD_PREFIXES == (
        "org/*/plant/*/zone/*/node/*",
        "org/*/plant/*/documents/*",
        "org/*/closure/*",
    )
    pattern = next(w.pattern for w in RESOURCE_WILDCARDS if w.name == "evidence-upload-prefixes")
    evidence = "arn:aws:s3:::vigia-evidence-<AccountId>-us-east-1"
    for prefix in EVIDENCE_UPLOAD_PREFIXES:
        assert pattern.match(f"{evidence}/{prefix}")
    # Borde: otro prefijo, un prefijo más ancho o una ruta libre no son la forma declarada.
    for other in ("org/*/export/*", "org/*", "org/*/plant/*", "*", "org/*/closure/*/x*"):
        assert not pattern.match(f"{evidence}/{other}"), other


# --- Criterio 2: definición de tarea de vigia-api sin secretos ni tmpfs ------------------


def _secret_logical_ids(deployment: Synthesized) -> set[str]:
    return {
        logical_id
        for template in deployment.templates.values()
        for logical_id, r in resources(template)
        if r["Type"] == "AWS::SecretsManager::Secret"
    }


def test_task_definitions_reference_no_secret_values(deployment: Synthesized) -> None:
    secrets = _secret_logical_ids(deployment)
    for _, task_definition in _of_type(_compute(deployment), TASK_DEFINITION):
        for container in properties(task_definition)["ContainerDefinitions"]:
            assert "Secrets" not in container, container["Name"]
            for name, value in _environment(container).items():
                text = str(value)
                assert "{{resolve:secretsmanager" not in text, name
                assert "assistant/api-key" not in text, name
                _, reference = _resolve(value, deployment)
                target = reference_target(reference)
                assert target not in secrets, name


def test_task_definitions_declare_no_tmpfs(deployment: Synthesized) -> None:
    """Nota U02-H-10: Fargate no admite ``tmpfs``; ``/tmp`` es el volumen efímero."""
    for _, task_definition in _of_type(_compute(deployment), TASK_DEFINITION):
        props = properties(task_definition)
        assert props["EphemeralStorage"] == {"SizeInGiB": 21}
        assert props["Volumes"] == [{"Name": "tmp"}]
        for container in props["ContainerDefinitions"]:
            assert "Tmpfs" not in container.get("LinuxParameters", {}), container["Name"]
            assert container["ReadonlyRootFilesystem"] is True, container["Name"]
            assert container["LinuxParameters"]["Capabilities"] == {"Drop": ["ALL"]}
            assert "Add" not in container["LinuxParameters"]["Capabilities"]
            assert "Privileged" not in container


def test_main_containers_run_as_vigia_with_tmp_from_the_volume(pilot: Synthesized) -> None:
    for family in ("api", "worker", "migrate", "admin"):
        container = _container(_task_definition(pilot, family), family)
        assert container["User"] == "vigia"
        assert container["MountPoints"] == [
            {"ContainerPath": "/tmp", "ReadOnly": False, "SourceVolume": "tmp"}  # noqa: S108
        ]


# --- Criterio 3: vigia-api en dos zonas y en los dos grupos de destino --------------------


def _target_group_names(deployment: Synthesized, service: JsonObject) -> dict[str, int]:
    names = {}
    for entry in properties(service)["LoadBalancers"]:
        group = _resolved_resource(entry["TargetGroupArn"], deployment)
        names[properties(group)["Name"]] = entry["ContainerPort"]
    return names


def test_api_runs_two_tasks_in_two_zones_on_both_target_groups(pilot: Synthesized) -> None:
    _, api = _service(pilot, "vigia-api")
    props = properties(api)
    assert props["DesiredCount"] == 2
    zones = {
        properties(_resolved_resource(subnet, pilot))["AvailabilityZone"]
        for subnet in props["NetworkConfiguration"]["AwsvpcConfiguration"]["Subnets"]
    }
    assert zones == {"us-east-1a", "us-east-1b"}
    assert props["AvailabilityZoneRebalancing"] == "ENABLED"
    assert _target_group_names(pilot, api) == {"tg-api": 8000, "tg-api-nodes": 8000}


def test_api_scales_from_two_to_six(pilot: Synthesized) -> None:
    template = _compute(pilot)
    api_id, _ = _service(pilot, "vigia-api")
    (target,) = [
        properties(r)
        for _, r in _of_type(template, SCALABLE_TARGET)
        if f"<GetAtt.{api_id}.Name>" in render(properties(r)["ResourceId"], template)
    ]
    assert (target["MinCapacity"], target["MaxCapacity"]) == (2, 6)


def test_first_deploy_starts_with_zero_tasks_and_only_tg_api() -> None:
    """VIG-41: sin raíz no hay escucha de nodos y ECS rechazaría ``tg-api-nodes``."""
    deployment = _synth(first_deploy="true")
    _, api = _service(deployment, "vigia-api")
    _, worker = _service(deployment, "vigia-worker")
    assert properties(api)["DesiredCount"] == 0
    assert properties(worker)["DesiredCount"] == 0
    assert _target_group_names(deployment, api) == {"tg-api": 8000}
    assert not list(_of_type(_compute(deployment), SCALABLE_TARGET))


def test_passthrough_registers_the_tls_port_of_the_application() -> None:
    deployment = _synth(nodes_tls_mode="passthrough")
    _, api = _service(deployment, "vigia-api")
    assert _target_group_names(deployment, api) == {"tg-api": 8000, "tg-api-nodes": 8443}
    container = _container(_task_definition(deployment, "api"), "api")
    assert [p["ContainerPort"] for p in container["PortMappings"]] == [8000, 8443]
    assert _environment(container)["VIGIA_NODES_TLS_PORT"] == "8443"


def test_staging_keeps_the_pilot_minimums() -> None:
    deployment = _synth(environment="staging-7")
    _, api = _service(deployment, "vigia-api")
    _, worker = _service(deployment, "vigia-worker")
    assert (properties(api)["DesiredCount"], properties(worker)["DesiredCount"]) == (2, 1)


# --- Criterio 4: permisos del arranque solo con first_deploy o ca_rotation ---------------


def _admin_and_migrate(deployment: Synthesized) -> tuple[list[JsonObject], list[JsonObject]]:
    config = deployment.config
    return (
        _role_statements(deployment, config.resource_name("admin-task")),
        _role_statements(deployment, config.resource_name("migrate-task")),
    )


def _writes_edge(statements: list[JsonObject], template: JsonObject, bucket: str) -> bool:
    return any(
        bucket in text
        for s in _granting(statements, "s3:PutObject")
        for text in _rendered(s["Resource"], template)
    )


def _reads_secret(statements: list[JsonObject], reference: Any) -> bool:
    return any(
        reference in _as_list(s["Resource"])
        for s in _granting(statements, "secretsmanager:GetSecretValue")
    )


def _master_secret(deployment: Synthesized) -> Any:
    data = deployment.config.stack_name("data")
    matches = [
        output
        for output in _compute_outputs_used(deployment)
        if output["Fn::GetStackOutput"]["StackName"] == data
        and "MasterUserSecret" in output["Fn::GetStackOutput"]["OutputName"]
    ]
    return matches[0] if matches else None


def _app_secret(deployment: Synthesized) -> Any:
    return _stack_output(deployment, deployment.config.stack_name("data"), "DbSecretapp")


def test_without_bootstrap_contexts_admin_cannot_sign_or_publish(pilot: Synthesized) -> None:
    template = _compute(pilot)
    admin, migrate = _admin_and_migrate(pilot)
    assert not _granting(admin, "kms:Sign")
    assert not _granting(admin, "kms:GetPublicKey")
    assert not _writes_edge(admin, template, _bucket(pilot, "edge"))
    assert _master_secret(pilot) is None  # nadie en vigia-compute nombra el secreto maestro
    assert not _reads_secret(migrate, _app_secret(pilot))
    names = {
        render(properties(p)["ManagedPolicyName"], template) for _, p in _of_type(template, POLICY)
    }
    assert not {n for n in names if n.endswith("-bootstrap")}


def test_first_deploy_grants_the_bootstrap_permissions() -> None:
    deployment = _synth(first_deploy="true")
    template = _compute(deployment)
    admin, migrate = _admin_and_migrate(deployment)
    (sign,) = _granting(admin, "kms:Sign")
    assert sign["Condition"] == {"StringEquals": {"kms:SigningAlgorithm": "ECDSA_SHA_256"}}
    node_ca = _resolved_resource(sign["Resource"], deployment)
    assert node_ca["Properties"]["KeySpec"] == "ECC_NIST_P256"
    assert _granting(admin, "kms:GetPublicKey")
    (publish,) = _granting(admin, "s3:PutObject")
    edge = _bucket(deployment, "edge")
    assert _rendered(publish["Resource"], template) == [f"arn:aws:s3:::{edge}/ca/root.pem"]
    assert _reads_secret(migrate, _master_secret(deployment))
    assert _reads_secret(migrate, _app_secret(deployment))


def test_ca_rotation_grants_the_admin_permissions_but_not_the_master_secret() -> None:
    deployment = _synth(ca_rotation="true")
    template = _compute(deployment)
    admin, migrate = _admin_and_migrate(deployment)
    assert _granting(admin, "kms:Sign")
    assert _writes_edge(admin, template, _bucket(deployment, "edge"))
    assert _master_secret(deployment) is None
    assert not _reads_secret(migrate, _app_secret(deployment))


def test_migrate_reads_only_its_secret_outside_the_first_deploy(pilot: Synthesized) -> None:
    _, migrate = _admin_and_migrate(pilot)
    (read,) = _granting(migrate, "secretsmanager:GetSecretValue")
    (secret,) = _as_list(read["Resource"])
    assert "DbSecretmigrate" in secret["Fn::GetStackOutput"]["OutputName"]
    environment = _environment(_container(_task_definition(pilot, "migrate"), "migrate"))
    assert "VIGIA_DB_MASTER_SECRET_ARN" not in environment


# --- Roles (§8) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("context", "suffix"),
    [({}, ""), ({"environment": "staging-7"}, "-staging-7"), ({"instance": "acme"}, "-acme")],
    ids=["pilot", "staging-7", "dedicated-acme"],
)
def test_roles_have_fixed_names(context: dict[str, str], suffix: str) -> None:
    deployment = _synth(**context)
    template = _compute(deployment)
    names = {render(properties(r)["RoleName"], template) for _, r in _of_type(template, ROLE)}
    expected = {
        f"vigia-{role}{suffix}"
        for role in (
            "task-execution",
            "api-task",
            "worker-task",
            "migrate-task",
            "admin-task",
            "restore",
        )
    }
    if deploy_role_owned(deployment.config):
        expected.add("vigia-deploy" if not suffix else f"vigia-deploy{suffix}")
    assert names == expected


def test_worker_role_is_published_for_other_units(deployment: Synthesized) -> None:
    template = _compute(deployment)
    ((_, parameter),) = _of_type(template, "AWS::SSM::Parameter")
    props = properties(parameter)
    assert props["Name"] == f"/vigia/{deployment.config.deployment}/worker-task-role-arn"
    worker_id, _ = _role(deployment, deployment.config.resource_name("worker-task"))
    assert props["Value"] == {"Fn::GetAtt": [worker_id, "Arn"]}


def test_task_roles_trust_only_ecs_tasks_of_the_account(pilot: Synthesized) -> None:
    for role in ("task-execution", "api-task", "worker-task", "migrate-task", "admin-task"):
        _, resource = _role(pilot, f"vigia-{role}")
        (statement,) = properties(resource)["AssumeRolePolicyDocument"]["Statement"]
        assert statement["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
        assert statement["Condition"] == {
            "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}
        }


def test_no_role_can_delete_logs_evidence_archives_or_the_database(deployment: Synthesized) -> None:
    """§8: ningún rol con borrado de registros, objetos o la base (NFR-NUC-20, 28)."""
    template = _compute(deployment)
    forbidden = {
        "logs:DeleteLogGroup",
        "logs:DeleteLogStream",
        "s3:DeleteObject",
        "rds:DeleteDBInstance",
    }
    for _, policy in _of_type(template, POLICY):
        for statement in _as_list(properties(policy)["PolicyDocument"]["Statement"]):
            if statement["Effect"] == "Allow":
                assert not _actions(statement) & forbidden, statement


def test_execution_role_pulls_the_image_and_writes_the_deployment_logs(pilot: Synthesized) -> None:
    template = _compute(pilot)
    statements = _role_statements(pilot, "vigia-task-execution")
    assert {a for s in statements for a in _actions(s)} == {
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
    (logs_statement,) = _granting(statements, "logs:PutLogEvents")
    assert _rendered(logs_statement["Resource"], template) == [
        f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/vigia/pilot/*"
    ]


def test_worker_publishes_revocations_and_reads_the_edge_ca(pilot: Synthesized) -> None:
    template = _compute(pilot)
    statements = _role_statements(pilot, "vigia-worker-task")
    edge_ca = [f"arn:aws:s3:::{_bucket(pilot, 'edge')}/ca/*"]
    (read,) = _granting(statements, "s3:GetObjectVersion")
    assert _actions(read) == {"s3:GetObject", "s3:GetObjectVersion"}
    assert _rendered(read["Resource"], template) == edge_ca
    puts = {
        r for s in _granting(statements, "s3:PutObject") for r in _rendered(s["Resource"], template)
    }
    assert puts == {edge_ca[0], f"arn:aws:s3:::{_bucket(pilot, 'archive')}/*"}
    (revocations,) = _granting(statements, "elasticloadbalancing:AddTrustStoreRevocations")
    assert _actions(revocations) == {
        "elasticloadbalancing:AddTrustStoreRevocations",
        "elasticloadbalancing:RemoveTrustStoreRevocations",
        "elasticloadbalancing:DescribeTrustStoreRevocations",
        "elasticloadbalancing:ModifyTrustStore",
    }
    store = _resolved_resource(revocations["Resource"], pilot)
    assert store["Type"] == "AWS::ElasticLoadBalancingV2::TrustStore"
    # El worker no lleva la denegación de vigia-api-task: U-04 le adjunta vigia-loop-worker.
    assert not [s for s in statements if s["Effect"] == "Deny"]


@pytest.mark.parametrize(
    "context",
    [{"first_deploy": "true"}, {"nodes_tls_mode": "passthrough"}],
    ids=["first-deploy", "passthrough"],
)
def test_without_a_trust_store_there_are_no_revocation_permissions(context: dict[str, str]) -> None:
    deployment = _synth(**context)
    statements = _role_statements(deployment, "vigia-worker-task")
    assert not [
        s for s in statements if any(a.startswith("elasticloadbalancing:") for a in _actions(s))
    ]
    worker = _environment(_container(_task_definition(deployment, "worker"), "worker"))
    assert "VIGIA_NODE_TRUST_STORE_ARN" not in worker


def test_restore_role_needs_the_second_factor_and_reaches_the_drill(pilot: Synthesized) -> None:
    template = _compute(pilot)
    _, role = _role(pilot, "vigia-restore")
    (trust,) = properties(role)["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Condition"] == {"Bool": {"aws:MultiFactorAuthPresent": "true"}}
    statements = _role_statements(pilot, "vigia-restore")
    evidence = f"arn:aws:s3:::{_bucket(pilot, 'evidence')}/org/*"
    reads = {
        r
        for s in _granting(statements, "s3:GetObjectVersion")
        for r in _rendered(s["Resource"], template)
    }
    assert evidence in reads
    assert f"arn:aws:s3:::{_bucket(pilot, 'archive')}/*" in reads
    (drill,) = _granting(statements, "s3:PutObject")
    assert _rendered(drill["Resource"], template) == [f"arn:aws:s3:::{_bucket(pilot, 'drill')}/*"]
    keys = {
        _resolved_resource(r, pilot)["Properties"]["Description"].split(":")[0]: _actions(s)
        for s in statements
        for r in _as_list(s.get("Resource"))
        if isinstance(r, Mapping)
        and "Fn::GetStackOutput" in r
        and "Key" in r["Fn::GetStackOutput"]["OutputName"]
    }
    assert keys["vigia-evidence"] == {"kms:Decrypt", "kms:GenerateDataKey"}
    assert {"kms:Decrypt"} <= keys["vigia-archive"]
    assert {a for s in statements for a in _actions(s) if a.startswith("rds:")} == {
        "rds:RestoreDBInstanceToPointInTime",
        "rds:RestoreDBInstanceFromDBSnapshot",
    }


def test_evidence_key_admits_the_restore_role(pilot: Synthesized) -> None:
    """Sin su sentencia en la política de clave, el ``kms:Decrypt`` del rol no sirve."""
    foundation = pilot.templates["vigia-foundation"]
    (key,) = [
        r
        for _, r in _of_type(foundation, "AWS::KMS::Key")
        if r["Properties"]["Description"].startswith("vigia-evidence:")
    ]
    (statement,) = [
        s
        for s in key["Properties"]["KeyPolicy"]["Statement"]
        if s["Sid"] == "RestoreDrillThroughS3"
    ]
    assert render(statement["Condition"]["ArnEquals"]["aws:PrincipalArn"][0], foundation).endswith(
        ":role/vigia-restore"
    )


# --- vigia-deploy (§8, deployment-architecture §3.1) ------------------------------------


def test_deploy_role_federates_only_the_platform_repository(pilot: Synthesized) -> None:
    _, role = _role(pilot, "vigia-deploy")
    (trust,) = properties(role)["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert render(trust["Principal"]["Federated"], _compute(pilot)).endswith(
        ":oidc-provider/token.actions.githubusercontent.com"
    )
    assert trust["Condition"] == {
        "StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": [
                "repo:Machaves07/vigia-platform:environment:pilot",
                "repo:Machaves07/vigia-platform:environment:staging",
            ],
        }
    }


def test_deploy_role_reads_the_edge_ca_and_writes_no_bucket(pilot: Synthesized) -> None:
    template = _compute(pilot)
    statements = _role_statements(pilot, "vigia-deploy")
    (read,) = _granting(statements, "s3:GetObject")
    assert _rendered(read["Resource"], template) == [f"arn:aws:s3:::{_bucket(pilot, 'edge')}/ca/*"]
    assert not _granting(statements, "s3:PutObject")
    (decrypt,) = _granting(statements, "kms:Decrypt")
    assert decrypt["Condition"]["StringEquals"] == {"kms:ViaService": "s3.us-east-1.amazonaws.com"}
    assert _granting(statements, "elasticloadbalancing:ModifyTrustStore")
    (run,) = _granting(statements, "ecs:RunTask")
    task_definition = template["Resources"][reference_target(run["Resource"])]
    assert properties(task_definition)["Family"] == "vigia-migrate"


def test_staging_uses_the_account_deploy_role(staging: Synthesized) -> None:
    template = _compute(staging)
    names = {render(properties(r)["RoleName"], template) for _, r in _of_type(template, ROLE)}
    assert not {n for n in names if n.startswith("vigia-deploy")}


# --- Registro, clúster y servicios (§5.1, §5.2) -----------------------------------------


def test_pilot_owns_the_immutable_scanned_registry(pilot: Synthesized) -> None:
    ((_, registry),) = _of_type(_compute(pilot), "AWS::ECR::Repository")
    props = properties(registry)
    assert props["RepositoryName"] == "vigia-platform"
    assert props["ImageTagMutability"] == "IMMUTABLE"
    assert props["ImageScanningConfiguration"] == {"ScanOnPush": True}
    # AES-256 del servicio: CDK omite la propiedad (valor por defecto del registro).
    assert props.get("EncryptionConfiguration", {"EncryptionType": "AES256"}) == {
        "EncryptionType": "AES256"
    }
    assert '"countNumber":20' in props["LifecyclePolicy"]["LifecyclePolicyText"]
    assert registry["DeletionPolicy"] == "Retain"


@pytest.mark.parametrize(
    "context", [{"environment": "staging-7"}, {"instance": "acme"}], ids=["staging-7", "acme"]
)
def test_other_deployments_import_the_account_registry(context: dict[str, str]) -> None:
    deployment = _synth(**context)
    assert not list(_of_type(_compute(deployment), "AWS::ECR::Repository"))
    image = _container(_task_definition(deployment, "api"), "api")["Image"]
    assert "/vigia-platform@sha256:" in render(image, _compute(deployment))


def test_cluster_has_container_insights(deployment: Synthesized) -> None:
    ((_, cluster),) = _of_type(_compute(deployment), "AWS::ECS::Cluster")
    props = properties(cluster)
    assert props["ClusterName"] == f"vigia-{deployment.config.deployment}"
    assert props["ClusterSettings"] == [{"Name": "containerInsights", "Value": "enabled"}]


@pytest.mark.parametrize(
    ("family", "cpu", "memory"),
    [
        ("api", "512", "1024"),
        ("worker", "1024", "2048"),
        ("migrate", "512", "1024"),
        ("admin", "512", "1024"),
    ],
)
def test_task_definitions_are_fargate_arm64(
    pilot: Synthesized, family: str, cpu: str, memory: str
) -> None:
    props = properties(_task_definition(pilot, family))
    assert (props["Cpu"], props["Memory"]) == (cpu, memory)
    assert props["RequiresCompatibilities"] == ["FARGATE"]
    assert props["RuntimePlatform"] == {
        "CpuArchitecture": "ARM64",
        "OperatingSystemFamily": "LINUX",
    }


def test_services_roll_with_circuit_breaker_and_without_exec(deployment: Synthesized) -> None:
    for name in ("vigia-api", "vigia-worker"):
        _, service = _service(deployment, name)
        props = properties(service)
        assert props["EnableExecuteCommand"] is False
        assert props["DeploymentConfiguration"] == {
            "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
            "MaximumPercent": 200,
            "MinimumHealthyPercent": 100,
        }
        assert props["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"] == "DISABLED"
        assert (props["LaunchType"], props["PlatformVersion"]) == ("FARGATE", "LATEST")


def test_api_carries_the_sizes_of_pendiente_17(pilot: Synthesized) -> None:
    environment = _environment(_container(_task_definition(pilot, "api"), "api"))
    assert {name: environment[name] for name in API_TUNING} == {
        "VIGIA_UVICORN_WORKERS": "2",
        "VIGIA_BULKHEAD_NODE": "35",
        "VIGIA_BULKHEAD_PERSON": "15",
        "VIGIA_DB_POOL_NODE": "10",
        "VIGIA_DB_POOL_PERSON": "5",
        "VIGIA_DB_MAX_OVERFLOW": "0",
        "VIGIA_DB_POOL_TIMEOUT_SECONDS": "5",
        "VIGIA_THREADPOOL_SIZE": "4",
        "VIGIA_DOCUMENTS_PREFIX": "documents/",
        "VIGIA_DOCUMENTS_MAX_BYTES": "20971520",
    }


RUNTIME_CONFIG = Path(__file__).resolve().parents[2] / (
    "backend/src/vigia_platform/shared/runtime/config.py"
)


def runtime_variables() -> tuple[str, ...]:
    """Las variables que lee ``RuntimeConfig.from_environ`` (``VARIABLES`` de la raíz de
    composición de VIG-137), leídas con ``ast``: la infraestructura no instala el backend."""
    tree = ast.parse(RUNTIME_CONFIG.read_text(encoding="utf-8"))
    (table,) = [
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "VARIABLES"
    ]
    assert isinstance(table, ast.Tuple)
    names = []
    for call in table.elts:
        assert isinstance(call, ast.Call)
        name = call.args[1]
        assert isinstance(name, ast.Constant) and isinstance(name.value, str)
        names.append(name.value)
    return tuple(names)


U03_VARIABLES = {
    # U-03 §6 (pendiente nº 17) y TASK-219: lo que lee la raíz para vigia-api.
    "api": (
        "VIGIA_UVICORN_WORKERS",
        "VIGIA_BULKHEAD_NODE",
        "VIGIA_BULKHEAD_PERSON",
        "VIGIA_DB_POOL_NODE",
        "VIGIA_DB_POOL_PERSON",
        "VIGIA_DB_MAX_OVERFLOW",
        "VIGIA_DB_POOL_TIMEOUT_SECONDS",
        "VIGIA_THREADPOOL_SIZE",
        "VIGIA_DOCUMENTS_PREFIX",
        "VIGIA_DOCUMENTS_MAX_BYTES",
        "VIGIA_NODES_BASE_URL",
        "VIGIA_EDGE_BUCKET",
    ),
    # Publicación de la lista de revocación (§5.3).
    "worker": ("VIGIA_NODE_TRUST_STORE_ARN", "VIGIA_EDGE_BUCKET", "VIGIA_CRL_KEY"),
}
"""Variables de U-03 por definición de tarea."""

RUNTIME_VARIABLES_OUTSIDE_TASKS = {
    "VIGIA_AWS_ENDPOINT_URL": "solo local y test (LocalStack)",
    "PGSSLMODE": "por defecto verify-full",
    "PGSSLROOTCERT": "por defecto el paquete de la imagen",
    "VIGIA_BREACH_LIST_PATH": "por defecto el respaldo de la imagen",
    "VIGIA_PROVIDER_ORGANIZATION_ID": "solo con el contexto provider_organization_id",
    "VIGIA_ENROLLMENT_SOURCE_KEY_SECRET": "su secreto aun no existe: VIG-174 (sin el, el alta "
    "falla cerrada)",
}
"""Variables de ``RuntimeConfig`` que ninguna definición de tarea de ``pilot`` lleva, con el
motivo. Cualquier otra que la raíz lea tiene que estar en una de las dos."""


def test_u03_variables_are_the_ones_the_composition_root_reads(pilot: Synthesized) -> None:
    """Ninguna variable de U-03 que lee la raíz falta en su definición de tarea, ninguna sobra, y
    ninguna lleva un secreto (§6: solo nombres, ARN y tamaños)."""
    read = set(runtime_variables())
    environments = {
        family: _environment(_container(_task_definition(pilot, family), family))
        for family in ("api", "worker")
    }
    for family, names in U03_VARIABLES.items():
        assert set(names) <= read, set(names) - read
        assert set(names) <= set(environments[family]), set(names) - set(environments[family])
    assert "VIGIA_ENROLLMENT_PUBLIC_URL" not in environments["api"]  # ningún proceso la lee
    missing = read - set(environments["api"]) - set(environments["worker"])
    assert missing == set(RUNTIME_VARIABLES_OUTSIDE_TASKS), missing
    assert not set(RUNTIME_VARIABLES_OUTSIDE_TASKS) & set(U03_VARIABLES["api"])
    template = _compute(pilot)
    for family in ("api", "worker"):
        container = _container(_task_definition(pilot, family), family)
        assert "Secrets" not in container, family
        for name in (*U03_VARIABLES[family], *API_TUNING):
            if name not in environments[family]:
                continue
            value = render(environments[family][name], template)
            assert "secretsmanager" not in value and "resolve:" not in value, name
            assert not re.search(r"(?i)password|secret", name), name
    assert environments["worker"]["VIGIA_CRL_KEY"] == "ca/crl.pem"
    edge = _bucket(pilot, "edge")
    for family in ("api", "worker"):
        assert render(environments[family]["VIGIA_EDGE_BUCKET"], template) == edge, family
    trust_store = environments["worker"]["VIGIA_NODE_TRUST_STORE_ARN"]
    assert "Fn::GetStackOutput" in trust_store


def test_api_reads_only_the_published_root_of_vigia_edge(pilot: Synthesized) -> None:
    """El alta y la emisión de códigos leen ``ca/root.pem``; vigia-api no escribe en vigia-edge."""
    template = _compute(pilot)
    statements = _role_statements(pilot, pilot.config.resource_name("api-task"))
    edge = _bucket(pilot, "edge")
    reads = [
        text
        for s in _granting(statements, "s3:GetObject")
        for text in _rendered(s["Resource"], template)
        if edge in text
    ]
    assert reads == [f"arn:aws:s3:::{edge}/ca/root.pem"]
    assert not _writes_edge(statements, template, edge)


@pytest.mark.parametrize(
    ("context", "bucket"),
    [
        ({}, f"vigia-evidence-{ACCOUNT}-us-east-1"),
        ({"environment": "staging-7"}, f"vigia-evidence-staging-7-{ACCOUNT}-us-east-1"),
    ],
    ids=["pilot", "staging-7"],
)
def test_csp_store_origin_is_the_evidence_bucket_of_the_environment(
    context: dict[str, str], bucket: str
) -> None:
    """Nº 10: un solo origen, el depósito de evidencias del entorno (U-05 §5.1)."""
    deployment = _synth(**context)
    environment = _environment(_container(_task_definition(deployment, "api"), "api"))
    origin = render(environment["VIGIA_CSP_STORE_ORIGINS"], _compute(deployment))
    assert origin == f"https://{bucket}.s3.us-east-1.amazonaws.com"
    assert " " not in origin


RUNTIME_REFERENCE = re.compile(r"vigia_platform(?:\.[a-z_][a-z0-9_]*)+:[a-z_][a-z0-9_]*")
"""``_RUNTIME_REFERENCE`` de los tres puntos de entrada del backend (se comprueba abajo que es el
mismo patrón): una variable solo puede nombrar un constructor del propio paquete."""
BACKEND_SRC = Path(__file__).resolve().parents[2] / "backend" / "src"
ENTRY_POINTS = (
    "vigia_platform/shared/api/main.py",
    "vigia_platform/shared/worker/main.py",
    "vigia_platform/identity/application/admin_cli.py",
)
PROVIDER = "0192f2b0-0000-4000-8000-0000000000aa"


def test_task_definitions_name_the_production_runtime_builders(deployment: Synthesized) -> None:
    """VIG-137 (A-52): cada proceso nombra su constructor de ``vigia_platform.shared.runtime``,
    con una referencia que acepta su ``_RUNTIME_REFERENCE`` y que existe en el backend."""
    for source in ENTRY_POINTS:
        assert RUNTIME_REFERENCE.pattern in (BACKEND_SRC / source).read_text(encoding="utf-8")
    for family, (variable, reference) in RUNTIME_VARIABLES.items():
        environment = _environment(_container(_task_definition(deployment, family), family))
        assert environment[variable] == reference
        assert RUNTIME_REFERENCE.fullmatch(reference), reference
        module, function = reference.split(":")
        path = BACKEND_SRC / Path(*module.split(".")).with_suffix(".py")
        assert f"async def {function}(" in path.read_text(encoding="utf-8"), reference
        others = {name for name, _ in RUNTIME_VARIABLES.values()} - {variable}
        assert not others & set(environment), family
    migrate = _environment(_container(_task_definition(deployment, "migrate"), "migrate"))
    assert not {name for name, _ in RUNTIME_VARIABLES.values()} & set(migrate)
    assert tuple(reference for _, reference in RUNTIME_VARIABLES.values()) == (
        API_RUNTIME,
        WORKER_RUNTIME,
        ADMIN_RUNTIME,
    )


def test_api_and_admin_receive_the_public_origin(deployment: Synthesized) -> None:
    """Base de los enlaces de invitación y origen de la barrera anti-falsificación."""
    template = _compute(deployment)
    for family in ("api", "admin"):
        environment = _environment(_container(_task_definition(deployment, family), family))
        origin = render(environment["VIGIA_PUBLIC_ORIGIN"], template)
        assert origin.startswith("https://"), origin
        assert "/" not in origin.removeprefix("https://"), origin
    worker = _environment(_container(_task_definition(deployment, "worker"), "worker"))
    assert "VIGIA_PUBLIC_ORIGIN" not in worker


def test_provider_organization_reaches_the_processes_only_with_its_context(
    pilot: Synthesized,
) -> None:
    families = ("api", "worker", "admin")
    for family in families:
        assert "VIGIA_PROVIDER_ORGANIZATION_ID" not in _environment(
            _container(_task_definition(pilot, family), family)
        )
    deployment = _synth(provider_organization_id=PROVIDER)
    for family in families:
        environment = _environment(_container(_task_definition(deployment, family), family))
        assert environment["VIGIA_PROVIDER_ORGANIZATION_ID"] == PROVIDER
    migrate = _environment(_container(_task_definition(deployment, "migrate"), "migrate"))
    assert "VIGIA_PROVIDER_ORGANIZATION_ID" not in migrate


@pytest.mark.parametrize(
    "value",
    [PROVIDER.upper(), "no-es-un-uuid", f"{PROVIDER} ", "{" + PROVIDER + "}"],
)
def test_a_malformed_provider_organization_stops_the_synthesis(value: str) -> None:
    with pytest.raises(ContextError, match="provider_organization_id"):
        _synth(provider_organization_id=value)


def test_statement_timeout_and_tmp_budget_are_only_in_the_worker(pilot: Synthesized) -> None:
    worker = _container(_task_definition(pilot, "worker"), "worker")
    environment = _environment(worker)
    assert environment["VIGIA_DB_STATEMENT_TIMEOUT_MS"] == "30000"
    assert environment["VIGIA_TMP_BUDGET_BYTES"] == str(4 * 1024**3)
    assert worker["StopTimeout"] == 120
    assert worker["Command"] == ["vigia-worker"]
    for family in ("api", "migrate", "admin"):
        assert "VIGIA_DB_STATEMENT_TIMEOUT_MS" not in _environment(
            _container(_task_definition(pilot, family), family)
        )


def test_one_off_tasks_run_their_commands(pilot: Synthesized) -> None:
    assert _container(_task_definition(pilot, "migrate"), "migrate")["Command"] == [
        "alembic",
        "upgrade",
        "head",
    ]
    assert _container(_task_definition(pilot, "admin"), "admin")["Command"][0] == "vigia-admin"


def test_logs_go_non_blocking_to_the_deployment_groups(staging: Synthesized) -> None:
    for family in ("api", "worker", "migrate", "admin"):
        task_definition = _task_definition(staging, family)
        for container in properties(task_definition)["ContainerDefinitions"]:
            options = container["LogConfiguration"]["Options"]
            process = container["Name"]
            assert options["awslogs-group"] == f"/vigia/staging-7/{process}"
            assert options["mode"] == "non-blocking"
            assert options["max-buffer-size"] == "26214400b"


# --- Escalado (§5.2) ------------------------------------------------------------------


def _scaling_policies(deployment: Synthesized) -> list[JsonObject]:
    return [properties(r) for _, r in _of_type(_compute(deployment), SCALING_POLICY)]


def test_api_tracks_cpu_and_requests_per_target(pilot: Synthesized) -> None:
    tracking = {
        p["TargetTrackingScalingPolicyConfiguration"]["PredefinedMetricSpecification"][
            "PredefinedMetricType"
        ]: p["TargetTrackingScalingPolicyConfiguration"]
        for p in _scaling_policies(pilot)
        if p["PolicyType"] == "TargetTrackingScaling" and "ApiScaling" in str(p["ScalingTargetId"])
    }
    cpu = tracking["ECSServiceAverageCPUUtilization"]
    requests = tracking["ALBRequestCountPerTarget"]
    assert (cpu["TargetValue"], requests["TargetValue"]) == (60, 300)
    for configuration in (cpu, requests):
        assert (configuration["ScaleInCooldown"], configuration["ScaleOutCooldown"]) == (120, 120)
    label = requests["PredefinedMetricSpecification"]["ResourceLabel"]
    assert "ApiTargetGroup" in str(label)


def test_worker_scales_on_the_outbox_age(pilot: Synthesized) -> None:
    template = _compute(pilot)
    alarms = [properties(r) for _, r in _of_type(template, "AWS::CloudWatch::Alarm")]
    by_periods = {a["EvaluationPeriods"]: a for a in alarms}
    assert set(by_periods) == {2, 15}
    for alarm in alarms:
        assert (alarm["Namespace"], alarm["MetricName"]) == (
            "Vigia/Platform",
            "outbox_oldest_pending_age_seconds",
        )
        assert alarm["Threshold"] == 30
        assert alarm["Period"] == 60
        assert sorted(alarm["Dimensions"], key=lambda d: d["Name"]) == [
            {"Name": "environment", "Value": "pilot"},
            {"Name": "service", "Value": "vigia-worker"},
        ]
    assert by_periods[2]["ComparisonOperator"] == "GreaterThanThreshold"
    assert by_periods[15]["ComparisonOperator"] == "LessThanOrEqualToThreshold"
    steps = {
        adjustment["ScalingAdjustment"]
        for p in _scaling_policies(pilot)
        if p["PolicyType"] == "StepScaling"
        for adjustment in p["StepScalingPolicyConfiguration"]["StepAdjustments"]
    }
    assert steps == {1, -1}
    worker_cpu = [
        p["TargetTrackingScalingPolicyConfiguration"]
        for p in _scaling_policies(pilot)
        if p["PolicyType"] == "TargetTrackingScaling"
        and "WorkerScaling" in str(p["ScalingTargetId"])
    ]
    assert [(c["TargetValue"], c["DisableScaleIn"]) for c in worker_cpu] == [(60, True)]


def test_scaling_uses_the_service_linked_role(pilot: Synthesized) -> None:
    template = _compute(pilot)
    for _, target in _of_type(template, SCALABLE_TARGET):
        assert render(properties(target)["RoleARN"], template).endswith(
            "/AWSServiceRoleForApplicationAutoScaling_ECSService"
        )


# --- Imagen por digest (§5.1) ---------------------------------------------------------


def test_release_digest_pins_both_services() -> None:
    deployment = _synth(image_digest=DIGEST)
    template = _compute(deployment)
    for family in ("api", "worker", "migrate", "admin"):
        image = render(_container(_task_definition(deployment, family), family)["Image"], template)
        assert image.endswith(
            f"/<Ref.{next(i for i, _ in _of_type(template, 'AWS::ECR::Repository'))}>@{DIGEST}"
        )


def test_without_a_digest_the_placeholder_is_used(pilot: Synthesized) -> None:
    image = render(_container(_task_definition(pilot, "api"), "api")["Image"], _compute(pilot))
    assert image.endswith(f"@{NO_IMAGE_DIGEST}")


@pytest.mark.parametrize(
    "value",
    [
        "latest",
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha512:" + "a" * 64,
        " sha256:" + "a" * 64,
        7,
    ],
)
def test_malformed_digests_stop_the_synthesis(value: object) -> None:
    app = App(context={"image_digest": value})
    with pytest.raises(ContextError, match="image_digest"):
        read_image_digest(app)


def test_digest_is_optional() -> None:
    assert read_image_digest(App()) is None
    assert read_image_digest(App(context={"image_digest": DIGEST})) == DIGEST


# --- Colector lateral (§9.1) ----------------------------------------------------------


def test_collector_listens_locally_and_exports_to_vigia_platform() -> None:
    text = COLLECTOR_CONFIG.read_text(encoding="utf-8")
    assert "endpoint: 127.0.0.1:4317" in text
    assert "namespace: Vigia/Platform" in text
    assert "[service, environment, route]" in text
    assert "[service, environment, component]" in text
    assert "timeout: 5s" in text and "send_batch_size: 512" in text
    assert re.search(r"exporters: \[awsxray\]", text)
    assert re.search(r"exporters: \[awsemf\]", text)


def test_collector_sidecar_is_pinned_and_restartable(pilot: Synthesized) -> None:
    for family, memory in (("api", 128), ("worker", 256)):
        task_definition = _task_definition(pilot, family)
        collector = _container(task_definition, "otel")
        assert re.fullmatch(
            r"public\.ecr\.aws/aws-observability/aws-otel-collector:v[0-9.]+@sha256:[0-9a-f]{64}",
            collector["Image"],
        )
        assert collector["Essential"] is False
        assert collector["RestartPolicy"]["Enabled"] is True
        assert collector["MemoryReservation"] == memory
        assert "PortMappings" not in collector
        environment = _environment(collector)
        assert "${" not in environment["AOT_CONFIG_CONTENT"]
        assert "log_group_name: /vigia/pilot/otel" in environment["AOT_CONFIG_CONTENT"]
        assert _container(task_definition, family)["DependsOn"] == [
            {"Condition": "START", "ContainerName": "otel"}
        ]
        assert (
            _environment(_container(task_definition, family))["OTEL_EXPORTER_OTLP_ENDPOINT"]
            == "http://localhost:4317"
        )


def test_collector_config_rejects_an_unknown_variable() -> None:
    with pytest.raises(KeyError, match="VIGIA_SERVICE"):
        render_collector_config({"AWS_REGION": "us-east-1"})


def test_api_and_worker_emit_embedded_metrics_to_the_otel_group_only(pilot: Synthesized) -> None:
    template = _compute(pilot)
    for role in ("vigia-api-task", "vigia-worker-task"):
        statements = _role_statements(pilot, role)
        (emf,) = _granting(statements, "logs:PutLogEvents")
        assert _rendered(emf["Resource"], template) == [
            f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/vigia/pilot/otel:*"
        ]
        (metrics,) = _granting(statements, "cloudwatch:PutMetricData")
        assert metrics["Condition"] == {"StringEquals": {"cloudwatch:namespace": "Vigia/Platform"}}


def test_nodes_tls_mode_is_declared_to_the_application(deployment: Synthesized) -> None:
    environment = _environment(_container(_task_definition(deployment, "api"), "api"))
    assert environment["VIGIA_NODES_TLS_MODE"] == deployment.config.nodes_tls_mode.value
    assert ("VIGIA_NODES_TLS_PORT" in environment) is (
        deployment.config.nodes_tls_mode is NodesTlsMode.PASSTHROUGH
    )
