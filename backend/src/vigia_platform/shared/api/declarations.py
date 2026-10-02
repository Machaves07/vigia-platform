"""Declaración obligatoria de cada ruta (BR-NUC-91, BR-NUC-15; PR-NUC-37; PAT-NUC-SEG-06).

Denegación por defecto: toda ruta declara **exactamente una** de estas tres cosas, como
dependencia de FastAPI:

- ``requires(clave)``: la clave de permiso que exige (``users.manage``…), que debe existir en la
  matriz de permisos que recibe la fábrica (TASK-125 la aporta desde ``identity.authz``);
- ``authenticated(SessionRoute.X)``: la ruta solo exige una **sesión utilizable**, sin clave de la
  matriz (``business-logic-model.md`` §10.2, «sesión»: ``GET /me``, las sesiones propias, el cambio
  de contraseña y la aceptación del aviso). Es otra **lista cerrada** (``SessionRoute``): lo que
  hace cada una solo afecta a la persona de la sesión. La cadena valida la sesión igual que en una
  ruta con permiso; sin sesión válida, ``unauthenticated``;
- ``unauthenticated(UnauthenticatedRoute.X)``: la ruta está en la **lista cerrada**
  ``UnauthenticatedRoute`` (inicio de sesión, segundo factor y su inscripción con la sesión
  pendiente, cierre de sesión, aceptación de invitación, salud superficial, claves públicas de
  ``checkpoint`` y hash del verificador, la salud profunda, que es interna, y los estáticos de la
  aplicación de página única: pantallas, ``/assets/*``, ``/version.json`` y ``/robots.txt``);
  método y plantilla de la ruta deben coincidir con los de la lista.

``check_routes`` recorre las rutas de la aplicación al construirla y devuelve un problema en
español por cada ruta sin declaración, con dos declaraciones, con una clave inexistente, que no
coincide con su entrada de la lista, o que no es una ruta de FastAPI (un ``Mount`` o una ruta de
Starlette sin declarar). La fábrica no arranca si hay alguno. Cada declaración puede enumerar los
``detail_code`` que la ruta responde; todos deben estar registrados.

En la petición, ``requires`` delega en el ``Authorizer`` de la aplicación (cadena de middleware,
TASK-134, y ``identity.authz.authorize``, TASK-125). Sin autorizador instalado **deniega** con
``unauthenticated``: nunca deja pasar.

La declaración es el último eslabón de la cadena fija (``request_state.ChainStep.AUTHORIZATION``):
antes de autorizar, toda ruta (también las públicas) exige que la petición haya pasado por los
diez eslabones anteriores en su orden y que la ruta que resolvió la cadena sea esta. Si no,
``internal_error``: una cadena desordenada, incompleta o que resolvió otra ruta nunca deja pasar.

``body_limit(n)`` (opcional, una por ruta) declara un límite de cuerpo propio en bytes para las
rutas del contrato de U-03 (BR-CTR-12); sin ella rige el de 1 MB de la cadena.

``APP_SCREEN`` (``/{screen_path:path}``) solo se admite en una ruta que coincide únicamente con
navegaciones a pantallas (``navigation_only``: la ruta de ``shared.api.static``, reconocida por su
clase exacta y no por un atributo que otra unidad podría copiar): en cualquier otra sería un
comodín público delante de la API.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from fastapi import Depends, Request
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import BaseRoute

from vigia_platform.shared.api.errors import ApiError, ApiErrorCode, DetailCodeRegistry
from vigia_platform.shared.api.request_state import MIDDLEWARE_CHAIN, request_state
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "MAX_ROUTE_BODY_LIMIT_BYTES",
    "PERMISSION_KEY",
    "Authorizer",
    "DeclaredRoute",
    "DenyAll",
    "Exposure",
    "RouteDeclaration",
    "SessionRoute",
    "UnauthenticatedRoute",
    "authenticated",
    "body_limit",
    "check_routes",
    "iter_declared_routes",
    "requires",
    "unauthenticated",
]

PERMISSION_KEY: Final = re.compile(r"[a-z][a-z_]{0,31}(?:\.[a-z][a-z_]{0,31}){1,3}")
"""Forma de una clave de permiso (``ledger.read``, ``platform.keys.rotate``)."""
MAX_ROUTE_BODY_LIMIT_BYTES: Final = 16 * 1024 * 1024
"""Tope de un límite de cuerpo propio (``body_limit``) ``[objetivo propio]``."""

_log = get_logger("shared.api.declarations")

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
    # Solo con la sesión **pendiente** (BR-NUC-22): la ruta la valida para ese fin y nada más.
    AUTH_SECOND_FACTOR_ENROLL = ("POST", "/auth/second-factor/enroll", Exposure.PUBLIC)
    # Cierra la sesión de la cookie aunque ya haya vencido o falte el aviso (BR-NUC-27).
    AUTH_LOGOUT = ("POST", "/auth/logout", Exposure.PUBLIC)
    INVITATION_ACCEPT = ("POST", "/invitations/{token}/accept", Exposure.PUBLIC)
    HEALTH_LIVE = ("GET", "/health/live", Exposure.PUBLIC)
    CHECKPOINT_KEYS = ("GET", "/.well-known/vigia-checkpoint-keys", Exposure.PUBLIC)
    VERIFIER_HASH = ("GET", "/.well-known/vigia-verifier", Exposure.PUBLIC)
    HEALTH_READY = ("GET", "/health/ready", Exposure.INTERNAL)
    # Aplicación de página única (pendiente nº 10 ampliado, D-3; ``shared.api.static``). La
    # pantalla solo responde a navegaciones y a las rutas de ``static.SCREEN_ROUTES``.
    APP_SCREEN = ("GET", "/{screen_path:path}", Exposure.PUBLIC)
    APP_ASSET = ("GET", "/assets/{asset_path:path}", Exposure.PUBLIC)
    APP_VERSION = ("GET", "/version.json", Exposure.PUBLIC)
    ROBOTS = ("GET", "/robots.txt", Exposure.PUBLIC)

    @property
    def method(self) -> str:
        return str(self.value[0])

    @property
    def path(self) -> str:
        return str(self.value[1])

    @property
    def exposure(self) -> Exposure:
        return Exposure(self.value[2])


class SessionRoute(enum.Enum):
    """Rutas que exigen una sesión utilizable y ninguna clave de la matriz (§10.2, «sesión»).

    Lista cerrada: cada una actúa solo sobre la persona de la sesión (sus datos, sus sesiones, su
    contraseña, su aceptación del aviso). Añadir una entrada es un cambio revisado del código.
    """

    ME = ("GET", "/me")
    AUTH_SESSIONS = ("GET", "/auth/sessions")
    AUTH_SESSIONS_CLOSE_OTHERS = ("POST", "/auth/sessions/close-others")
    AUTH_PASSWORD = ("POST", "/auth/password")
    PRIVACY_NOTICE_ACCEPT = ("POST", "/privacy-notice/accept")

    @property
    def method(self) -> str:
        return str(self.value[0])

    @property
    def path(self) -> str:
        return str(self.value[1])


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
    """Lo que declara una ruta: su clave o su entrada de una lista cerrada, y sus detail_code."""

    permission: str | None
    unauthenticated: UnauthenticatedRoute | None
    detail_codes: tuple[str, ...]
    session: SessionRoute | None = None

    @property
    def requires_session(self) -> bool:
        """La cadena construye el contexto de la sesión: clave de permiso o ``SessionRoute``."""
        return self.permission is not None or self.session is not None


class _Declared:
    """Dependencia marcada: FastAPI la llama en cada petición; la fábrica la busca al arrancar."""

    def __init__(self, declaration: RouteDeclaration) -> None:
        self.declaration = declaration

    def _chain_completed(self, request: Request) -> bool:
        """¿Pasó la petición por los diez eslabones, en orden, y resolvió esta misma ruta?"""
        state = request_state(request.scope)
        route = state.route
        return (
            tuple(state.trace) == MIDDLEWARE_CHAIN
            and route is not None
            and any(found is self.declaration for found in route.declarations)
        )

    async def __call__(self, request: Request) -> None:
        if not self._chain_completed(request):
            _log.error("petición sin la cadena de middleware completa: se deniega")
            raise ApiError(ApiErrorCode.INTERNAL_ERROR)
        if self.declaration.session is not None:
            if request_state(request.scope).session is None:
                raise ApiError(ApiErrorCode.UNAUTHENTICATED)
            return
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


def authenticated(route: SessionRoute, *, detail_codes: Iterable[str] = ()) -> Any:
    """Dependencia que declara la ruta como entrada ``route`` de las rutas de sesión."""
    if not isinstance(route, SessionRoute):
        raise TypeError("route debe ser SessionRoute")
    return Depends(_Declared(RouteDeclaration(None, None, tuple(detail_codes), session=route)))


class _BodyLimit:
    """Dependencia marcada con el límite de cuerpo propio de una ruta; no hace nada al llamarla."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes

    async def __call__(self) -> None:
        return None


