"""Aplicación de página única servida desde ``vigia-api`` (pendiente nº 10 ampliado, D-3; nº 12).

U-05 construye la aplicación y la imagen la copia en ``/app/static`` (``AppConfig.static_dir``).
Este módulo la sirve desde el mismo origen que la API, sin ocultar ninguna ruta de la API:

- ``/assets/*``: recursos con hash de contenido; el ``.br`` o el ``.gz`` precomprimido según
  ``Accept-Encoding``, con ``Content-Encoding``, ``Vary: Accept-Encoding`` y ``ETag`` fuerte por
  hash; ``Cache-Control: public, max-age=31536000, immutable``.
- ``/version.json``: ``{app_version, contract_tag, built_at}`` de la construcción; ``no-store``.
- ``/robots.txt``: ``User-agent: *`` / ``Disallow: /`` (fijo: la aplicación es privada);
  ``no-store``.
- ``/`` y ``SCREEN_ROUTES``: ``index.html``, **solo a peticiones de navegación** (D-3);
  ``no-store``.

Reglas:

- **Navegación** (``is_navigation``): ``Sec-Fetch-Mode: navigate``, o ``Accept`` con ``text/html``
  y sin ``application/json``. Cualquier otra petición a una ruta de pantalla (``/concessions``,
  ``/zones/*``…) va a la API: la ruta de pantalla no coincide y el enrutador sigue buscando.
- **Precedencia de la API**: la ruta de pantalla va delante de las de la API pero solo coincide
  con navegaciones a ``SCREEN_ROUTES``; los recursos, ``version.json`` y ``robots.txt`` van detrás
  de las rutas de la API. Un archivo de ``static/`` fuera de ``assets/`` nunca se sirve, así que
  un ``static/me`` no oculta ``GET /me``.
- Solo se sirve lo que ``StaticSite.load`` catalogó al construir la aplicación (archivos
  regulares, sin enlaces simbólicos, con nombres de la forma cerrada y sin mapas de fuentes
  ``.map``); cualquier otro recurso responde ``not_found``. La raíz de la tarea es de solo
  lectura, así que el catálogo no cambia.
- Las cabeceras de seguridad (política de contenido, ``nosniff``…) las pone la cadena de
  middleware en toda respuesta, también en estas (``shared.api.middleware``).
- Las cuatro rutas están en la lista pública cerrada de BR-NUC-91 (``UnauthenticatedRoute``).
- ``/assets/*`` no deja línea en el registro JSON de la aplicación (``is_request_logged``): queda
  en el registro de acceso del balanceador. Ninguna ruta de este módulo escribe la ruta pedida
  en un registro (``/invitations/<token>`` es una pantalla).
- ``StaticSite.metric_version`` es la dimensión ``app_version`` de la métrica de salud
  ``health_ready`` (LC-NUC-31, pendiente nº 12): una por release, baja cardinalidad.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
import stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from starlette.routing import Match
from starlette.types import Scope

from vigia_platform.shared.api.declarations import UnauthenticatedRoute, unauthenticated
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.redaction import redact_text

__all__ = [
    "ASSET_CACHE_CONTROL",
    "DEFAULT_STATIC_DIR",
    "NO_STORE",
    "ROBOTS_TXT",
    "SCREEN_ROUTES",
    "UNKNOWN_VERSION",
    "AppRelease",
    "Encoding",
    "StaticSite",
    "StaticSiteInvalid",
    "choose_encoding",
    "is_navigation",
    "is_request_logged",
    "is_screen_path",
    "static_routers",
]

DEFAULT_STATIC_DIR: Final = Path("/app/static")
"""Donde la etapa ``frontend-build`` de la imagen deja ``frontend/dist`` (U-05)."""
ASSET_CACHE_CONTROL: Final = "public, max-age=31536000, immutable"
NO_STORE: Final = "no-store"
ROBOTS_TXT: Final = b"User-agent: *\nDisallow: /\n"
UNKNOWN_VERSION: Final = "unknown"
"""``app_version`` cuando la imagen no trae ``version.json`` (o su versión no es publicable)."""

MAX_INDEX_BYTES: Final = 1 << 20
MAX_VERSION_BYTES: Final = 64 << 10
MAX_ASSETS: Final = 10_000
MAX_ASSET_DEPTH: Final = 8
_HASH_CHUNK: Final = 1 << 16
_MAX_HEADER_ITEMS: Final = 64

SCREEN_ROUTES: Final[tuple[str, ...]] = (
    "/",
    "/login",
    "/home",
    "/findings/*",
    "/zones/*",
    "/fleet/*",
    "/metrics/*",
    "/exports",
    "/notifications/*",
    "/transparency/*",
    "/admin/*",
    "/account",
    "/concessions",
    "/provider/*",
    "/integrity",
    "/review-queue",
    "/invitations/*",
)
"""Rutas de pantalla de U-05 (``plataforma-aplicacion/infrastructure-design.md`` §3).

