"""Pila ``vigia-observability`` (TASK-149): grupos de registro, alarmas, tablero y comprobaciones.

Infrastructure-design §9 y §10 con las notas de 2026-09-23 (salud de ``nodes.`` y NFR-NUC-10 en
§4.3, ``restore-drill-overdue`` en §9.4) y los pendientes nº 12, 17, 18 y 23 aceptados en TASK-102.
Cada prueba lee la plantilla sintetizada. ``APPLICATION_ALARM_METRICS`` se contrasta aquí con la
síntesis y en ``backend/tests/unit/test_alarm_metrics_crosscheck.py`` con ``observability.metrics``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import Stack
from aws_cdk import aws_iam as iam

from config import EnvironmentConfig, NodesTlsMode
from stacks.compute import COLLECTOR_CONFIG, cluster_name
from stacks.data import db_identifier
from stacks.observability import (
    APPLICATION_ALARM_METRICS,
    LOG_PROCESSES,
    NFR_NUC_38_ALARMS,
    NODE_ROUTE_P95_TARGETS_MS,
    OPERATION_P95_TARGETS_MS,
    PERIODIC_TASK_MAX_AGE_SECONDS,
    USAGE_QUOTAS,
    dashboard_name,
)
from tests.conftest import INFRA, Synthesized, probe
from tests.template_rules import properties, render, resources

JsonObject = Mapping[str, Any]
ALARM = "AWS::CloudWatch::Alarm"
NAMESPACE = "Vigia/Platform"
FIXED_DIMENSIONS = frozenset({"service", "environment"})
COLLECTOR = INFRA / "otel" / "collector.yaml"
DELETE_LOG_ACTIONS = ("logs:DeleteLogGroup", "logs:DeleteLogStream")


def _service_alarm_keys(config: EnvironmentConfig) -> set[str]:
    keys = {
        "server-error-rate-app",
        "single-zone-api",
        "availability-internal-probe",
        "worker-absent",
        "api-memory-high",
        "rds-storage-low",
        "rds-connections-high",
        "rds-cpu-high",
        "health-check-failed-app",
        "health-check-failed-nodes",
        "vigia-app-index-unhealthy",
        "quota-evidence-reads",
        *USAGE_QUOTAS,
    }
    if config.nodes_tls_mode is NodesTlsMode.MTLS:
        keys.add("server-error-rate-nodes")
    keys |= (
        {"nat-a-degraded", "nat-b-degraded"} if config.nat_per_az else {"nat-single-az-degraded"}
    )
    if config.waf_enabled:
        keys.add("waf-blocked-spike")
    return keys


def expected_alarm_keys(config: EnvironmentConfig) -> set[str]:
    return set(APPLICATION_ALARM_METRICS) | _service_alarm_keys(config)


def expected_observability_resources(config: EnvironmentConfig) -> Counter[str]:
    """Recursos de ``vigia-observability`` por despliegue (sin ``AWS::CDK::Metadata``)."""
    return Counter(
        {
            "AWS::Logs::LogGroup": len(LOG_PROCESSES),
            ALARM: len(expected_alarm_keys(config)),
            "AWS::Route53::HealthCheck": 3,
            "AWS::Events::Rule": 1,
            "AWS::CloudWatch::Dashboard": 1,
        }
    )


# --- Lectura de la plantilla ---------------------------------------------------------------


def _template(deployment: Synthesized, key: str = "observability") -> dict[str, Any]:
    return dict(deployment.templates[deployment.config.stack_name(key)])


def _of_type(template: JsonObject, kind: str) -> list[tuple[str, JsonObject]]:
    return [(logical_id, r) for logical_id, r in resources(template) if r["Type"] == kind]


def _key(config: EnvironmentConfig, name: str) -> str:
    """``vigia-<clave>[<sufijo>]`` → ``<clave>``."""
    assert name.startswith("vigia-") and name.endswith(config.name_suffix), name
    return name.removeprefix("vigia-")[: len(name) - len("vigia-") - len(config.name_suffix)]


def _alarms(deployment: Synthesized) -> dict[str, JsonObject]:
    """Propiedades de cada alarma por su clave."""
    template = _template(deployment)
    found = {
        _key(deployment.config, str(properties(r)["AlarmName"])): properties(r)
        for _, r in _of_type(template, ALARM)
    }
    assert len(found) == len(_of_type(template, ALARM)), "nombres de alarma repetidos"
    return found


def _metric_stats(alarm: JsonObject) -> Iterator[JsonObject]:
    """``{Namespace, MetricName, Dimensions, Stat, Period}`` de cada métrica de la alarma."""
    if "MetricName" in alarm:
        yield {
            "Namespace": alarm["Namespace"],
            "MetricName": alarm["MetricName"],
            "Dimensions": alarm.get("Dimensions", []),
            "Stat": alarm.get("Statistic") or alarm.get("ExtendedStatistic"),
            "Period": alarm["Period"],
        }
        return
    for query in alarm.get("Metrics", []):
        if "MetricStat" in query:
            stat = query["MetricStat"]
            yield {**stat["Metric"], "Stat": stat["Stat"], "Period": stat["Period"]}


def _dimensions(metric: JsonObject) -> dict[str, Any]:
    return {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}


def _expression(alarm: JsonObject) -> str:
    (expression,) = [q["Expression"] for q in alarm["Metrics"] if q.get("ReturnData", True)]
    return str(expression)


def _application_metrics(alarm: JsonObject) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Métricas de ``Vigia/Platform`` y sus atributos, como en ``APPLICATION_ALARM_METRICS``."""
    pairs = {
        (
            str(metric["MetricName"]),
            tuple(sorted(set(_dimensions(metric)) - FIXED_DIMENSIONS)),
        )
        for metric in _metric_stats(alarm)
        if metric["Namespace"] == NAMESPACE
    }
    return tuple(sorted(pairs))


