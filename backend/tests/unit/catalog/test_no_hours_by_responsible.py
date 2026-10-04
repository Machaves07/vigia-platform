"""PR-GOB-10, parte estructural: no existe forma de sumar horas por responsable (H-53, BR-GOB-46).

La prohibición es estructural, no una omisión (DE §2.12, NFR-GOB-41). Esta prueba falla si:

1. alguna ruta de la aplicación (``iter_declared_routes`` sobre ``build_openapi_app``) acepta un
   parámetro de ruta, consulta, cabecera o cookie que nombre al responsable, o un cuerpo con
   ``responsible_user_id`` fuera de la apertura de un paso (donde es el dato del paso, no un
   filtro);
2. alguna respuesta devuelve horas agregadas junto a un responsable o un resumen de horas con un
   usuario (un modelo con un campo de total, horas o resumen no lleva campos de responsable, y
   los elementos de un resumen no llevan ningún usuario);
3. algún protocolo de puerto de U-03 (``catalog``, ``fleet``, ``node_api``) o algún método público
   de ``WalkTestService`` acepta un parámetro de responsable (salvo ``start_step``, que lo
   guarda) o se llama como una agregación por persona;
4. alguna sentencia SQL de ``catalog/`` (o índice de las migraciones ``gob_*``) agrupa, ordena o
   indexa por ``responsible_user_id``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import types
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

from vigia_platform.catalog.application.walk_test import WalkTestService
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import iter_declared_routes

SRC: Final = Path(__file__).resolve().parents[3] / "src" / "vigia_platform"
MIGRATIONS: Final = Path(__file__).resolve().parents[3] / "migrations" / "versions"
U03_PACKAGES: Final = ("catalog", "fleet", "node_api")
RESPONSIBLE: Final = re.compile(r"responsible", re.IGNORECASE)
PERSON: Final = re.compile(r"responsible|user|person|by$", re.IGNORECASE)
HOURS_AGGREGATE: Final = re.compile(r"total|hours|summary", re.IGNORECASE)
AGGREGATION_BY_PERSON: Final = re.compile(
    r"(by|per)_(responsible|user|person)|hours_by|responsible_hours", re.IGNORECASE
)
SQL_BY_RESPONSIBLE: Final = re.compile(
    r"\b(group|order|partition)\s+by\b[^;]*?\bresponsible_user_id\b", re.IGNORECASE | re.DOTALL
)
INDEX_ON_RESPONSIBLE: Final = re.compile(
    r"create\s+(unique\s+)?index\b[^;]*?\bresponsible_user_id\b", re.IGNORECASE | re.DOTALL
)
STEP_OPENING: Final = ("POST", "/walk-tests/{session_id}/steps")
"""La única ruta cuyo cuerpo lleva ``responsible_user_id``: es el dato del paso (DE §2.12)."""


# --- 1 y 2. Rutas ---------------------------------------------------------------------------------


def _dependants(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _dependants(sub)


def _models(annotation: Any) -> Iterator[type[BaseModel]]:
    """Los modelos de Pydantic que aparecen en ``annotation`` (listas, uniones, opcionales)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from _models(argument)


