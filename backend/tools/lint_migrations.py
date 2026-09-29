"""Lint de la cadena de migraciones: solo hacia adelante (NFR-NUC-14, PAT-NUC-MAN-09, LC-NUC-12).

Lee las migraciones de ``migrations/versions/`` **sin importarlas** (árbol sintáctico de Python) y
falla ante:

- ``MIG001`` — ``downgrade`` ausente o con efecto. Su cuerpo solo puede ser (docstring opcional
  y) ``raise NotImplementedError(...)``: la reversión es redesplegar la imagen anterior.
- ``MIG002`` — ``DROP TABLE``, ``TRUNCATE`` o ``DELETE FROM`` sobre una tabla de solo anexar, o
  ``DROP SCHEMA`` sobre un esquema que las contiene (``migrations/append_only.py``), en el SQL
  de cualquier cadena del módulo o en ``op.drop_table``. Una tabla protege sus particiones
  (``shared.audit_entry_2026_10``).
- ``MIG003`` — sentencia destructiva que no se puede verificar: nombre dinámico (f-string,
  ``%``, ``.format``, concatenación, ``EXECUTE`` en ``DO``), nombre sin esquema, ``op.drop_table``
  con argumentos no literales o ``delete()`` de SQLAlchemy. Fallo cerrado: se escribe con el
  nombre calificado y literal, o no se escribe.
- ``MIG004`` — la cadena no es lineal: más de una cabeza o de una raíz, bifurcación, fusión
  (``down_revision`` en tupla), ``branch_labels`` o ``depends_on``, referencia a una revisión
  inexistente o ciclo.
- ``MIG005`` — identificador de revisión: sin prefijo de unidad (``nuc_`` U-02, ``gob_`` U-03,
  ``laz_`` U-04) o fuera del formato ``<unidad>_<NNNN>``; ``NNNN`` distinto de la posición del
  eslabón en la cadena (1, 2, 3…: es lo que devuelve ``shared.vigia_schema_version()``);
  duplicado; archivo que no empieza por la revisión; ``revision`` ausente o no literal.

Uso: ``uv run python tools/lint_migrations.py [directorio de versiones]`` (por defecto
``migrations/versions``). Imprime ``archivo:línea:columna: MIGnnn mensaje`` y termina en 1 si hay
alguna violación. ``tests/unit/test_lint_migrations.py`` lo ejecuta sobre el árbol en cada
corrida de pytest.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "AppendOnlyRegistry",
    "Migration",
    "Violation",
    "check_directory",
    "check_migrations",
    "load_registry",
    "main",
    "parse_migration",
]

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_VERSIONS = BACKEND / "migrations" / "versions"
DEFAULT_REGISTRY = BACKEND / "migrations" / "append_only.py"

UNIT_PREFIXES = ("nuc", "gob", "laz")
"""Prefijos de unidad: U-02 núcleo, U-03 gobernanza y flota, U-04 lazo y acreditación."""
REVISION_PATTERN = re.compile(r"^(?P<unit>[a-z]+)_(?P<number>[0-9]{4})$")

_IDENTIFIER = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_QUALIFIED = re.compile(rf"(?P<first>{_IDENTIFIER})(?:\s*\.\s*(?P<second>{_IDENTIFIER}))?")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_DESTRUCTIVE = re.compile(
    r"\b(?:(?P<drop_table>DROP\s+TABLE)|(?P<truncate>TRUNCATE)|(?P<delete>DELETE\s+FROM)"
    r"|(?P<drop_schema>DROP\s+SCHEMA))\b",
    re.IGNORECASE,
)
_PRIVILEGE_CONTEXT = re.compile(r"\s*(?:,|ON\b)", re.IGNORECASE)
_OPTIONAL_WORDS = {
    "drop_table": ("IF EXISTS",),
    "truncate": ("TABLE", "ONLY"),
    "delete": ("ONLY",),
    "drop_schema": ("IF EXISTS",),
}
_LIST_TARGETS = frozenset({"drop_table", "truncate", "drop_schema"})
_STATEMENT_LABEL = {
    "drop_table": "DROP TABLE",
    "truncate": "TRUNCATE",
    "delete": "DELETE",
    "drop_schema": "DROP SCHEMA",
}


@dataclass(frozen=True, slots=True)
class Violation:
    """Una violación con su regla, posición y mensaje."""

    filename: str
    line: int
    column: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.filename}:{self.line}:{self.column}: {self.rule} {self.message}"


@dataclass(frozen=True, slots=True)
class AppendOnlyRegistry:
    """Esquemas y tablas (``esquema.tabla``) de solo anexar."""

    schemas: frozenset[str]
    tables: frozenset[str]

    def protects_table(self, schema: str, table: str) -> bool:
        if schema in self.schemas:
            return True
        name = f"{schema}.{table}"
        return any(name == entry or name.startswith(f"{entry}_") for entry in self.tables)

    def protects_schema(self, schema: str) -> bool:
        return schema in self.schemas or any(
            entry.split(".", 1)[0] == schema for entry in self.tables
        )


@dataclass(slots=True)
class Migration:
    """Lo que el lint sabe de un archivo de migración."""

    path: Path
    filename: str
    revision: str | None = None
    revision_line: int = 1
    down_revision: str | None = None
    down_line: int = 1
    violations: list[Violation] = field(default_factory=list)


def load_registry(path: Path = DEFAULT_REGISTRY) -> AppendOnlyRegistry:
    """Carga ``APPEND_ONLY_SCHEMAS`` y ``APPEND_ONLY_TABLES`` del registro."""
    spec = importlib.util.spec_from_file_location("_vigia_append_only", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schemas = frozenset(module.APPEND_ONLY_SCHEMAS)
    tables = frozenset(module.APPEND_ONLY_TABLES)
    for entry in tables:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", entry):
            raise ValueError(f"{path}: {entry!r} no es 'esquema.tabla' en minúsculas")
    return AppendOnlyRegistry(schemas=schemas, tables=tables)


# --- SQL ----------------------------------------------------------------------------------------


def _normalize_identifier(token: str) -> str:
    if token.startswith('"'):
        return token[1:-1].replace('""', '"')
    return token.lower()


def _skip_words(sql: str, position: int, words: Iterable[str]) -> int:
    for phrase in words:
        pattern = re.compile(r"\s*" + r"\s+".join(phrase.split()) + r"\b", re.IGNORECASE)
        match = pattern.match(sql, position)
        if match is not None:
            position = match.end()
    return position


def _targets(sql: str, position: int, *, many: bool) -> Iterator[tuple[str, str] | None]:
    """Nombres ``(primero, segundo)`` tras la palabra clave; ``None`` si no hay nombre literal."""
    while True:
        match = _QUALIFIED.match(sql, _skip_ws(sql, position))
        if match is None:
            yield None
            return
        yield (match.group("first"), match.group("second") or "")
        position = match.end()
        rest = sql[position:].lstrip()
        if rest.startswith("*"):  # TRUNCATE tabla * (incluye descendientes)
            position = sql.index("*", position) + 1
            rest = sql[position:].lstrip()
        if not many or not rest.startswith(","):
            return
        position = sql.index(",", position) + 1
        position = _skip_words(sql, position, ("ONLY",))


def _skip_ws(sql: str, position: int) -> int:
    while position < len(sql) and sql[position].isspace():
        position += 1
    return position


def _strip_comments(sql: str) -> str:
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))


def check_sql(
    sql: str, registry: AppendOnlyRegistry, filename: str, line: int, column: int
) -> list[Violation]:
    """Violaciones MIG002 y MIG003 de un fragmento de SQL."""
    violations: list[Violation] = []
    cleaned = _strip_comments(sql)
    for match in _DESTRUCTIVE.finditer(cleaned):
        kind = next(name for name, value in match.groupdict().items() if value is not None)
        if kind == "truncate" and _PRIVILEGE_CONTEXT.match(cleaned, match.end()):
            continue  # el privilegio TRUNCATE en un GRANT o REVOKE, no la sentencia
        label = _STATEMENT_LABEL[kind]
        position = _skip_words(cleaned, match.end(), _OPTIONAL_WORDS[kind])
        for target in _targets(cleaned, position, many=kind in _LIST_TARGETS):
            if target is None:
                violations.append(
                    Violation(
                        filename,
                        line,
                        column,
                        "MIG003",
                        f"{label} sin nombre literal: no se puede verificar que no toca una "
                        "tabla de solo anexar (escribe el nombre calificado y literal)",
                    )
                )
                continue
            first, second = (_normalize_identifier(part) if part else "" for part in target)
            if kind == "drop_schema":
                if registry.protects_schema(first) or second:
                    violations.append(
                        Violation(
                            filename,
                            line,
                            column,
                            "MIG002",
                            f"DROP SCHEMA {first}: contiene tablas de solo anexar",
                        )
                    )
                continue
            if not second:
                violations.append(
                    Violation(
                        filename,
                        line,
                        column,
                        "MIG003",
                        f"{label} {first} sin esquema: califica la tabla (esquema.tabla)",
                    )
                )
                continue
            if registry.protects_table(first, second):
                violations.append(
                    Violation(
                        filename,
                        line,
                        column,
                        "MIG002",
                        f"{label} sobre {first}.{second}, tabla de solo anexar (P4, NFR-NUC-14)",
                    )
                )
    return violations


# --- Módulo de migración ------------------------------------------------------------------------


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """``id`` de los nodos de docstring (módulo, funciones, clases): prosa, no SQL."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _string_fragments(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Cada cadena literal del módulo; un f-string da sus partes literales por separado."""
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.value, node


