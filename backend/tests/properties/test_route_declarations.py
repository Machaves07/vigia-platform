"""PR-NUC-37: toda ruta declara una clave de permiso existente o está en la lista pública cerrada;
el arranque falla ante cualquier ruta sin declaración (BR-NUC-91, BR-NUC-15; TASK-133).

- Metapropiedad sobre aplicaciones generadas: con cualquier mezcla de rutas (clave existente,
  inexistente o mal formada, entrada de la lista pública que coincide o no, ninguna o dos
  declaraciones, en la firma, en ``dependencies=`` o en el ``include_router``, anidadas), la
  fábrica lanza ``ApiStartupError`` **si y solo si** alguna ruta no cumple, y el mensaje, en
  español, nombra cada ruta que no cumple.
- ``detail_code`` (pendiente nº 33): una unidad que registra un valor sin prefijo registrado o que
  no es ``snake_case`` impide arrancar, con el oráculo independiente de la forma; una ruta que
  declara un ``detail_code`` sin registrar también.
- Rutas que no son de FastAPI (``Mount``, WebSocket) sin declaración impiden arrancar.
- En la petición, ``requires`` deniega sin autorizador (fallo cerrado) y delega en él si lo hay.

Solo rutas y claves generadas (NFR-CTR-43).
"""

from __future__ import annotations

import enum
import string
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from starlette.routing import Mount

from tests.api_support import World
from vigia_platform.shared.api.app import UnitRegistration, platform_units
from vigia_platform.shared.api.declarations import (
    UnauthenticatedRoute,
    requires,
    unauthenticated,
)
from vigia_platform.shared.api.errors import (
    DETAIL_CODE_PREFIXES,
    ApiError,
    ApiErrorCode,
    ApiStartupError,
    DetailCodeRegistry,
)

KNOWN = frozenset({"users.manage", "ledger.read", "platform.keys.rotate"})
REGISTERED_DETAIL = "fleet_node_mute"