``/x/*`` admite ``/x`` y ``/x/<segmentos>``: una pantalla desconocida dentro de la aplicación
recibe ``index.html`` y la propia aplicación muestra «no disponible en su alcance»."""

_SEGMENT: Final = r"[A-Za-z0-9_~][A-Za-z0-9._~-]{0,127}"
_ASSET_PATH: Final = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,{MAX_ASSET_DEPTH - 1}}}$")
_ASSET_NAME: Final = re.compile(rf"^{_SEGMENT}$")
"""Un segmento nunca empieza por ``.``: ``..`` y los archivos ocultos quedan fuera."""


def _screen_pattern() -> re.Pattern[str]:
    alternatives: list[str] = []
    for screen in SCREEN_ROUTES:
        if screen.endswith("/*"):
            base = re.escape(screen[:-2])
            alternatives.append(rf"{base}(?:/{_SEGMENT}){{0,16}}/?")
        else:
            alternatives.append(re.escape(screen))
    return re.compile(rf"^(?:{'|'.join(alternatives)})$")


_SCREEN: Final = _screen_pattern()

_APP_VERSION: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_CONTRACT_TAG: Final = _APP_VERSION
_BUILT_AT: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_FULL_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_METRIC_VALUE: Final = re.compile(r"^[A-Za-z0-9_./{}:-]{1,128}$")
_QVALUE: Final = re.compile(r"^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$")

_CONTENT_TYPES: Final[Mapping[str, str]] = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}
"""Tipos por extensión, cerrados (``mimetypes`` depende del sistema). El resto sale como
``application/octet-stream``; ``nosniff`` lo pone la cadena de cabeceras (TASK-134)."""
_OCTET_STREAM: Final = "application/octet-stream"

_UNLOGGED_PREFIXES: Final = ("/assets/",)
_SOURCE_MAP_SUFFIX: Final = ".map"
"""Mapas de fuentes (``.js.map``, ``.css.map``…): fuera del catálogo aunque la construcción los
deje en ``assets/`` (seguimiento de la revisión de VIG-71)."""

_log = get_logger("shared.api.static")


class Encoding(enum.StrEnum):
    """Codificaciones servidas, en orden de preferencia ante el mismo ``q``."""

    BR = "br"
    GZIP = "gzip"
    IDENTITY = "identity"


_SUFFIXES: Final[Mapping[Encoding, str]] = {Encoding.BR: ".br", Encoding.GZIP: ".gz"}


class StaticSiteInvalid(Exception):
    """La construcción de la aplicación en ``static_dir`` no es servible (fallo cerrado)."""

    def __init__(self, problems: Collection[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


# --- Negociación ------------------------------------------------------------------------------


def _qualities(header: str) -> dict[str, float]:
    """``valor → q`` de una cabecera con parámetro ``q`` (``Accept``, ``Accept-Encoding``).

    Un ``q`` mal formado descarta la entrada; ``q=0`` la deja con calidad cero (no aceptable).
    Solo se leen las primeras ``_MAX_HEADER_ITEMS`` entradas.
    """
    qualities: dict[str, float] = {}
    for item in header.split(",", _MAX_HEADER_ITEMS)[:_MAX_HEADER_ITEMS]:
        name, *params = item.split(";")
        token = name.strip().lower()
        if not token:
            continue
        quality: float | None = 1.0
        for param in params:
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                value = value.strip()
                quality = float(value) if _QVALUE.fullmatch(value) else None
        if quality is not None:
            qualities[token] = max(quality, qualities.get(token, 0.0))
    return qualities


def is_navigation(headers: Mapping[str, str]) -> bool:
    """La petición es una navegación del navegador (D-3).

    ``Sec-Fetch-Mode: navigate``, o ``Accept`` que admite ``text/html`` y no admite
    ``application/json`` (``q=0`` cuenta como no admitido; ``*/*`` no es ``text/html``).
    """
    mode = headers.get("sec-fetch-mode")
    if mode is not None and mode.strip().lower() == "navigate":
        return True
    accepted = _qualities(headers.get("accept") or "")
    return accepted.get("text/html", 0.0) > 0 and accepted.get("application/json", 0.0) <= 0


def choose_encoding(accept_encoding: str | None, available: Collection[Encoding]) -> Encoding:
    """La codificación precomprimida de ``available`` que la petición prefiere, o ``identity``.

    El mayor ``q`` gana; a igual ``q``, ``br`` antes que ``gzip``. ``*`` cubre lo que la
    cabecera no nombra. Sin cabecera, ``identity`` (RFC 9110 §12.5.3).
    """
    if not accept_encoding:
        return Encoding.IDENTITY
    qualities = _qualities(accept_encoding)
    wildcard = qualities.get("*", 0.0)
    best, best_quality = Encoding.IDENTITY, 0.0
    for encoding in (Encoding.BR, Encoding.GZIP):
        if encoding not in available:
            continue
        quality = qualities.get(encoding.value, wildcard)
        if quality > best_quality:
            best, best_quality = encoding, quality
    return best


def is_screen_path(path: str) -> bool:
    """``path`` es ``/`` o una ruta de ``SCREEN_ROUTES``."""
    return _SCREEN.fullmatch(path) is not None


def is_request_logged(path: str) -> bool:
    """La petición a ``path`` deja línea en el registro JSON de la aplicación.

    ``/assets/*`` no: queda en el registro de acceso del balanceador (``alb/app/``, 365 días).
    La cadena de middleware (TASK-134) lo consulta antes de escribir su línea por petición.
    """
    return not path.startswith(_UNLOGGED_PREFIXES)


# --- Catálogo ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppRelease:
    """Lo que dice ``version.json`` de la construcción servida."""

    app_version: str
    contract_tag: str
    built_at: str


@dataclass(frozen=True, slots=True)
class _Variant:
    path: Path
    etag: str
    stat_result: os.stat_result


@dataclass(frozen=True, slots=True)
class _Asset:
    content_type: str
    variants: Mapping[Encoding, _Variant]


@dataclass(frozen=True, slots=True)
class StaticSite:
    """La aplicación catalogada al construir ``vigia-api``; vacía si la imagen no la trae."""

    index: bytes | None = None
    version_json: bytes | None = None
    release: AppRelease | None = None
    assets: Mapping[str, _Asset] = field(default_factory=dict)

    @property
    def app_version(self) -> str:
        return UNKNOWN_VERSION if self.release is None else self.release.app_version

    @property
    def metric_version(self) -> str:
        """``app_version`` publicable como dimensión: un sha completo se acorta a 12; lo que la
        redacción alteraría sale como ``unknown`` (nunca un valor sin lista cerrada)."""
        version = self.app_version
        if _FULL_SHA.fullmatch(version):
            version = version[:12]
        if not _METRIC_VALUE.fullmatch(version) or redact_text(version) != version:
            return UNKNOWN_VERSION
        return version

    @classmethod
    def load(cls, directory: Path) -> StaticSite:
        """Cataloga ``directory``: ``index.html``, ``version.json`` y ``assets/``.

        Un directorio inexistente da una aplicación vacía (imagen sin etapa ``frontend-build``).

        Raises:
            StaticSiteInvalid: ``directory`` no es un directorio, o ``index.html``,
                ``version.json`` o ``assets/`` no son servibles.
        """
        try:
            info = directory.lstat()
        except FileNotFoundError:
            _log.warning("la imagen no trae la aplicación de página única")
            return cls()
        except OSError:
            raise StaticSiteInvalid(["el directorio de estáticos no se puede leer"]) from None
        if not stat.S_ISDIR(info.st_mode):
            raise StaticSiteInvalid(["el directorio de estáticos no es un directorio"])
        problems: list[str] = []
        index = _small_file(directory / "index.html", MAX_INDEX_BYTES, "index.html", problems)
        raw_version = _small_file(
            directory / "version.json", MAX_VERSION_BYTES, "version.json", problems
        )
        release = None if raw_version is None else _release(raw_version, problems)
        assets = _catalog(directory / "assets", problems)
        if problems:
            raise StaticSiteInvalid(problems)
        if index is None:
            _log.warning("la aplicación de página única no trae index.html")
        return cls(index=index, version_json=raw_version, release=release, assets=assets)


def _small_file(path: Path, limit: int, name: str, problems: list[str]) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        problems.append(f"{name} no se puede leer")
        return None
    if not stat.S_ISREG(info.st_mode):
        problems.append(f"{name} no es un archivo regular")
        return None
    if info.st_size > limit:
        problems.append(f"{name} supera {limit} bytes")
        return None
    try:
        data = path.read_bytes()
    except OSError:
        problems.append(f"{name} no se puede leer")
        return None
    if len(data) > limit:
        problems.append(f"{name} supera {limit} bytes")
        return None
    return data


def _reject_constant(_: str) -> Any:
    raise ValueError("constante no admitida")


def _release(raw: bytes, problems: list[str]) -> AppRelease | None:
    try:
        document = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        problems.append("version.json no es JSON válido")
        return None
    if not isinstance(document, dict):
        problems.append("version.json no es un objeto")
        return None
    fields = {
        "app_version": _APP_VERSION,
        "contract_tag": _CONTRACT_TAG,
        "built_at": _BUILT_AT,
    }
    values: dict[str, str] = {}
    for key, pattern in fields.items():
        value = document.get(key)
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            problems.append(f"version.json: {key} falta o no tiene la forma esperada")
        else:
            values[key] = value
    if len(values) != len(fields):
        return None
    return AppRelease(**values)


def _etag(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return f'"{digest.hexdigest()[:32]}"'


def _regular_files(directory: Path, problems: list[str]) -> dict[str, os.stat_result]:
    """Archivos regulares bajo ``directory`` por ruta relativa (``/``), sin seguir enlaces."""
    found: dict[str, os.stat_result] = {}
    pending: list[tuple[Path, str, int]] = [(directory, "", 1)]
    while pending:
        current, prefix, depth = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError:
            problems.append("assets/ no se puede recorrer")
            return {}
        for entry in entries:
            if not _ASSET_NAME.fullmatch(entry.name):
                continue
            relative = f"{prefix}{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                if depth < MAX_ASSET_DEPTH:
                    pending.append((Path(entry.path), f"{relative}/", depth + 1))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            found[relative] = entry.stat(follow_symlinks=False)
            if len(found) > MAX_ASSETS * 3:
                problems.append(f"assets/ supera {MAX_ASSETS} recursos")
                return {}
    return found


def _catalog(directory: Path, problems: list[str]) -> dict[str, _Asset]:
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        problems.append("assets/ no se puede leer")
        return {}
    if not stat.S_ISDIR(info.st_mode):
        problems.append("assets/ no es un directorio")
        return {}
    files = _regular_files(directory, problems)
    assets: dict[str, _Asset] = {}
    try:
        for relative, stat_result in files.items():
            if relative.endswith(tuple(_SUFFIXES.values())):
                continue
            if relative.lower().endswith(_SOURCE_MAP_SUFFIX):
                # Un mapa de fuentes revela el código original: nunca se sirve (``not_found``).
                continue
            path = directory / relative
            variants = {Encoding.IDENTITY: _Variant(path, _etag(path), stat_result)}
            for encoding, suffix in _SUFFIXES.items():
                compressed = files.get(relative + suffix)
                if compressed is not None:
                    variant_path = directory / (relative + suffix)
                    variants[encoding] = _Variant(variant_path, _etag(variant_path), compressed)
            content_type = _CONTENT_TYPES.get(Path(relative).suffix.lower(), _OCTET_STREAM)
            assets[relative] = _Asset(content_type, variants)
    except OSError:
        problems.append("un recurso de assets/ no se puede leer")
        return {}
    if len(assets) > MAX_ASSETS:
        problems.append(f"assets/ supera {MAX_ASSETS} recursos")
        return {}
    return assets


# --- Rutas ------------------------------------------------------------------------------------


def _route_path(scope: Scope) -> str:
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", ""))
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


class _NavigationRoute(APIRoute):
    """Ruta que solo coincide con navegaciones ``GET``/``HEAD`` a una pantalla (D-3).

    Va delante de las rutas de la API; ante cualquier otra petición responde ``Match.NONE`` y el
    enrutador sigue con la API, así que ``/concessions`` con ``Accept: application/json`` llega a
    su ruta de la API y nunca recibe ``405`` por culpa de la pantalla.
    """

    navigation_only = True
    """Marca que ``check_routes`` exige a la única ruta que declara ``APP_SCREEN``."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope.get("type") != "http" or scope.get("method") not in ("GET", "HEAD"):
            return Match.NONE, {}
        if not is_screen_path(_route_path(scope)) or not is_navigation(Headers(scope=scope)):
            return Match.NONE, {}
        return super().matches(scope)


def _etag_matches(header: str, etag: str) -> bool:
    """``If-None-Match`` coincide con ``etag`` (comparación débil, RFC 9110 §13.1.2)."""
    for item in header.split(",", _MAX_HEADER_ITEMS)[:_MAX_HEADER_ITEMS]:
        tag = item.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def static_routers(site: StaticSite) -> tuple[APIRouter, APIRouter]:
    """``(pantallas, archivos)``: el primero va **delante** de las rutas de la API y el segundo
    **detrás**. Sin ``index.html`` no hay ruta de pantalla: toda petición llega a la API."""
    screens = APIRouter(route_class=_NavigationRoute)
    files = APIRouter()
    index = site.index

    if index is not None:

        @screens.api_route(
            UnauthenticatedRoute.APP_SCREEN.path,
            methods=["GET", "HEAD"],
            dependencies=[unauthenticated(UnauthenticatedRoute.APP_SCREEN)],
            include_in_schema=False,
        )
        async def screen() -> Response:
            _log.info("aplicación de página única servida a una navegación")
            return Response(
                index,
                media_type="text/html; charset=utf-8",
                headers={"Cache-Control": NO_STORE},
            )

    @files.api_route(
        UnauthenticatedRoute.APP_ASSET.path,
        methods=["GET", "HEAD"],
        dependencies=[unauthenticated(UnauthenticatedRoute.APP_ASSET)],
        include_in_schema=False,
    )
    async def asset(asset_path: str, request: Request) -> Response:
        found = site.assets.get(asset_path) if _ASSET_PATH.fullmatch(asset_path) else None
        if found is None:
            raise ApiError(ApiErrorCode.NOT_FOUND)
        encoding = choose_encoding(
            request.headers.get("accept-encoding"), found.variants.keys() - {Encoding.IDENTITY}
        )
        variant = found.variants[encoding]
        headers = {
            "Cache-Control": ASSET_CACHE_CONTROL,
            "Vary": "Accept-Encoding",
            "ETag": variant.etag,
        }
        if encoding is not Encoding.IDENTITY:
            headers["Content-Encoding"] = encoding.value
        if_none_match = request.headers.get("if-none-match")
        if if_none_match is not None and _etag_matches(if_none_match, variant.etag):
            return Response(status_code=304, headers=headers)
        return FileResponse(
            variant.path,
            media_type=found.content_type,
            headers=headers,
            stat_result=variant.stat_result,
        )

    @files.api_route(
        UnauthenticatedRoute.APP_VERSION.path,
        methods=["GET", "HEAD"],
        dependencies=[unauthenticated(UnauthenticatedRoute.APP_VERSION)],
        include_in_schema=False,
    )
    async def version() -> Response:
        if site.version_json is None:
            raise ApiError(ApiErrorCode.NOT_FOUND)
        return Response(
            site.version_json,
            media_type="application/json",
            headers={"Cache-Control": NO_STORE},
        )

    @files.api_route(
        UnauthenticatedRoute.ROBOTS.path,
        methods=["GET", "HEAD"],
        dependencies=[unauthenticated(UnauthenticatedRoute.ROBOTS)],
        include_in_schema=False,
    )
    async def robots() -> Response:
        return Response(
            ROBOTS_TXT,
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": NO_STORE},
        )

    return screens, files
