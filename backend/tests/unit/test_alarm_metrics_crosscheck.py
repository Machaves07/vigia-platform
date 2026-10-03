"""Prueba cruzada: cada alarma de ``vigia-observability`` vigila una métrica de
``observability.metrics`` (TASK-149 con TASK-104, criterio 1).

La pila de CDK (``infra/stacks/observability.py``) declara sus alarmas de aplicación en tablas
literales: ``APPLICATION_ALARM_METRICS`` (alarma → métricas y atributos que usa como dimensión)
y ``NFR_NUC_38_ALARMS`` (condición de NFR-NUC-38 → alarmas). ``infra/tests/test_observability.py``
comprueba que esas tablas son las de la plantilla sintetizada; aquí se leen con ``ast`` (el
backend no instala CDK) y se contrastan con el catálogo:

- toda métrica de una alarma está en ``CATALOG`` o en ``PENDING_UNIT_METRICS`` (de U-03, aún sin
  catálogo), nunca en los dos;
- todo atributo que una alarma usa como dimensión es uno de los que admite su métrica;
- cada condición de ``ALARM_CONDITIONS`` tiene alarmas, y cada una vigila la métrica de la
  condición (y su denominador) con los valores de su selector;
- los umbrales copiados en la pila son los del núcleo: objetivos p95 de NFR-NUC-01, 26 h de las
  tareas de la condición y ``RESTORE_DRILL_OVERDUE_DAYS``.

Las propiedades del final comprueban que el contraste detecta una tabla alterada.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.shared.archive.restore_drill import RESTORE_DRILL_OVERDUE_DAYS
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import (
    ALARM_CONDITIONS,
    CATALOG,
    OPERATION_P95_TARGET_MS,
)

STACK = Path(__file__).resolve().parents[3] / "infra" / "stacks" / "observability.py"
TABLES = (
    "APPLICATION_ALARM_METRICS",
    "NFR_NUC_38_ALARMS",
    "OPERATION_P95_TARGETS_MS",
    "PERIODIC_TASK_MAX_AGE_SECONDS",
    "RESTORE_DRILL_OVERDUE_DAYS",
    "PENDING_UNIT_METRICS",
    "PENDING_UNIT_TASKS",
)
TWENTY_SIX_HOURS = 26 * 3600

AlarmTable = Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]]


def _read_tables() -> dict[str, Any]:
    literals: dict[str, Any] = {}
    for node in ast.parse(STACK.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            name, value = (target.id if isinstance(target, ast.Name) else ""), node.value
        else:
            continue
        if name in TABLES and value is not None:
            literals[name] = ast.literal_eval(value)
    return literals


TABLE = _read_tables()
ALARMS: AlarmTable = TABLE["APPLICATION_ALARM_METRICS"]
CONDITION_ALARMS: Mapping[str, tuple[str, ...]] = TABLE["NFR_NUC_38_ALARMS"]
PENDING: Mapping[str, str] = TABLE["PENDING_UNIT_METRICS"]
SPECS = {spec.name.value: spec for spec in CATALOG}


def metric_problems(alarms: AlarmTable, pending: Mapping[str, str]) -> list[str]:
    """Métricas de alarma fuera del catálogo y atributos que su métrica no admite."""
    problems = [
        f"pendiente ya publicada en el catálogo: {name}" for name in pending if name in SPECS
    ]
    for key, metrics in alarms.items():
        if not metrics:
            problems.append(f"{key}: sin métricas")
        for name, attributes in metrics:
            spec = SPECS.get(name)
            if spec is None:
                if name not in pending:
                    problems.append(f"{key}: {name} no está en observability.metrics")
                continue
            for attribute in attributes:
                if attribute not in spec.attributes:
                    problems.append(f"{key}: {name} no admite el atributo {attribute}")
    return problems


def condition_problems(conditions: Mapping[str, tuple[str, ...]], alarms: AlarmTable) -> list[str]:
    """Condiciones de ``ALARM_CONDITIONS`` sin alarma o con una alarma sobre otra métrica."""
    problems = []
    expected = {condition.key for condition in ALARM_CONDITIONS}
    if set(conditions) != expected:
        problems.append(f"condiciones distintas: {sorted(set(conditions) ^ expected)}")
    for condition in ALARM_CONDITIONS:
        keys = conditions.get(condition.key, ())
        if not keys:
            problems.append(f"{condition.key}: sin alarma")
        needed = {condition.metric.value}
        if condition.denominator is not None:
            needed.add(condition.denominator.value)
        for key in keys:
            watched = {name for name, _ in alarms.get(key, ())}
            if not needed <= watched:
                problems.append(f"{condition.key}: {key} no vigila {sorted(needed - watched)}")
            selected = {name: attrs for name, attrs in alarms.get(key, ())}
            for attribute in condition.selector:
                if attribute not in selected.get(condition.metric.value, ()):
                    problems.append(f"{condition.key}: {key} no separa por {attribute}")
    return problems


def test_every_alarm_metric_is_in_the_catalog_or_declared_pending() -> None:
    assert metric_problems(ALARMS, PENDING) == []


def test_pending_metrics_belong_to_another_unit() -> None:
    assert PENDING
    assert all(origin.startswith("U-0") for origin in PENDING.values())
    used = {name for metrics in ALARMS.values() for name, _ in metrics}
    assert set(PENDING) <= used


def test_every_nfr_nuc_38_condition_has_alarms_on_its_metric() -> None:
    assert condition_problems(CONDITION_ALARMS, ALARMS) == []
    for keys in CONDITION_ALARMS.values():
        assert set(keys) <= set(ALARMS)


def test_latency_alarms_cover_every_nfr_nuc_01_operation_with_its_target() -> None:
    assert TABLE["OPERATION_P95_TARGETS_MS"] == dict(OPERATION_P95_TARGET_MS)
    latency = CONDITION_ALARMS["operation_latency_p95"]
    assert sorted(latency) == sorted(
        f"latency-{operation.replace('_', '-')}" for operation in OPERATION_P95_TARGET_MS
    )
    assert set(OPERATION_P95_TARGET_MS) <= set(redaction.DEFAULT_ENUMERATIONS["operation"])


def test_stale_task_alarms_use_26_hours_for_the_condition_tasks() -> None:
    ages: Mapping[str, int] = TABLE["PERIODIC_TASK_MAX_AGE_SECONDS"]
    for condition in ALARM_CONDITIONS:
        for task in condition.selector.get("task", frozenset()):
            assert ages[task] == TWENTY_SIX_HOURS, task
            assert (
                f"periodic-task-stale-{task.replace('_', '-')}" in CONDITION_ALARMS[condition.key]
            )


def test_stale_task_names_are_in_the_closed_list_or_pending() -> None:
    """Un valor de ``task`` fuera de la lista cerrada se borra al limpiar los atributos y la
    alarma no recibiría datos: las de U-03 esperan a que U-03 amplíe la lista."""
    allowed = set(redaction.DEFAULT_ENUMERATIONS["task"])
    pending: Mapping[str, str] = TABLE["PENDING_UNIT_TASKS"]
    for task in TABLE["PERIODIC_TASK_MAX_AGE_SECONDS"]:
        assert (task in allowed) != (task in pending), task


def test_restore_drill_threshold_is_the_core_one() -> None:
    assert TABLE["RESTORE_DRILL_OVERDUE_DAYS"] == RESTORE_DRILL_OVERDUE_DAYS
    assert ALARMS["restore-drill-overdue"] == (("restore_drill_age_days", ()),)


# --- El contraste detecta una tabla alterada ----------------------------------------------

_KEYS = sorted(ALARMS)
_CONDITIONS = sorted(CONDITION_ALARMS)


@given(st.sampled_from(_KEYS), st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=3))
def test_an_unknown_metric_name_is_reported(key: str, name: str) -> None:
    unknown = f"{name}_not_published"
    metrics = ALARMS[key]
    altered = {**ALARMS, key: ((unknown, ()), *metrics[1:])}
    assert any(unknown in problem for problem in metric_problems(altered, PENDING))


@given(st.sampled_from(_CONDITIONS))
def test_a_condition_without_alarms_is_reported(condition: str) -> None:
    altered = {**CONDITION_ALARMS, condition: ()}
    assert any(condition in problem for problem in condition_problems(altered, ALARMS))


@given(st.sampled_from(_CONDITIONS), st.data())
def test_an_alarm_on_another_metric_is_reported(condition: str, data: st.DataObject) -> None:
    key = data.draw(st.sampled_from(CONDITION_ALARMS[condition]))
    altered = {**ALARMS, key: (("otel_dropped_total", ()),)}
    assert any(key in problem for problem in condition_problems(CONDITION_ALARMS, altered))


@given(st.sampled_from([k for k in _KEYS if ALARMS[k][0][0] in SPECS]))
def test_an_attribute_the_metric_does_not_admit_is_reported(key: str) -> None:
    (name, _), *rest = ALARMS[key]
    altered = {**ALARMS, key: ((name, ("user_id",)), *rest)}
    assert any("user_id" in problem for problem in metric_problems(altered, PENDING))