class Kind(enum.Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    MALFORMED = "malformed"
    PUBLIC_MATCH = "public_match"
    PUBLIC_WRONG_PATH = "public_wrong_path"
    NONE = "none"
    DOUBLE = "double"
    UNREGISTERED_DETAIL = "unregistered_detail"


VALID_KINDS = frozenset({Kind.KNOWN, Kind.PUBLIC_MATCH})


class Where(enum.Enum):
    SIGNATURE = "signature"
    DEPENDENCIES = "dependencies"
    INCLUDE = "include"
    NESTED = "nested"


@dataclass(frozen=True)
class Spec:
    kind: Kind
    where: Where
    method: str
    path: str
    public: UnauthenticatedRoute | None


PUBLIC_CANDIDATES = [
    entry
    for entry in UnauthenticatedRoute
    if entry
    not in (
        UnauthenticatedRoute.HEALTH_LIVE,
        UnauthenticatedRoute.HEALTH_READY,
        UnauthenticatedRoute.APP_SCREEN,
        UnauthenticatedRoute.APP_ASSET,
        UnauthenticatedRoute.APP_VERSION,
        UnauthenticatedRoute.ROBOTS,
    )
]
"""Las rutas de salud ya las registra la unidad ``shared`` y los archivos estáticos la fábrica
(``shared.api.static``); la pantalla solo existe si la construcción trae ``index.html`` y solo en
la ruta de navegación (``test_app_screen_is_only_admitted_on_the_navigation_route``)."""


def _declaration(spec: Spec) -> list[Any]:
    kind = spec.kind
    if kind is Kind.KNOWN:
        return [requires("ledger.read")]
    if kind is Kind.UNKNOWN:
        return [requires("ledger.write_everything")]
    if kind is Kind.MALFORMED:
        return [requires("Ledger Read")]
    if kind is Kind.PUBLIC_MATCH:
        assert spec.public is not None
        return [unauthenticated(spec.public)]
    if kind is Kind.PUBLIC_WRONG_PATH:
        return [unauthenticated(UnauthenticatedRoute.AUTH_LOGIN)]
    if kind is Kind.DOUBLE:
        return [requires("ledger.read"), requires("users.manage")]
    if kind is Kind.UNREGISTERED_DETAIL:
        return [requires("ledger.read", detail_codes=["fleet_not_registered"])]
    return []


def _endpoint(name: str) -> Any:
    async def endpoint() -> dict[str, str]:
        return {"ok": name}

    endpoint.__name__ = name
    return endpoint


def _routers(specs: list[Spec]) -> tuple[APIRouter, ...]:
    routers: list[APIRouter] = []
    for index, spec in enumerate(specs):
        declared = _declaration(spec)
        endpoint = _endpoint(f"route_{index}")
        if spec.where is Where.SIGNATURE and declared:
            # Una sola declaración va en la firma; el resto, en dependencies=.
            first, rest = declared[0], declared[1:]

            async def signed(_: None = first, name: str = f"route_{index}") -> dict[str, str]:
                return {"ok": name}

            signed.__name__ = f"route_{index}"
            router = APIRouter()
            router.add_api_route(spec.path, signed, methods=[spec.method], dependencies=rest)
            routers.append(router)
        elif spec.where in (Where.INCLUDE, Where.NESTED):
            inner = APIRouter()
            inner.add_api_route(spec.path, endpoint, methods=[spec.method])
            outer = APIRouter()
            if spec.where is Where.NESTED:
                middle = APIRouter()
                middle.include_router(inner, dependencies=declared)
                outer.include_router(middle)
            else:
                outer.include_router(inner, dependencies=declared)
            routers.append(outer)
        else:
            router = APIRouter()
            router.add_api_route(spec.path, endpoint, methods=[spec.method], dependencies=declared)
            routers.append(router)
    return tuple(routers)


@st.composite
def route_specs(draw: st.DrawFn) -> list[Spec]:
    count = draw(st.integers(1, 6))
    publics = draw(st.permutations(PUBLIC_CANDIDATES))
    specs: list[Spec] = []
    for index in range(count):
        kind = draw(st.sampled_from(list(Kind)))
        where = draw(st.sampled_from(list(Where)))
        if kind is Kind.PUBLIC_MATCH and index < len(publics):
            entry = publics[index]
            specs.append(Spec(kind, where, entry.method, entry.path, entry))
            continue
        if kind is Kind.PUBLIC_MATCH:
            kind = Kind.NONE
        method = draw(st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE"]))
        segment = draw(st.text(string.ascii_lowercase, min_size=1, max_size=8))
        specs.append(Spec(kind, where, method, f"/generated/{index}/{segment}", None))
    return specs


def _build(specs: list[Spec], *, detail_codes: tuple[str, ...] = (REGISTERED_DETAIL,)) -> Any:
    unit = UnitRegistration("prueba", routers=_routers(specs), detail_codes=detail_codes)
    return World().app(units=(*platform_units(), unit), permissions=KNOWN)


@given(specs=route_specs())
def test_pr_nuc_37_startup_fails_iff_some_route_is_not_declared(specs: list[Spec]) -> None:
    invalid = [spec for spec in specs if spec.kind not in VALID_KINDS]
    if not invalid:
        app = _build(specs)
        assert isinstance(app, FastAPI)
        return
    with pytest.raises(ApiStartupError) as raised:
        _build(specs)
    message = str(raised.value)
    assert message.startswith("la aplicación no puede arrancar: ")
    for spec in invalid:
        assert spec.path in message or UnauthenticatedRoute.AUTH_LOGIN.path in message
    assert len(raised.value.problems) >= len(invalid)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_app_screen_is_only_admitted_on_the_navigation_route(method: str) -> None:
    # Seguimiento de la revisión de VIG-71: sin ``index.html`` la fábrica no registra la pantalla,
    # y una unidad no puede declarar ``APP_SCREEN`` en una ruta comodín corriente.
    router = APIRouter()
    router.add_api_route(
        UnauthenticatedRoute.APP_SCREEN.path,
        _endpoint("wildcard"),
        methods=[method],
        dependencies=[unauthenticated(UnauthenticatedRoute.APP_SCREEN)],
    )
    unit = UnitRegistration("prueba", routers=(router,))
    with pytest.raises(ApiStartupError) as raised:
        World().app(units=(*platform_units(), unit), permissions=KNOWN)
    assert "«APP_SCREEN» solo se admite en la ruta de pantallas" in str(raised.value)


def test_a_route_without_declaration_prevents_startup_with_a_spanish_message() -> None:
    router = APIRouter()

    @router.get("/sin-declarar")
    async def undeclared() -> dict[str, str]:
        return {}

    unit = UnitRegistration("prueba", routers=(router,))
    with pytest.raises(ApiStartupError) as raised:
        World().app(units=(*platform_units(), unit), permissions=KNOWN)
    assert raised.value.problems == (
        "la ruta GET /sin-declarar no declara su clave de permiso ni está en la lista pública "
        "cerrada (BR-NUC-91)",
    )


def test_a_key_outside_the_permission_matrix_prevents_startup() -> None:
    router = APIRouter()
    router.add_api_route(
        "/users", _endpoint("users"), methods=["GET"], dependencies=[requires("users.manage")]
    )
    unit = UnitRegistration("prueba", routers=(router,))
    World().app(units=(*platform_units(), unit), permissions=KNOWN)
    with pytest.raises(ApiStartupError, match=r"«users\.manage», que no está en la matriz"):
        World().app(units=(*platform_units(), unit), permissions=frozenset())


@pytest.mark.parametrize(
    "value",
    [
        "zone_without_node_detail",  # sin prefijo registrado
        "fleet",  # solo el prefijo, sin el guion bajo
        "fleet_",  # solo el prefijo
        "Fleet_node",  # no es snake_case
        "fleet_node-mute",
        "fleet_nodé",
        "fleet__node",
        "fleet_node_",
        " fleet_node",
        "fleet_node\n",
        "fleet_" + "x" * 60,  # más de 64 caracteres
    ],
)
def test_a_detail_code_without_a_registered_prefix_prevents_startup(value: str) -> None:
    unit = UnitRegistration("prueba", detail_codes=(value,))
    with pytest.raises(ApiStartupError) as raised:
        World().app(units=(*platform_units(), unit), permissions=KNOWN)
    assert "detail_code" in str(raised.value)


def test_the_six_prefixes_are_accepted() -> None:
    codes = tuple(prefix + "example_code" for prefix in DETAIL_CODE_PREFIXES)
    unit = UnitRegistration("prueba", detail_codes=codes)
    World().app(units=(*platform_units(), unit), permissions=KNOWN)


def _oracle_is_valid(value: str) -> bool:
    """Oráculo independiente de la forma de ``detail_code``: palabras separadas por un guion bajo,
    cada una de minúsculas ASCII y dígitos, la primera empieza por letra y es un prefijo."""
    if not 0 < len(value) <= 64:
        return False
    words = value.split("_")
    allowed = set(string.ascii_lowercase + string.digits)
    if len(words) < 2 or any(not word or set(word) - allowed for word in words):
        return False
    return words[0][0] in string.ascii_lowercase and words[0] + "_" in DETAIL_CODE_PREFIXES


detail_candidates = st.one_of(
    st.builds(
        lambda prefix, rest: prefix + rest,
        st.sampled_from([*DETAIL_CODE_PREFIXES, "zone_", "ledger_", "Fleet_", "app", ""]),
        st.text(string.ascii_lowercase + string.digits + "_-É ", max_size=70),
    ),
    st.text(max_size=80),
)


@given(value=detail_candidates)
def test_detail_code_registration_matches_the_oracle(value: str) -> None:
    registry = DetailCodeRegistry()
    if _oracle_is_valid(value):
        registry.register([value])
        assert value in registry
    else:
        with pytest.raises(ApiStartupError):
            registry.register([value])
        assert value not in registry


def test_a_route_declaring_an_unregistered_detail_code_prevents_startup() -> None:
    specs = [Spec(Kind.UNREGISTERED_DETAIL, Where.DEPENDENCIES, "GET", "/x", None)]
    with pytest.raises(ApiStartupError, match="«fleet_not_registered», que ninguna unidad"):
        _build(specs)


def test_non_fastapi_routes_prevent_startup() -> None:
    mount_router = APIRouter()
    mount_router.routes.append(Mount("/estaticos", app=FastAPI()))
    with pytest.raises(ApiStartupError, match="/estaticos no es una ruta de FastAPI"):
        World().app(units=(UnitRegistration("prueba", routers=(mount_router,)),))

    ws_router = APIRouter()

    @ws_router.websocket("/canal")
    async def channel(websocket: WebSocket) -> None:
        await websocket.close()

    with pytest.raises(ApiStartupError, match="/canal no es una ruta de FastAPI"):
        World().app(units=(UnitRegistration("prueba", routers=(ws_router,)),))


def test_a_public_entry_cannot_be_declared_twice() -> None:
    first, second = APIRouter(), APIRouter()
    for router in (first, second):
        router.add_api_route(
            "/auth/login",
            _endpoint("login"),
            methods=["POST"],
            dependencies=[unauthenticated(UnauthenticatedRoute.AUTH_LOGIN)],
        )
    unit = UnitRegistration("prueba", routers=(first, second))
    with pytest.raises(ApiStartupError, match="AUTH_LOGIN» de la lista pública está repetida"):
        World().app(units=(*platform_units(), unit))


# --- En la petición: denegación por defecto ------------------------------------------------


class AllowOnly:
    def __init__(self, allowed: str) -> None:
        self.allowed = allowed
        self.asked: list[str] = []

    async def authorize(self, request: Request, permission: str) -> None:
        self.asked.append(permission)
        if permission != self.allowed:
            raise ApiError(ApiErrorCode.NOT_FOUND)


def _protected_app(**runtime: Any) -> Any:
    router = APIRouter()
    router.add_api_route(
        "/ledger/records",
        _endpoint("records"),
        methods=["GET"],
        dependencies=[requires("ledger.read")],
    )
    unit = UnitRegistration("prueba", routers=(router,))
    return World().app(units=(*platform_units(), unit), permissions=KNOWN, runtime=runtime)


def test_without_an_authorizer_a_protected_route_denies() -> None:
    with TestClient(_protected_app()) as client:
        response = client.get("/ledger/records")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_the_authorizer_decides_each_request() -> None:
    allow = AllowOnly("ledger.read")
    with TestClient(_protected_app(authorizer=allow)) as client:
        assert client.get("/ledger/records").json() == {"ok": "records"}
    deny = AllowOnly("users.manage")
    with TestClient(_protected_app(authorizer=deny)) as client:
        response = client.get("/ledger/records")
    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert allow.asked == deny.asked == ["ledger.read"]
