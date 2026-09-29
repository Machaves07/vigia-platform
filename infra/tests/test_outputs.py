"""Contrato de salidas entre pilas (pendiente nº 38, ``stacks/outputs.py``).

- ``roleName`` fijos y parámetros ``/vigia/<despliegue>/<recurso>`` para el rol de tarea del
  worker, ``sg-worker`` y la zona DNS.
- Una pila de otra unidad (como ``vigia-loop`` de U-04) se añade a la aplicación y lee esos
  valores sin cambiar ninguna plantilla de U-02, sin ``Fn::ImportValue`` y sin búsquedas.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aws_cdk import App, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_route53 as route53

from config import EnvironmentConfig, load_config
from stacks.base import VigiaStack
from stacks.outputs import (
    Output,
    TaskRole,
    import_hosted_zone,
    import_value,
    import_worker_security_group,
    import_worker_task_role,
    parameter_name,
    publish,
    role_name,
)
from tests.conftest import Synthesized, default_context, probe, synthesize
from tests.template_rules import resources


def _config(**context: Any) -> EnvironmentConfig:
    return load_config({**default_context(), **context})


def test_parameter_paths_follow_the_contract() -> None:
    assert {output: parameter_name("pilot", output) for output in Output} == {
        Output.WORKER_TASK_ROLE_ARN: "/vigia/pilot/worker-task-role-arn",
        Output.WORKER_SECURITY_GROUP_ID: "/vigia/pilot/sg-worker-id",
        Output.HOSTED_ZONE_ID: "/vigia/pilot/zone-id",
        Output.HOSTED_ZONE_NAME: "/vigia/pilot/zone-name",
    }
    assert parameter_name("staging-7", Output.HOSTED_ZONE_ID) == "/vigia/staging-7/zone-id"


@pytest.mark.parametrize("deployment", ["", "Pilot", "pilot/x", "pilot ", "../pilot"])
def test_parameter_paths_reject_other_deployments(deployment: str) -> None:
    with pytest.raises(ValueError, match="fuera del contrato"):
        parameter_name(deployment, Output.HOSTED_ZONE_ID)


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({}, "vigia-worker-task"),
        ({"environment": "staging-7"}, "vigia-worker-task-staging-7"),
        ({"instance": "acme"}, "vigia-worker-task-acme"),
    ],
)
def test_worker_role_name_is_fixed_per_deployment(context: dict[str, str], expected: str) -> None:
    assert role_name(_config(**context), TaskRole.WORKER_TASK) == expected


def test_every_task_role_of_section_8_has_a_fixed_name() -> None:
    config = _config()
    assert [role_name(config, role) for role in TaskRole] == [
        "vigia-task-execution",
        "vigia-api-task",
        "vigia-worker-task",
        "vigia-migrate-task",
        "vigia-admin-task",
    ]


def test_publish_creates_a_standard_string_parameter() -> None:
    config = _config(environment="staging-7")

    def build(stack: Stack) -> None:
        publish(stack, config, Output.WORKER_SECURITY_GROUP_ID, "sg-0123456789abcdef0")

    parameters = [r for _, r in resources(probe(build)) if r["Type"] == "AWS::SSM::Parameter"]
    assert len(parameters) == 1
    props = parameters[0]["Properties"]
    assert props["Name"] == "/vigia/staging-7/sg-worker-id"
    assert props["Type"] == "String"
    assert props["Value"] == "sg-0123456789abcdef0"
    assert props["Tier"] == "Standard"


def test_import_value_is_resolved_at_deploy_time_not_at_synthesis() -> None:
    config = _config()
    template = probe(lambda stack: import_value(stack, config, Output.WORKER_SECURITY_GROUP_ID))
    parameters = template["Parameters"]
    ssm = [p for p in parameters.values() if p["Type"] == "AWS::SSM::Parameter::Value<String>"]
    assert {p["Default"] for p in ssm} >= {"/vigia/pilot/sg-worker-id"}


def test_staging_imports_the_zone_published_by_pilot() -> None:
    config = _config(environment="staging-7")
    template = probe(lambda stack: import_hosted_zone(stack, "Zone", config))
    defaults = {p["Default"] for p in template["Parameters"].values()}
    assert {"/vigia/pilot/zone-id", "/vigia/pilot/zone-name"} <= defaults
    assert not any("staging-7" in d for d in defaults)


def test_pilot_imports_its_own_zone() -> None:
    template = probe(lambda stack: import_hosted_zone(stack, "Zone", _config()))
    defaults = {p["Default"] for p in template["Parameters"].values()}
    assert {"/vigia/pilot/zone-id", "/vigia/pilot/zone-name"} <= defaults


class _LoopStack(VigiaStack):
    """Imitación de ``vigia-loop`` (U-04): solo usa el contrato para leer lo de U-02."""

    key = "loop"
    summary = "pila de prueba del contrato de salidas"

    def __init__(self, scope: App, config: EnvironmentConfig) -> None:
        super().__init__(scope, config, tags={"unit": "U-04"})
        role = import_worker_task_role(self, "WorkerTaskRole", config, mutable=True)
        worker = import_worker_security_group(self, "SgWorker", config)
        zone = import_hosted_zone(self, "Zone", config)
        policy = iam.ManagedPolicy(
            self,
            "LoopWorker",
            managed_policy_name=config.resource_name("loop-worker"),
            statements=[
                iam.PolicyStatement(
                    actions=["sqs:SendMessage"],
                    resources=[f"arn:aws:sqs:{self.region}:{self.account}:vigia-loop"],
                )
            ],
        )
        role.add_managed_policy(policy)
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=0)
        endpoint = ec2.SecurityGroup(self, "SgQueueEndpoint", vpc=vpc, allow_all_outbound=False)
        endpoint.add_ingress_rule(worker, ec2.Port.tcp(443))
        route53.TxtRecord(self, "Probe", zone=zone, record_name="loop", values=["vigia"])


def _add_loop(application: App, config: EnvironmentConfig) -> None:
    _LoopStack(application, config)


@pytest.mark.parametrize("environment", ["pilot", "staging-7"])
def test_another_unit_reads_the_contract_without_touching_u02(environment: str) -> None:
    baseline: Synthesized = synthesize(environment=environment)
    extended = synthesize(_add_loop, environment=environment)
    config = extended.config
    loop_name = config.stack_name("loop")

    assert extended.stack_names == (*baseline.stack_names, loop_name)
    for name in baseline.stack_names:
        assert json.dumps(extended.templates[name], sort_keys=True) == json.dumps(
            baseline.templates[name], sort_keys=True
        ), f"{name} cambió al añadir la pila de otra unidad"

    loop = extended.templates[loop_name]
    text = json.dumps(loop)
    assert "Fn::ImportValue" not in text
    assert extended.missing_context == ()
    deployment = config.deployment
    zone_source = "pilot" if config.ephemeral else deployment
    defaults = {p["Default"] for p in loop["Parameters"].values()}
    assert {
        f"/vigia/{deployment}/sg-worker-id",
        f"/vigia/{zone_source}/zone-id",
        f"/vigia/{zone_source}/zone-name",
    } <= defaults
    attachments = [r for _, r in resources(loop) if r["Type"] == "AWS::IAM::ManagedPolicy"]
    assert [r["Properties"]["Roles"] for r in attachments] == [
        [role_name(config, TaskRole.WORKER_TASK)]
    ]
    assert extended.dependencies[loop_name] == ()
