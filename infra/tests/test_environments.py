"""Contextos de entorno, tabla ``pilot`` frente a ``staging-<n>`` (D-8) y nombres.

- El contexto se lee en forma cerrada: un valor inesperado detiene la síntesis.
- ``--context environment=staging-7`` sintetiza pilas con sufijo ``staging-7``, sin
  ``vigia-datasets``, y su configuración no tiene protección de borrado ni bloqueo de objetos.
- Las reglas de D-8 sobre la plantilla: ``staging`` destruible entero y con nombres propios;
  ``pilot`` con depósitos y base retenidos.
"""

from __future__ import annotations

from typing import Any

import pytest
from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_kms as kms
from aws_cdk import aws_rds as rds
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_wafv2 as wafv2

from config import (
    ContextError,
    EnvironmentConfig,
    NodesTlsMode,
    ObjectLock,
    ObjectLockMode,
    load_config,
)
from tests.conftest import Synthesized, default_context, probe
from tests.template_rules import (
    SECURITY_RULES,
    check_ephemeral_teardown,
    check_permanent_retention,
    describe,
)

ACCOUNT = "123456789012"


def _config(**context: Any) -> EnvironmentConfig:
    return load_config({**default_context(), **context})


# --- Lectura del contexto -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ephemeral"),
    [("pilot", False), ("staging-1", True), ("staging-7", True), ("staging-1234567", True)],
)
def test_valid_environments(value: str, ephemeral: bool) -> None:
    config = _config(environment=value)
    assert (config.environment, config.ephemeral) == (value, ephemeral)


@pytest.mark.parametrize(
    "value",
    [
        "staging",
        "staging-0",
        "staging-07",
        "staging--7",
        "staging-7a",
        "staging-1234567890",
        "Staging-7",
        "staging-7 ",
        "prod",
        "Pilot",
        "",
        7,
        True,
    ],
)
def test_invalid_environments_stop_the_synthesis(value: object) -> None:
    with pytest.raises(ContextError, match="'environment'"):
        _config(environment=value)


@pytest.mark.parametrize("value", ["shared", "acme", "acme-2", "a1", "abcdefghij01234"])
def test_valid_instances(value: str) -> None:
    assert _config(instance=value).instance == value


@pytest.mark.parametrize(
    "value",
    [
        "a",
        "abcdefghij01234567890",
        "Acme",
        "acme-",
        "-acme",
        "acme--x",
        "acme_x",
        "pilot",
        "staging-7",
        "stagingco",
        "",
        None,
        3,
    ],
)
def test_invalid_instances_stop_the_synthesis(value: object) -> None:
    error = "Falta el contexto" if value is None else "'instance'"
    with pytest.raises(ContextError, match=error):
        _config(instance=value)


@pytest.mark.parametrize(
    ("value", "expected"), [(True, True), (False, False), ("true", True), ("false", False)]
)
@pytest.mark.parametrize("key", ["first_deploy", "ca_rotation", "nat_per_az"])
def test_flags_accept_booleans_and_their_cli_form(key: str, value: object, expected: bool) -> None:
    assert getattr(_config(**{key: value}), key) is expected


@pytest.mark.parametrize("value", ["True", "yes", "1", 1, 0, ""])
@pytest.mark.parametrize("key", ["first_deploy", "ca_rotation", "nat_per_az"])
def test_flags_reject_other_values(key: str, value: object) -> None:
    with pytest.raises(ContextError, match=f"'{key}'"):
        _config(**{key: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("mtls", NodesTlsMode.MTLS), ("passthrough", NodesTlsMode.PASSTHROUGH)],
)
def test_nodes_tls_mode_accepts_its_two_values(value: str, expected: NodesTlsMode) -> None:
    assert _config(nodes_tls_mode=value).nodes_tls_mode is expected


def test_nodes_tls_mode_defaults_to_mutual_authentication() -> None:
    assert default_context()["nodes_tls_mode"] == "mtls"
    assert _config().nodes_tls_mode is NodesTlsMode.MTLS


