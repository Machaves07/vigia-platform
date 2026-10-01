"""Resolución de la ruta y clase de ruta antes del enrutador (eslabón del pendiente nº 37).

Los pasos 4 a 9 necesitan saber qué ruta atenderá la petición antes de que FastAPI la enrute:
su límite de cuerpo, si es pública o exige permiso, si es un estático exento del limitador.
``RouteTable`` replica el orden del enrutador sobre las rutas efectivas de la aplicación (las de
``iter_declared_routes``): la primera cuya plantilla y método coinciden. La ruta de pantallas solo
coincide con navegaciones a ``SCREEN_ROUTES``, igual que en ``shared.api.static``.

Si la tabla y el enrutador discreparan alguna vez, no pasa nada: la declaración de la ruta que
FastAPI ejecuta exige que la cadena haya resuelto **esa** ruta (``declarations._Declared``) y, si
no, deniega con ``internal_error``.

**Clase de ruta** (pendiente nº 37): ``/api/nodes`` y todo lo que cuelga de ``/api/nodes/`` es
``node`` (rutas del contrato de U-03, autenticación mutua, sin cookie); lo demás, ``person``. La
clase elige el pool de la base (``shared.db.route_class_scope``) y deja fuera de las rutas de
nodos, por construcción, la sesión, su límite de tasa, la barrera anti-falsificación y el aviso de
tratamiento.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from starlette.datastructures import Headers
from starlette.routing import BaseRoute, compile_path

from vigia_platform.shared.api.declarations import (
    DeclaredRoute,
    UnauthenticatedRoute,
    iter_declared_routes,
)
from vigia_platform.shared.api.static import is_navigation, is_screen_path
from vigia_platform.shared.db import RouteClass

__all__ = ["NODE_PREFIX", "RATE_EXEMPT_ROUTES", "ResolvedRoute", "RouteTable", "route_class_of"]

NODE_PREFIX: Final = "/api/nodes"

RATE_EXEMPT_ROUTES: Final = frozenset(
    {
        UnauthenticatedRoute.APP_SCREEN,
        UnauthenticatedRoute.APP_ASSET,
        UnauthenticatedRoute.APP_VERSION,
        UnauthenticatedRoute.ROBOTS,
        UnauthenticatedRoute.HEALTH_READY,
    }
)
"""Fuera del limitador de la aplicación: los estáticos de la lista pública (los cubre el
cortafuegos, nº 10 ampliado) y la salud profunda, que es interna (un ``429`` sacaría la tarea del
balanceador)."""


def route_class_of(path: str) -> RouteClass:
    """``node`` para ``/api/nodes`` y ``/api/nodes/...``; ``person`` para todo lo demás."""
    if path == NODE_PREFIX or path.startswith(NODE_PREFIX + "/"):
        return RouteClass.NODE
    return RouteClass.PERSON


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """Una entrada de la tabla: la ruta declarada y su patrón."""

    declared: DeclaredRoute
    pattern: re.Pattern[str]


def _path(scope: Mapping[str, Any]) -> str:
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", ""))
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


class RouteTable:
    """Las rutas de la aplicación en el orden del enrutador."""

    def __init__(self, routes: Sequence[BaseRoute]) -> None:
        entries: list[ResolvedRoute] = []
        for declared in iter_declared_routes(routes):
            pattern, _, _ = compile_path(declared.path)
            entries.append(ResolvedRoute(declared, pattern))
        self._entries = tuple(entries)

    @property
    def routes(self) -> tuple[DeclaredRoute, ...]:
        return tuple(entry.declared for entry in self._entries)

    def resolve(self, scope: MutableMapping[str, Any]) -> DeclaredRoute | None:
        """La ruta que el enrutador ejecutará para ``scope``, o ``None`` si ninguna coincide del
        todo (FastAPI responderá ``not_found``)."""
        path = _path(scope)
        method = str(scope.get("method", ""))
        for entry in self._entries:
            declared = entry.declared
            if declared.navigation_only and not (
                method in ("GET", "HEAD")
                and is_screen_path(path)
                and is_navigation(Headers(scope=scope))
            ):
                continue
            if entry.pattern.match(path) is None:
                continue
            if declared.methods and method not in declared.methods:
                continue
            return declared
        return None