def _literal_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _check_calls(
    tree: ast.Module, registry: AppendOnlyRegistry, filename: str
) -> Iterator[Violation]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name == "drop_table":
            yield from _check_drop_table(node, registry, filename)
        elif name == "delete":
            yield Violation(
                filename,
                node.lineno,
                node.col_offset + 1,
                "MIG003",
                "delete() de SQLAlchemy no verificable: escribe el DELETE en SQL con la tabla "
                "calificada",
            )


def _check_drop_table(
    node: ast.Call, registry: AppendOnlyRegistry, filename: str
) -> Iterator[Violation]:
    table = _literal_str(node.args[0]) if node.args else None
    for keyword in node.keywords:
        if keyword.arg == "table_name":
            table = _literal_str(keyword.value)
    schema_nodes = [k.value for k in node.keywords if k.arg == "schema"]
    if len(node.args) > 1:
        schema_nodes.append(node.args[1])
    schema = _literal_str(schema_nodes[0]) if schema_nodes else None
    where = (node.lineno, node.col_offset + 1)
    if table is None or (schema_nodes and schema is None):
        yield Violation(filename, *where, "MIG003", "op.drop_table con argumentos no literales")
        return
    if schema is None and "." in table:
        schema, table = table.split(".", 1)
    if schema is None:
        yield Violation(
            filename, *where, "MIG003", f"op.drop_table({table!r}) sin esquema: pasa schema="
        )
        return
    if registry.protects_table(schema.lower(), table.lower()):
        yield Violation(
            filename,
            *where,
            "MIG002",
            f"op.drop_table sobre {schema}.{table}, tabla de solo anexar (P4, NFR-NUC-14)",
        )


