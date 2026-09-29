"""Chequeos de lint propios de ``tools/lint_rules.py`` (NFR-NUC-19, PAT-NUC-RES-03).

Comprueba que el árbol del repositorio no tiene violaciones y que cada regla detecta lo que
promete. La prueba de aceptación 5 de TASK-101 es ``test_text_with_fstring_fails_lint_naming_rule``:
un archivo con ``text(f"... {x}")`` hace fallar el lint con la regla ``VIG001``.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tools.lint_rules import check_paths, check_source

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "tools" / "lint_rules.py"


def _rules(source: str) -> list[tuple[int, str]]:
    return [(v.line, v.rule) for v in check_source(source, "m.py")]


def test_repository_tree_has_no_violations() -> None:
    paths = [BACKEND / "src", BACKEND / "tests", BACKEND / "tools", BACKEND / "migrations"]
    assert [str(v) for v in check_paths(paths, root=BACKEND)] == []


def test_text_with_fstring_fails_lint_naming_rule(tmp_path: Path) -> None:
    """Criterio 5: ``text(f"... {x}")`` hace fallar el lint con la regla nombrada."""
    offending = tmp_path / "query.py"
    offending.write_text(
        "from sqlalchemy import text\n\n\n"
        "def by_name(x: str):\n"
        "    return text(f\"SELECT * FROM identity.users WHERE name = '{x}'\")\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(offending)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "VIG001" in completed.stdout
    assert "query.py:5:17: VIG001 text() con f-string" in completed.stdout


@pytest.mark.parametrize(
    "source",
    [
        'from sqlalchemy import text\nq = text(f"SELECT {x}")\n',
        'import sqlalchemy as sa\nq = sa.text("SELECT %s" % x)\n',
        'from sqlalchemy.sql import text\nq = text("SELECT {}".format(x))\n',
        'from sqlalchemy import text\nq = text("SELECT " + x)\n',
        'from sqlalchemy import text\nq = text(text=f"SELECT {x}")\n',
        'q = text(f"SELECT {x}")\n',
    ],
    ids=["fstring", "percent", "format", "concat", "keyword", "unresolved-name"],
)
def test_vig001_reports_text_with_string_formatting(source: str) -> None:
    assert _rules(source) == [(2 if "\n" in source.strip() else 1, "VIG001")]


@pytest.mark.parametrize(
    "source",
    [
        'from sqlalchemy import text\nq = text("SELECT 1 WHERE x = :x").bindparams(x=x)\n',
        "from sqlalchemy import text\nq = text(QUERY)\n",
        'def text(s):\n    return s\nq = text(f"hola {x}")\n',
        'from markupsafe import text\nq = text(f"hola {x}")\n',
    ],
    ids=["parametrized", "constant-name", "own-function", "other-text"],
)
def test_vig001_allows_parametrized_or_unrelated_text(source: str) -> None:
    violations = _rules(source)
    if "def text" in source:
        # Un ``text`` definido en el módulo no se resuelve a un import: se trata de forma
        # conservadora y sí se marca; se documenta como límite de la regla.
        assert violations == [(3, "VIG001")]
    else:
        assert violations == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import httpx\nc = httpx.Client()\n", [(2, "VIG002")]),
        ("import httpx\nc = httpx.AsyncClient(base_url=u)\n", [(2, "VIG002")]),
        ("from httpx import AsyncClient\nc = AsyncClient()\n", [(2, "VIG002")]),
        ("import httpx as h\nr = h.get(u)\n", [(2, "VIG002")]),
        ("import httpx\nc = httpx.Client(timeout=5.0)\n", []),
        ("import httpx\nr = httpx.post(u, timeout=httpx.Timeout(3.0))\n", []),
        ("import httpx\nc = httpx.Client(**opts)\n", []),
    ],
    ids=["client", "async-client", "from-import", "alias-get", "with-timeout", "post", "kwargs"],
)
def test_vig002_httpx_without_timeout(source: str, expected: list[tuple[int, str]]) -> None:
    assert _rules(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import boto3\ns3 = boto3.client("s3")\n', [(2, "VIG003")]),
        ('import boto3\ns3 = boto3.resource("s3", region_name=r)\n', [(2, "VIG003")]),
        ('import boto3\ns3 = boto3.Session().client("s3")\n', [(2, "VIG003")]),
        ('import boto3\ns3 = boto3.client("s3", config=cfg)\n', []),
        (
            "from botocore.config import Config\ncfg = Config(connect_timeout=5)\n",
            [(2, "VIG003")],
        ),
        (
            "from botocore.config import Config\n"
            "cfg = Config(connect_timeout=5, read_timeout=10, retries={'max_attempts': 2})\n",
            [],
        ),
        ("import botocore.config\ncfg = botocore.config.Config()\n", [(2, "VIG003")]),
    ],
    ids=["client", "resource", "session", "with-config", "config-missing-read", "config", "empty"],
)
def test_vig003_boto3_without_timeout(source: str, expected: list[tuple[int, str]]) -> None:
    assert _rules(source) == expected


def test_cli_reports_clean_tree() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=BACKEND, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.startswith("lint_rules: sin violaciones")


# --- VIG004: mensajes y nombres constantes -------------------------------------------------

VIG004_VIOLATING = """\
import logging

