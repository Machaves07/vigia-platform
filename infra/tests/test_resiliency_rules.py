"""RESILIENCY-08, 09 y 12 y NFR-NUC-31 sobre la síntesis (infrastructure-design §2.3).

- En los despliegues permanentes, ``vigia-api`` ≥ 2 tareas y ``vigia-worker`` ≥ 1; 0 solo con
  ``first_deploy=true`` (nota U02-H-06 de §2.3; deployment-architecture §5, pasos 4 y 8).
- Base con réplica en espera en otra zona en ``pilot`` (§6.1).
- Ningún recurso fuera de ``us-east-1`` y ninguna replicación (NFR-NUC-31, §11).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from aws_cdk import Stack
from aws_cdk import aws_applicationautoscaling as autoscaling
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_kms as kms
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager

from config import EnvironmentConfig, load_config
from tests.conftest import Synthesized, default_context, probe
from tests.template_rules import (
    MIN_TASKS,
    Violation,
    check_database_replica,
    check_no_replication,
    check_region,
    check_service_min_tasks,
    describe,
)


def _services(api: int | None, worker: int | None) -> Callable[[Stack], None]:
    def build(stack: Stack) -> None:
        ecs.CfnService(stack, "Api", service_name="vigia-api", desired_count=api)
        ecs.CfnService(stack, "Worker", service_name="vigia-worker", desired_count=worker)

    return build


def _tasks(api: int | None, worker: int | None, *, first_deploy: bool) -> list[Violation]:
    template = probe(_services(api, worker))
    return check_service_min_tasks("vigia-probe", template, first_deploy=first_deploy)


# --- Todas las pilas de todos los despliegues -----------------------------------------


def test_every_permanent_stack_meets_min_tasks_and_replica(
    permanent_deployment: Synthesized,
) -> None:
    deployment = permanent_deployment
    assert not deployment.config.ephemeral
    violations = [
        v
        for name, template in deployment.templates.items()
        for v in check_service_min_tasks(
            name, template, first_deploy=deployment.config.first_deploy
        )
        + check_database_replica(name, template)
    ]
    assert not violations, describe(violations)


def test_every_stack_stays_in_us_east_1_without_replication(deployment: Synthesized) -> None:
    violations = [
        v
        for name, template in deployment.templates.items()
        for v in check_region(name, template, stack_region=deployment.regions[name])
        + check_no_replication(name, template)
    ]
    assert not violations, describe(violations)


# --- Tareas mínimas y first_deploy ----------------------------------------------------


def test_minimums_are_those_of_the_design() -> None:
    assert MIN_TASKS == {"vigia-api": 2, "vigia-worker": 1}


@pytest.mark.parametrize(("api", "worker"), [(2, 1), (3, 2), (6, 3)])
@pytest.mark.parametrize("first_deploy", [False, True])
def test_services_at_or_above_the_minimum_pass(api: int, worker: int, first_deploy: bool) -> None:
    assert _tasks(api, worker, first_deploy=first_deploy) == []


def test_zero_tasks_are_rejected_without_first_deploy() -> None:
    violations = _tasks(0, 0, first_deploy=False)
    assert sorted(v.physical_name or "" for v in violations) == ["vigia-api", "vigia-worker"]
    assert all("se exige ≥" in v.detail for v in violations), describe(violations)


def test_zero_tasks_are_accepted_with_first_deploy() -> None:
    assert _tasks(0, 0, first_deploy=True) == []


@pytest.mark.parametrize("first_deploy", [False, True])
def test_one_api_task_is_rejected_even_with_first_deploy(first_deploy: bool) -> None:
    """``first_deploy`` admite 0, no un valor intermedio por debajo del mínimo."""
    violations = _tasks(1, 1, first_deploy=first_deploy)
    assert [(v.physical_name, v.detail.split(";")[0]) for v in violations] == [
        ("vigia-api", "DesiredCount = 1")
    ]


def test_undeclared_task_count_is_rejected() -> None:
    violations = _tasks(None, 1, first_deploy=True)
    assert [v.detail for v in violations] == ["DesiredCount no declarado explícitamente"]


def test_scaling_minimum_below_the_floor_is_rejected() -> None:
    def build(stack: Stack) -> None:
        _services(2, 1)(stack)
        api = stack.node.find_child("Api")
        assert isinstance(api, ecs.CfnService)
        autoscaling.CfnScalableTarget(
            stack,
            "ApiScaling",
            service_namespace="ecs",
            scalable_dimension="ecs:service:DesiredCount",
            resource_id=f"service/vigia-pilot/{api.attr_name}",
            min_capacity=1,
            max_capacity=6,
        )

    violations = check_service_min_tasks("vigia-probe", probe(build), first_deploy=False)
    assert [v.detail.split(";")[0] for v in violations] == ["MinCapacity del escalado = 1"]


def test_other_services_are_not_constrained() -> None:
    template = probe(
        lambda stack: ecs.CfnService(stack, "Other", service_name="vigia-other", desired_count=0)
    )
    assert check_service_min_tasks("vigia-probe", template, first_deploy=False) == []


@pytest.mark.parametrize("first_deploy", [False, True])
def test_configured_task_counts_satisfy_the_rule(first_deploy: bool) -> None:
    """Los servicios que TASK-148 cree con ``api_desired_tasks`` y ``worker_desired_tasks``
    cumplen la regla con cada valor de ``first_deploy``."""
    config: EnvironmentConfig = load_config(
        {**default_context(), "first_deploy": "true" if first_deploy else "false"}
    )
    expected = (0, 0) if first_deploy else (2, 1)
    assert (config.api_desired_tasks, config.worker_desired_tasks) == expected
    counts = (config.api_desired_tasks, config.worker_desired_tasks)
    assert _tasks(*counts, first_deploy=first_deploy) == []


# --- Réplica de la base ----------------------------------------------------------------


def _db(stack: Stack, logical_id: str, **overrides: Any) -> rds.CfnDBInstance:
    props: dict[str, Any] = {
        "engine": "postgres",
        "db_instance_class": "db.t4g.medium",
        "db_instance_identifier": "vigia-pilot-db",
        **overrides,
    }
    return rds.CfnDBInstance(stack, logical_id, **props)


def test_single_zone_database_fails_naming_the_resource() -> None:
    template = probe(lambda stack: _db(stack, "Db", multi_az=False))
    violations = check_database_replica("vigia-probe", template)
    assert [(v.physical_name, v.rule) for v in violations] == [("vigia-pilot-db", "RESILIENCY-08")]


def test_multi_az_database_passes() -> None:
    assert check_database_replica("p", probe(lambda stack: _db(stack, "Db", multi_az=True))) == []


def test_cluster_needs_two_instances() -> None:
    def build(members: int) -> Callable[[Stack], None]:
        def inner(stack: Stack) -> None:
            cluster = rds.CfnDBCluster(stack, "Cluster", engine="aurora-postgresql")
            for index in range(members):
                rds.CfnDBInstance(
                    stack,
                    f"Member{index}",
                    engine="aurora-postgresql",
                    db_instance_class="db.t4g.medium",
                    db_cluster_identifier=cluster.ref,
                )

        return inner

    assert [v.logical_id for v in check_database_replica("p", probe(build(1)))] == ["Cluster"]
    assert check_database_replica("p", probe(build(2))) == []


# --- Región única y sin replicación ----------------------------------------------------


def test_stack_outside_us_east_1_fails() -> None:
    violations = check_region("vigia-probe", {"Resources": {}}, stack_region="eu-west-1")
    assert [v.detail for v in violations] == ["pila en la región 'eu-west-1'; se exige us-east-1"]


def test_resource_naming_another_region_fails() -> None:
    def build(stack: Stack) -> None:
        s3.CfnBucket(stack, "Copy", bucket_name="vigia-copy-eu-west-1")
        s3.CfnBucket(stack, "Home", bucket_name="vigia-home-us-east-1")
        rds.CfnDBInstance(
            stack,
            "Db",
            engine="postgres",
            db_instance_class="db.t4g.medium",
            availability_zone="us-west-2a",
            multi_az=False,
        )

    violations = check_region("vigia-probe", probe(build), stack_region="us-east-1")
    assert sorted((v.logical_id, v.detail) for v in violations) == [
        ("Copy", "referencia a la región eu-west-1 fuera de us-east-1"),
        ("Db", "referencia a la región us-west-2 fuera de us-east-1"),
        ("Db", "zona de disponibilidad us-west-2a fuera de us-east-1"),
    ]


def test_replication_of_any_kind_fails() -> None:
    def build(stack: Stack) -> None:
        s3.CfnBucket(
            stack,
            "Replicated",
            replication_configuration=s3.CfnBucket.ReplicationConfigurationProperty(
                role="arn:aws:iam::123456789012:role/vigia-replication",
                rules=[
                    s3.CfnBucket.ReplicationRuleProperty(
                        status="Enabled",
                        destination=s3.CfnBucket.ReplicationDestinationProperty(
                            bucket="arn:aws:s3:::vigia-copy"
                        ),
                    )
                ],
            ),
        )
        kms.CfnKey(stack, "MultiRegionKey", multi_region=True, key_policy={})
        secretsmanager.CfnSecret(
            stack,
            "ReplicatedSecret",
            replica_regions=[secretsmanager.CfnSecret.ReplicaRegionProperty(region="us-west-2")],
        )
        _db(stack, "Replica", source_region="us-west-2")

    violations = check_no_replication("vigia-probe", probe(build))
    assert sorted(v.logical_id for v in violations) == [
        "MultiRegionKey",
        "Replica",
        "Replicated",
        "ReplicatedSecret",
    ]
    assert {v.rule for v in violations} == {"RESILIENCY-12"}


def test_single_region_resources_pass() -> None:
    def build(stack: Stack) -> None:
        key = kms.Key(stack, "Key")
        s3.Bucket(stack, "Evidence", encryption_key=key)
        _db(stack, "Db", multi_az=True)

    template = probe(build)
    assert check_no_replication("p", template) == []
    assert check_region("p", template, stack_region="us-east-1") == []
