"""PR-LAZ-01 (metapropiedad): ninguna operación del lazo tiene a una persona por sujeto (TASK-307;
BR-LAZ-02 y 03; P3, H-20).

Sobre el **catálogo de operaciones registrado** (``loop_routers()``, lo que la raíz monta en
``/loop/*``) y sus **esquemas de salida**: ningún parámetro de consulta, de ruta ni del cuerpo, y
ningún campo de la respuesta (recursivamente, también en los esquemas anidados), lleva en su
nombre una palabra cuyo sujeto sea una persona (identificador, nombre, documento, turno, equipo o
cuadrilla) ni un contador de personas. La ausencia es **estructural**: la prueba lee las rutas,
no una lista escrita a mano. VIG-242 (TASK-327) la repite sobre el catálogo completo de U-04.

Los filtros de la bandeja son exactamente los cinco de BR-LAZ-02 (``InboxFilters``) y la
metapropiedad **falla** con una ruta sonda que acepta o devuelve un campo de persona, con
cualquier palabra de la lista en cualquier posición del nombre.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Iterator
from typing import Any, Final

from fastapi import APIRouter, Query
from fastapi.routing import APIRoute
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, create_model

from vigia_platform.loop.adapters.postgres.inbox_query import InboxFilters
from vigia_platform.loop.registration import loop_routers

PERSON_WORDS: Final = frozenset(
    {
        # persona, en general
        "person",
        "persons",
        "people",
        "persona",
        "personas",
        "individual",
        "worker",
        "workers",
        "trabajador",
        "trabajadores",
        "employee",
        "employees",
        "empleado",
        "empleados",
        "operario",
        "operarios",
        "occupant",
        "occupants",
        "ocupante",
        "headcount",
        # identificadores de persona
        "name",
        "names",
        "nombre",
        "nombres",
        "surname",
        "apellido",
        "document",
        "documento",
        "cedula",
        "passport",
        "badge",
        "carnet",
        "face",
        "faces",
        "rostro",
        "biometric",
        # organización del trabajo
        "shift",
        "shifts",
        "turno",
        "turnos",
        "team",
        "teams",
        "equipo",
        "crew",
        "crews",
        "cuadrilla",
        "cuadrillas",
        "squad",
        "brigada",
    }
)
"""Palabras cuyo sujeto es una persona (BR-LAZ-02) o un grupo de personas (BR-LAZ-03)."""

_WORD: Final = re.compile(r"[a-z]+")


def _words(name: str) -> set[str]:
    """Las palabras de un nombre ``snake_case`` o ``camelCase``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return set(_WORD.findall(spaced.lower()))


