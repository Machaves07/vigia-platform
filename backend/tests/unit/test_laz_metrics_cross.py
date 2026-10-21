"""Prueba cruzada de U-04: alarmas y paneles frente a las métricas que el código emite, y tareas
y consumidores frente al catálogo del worker (TASK-332; NFR-LAZ-28, 42 y 44; A-64).

Las alarmas de ``vigia-loop`` (``infra/stacks/loop.py``) y los cinco paneles de U-04 en
``vigia-pilot`` (``infra/stacks/observability.py``) se declaran en tablas literales; aquí se leen
con ``ast`` (el backend no instala CDK) y ``infra/tests/test_loop_alarms.py`` y
``test_u04_additive_pendings.py`` comprueban que son las de la síntesis. Reglas:

- toda métrica de ``Vigia/Platform`` de una alarma o de un panel está en el módulo de nombres de
  U-04 (``loop.observability.metrics``) o en el de U-02 (``observability.metrics``, las que el
  despachador y el planificador emiten por consumidor y tarea), con atributos que su métrica
  admite, y **el código la emite**: fuera de los dos módulos de nombres hay una referencia a su
  miembro (``LoopMetricName.X``, ``MetricName.X``) o a su instrumento (``metrics.x``). Un nombre
  declarado sin emisor no vale (A-64);
- cada condición de ``LOOP_ALARM_CONDITIONS`` tiene sus alarmas, y vigilan su métrica o, si no,
  los sustitutos declarados en ``LOOP_METRIC_SUBSTITUTES``; ``export-failed`` vigila
  ``EXPORT_FAILED_ALARM``;
- las diez periódicas de ``U04_TASKS`` tienen su ``periodic-task-stale-<tarea>`` con la ventana de
  §10.3 (26 h; 35 días ``monthly_package`` y ``metrics_materialize``; 9 días ``digest_weekly``),
  ``rebuild_loop_projection`` ninguna (U04-H-14), y cada nombre está en la lista cerrada ``task``;
- los consumidores de ``loop-dead-letter`` son los de ``U04_CONSUMERS`` y están en la lista
  cerrada ``consumer``.

Las propiedades del final comprueban que el contraste detecta una tabla alterada.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.accreditation.export.worker import EXPORT_WORKER
from vigia_platform.loop.observability.metrics import (
    EXPORT_FAILED_ALARM,
    LOOP_ALARM_CONDITIONS,
    LOOP_CATALOG,
    LoopMetricName,
)
from vigia_platform.loop.registration import U04_CONSUMERS, U04_TASKS
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.metrics import CATALOG, MetricName

ROOT: Final = Path(__file__).resolve().parents[3]
LOOP_STACK: Final = ROOT / "infra" / "stacks" / "loop.py"
OBSERVABILITY_STACK: Final = ROOT / "infra" / "stacks" / "observability.py"
SOURCE: Final = ROOT / "backend" / "src" / "vigia_platform"
NAME_MODULES: Final = (
    SOURCE / "shared" / "observability" / "metrics.py",
    SOURCE / "loop" / "observability" / "metrics.py",
)
HOURS_26: Final = 26 * 3600
OWN_WINDOWS: Final = {
    "monthly_package": 35 * 86_400,
    "metrics_materialize": 35 * 86_400,
    "digest_weekly": 9 * 86_400,
}
"""Las ventanas propias de §10.3 ``[objetivo propio]``; las demás periódicas, 26 h."""
ON_DEMAND: Final = "rebuild_loop_projection"

MetricTable = Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]]


def _literals(path: Path, names: Iterable[str]) -> dict[str, Any]:
    wanted = set(names)
    found: dict[str, Any] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            name, value = (target.id if isinstance(target, ast.Name) else ""), node.value
        else:
            continue
        if name in wanted and value is not None:
            found[name] = ast.literal_eval(value)
    assert set(found) == wanted, sorted(wanted - set(found))
    return found


LOOP = _literals(
    LOOP_STACK,
    (
        "LOOP_APPLICATION_ALARM_METRICS",
        "LOOP_SERVICE_ALARM_METRICS",
        "LOOP_CONDITION_ALARMS",
        "LOOP_METRIC_SUBSTITUTES",
        "LOOP_PERIODIC_TASK_MAX_AGE_SECONDS",
        "LOOP_CONSUMERS",
        "EXPORT_CONSUMER",
    ),
)
PANELS = _literals(OBSERVABILITY_STACK, ("LOOP_PANELS", "LOOP_PANEL_METRICS"))
ALARMS: MetricTable = LOOP["LOOP_APPLICATION_ALARM_METRICS"]
SERVICE_ALARMS: Mapping[str, tuple[tuple[str, str], ...]] = LOOP["LOOP_SERVICE_ALARM_METRICS"]
CONDITIONS: Mapping[str, tuple[str, ...]] = LOOP["LOOP_CONDITION_ALARMS"]
SUBSTITUTES: Mapping[str, tuple[str, ...]] = LOOP["LOOP_METRIC_SUBSTITUTES"]
AGES: Mapping[str, int] = LOOP["LOOP_PERIODIC_TASK_MAX_AGE_SECONDS"]
PANEL_METRICS: MetricTable = PANELS["LOOP_PANEL_METRICS"]

SPECS: Final = {
    **{spec.name.value: spec.attributes for spec in CATALOG},
    **{spec.name.value: spec.attributes for spec in LOOP_CATALOG},
}
"""Nombre publicado → atributos admitidos, de los dos módulos de nombres."""
MEMBERS: Final = {
    **{name.value: name.name for name in MetricName},
    **{name.value: name.name for name in LoopMetricName},
}


def _instrument_attributes() -> set[str]:
    """Atributos de instrumento (``self.x = ...``) de ``PlatformMetrics`` y ``LoopMetrics``."""
    found: set[str] = set()
    for path in NAME_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        found.add(target.attr)
    return found


def _references() -> tuple[frozenset[str], frozenset[str]]:
    """Miembros de ``MetricName``/``LoopMetricName`` y atributos leídos fuera de los módulos de
    nombres (el código que emite)."""
    members: set[str] = set()
    attributes: set[str] = set()
    for path in sorted(SOURCE.rglob("*.py")):
        if path in NAME_MODULES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Attribute):
                continue
            attributes.add(node.attr)
            if isinstance(node.value, ast.Name) and node.value.id in (
                "MetricName",
                "LoopMetricName",
                "_N",
            ):
                members.add(node.attr)
    return frozenset(members), frozenset(attributes)


INSTRUMENTS: Final = _instrument_attributes()
REFERENCED_MEMBERS, READ_ATTRIBUTES = _references()


def emitted(name: str) -> bool:
    """El código fuera de los módulos de nombres usa su miembro o su instrumento."""
    member = MEMBERS.get(name)
    if member is not None and member in REFERENCED_MEMBERS:
        return True
    return name in INSTRUMENTS and name in READ_ATTRIBUTES


EMITTED: Final = frozenset(name for name in SPECS if emitted(name))


def metric_problems(tables: Mapping[str, MetricTable], emitted_names: frozenset[str]) -> list[str]:
    """Métricas de una alarma o un panel fuera de los catálogos, sin emisor o con un atributo que
    su métrica no admite."""
    problems = []
    for table, entries in tables.items():
        for key, metrics in entries.items():
            if not metrics:
                problems.append(f"{table}/{key}: sin métricas")
            for name, attributes in metrics:
                admitted = SPECS.get(name)
                if admitted is None:
                    problems.append(f"{table}/{key}: {name} no está en ningún módulo de nombres")
                    continue
                if name not in emitted_names:
                    problems.append(f"{table}/{key}: {name} no la emite el código")
                for attribute in attributes:
                    if attribute not in admitted:
                        problems.append(f"{table}/{key}: {name} no admite {attribute}")
    return problems


def _watched(key: str) -> set[str]:
    application = {name for name, _ in ALARMS.get(key, ())}
    service = {f"{namespace}:{name}" for namespace, name in SERVICE_ALARMS.get(key, ())}
    return application | service


def condition_problems(
    conditions: Mapping[str, tuple[str, ...]], substitutes: Mapping[str, tuple[str, ...]]
) -> list[str]:
    """Condiciones de NFR-LAZ-42 sin alarma o con alarmas que no vigilan su métrica (ni sus
    sustitutos declarados)."""
    expected = {condition.key for condition in LOOP_ALARM_CONDITIONS}
    problems = []
    if set(conditions) != expected:
        problems.append(f"condiciones distintas: {sorted(set(conditions) ^ expected)}")
    for condition in LOOP_ALARM_CONDITIONS:
        keys = conditions.get(condition.key, ())
        if not keys:
            problems.append(f"{condition.key}: sin alarma")
        unknown = [key for key in keys if key not in ALARMS and key not in SERVICE_ALARMS]
        problems += [f"{condition.key}: {key} no existe" for key in unknown]
        watched = set().union(*(_watched(key) for key in keys)) if keys else set()
        needed = {condition.metric.value}
        if condition.denominator is not None:
            needed.add(condition.denominator.value)
        for metric in needed:
            accepted = {metric, *substitutes.get(metric, ())}
            if metric not in watched and not set(substitutes.get(metric, ())) <= watched:
                problems.append(f"{condition.key}: no vigila {sorted(accepted)}")
            if metric not in watched and not substitutes.get(metric):
                problems.append(f"{condition.key}: {metric} sin sustituto")
    return problems


def task_problems(ages: Mapping[str, int], alarms: MetricTable) -> list[str]:
    """Periódicas de ``U04_TASKS`` sin alarma, con otra ventana o fuera de la lista cerrada."""
    allowed = redaction.DEFAULT_POLICY.values("task")
    problems = [f"{name}: no es de U04_TASKS" for name in sorted(set(ages) - set(U04_TASKS))]
    for name in sorted(U04_TASKS):
        if ages.get(name) != OWN_WINDOWS.get(name, HOURS_26):
            problems.append(f"{name}: ventana {ages.get(name)}")
        key = f"periodic-task-stale-{name.replace('_', '-')}"
        if alarms.get(key) != (("periodic_task_last_success_age_seconds", ("task",)),):
            problems.append(f"{name}: sin alarma {key}")
        if name not in allowed:
            problems.append(f"{name}: fuera de la lista cerrada task")
    stale = {key for key in alarms if key.startswith("periodic-task-stale-")}
    if len(stale) != len(U04_TASKS):
        problems.append(f"{len(stale)} alarmas periodic-task-stale para {len(U04_TASKS)} tareas")
    return problems


TABLES: Final = {"alarma": ALARMS, "panel": PANEL_METRICS}


# --- Métricas: catálogo y emisión (A-64) ----------------------------------------------------


def test_every_alarm_and_panel_metric_is_named_and_emitted() -> None:
    assert metric_problems(TABLES, EMITTED) == []


def test_every_watched_substitute_is_emitted_and_replaces_an_unemitted_metric() -> None:
    for metric, replacements in SUBSTITUTES.items():
        assert metric in SPECS, metric
        for replacement in replacements:
            if ":" not in replacement:
                assert replacement in EMITTED, replacement
    # Lo que se sustituye por falta de emisor sigue sin emitirse: al publicarse, la alarma pasa a
    # vigilarlo y su fila sale de la tabla. La tasa de rebotes sale del conjunto por diseño (§10.3).
    for metric in SUBSTITUTES.keys() - {"email_messages_total"}:
        assert metric not in EMITTED, metric


def test_panels_are_the_five_of_nfr_laz_44_each_with_metrics() -> None:
    assert [title.removesuffix(" (U-04, n 23)") for title in PANELS["LOOP_PANELS"]] == [
        "Estado del lazo",
        "Trabajos diferidos",
        "Asistente",
        "Correo",
        "Salud de la proyeccion",
    ]
    assert set(PANEL_METRICS) == set(PANELS["LOOP_PANELS"])
    assert all(PANEL_METRICS.values())


def test_the_emission_scan_sees_the_known_emitters() -> None:
    """Sonda del analizador: lo que emiten el asistente, la verificación de la proyección, el
    despachador, el planificador y ``export_worker`` cuenta; lo declarado sin emisor, no."""
    for name in (
        "assistant_requests_total",
        "assistant_unavailable",
        "loop_projection_divergences_total",
        "export_failed_total",
        "dead_letter_created_total",
        "outbox_oldest_pending_age_seconds",
        "periodic_task_last_success_age_seconds",
        "periodic_task_duration_ms",
    ):
        assert name in EMITTED, name
    for name in ("loop_overdue_findings", "loop_unclassified_findings", "export_stuck_or_failed"):
        assert name not in EMITTED, name


# --- Condiciones de NFR-LAZ-42 ---------------------------------------------------------------


def test_every_nfr_laz_42_condition_has_alarms_on_its_metric_or_substitutes() -> None:
    assert condition_problems(CONDITIONS, SUBSTITUTES) == []


def test_export_failed_watches_the_export_worker_counter() -> None:
    assert ALARMS["export-failed"] == ((EXPORT_FAILED_ALARM.metric.value, ()),)
    assert "export-failed" in CONDITIONS["export_stuck_or_failed"]


def test_service_alarms_read_the_email_service_and_the_queues() -> None:
    namespaces = {namespace for metrics in SERVICE_ALARMS.values() for namespace, _ in metrics}
    assert namespaces == {"AWS/SES", "AWS/SQS"}
    assert not set(SERVICE_ALARMS) & set(ALARMS)
    assert len(set(SERVICE_ALARMS) | set(ALARMS)) == 24


# --- Tareas y consumidores -------------------------------------------------------------------


def test_the_ten_u04_tasks_have_their_stale_alarm_and_window() -> None:
    """Prueba cruzada contra el catálogo real del worker (``U04_TASKS``)."""
    assert len(U04_TASKS) == 10
    assert ON_DEMAND not in U04_TASKS and ON_DEMAND not in AGES
    assert f"periodic-task-stale-{ON_DEMAND.replace('_', '-')}" not in ALARMS
    assert set(AGES) == set(U04_TASKS)
    assert task_problems(AGES, ALARMS) == []


def test_dead_letter_consumers_are_the_u04_consumers_of_the_closed_list() -> None:
    assert set(LOOP["LOOP_CONSUMERS"]) == set(U04_CONSUMERS)
    assert len(LOOP["LOOP_CONSUMERS"]) == len(U04_CONSUMERS) == 6
    assert set(U04_CONSUMERS) <= redaction.DEFAULT_POLICY.values("consumer")
    assert LOOP["EXPORT_CONSUMER"] == EXPORT_WORKER


# --- El contraste detecta una tabla alterada ------------------------------------------------

_UNEMITTED: Final = sorted(set(SPECS) - EMITTED)
_ALARM_KEYS: Final = sorted(ALARMS)
_PANEL_KEYS: Final = sorted(PANEL_METRICS)


@given(
    st.sampled_from(sorted(TABLES)),
    st.data(),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=3),
)
def test_an_unknown_metric_name_is_reported(table: str, data: st.DataObject, name: str) -> None:
    key = data.draw(st.sampled_from(sorted(TABLES[table])))
    unknown = f"{name}_not_published"
    altered = {**TABLES, table: {**TABLES[table], key: ((unknown, ()), *TABLES[table][key][1:])}}
    assert any(unknown in p for p in metric_problems(altered, EMITTED))


@given(st.sampled_from(sorted(TABLES)), st.data(), st.sampled_from(_UNEMITTED))
def test_a_named_metric_without_emitter_is_reported(
    table: str, data: st.DataObject, name: str
) -> None:
    """A-64: un nombre del módulo que el código no emite no puede ir en una alarma ni en un panel
    (``loop_overdue_findings``, ``export_stuck_or_failed``…)."""
    key = data.draw(st.sampled_from(sorted(TABLES[table])))
    altered = {**TABLES, table: {**TABLES[table], key: ((name, ()), *TABLES[table][key])}}
    assert f"{table}/{key}: {name} no la emite el código" in metric_problems(altered, EMITTED)


@given(
    st.sampled_from(
        sorted(EMITTED & {name for t in TABLES.values() for m in t.values() for name, _ in m})
    )
)
def test_an_emitter_removed_from_the_code_is_reported(name: str) -> None:
    assert any(name in p for p in metric_problems(TABLES, EMITTED - {name}))


@given(st.sampled_from(_ALARM_KEYS + _PANEL_KEYS))
def test_an_attribute_the_metric_does_not_admit_is_reported(key: str) -> None:
    table = "alarma" if key in ALARMS else "panel"
    (name, _), *rest = TABLES[table][key]
    altered = {**TABLES, table: {**TABLES[table], key: ((name, ("user_id",)), *rest)}}
    assert any("user_id" in p for p in metric_problems(altered, EMITTED))


@given(st.sampled_from(sorted(CONDITIONS)))
def test_a_condition_without_alarms_is_reported(condition: str) -> None:
    altered = {**CONDITIONS, condition: ()}
    assert any(condition in p for p in condition_problems(altered, SUBSTITUTES))


@given(st.sampled_from(sorted(SUBSTITUTES)))
def test_a_withdrawn_substitute_is_reported(metric: str) -> None:
    altered = {k: v for k, v in SUBSTITUTES.items() if k != metric}
    assert any(metric in p for p in condition_problems(CONDITIONS, altered))


@given(st.sampled_from(sorted(U04_TASKS)), st.integers(min_value=1, max_value=40 * 86_400))
def test_a_missing_task_alarm_or_another_window_is_reported(task: str, age: int) -> None:
    key = f"periodic-task-stale-{task.replace('_', '-')}"
    without = {k: v for k, v in ALARMS.items() if k != key}
    assert any(task in p for p in task_problems(AGES, without))
    if age != AGES[task]:
        assert any(task in p for p in task_problems({**AGES, task: age}, ALARMS))
