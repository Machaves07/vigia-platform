"""Declaración obligatoria de cada ruta (BR-NUC-91, BR-NUC-15; PR-NUC-37; PAT-NUC-SEG-06).

Denegación por defecto: toda ruta declara **exactamente una** de estas dos cosas, como
dependencia de FastAPI:

- ``requires(clave)``: la clave de permiso que exige (``users.manage``…), que debe existir en la
  matriz de permisos que recibe la fábrica (TASK-125 la aporta desde ``identity.authz``);
- ``unauthenticated(UnauthenticatedRoute.X)``: la ruta está en la **lista cerrada**
  ``UnauthenticatedRoute`` (inicio de sesión y segundo factor, aceptación de invitación, salud
  superficial, claves públicas de ``checkpoint`` y hash del verificador, y la salud profunda,
  que es interna); método y plantilla de la ruta deben coincidir con los de la lista.

``check_routes`` recorre las rutas de la aplicación al construirla y devuelve un problema en
español por cada ruta sin declaración, con dos declaraciones, con una clave inexistente, que no
coincide con su entrada de la lista, o que no es una ruta de FastAPI (un ``Mount`` o una ruta de
Starlette sin declarar). La fábrica no arranca si hay alguno. Cada declaración puede enumerar los
``detail_code`` que la ruta responde; todos deben estar registrados.

En la petición, ``requires`` delega en el ``Authorizer`` de la aplicación (cadena de middleware,
TASK-134, y ``identity.authz.authorize``, TASK-125). Sin autorizador instalado **deniega** con
``unauthenticated``: nunca deja pasar.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from fastapi import Depends, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import BaseRoute

from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, DetailCodeRegistry

__all__ = [
    "PERMISSION_KEY",
    "Authorizer",
    "DeclaredRoute",
    "DenyAll",
    "Exposure",
    "RouteDeclaration",
    "UnauthenticatedRoute",
    "check_routes",
    "iter_declared_routes",
    "requires",
    "unauthenticated",
]

PERMISSION_KEY: Final = re.compile(r"[a-z][a-z_]{0,31}(?:\.[a-z][a-z_]{0,31}){1,3}")
"""Forma de una clave de permiso (``ledger.read``, ``platform.keys.rotate``)."""

_DOCS_PATHS: Final = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})
"""Rutas de documentación que FastAPI añade por su cuenta; solo existen fuera de producción."""


class Exposure(enum.StrEnum):
    PUBLIC = "public"
    """Detrás del balanceador público, sin sesión."""
    INTERNAL = "internal"
    """Sin sesión, pero el balanceador no la publica (salud profunda, BR-NUC-97)."""


class UnauthenticatedRoute(enum.Enum):
    """Lista pública cerrada (BR-NUC-91): método, plantilla y exposición.

    Añadir una entrada es un cambio revisado del código; las rutas del contrato para nodos
    (autenticación mutua) las añade U-03 con su propio mecanismo.
    """

    AUTH_LOGIN = ("POST", "/auth/login", Exposure.PUBLIC)
    AUTH_SECOND_FACTOR = ("POST", "/auth/second-factor", Exposure.PUBLIC)
    INVITATION_ACCEPT = ("POST", "/invitations/{token}/accept", Exposure.PUBLIC)
    HEALTH_LIVE = ("GET", "/health/live", Exposure.PUBLIC)
    CHECKPOINT_KEYS = ("GET", "/.well-known/vigia-checkpoint-keys", Exposure.PUBLIC)
    VERIFIER_HASH = ("GET", "/.well-known/vigia-verifier", Exposure.PUBLIC)
    HEALTH_READY = ("GET", "/health/ready", Exposure.INTERNAL)

    @property
    def method(self) -> str:
        return str(self.value[0])

    @property
    def path(self) -> str:
        return str(self.value[1])

    @property
    def exposure(self) -> Exposure:
        return Exposure(self.value[2])


class Authorizer(Protocol):
    """Autorización de una petición para una clave (TASK-134 y TASK-125 la implementan).

    Lanza ``ApiError`` (``unauthenticated``, ``forbidden``, ``not_found``…) si no autoriza.
    """

    async def authorize(self, request: Request, permission: str) -> None: ...


class DenyAll:
    """Autorizador por defecto: sin sesión ni matriz instaladas, nadie pasa."""

    async def authorize(self, request: Request, permission: str) -> None:
        raise ApiError(ApiErrorCode.UNAUTHENTICATED)


AUTHORIZER_STATE_KEY: Final = "vigia_authorizer"


@dataclass(frozen=True, slots=True)
class RouteDeclaration:
    """Lo que declara una ruta: su clave o su entrada de la lista cerrada, y sus ``detail_code``."""

    permission: str | None
    unauthenticated: UnauthenticatedRoute | None
    detail_codes: tuple[str, ...]


class _Declared:
    """Dependencia marcada: FastAPI la llama en cada petición; la fábrica la busca al arrancar."""

    def __init__(self, declaration: RouteDeclaration) -> None:
        self.declaration = declaration

    async def __call__(self, request: Request) -> None:
        permission = self.declaration.permission
        if permission is None:
            return
        authorizer = getattr(request.app.state, AUTHORIZER_STATE_KEY, None)
        if authorizer is None:
            raise ApiError(ApiErrorCode.UNAUTHENTICATED)
        await authorizer.authorize(request, permission)


def requires(permission: str, *, detail_codes: Iterable[str] = ()) -> Any:
    """Dependencia que declara (y en la petición exige) la clave ``permission``."""
    return Depends(_Declared(RouteDeclaration(permission, None, tuple(detail_codes))))


def unauthenticated(route: UnauthenticatedRoute, *, detail_codes: Iterable[str] = ()) -> Any:
    """Dependencia que declara la ruta como entrada ``route`` de la lista cerrada."""
    if not isinstance(route, UnauthenticatedRoute):
        raise TypeError("route debe ser UnauthenticatedRoute")
    return Depends(_Declared(RouteDeclaration(None, route, tuple(detail_codes))))


def _walk(dependant: Dependant | None) -> Iterator[Dependant]:
    if dependant is None:
        return
    yield dependant
    for child in dependant.dependencies:
        yield from _walk(child)


@dataclass(frozen=True, slots=True)
class DeclaredRoute:
    """Una ruta efectiva de la aplicación (con el prefijo y las dependencias de su inclusión)."""

    path: str
    methods: frozenset[str]
    is_api_route: bool
    declarations: tuple[RouteDeclaration, ...]


def iter_declared_routes(routes: Sequence[BaseRoute]) -> Iterator[DeclaredRoute]:
    """Rutas efectivas de ``routes``, también las de enrutadores incluidos (FastAPI ≥ 0.140).

    Las declaraciones salen de la firma de la ruta, de su ``dependencies=`` y de las
    dependencias de cada ``include_router`` que la contiene; una misma declaración cuenta una vez.
    """
    for context in iter_route_contexts(routes):
        original = context.original_route
        found: dict[int, RouteDeclaration] = {}
        dependants = [original.dependant if isinstance(original, APIRoute) else None]
        effective = getattr(context, "dependant", None)
        if effective is not None and effective is not dependants[0]:
            dependants.append(effective)
        for dependant in dependants:
            for node in _walk(dependant):
                if isinstance(node.call, _Declared):
                    found.setdefault(id(node.call), node.call.declaration)
        yield DeclaredRoute(
            path=str(context.path or getattr(original, "path", "") or "?"),
            methods=frozenset(context.methods or ()),
            is_api_route=isinstance(original, APIRoute),
            declarations=tuple(found.values()),
        )


def _route_problems(
    route: DeclaredRoute,
    known_permissions: Collection[str],
    detail_codes: DetailCodeRegistry,
) -> list[str]:
    methods = sorted(route.methods)
    where = f"{'/'.join(methods) or '?'} {route.path}"
    declarations = route.declarations
    if not declarations:
        return [
            f"la ruta {where} no declara su clave de permiso ni está en la lista pública "
            "cerrada (BR-NUC-91)"
        ]
    if len(declarations) > 1:
        return [f"la ruta {where} tiene {len(declarations)} declaraciones; debe tener una"]
    declaration = declarations[0]
    problems: list[str] = []
    if declaration.permission is not None:
        key = declaration.permission
        if not isinstance(key, str) or PERMISSION_KEY.fullmatch(key) is None:
            problems.append(f"la ruta {where} declara una clave de permiso mal formada")
        elif key not in known_permissions:
            problems.append(
                f"la ruta {where} exige la clave «{key}», que no está en la matriz de permisos "
                "(BR-NUC-15)"
            )
    elif (entry := declaration.unauthenticated) is not None and (
        route.path != entry.path or set(methods) - {"HEAD"} != {entry.method}
    ):
        problems.append(
            f"la ruta {where} dice ser «{entry.name}» de la lista pública, que es "
            f"{entry.method} {entry.path}"
        )
    problems.extend(
        f"la ruta {where} responde el detail_code «{code}», que ninguna unidad registró"
        for code in declaration.detail_codes
        if code not in detail_codes
    )
    return problems


def check_routes(
    routes: Sequence[BaseRoute],
    known_permissions: Collection[str],
    detail_codes: DetailCodeRegistry,
    *,
    docs_enabled: bool,
) -> list[str]:
    """Problemas de declaración de ``routes`` (vacío si todas cumplen BR-NUC-91)."""
    problems: list[str] = []
    seen: set[UnauthenticatedRoute] = set()
    for route in iter_declared_routes(routes):
        if not route.is_api_route:
            if docs_enabled and route.path in _DOCS_PATHS and not route.declarations:
                continue
            problems.append(
                f"la ruta {route.path} no es una ruta de FastAPI con declaración de permiso "
                "(BR-NUC-91)"
            )
            continue
        problems.extend(_route_problems(route, known_permissions, detail_codes))
        for declaration in route.declarations[:1]:
            entry = declaration.unauthenticated
            if entry is not None and entry in seen:
                problems.append(f"la entrada «{entry.name}» de la lista pública está repetida")
            if entry is not None:
                seen.add(entry)
    return problems