def body_limit(max_bytes: int) -> Any:
    """Dependencia que declara el límite de cuerpo propio de la ruta (rutas del contrato)."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ROUTE_BODY_LIMIT_BYTES:
        raise ValueError(f"el límite de cuerpo debe ser de 1 a {MAX_ROUTE_BODY_LIMIT_BYTES} bytes")
    return Depends(_BodyLimit(max_bytes))


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
    body_limits: tuple[int, ...] = ()
    """Límites de cuerpo propios declarados (``body_limit``); como mucho uno es válido."""
    navigation_only: bool = False
    """La ruta solo coincide con navegaciones a pantallas (``shared.api.static``)."""
    route: BaseRoute | None = field(default=None, compare=False, repr=False)
    """La ruta original de FastAPI o Starlette."""


def iter_declared_routes(routes: Sequence[BaseRoute]) -> Iterator[DeclaredRoute]:
    """Rutas efectivas de ``routes``, también las de enrutadores incluidos (FastAPI ≥ 0.140).

    Las declaraciones salen de la firma de la ruta, de su ``dependencies=`` y de las
    dependencias de cada ``include_router`` que la contiene; una misma declaración cuenta una vez.
    """
    for context in iter_route_contexts(routes):
        original = context.original_route
        found: dict[int, RouteDeclaration] = {}
        limits: dict[int, int] = {}
        dependants = [original.dependant if isinstance(original, APIRoute) else None]
        effective = getattr(context, "dependant", None)
        if effective is not None and effective is not dependants[0]:
            dependants.append(effective)
        for dependant in dependants:
            for node in _walk(dependant):
                if isinstance(node.call, _Declared):
                    found.setdefault(id(node.call), node.call.declaration)
                elif isinstance(node.call, _BodyLimit):
                    limits.setdefault(id(node.call), node.call.max_bytes)
        yield DeclaredRoute(
            path=str(context.path or getattr(original, "path", "") or "?"),
            methods=frozenset(context.methods or ()),
            is_api_route=isinstance(original, APIRoute),
            declarations=tuple(found.values()),
            body_limits=tuple(limits.values()),
            navigation_only=_is_navigation_route(original),
            route=original,
        )


def _is_navigation_route(route: BaseRoute) -> bool:
    """La ruta de pantallas de ``shared.api.static``, reconocida por su clase exacta (VIG-78)."""
    # Importación diferida: ``shared.api.static`` importa este módulo para declararse.
    from vigia_platform.shared.api.static import is_navigation_route

    return is_navigation_route(route)


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
    if len(route.body_limits) > 1:
        problems.append(f"la ruta {where} declara {len(route.body_limits)} límites de cuerpo")
    if (declaration.unauthenticated is UnauthenticatedRoute.APP_SCREEN) != route.navigation_only:
        problems.append(
            f"la ruta {where}: «APP_SCREEN» solo se admite en la ruta de pantallas, que coincide "
            "únicamente con navegaciones (D-3)"
        )
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
    elif (session := declaration.session) is not None and (
        route.path != session.path or set(methods) - {"HEAD"} != {session.method}
    ):
        problems.append(
            f"la ruta {where} dice ser «{session.name}» de las rutas de sesión, que es "
            f"{session.method} {session.path}"
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
    seen: set[UnauthenticatedRoute | SessionRoute] = set()
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
            entry = declaration.unauthenticated or declaration.session
            if entry is not None and entry in seen:
                problems.append(f"la entrada «{entry.name}» de una lista cerrada está repetida")
            if entry is not None:
                seen.add(entry)
    return problems