def _is_not_implemented_raise(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Raise) or statement.cause is not None:
        return False
    exception = statement.exc
    if isinstance(exception, ast.Call):
        exception = exception.func
    return isinstance(exception, ast.Name) and exception.id == "NotImplementedError"


def _check_downgrade(tree: ast.Module, filename: str) -> Iterator[Violation]:
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "downgrade"
    ]
    if not functions:
        yield Violation(
            filename,
            1,
            1,
            "MIG001",
            "falta downgrade(): debe existir y lanzar NotImplementedError (solo hacia adelante)",
        )
        return
    for function in functions:
        body = list(function.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if len(body) != 1 or not _is_not_implemented_raise(body[0]) or function.decorator_list:
            yield Violation(
                filename,
                function.lineno,
                function.col_offset + 1,
                "MIG001",
                "downgrade() con efecto: su único cuerpo es raise NotImplementedError(...) "
                "(NFR-NUC-14; la reversión es redesplegar la imagen anterior)",
            )


def _module_assignments(tree: ast.Module) -> dict[str, tuple[ast.expr | None, int]]:
    values: dict[str, tuple[ast.expr | None, int]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = (node.value, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            values[node.target.id] = (node.value, node.lineno)
    return values


def _is_none(node: ast.expr | None) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


def parse_migration(
    path: Path, registry: AppendOnlyRegistry, root: Path | None = None
) -> Migration:
    """Lee una migración y devuelve sus datos de cadena y sus violaciones locales."""
    filename = str(path.relative_to(root)) if root is not None else str(path)
    migration = Migration(path=path, filename=filename)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=filename)
    except SyntaxError as error:
        migration.violations.append(
            Violation(filename, error.lineno or 1, error.offset or 1, "MIG005", "no compila")
        )
        return migration

    assignments = _module_assignments(tree)
    revision_node, migration.revision_line = assignments.get("revision", (None, 1))
    migration.revision = _literal_str(revision_node)
    if migration.revision is None:
        migration.violations.append(
            Violation(
                filename, migration.revision_line, 1, "MIG005", "revision ausente o no literal"
            )
        )
    down_node, migration.down_line = assignments.get("down_revision", (None, 1))
    if isinstance(down_node, ast.Tuple | ast.List):
        migration.violations.append(
            Violation(
                filename,
                migration.down_line,
                1,
                "MIG004",
                "down_revision en tupla: una fusión rompe la cadena lineal única",
            )
        )
    elif not _is_none(down_node):
        migration.down_revision = _literal_str(down_node)
        if migration.down_revision is None:
            migration.violations.append(
                Violation(filename, migration.down_line, 1, "MIG004", "down_revision no literal")
            )
    for name in ("branch_labels", "depends_on"):
        value, line = assignments.get(name, (None, 1))
        if not _is_none(value):
            migration.violations.append(
                Violation(filename, line, 1, "MIG004", f"{name} debe ser None: la cadena es lineal")
            )

    for text, fragment in _string_fragments(tree):
        line = getattr(fragment, "lineno", 1)
        column = getattr(fragment, "col_offset", 0) + 1
        migration.violations.extend(check_sql(text, registry, filename, line, column))
    migration.violations.extend(_check_calls(tree, registry, filename))
    migration.violations.extend(_check_downgrade(tree, filename))
    return migration


# --- Cadena -------------------------------------------------------------------------------------


def _check_identifier(migration: Migration) -> Iterator[Violation]:
    revision = migration.revision
    if revision is None:
        return
    match = REVISION_PATTERN.fullmatch(revision)
    where = (migration.filename, migration.revision_line, 1)
    if match is None or match.group("unit") not in UNIT_PREFIXES:
        prefixes = ", ".join(f"{prefix}_" for prefix in UNIT_PREFIXES)
        yield Violation(
            *where,
            "MIG005",
            f"revisión {revision!r} sin prefijo de unidad: usa <unidad>_<NNNN> con {prefixes}",
        )
    stem = migration.path.stem
    if stem != revision and not stem.startswith(f"{revision}_"):
        yield Violation(*where, "MIG005", f"el archivo debe llamarse {revision}_<descripción>.py")


def check_migrations(migrations: list[Migration]) -> list[Violation]:
    """Violaciones de cadena (MIG004, MIG005) más las locales de cada migración."""
    violations = [v for migration in migrations for v in migration.violations]
    for migration in migrations:
        violations.extend(_check_identifier(migration))

    by_revision: dict[str, Migration] = {}
    for migration in migrations:
        if migration.revision is None:
            continue
        if migration.revision in by_revision:
            violations.append(
                Violation(
                    migration.filename,
                    migration.revision_line,
                    1,
                    "MIG005",
                    f"revisión {migration.revision!r} duplicada "
                    f"(también en {by_revision[migration.revision].filename})",
                )
            )
            continue
        by_revision[migration.revision] = migration
    if not by_revision:
        return violations

    children: dict[str, list[Migration]] = {}
    roots: list[Migration] = []
    for migration in by_revision.values():
        parent = migration.down_revision
        if parent is None:
            roots.append(migration)
            continue
        if parent not in by_revision:
            violations.append(
                Violation(
                    migration.filename,
                    migration.down_line,
                    1,
                    "MIG004",
                    f"down_revision {parent!r} no existe en la cadena",
                )
            )
            continue
        children.setdefault(parent, []).append(migration)

    for parent, kids in sorted(children.items()):
        if len(kids) > 1:
            names = ", ".join(sorted(kid.revision or "?" for kid in kids))
            violations.append(
                Violation(
                    by_revision[parent].filename,
                    1,
                    1,
                    "MIG004",
                    f"bifurcación: {names} cuelgan de {parent!r}; cada unidad añade al final",
                )
            )
    heads = sorted(rev for rev in by_revision if rev not in children)
    if len(heads) != 1:
        violations.append(
            Violation(
                by_revision[heads[-1]].filename if heads else migrations[0].filename,
                1,
                1,
                "MIG004",
                f"la cadena debe tener una sola cabeza; tiene {len(heads)}: {', '.join(heads)}",
            )
        )
    if len(roots) != 1:
        names = ", ".join(sorted(root.revision or "?" for root in roots))
        violations.append(
            Violation(
                roots[0].filename if roots else migrations[0].filename,
                1,
                1,
                "MIG004",
                f"la cadena debe tener una sola raíz; tiene {len(roots)}: {names}",
            )
        )
        return violations

    # Recorrido desde la raíz: numeración consecutiva y sin ciclos ni eslabones sueltos.
    visited: list[Migration] = []
    current: Migration | None = roots[0]
    while current is not None and current not in visited:
        visited.append(current)
        kids = children.get(current.revision or "", [])
        current = kids[0] if len(kids) == 1 else None
    for position, migration in enumerate(visited, start=1):
        match = REVISION_PATTERN.fullmatch(migration.revision or "")
        if match is not None and int(match.group("number")) != position:
            violations.append(
                Violation(
                    migration.filename,
                    migration.revision_line,
                    1,
                    "MIG005",
                    f"revisión {migration.revision!r} en la posición {position} de la cadena: "
                    f"el número debe ser {position:04d}",
                )
            )
    unreachable = sorted(set(by_revision) - {m.revision for m in visited if m.revision})
    if unreachable and len(heads) == 1 and all(len(k) == 1 for k in children.values()):
        violations.append(
            Violation(
                by_revision[unreachable[0]].filename,
                1,
                1,
                "MIG004",
                f"revisiones fuera de la cadena (ciclo): {', '.join(unreachable)}",
            )
        )
    return violations


def check_directory(
    versions: Path = DEFAULT_VERSIONS,
    registry: AppendOnlyRegistry | None = None,
    root: Path | None = None,
) -> list[Violation]:
    """Lint completo del directorio de versiones."""
    registry = load_registry() if registry is None else registry
    paths = sorted(p for p in versions.glob("*.py") if p.name != "__init__.py")
    migrations = [parse_migration(path, registry, root) for path in paths]
    violations = check_migrations(migrations)
    return sorted(violations, key=lambda v: (v.filename, v.line, v.column, v.rule))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    versions = Path(args[0]) if args else DEFAULT_VERSIONS
    violations = check_directory(versions)
    for violation in violations:
        print(violation)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
