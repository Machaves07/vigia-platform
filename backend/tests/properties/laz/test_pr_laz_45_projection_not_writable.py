"""PR-LAZ-45: ningún campo de estado de la proyección es escribible por una operación expuesta.

Metapropiedad (business-logic-model.md de U-04 §11; BR-LAZ-147; TASK-305). Ninguna decisión vive
solo en la proyección: todo campo de ``FindingState`` es función de los registros del sujeto.

1. **Esquema**: las columnas de ``loop.finding_state`` son exactamente los campos de
   ``FindingStateRow`` (lo que el pliegue deriva), y cada una es campo de estado del resumen o
   contabilidad de la proyección: no queda ninguna «a rellenar» por una operación.
2. **Catálogo de operaciones**: ninguna operación de U-04 (las rutas de la aplicación cuyo
   manejador vive en ``loop``, ``accreditation`` o ``notifications``, más los enrutadores de
   ``registration.py``) acepta en su ruta, su consulta, sus cabeceras o su cuerpo un campo con el
   nombre de un campo de estado. Hoy el catálogo está vacío salvo lo registrado; TASK-327 repite
   la comprobación sobre las 41 operaciones. Dos canarios demuestran que la comprobación caza un
   cuerpo con ``lifecycle_state`` y un parámetro ``overdue_days``.
3. **Escritura**: en ``src`` solo ``loop.projection.store`` escribe ``loop.finding_state`` y
   ``loop.episode`` (y ``tables.py`` los declara).
4. **Propiedad**: sobre ``loop_projection_commands``, la fila de cada sujeto plegando **solo** sus
   registros es la misma que plegando toda la planta.
"""

from __future__ import annotations

import dataclasses
import re
import typing
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import APIRouter, Query
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from hypothesis import given
from pydantic import BaseModel

from tests.properties.laz.strategies.projection import loop_projection_commands
from vigia_platform.accreditation.registration import accreditation_routers
from vigia_platform.loop.adapters.postgres.tables import FINDING_STATE
from vigia_platform.loop.projection.digest import FINDING_STATE_DIGEST_FIELDS
from vigia_platform.loop.projection.fold import (
    FindingStateRow,
    ProjectionInput,
    fold,
    subject_id_of,
)
from vigia_platform.loop.registration import loop_routers
from vigia_platform.notifications.registration import notifications_routers
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import iter_declared_routes

SRC: Final = Path(__file__).resolve().parents[3] / "src" / "vigia_platform"
U04_MODULES: Final = (
    "vigia_platform.loop",
    "vigia_platform.accreditation",
    "vigia_platform.notifications",
)
BOOKKEEPING: Final = frozenset(
    {"last_event_id", "last_record_id", "projection_updated_at", "projection_digest"}
)
ADDRESSING: Final = frozenset(
    {"organization_id", "plant_id", "zone_id", "subject_kind", "subject_id"}
)
"""Lo que localiza al sujeto: una operación lo recibe para decir **sobre qué** actúa."""
RECORD_INPUTS: Final = frozenset({"assignee_user_id"})
"""Datos que una operación recibe para escribir **su registro** (``action_assigned``) y que la
proyección copia de ese registro: no se escriben en la fila sino en el expediente."""
STATE_FIELDS: Final = (
    frozenset(FINDING_STATE_DIGEST_FIELDS) - ADDRESSING - RECORD_INPUTS | BOOKKEEPING
)
WRITERS: Final = {
    SRC / "loop" / "projection" / "store.py",
    SRC / "loop" / "adapters" / "postgres" / "tables.py",
}
WRITES_PROJECTION: Final = re.compile(
    r"\b(insert|update)\(\s*(FINDING_STATE|EPISODE)\b"
    r"|(?i:\b(insert\s+into|update)\s+loop\.(finding_state|episode)\b)"
)
"""Una escritura de las dos tablas: ``insert(FINDING_STATE)``, ``update(EPISODE)`` o el SQL."""


# --- 1. esquema -------------------------------------------------------------------------------