def _schema_names(schema: Any) -> Iterator[str]:
    """Los nombres de propiedad de un esquema JSON y de todos sus esquemas anidados."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                yield from value
            yield from _schema_names(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _schema_names(value)


def _model_names(annotation: Any) -> Iterator[str]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield from _schema_names(annotation.model_json_schema())


def route_names(route: APIRoute) -> Iterator[tuple[str, str]]:
    """``(lugar, nombre)`` de cada parámetro y de cada campo de la respuesta de ``route``."""
    dependant = route.dependant
    for param in (*dependant.query_params, *dependant.path_params, *dependant.header_params):
        yield "parameter", param.alias or param.name
    for param in dependant.body_params:
        yield "body", param.alias or param.name
        for name in _model_names(param.field_info.annotation):
            yield "body", name
    for name in _model_names(route.response_model):
        yield "response", name


SIGNER_FIELDS: Final = frozenset(
    {
        ("GET", "/loop/plants/{plant_id}/configuration", "response", "display_name"),
        ("PUT", "/loop/plants/{plant_id}/configuration", "response", "display_name"),
        # El firmante del análisis de causa raíz (``SignerOut``, VIG-214).
        (
            "POST",
            "/loop/findings/{finding_state_id}/root-cause-analyses",
            "response",
            "display_name",
        ),
        (
            "PUT",
            "/loop/root-cause-analyses/{analysis_id}/steps/{step_index}",
            "response",
            "display_name",
        ),
        ("POST", "/loop/root-cause-analyses/{analysis_id}/signature", "response", "display_name"),
        # La declaración de privacidad de un adjunto de evidencia (VIG-223, TASK-310): afirma que
        # **ninguna** persona identificable aparece; es la guarda de P3, no un dato de persona.
        ("POST", "/loop/actions/{action_id}/evidence", "body", "no_identifiable_person_declared"),
        # El hash del **documento de la política** de la planta (VIG-228, TASK-312; DE §2.7
        # ``policy_document_sha256``): el «documento» es el texto del abogado que la planta firmó,
        # no un documento de identidad de una persona observada.
        (
            "POST",
            "/loop/findings/{finding_state_id}/unclosable",
            "response",
            "policy_document_sha256",
        ),
    }
)
"""Excepciones nombradas una a una: el **firmante** de un cambio (P8) es un usuario de la
plataforma que decide, nunca una persona observada (DE §2.2, ``assignee_user_id``), la
declaración de que un adjunto no muestra a nadie es la guarda de P3, y el hash del documento de la
política de la planta cita el texto que el abogado firmó. La bandeja no tiene ninguna; VIG-242
decide sobre el catálogo completo."""


def violations(routes: Iterable[Any]) -> list[str]:
    """Las rutas de ``/loop/*`` con un parámetro o un campo de persona, como ``MÉTODO ruta: …``."""
    found: list[str] = []
    for route in routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/loop/"):
            continue
        for method in sorted(route.methods):
            for place, name in route_names(route):
                words = _words(name) & PERSON_WORDS
                if words and (method, route.path, place, name) not in SIGNER_FIELDS:
                    found.append(
                        f"{method} {route.path}: {place} «{name}» ({', '.join(sorted(words))})"
                    )
    return found


REGISTERED: Final = tuple(
    route for router in loop_routers() for route in router.routes if isinstance(route, APIRoute)
)
"""El catálogo de operaciones de ``/loop/*`` que la raíz monta."""


def test_pr_laz_01_no_registered_loop_operation_has_a_person_parameter_or_field() -> None:
    # La metapropiedad mira de verdad: la bandeja está registrada y tiene parámetros y campos.
    inbox = [r for r in REGISTERED if r.path == "/loop/findings" and "GET" in r.methods]
    assert len(inbox) == 1
    names = {name for _, name in route_names(inbox[0])}
    assert {"zone_id", "family", "tier", "lifecycle_state", "from", "to", "cursor"} <= names
    assert {"items", "zones", "partitions", "occurred_at", "notices"} <= names
    assert violations(REGISTERED) == []
    # Las excepciones existen y no tapan nada de la bandeja.
    assert all(path != "/loop/findings" for _, path, _, _ in SIGNER_FIELDS)
    for method, path, place, name in SIGNER_FIELDS:
        (route,) = [r for r in REGISTERED if r.path == path and method in r.methods]
        assert (place, name) in set(route_names(route))


def test_the_inbox_filters_are_exactly_the_five_criteria_of_br_laz_02() -> None:
    assert [field.name for field in dataclasses.fields(InboxFilters)] == [
        "zone_ids",
        "family",
        "tier",
        "lifecycle_states",
        "period_from",
        "period_to",
    ]


def _probe(parameter: str, field: str) -> APIRouter:
    out = create_model("ProbeOut", **{field: (int, 0), "zone_id": (str, "")})  # type: ignore[call-overload]
    router = APIRouter()

    async def probe(value: str | None = Query(None, alias=parameter)) -> Any:
        return out()

    router.add_api_route("/loop/probe", probe, methods=["GET"], response_model=out)
    return router


@st.composite
def _person_name(draw: st.DrawFn) -> str:
    word = draw(st.sampled_from(sorted(PERSON_WORDS)))
    prefix = draw(st.sampled_from(("", "zone_", "current_", "assigned_")))
    suffix = draw(st.sampled_from(("", "_id", "_count", "_code", "_ids")))
    return prefix + word + suffix


@given(_person_name(), _person_name())
def test_pr_laz_01_fails_with_a_probe_route_that_violates_it(parameter: str, field: str) -> None:
    # El catálogo registrado ya está limpio (prueba de arriba): basta con la sonda.
    found = violations(_probe(parameter, field).routes)
    assert [f for f in found if f.startswith(f"GET /loop/probe: parameter «{parameter}»")], found
    assert [f for f in found if f.startswith(f"GET /loop/probe: response «{field}»")], found
    assert all(f.startswith("GET /loop/probe") for f in found)


def test_a_probe_with_zone_level_names_only_passes() -> None:
    assert violations((*REGISTERED, *_probe("zone_code", "finding_count").routes)) == []


def test_the_signer_exception_never_covers_the_same_name_on_another_route() -> None:
    found = violations(_probe("zone_id", "display_name").routes)
    assert found == ["GET /loop/probe: response «display_name» (name)"]
