"""Versión vigente del aviso de tratamiento de datos de los usuarios (NFR-NUC-29; BR-NUC-32).

Módulo puro, sin dependencias: lo leen el constructor de contextos de sesión
(``identity.authz.context``, que no deja usar una sesión sin la aceptación de esta versión salvo
para aceptarla) y el servicio del aviso (``identity.application.privacy_notice``), que sirve el
texto de ``backend/resources/privacy-notice/<versión>.md``.

El texto definitivo lo entrega el abogado del gate 21 (pendiente P4). Hasta entonces la versión
vigente es un **texto marcador** identificado como pendiente: publicar el definitivo es añadir su
archivo con una versión nueva y apuntar aquí ``CURRENT_PRIVACY_NOTICE_VERSION``; las versiones
anteriores no se editan ni se borran, porque cada aceptación guarda la versión que se aceptó.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "CURRENT_PRIVACY_NOTICE_VERSION",
    "PRIVACY_NOTICE_PENDING_LEGAL_TEXT",
    "PRIVACY_NOTICE_VERSION_PATTERN",
]

CURRENT_PRIVACY_NOTICE_VERSION: Final = "v0-pendiente"
"""La versión que hay que haber aceptado para usar una sesión (≤ 32 caracteres)."""

PRIVACY_NOTICE_PENDING_LEGAL_TEXT: Final = True
"""La versión vigente es el texto marcador: el definitivo del abogado está pendiente (P4)."""

PRIVACY_NOTICE_VERSION_PATTERN: Final = re.compile(r"v[0-9]{1,4}(-[a-z0-9]{1,24})?")
"""Forma de una versión: también nombra su archivo, así que no admite rutas ni puntos."""
