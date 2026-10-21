"""Alarmas de U-04 en ``vigia-loop`` (TASK-332): infrastructure-design de U-04 §10.3 y §11,
NFR-LAZ-28 y 42, histéresis de PAT-LAZ-MAN-01.

Las quince alarmas nombradas (24 recursos: ``periodic-task-stale-<tarea>`` por cada una de las
diez periódicas de ``U04_TASKS``) con su umbral, su periodo y sus evaluaciones, escritos **a mano**
desde §10.3 en :data:`EXPECTED`; todas con una sola acción, hacia ``vigia-alerts``, y el runbook en
la descripción. Las tablas literales de ``stacks/loop.py`` (las que lee la prueba cruzada del
backend) son las de la síntesis, y el colector publica cada conjunto de dimensiones que vigilan.
Solo ``cdk synth`` (A-47).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from config import EnvironmentConfig
from stacks.loop import (
    CONFIGURATION_SET_DIMENSION,
    LOOP_APPLICATION_ALARM_METRICS,
    LOOP_CONDITION_ALARMS,
    LOOP_CONSUMERS,
    LOOP_PERIODIC_TASK_MAX_AGE_SECONDS,
    LOOP_SERVICE_ALARM_METRICS,
    configuration_set_name,
    email_events_dlq_name,
    email_events_name,
    stale_alarm_key,
)
from tests.conftest import Synthesized
from tests.template_rules import properties, resources
from tests.test_observability import _collector_declarations

JsonObject = Mapping[str, Any]
ALARM = "AWS::CloudWatch::Alarm"
NAMESPACE = "Vigia/Platform"
FIXED_DIMENSIONS = frozenset({"service", "environment"})
GT, GE, LT = "GreaterThanThreshold", "GreaterThanOrEqualToThreshold", "LessThanThreshold"
FIVE_MINUTES, HALF_HOUR, HOUR, DAY = 300, 1_800, 3_600, 86_400
HOURS_26, DAYS_35, DAYS_9 = 26 * HOUR, 35 * DAY, 9 * DAY

# Las diez periódicas de ``U04_TASKS`` (``docs/runbooks/u04-registration.md``) y su ventana de
# §10.3: 26 h, salvo ``monthly_package`` y ``metrics_materialize`` (35 días) y ``digest_weekly``
# (9 días), ``[objetivo propio]``. ``rebuild_loop_projection`` es a demanda (U04-H-14).
U04_PERIODIC: dict[str, int] = {
    "verify_loop_projection": HOURS_26,
    "review_reminders": HOURS_26,
    "evaluate_deadlines": HOURS_26,
    "unclosable_review_due": HOURS_26,
    "metrics_materialize": DAYS_35,
    "monthly_package": DAYS_35,
    "adoption_check": HOURS_26,
    "email_delivery_events_poll": HOURS_26,
    "email_grouping": HOURS_26,
    "digest_weekly": DAYS_9,
}

# Clave → (umbral, comparación, evaluaciones, periodo en s, runbook). PAT-LAZ-MAN-01: dos
# evaluaciones donde el dato oscila (tasa, margen, duración), una donde el hecho es único.
EXPECTED: dict[str, tuple[float, str, int, int, str]] = {
    # Las ocho condiciones de NFR-LAZ-42 (nueve recursos más las periódicas).
    "loop-dead-letter": (1, GE, 1, FIVE_MINUTES, "7.2"),
    "assistant-unavailable": (1, GE, 1, HALF_HOUR, "7.4"),
    "export-stuck": (1, GE, 2, FIVE_MINUTES, "7.6"),
    "export-failed": (1, GE, 1, FIVE_MINUTES, "7.6"),
    "email-bounce-rate": (5, GT, 2, DAY, "7.5"),
    "loop-projection-divergence": (1, GE, 1, FIVE_MINUTES, "7.1"),
    "assistant-token-budget": (0.8, GT, 2, HOUR, "7.4"),
    "assistant-rate-margin": (0.2, LT, 2, FIVE_MINUTES, "7.4"),
    # Las cinco propias de los recursos nuevos.
    "email-events-dlq": (1, GE, 1, FIVE_MINUTES, "7.2"),
    "email-events-queue-age": (900, GT, 2, FIVE_MINUTES, "7.2"),
    "email-send-quota-high": (40_000, GT, 2, DAY, "7.5"),
    "email-bounce-rate-warning": (3, GT, 1, DAY, "7.5"),
    "email-complaint-rate-warning": (0.06, GT, 1, DAY, "7.5"),
    # La que la respuesta 2 exige (I-2).
    "email-complaint-rate": (0.1, GT, 2, DAY, "7.5"),
    **{stale_alarm_key(task): (age, GT, 1, FIVE_MINUTES, "") for task, age in U04_PERIODIC.items()},
}
TASK_RUNBOOKS = {"verify_loop_projection": "7.1", "monthly_package": "7.6"}
TASK_RUNBOOKS["email_delivery_events_poll"] = "7.2"


# --- Lectura de la plantilla ----------------------------------------------------------------


def _loop(deployment: Synthesized) -> JsonObject:
    return deployment.templates[deployment.config.stack_name("loop")]


def _key(config: EnvironmentConfig, name: str) -> str:
    """``vigia-<clave>[<sufijo>]`` → ``<clave>``."""
    assert name.startswith("vigia-") and name.endswith(config.name_suffix), name
    return name.removeprefix("vigia-")[: len(name) - len("vigia-") - len(config.name_suffix)]


def loop_alarms(template: JsonObject, config: EnvironmentConfig) -> dict[str, JsonObject]:
    """Propiedades de cada alarma de ``vigia-loop`` por su clave."""
    found = [properties(r) for _, r in resources(template) if r["Type"] == ALARM]
    keyed = {_key(config, str(alarm["AlarmName"])): alarm for alarm in found}
    assert len(keyed) == len(found), "nombres de alarma repetidos"
    return keyed


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


def _period(alarm: JsonObject) -> int:
    """El periodo de la alarma: el de su métrica o, en una expresión, el único de sus métricas."""
    (period,) = {int(metric["Period"]) for metric in _metric_stats(alarm)}
    return period


def application_metrics(alarm: JsonObject) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Métricas de ``Vigia/Platform`` y sus atributos, como en la tabla de ``stacks/loop.py``."""
    pairs = {
        (str(m["MetricName"]), tuple(sorted(set(_dimensions(m)) - FIXED_DIMENSIONS)))
        for m in _metric_stats(alarm)
        if m["Namespace"] == NAMESPACE
    }
    return tuple(sorted(pairs))


