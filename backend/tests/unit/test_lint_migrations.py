"""Lint de migraciones solo hacia adelante (TASK-106, NFR-NUC-14, PAT-NUC-MAN-09).

Criterio 2 de TASK-106: una migración de prueba con ``DROP TABLE ledger.x`` hace fallar el lint;
una con ``downgrade`` que ejecuta SQL también; una segunda cabeza o una revisión sin prefijo
``nuc_`` también. Además, los bordes de cada regla: nombres entrecomillados, comentarios en
medio, listas, particiones, nombres dinámicos o sin esquema (fallo cerrado), ``op.drop_table``,
fusiones, bifurcaciones, ciclos y numeración.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from alembic.util import format_as_comma
from hypothesis import given
from hypothesis import strategies as st
from mako.template import Template  # type: ignore[import-untyped]

from tools.lint_migrations import (
    DEFAULT_VERSIONS,
    UNIT_PREFIXES,
    AppendOnlyRegistry,
    check_directory,
    check_sql,
    load_registry,
)

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "tools" / "lint_migrations.py"
TEMPLATE = BACKEND / "migrations" / "script.py.mako"
REGISTRY = load_registry()

FORWARD_ONLY = '    raise NotImplementedError("solo hacia adelante")'


def migration(
    revision: str,
    down: str | None = None,
    *,
    upgrade: str = "    pass",
    downgrade: str | None = FORWARD_ONLY,
    header: str = "",
) -> str:
    """Fuente de una migración mínima con ``upgrade`` y ``downgrade`` dados (ya indentados)."""
    parts = [
        '"""Migración de prueba."""',
        "from alembic import op",
        header,
        f"revision = {revision!r}",
        f"down_revision = {down!r}",
        "branch_labels = None",
        "depends_on = None",
        "",
        "def upgrade() -> None:",
        upgrade,
    ]
    if downgrade is not None:
        parts += ["", "def downgrade() -> None:", downgrade]
    return "\n".join(parts) + "\n"


def chain(tmp_path: Path, *files: tuple[str, str]) -> Path:
    versions = tmp_path / "versions"
    versions.mkdir()
    for name, source in files:
        (versions / name).write_text(source, encoding="utf-8")
    return versions


def rules(versions: Path) -> list[str]:
    return [v.rule for v in check_directory(versions, REGISTRY)]


def base(tmp_path: Path, *extra: tuple[str, str]) -> Path:
    """Cadena válida ``nuc_0001`` más los eslabones ``extra``."""
    return chain(tmp_path, ("nuc_0001_base.py", migration("nuc_0001")), *extra)


def sql_rules(sql: str, registry: AppendOnlyRegistry = REGISTRY) -> list[str]:
    return [v.rule for v in check_sql(sql, registry, "m.py", 1, 1)]


# --- El árbol del repositorio -------------------------------------------------------------------


def test_repository_migrations_pass_lint() -> None:
    assert [str(v) for v in check_directory(DEFAULT_VERSIONS, REGISTRY, BACKEND)] == []


def test_template_renders_a_forward_only_migration(tmp_path: Path) -> None:
    """Lo que genera ``alembic revision`` con la plantilla pasa el lint tal cual."""
    rendered = Template(filename=str(TEMPLATE)).render(  # noqa: S702 - plantilla propia
        message="identity tables",
        up_revision="nuc_0002",
        down_revision="nuc_0001",
        create_date="2026-09-29",
        imports="",
        upgrades="",
        comma=format_as_comma,
    )
    versions = base(tmp_path, ("nuc_0002_identity_tables.py", rendered))
    assert rules(versions) == []
    assert "raise NotImplementedError(" in rendered


# --- Criterio 2 de TASK-106 ---------------------------------------------------------------------


def test_drop_table_on_ledger_fails_lint(tmp_path: Path) -> None:
    versions = base(
        tmp_path,
        (
            "nuc_0002_drop.py",
            migration("nuc_0002", "nuc_0001", upgrade='    op.execute("DROP TABLE ledger.x")'),
        ),
    )
    assert rules(versions) == ["MIG002"]


def test_downgrade_that_runs_sql_fails_lint(tmp_path: Path) -> None:
    versions = base(
        tmp_path,
        (
            "nuc_0002_undo.py",
            migration(
                "nuc_0002",
                "nuc_0001",
                downgrade='    op.execute("ALTER TABLE shared.consumer DROP COLUMN unit")',
            ),
        ),
    )
    assert rules(versions) == ["MIG001"]


def test_second_head_fails_lint(tmp_path: Path) -> None:
    versions = base(
        tmp_path,
        ("nuc_0002_a.py", migration("nuc_0002", "nuc_0001")),
        ("gob_0002_b.py", migration("gob_0002", "nuc_0001")),
    )
    assert "MIG004" in rules(versions)
    messages = [v.message for v in check_directory(versions, REGISTRY)]
    assert any("una sola cabeza; tiene 2: gob_0002, nuc_0002" in m for m in messages)


def test_revision_without_unit_prefix_fails_lint(tmp_path: Path) -> None:
    versions = base(tmp_path, ("0002_identity.py", migration("0002", "nuc_0001")))
    assert rules(versions) == ["MIG005"]


def test_cli_exits_1_and_names_the_rule(tmp_path: Path) -> None:
    versions = base(
        tmp_path,
        (
            "nuc_0002_drop.py",
            migration("nuc_0002", "nuc_0001", upgrade='    op.execute("DROP TABLE ledger.x")'),
        ),
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(versions)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "nuc_0002_drop.py:10:16: MIG002 DROP TABLE sobre ledger.x" in completed.stdout
    clean = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=BACKEND, capture_output=True, text=True, check=False
    )
    assert (clean.returncode, clean.stdout) == (0, "")


# --- MIG002 y MIG003: SQL destructivo -----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE ledger.x",
        "drop table ledger.ledger_record",
        "DROP TABLE IF EXISTS ledger.x CASCADE",
        'DROP TABLE "ledger"."Record"',
        "DROP /* sin nada */ TABLE -- comentario\n ledger.x",
        "DROP TABLE identity.session, ledger.x",
        "DROP TABLE shared.audit_entry",
        "DROP TABLE shared.audit_entry_2026_10",  # partición de una tabla protegida
        "TRUNCATE ledger.x",
        "TRUNCATE TABLE ONLY identity.role_assignment",
        "TRUNCATE identity.session, ONLY shared.outbox_event *",
        "DELETE FROM ledger.x WHERE true",
        "DELETE FROM ONLY shared.dead_letter",
        "delete from Identity.Zone_Node_Assignment",
        "DROP SCHEMA ledger CASCADE",
        "DROP SCHEMA IF EXISTS shared",  # contiene shared.audit_entry
        "SELECT 1; DROP TABLE ledger.x;",
    ],
)
def test_destructive_sql_on_append_only_fails(sql: str) -> None:
    assert "MIG002" in sql_rules(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE x",  # sin esquema: no se sabe qué borra
        "DELETE FROM record_type",
        "DROP TABLE ",  # nombre concatenado después
        "DROP TABLE %s",
        "DROP TABLE {name}",
        "TRUNCATE %(table)s",
        "EXECUTE 'DROP TABLE ' || quote_ident(t)",
        "EXECUTE 'DELETE FROM ledger.' || t",  # esquema literal, tabla dinámica
        "EXECUTE format('DROP TABLE %I.%I', s, t)",
    ],
)
def test_unverifiable_destructive_sql_fails_closed(sql: str) -> None:
    assert "MIG003" in sql_rules(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE identity.scratch",
        "DROP TABLE IF EXISTS shared.tmp_import",
        "DELETE FROM shared.consumer_tmp WHERE false",
        "TRUNCATE identity.throttle_window",
        "DROP SCHEMA scratch",
        "GRANT SELECT, INSERT, UPDATE ON shared.consumer TO vigia_app",
        "REVOKE TRUNCATE ON ledger.x FROM vigia_app",  # privilegio, no la sentencia
        "REVOKE DELETE, TRUNCATE, UPDATE ON ledger.x FROM vigia_app",
        "CREATE TABLE ledger.x (deleted_from text, truncated boolean)",
        "-- DROP TABLE ledger.x\nSELECT 1",
        "/* DELETE FROM ledger.x */ SELECT 1",
        "SELECT 'no DROP TABLES here'",
    ],
)
def test_allowed_sql_passes(sql: str) -> None:
    assert sql_rules(sql) == []


# Revisión de VIG-31, menor 4: formas destructivas que la primera versión no veía.
@pytest.mark.parametrize(
    ("sql", "rule"),
    [
        ("DROP OWNED BY vigia_migrate CASCADE", "MIG003"),
        ("drop owned by current_user", "MIG003"),
        ("ALTER TABLE shared.audit_entry RENAME TO tmp", "MIG002"),
        ("ALTER TABLE IF EXISTS ONLY ledger.ledger_record RENAME TO old", "MIG002"),
        ('ALTER TABLE "identity"."role_assignment" rename to x', "MIG002"),
        ("ALTER TABLE audit_entry RENAME TO tmp", "MIG003"),
        ("DROP ", "MIG003"),  # 'DROP ' + 'TABLE ledger.x'
        ("SELECT 1; DELETE", "MIG003"),
        ("TRUNCATE\n", "MIG003"),
        ("ALTER ROLE vigia_app PASSWORD 'plain-text'", "MIG006"),
        ("CREATE ROLE x LOGIN PASSWORD E'plain'", "MIG006"),
        ("alter role x password $$plain$$", "MIG006"),
    ],
)
def test_more_destructive_forms_fail(sql: str, rule: str) -> None:
    assert rule in sql_rules(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE identity.scratch RENAME TO scratch_old",
        "ALTER TABLE shared.audit_entry RENAME COLUMN a TO b",
        "ALTER TABLE shared.consumer ADD COLUMN probe integer",
        "EXECUTE format('ALTER ROLE vigia_app WITH %s PASSWORD %L', a, b)",
        "SELECT set_config('vigia.role_verifier', :verifier, true)",
    ],
)
def test_harmless_alter_and_parametrized_password_pass(sql: str) -> None:
    assert sql_rules(sql) == []


def test_docstrings_are_prose_not_sql(tmp_path: Path) -> None:
    source = migration("nuc_0002", "nuc_0001", upgrade='    """Nunca DROP TABLE ni DELETE FROM."""')
    source = source.replace('"""Migración de prueba."""', '"""Prohibido DROP TABLE ledger.x."""')
    assert rules(base(tmp_path, ("nuc_0002_doc.py", source))) == []


def test_fstring_parts_are_checked_fail_closed(tmp_path: Path) -> None:
    body = '    name = "x"\n    op.execute(f"DROP TABLE ledger.{name}")'
    versions = base(tmp_path, ("nuc_0002_f.py", migration("nuc_0002", "nuc_0001", upgrade=body)))
    # "DROP TABLE ledger." sin tabla: esquema de solo anexar o nombre dinámico, siempre falla.
    assert set(rules(versions)) <= {"MIG002", "MIG003"}
    assert rules(versions)


@pytest.mark.parametrize(
    ("call", "rule"),
    [
        ('op.drop_table("x", schema="ledger")', "MIG002"),
        ('op.drop_table("audit_entry", schema="shared")', "MIG002"),
        ('op.drop_table(table_name="role_assignment", schema="identity")', "MIG002"),
        ('op.drop_table("ledger.x")', "MIG002"),
        ('op.drop_table("x")', "MIG003"),
        ('op.drop_table(name, schema="identity")', "MIG003"),
        ('op.drop_table("x", schema=schema)', "MIG003"),
        ("op.execute(sa.table('x', schema='ledger').delete())", "MIG003"),
    ],
)
def test_alembic_operations_on_append_only(tmp_path: Path, call: str, rule: str) -> None:
    body = f"    name = schema = 'x'\n    import sqlalchemy as sa\n    {call}"
    versions = base(tmp_path, ("nuc_0002_op.py", migration("nuc_0002", "nuc_0001", upgrade=body)))
    assert rules(versions) == [rule]


def test_drop_table_on_plain_table_passes(tmp_path: Path) -> None:
    body = '    op.drop_table("scratch", schema="identity")'
    versions = base(tmp_path, ("nuc_0002_op.py", migration("nuc_0002", "nuc_0001", upgrade=body)))
    assert rules(versions) == []


# --- MIG001: downgrade --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "downgrade",
    [
        "    raise NotImplementedError",
        '    raise NotImplementedError("solo hacia adelante")',
        '    """Solo hacia adelante."""\n    raise NotImplementedError()',
    ],
)
def test_forward_only_downgrade_passes(tmp_path: Path, downgrade: str) -> None:
    versions = chain(tmp_path, ("nuc_0001_a.py", migration("nuc_0001", downgrade=downgrade)))
    assert rules(versions) == []


@pytest.mark.parametrize(
    "downgrade",
    [
        None,  # ausente
        "    pass",
        '    """Nada."""',
        '    op.execute("SELECT 1")\n    raise NotImplementedError',
        "    raise ValueError('no')",
        "    raise NotImplementedError from None",
        "    if False:\n        raise NotImplementedError",
        "    return None",
    ],
)
def test_downgrade_with_effect_or_missing_fails(tmp_path: Path, downgrade: str | None) -> None:
    versions = chain(tmp_path, ("nuc_0001_a.py", migration("nuc_0001", downgrade=downgrade)))
    assert rules(versions) == ["MIG001"]


# --- MIG004 y MIG005: cadena e identificadores --------------------------------------------------


def test_linear_chain_across_units_passes(tmp_path: Path) -> None:
    versions = base(
        tmp_path,
        ("gob_0002_catalog.py", migration("gob_0002", "nuc_0001")),
        ("laz_0003_loop.py", migration("laz_0003", "gob_0002")),
        ("nuc_0004_more.py", migration("nuc_0004", "laz_0003")),
    )
    assert rules(versions) == []


def test_empty_directory_passes(tmp_path: Path) -> None:
    assert rules(chain(tmp_path)) == []


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (  # fusión
            [
                ("nuc_0002_a.py", migration("nuc_0002", "nuc_0001")),
                (
                    "nuc_0003_m.py",
                    migration("nuc_0003", "nuc_0002").replace(
                        "down_revision = 'nuc_0002'", "down_revision = ('nuc_0002', 'nuc_0001')"
                    ),
                ),
            ],
            "MIG004",
        ),
        (  # referencia inexistente
            [("nuc_0002_a.py", migration("nuc_0002", "nuc_0099"))],
            "MIG004",
        ),
        (  # segunda raíz
            [("gob_0002_a.py", migration("gob_0002"))],
            "MIG004",
        ),
        (  # etiqueta de rama
            [
                (
                    "nuc_0002_a.py",
                    migration("nuc_0002", "nuc_0001").replace(
                        "branch_labels = None", "branch_labels = ('gob',)"
                    ),
                )
            ],
            "MIG004",
        ),
        (  # depends_on
            [
                (
                    "nuc_0002_a.py",
                    migration("nuc_0002", "nuc_0001").replace(
                        "depends_on = None", "depends_on = 'nuc_0001'"
                    ),
                )
            ],
            "MIG004",
        ),
        (  # número que no es la posición
            [("nuc_0003_a.py", migration("nuc_0003", "nuc_0001"))],
            "MIG005",
        ),
        (  # revisión duplicada
            [("nuc_0001_b.py", migration("nuc_0001"))],
            "MIG005",
        ),
        (  # archivo que no empieza por la revisión
            [("0002_identity.py", migration("nuc_0002", "nuc_0001"))],
            "MIG005",
        ),
        (  # prefijo de unidad desconocido
            [("abc_0002_a.py", migration("abc_0002", "nuc_0001"))],
            "MIG005",
        ),
        (  # formato: sin cuatro cifras
            [("nuc_2_a.py", migration("nuc_2", "nuc_0001"))],
            "MIG005",
        ),
        (  # prefijo en mayúsculas
            [("NUC_0002_a.py", migration("NUC_0002", "nuc_0001"))],
            "MIG005",
        ),
        (  # revisión ausente
            [("nuc_0002_a.py", migration("nuc_0002", "nuc_0001").replace("revision = ", "rev = "))],
            "MIG005",
        ),
    ],
)
def test_chain_rules(tmp_path: Path, files: list[tuple[str, str]], expected: str) -> None:
    assert expected in rules(base(tmp_path, *files))


def test_cycle_without_root_fails(tmp_path: Path) -> None:
    versions = chain(
        tmp_path,
        ("nuc_0001_a.py", migration("nuc_0001", "nuc_0002")),
        ("nuc_0002_b.py", migration("nuc_0002", "nuc_0001")),
    )
    assert "MIG004" in rules(versions)


def test_branch_point_is_reported(tmp_path: Path) -> None:
    versions = base(
        tmp_path,
        ("nuc_0002_a.py", migration("nuc_0002", "nuc_0001")),
        ("laz_0002_b.py", migration("laz_0002", "nuc_0001")),
    )
    messages = [v.message for v in check_directory(versions, REGISTRY)]
    assert any(
        m.startswith("bifurcación: laz_0002, nuc_0002 cuelgan de 'nuc_0001'") for m in messages
    )


def test_syntax_error_is_reported_not_raised(tmp_path: Path) -> None:
    versions = base(tmp_path, ("nuc_0002_a.py", "def upgrade(:\n"))
    assert "MIG005" in rules(versions)


# --- Propiedades --------------------------------------------------------------------------------

_WORD = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)
_RESERVED = {"only", "if", "table", "cascade", "restrict"}
_PLAIN_TABLES = _WORD.filter(lambda w: w not in _RESERVED and not w.startswith("audit_entry"))


def _spell(identifier: str, draw: st.DrawFn) -> str:
    """Una forma válida de escribir el identificador: con comillas o en mayúsculas mezcladas."""
    if draw(st.booleans()):
        return f'"{identifier}"'
    return "".join(c.upper() if draw(st.booleans()) else c for c in identifier)


_GAPS = st.sampled_from([" ", "  ", "\n", "\t", " /* x */ ", " -- y\n"])


@st.composite
def destructive_statements(draw: st.DrawFn) -> str:
    """``DROP TABLE``/``TRUNCATE``/``DELETE FROM`` sobre una tabla protegida, con variaciones."""
    protected = [*sorted(REGISTRY.tables), f"ledger.{draw(_PLAIN_TABLES)}"]
    schema, table = draw(st.sampled_from(protected)).split(".")
    if draw(st.booleans()) and schema != "ledger":
        table = f"{table}_{draw(st.from_regex(r'[0-9]{4}_[0-9]{2}', fullmatch=True))}"
    gap = draw(_GAPS)
    keyword = draw(
        st.sampled_from(
            [
                f"DROP{gap}TABLE",
                f"drop{gap}table{gap}if{gap}exists",
                "TRUNCATE",
                f"truncate{gap}table{gap}only",
                f"DELETE{gap}FROM",
                f"delete{gap}from{gap}only",
            ]
        )
    )
    return f"{keyword}{draw(_GAPS)}{_spell(schema, draw)}.{_spell(table, draw)}"


@given(statement=destructive_statements())
def test_any_spelling_of_a_destructive_statement_on_append_only_fails(statement: str) -> None:
    assert "MIG002" in sql_rules(statement)


@given(schema=st.sampled_from(["identity", "shared"]), table=_PLAIN_TABLES)
def test_destructive_statements_on_plain_tables_pass(schema: str, table: str) -> None:
    name = f"{schema}.{table}"
    if REGISTRY.protects_table(schema, table):
        return
    for keyword in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert sql_rules(f"{keyword} {name}") == [], name


@given(revision=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", max_size=12))
def test_only_unit_prefixed_revisions_are_accepted(
    tmp_path_factory: pytest.TempPathFactory, revision: str
) -> None:
    tmp_path = tmp_path_factory.mktemp("rev")
    versions = chain(tmp_path, (f"{revision}_x.py", migration(revision)))
    valid = revision in {f"{prefix}_0001" for prefix in UNIT_PREFIXES}
    assert ("MIG005" not in rules(versions)) == valid, revision


def test_registry_entries_are_schema_qualified(tmp_path: Path) -> None:
    assert REGISTRY.schemas == frozenset({"ledger"})
    assert all(entry.count(".") == 1 for entry in REGISTRY.tables)
    bad = tmp_path / "append_only.py"
    bad.write_text(
        textwrap.dedent(
            """
            APPEND_ONLY_SCHEMAS = frozenset()
            APPEND_ONLY_TABLES = frozenset({"audit_entry"})
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"no es 'esquema\.tabla'"):
        load_registry(bad)