def _reachable(models: list[type[BaseModel]]) -> Iterator[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    pending = list(models)
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        yield model
        for field in model.model_fields.values():
            pending.extend(_models(field.annotation))


def _routes() -> list[tuple[tuple[str, str], APIRoute]]:
    found: list[tuple[tuple[str, str], APIRoute]] = []
    for declared in iter_declared_routes(build_openapi_app().routes):
        route = declared.route
        if isinstance(route, APIRoute):
            for method in sorted(declared.methods):
                found.append(((method, declared.path), route))
    return found


def _parameter_violations(key: tuple[str, str], route: APIRoute) -> list[str]:
    violations: list[str] = []
    for dependant in _dependants(route.dependant):
        for kind, params in (
            ("ruta", dependant.path_params),
            ("consulta", dependant.query_params),
            ("cabecera", dependant.header_params),
            ("cookie", dependant.cookie_params),
        ):
            violations += [
                f"{key}: parámetro de {kind} {p.name!r}"
                for p in params
                if RESPONSIBLE.search(p.name) or RESPONSIBLE.search(str(p.alias))
            ]
        for body in dependant.body_params:
            for model in _reachable(list(_models(body.field_info.annotation))):
                for name in model.model_fields:
                    if RESPONSIBLE.search(name) and not (
                        key == STEP_OPENING and name == "responsible_user_id"
                    ):
                        violations.append(f"{key}: cuerpo {model.__name__}.{name}")
    return violations


def _response_violations(key: tuple[str, str], route: APIRoute) -> list[str]:
    violations: list[str] = []
    for model in _reachable(list(_models(route.response_model))):
        fields = model.model_fields
        aggregates = [name for name in fields if HOURS_AGGREGATE.search(name)]
        if aggregates:
            violations += [
                f"{key}: {model.__name__} une {aggregates} con {name!r}"
                for name in fields
                if RESPONSIBLE.search(name)
            ]
        for name in aggregates:
            for item in _reachable(list(_models(fields[name].annotation))):
                violations += [
                    f"{key}: {model.__name__}.{name} agrega por {inner!r} ({item.__name__})"
                    for inner in item.model_fields
                    if PERSON.search(inner)
                ]
    return violations


def test_no_route_filters_orders_or_aggregates_hours_by_responsible() -> None:
    routes = _routes()
    assert any(key == STEP_OPENING for key, _ in routes), "la apertura de pasos está registrada"
    violations = [
        violation
        for key, route in routes
        for violation in (*_parameter_violations(key, route), *_response_violations(key, route))
    ]
    assert violations == []


def test_the_step_opening_body_is_the_only_place_a_route_receives_the_responsible() -> None:
    receiving = sorted(
        key
        for key, route in _routes()
        for dependant in _dependants(route.dependant)
        for body in dependant.body_params
        for model in _reachable(list(_models(body.field_info.annotation)))
        if "responsible_user_id" in model.model_fields
    )
    assert receiving == [STEP_OPENING]


# --- 3. Puertos y servicio ------------------------------------------------------------------------


def _u03_modules() -> Iterator[types.ModuleType]:
    for package_name in U03_PACKAGES:
        package = importlib.import_module(f"vigia_platform.{package_name}")
        yield package
        for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
            yield importlib.import_module(info.name)


def _u03_protocols() -> list[type[Any]]:
    protocols: dict[str, type[Any]] = {}
    for module in _u03_modules():
        for name, value in vars(module).items():
            if (
                inspect.isclass(value)
                and getattr(value, "_is_protocol", False)
                and value.__module__ == module.__name__
            ):
                protocols[f"{module.__name__}.{name}"] = value
    return list(protocols.values())


def _method_violations(owner: type[Any], allowed: set[tuple[str, str]]) -> list[str]:
    violations: list[str] = []
    for name, member in inspect.getmembers(owner, inspect.isfunction):
        if name.startswith("_"):
            continue
        if AGGREGATION_BY_PERSON.search(name):
            violations.append(f"{owner.__qualname__}.{name}: agregación por persona")
        for parameter in inspect.signature(member).parameters:
            if RESPONSIBLE.search(parameter) and (name, parameter) not in allowed:
                violations.append(f"{owner.__qualname__}.{name}({parameter})")
    return violations


def test_no_u03_port_accepts_or_aggregates_by_responsible() -> None:
    protocols = _u03_protocols()
    names = {protocol.__name__ for protocol in protocols}
    # El recorrido ve los puertos del walk-test y los de las demás tareas de U-03.
    assert {"OcclusionTestsProvider", "ResponsibleLookup", "AssignedNodeLookup"} <= names
    violations = [v for protocol in protocols for v in _method_violations(protocol, set())]
    assert violations == []


def test_the_walk_test_service_only_takes_the_responsible_to_open_a_step() -> None:
    allowed = {("start_step", "responsible_user_id")}
    assert _method_violations(WalkTestService, allowed) == []
    assert "responsible_user_id" in inspect.signature(WalkTestService.start_step).parameters


# --- 4. SQL ----------------------------------------------------------------------------------


def _string_constants(path: Path) -> Iterator[tuple[int, str]]:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _sql_violations(paths: list[Path], pattern: re.Pattern[str]) -> list[str]:
    return [
        f"{path.relative_to(path.parents[2])}:{line}"
        for path in paths
        for line, value in _string_constants(path)
        if pattern.search(value)
    ]


def test_no_catalog_statement_groups_or_orders_by_responsible() -> None:
    sources = sorted((SRC / "catalog").rglob("*.py"))
    assert any(path.name == "walk_test_repository.py" for path in sources)
    assert _sql_violations(sources, SQL_BY_RESPONSIBLE) == []


def test_no_migration_indexes_responsible_user_id() -> None:
    migrations = sorted(MIGRATIONS.glob("gob_*.py"))
    assert _sql_violations(migrations, INDEX_ON_RESPONSIBLE) == []


def test_the_sql_search_detects_a_group_or_order_by_responsible() -> None:
    """La búsqueda no es ciega: reconoce las formas que prohíbe, en una o varias líneas."""
    for statement in (
        "SELECT responsible_user_id, sum(x) FROM catalog.walk_test_step"
        " GROUP BY responsible_user_id",
        "SELECT * FROM catalog.walk_test_step\n ORDER BY started_at, responsible_user_id",
        "SELECT sum(x) OVER (PARTITION BY responsible_user_id) FROM catalog.walk_test_step",
    ):
        assert SQL_BY_RESPONSIBLE.search(statement), statement
    assert not SQL_BY_RESPONSIBLE.search(
        "SELECT responsible_user_id FROM catalog.walk_test_step ORDER BY started_at, step_id"
    )