@pytest.mark.parametrize(
    "value", ["MTLS", "mtls ", "pass-through", "tls", "nlb", "", True, 1, ["mtls"]]
)
def test_nodes_tls_mode_rejects_other_values(value: object) -> None:
    with pytest.raises(ContextError, match="'nodes_tls_mode'"):
        _config(nodes_tls_mode=value)


@pytest.mark.parametrize(
    "key",
    ["environment", "instance", "first_deploy", "ca_rotation", "nat_per_az", "nodes_tls_mode"],
)
def test_missing_context_stops_the_synthesis(key: str) -> None:
    context = {k: v for k, v in default_context().items() if k != key}
    with pytest.raises(ContextError, match=f"Falta el contexto '{key}'"):
        load_config(context)


def test_elevated_bootstrap_follows_either_flag() -> None:
    assert not _config().elevated_bootstrap
    assert _config(first_deploy="true").elevated_bootstrap
    assert _config(ca_rotation="true").elevated_bootstrap


# --- Tabla pilot frente a staging-<n> (D-8) --------------------------------------------


def test_pilot_column_of_the_d8_table() -> None:
    config = _config()
    assert config.evidence_object_lock == ObjectLock(ObjectLockMode.GOVERNANCE, 365)
    assert config.archive_object_lock == ObjectLock(ObjectLockMode.COMPLIANCE, 3653)
    assert config.buckets_retained and not config.buckets_auto_delete_objects
    assert not config.access_logs_bucket_owned
    assert (config.db_instance_class, config.db_multi_az) == ("db.t4g.medium", True)
    assert config.db_deletion_protection and config.db_final_snapshot
    assert config.db_backup_retention_days == 35
    assert not config.node_ca_per_run
    assert (config.hosted_zone_owned, config.hosted_zone_source) == (True, None)
    assert config.waf_enabled and config.include_datasets
    assert (config.api_min_tasks, config.api_max_tasks) == (2, 6)
    assert (config.worker_min_tasks, config.worker_max_tasks) == (1, 3)


def test_staging_column_of_the_d8_table() -> None:
    config = _config(environment="staging-7")
    assert config.evidence_object_lock is None and config.archive_object_lock is None
    assert not config.buckets_retained and config.buckets_auto_delete_objects
    assert config.access_logs_bucket_owned
    assert config.db_instance_class == "db.t4g.small"
    assert not config.db_deletion_protection and not config.db_final_snapshot
    assert config.db_backup_retention_days == 1
    assert config.node_ca_per_run and config.node_ca_pending_window_days == 7
    assert (config.hosted_zone_owned, config.hosted_zone_source) == (False, "pilot")
    assert not config.waf_enabled and not config.include_datasets
    assert (config.api_min_tasks, config.worker_min_tasks) == (2, 1)


def test_dedicated_instance_has_its_own_logs_and_no_datasets() -> None:
    config = _config(instance="acme")
    assert config.access_logs_bucket_owned and not config.include_datasets
    assert config.buckets_retained and config.db_deletion_protection
    staging = _config(instance="acme", environment="staging-7")
    assert staging.hosted_zone_source == "acme"


# --- Nombres por despliegue ------------------------------------------------------------


@pytest.mark.parametrize(
    ("context", "deployment", "stack", "bucket", "role"),
    [
        ({}, "pilot", "vigia-data", f"vigia-evidence-{ACCOUNT}-us-east-1", "vigia-worker-task"),
        (
            {"environment": "staging-7"},
            "staging-7",
            "vigia-data-staging-7",
            f"vigia-evidence-staging-7-{ACCOUNT}-us-east-1",
            "vigia-worker-task-staging-7",
        ),
        (
            {"instance": "acme"},
            "acme",
            "vigia-data-acme",
            f"vigia-evidence-acme-{ACCOUNT}-us-east-1",
            "vigia-worker-task-acme",
        ),
        (
            {"instance": "acme", "environment": "staging-7"},
            "acme-staging-7",
            "vigia-data-acme-staging-7",
            f"vigia-evidence-acme-staging-7-{ACCOUNT}-us-east-1",
            "vigia-worker-task-acme-staging-7",
        ),
    ],
)
def test_names_carry_the_deployment_suffix(
    context: dict[str, str], deployment: str, stack: str, bucket: str, role: str
) -> None:
    config = _config(**context)
    assert config.deployment == deployment
    assert config.stack_name("data") == stack
    assert config.bucket_name("evidence", ACCOUNT) == bucket
    assert config.resource_name("worker-task") == role