from opentelemetry import trace

from vigia_platform.shared.observability.logging import get_logger

log = get_logger("identity.auth")
tracer = trace.get_tracer("identity")


class Service:
    def __init__(self, name: str) -> None:
        self.log = get_logger(name)  # VIG004

    def login(self, reason: str, user: str) -> None:
        log.warning(reason)  # VIG004
        log.info(f"fallo de {user}")  # VIG004
        self.log.error("fallo de " + user)  # VIG004
        get_logger("identity.otro").critical(reason)  # VIG004
        with tracer.start_as_current_span("login " + user) as span:  # VIG004
            span.update_name(user)  # VIG004
            span.add_event(reason)  # VIG004
        tracer.start_span(name=user).end()  # VIG004
        logging.getLogger("tercero").info(reason)
"""
"""Cada línea marcada viola VIG004; la última es un registrador ajeno y no aplica."""

VIG004_COMPLIANT = """\
from typing import Final

from opentelemetry import trace

from vigia_platform.shared.observability import tracing
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import METER_NAME

LOGIN: Final = tracing.span_name("identity.login")
FAILED: Final[str] = "inicio de sesión fallido"
log = get_logger("identity.auth")


class Service:
    def __init__(self) -> None:
        self.log = get_logger("identity.service")

    def login(self, user: str) -> None:
        self.log.info("servicio iniciado", actor_id=user)
        log.warning(FAILED)
        with trace.get_tracer(METER_NAME).start_as_current_span(LOGIN) as span:
            span.add_event("reintento")
            span.update_name(tracing.TRACER_NAME)
"""


def _marked_lines(source: str) -> list[int]:
    return [n for n, line in enumerate(source.splitlines(), start=1) if "# VIG004" in line]


def _lint_file(tmp_path: Path, name: str, source: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )


def test_vig004_violating_file_fails_naming_the_rule(tmp_path: Path) -> None:
    completed = _lint_file(tmp_path, "violating.py", VIG004_VIOLATING)
    assert completed.returncode == 1
    reported = [
        int(line.split(":")[1]) for line in completed.stdout.splitlines() if " VIG004 " in line
    ]
    assert reported == _marked_lines(VIG004_VIOLATING)
    assert "VIG004 warning() con primer argumento no constante" in completed.stdout


def test_vig004_compliant_file_passes(tmp_path: Path) -> None:
    completed = _lint_file(tmp_path, "compliant.py", VIG004_COMPLIANT)
    assert completed.returncode == 0, completed.stdout
    assert completed.stdout.startswith("lint_rules: sin violaciones")


def test_noqa_suppresses_only_the_named_rule() -> None:
    source = (
        "from vigia_platform.shared.observability.logging import get_logger\n"
        'log = get_logger("x")\n'
        "log.info(dato)  # noqa: VIG004 — prueba hostil\n"
        "log.info(dato)  # noqa: VIG001\n"
    )
    assert _rules(source) == [(4, "VIG004")]


# --- Ronda 2: registro de la biblioteca estándar prohibido en src/ (TID251) ---------------

STDLIB_LOGGING_VIOLATING = """\
import logging
from logging import getLogger

log = logging.getLogger(__name__)
other = getLogger("identity.auth")


def report(template: str, value: int) -> None:
    log.info(template, value)
    logging.warning(template, value)
    logging.root.error(template)
"""

STDLIB_LOGGING_COMPLIANT = """\
from vigia_platform.shared.observability.logging import get_logger

log = get_logger("identity.auth")


def report(value: int) -> None:
    log.info("informe generado", status=value)
"""


def _ruff(source: str, filename: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format",
            "concise",
            "--stdin-filename",
            filename,
            "-",
        ],
        cwd=BACKEND,
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )


def test_src_forbids_stdlib_logging_naming_the_rule() -> None:
    completed = _ruff(STDLIB_LOGGING_VIOLATING, "src/vigia_platform/identity/informe.py")
    assert completed.returncode == 1
    banned = [line for line in completed.stdout.splitlines() if " TID251 " in line]
    assert [int(line.split(":")[1]) for line in banned] == [2, 4, 10, 11]
    assert all("usa vigia_platform.shared.observability.logging.get_logger" in b for b in banned)


def test_src_accepts_get_logger() -> None:
    completed = _ruff(STDLIB_LOGGING_COMPLIANT, "src/vigia_platform/identity/informe.py")
    assert completed.returncode == 0, completed.stdout


def test_stdlib_logging_stays_allowed_outside_src() -> None:
    completed = _ruff(STDLIB_LOGGING_VIOLATING, "tests/unit/informe.py")
    assert " TID251 " not in completed.stdout


def test_src_ruff_config_keeps_every_root_ban() -> None:
    """ruff sustituye la tabla al extender: src/ruff.toml repite cada prohibición de la raíz."""
    with (BACKEND / "pyproject.toml").open("rb") as handle:
        root = tomllib.load(handle)["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
    with (BACKEND / "src" / "ruff.toml").open("rb") as handle:
        src = tomllib.load(handle)["lint"]["flake8-tidy-imports"]["banned-api"]
    assert {key: value for key, value in src.items() if key in root} == root
    assert "logging.getLogger" in src
