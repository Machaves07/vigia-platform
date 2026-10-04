"""Estado de una petición dentro de la cadena fija de middleware (PAT-NUC-SEG-06; TASK-134).

``ChainStep`` y ``CHAIN`` fijan el orden de los doce eslabones: los diez de PAT-NUC-SEG-06, el
eslabón de clase de ruta del pendiente nº 37, que va tras las cabeceras y antes del límite de
cuerpo (el límite y todo lo demás ya saben si la petición es de un nodo o de una persona), y el
mamparo de LC-GOB-20, tras el límite de cuerpo y antes del límite de tasa por origen.

Cada eslabón deja su nombre en ``RequestState.trace`` al pasar. La autorización por ruta (el
último eslabón, una dependencia obligatoria de cada ruta) exige que la traza sea exactamente
``CHAIN`` sin el propio eslabón y que la ruta que resolvió la cadena sea la que FastAPI va a
ejecutar: si no, deniega con ``internal_error`` (fallo cerrado). Así, una cadena desordenada o
saltada no deja pasar ninguna petición aunque alguien se salte la comprobación de arranque.

Módulo sin dependencias de la cadena: lo importan ``declarations`` y ``middleware``.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from vigia_platform.shared.db import RouteClass

if TYPE_CHECKING:
    from vigia_platform.identity.authz.context import SessionScope
    from vigia_platform.shared.api.declarations import DeclaredRoute

__all__ = [
    "CHAIN",
    "MIDDLEWARE_CHAIN",
    "STATE_KEY",
    "ChainStep",
    "RequestState",
    "request_state",
]


class ChainStep(enum.StrEnum):
    """Eslabones de la cadena, en su orden (PAT-NUC-SEG-06, pendiente nº 37 y LC-GOB-20)."""

    CORRELATION = "correlation"
    """(1) ``correlation_id`` generado por la plataforma; nunca se acepta del cliente."""
    ERRORS = "errors"
    """(2) Manejador global de errores (fallo cerrado)."""
    SECURITY_HEADERS = "security_headers"
    """(3) Cabeceras de seguridad en toda respuesta."""
    ROUTE_CLASS = "route_class"
    """(nº 37) Clase de ruta ``node`` o ``person``: fija el pool de la base."""
    BODY_LIMIT = "body_limit"
    """(4) Límite de cuerpo: 1 MB salvo la ruta que declara el suyo."""
    BULKHEAD = "bulkhead"
    """(LC-GOB-20) Mamparo: un puesto del semáforo de la clase de ruta."""
    ORIGIN_RATE_LIMIT = "origin_rate_limit"
    """(5) Límite de tasa por origen."""
    SESSION = "session"
    """(6) Sesión y contexto (``ScopeContext``)."""
    SESSION_RATE_LIMIT = "session_rate_limit"
    """(7) Límite de tasa por sesión."""
    CSRF = "csrf"
    """(8) Barrera anti-falsificación (``Sec-Fetch-Site`` y ``Origin``)."""
    PRIVACY_NOTICE = "privacy_notice"
    """(9) Aviso de tratamiento aceptado en su versión vigente."""
    AUTHORIZATION = "authorization"
    """(10) Autorización por ruta: dependencia obligatoria de cada ruta."""


CHAIN: Final[tuple[ChainStep, ...]] = tuple(ChainStep)
"""El orden fijo; la prueba de arranque y la traza de cada petición lo comparan."""
MIDDLEWARE_CHAIN: Final[tuple[ChainStep, ...]] = CHAIN[:-1]
"""Los eslabones que son middleware ASGI (todos menos la autorización, que es una dependencia)."""

STATE_KEY: Final = "vigia_request"


@dataclass(slots=True)
class RequestState:
    """Lo que la cadena sabe de la petición en curso."""

    correlation_id: uuid.UUID | None = None
    started_monotonic: float | None = None
    """Inicio de la petición (``Clock.monotonic`` del primer eslabón): mide su duración."""
    trace: list[ChainStep] = field(default_factory=list)
    resolved: bool = False
    """``True`` cuando el eslabón de clase de ruta ya resolvió la ruta."""
    route: DeclaredRoute | None = None
    """La ruta que atenderá la petición (``None``: ninguna; FastAPI responderá ``not_found``)."""
    route_class: RouteClass = RouteClass.PERSON
    session: SessionScope | None = None
    """El contexto de la sesión, si la ruta la exige y la cookie es válida."""
    body_exceeded: bool = False
    """Clase ``node``: el cuerpo recibido supera el límite de la ruta. El límite de cuerpo lo
    **marca** sin responder y ``node_api`` emite ``payload_too_large`` en el paso 3 de BR-GOB-84,
    después de la versión y del certificado."""
    body_length_invalid: bool = False
    """Clase ``node``: ``Content-Length`` repetido o mal formado (``schema_invalid``, paso 3)."""
    body_bytes_received: int = 0
    """Bytes del cuerpo que la cadena llegó a leer (como mucho el límite más un fragmento)."""
    node: object | None = None
    """Lo que la verificación previa de ``node_api`` dejó para la operación de la ruta."""


def request_state(scope: MutableMapping[str, Any]) -> RequestState:
    """El estado de la petición en ``scope`` (lo crea vacío si aún no existe)."""
    state = scope.setdefault("state", {})
    current = state.get(STATE_KEY)
    if not isinstance(current, RequestState):
        current = RequestState()
        state[STATE_KEY] = current
    return current