@pytest.mark.parametrize(
    "context",
    [
        {"instance": "abcdefghij01234"},  # vigia-node-trust-abcdefghij01234: 32 exactos
        {"environment": "staging-1234567"},
        {"instance": "ab", "environment": "staging-1234"},
        {"instance": "abcde", "environment": "staging-7"},
    ],
)
def test_longest_accepted_names_fit_their_service_limits(context: dict[str, str]) -> None:
    config = _config(**context)
    assert len(config.bucket_name("evidence", ACCOUNT)) <= 63
    assert len(config.resource_name("task-execution")) <= 64
    assert len(config.stack_name("observability")) <= 128
    # Balanceadores, grupos de destino y almacén de confianza de vigia-edge (§4).
    assert len(config.resource_name("node-trust")) == 32


@pytest.mark.parametrize(
    "context",
    [
        {"instance": "abcdefghij012345"},
        {"instance": "abcdefghij0123456789"},
        {"environment": "staging-12345678"},
        {"environment": "staging-123456789"},
        {"instance": "abcdef", "environment": "staging-7"},
    ],
)
def test_deployments_whose_load_balancing_names_overflow_stop_the_synthesis(
    context: dict[str, str],
) -> None:
    """``vigia-node-trust`` con el sufijo del despliegue no cabe en los 32 caracteres."""
    with pytest.raises(ContextError, match="el máximo es 32"):
        _config(**context)


@pytest.mark.parametrize(
    "context",
    [
        {"instance": "abcdefgh", "environment": "staging-123456789"},
        {"instance": "abcdefghij0123456789", "environment": "staging-12345"},
    ],
)
def test_deployments_whose_bucket_names_overflow_stop_the_synthesis(
    context: dict[str, str],
) -> None:
    with pytest.raises(ContextError, match="el máximo es 63"):
        _config(**context)


def test_staging_7_synthesizes_suffixed_stacks_without_datasets(staging: Synthesized) -> None:
    assert staging.stack_names == tuple(
        f"vigia-{key}-staging-7"
        for key in ("foundation", "data", "edge", "compute", "observability")
    )
    assert not any("datasets" in name for name in staging.stack_names)


def test_pilot_keeps_the_design_names(pilot: Synthesized) -> None:
    assert "vigia-datasets" in pilot.stack_names
    assert all(not name.endswith("-pilot") for name in pilot.stack_names)


# --- Reglas de D-8 sobre la plantilla -------------------------------------------------


def test_staging_stacks_are_fully_destroyable(staging: Synthesized) -> None:
    violations = [
        v
        for name, template in staging.templates.items()
        for v in check_ephemeral_teardown(name, template, deployment="staging-7")
    ]
    assert not violations, describe(violations)


def test_permanent_stacks_retain_buckets_and_database(permanent_deployment: Synthesized) -> None:
    violations = [
        v
        for name, template in permanent_deployment.templates.items()
        for v in check_permanent_retention(name, template)
    ]
    assert not violations, describe(violations)