def _collector_declarations() -> list[tuple[list[frozenset[str]], list[str]]]:
    """``metric_declarations`` de ``awsemf``: conjuntos de dimensiones y selectores."""
    declarations: list[tuple[list[frozenset[str]], list[str]]] = []
    section = None
    inside = False
    for line in COLLECTOR.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "metric_declarations:":
            inside = True
            continue
        if not inside or stripped.startswith("#") or not stripped:
            continue
        if not line.startswith("      "):
            break
        if stripped == "- dimensions:":
            declarations.append(([], []))
            section = "dimensions"
        elif stripped == "metric_name_selectors:":
            section = "selectors"
        elif section == "dimensions" and (match := re.fullmatch(r"- \[(.*)\]", stripped)):
            declarations[-1][0].append(frozenset(p.strip() for p in match.group(1).split(",")))
        elif section == "selectors" and (match := re.fullmatch(r'- "(.*)"', stripped)):
            declarations[-1][1].append(match.group(1))
    assert declarations, "collector.yaml sin metric_declarations"
    return declarations


# --- Registros (§9.2) ----------------------------------------------------------------------


def test_application_log_groups_keep_180_days_encrypted_with_vigia_logs(
    deployment: Synthesized,
) -> None:
    config = deployment.config
    template = _template(deployment)
    groups = {
        properties(r)["LogGroupName"]: r for _, r in _of_type(template, "AWS::Logs::LogGroup")
    }
    assert set(groups) == {f"/vigia/{config.deployment}/{p}" for p in LOG_PROCESSES}
    for name, group in groups.items():
        props = properties(group)
        assert props["RetentionInDays"] == 180, name
        key = props["KmsKeyId"]["Fn::GetStackOutput"]
        assert key["StackName"] == config.stack_name("foundation"), name
        assert "Keylogs" in key["OutputName"], name
        expected = "Delete" if config.ephemeral else "Retain"
        assert group["DeletionPolicy"] == expected, name