def service_metrics(alarm: JsonObject) -> tuple[tuple[str, str], ...]:
    pairs = {
        (str(m["Namespace"]), str(m["MetricName"]))
        for m in _metric_stats(alarm)
        if m["Namespace"] != NAMESPACE
    }
    return tuple(sorted(pairs))


def threshold_problems(alarms: Mapping[str, JsonObject]) -> list[str]:
    """Alarmas de más o de menos, o con otro umbral, comparación, evaluación o periodo."""
    problems = [f"sobra {key}" for key in sorted(set(alarms) - set(EXPECTED))]
    problems += [f"falta {key}" for key in sorted(set(EXPECTED) - set(alarms))]
    for key, (threshold, comparison, evaluations, period, _) in EXPECTED.items():
        alarm = alarms.get(key)
        if alarm is None:
            continue
        if alarm["Threshold"] != pytest.approx(threshold):
            problems.append(f"{key}: umbral {alarm['Threshold']} != {threshold}")
        if alarm["ComparisonOperator"] != comparison:
            problems.append(f"{key}: {alarm['ComparisonOperator']} != {comparison}")
        if (alarm["EvaluationPeriods"], alarm.get("DatapointsToAlarm")) != (evaluations,) * 2:
            problems.append(f"{key}: evaluaciones {alarm['EvaluationPeriods']} != {evaluations}")
        if _period(alarm) != period:
            problems.append(f"{key}: periodo {_period(alarm)} != {period}")
    return problems


