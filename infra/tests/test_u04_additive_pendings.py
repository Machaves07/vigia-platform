"""Pendientes aditivos de U-04 sobre las pilas de U-02 (TASK-332): nº 20, 22 y 23.

- **Nº 20** (A-23 y A-34, U04-H-11): ``vigia-api-task`` escribe en ``org/*/closure/*`` de
  ``vigia-evidence`` con ``s3:PutObject`` en **una sola** sentencia, con la condición de
  ``x-amz-checksum-sha256`` y **sin** condición de cifrado, y el prefijo sale de la denegación.
- **Nº 22** (A-22 y A-33): la regla que aborta las subidas por partes a los 7 días cubre **todo**
  ``vigia-evidence`` (sin filtro de prefijo ni de etiqueta: también ``export/`` y ``monthly/``).
- **Nº 23** (A-24): los cinco paneles de NFR-LAZ-44 en el tablero ``vigia-pilot`` de
  ``vigia-observability``, cada uno con métricas que el código emite (A-64), y ningún tablero en
  ``vigia-loop`` (sin la contingencia ``loop_dashboard_own``).

Cada regla es una función que devuelve sus problemas; las sondas del final comprueban que detecta
el pendiente retirado. Solo ``cdk synth`` (A-47).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from config import EnvironmentConfig
from stacks.observability import LOOP_PANEL_METRICS, LOOP_PANELS, dashboard_name
from tests.conftest import Synthesized
from tests.template_rules import properties, reference_target, render, resources
from tests.test_observability import _dashboard, _section, _widget_metrics

JsonObject = Mapping[str, Any]
ACCOUNT = "<AccountId>"
CLOSURE = "org/*/closure/*"
CHECKSUM = {"Null": {"s3:x-amz-checksum-sha256": "false"}}
FIXED_DIMENSIONS = frozenset({"service", "environment"})
NFR_LAZ_44 = (
    "Estado del lazo",
    "Trabajos diferidos",
    "Asistente",
    "Correo",
    "Salud de la proyeccion",
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


# --- Nº 20 -----------------------------------------------------------------------------------


def api_task_statements(deployment: Synthesized) -> list[JsonObject]:
    """Sentencias de las políticas gestionadas de ``vigia-api-task`` en ``vigia-compute``."""
    config = deployment.config
    template = deployment.templates[config.stack_name("compute")]
    (role,) = (
        r
        for _, r in resources(template)
        if r["Type"] == "AWS::IAM::Role"
        and properties(r).get("RoleName") == config.resource_name("api-task")
    )
    statements: list[JsonObject] = []
    for arn in properties(role).get("ManagedPolicyArns", []):
        policy = template["Resources"][reference_target(arn)]
        statements += _as_list(properties(policy)["PolicyDocument"]["Statement"])
    return statements


def closure_upload_problems(
    statements: Sequence[JsonObject], template: JsonObject, config: EnvironmentConfig
) -> list[str]:
    """Lo que falta del nº 20 en las sentencias de ``vigia-api-task``."""
    closure = f"arn:aws:s3:::{config.bucket_name('evidence', ACCOUNT)}/{CLOSURE}"
    puts = [
        s
        for s in statements
        if s["Effect"] == "Allow" and "s3:PutObject" in _as_list(s.get("Action"))
    ]
    if len(puts) != 1:
        return [f"s3:PutObject en {len(puts)} sentencias, no en una"]
    (put,) = puts
    problems = []
    if closure not in {render(value, template) for value in _as_list(put.get("Resource"))}:
        problems.append(f"sin {CLOSURE}")
    if put.get("Condition") != CHECKSUM:
        problems.append(f"condicion {put.get('Condition')} en vez de la suma")
    if "x-amz-server-side-encryption" in json.dumps(put.get("Condition", {})):
        problems.append("con condicion de cifrado")
    denied = [
        {render(value, template) for value in _as_list(s.get("NotResource"))}
        for s in statements
        if s["Effect"] == "Deny" and "s3:PutObject" in _as_list(s.get("Action"))
    ]
    if not denied or any(closure not in exempt for exempt in denied):
        problems.append(f"{CLOSURE} no sale de la denegacion")
    return problems


def test_pending_20_closure_uploads_need_the_checksum_in_one_statement(
    deployment: Synthesized,
) -> None:
    config = deployment.config
    template = deployment.templates[config.stack_name("compute")]
    assert closure_upload_problems(api_task_statements(deployment), template, config) == []


def _altered_put(statements: list[JsonObject], change: Mapping[str, Any]) -> list[JsonObject]:
    return [
        {**s, **change}
        if s["Effect"] == "Allow" and "s3:PutObject" in _as_list(s.get("Action"))
        else s
        for s in statements
    ]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"Condition": None}, "condicion"),
        (
            {
                "Condition": {
                    **CHECKSUM,
                    "StringEquals": {"s3:x-amz-server-side-encryption": "aws:kms"},
                }
            },
            "cifrado",
        ),
        ({"Resource": "arn:aws:s3:::otro/org/*/closure/*"}, CLOSURE),
    ],
    ids=["sin-suma", "con-cifrado", "sin-prefijo"],
)
def test_pending_20_rule_detects_its_withdrawal(
    pilot: Synthesized, change: Mapping[str, Any], expected: str
) -> None:
    config = pilot.config
    template = pilot.templates[config.stack_name("compute")]
    altered = _altered_put(api_task_statements(pilot), change)
    assert any(expected in p for p in closure_upload_problems(altered, template, config))


def test_pending_20_rule_detects_a_second_put_statement(pilot: Synthesized) -> None:
    config = pilot.config
    template = pilot.templates[config.stack_name("compute")]
    statements = api_task_statements(pilot)
    extra = {"Effect": "Allow", "Action": "s3:PutObject", "Resource": "*"}
    assert closure_upload_problems([*statements, extra], template, config) == [
        "s3:PutObject en 2 sentencias, no en una"
    ]


# --- Nº 22 -----------------------------------------------------------------------------------


def evidence_rules(deployment: Synthesized) -> list[JsonObject]:
    config = deployment.config
    template = deployment.templates[config.stack_name("data")]
    (bucket,) = (
        r
        for _, r in resources(template)
        if r["Type"] == "AWS::S3::Bucket"
        and render(properties(r).get("BucketName"), template)
        == config.bucket_name("evidence", ACCOUNT)
    )
    return list(properties(bucket)["LifecycleConfiguration"]["Rules"])


def abort_rule_problems(rules: Sequence[JsonObject]) -> list[str]:
    """Lo que falta del nº 22: una regla activa que aborta a los 7 días en todo el depósito."""
    aborts = [rule for rule in rules if "AbortIncompleteMultipartUpload" in rule]
    if not aborts:
        return ["sin regla que aborte las subidas por partes"]
    problems = []
    for rule in aborts:
        if rule["AbortIncompleteMultipartUpload"] != {"DaysAfterInitiation": 7}:
            problems.append(f"{rule['Id']}: {rule['AbortIncompleteMultipartUpload']}")
        if rule.get("Status") != "Enabled":
            problems.append(f"{rule['Id']}: no activa")
        # Un filtro deja fuera export/ y monthly/ (o cualquier otro prefijo).
        narrowed = {"Prefix", "Filter", "TagFilters", "ObjectSizeGreaterThan", "ObjectSizeLessThan"}
        if narrowed & set(rule):
            problems.append(f"{rule['Id']}: filtrada por {sorted(narrowed & set(rule))}")
    return problems


def test_pending_22_aborts_incomplete_uploads_after_7_days_in_the_whole_bucket(
    deployment: Synthesized,
) -> None:
    assert abort_rule_problems(evidence_rules(deployment)) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"Prefix": "org/"}, "filtrada"),
        ({"Filter": {"Prefix": "org/*/closure/"}}, "filtrada"),
        ({"AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 30}}, "30"),
        ({"Status": "Disabled"}, "no activa"),
    ],
    ids=["prefijo", "filtro", "otros-dias", "inactiva"],
)
def test_pending_22_rule_detects_a_narrowed_or_altered_rule(
    pilot: Synthesized, change: Mapping[str, Any], expected: str
) -> None:
    rules = [
        {**rule, **change} if "AbortIncompleteMultipartUpload" in rule else rule
        for rule in evidence_rules(pilot)
    ]
    assert any(expected in p for p in abort_rule_problems(rules))


def test_pending_22_rule_detects_the_withdrawn_rule(pilot: Synthesized) -> None:
    rules = [r for r in evidence_rules(pilot) if "AbortIncompleteMultipartUpload" not in r]
    assert abort_rule_problems(rules) == ["sin regla que aborte las subidas por partes"]


# --- Nº 23 -----------------------------------------------------------------------------------


def panel_problems(widgets: list[JsonObject]) -> list[str]:
    """Los cinco paneles de U-04: su sección, al menos un gráfico y las métricas de
    ``LOOP_PANEL_METRICS`` (ni más ni menos) en ``Vigia/Platform``."""
    problems = []
    headers = {str(w["properties"]["markdown"]) for w in widgets if w["type"] == "text"}
    for panel in LOOP_PANELS:
        if f"## {panel}" not in headers:
            problems.append(f"falta el panel {panel}")
            continue
        section = _section(widgets, f"## {panel}")
        graphs = [w for w in section if w["type"] == "metric"]
        if not graphs:
            problems.append(f"{panel}: sin graficos")
        drawn = {
            (name, tuple(sorted(dimensions - FIXED_DIMENSIONS)))
            for widget in graphs
            for name, dimensions, _ in _widget_metrics(widget)
        }
        if drawn != set(LOOP_PANEL_METRICS[panel]):
            problems.append(f"{panel}: {sorted(drawn ^ set(LOOP_PANEL_METRICS[panel]))}")
    return problems


def test_pending_23_five_panels_of_nfr_laz_44_in_the_shared_dashboard(
    deployment: Synthesized,
) -> None:
    name, widgets = _dashboard(deployment)
    assert name == dashboard_name(deployment.config)
    assert [panel.removesuffix(" (U-04, n 23)") for panel in LOOP_PANELS] == list(NFR_LAZ_44)
    assert set(LOOP_PANEL_METRICS) == set(LOOP_PANELS)
    assert panel_problems(widgets) == []
    assert all(
        "Sin emisor en el codigo" not in w.get("properties", {}).get("markdown", "")
        for w in widgets
    )


def test_pilot_dashboard_is_vigia_pilot_with_the_loop_panels(pilot: Synthesized) -> None:
    name, widgets = _dashboard(pilot)
    assert name == "vigia-pilot"
    assert panel_problems(widgets) == []


def test_pending_23_without_loop_dashboard_own(deployment: Synthesized) -> None:
    """A-24: los paneles van al tablero de U-02; ``vigia-loop`` no crea tablero propio y la
    configuración no tiene la contingencia ``loop_dashboard_own``."""
    config = deployment.config
    loop = deployment.templates[config.stack_name("loop")]
    assert not [r for _, r in resources(loop) if r["Type"] == "AWS::CloudWatch::Dashboard"]
    assert "dashboard" not in json.dumps(dataclasses.asdict(config.loop)).lower()
    observability = deployment.templates[config.stack_name("observability")]
    dashboards = [
        r for _, r in resources(observability) if r["Type"] == "AWS::CloudWatch::Dashboard"
    ]
    assert len(dashboards) == 1


def _drop_section(widgets: list[JsonObject], panel: str) -> list[JsonObject]:
    section = {id(w) for w in _section(widgets, f"## {panel}")}
    return [
        w
        for w in widgets
        if id(w) not in section and w.get("properties", {}).get("markdown") != f"## {panel}"
    ]


@pytest.mark.parametrize("panel", LOOP_PANELS)
def test_pending_23_rule_detects_a_withdrawn_or_emptied_panel(
    pilot: Synthesized, panel: str
) -> None:
    _, widgets = _dashboard(pilot)
    assert f"falta el panel {panel}" in panel_problems(_drop_section(widgets, panel))
    section = {id(w) for w in _section(widgets, f"## {panel}")}
    texts_only = [w for w in widgets if id(w) not in section or w["type"] == "text"]
    assert f"{panel}: sin graficos" in panel_problems(texts_only)
