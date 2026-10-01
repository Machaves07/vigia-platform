"""Cadena fija de middleware de ``vigia-api`` (LC-NUC-20; PAT-NUC-SEG-02, SEG-06, MAN-01).

Toda petición pasa por once eslabones en un orden fijo (``CHAIN``): (1) ``correlation_id``
generado; (2) manejador global de errores; (3) cabeceras de seguridad; el eslabón de clase de ruta
``node``/``person`` del pendiente nº 37, que fija el pool de la base; (4) límite de cuerpo;
(5) tasa por origen; (6) sesión y contexto; (7) tasa por sesión; (8) barrera anti-falsificación;
(9) aviso de tratamiento; y (10) autorización por ruta, la dependencia obligatoria de cada ruta
(``declarations.requires``/``unauthenticated``). Los detalles de cada uno están en ``steps``.

``install_chain`` instala los diez middleware en ese orden; ``verify_chain`` es la prueba de
arranque: la fábrica no arranca (``ApiStartupError``) si el orden instalado no es exactamente
``CHAIN``. Además, cada petición deja su traza y la autorización de la ruta exige la traza
completa y en orden (``request_state``): una cadena alterada después de arrancar tampoco deja
pasar ninguna petición.

Sin configuración por ruta salvo la declaración de permiso y ``body_limit``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI

from vigia_platform.shared.api.middleware.authorization import (
    AuditCsrfRejections,
    ContextAuthorizer,
    request_context,
    request_session,
)
from vigia_platform.shared.api.middleware.headers import (
    SecurityHeaders,
    content_security_policy,
    parse_store_origins,
)
from vigia_platform.shared.api.middleware.routing import RouteTable, route_class_of
from vigia_platform.shared.api.middleware.steps import (
    DEFAULT_BODY_LIMIT_BYTES,
    PRIVACY_NOTICE_ACCEPT,
    STEP_CLASSES,
    ChainSettings,
    CsrfAuditPort,
    CsrfReason,
    CsrfRejection,
    SessionContextPort,
)
from vigia_platform.shared.api.request_state import CHAIN, MIDDLEWARE_CHAIN, ChainStep

__all__ = [
    "CHAIN",
    "DEFAULT_BODY_LIMIT_BYTES",
    "MIDDLEWARE_CHAIN",
    "PRIVACY_NOTICE_ACCEPT",
    "AuditCsrfRejections",
    "ChainSettings",
    "ChainStep",
    "ContextAuthorizer",
    "CsrfAuditPort",
    "CsrfReason",
    "CsrfRejection",
    "RouteTable",
    "SecurityHeaders",
    "SessionContextPort",
    "content_security_policy",
    "install_chain",
    "installed_chain",
    "parse_store_origins",
    "request_context",
    "request_session",
    "route_class_of",
    "verify_chain",
]


def install_chain(
    app: FastAPI,
    settings: ChainSettings,
    *,
    order: Sequence[ChainStep] = MIDDLEWARE_CHAIN,
) -> None:
    """Instala los eslabones de ``order`` (el primero, el más externo).

    ``order`` existe para que la prueba de orden pueda instalar una cadena alterada y comprobar
    que ``verify_chain`` la rechaza; la fábrica usa siempre el orden fijo.
    """
    for step in reversed(tuple(order)):
        app.add_middleware(STEP_CLASSES[step], chain=settings)


def installed_chain(app: FastAPI) -> tuple[ChainStep, ...]:
    """Los eslabones instalados en ``app``, del más externo al más interno."""
    found: list[ChainStep] = []
    for middleware in app.user_middleware:
        step: Any = getattr(middleware.cls, "step", None)
        if isinstance(step, ChainStep):
            found.append(step)
    return tuple(found)


def verify_chain(app: FastAPI) -> list[str]:
    """Problemas del orden de la cadena instalada en ``app`` (vacío si es exactamente ``CHAIN``)."""
    installed = installed_chain(app)
    if installed == MIDDLEWARE_CHAIN:
        return []
    expected = ", ".join(step.value for step in MIDDLEWARE_CHAIN)
    actual = ", ".join(step.value for step in installed) or "ninguno"
    return [
        "la cadena de middleware no tiene el orden fijo de PAT-NUC-SEG-06: se esperaba "
        f"«{expected}» y está instalada «{actual}»"
    ]
