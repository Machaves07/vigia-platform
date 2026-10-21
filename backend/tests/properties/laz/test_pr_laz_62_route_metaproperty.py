"""PR-LAZ-62: el catálogo de las 41 operaciones de U-04 no deja rutas sin aislar.

LC-LAZ §6 define 41 operaciones, aunque ``SUP-U05-35`` usa dos verbos sobre el mismo recurso de
preferencias. Por ello hay 42 rutas y 42 casos de aislamiento. Esta metapropiedad mantiene ambas
fronteras sincronizadas: una ruta nueva, una operación sin caso, una ruta pública o una clave de
instalador del proveedor rompe la prueba antes de llegar a producción.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final

import pytest

from tests.isolation.laz_cases import LAZ_CASES, LAZ_OPERATION_CASES
from tests.isolation.route_cases import Kind
from vigia_platform.identity.authz.matrix import permissions_of
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.api.declarations import DeclaredRoute, iter_declared_routes
from vigia_platform.shared.api.request_state import MIDDLEWARE_CHAIN, ChainStep
from vigia_platform.shared.context import Role

RouteKey = tuple[str, str]

U04_PREFIXES: Final = ("/loop", "/accreditation", "/notifications")
EXPECTED_OPERATIONS: Final = frozenset(f"SUP-U05-{number:02d}" for number in range(1, 42))
INSTALLER_PERMISSIONS: Final = frozenset(
    permission.value for permission in permissions_of(Role.PROVIDER_INSTALLER)
)
U04_NEW_PERMISSIONS: Final = frozenset(
    {
        "metrics.read",
        "exposure.read",
        "commitments.write",
        "vocabulary.manage",
        "closure_attachments.read",
    }
)
DENIED_REUSED_PERMISSIONS: Final = frozenset({"hierarchy.read", "notifications.read"})


def u04_routes() -> dict[RouteKey, DeclaredRoute]:
    """Las rutas de U-04 declaradas por la aplicación que genera la especificación."""
    routes: dict[RouteKey, DeclaredRoute] = {}
    for route in iter_declared_routes(build_openapi_app().routes):
        if not route.path.startswith(U04_PREFIXES):
            continue
        for method in route.methods:
            routes[(method, route.path)] = route
    return routes


def operation_case_keys(
    operations: Mapping[str, Collection[RouteKey]] = LAZ_OPERATION_CASES,
) -> set[RouteKey]:
    """Las rutas cubiertas por el catálogo de operaciones, sin contar dos veces preferencias."""
    return {key for keys in operations.values() for key in keys}


def uncovered_operation_cases(
    routes: Collection[RouteKey],
    operations: Mapping[str, Collection[RouteKey]] = LAZ_OPERATION_CASES,
) -> list[str]:
    """Rutas de U-04 que no tienen una operación con caso de aislamiento."""
    missing = set(routes) - operation_case_keys(operations)
    return sorted(f"{method} {path}" for method, path in missing)


def test_pr_laz_62_every_operation_has_a_private_declared_route_and_case() -> None:
    routes = u04_routes()
    case_by_key = dict(LAZ_CASES)

    assert set(LAZ_OPERATION_CASES) == EXPECTED_OPERATIONS
    assert len(LAZ_OPERATION_CASES) == 41
    assert set(routes) == operation_case_keys()
    assert set(case_by_key) == set(routes)
    assert len(case_by_key) == 42  # SUP-U05-35 tiene GET y PUT.
    assert uncovered_operation_cases(routes) == []
    assert ChainStep.CSRF in MIDDLEWARE_CHAIN

    for key, route in routes.items():
        (declaration,) = route.declarations
        assert declaration.permission is not None, key
        assert declaration.unauthenticated is None, key
        assert declaration.session is None, key
        assert declaration.node is None, key
        # Las cinco claves propias de U-04 no están en la columna del instalador. Dos claves
        # heredadas sí aparecen en ella, pero sus servicios las cierran bajo concesión (BR-LAZ-142).
        if declaration.permission in INSTALLER_PERMISSIONS:
            assert declaration.permission in DENIED_REUSED_PERMISSIONS, key
        assert case_by_key[key].kind in (Kind.RESOURCE, Kind.OWN), key
        assert case_by_key[key].call is not None, key
    assert not U04_NEW_PERMISSIONS & INSTALLER_PERMISSIONS


def test_pr_laz_62_names_a_route_when_its_operation_loses_the_case() -> None:
    routes = u04_routes()
    without = dict(LAZ_OPERATION_CASES)
    removed = without.pop("SUP-U05-41")

    expected = [f"{method} {path}" for method, path in sorted(removed)]
    assert uncovered_operation_cases(routes, without) == expected
    with pytest.raises(AssertionError, match="monthly-packages"):
        assert not uncovered_operation_cases(routes, without)