def _alerts_parameter(template: JsonObject, config: EnvironmentConfig) -> str:
    (logical_id,) = (
        logical_id
        for logical_id, parameter in template["Parameters"].items()
        if parameter.get("Default") == f"/vigia/{config.deployment}/alerts-topic-arn"
    )
    return logical_id


def action_problems(alarms: Mapping[str, JsonObject], alerts: str) -> list[str]:
    """Alarmas sin ``vigia-alerts`` como acción única, o con acciones de vuelta a ``OK`` o de
    datos insuficientes (NFR-LAZ-28: solo la organización proveedora, solo el aviso)."""
    problems = []
    for key, alarm in alarms.items():
        if alarm.get("AlarmActions") != [{"Ref": alerts}]:
            problems.append(f"{key}: acciones {alarm.get('AlarmActions')}")
        for extra in ("OKActions", "InsufficientDataActions"):
            if alarm.get(extra):
                problems.append(f"{key}: {extra}")
    return problems


@pytest.fixture
def loop(deployment: Synthesized) -> tuple[Synthesized, dict[str, JsonObject]]:
    return deployment, loop_alarms(_loop(deployment), deployment.config)


# --- Las quince alarmas y sus 24 recursos ---------------------------------------------------


def test_fifteen_named_alarms_in_twenty_four_resources(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    _, alarms = loop
    assert len(alarms) == 24
    named = {re.sub(r"^periodic-task-stale-.*$", "periodic-task-stale-<tarea>", k) for k in alarms}
    assert len(named) == 15
    stale = [key for key in alarms if key.startswith("periodic-task-stale-")]
    assert sorted(stale) == sorted(stale_alarm_key(task) for task in U04_PERIODIC)
    assert stale_alarm_key("rebuild_loop_projection") not in alarms


def test_each_alarm_has_its_threshold_period_and_evaluations(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    _, alarms = loop
    assert threshold_problems(alarms) == []


def test_every_alarm_has_vigia_alerts_as_its_only_action(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    deployment, alarms = loop
    alerts = _alerts_parameter(_loop(deployment), deployment.config)
    assert action_problems(alarms, alerts) == []


def test_every_description_names_its_runbook_and_source(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    _, alarms = loop
    for key, (_, _, _, _, runbook) in EXPECTED.items():
        description = str(alarms[key]["AlarmDescription"])
        if key.startswith("periodic-task-stale-"):
            task = next(t for t in U04_PERIODIC if stale_alarm_key(t) == key)
            assert task in description and "NFR-LAZ-42" in description, key
            expected = TASK_RUNBOOKS.get(task)
            if expected is None:
                assert "Sin runbook propio" in description, key
            else:
                assert f"Runbook {expected} de U-04" in description, key
        else:
            assert f"Runbook {runbook} de U-04" in description, key
        # El asunto del correo lleva el nombre; la descripción, en ASCII como las de U-02.
        assert description.isascii(), key


# --- Las tablas literales son las de la síntesis --------------------------------------------


def test_alarm_tables_match_the_template(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    _, alarms = loop
    application = {k: m for k, a in alarms.items() if (m := application_metrics(a))}
    assert application == {k: tuple(sorted(v)) for k, v in LOOP_APPLICATION_ALARM_METRICS.items()}
    service = {k: m for k, a in alarms.items() if (m := service_metrics(a))}
    assert service == {k: tuple(sorted(v)) for k, v in LOOP_SERVICE_ALARM_METRICS.items()}
    assert not set(application) & set(service)


def test_condition_table_covers_every_alarm_of_nfr_laz_42_once() -> None:
    keys = [key for alarms in LOOP_CONDITION_ALARMS.values() for key in alarms]
    # Los nueve recursos de §10.3 (la exportación en dos), con la periódica una vez por tarea.
    assert len(keys) == len(set(keys)) == 9 - 1 + len(U04_PERIODIC)
    assert set(keys) <= set(LOOP_APPLICATION_ALARM_METRICS) | set(LOOP_SERVICE_ALARM_METRICS)
    assert dict(LOOP_PERIODIC_TASK_MAX_AGE_SECONDS) == U04_PERIODIC


# --- Qué mira cada alarma -------------------------------------------------------------------


def test_application_alarms_read_the_deployment_worker_and_its_dimension_values(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    deployment, alarms = loop
    for key, alarm in alarms.items():
        for metric in _metric_stats(alarm):
            if metric["Namespace"] != NAMESPACE:
                continue
            dimensions = _dimensions(metric)
            assert dimensions["environment"] == deployment.config.deployment, key
            expected = ("vigia-api", "vigia-worker") if key == "assistant-token-budget" else None
            assert dimensions["service"] in (expected or ("vigia-worker",)), key
    dead = [_dimensions(m)["consumer"] for m in _metric_stats(alarms["loop-dead-letter"])]
    assert sorted(dead) == sorted(LOOP_CONSUMERS)
    (stuck,) = _metric_stats(alarms["export-stuck"])
    assert stuck["MetricName"] == "export_stuck_or_failed"
    assert set(_dimensions(stuck)) == FIXED_DIMENSIONS
    for task in U04_PERIODIC:
        (metric,) = _metric_stats(alarms[stale_alarm_key(task)])
        assert _dimensions(metric)["task"] == task
        assert metric["Stat"] == "Maximum"


def test_the_six_u04_consumers_are_the_ones_of_the_runbook() -> None:
    """Runbook 7.2, camino A: los seis consumidores de U-04 en la bandeja de salida."""
    assert set(LOOP_CONSUMERS) == {
        "loop_projection",
        "review_queue",
        "notification_engine",
        "assistant_worker",
        "export_worker",
        "email_delivery_events",
    }


def test_email_alarms_read_the_configuration_set_the_account_and_the_queues(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    deployment, alarms = loop
    config = deployment.config
    configuration_set = {CONFIGURATION_SET_DIMENSION: configuration_set_name(config)}
    for key in (
        "email-bounce-rate",
        "email-bounce-rate-warning",
        "email-complaint-rate",
        "email-complaint-rate-warning",
    ):
        metrics = list(_metric_stats(alarms[key]))
        assert {m["MetricName"] for m in metrics} == {
            "Send",
            "Complaint" if "complaint" in key else "Bounce",
        }, key
        for metric in metrics:
            assert metric["Namespace"] == "AWS/SES" and metric["Stat"] == "Sum", key
            assert _dimensions(metric) == configuration_set, key
        (rate,) = [q for q in alarms[key]["Metrics"] if q.get("ReturnData", True)]
        assert rate["Expression"].startswith("IF(FILL(s, 0) > 0, 100 *"), key
    (quota,) = _metric_stats(alarms["email-send-quota-high"])
    assert quota["Namespace"] == "AWS/SES" and quota["MetricName"] == "Send"
    assert _dimensions(quota) == {} and quota["Stat"] == "Sum"
    template = _loop(deployment)
    queues = {
        str(properties(r)["QueueName"]): logical_id
        for logical_id, r in resources(template)
        if r["Type"] == "AWS::SQS::Queue"
    }
    dlq = alarms["email-events-dlq"]
    assert dlq["Dimensions"] == [
        {
            "Name": "QueueName",
            "Value": {"Fn::GetAtt": [queues[email_events_dlq_name(config)], "QueueName"]},
        }
    ]
    assert dlq["Statistic"] == "Maximum"
    age = alarms["email-events-queue-age"]
    assert age["MetricName"] == "ApproximateAgeOfOldestMessage"
    assert age["Dimensions"] == [
        {
            "Name": "QueueName",
            "Value": {"Fn::GetAtt": [queues[email_events_name(config)], "QueueName"]},
        }
    ]


def test_collector_publishes_every_application_dimension_set(
    loop: tuple[Synthesized, dict[str, JsonObject]],
) -> None:
    """Una alarma sobre ``(métrica, dimensiones)`` solo recibe datos si ``awsemf`` publica ese
    conjunto de dimensiones para esa métrica (``consumer`` y ``task`` incluidos)."""
    _, alarms = loop
    declarations = _collector_declarations()
    seen = 0
    for key, alarm in alarms.items():
        for metric in _metric_stats(alarm):
            if metric["Namespace"] != NAMESPACE:
                continue
            seen += 1
            name, dimensions = str(metric["MetricName"]), frozenset(_dimensions(metric))
            assert any(
                dimensions in sets and any(re.search(s, name) for s in selectors)
                for sets, selectors in declarations
            ), f"{key}: {name} {sorted(dimensions)}"
    assert seen


# --- Las reglas detectan una alarma alterada ------------------------------------------------


def _pilot_alarms(pilot: Synthesized) -> dict[str, JsonObject]:
    return loop_alarms(_loop(pilot), pilot.config)


@pytest.mark.parametrize(
    ("key", "field", "value"),
    [
        ("export-stuck", "EvaluationPeriods", 1),  # sin histéresis
        ("email-bounce-rate", "Threshold", 6),
        ("assistant-rate-margin", "ComparisonOperator", GT),
        ("periodic-task-stale-digest-weekly", "Threshold", HOURS_26),
        ("loop-dead-letter", "DatapointsToAlarm", 2),
    ],
)
def test_threshold_rule_detects_an_altered_alarm(
    pilot: Synthesized, key: str, field: str, value: Any
) -> None:
    alarms = _pilot_alarms(pilot)
    altered = {**alarms, key: {**alarms[key], field: value}}
    assert any(problem.startswith(key) for problem in threshold_problems(altered))


def test_threshold_rule_detects_a_missing_or_extra_alarm(pilot: Synthesized) -> None:
    alarms = _pilot_alarms(pilot)
    without = {k: v for k, v in alarms.items() if k != stale_alarm_key("email_grouping")}
    assert f"falta {stale_alarm_key('email_grouping')}" in threshold_problems(without)
    extra = {**alarms, stale_alarm_key("rebuild_loop_projection"): alarms["export-failed"]}
    assert f"sobra {stale_alarm_key('rebuild_loop_projection')}" in threshold_problems(extra)


@pytest.mark.parametrize(
    "actions",
    [
        {"AlarmActions": []},
        {"AlarmActions": [{"Ref": "OtherTopic"}]},
        {"OKActions": [{"Ref": "Alerts"}]},
    ],
    ids=["sin-accion", "otro-tema", "vuelta-a-ok"],
)
def test_action_rule_detects_another_action(pilot: Synthesized, actions: JsonObject) -> None:
    alerts = _alerts_parameter(_loop(pilot), pilot.config)
    alarms = _pilot_alarms(pilot)
    altered = {**alarms, "export-failed": {**alarms["export-failed"], **actions}}
    assert [p for p in action_problems(altered, alerts) if p.startswith("export-failed")]
    assert action_problems(alarms, alerts) == []


def test_alarm_names_carry_the_deployment_suffix(deployment: Synthesized) -> None:
    config = deployment.config
    names = [
        str(properties(r)["AlarmName"])
        for _, r in resources(_loop(deployment))
        if r["Type"] == ALARM
    ]
    assert sorted(names) == sorted(config.resource_name(key) for key in EXPECTED)
    assert json.dumps(names).count("vigia-") == len(EXPECTED)
