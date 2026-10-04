"""Cabeceras de seguridad de toda respuesta (paso 3; BR-NUC-95, NFR-NUC-18; pendientes nº 8 y 9).

Valores fijos, iguales para la API y para los estáticos de la aplicación (adenda A-17):

- ``Strict-Transport-Security: max-age=31536000; includeSubDomains``;
- ``X-Content-Type-Options: nosniff``; ``X-Frame-Options: DENY``;
  ``Referrer-Policy: strict-origin-when-cross-origin``;
- ``Content-Security-Policy`` con las directivas de PAT-APP-SEG-01: ``default-src``,
  ``script-src``, ``style-src`` e ``img-src`` en ``'self'``; ``media-src`` y ``connect-src`` con
  ``'self'`` más los orígenes de ``VIGIA_CSP_STORE_ORIGINS`` (el depósito de evidencias, URL
  prefirmadas); ``frame-src``, ``object-src`` y ``frame-ancestors`` en ``'none'``; ``base-uri``
  y ``form-action`` en ``'self'``; y ``require-trusted-types-for 'script'`` (PAT-APP-SEG-04).
  Ninguna directiva ``unsafe-*``, ningún comodín.
- **Sin CORS**: la aplicación se sirve desde el mismo origen; cualquier ``Access-Control-*`` que
  una ruta intente añadir se quita. Vale también para las rutas del contrato (TASK-206).
- Clase ``node``: ``X-Vigia-Contract-Version`` con la versión de la plataforma en toda respuesta,
  también en las de error y en ``not_found`` (BR-CTR-18).

Una cabecera de seguridad que una ruta haya puesto se **sustituye** por la de la plataforma:
nunca sale duplicada ni rebajada.

Los orígenes del almacén son una lista cerrada (``store_origin``): ``https://`` y un nombre de
host en minúsculas, con puerto opcional; ``http://localhost`` o ``http://127.0.0.1`` solo en
``local`` y ``test`` (LocalStack). Nada que pueda cerrar o ampliar una directiva (``;``, ``,``,
comillas, espacios, ``*``) pasa la validación.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, MutableMapping
from typing import Any, Final

__all__ = [
    "CONTENT_SECURITY_POLICY",
    "CONTRACT_VERSION_HEADER",
    "MAX_STORE_ORIGINS",
    "SECURITY_HEADER_NAMES",
    "SecurityHeaders",
    "content_security_policy",
    "parse_store_origins",
    "store_origin",
]

MAX_STORE_ORIGINS: Final = 8
CONTRACT_VERSION_HEADER: Final = b"x-vigia-contract-version"
"""Cabecera de versión que lleva toda respuesta a un nodo (BR-CTR-18)."""
CONTENT_SECURITY_POLICY: Final = "content-security-policy"

_HOST: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
_PORT: Final = r"(?::[1-9][0-9]{0,4})?"
_HTTPS_ORIGIN: Final = re.compile(rf"https://{_HOST}{_PORT}")
_LOCAL_ORIGIN: Final = re.compile(rf"http://(?:localhost|127\.0\.0\.1){_PORT}")

_STATIC_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("strict-transport-security", "max-age=31536000; includeSubDomains"),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "strict-origin-when-cross-origin"),
)

SECURITY_HEADER_NAMES: Final = frozenset(
    {name for name, _ in _STATIC_HEADERS} | {CONTENT_SECURITY_POLICY}
)


def store_origin(value: object, *, allow_local: bool) -> str:
    """El origen del almacén validado; ``ValueError`` si no tiene la forma cerrada."""
    if not isinstance(value, str) or len(value) > 255:
        raise ValueError("origen del almacén mal formado")
    if _HTTPS_ORIGIN.fullmatch(value) or (allow_local and _LOCAL_ORIGIN.fullmatch(value)):
        return value
    raise ValueError("origen del almacén mal formado")


def parse_store_origins(raw: str, *, allow_local: bool) -> tuple[str, ...]:
    """``VIGIA_CSP_STORE_ORIGINS``: lista separada por espacios (sin repetidos, como mucho 8)."""
    if not isinstance(raw, str):
        raise ValueError("VIGIA_CSP_STORE_ORIGINS debe ser texto")
    items = raw.split()
    origins = tuple(dict.fromkeys(store_origin(item, allow_local=allow_local) for item in items))
    if len(origins) > MAX_STORE_ORIGINS:
        raise ValueError(f"VIGIA_CSP_STORE_ORIGINS admite como mucho {MAX_STORE_ORIGINS} orígenes")
    return origins


def content_security_policy(store_origins: Iterable[str], *, allow_local: bool = False) -> str:
    """La política de contenido con los orígenes del almacén en ``media-src`` y ``connect-src``."""
    origins = [store_origin(origin, allow_local=allow_local) for origin in store_origins]
    store = " ".join(["'self'", *dict.fromkeys(origins)])
    directives = (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        f"media-src {store}",
        f"connect-src {store}",
        "frame-src 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "require-trusted-types-for 'script'",
    )
    return "; ".join(directives)


class SecurityHeaders:
    """Aplica las cabeceras de seguridad a un mensaje ``http.response.start``."""

    def __init__(self, store_origins: Iterable[str] = (), *, allow_local: bool = False) -> None:
        policy = content_security_policy(store_origins, allow_local=allow_local)
        self._headers: tuple[tuple[bytes, bytes], ...] = tuple(
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in (*_STATIC_HEADERS, (CONTENT_SECURITY_POLICY, policy))
        )
        self.policy = policy

    def apply(
        self, message: MutableMapping[str, Any], *, contract_version: str | None = None
    ) -> None:
        """Sustituye las cabeceras de seguridad y quita las de CORS del mensaje de inicio.

        Con ``contract_version`` (respuestas de la clase ``node``), fija además
        ``X-Vigia-Contract-Version`` con la versión del contrato de la plataforma (BR-CTR-18).
        """
        if message.get("type") != "http.response.start":
            return
        kept = [
            (name, value)
            for name, value in message.get("headers", ())
            if (lowered := bytes(name).lower()).decode("latin-1") not in SECURITY_HEADER_NAMES
            and not lowered.startswith(b"access-control-")
            and (contract_version is None or lowered != CONTRACT_VERSION_HEADER)
        ]
        extra: list[tuple[bytes, bytes]] = []
        if contract_version is not None:
            extra.append((CONTRACT_VERSION_HEADER, contract_version.encode("latin-1")))
        message["headers"] = [*kept, *self._headers, *extra]
