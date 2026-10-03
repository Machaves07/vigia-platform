"""``vigia-deploy`` y ``vigia-deploy-staging-<n>`` para los flujos de vigia-platform (TASK-151).

- El rol de la cuenta (``pilot``) confía solo en los entornos ``pilot`` y ``staging`` de
  ``Machaves07/vigia-platform`` (A-40, A-47), sin claves de acceso (SECURITY-10), y no lanza
  ``vigia-admin``: en ``pilot`` las órdenes administrativas son del dueño (§5).
- Concede la lectura que necesitan los flujos (salidas de las pilas, comprobaciones nº 2 y 7,
  barrido y residuos de ``staging-<n>``) y las revocaciones de ``vigia-node-trust`` para
  ``trust-store.yml`` (nº 19).
- ``staging-<n>`` crea ``vigia-deploy-staging-<n>``, adjunta al ``vigia-deploy`` importado, con
  ``vigia-migrate`` y ``vigia-admin`` de ese ``staging`` y la invitación sintética del arranque
  (seguimiento de VIG-48 en VIG-95), nada de otro despliegue.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any

from stacks.compute import PIPELINE_READ_ACTIONS, TRUST_STORE_ACTIONS
from tests.conftest import Synthesized, synthesize
from tests.template_rules import _as_list, properties, render

JsonObject = Mapping[str, Any]
POLICY = "AWS::IAM::ManagedPolicy"
ROLE = "AWS::IAM::Role"


@cache
def _synth(**context: str) -> Synthesized:
    return synthesize(None, **context)


def _compute(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("compute")]


def _policy(deployment: Synthesized, name: str) -> tuple[str, JsonObject]:
    template = _compute(deployment)
    (found,) = [
        (logical_id, resource)
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == POLICY
        and render(properties(resource)["ManagedPolicyName"], template) == name
    ]
    return found


def _statements(deployment: Synthesized, name: str) -> dict[str, JsonObject]:
    _, policy = _policy(deployment, name)
    return {s["Sid"]: s for s in _as_list(properties(policy)["PolicyDocument"]["Statement"])}


def _resources(deployment: Synthesized, statement: JsonObject) -> list[str]:
    template = _compute(deployment)
    return sorted(render(r, template) for r in _as_list(statement["Resource"]))


def _task_definition_ids(deployment: Synthesized, *families: str) -> list[str]:
    template = _compute(deployment)
    expected = {deployment.config.resource_name(family) for family in families}
    return sorted(
        f"<Ref.{logical_id}>"
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::ECS::TaskDefinition"
        and properties(resource)["Family"] in expected
    )


def _role_ids(deployment: Synthesized, *roles: str) -> list[str]:
    template = _compute(deployment)
    names = {deployment.config.resource_name(role) for role in roles}
    return sorted(
        f"<GetAtt.{logical_id}.Arn>"
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == ROLE and render(properties(resource)["RoleName"], template) in names
    )


# --- Rol de la cuenta ---------------------------------------------------------------------


def test_the_account_role_trusts_only_the_two_github_environments() -> None:
    deployment = _synth()
    template = _compute(deployment)
    (role,) = [
        r
        for r in template["Resources"].values()
        if r["Type"] == ROLE and properties(r)["RoleName"] == "vigia-deploy"
    ]
    (statement,) = properties(role)["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    condition = statement["Condition"]["StringEquals"]
    assert condition["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert sorted(condition["token.actions.githubusercontent.com:sub"]) == [
        "repo:Machaves07/vigia-platform:environment:pilot",
        "repo:Machaves07/vigia-platform:environment:staging",
    ]


def test_no_deployment_creates_long_lived_access_keys() -> None:
    for context in ({}, {"environment": "staging-7"}, {"first_deploy": "true"}):
        for template in _synth(**context).templates.values():
            types = {r["Type"] for r in template.get("Resources", {}).values()}
            assert "AWS::IAM::AccessKey" not in types
            assert "AWS::IAM::User" not in types


def test_the_account_role_runs_only_the_migration_in_pilot() -> None:
    deployment = _synth()
    statements = _statements(deployment, "vigia-deploy")
    assert _resources(deployment, statements["RunMigration"]) == _task_definition_ids(
        deployment, "migrate"
    )
    passed = _resources(deployment, statements["PassTaskRoles"])
    assert passed == _role_ids(
        deployment, "task-execution", "api-task", "worker-task", "migrate-task"
    )


def test_the_account_role_reads_what_the_workflows_need() -> None:
    deployment = _synth()
    statements = _statements(deployment, "vigia-deploy")
    assert statements["ReadStacks"]["Action"] == "cloudformation:DescribeStacks"
    assert _resources(deployment, statements["ReadStacks"]) == [
        "arn:aws:cloudformation:us-east-1:<AccountId>:stack/vigia-*"
    ]
    read = statements["ReadDeploymentState"]
    assert sorted(_as_list(read["Action"])) == sorted(PIPELINE_READ_ACTIONS)
    assert read["Resource"] == "*"
    keys = statements["InspectStagingKeys"]
    assert keys["Action"] == "kms:DescribeKey"
    assert keys["Condition"] == {"StringLike": {"aws:ResourceTag/environment": "staging-*"}}
    assert sorted(_as_list(statements["ReadEdgeCa"]["Action"])) == [
        "s3:GetObject",
        "s3:GetObjectVersion",
    ]


def test_the_account_role_manages_the_revocations_of_vigia_node_trust() -> None:
    deployment = _synth()
    statements = _statements(deployment, "vigia-deploy")
    update = statements["UpdateTrustStore"]
    assert sorted(_as_list(update["Action"])) == sorted(TRUST_STORE_ACTIONS)
    (resource,) = _as_list(update["Resource"])
    assert "TrustStore" in str(resource)


def test_trust_store_arn_is_an_output_only_when_the_store_exists() -> None:
    assert "NodeTrustStoreArn" in _compute(_synth())["Outputs"]
    assert "NodeTrustStoreArn" not in _compute(_synth(first_deploy="true"))["Outputs"]


# --- staging-<n> ------------------------------------------------------------------------


def test_staging_attaches_its_own_policy_to_the_imported_account_role() -> None:
    deployment = _synth(environment="staging-7")
    _, policy = _policy(deployment, "vigia-deploy-staging-7")
    assert properties(policy)["Roles"] == ["vigia-deploy"]
    template = _compute(deployment)
    assert not [
        r
        for r in template["Resources"].values()
        if r["Type"] == ROLE and "deploy" in str(properties(r).get("RoleName"))
    ]


def test_staging_policy_runs_migrate_and_admin_of_that_staging_only() -> None:
    for context in (
        {"environment": "staging-7"},
        {"environment": "staging-7", "first_deploy": "true"},
    ):
        deployment = _synth(**context)
        statements = _statements(deployment, "vigia-deploy-staging-7")
        assert _resources(deployment, statements["RunMigration"]) == _task_definition_ids(
            deployment, "migrate", "admin"
        )
        assert _resources(deployment, statements["PassTaskRoles"]) == _role_ids(
            deployment, "task-execution", "api-task", "worker-task", "migrate-task", "admin-task"
        )
        watched = _resources(deployment, statements["WatchMigration"])
        assert watched == ["arn:aws:ecs:us-east-1:<AccountId>:task/vigia-staging-7/*"]


def test_staging_policy_reads_only_its_bootstrap_invitation() -> None:
    deployment = _synth(environment="staging-7")
    statements = _statements(deployment, "vigia-deploy-staging-7")
    assert _resources(deployment, statements["ReadBootstrapInvitation"]) == [
        "arn:aws:secretsmanager:us-east-1:<AccountId>:secret:vigia/staging-7/bootstrap/invitation-??????"
    ]
    key = statements["BootstrapInvitationKey"]
    assert key["Condition"] == {
        "StringEquals": {"kms:ViaService": "secretsmanager.us-east-1.amazonaws.com"}
    }


def test_staging_policy_has_no_wildcard_resource_nor_registry_push() -> None:
    deployment = _synth(environment="staging-7")
    statements = _statements(deployment, "vigia-deploy-staging-7")
    for statement in statements.values():
        assert "*" not in _resources(deployment, statement)
        assert not [a for a in _as_list(statement["Action"]) if a.startswith("ecr:")]


def test_pilot_has_no_staging_policy() -> None:
    template = _compute(_synth())
    names = [
        render(properties(r)["ManagedPolicyName"], template)
        for r in template["Resources"].values()
        if r["Type"] == POLICY
    ]
    assert not [name for name in names if "staging" in name]