def test_network_and_firewall_log_groups_keep_90_days_encrypted(deployment: Synthesized) -> None:
    config = deployment.config
    found: dict[str, JsonObject] = {}
    for key in ("foundation", "edge"):
        for _, group in _of_type(_template(deployment, key), "AWS::Logs::LogGroup"):
            found[str(properties(group)["LogGroupName"])] = properties(group)
    expected = {f"/vigia/{config.deployment}/vpc-flow"}
    if config.waf_enabled:
        expected.add(f"aws-waf-logs-{config.resource_name('app')}")
    assert set(found) == expected
    for name, props in found.items():
        assert props["RetentionInDays"] == 90, name
        assert props.get("KmsKeyId"), name


def test_compute_tasks_write_to_the_groups_this_stack_creates(deployment: Synthesized) -> None:
    """``awslogs`` no arranca una tarea sin su grupo: ``vigia-compute`` depende de esta pila."""
    config = deployment.config
    compute = deployment.config.stack_name("compute")
    assert config.stack_name("observability") in deployment.dependencies[compute]
    groups = {
        str(properties(r)["LogGroupName"])
        for _, r in _of_type(_template(deployment), "AWS::Logs::LogGroup")
    }
    used = {
        str(container["LogConfiguration"]["Options"]["awslogs-group"])
        for _, definition in _of_type(_template(deployment, "compute"), "AWS::ECS::TaskDefinition")
        for container in properties(definition)["ContainerDefinitions"]
    }
    assert used and used <= groups


def _allowed_actions(statement: JsonObject) -> list[str]:
    if statement.get("Effect") != "Allow":
        return []
    actions = statement.get("Action", [])
    return [actions] if isinstance(actions, str) else list(actions)


def _policy_documents(template: JsonObject) -> Iterator[tuple[str, JsonObject]]:
    for logical_id, resource in resources(template):
        props = properties(resource)
        kind = resource["Type"]
        if kind in ("AWS::IAM::ManagedPolicy", "AWS::IAM::Policy"):
            yield logical_id, props["PolicyDocument"]
        elif kind == "AWS::IAM::Role":
            for policy in props.get("Policies", []):
                yield logical_id, policy["PolicyDocument"]


def delete_log_permissions(template: JsonObject) -> list[str]:
    """Políticas de identidad que conceden borrar grupos o flujos de registro, también por
    comodín (``logs:*``, ``logs:Delete*``, ``*``)."""
    found: list[str] = []
    for logical_id, document in _policy_documents(template):
        statements = document.get("Statement", [])
        for statement in [statements] if isinstance(statements, Mapping) else statements:
            for action in _allowed_actions(statement):
                if any(
                    fnmatchcase(target.lower(), action.lower()) for target in DELETE_LOG_ACTIONS
                ):
                    found.append(f"{logical_id}: {action}")
    return found


def test_no_role_may_delete_log_groups_or_streams(deployment: Synthesized) -> None:
    """Criterio de aceptación: ningún rol de aplicación tiene ``logs:DeleteLogGroup`` ni
    ``DeleteLogStream`` (§8 y §9.2; NFR-NUC-20, 28), en ninguna pila."""
    for name, template in deployment.templates.items():
        assert delete_log_permissions(template) == [], name