def _bucket_from_config(stack: Stack, config: EnvironmentConfig, usage: str) -> s3.Bucket:
    """Depósito como lo construiría TASK-146 a partir de la configuración."""
    lock = config.evidence_object_lock
    return s3.Bucket(
        stack,
        f"{usage.capitalize()}Bucket",
        bucket_name=config.bucket_name(usage, stack.account),
        encryption_key=kms.Key(
            stack,
            f"{usage.capitalize()}Key",
            removal_policy=(
                RemovalPolicy.RETAIN if config.buckets_retained else RemovalPolicy.DESTROY
            ),
        ),
        object_lock_enabled=lock is not None,
        removal_policy=RemovalPolicy.RETAIN if config.buckets_retained else RemovalPolicy.DESTROY,
        auto_delete_objects=config.buckets_auto_delete_objects,
    )


def test_a_staging_bucket_built_from_the_config_passes_every_rule() -> None:
    config = _config(environment="staging-7")
    template = probe(lambda stack: _bucket_from_config(stack, config, "evidence"))
    assert check_ephemeral_teardown("vigia-probe", template, deployment="staging-7") == []
    violations = [
        v
        for rule in SECURITY_RULES
        for v in rule("vigia-probe", template)
        if "kms:*" not in v.detail  # política de clave por defecto: la escribe TASK-145
    ]
    assert violations == [], describe(violations)


def test_a_pilot_bucket_built_from_the_config_is_retained() -> None:
    config = _config()
    template = probe(lambda stack: _bucket_from_config(stack, config, "evidence"))
    assert check_permanent_retention("vigia-probe", template) == []


def test_staging_rejects_locks_protection_retention_zone_waf_and_foreign_names() -> None:
    def build(stack: Stack) -> None:
        s3.Bucket(
            stack,
            "Locked",
            bucket_name=f"vigia-evidence-{stack.account}-us-east-1",
            object_lock_enabled=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        rds.CfnDBInstance(
            stack,
            "Db",
            engine="postgres",
            db_instance_class="db.t4g.small",
            db_instance_identifier="vigia-staging-7-db",
            deletion_protection=True,
        )
        route53.HostedZone(stack, "Zone", zone_name="vigia.example")
        wafv2.CfnWebACL(
            stack,
            "Waf",
            name="vigia-app-waf-staging-7",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=False,
                metric_name="vigia",
                sampled_requests_enabled=False,
            ),
        )

    violations = check_ephemeral_teardown("vigia-probe", probe(build), deployment="staging-7")
    details = {(v.logical_id.rstrip("0123456789ABCDEF"), v.detail) for v in violations}
    assert details == {
        ("Locked", "DeletionPolicy Retain en un entorno efímero"),
        ("Locked", "UpdateReplacePolicy Retain en un entorno efímero"),
        ("Locked", "depósito con bloqueo de objetos en staging"),
        ("Locked", "depósito sin vaciado automático en staging"),
        ("Locked", "nombre físico sin el sufijo 'staging-7'"),
        ("Db", "base con protección contra borrado en staging"),
        ("Zone", "zona DNS creada en staging (se importa por atributos)"),
        ("Zone", "nombre físico sin el sufijo 'staging-7'"),
        ("Waf", "cortafuegos de aplicación en staging"),
    }, describe(violations)


def test_pilot_rejects_destroyable_buckets_and_unprotected_database() -> None:
    def build(stack: Stack) -> None:
        s3.Bucket(
            stack,
            "Evidence",
            bucket_name=f"vigia-evidence-{stack.account}-us-east-1",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        s3.Bucket(
            stack,
            "Drill",
            bucket_name=f"vigia-drill-{stack.account}-us-east-1",
            removal_policy=RemovalPolicy.DESTROY,
        )
        rds.CfnDBInstance(
            stack, "Db", engine="postgres", db_instance_class="db.t4g.medium", multi_az=True
        )

    violations = check_permanent_retention("vigia-probe", probe(build))
    details = sorted((v.logical_id.rstrip("0123456789ABCDEF"), v.detail) for v in violations)
    assert details == [
        ("Db", "base sin protección contra borrado"),
        ("Db", "base sin retención ni instantánea final"),
        ("Evidence", "depósito con vaciado automático"),
        ("Evidence", "depósito sin DeletionPolicy Retain"),
    ], describe(violations)
