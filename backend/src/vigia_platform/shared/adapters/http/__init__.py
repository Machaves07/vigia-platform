"""Interfaz HTTP de ``shared`` (TASK-137): verificación pública y operación de la plataforma.

- ``wellknown``: ``GET /.well-known/vigia-checkpoint-keys`` y ``GET /.well-known/vigia-verifier``
  (lista pública cerrada).
- ``platform_ops``: ``POST /platform/dead-letter/{event_id}/{consumer}/replay`` y
  ``POST /platform/keys/{purpose}/rotate`` (``platform.*``, solo la organización proveedora).
"""

from __future__ import annotations

from fastapi import APIRouter

from vigia_platform.shared.adapters.http.platform_ops import platform_ops_router
from vigia_platform.shared.adapters.http.services import (
    DEFAULT_VERIFIER_PATH,
    PLATFORM_STATE_KEY,
    VERIFIER_STATE_KEY,
    PlatformHttp,
    VerifierDigest,
)
from vigia_platform.shared.adapters.http.wellknown import wellknown_router

__all__ = [
    "DEFAULT_VERIFIER_PATH",
    "PLATFORM_STATE_KEY",
    "VERIFIER_STATE_KEY",
    "PlatformHttp",
    "VerifierDigest",
    "shared_routers",
]


def shared_routers() -> tuple[APIRouter, ...]:
    """Los enrutadores públicos y de operación que registra ``platform_units()``."""
    return (wellknown_router(), platform_ops_router())