def test_finding_state_columns_are_exactly_what_the_fold_derives() -> None:
    columns = [column.name for column in FINDING_STATE.columns]
    fields = [field.name for field in dataclasses.fields(FindingStateRow)]
    assert columns == fields
    assert set(fields) == set(FINDING_STATE_DIGEST_FIELDS) | BOOKKEEPING


# --- 2. catálogo de operaciones ----------------------------------------------------------------


def _dependants(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _dependants(sub)


def _models(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in typing.get_args(annotation):
        yield from _models(argument)


def _reachable(models: Iterable[type[BaseModel]]) -> Iterator[type[BaseModel]]:
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


def writable_state_fields(route: APIRoute) -> list[str]:
    """Los campos de estado que ``route`` acepta como entrada, con dónde."""
    found: list[str] = []
    for dependant in _dependants(route.dependant):
        for kind, params in (
            ("ruta", dependant.path_params),
            ("consulta", dependant.query_params),
            ("cabecera", dependant.header_params),
            ("cookie", dependant.cookie_params),
        ):
            found += [
                f"{route.path}: {kind} {param.name}"
                for param in params
                if {param.name, str(param.alias)} & STATE_FIELDS
            ]
        for body in dependant.body_params:
            if body.name in STATE_FIELDS:
                found.append(f"{route.path}: cuerpo {body.name}")
            for model in _reachable(_models(body.field_info.annotation)):
                found += [
                    f"{route.path}: cuerpo {model.__name__}.{name}"
                    for name, field in model.model_fields.items()
                    if {name, str(field.alias)} & STATE_FIELDS
                ]
    return found


def _u04_routes() -> list[APIRoute]:
    routes = [
        declared.route
        for declared in iter_declared_routes(build_openapi_app().routes)
        if isinstance(declared.route, APIRoute)
        and declared.route.endpoint.__module__.startswith(U04_MODULES)
    ]
    for router in (*loop_routers(), *accreditation_routers(), *notifications_routers()):
        routes += [route for route in router.routes if isinstance(route, APIRoute)]
    return routes


def test_no_u04_operation_accepts_a_projection_state_field() -> None:
    violations = [found for route in _u04_routes() for found in writable_state_fields(route)]
    assert violations == []


class _Canary(BaseModel):
    reason_text: str
    nested: _CanaryInner


class _CanaryInner(BaseModel):
    lifecycle_state: str


_Canary.model_rebuild()


def _canary_routes() -> list[APIRoute]:
    router = APIRouter()

    @router.post("/canary/body")
    def body(payload: _Canary) -> None:  # pragma: no cover - nunca se llama
        return None

    @router.get("/canary/query")
    def query(overdue_days: int = Query()) -> None:  # pragma: no cover - nunca se llama
        return None

    @router.post("/canary/clean/{subject_id}")
    def clean(subject_id: uuid.UUID, assignee_user_id: uuid.UUID) -> None:  # pragma: no cover
        return None

    return [route for route in router.routes if isinstance(route, APIRoute)]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/canary/body", ["/canary/body: cuerpo _CanaryInner.lifecycle_state"]),
        ("/canary/query", ["/canary/query: consulta overdue_days"]),
        ("/canary/clean/{subject_id}", []),
    ],
)
def test_the_check_catches_a_state_field_in_body_or_query(path: str, expected: list[str]) -> None:
    routes = _canary_routes()
    matching = [route for route in routes if route.path == path]
    assert len(matching) == 1, [route.path for route in routes]
    assert writable_state_fields(matching[0]) == expected


# --- 3. escritura -------------------------------------------------------------------------------


def test_only_the_projection_store_writes_finding_state_and_episode() -> None:
    offenders = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path not in WRITERS and WRITES_PROJECTION.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == []


# --- 4. función de los registros del sujeto ----------------------------------------------------


@given(items=loop_projection_commands())
def test_each_row_is_a_function_of_its_subject_records(items: list[ProjectionInput]) -> None:
    for key, projection in fold(items).items():
        for subject, row in projection.findings.items():
            own = [
                item
                for item in items
                if (item.organization_id, item.plant_id) == key and subject_id_of(item) == subject
            ]
            (alone,) = fold(own).values()
            assert alone.findings == {subject: row}
            assert alone.episodes.get(subject) == projection.episodes.get(subject)
