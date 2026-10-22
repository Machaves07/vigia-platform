"""Runbooks de U-04 (TASK-334, VIG-250; RESILIENCY-13; deployment-architecture de U-04 §6 y §6.7).

Los seis runbooks ``docs/runbooks/u04-7.1-*.md`` a ``u04-7.6-*.md`` citan órdenes, alarmas,
métricas, tareas y consumidores. Esta prueba exige que **existan en el código**, no en el diseño:

- secciones fijas de los runbooks de U-03 (Disparador, Pasos, Vuelta atrás, Validación,
  Comunicación, Estado en el código), con los dos caminos de 7.2 y las dos partes de 7.5;
- alarmas: toda clave citada es una de las de ``infra/stacks/loop.py`` (leídas con ``ast``: el
  backend no instala CDK), las quince están citadas y cada alarma aparece en el runbook que nombra
  su descripción (``RUNBOOK_*`` y ``TASK_RUNBOOKS``);
- métricas: todo nombre con forma de métrica es de ``loop.observability.metrics`` (VIG-210) o de
  ``shared.observability.metrics``, y cada runbook cita la métrica de las alarmas que lo disparan;
- consumidores y tareas: los de ``U04_CONSUMERS`` y ``U04_TASKS`` (``loop/registration.py``); 7.2
  cita los seis consumidores y el índice nombra las periódicas sin runbook propio;
- órdenes: cada línea ``vigia-admin …`` de un bloque de código se analiza con el analizador real;
- identificadores: todo identificador en código en línea (``snake_case`` o ``MAYÚSCULAS``) aparece
  en el código fuente, la infraestructura o las migraciones;
- secretos: ningún valor con forma de credencial y ninguna orden que escriba un secreto fuera de la
  entrada estándar.

Además, el índice, el ensayo trimestral (DR-LAZ-01 a 04), el ``README.md`` (los nombres de la
convención salen de las funciones de ``infra/stacks/loop.py``) y el ``CHANGELOG.md``.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path
from typing import Final

import pytest

from vigia_platform.identity.application.admin_cli import build_parser
from vigia_platform.loop.observability.metrics import LoopMetricName
from vigia_platform.loop.registration import U04_CONSUMERS, U04_TASKS
from vigia_platform.shared.observability.metrics import MetricName

ROOT: Final = Path(__file__).resolve().parents[3]
RUNBOOKS: Final = ROOT / "docs" / "runbooks"
LOOP_STACK: Final = ROOT / "infra" / "stacks" / "loop.py"
SOURCES: Final = (
    ROOT / "backend" / "src" / "vigia_platform",
    ROOT / "backend" / "migrations",
    ROOT / "backend" / "tests" / "resilience",
    ROOT / "infra" / "stacks",
    ROOT / "infra" / "config",
    ROOT / "infra" / "tests",
)
"""Donde un runbook puede encontrar sus nombres: código, migraciones, infraestructura y las pruebas
que cita (ensayos DR-LAZ y plantillas de las alarmas)."""

U04_RUNBOOKS: Final = {
    "7.1": "u04-7.1-loop-projection-rebuild.md",
    "7.2": "u04-7.2-dead-letter-replay.md",
    "7.3": "u04-7.3-lost-closure-object.md",
    "7.4": "u04-7.4-assistant-key-rotation.md",
    "7.5": "u04-7.5-email-sandbox-and-reputation.md",
    "7.6": "u04-7.6-export-stuck-or-failed.md",
}
SECTIONS: Final = (
    "Disparador",
    "Pasos",
    "Vuelta atrás",
    "Validación",
    "Comunicación",
    "Estado en el código",
)
STEP_GROUPS: Final = {"7.2": ("Camino A", "Camino B"), "7.5": ("Parte A", "Parte B")}
"""7.2 con sus dos colas muertas y 7.5 con la parte previa al primer despliegue (§6.2 y §6.5)."""

ADMIN_ORDERS: Final = {
    "7.1": "rebuild-loop-projection",
    "7.2": "replay-dead-letter",
    "7.3": "declare-unrecoverable-attachment",
}
PLACEHOLDERS: Final = {
    "<UUID>": "0192a3b4-c5d6-7e8f-9a0b-1c2d3e4f5a6b",
    "<HEX>": "a" * 64,
    "<AAAA-MM-DDTHH:MM:SS.mmmZ>": "2026-10-01T08:00:00.000Z",
    "<consumidor>": "review_queue",
}
"""Los únicos marcadores admitidos en una orden ``vigia-admin`` de un runbook."""

ALARM_TOKEN: Final = re.compile(r"(?:loop|email|export|assistant|periodic-task-stale)-[a-z0-9-]+")
METRIC_TOKEN: Final = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*_(?:total|seconds|ratio|ms|bytes)")
STALE_PREFIX: Final = "periodic-task-stale-"
SECRET_PATTERNS: Final = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(?:password|passwd|api[_-]?key|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{12,}"),
    re.compile(r"(?i)ANTHROPIC_(?:API_KEY|AUTH_TOKEN)\s*="),
)
"""Valores con forma de credencial: ninguno puede aparecer en un runbook."""


# --- Lectura -------------------------------------------------------------------------------------


@cache
def _text(name: str) -> str:
    return (RUNBOOKS / name).read_text(encoding="utf-8")


def _without_fences(text: str) -> str:
    """El texto sin bloques cercados, también los sangrados dentro de una lista."""
    return re.sub(r"^[ \t]*```.*?^[ \t]*```", "", text, flags=re.MULTILINE | re.DOTALL)


def _inline_code(text: str) -> list[str]:
    """El contenido de cada código en línea, fuera de los bloques cercados."""
    return re.findall(r"`([^`\n]+)`", _without_fences(text))


def _fences(text: str) -> list[str]:
    """El contenido de cada bloque cercado, también los sangrados dentro de una lista."""
    return re.findall(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", text, flags=re.MULTILINE | re.DOTALL)


def _headings(text: str, level: int) -> list[str]:
    marks = "#" * level
    return re.findall(rf"^{marks} (.+)$", _without_fences(text), flags=re.MULTILINE)


def _section(text: str, title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, title
    return match.group(1)


def _alarm_tokens(text: str) -> set[str]:
    return {code for code in _inline_code(text) if ALARM_TOKEN.fullmatch(code)}


@cache
def _loop_stack() -> ast.Module:
    return ast.parse(LOOP_STACK.read_text(encoding="utf-8"))


def _assignment(name: str) -> ast.expr:
    for node in _loop_stack().body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                return node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
    raise AssertionError(f"{name} no está en infra/stacks/loop.py")


def _literal(name: str) -> object:
    return ast.literal_eval(_assignment(name))


def loop_alarms() -> frozenset[str]:
    """Las 24 claves de alarma de ``vigia-loop`` (las 15 de §10.3, con una por periódica)."""
    application = _literal("LOOP_APPLICATION_ALARM_METRICS")
    service = _literal("LOOP_SERVICE_ALARM_METRICS")
    assert isinstance(application, dict)
    assert isinstance(service, dict)
    return frozenset(application) | frozenset(service)


def alarm_classes(alarms: frozenset[str]) -> frozenset[str]:
    """Las quince alarmas: las ``periodic-task-stale-<tarea>`` cuentan como una."""
    return frozenset(STALE_PREFIX if key.startswith(STALE_PREFIX) else key for key in alarms)


def _runbook_number(constant: str) -> str:
    value = _literal(constant)
    assert isinstance(value, str)
    match = re.match(r"Runbook (7\.[1-6]) de U-04", value)
    assert match is not None, value
    return match.group(1)


def alarm_runbooks() -> dict[str, str]:
    """Alarma → número del runbook que nombra su descripción en ``infra/stacks/loop.py``.

    Las alarmas con clave literal citan un ``RUNBOOK_*`` en su descripción; las periódicas, el de
    ``TASK_RUNBOOKS`` (las demás remiten a los registros del worker y no entran)."""
    found: dict[str, str] = {}
    for node in ast.walk(_loop_stack()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "LoopAlarm" or not node.args:
            continue
        key = node.args[0]
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        constants = {
            name.id
            for name in ast.walk(node)
            if isinstance(name, ast.Name) and name.id.startswith("RUNBOOK_")
        }
        assert len(constants) == 1, (key.value, constants)
        found[key.value] = _runbook_number(constants.pop())
    tasks = _assignment("TASK_RUNBOOKS")
    assert isinstance(tasks, ast.Dict)
    for task, constant in zip(tasks.keys, tasks.values, strict=True):
        assert isinstance(task, ast.Constant) and isinstance(task.value, str)
        assert isinstance(constant, ast.Name)
        found[STALE_PREFIX + task.value.replace("_", "-")] = _runbook_number(constant.id)
    return found


def convention_names() -> dict[str, str]:
    """Las funciones de nombres de ``infra/stacks/loop.py`` con ``<entorno>`` en el despliegue."""
    wanted = {
        "email_events_name",
        "email_events_dlq_name",
        "queue_endpoint_name",
        "assistant_secret_name",
        "configuration_set_name",
    }
    names: dict[str, str] = {}
    for node in _loop_stack().body:
        if not (isinstance(node, ast.FunctionDef) and node.name in wanted):
            continue
        returned = [item for item in node.body if isinstance(item, ast.Return)]
        assert len(returned) == 1 and isinstance(returned[0].value, ast.JoinedStr), node.name
        parts: list[str] = []
        for value in returned[0].value.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                assert isinstance(value, ast.FormattedValue), node.name
                assert ast.unparse(value.value) == "config.deployment", node.name
                parts.append("<entorno>")
        names[node.name] = "".join(parts)
    assert set(names) == wanted
    return names


@cache
def source_words() -> frozenset[str]:
    """Los identificadores y palabras de ``SOURCES``, más el nombre de cada módulo."""
    words: set[str] = set()
    for base in SOURCES:
        for path in base.rglob("*.py"):
            words.add(path.stem)
            words.update(re.findall(r"[A-Za-z0-9_]+", path.read_text(encoding="utf-8")))
    return frozenset(words)


def identifiers(code: str) -> Iterator[str]:
    """Los identificadores ``snake_case`` o ``MAYÚSCULAS`` con guion bajo de un código en línea."""
    for piece in re.split(r"[^A-Za-z0-9_]+", code):
        if "_" not in piece.strip("_"):
            continue
        if re.fullmatch(r"[a-z][a-z0-9_]*", piece) or re.fullmatch(r"[A-Z][A-Z0-9_]*", piece):
            yield piece


def admin_lines(text: str) -> list[str]:
    """Cada orden ``vigia-admin`` de los bloques de código, con las continuaciones unidas."""
    lines: list[str] = []
    for fence in _fences(text):
        joined = re.sub(r"\\\n\s*", " ", fence)
        for line in joined.splitlines():
            if "vigia-admin " in line:
                command = line[line.index("vigia-admin ") + len("vigia-admin ") :]
                lines.append(command.split("  #", 1)[0].strip())
    return lines


def admin_argv(command: str) -> list[str]:
    argv = [PLACEHOLDERS.get(token, token) for token in shlex.split(command)]
    unknown = [token for token in argv if "<" in token or ">" in token]
    assert not unknown, f"marcador sin valor de prueba en «vigia-admin {command}»: {unknown}"
    return argv


def admin_orders() -> frozenset[str]:
    parser = build_parser()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, Mapping):
            return frozenset(choices)
    raise AssertionError("vigia-admin sin órdenes")


# --- Secciones -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("number", "name"), sorted(U04_RUNBOOKS.items()))
def test_each_runbook_has_the_fixed_sections_in_order(number: str, name: str) -> None:
    text = _text(name)
    assert text.startswith(f"# Runbook U-04 {number} · ")
    assert _headings(text, 2) == list(SECTIONS)
    assert "**Índice**: [runbooks de operación](README.md)." in text
    groups = STEP_GROUPS.get(number, ())
    steps = _section(text, "Pasos")
    subheadings = [heading.split(" · ", 1)[0] for heading in _headings(steps, 3)]
    assert subheadings == list(groups)


def test_the_six_runbooks_and_no_other_u04_7_file_exist() -> None:
    present = sorted(path.name for path in RUNBOOKS.glob("u04-7.*.md"))
    assert present == sorted(U04_RUNBOOKS.values())


# --- Alarmas -------------------------------------------------------------------------------------


def test_every_cited_alarm_exists_in_the_loop_stack() -> None:
    alarms = loop_alarms()
    for name in U04_RUNBOOKS.values():
        unknown = _alarm_tokens(_text(name)) - alarms
        assert not unknown, f"{name}: alarmas que no están en infra/stacks/loop.py: {unknown}"


def test_the_fifteen_alarms_are_all_cited() -> None:
    alarms = loop_alarms()
    assert len(alarm_classes(alarms)) == 15
    assert len(alarms) == 24
    cited = set().union(*(_alarm_tokens(_text(name)) for name in U04_RUNBOOKS.values()))
    assert alarm_classes(alarms) - alarm_classes(frozenset(cited)) == set()


def test_each_alarm_is_cited_in_the_runbook_its_description_names() -> None:
    routes = alarm_runbooks()
    # Las catorce de clave literal y las tres periódicas con runbook propio.
    assert len(routes) == 14 + 3
    for alarm, number in sorted(routes.items()):
        assert alarm in _alarm_tokens(_section(_text(U04_RUNBOOKS[number]), "Disparador")), (
            f"{alarm}: su descripción remite al runbook {number}, que no la cita en «Disparador»"
        )


def test_the_periodic_tasks_without_runbook_are_named_in_the_index() -> None:
    index = (RUNBOOKS / "README.md").read_text(encoding="utf-8")
    line = next(line for line in index.splitlines() if "sin runbook propio" in line)
    named = {code for code in re.findall(r"`([^`]+)`", line) if code in U04_TASKS}
    with_runbook = {
        alarm.removeprefix(STALE_PREFIX).replace("-", "_")
        for alarm in alarm_runbooks()
        if alarm.startswith(STALE_PREFIX)
    }
    assert named == set(U04_TASKS) - with_runbook


# --- Métricas, consumidores y tareas -------------------------------------------------------------


def test_every_cited_metric_is_in_the_metric_modules() -> None:
    known = {metric.value for metric in LoopMetricName} | {metric.value for metric in MetricName}
    for name in U04_RUNBOOKS.values():
        cited = {
            piece
            for code in _inline_code(_text(name))
            for piece in identifiers(code)
            if METRIC_TOKEN.fullmatch(piece) or piece in known
        }
        assert cited - known == set(), name


def test_each_runbook_cites_the_metric_of_the_alarms_that_trigger_it() -> None:
    metrics = _literal("LOOP_APPLICATION_ALARM_METRICS")
    assert isinstance(metrics, dict)
    for alarm, number in alarm_runbooks().items():
        if alarm.startswith(STALE_PREFIX) or alarm not in metrics:
            continue
        cited = set(_inline_code(_section(_text(U04_RUNBOOKS[number]), "Disparador")))
        for metric, _attributes in metrics[alarm]:
            assert metric in cited, f"{U04_RUNBOOKS[number]}: {alarm} sin su métrica {metric}"
            assert metric in {member.value for member in LoopMetricName}


def test_runbook_7_2_names_the_six_consumers() -> None:
    camino_a = _section(_text(U04_RUNBOOKS["7.2"]), "Pasos").split("### Camino B", 1)[0]
    assert {code for code in _inline_code(camino_a) if code in U04_CONSUMERS} == set(U04_CONSUMERS)
    assert len(U04_CONSUMERS) == 6


def test_every_inline_identifier_exists_in_the_code() -> None:
    words = source_words()
    for name in U04_RUNBOOKS.values():
        missing = sorted(
            {
                piece
                for code in _inline_code(_text(name))
                for piece in identifiers(code)
                if piece not in words
            }
        )
        assert missing == [], f"{name}: identificadores que no están en el código: {missing}"


# --- Órdenes -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("number", "name"), sorted(U04_RUNBOOKS.items()))
def test_every_admin_command_parses_with_the_real_parser(number: str, name: str) -> None:
    parser = build_parser()
    commands = admin_lines(_text(name))
    # Las órdenes de 7.1 a 7.3 van en simulacro y en ejecución real; las demás no usan vigia-admin.
    assert len(commands) == (2 if number in ADMIN_ORDERS else 0)
    for command in commands:
        argv = admin_argv(command)
        try:
            parser.parse_args(argv)
        except BaseException as error:
            pytest.fail(f"{name}: «vigia-admin {command}» no es válida: {error!r}")


def test_inline_admin_orders_exist() -> None:
    orders = admin_orders()
    for name in U04_RUNBOOKS.values():
        cited = set(re.findall(r"vigia-admin ([a-z][a-z-]*)", _text(name)))
        assert cited <= orders, f"{name}: órdenes inexistentes {cited - orders}"


@pytest.mark.parametrize(("number", "order"), sorted(ADMIN_ORDERS.items()))
def test_the_runbook_runs_its_admin_order(number: str, order: str) -> None:
    commands = admin_lines(_text(U04_RUNBOOKS[number]))
    assert any(command.split()[0] == order for command in commands)
    assert order in admin_orders()


# --- Secretos ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(U04_RUNBOOKS.values()))
def test_no_runbook_contains_or_writes_a_secret_value(name: str) -> None:
    text = _text(name)
    for pattern in SECRET_PATTERNS:
        assert pattern.search(text) is None, f"{name}: {pattern.pattern}"
    # Un secreto solo se escribe desde la entrada estándar: ni como argumento ni desde un archivo.
    for value in re.findall(r"--secret-(?:string|binary)\s+(\S+)", text):
        assert value == "file:///dev/stdin", f"{name}: --secret-string {value}"
    assert ".env" not in text


# Las sondas se componen al correr para que ningún literal con forma de credencial quede en el árbol:
# el escaneo de secretos del CI (gitleaks) las marcaría como fuga.
@pytest.mark.parametrize(
    "probe",
    [
        pytest.param("AKIA" + "ABCDEFGHIJKLMNOP", id="aws-access-key"),
        pytest.param("sk-" + "ant-api03-" + "abcdefghijkl", id="anthropic-key"),
        pytest.param("-----BEGIN " + "PRIVATE KEY-----", id="private-key"),
        pytest.param("api_key" + " = abcdefghijklmnop", id="api-key-assignment"),
        pytest.param("ANTHROPIC_API_KEY" + "=x", id="anthropic-env-assignment"),
    ],
)
def test_the_secret_patterns_detect_their_probe(probe: str) -> None:
    assert any(pattern.search(probe) for pattern in SECRET_PATTERNS)


# --- Índice, ensayo trimestral, README y CHANGELOG -----------------------------------------------


def test_the_index_links_the_six_runbooks() -> None:
    index = (RUNBOOKS / "README.md").read_text(encoding="utf-8")
    for name in U04_RUNBOOKS.values():
        assert f"]({name})" in index


def test_the_quarterly_drill_links_dr_laz_01_to_04() -> None:
    drill = (RUNBOOKS / "6.1-quarterly-restore-drill.md").read_text(encoding="utf-8")
    section = _section(drill, "Ensayos de U-04 (DR-LAZ-01 a 04)")
    rows = {
        row.split("|")[1].strip(): row
        for row in section.splitlines()
        if row.startswith("| DR-LAZ-")
    }
    expected = {"DR-LAZ-01": "7.1", "DR-LAZ-02": "7.1", "DR-LAZ-03": "7.2", "DR-LAZ-04": "7.3"}
    assert set(rows) == set(expected)
    for drill_id, number in expected.items():
        assert f"]({U04_RUNBOOKS[number]})" in rows[drill_id]


def test_readme_has_the_u04_section_with_the_names_of_the_code() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(readme, "Lazo, acreditación y notificaciones (U-04)")
    assert _headings(section, 3) == [
        "Módulos",
        "Correo",
        "Cola de avisos de entrega",
        "Secreto del asistente, por nombre",
        "Variables nuevas",
        "Convención de nombres",
    ]
    convention = section.split("### Convención de nombres", 1)[1]
    for function, name in convention_names().items():
        assert f"`{name}`" in convention, f"{function}: {name}"
    for module in ("loop", "accreditation", "notifications"):
        assert f"| `{module}` |" in section
    for consumer in U04_CONSUMERS:
        assert f"`{consumer}`" in section
    for task in U04_TASKS:
        assert f"`{task}`" in section


def test_changelog_has_the_u04_entry() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        r"^### Lazo, acreditación y notificaciones \(U-04\)\n(.*?)(?=^### )",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    assert "(VIG-250, TASK-334)" in match.group(1)