@pytest.mark.parametrize("action", ["logs:DeleteLogGroup", "logs:DeleteLogStream", "logs:*", "*"])
def test_a_delete_log_permission_is_detected(action: str) -> None:
    def build(stack: Stack) -> None:
        role = iam.Role(stack, "Task", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        role.add_to_policy(iam.PolicyStatement(actions=[action], resources=["*"]))

    assert delete_log_permissions(probe(build)) != []


# --- Alarmas (§9.4) ------------------------------------------------------------------------


def test_alarms_are_exactly_the_declared_ones(deployment: Synthesized) -> None:
    assert set(_alarms(deployment)) == expected_alarm_keys(deployment.config)


def test_every_alarm_notifies_vigia_alerts(deployment: Synthesized) -> None:
    for key, alarm in _alarms(deployment).items():
        (action,) = alarm["AlarmActions"]
        output = action["Fn::GetStackOutput"]
        assert output["StackName"] == deployment.config.stack_name("foundation"), key
        assert "AlertsTopic" in output["OutputName"], key
        assert alarm["DatapointsToAlarm"] == alarm["EvaluationPeriods"], key


def test_application_alarm_table_matches_the_template(deployment: Synthesized) -> None:
    """La tabla literal que lee la prueba cruzada del backend es la de la síntesis."""
    found = {
        key: metrics
        for key, alarm in _alarms(deployment).items()
        if (metrics := _application_metrics(alarm))
    }
    expected = {key: tuple(sorted(value)) for key, value in APPLICATION_ALARM_METRICS.items()}
    assert found == expected


def test_application_alarms_read_the_deployment_and_its_services(deployment: Synthesized) -> None:
    for key, alarm in _alarms(deployment).items():
        for metric in _metric_stats(alarm):
            if metric["Namespace"] != NAMESPACE:
                continue
            dimensions = _dimensions(metric)
            assert dimensions["environment"] == deployment.config.deployment, key
            assert dimensions["service"] in ("vigia-api", "vigia-worker"), key


def test_collector_publishes_every_alarm_dimension_set(deployment: Synthesized) -> None:
    """Una alarma sobre ``(métrica, dimensiones)`` solo recibe datos si ``awsemf`` publica ese
    conjunto exacto de dimensiones para esa métrica."""
    declarations = _collector_declarations()
    for key, alarm in _alarms(deployment).items():
        for metric in _metric_stats(alarm):
            if metric["Namespace"] != NAMESPACE:
                continue
            name = str(metric["MetricName"])
            dimensions = frozenset(_dimensions(metric))
            assert any(
                dimensions in sets and any(re.search(s, name) for s in selectors)
                for sets, selectors in declarations
            ), f"{key}: {name} {sorted(dimensions)}"


def test_collector_declarations_only_use_closed_low_cardinality_attributes() -> None:
    allowed = FIXED_DIMENSIONS | {
        "route",
        "component",
        "status_class",
        "rate_limit",
        "operation",
        "task",
        "result",
        "pool_class",
        "alert_type",
        "app_version",
    }
    for sets, _ in _collector_declarations():
        for dimensions in sets:
            assert FIXED_DIMENSIONS <= dimensions <= allowed, sorted(dimensions)


def test_every_nfr_nuc_38_condition_has_its_alarms(deployment: Synthesized) -> None:
    """Las condiciones de aplicación (tabla del backend) y las de infraestructura: una sola zona,
    replicación (eventos de RDS), copias y cuotas."""
    alarms = _alarms(deployment)
    for condition, keys in NFR_NUC_38_ALARMS.items():
        assert keys and set(keys) <= set(alarms), condition
    assert "single-zone-api" in alarms
    assert "rds-connections-high" in alarms
    assert any(key.startswith("quota-") for key in alarms)
    data = _template(deployment, "data")
    ((_, subscription),) = _of_type(data, "AWS::RDS::EventSubscription")
    assert {"failover", "failure"} <= set(properties(subscription)["EventCategories"])
    ((_, rule),) = _of_type(_template(deployment), "AWS::Events::Rule")
    assert properties(rule)["Name"] == deployment.config.resource_name("backup-job-failed")


def test_integrity_compromised_fires_within_a_minute(pilot: Synthesized) -> None:
    alarm = _alarms(pilot)["integrity-compromised"]
    assert alarm["AlarmName"] == "vigia-integrity-compromised"
    assert alarm["AlarmDescription"].startswith("MAXIMA SEVERIDAD")
    assert (alarm["Threshold"], alarm["ComparisonOperator"]) == (
        1,
        "GreaterThanOrEqualToThreshold",
    )
    assert alarm["EvaluationPeriods"] == 1
    assert {m["Period"] for m in _metric_stats(alarm)} == {60}
    assert {_dimensions(m)["service"] for m in _metric_stats(alarm)} == {
        "vigia-api",
        "vigia-worker",
    }


@pytest.mark.parametrize(
    ("key", "threshold"),
    [
        ("dead-letter-created", 1),
        ("security-alert", 1),
        ("chain-lock-timeout-rate", 1),
        ("db-pool-saturation", 80),
        ("server-error-rate-application", 1),
        ("outbox-oldest-age", 300),
        ("otel-dropped", 0),
        ("restore-drill-overdue", 100),
        ("key-rotation-overdue", 0),
        ("default-partition-rows", 1),
    ],
)
def test_core_thresholds_follow_section_9_4(pilot: Synthesized, key: str, threshold: int) -> None:
    assert _alarms(pilot)[key]["Threshold"] == threshold


def test_rates_are_percentages_without_division_by_zero(pilot: Synthesized) -> None:
    alarms = _alarms(pilot)
    for key in (
        "chain-lock-timeout-rate",
        "server-error-rate-application",
        "server-error-rate-app",
        "node-rate-limited-high",
        "node-permanent-reject-rate",
    ):
        expression = _expression(alarms[key])
        assert expression.startswith("IF(("), key
        assert expression.endswith(", 0)") and "100 *" in expression, key


def test_server_error_rate_application_counts_only_5xx(pilot: Synthesized) -> None:
    alarm = _alarms(pilot)["server-error-rate-application"]
    errors = [m for m in _metric_stats(alarm) if m["MetricName"] == "http_server_errors_total"]
    assert [_dimensions(m).get("status_class") for m in errors] == ["5xx"]


def test_latency_alarms_fire_above_twice_the_nfr_nuc_01_target(pilot: Synthesized) -> None:
    alarms = _alarms(pilot)
    for operation, target in OPERATION_P95_TARGETS_MS.items():
        alarm = alarms[f"latency-{operation.replace('_', '-')}"]
        ((metric),) = list(_metric_stats(alarm))
        assert _dimensions(metric)["operation"] == operation
        assert metric["Stat"] == "p95"
        assert (metric["Period"], alarm["EvaluationPeriods"]) == (300, 3)  # 15 min
        assert alarm["Threshold"] == 2 * target
        assert alarm["ComparisonOperator"] == "GreaterThanThreshold"
    verify = alarms["latency-verify-chains-incremental"]
    assert (verify["Threshold"], verify["ComparisonOperator"]) == (2500, "LessThanThreshold")


def test_periodic_task_alarms_use_each_task_threshold(pilot: Synthesized) -> None:
    alarms = _alarms(pilot)
    assert PERIODIC_TASK_MAX_AGE_SECONDS["write_checkpoints"] == 26 * 3600
    assert PERIODIC_TASK_MAX_AGE_SECONDS["create_partitions"] == 8 * 86400
    assert PERIODIC_TASK_MAX_AGE_SECONDS["archive_audit_partitions"] == 35 * 86400
    for task, max_age in PERIODIC_TASK_MAX_AGE_SECONDS.items():
        alarm = alarms[f"periodic-task-stale-{task.replace('_', '-')}"]
        ((metric),) = list(_metric_stats(alarm))
        assert _dimensions(metric) == {
            "service": "vigia-worker",
            "environment": "pilot",
            "task": task,
        }
        assert (metric["Stat"], alarm["Threshold"]) == ("Maximum", max_age)
        assert alarm["TreatMissingData"] == "missing"
        if max_age == 180:  # tareas de 60 s de U-03: 3 periodos de 1 min
            assert (metric["Period"], alarm["EvaluationPeriods"]) == (60, 3)


def test_node_latency_alarms_follow_nfr_gob_01(pilot: Synthesized) -> None:
    alarms = _alarms(pilot)
    routes = {}
    for key, alarm in alarms.items():
        if key.startswith("latency-node-"):
            ((metric),) = list(_metric_stats(alarm))
            routes[_dimensions(metric)["route"]] = alarm["Threshold"]
    assert routes == {route: 2 * target for route, target in NODE_ROUTE_P95_TARGETS_MS.items()}


def test_single_zone_alarm_watches_each_zone_of_tg_api(pilot: Synthesized) -> None:
    alarm = _alarms(pilot)["single-zone-api"]
    zones = sorted(_dimensions(m)["AvailabilityZone"] for m in _metric_stats(alarm))
    assert zones == ["us-east-1a", "us-east-1b"]
    assert _expression(alarm) == "MIN([ha, hb])"
    assert (alarm["Threshold"], alarm["EvaluationPeriods"]) == (1, 5)
    assert alarm["TreatMissingData"] == "breaching"


def test_worker_absent_and_api_memory_read_the_cluster_by_name(deployment: Synthesized) -> None:
    alarms = _alarms(deployment)
    cluster = cluster_name(deployment.config)
    ((absent),) = list(_metric_stats(alarms["worker-absent"]))
    assert absent["Namespace"] == "ECS/ContainerInsights"
    assert _dimensions(absent) == {"ClusterName": cluster, "ServiceName": "vigia-worker"}
    ((memory),) = list(_metric_stats(alarms["api-memory-high"]))
    assert _dimensions(memory) == {"ClusterName": cluster, "ServiceName": "vigia-api"}
    assert alarms["api-memory-high"]["Threshold"] == 80


def test_rds_alarms_follow_the_class_of_the_deployment(deployment: Synthesized) -> None:
    alarms = _alarms(deployment)
    config = deployment.config
    maximum = {"db.t4g.medium": 450, "db.t4g.small": 225}[config.db_instance_class]
    assert alarms["rds-connections-high"]["Threshold"] == maximum * 0.8
    assert alarms["rds-storage-low"]["Threshold"] == 0.2 * 100 * 1024**3
    for key in ("rds-connections-high", "rds-storage-low", "rds-cpu-high"):
        ((metric),) = list(_metric_stats(alarms[key]))
        assert _dimensions(metric) == {"DBInstanceIdentifier": db_identifier(config)}, key


def test_quota_alarms_compare_usage_with_the_service_quota(pilot: Synthesized) -> None:
    alarms = _alarms(pilot)
    for key in USAGE_QUOTAS:
        alarm = alarms[key]
        assert "SERVICE_QUOTA(m)" in _expression(alarm), key
        assert (alarm["Threshold"], alarm["ComparisonOperator"]) == (80, "GreaterThanThreshold")
        ((metric),) = list(_metric_stats(alarm))
        assert metric["Namespace"] == "AWS/Usage", key
    reads = alarms["quota-evidence-reads"]
    buckets = {render(_dimensions(m)["BucketName"], {}) for m in _metric_stats(reads)}
    assert buckets == {"vigia-evidence-<AccountId>-us-east-1"}


def test_evidence_bucket_publishes_the_request_metrics_of_the_quota(
    deployment: Synthesized,
) -> None:
    buckets = {
        render(properties(r)["BucketName"], {}): properties(r)
        for _, r in _of_type(_template(deployment, "data"), "AWS::S3::Bucket")
    }
    evidence = deployment.config.bucket_name("evidence", "<AccountId>")
    assert buckets[evidence]["MetricsConfigurations"] == [{"Id": "EntireBucket"}]
    others = [name for name, props in buckets.items() if "MetricsConfigurations" in props]
    assert others == [evidence]


def test_nat_alarms_watch_each_translation(deployment: Synthesized) -> None:
    alarms = _alarms(deployment)
    nats = [key for key in alarms if key.startswith("nat-")]
    assert len(nats) == (2 if deployment.config.nat_per_az else 1)
    for key in nats:
        ((metric),) = list(_metric_stats(alarms[key]))
        assert metric["MetricName"] == "PacketsDropCount"
        assert "Fn::GetStackOutput" in metric["Dimensions"][0]["Value"]


def test_backup_rule_sends_failed_jobs_of_the_vault_to_vigia_alerts(
    deployment: Synthesized,
) -> None:
    ((_, rule),) = _of_type(_template(deployment), "AWS::Events::Rule")
    props = properties(rule)
    assert props["EventPattern"] == {
        "source": ["aws.backup"],
        "detail-type": ["Backup Job State Change"],
        "detail": {
            "state": ["FAILED", "EXPIRED", "ABORTED"],
            "backupVaultName": [deployment.config.resource_name("backup-vault")],
        },
    }
    (target,) = props["Targets"]
    assert "AlertsTopic" in target["Arn"]["Fn::GetStackOutput"]["OutputName"]


# --- Route 53 y disponibilidad (§9.3, nota U02-H-07, nº 12) --------------------------------


def _health_checks(deployment: Synthesized) -> dict[str, tuple[str, JsonObject]]:
    template = _template(deployment)
    found = {}
    for logical_id, check in _of_type(template, "AWS::Route53::HealthCheck"):
        props = properties(check)
        (name,) = [tag["Value"] for tag in props["HealthCheckTags"] if tag["Key"] == "Name"]
        found[_key(deployment.config, name)] = (logical_id, props["HealthCheckConfig"])
    return found


def test_health_checks_probe_app_live_nodes_tcp_and_the_app_index(
    deployment: Synthesized,
) -> None:
    config = deployment.config
    template = _template(deployment)
    checks = _health_checks(deployment)
    assert set(checks) == {"app", "nodes", "app-index"}
    app_host = "staging-7" if config.ephemeral else "app"
    nodes_host = "staging-7-nodes" if config.ephemeral else "nodes"
    for _, check in checks.values():
        assert (check["Port"], check["RequestInterval"], check["FailureThreshold"]) == (443, 30, 3)
    _, app = checks["app"]
    assert (app["Type"], app["ResourcePath"], app["EnableSNI"]) == ("HTTPS", "/health/live", True)
    assert render(app["FullyQualifiedDomainName"], template).startswith(f"{app_host}.")
    # Sin certificado de cliente no hay código HTTP: solo la conexión al 443.
    _, nodes = checks["nodes"]
    assert nodes["Type"] == "TCP"
    assert not {"ResourcePath", "SearchString", "EnableSNI"} & set(nodes)
    assert render(nodes["FullyQualifiedDomainName"], template).startswith(f"{nodes_host}.")
    # /version.json se sirve a cualquier petición; / solo ante navegación (D-3, nota de VIG-71).
    _, index = checks["app-index"]
    assert (index["Type"], index["ResourcePath"], index["SearchString"]) == (
        "HTTPS_STR_MATCH",
        "/version.json",
        "app_version",
    )


def test_each_health_check_has_its_alarm(deployment: Synthesized) -> None:
    alarms = _alarms(deployment)
    names = {"app": "health-check-failed-app", "nodes": "health-check-failed-nodes"}
    names["app-index"] = "vigia-app-index-unhealthy"
    for check, (logical_id, _) in _health_checks(deployment).items():
        alarm = alarms[names[check]]
        ((metric),) = list(_metric_stats(alarm))
        assert metric["MetricName"] == "HealthCheckStatus"
        assert _dimensions(metric)["HealthCheckId"] == {"Fn::GetAtt": [logical_id, "HealthCheckId"]}
        assert (alarm["Threshold"], alarm["EvaluationPeriods"]) == (1, 2)
        assert alarm["TreatMissingData"] == "breaching"


def test_availability_is_an_internal_probe_of_tg_api(pilot: Synthesized) -> None:
    """NFR-NUC-10 como sondeo interno: salud de ``tg-api`` (``/health/ready``) y 5XX."""
    alarm = _alarms(pilot)["availability-internal-probe"]
    ((metric),) = list(_metric_stats(alarm))
    assert metric["MetricName"] == "HealthyHostCount"
    assert set(_dimensions(metric)) == {"LoadBalancer", "TargetGroup"}
    edge = _template(pilot, "edge")
    (ready,) = [
        properties(r)
        for _, r in _of_type(edge, "AWS::ElasticLoadBalancingV2::TargetGroup")
        if properties(r)["Name"] == "tg-api"
    ]
    assert ready["HealthCheckPath"] == "/health/ready"


# --- Tablero (§9.3, NFR-NUC-44, nº 12, 18 y 23) -------------------------------------------


def _dashboard(deployment: Synthesized) -> tuple[str, list[JsonObject]]:
    template = _template(deployment)
    ((_, dashboard),) = _of_type(template, "AWS::CloudWatch::Dashboard")
    props = properties(dashboard)
    body = render(props["DashboardBody"], template)
    # Los tokens quedan como ``<...>`` dentro de cadenas JSON: el cuerpo sigue siendo JSON.
    return str(props["DashboardName"]), list(json.loads(body)["widgets"])


def test_dashboard_has_the_panels_of_nfr_nuc_44_and_the_units(deployment: Synthesized) -> None:
    name, widgets = _dashboard(deployment)
    assert name == dashboard_name(deployment.config)
    titles = {
        str(w["properties"]["markdown"]).removeprefix("## ")
        for w in widgets
        if w["type"] == "text" and str(w["properties"]["markdown"]).startswith("## ")
    }
    assert titles == {
        "Salud por zona",
        "Disponibilidad (NFR-NUC-10, sondeo interno)",
        "Aplicacion (n 12)",
        "Latencias p95 (NFR-NUC-01)",
        "Errores",
        "Estado de las cadenas",
        "Bandeja de salida",
        "Seguridad (NFR-NUC-28)",
        "Base de datos",
        "Cuotas (10)",
        "Costo",
        "Flota (U-03, n 18)",
        "Estado del lazo (U-04, n 23)",
        "Trabajos diferidos (U-04, n 23)",
        "Asistente (U-04, n 23)",
        "Correo (U-04, n 23)",
        "Salud de la proyeccion (U-04, n 23)",
    }


def test_pilot_dashboard_is_vigia_pilot(pilot: Synthesized) -> None:
    assert _dashboard(pilot)[0] == "vigia-pilot"


def test_application_widget_shows_the_deployed_version(pilot: Synthesized) -> None:
    """Nº 12: versión desplegada por la dimensión ``app_version`` de ``health_ready``."""
    _, widgets = _dashboard(pilot)
    text = json.dumps(widgets)
    assert 'app_version} MetricName=\\"health_ready\\"' in text
    alarms = [w for w in widgets if w["type"] == "alarm"]
    assert any("vigia-app-index" in json.dumps(w) for w in alarms)


# --- Cortafuegos (seguimiento de VIG-41) ---------------------------------------------------


def test_firewall_log_hides_credentials_and_cookies(permanent_deployment: Synthesized) -> None:
    template = _template(permanent_deployment, "edge")
    ((_, logging),) = _of_type(template, "AWS::WAFv2::LoggingConfiguration")
    redacted = properties(logging)["RedactedFields"]
    assert redacted == [
        {"SingleHeader": {"Name": "authorization"}},
        {"SingleHeader": {"Name": "cookie"}},
    ]


def test_waf_blocked_spike_reads_the_web_acl(permanent_deployment: Synthesized) -> None:
    config = permanent_deployment.config
    alarm = _alarms(permanent_deployment)["waf-blocked-spike"]
    ((metric),) = list(_metric_stats(alarm))
    assert _dimensions(metric) == {
        "WebACL": config.resource_name("app-waf"),
        "Region": "us-east-1",
        "Rule": "ALL",
    }
    assert (alarm["Threshold"], metric["Period"]) == (1000, 300)


def test_alarm_tables_are_literals_the_backend_can_read() -> None:
    """La prueba cruzada del backend no importa CDK: lee estas tablas con ``ast``."""
    import ast

    source = (INFRA / "stacks" / "observability.py").read_text(encoding="utf-8")
    literals = {
        target.id: node.value
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name) and node.value is not None
    }
    for name in (
        "APPLICATION_ALARM_METRICS",
        "NFR_NUC_38_ALARMS",
        "OPERATION_P95_TARGETS_MS",
        "PERIODIC_TASK_MAX_AGE_SECONDS",
        "RESTORE_DRILL_OVERDUE_DAYS",
        "PENDING_UNIT_METRICS",
        "PENDING_UNIT_TASKS",
    ):
        ast.literal_eval(literals[name])


def test_collector_file_is_the_one_compute_delivers() -> None:
    assert Path(COLLECTOR) == COLLECTOR_CONFIG
