"""Chequeos de lint propios de ``tools/lint_rules.py`` (NFR-NUC-19, PAT-NUC-RES-03).

Comprueba que el árbol del repositorio no tiene violaciones y que cada regla detecta lo que
promete. La prueba de aceptación 5 de TASK-101 es ``test_text_with_fstring_fails_lint_naming_rule``:
un archivo con ``text(f"... {x}")`` hace fallar el lint con la regla ``VIG001``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.lint_rules import check_paths, check_source

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "tools" / "lint_rules.py"


def _rules(source: str) -> list[tuple[int, str]]:
    return [(v.line, v.rule) for v in check_source(source, "m.py")]


def test_repository_tree_has_no_violations() -> None:
    paths = [BACKEND / "src", BACKEND / "tests", BACKEND / "tools"]
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
