"""Servicios de las rutas públicas y de operación de ``shared`` (TASK-137).

``PlatformHttp`` lo construye la raíz de composición y lo entrega en ``AppRuntime.platform``; la
fábrica lo deja en ``app.state``. Sin él, las rutas de operación responden ``internal_error`` y
las de claves ``temporarily_unavailable`` (fallo cerrado).

El hash del verificador no es un servicio: la fábrica lo calcula al construir la aplicación sobre
los bytes de ``tools/vigia_verify.py`` (``VerifierDigest``) y lo deja en ``app.state``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from fastapi import Request

from vigia_platform.identity.authz.authorize import Authorizer
from vigia_platform.ledger.application.audit_writer import AuditWriter
from vigia_platform.ledger.chain.package_verifier import FORMAT_VERSION
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.replay import ReplayReceipt
from vigia_platform.shared.signing.keys import SigningKeyRecord, SigningPurpose
from vigia_platform.shared.signing.service import RotationResult

__all__ = [
    "DEFAULT_VERIFIER_PATH",
    "PLATFORM_STATE_KEY",
    "VERIFIER_FILE_NAME",
    "VERIFIER_STATE_KEY",
    "DeadLetterReplayer",
    "KeyRotator",
    "OperatorContexts",
    "PlatformHttp",
    "PublicKeySource",
    "VerifierDigest",
    "platform_http",
    "verifier_digest",
]

PLATFORM_STATE_KEY: Final = "vigia_platform_http"
VERIFIER_STATE_KEY: Final = "vigia_verifier_digest"
VERIFIER_FILE_NAME: Final = "vigia_verify.py"
DEFAULT_VERIFIER_PATH: Final = Path(__file__).resolve().parents[5] / "tools" / VERIFIER_FILE_NAME
"""``backend/tools/vigia_verify.py``: el artefacto que genera ``tools/build_verifier.py`` (VIG-54)
y que se copia a la imagen con ``backend/``."""

_log = get_logger("shared.http")


class PublicKeySource(Protocol):
    """``SigningPort.public_keys``: para ``checkpoint``, también las retiradas (BR-NUC-55)."""

    def public_keys(self, purpose: SigningPurpose) -> tuple[SigningKeyRecord, ...]: ...


class KeyRotator(PublicKeySource, Protocol):
    """``SigningService``: claves públicas y rotación por propósito (BR-NUC-85, 86)."""

    async def rotate(self, purpose: SigningPurpose, *, context: ScopeContext) -> RotationResult: ...


class DeadLetterReplayer(Protocol):
    """``OutboxPort.replay`` (``DeadLetterReplay``; BR-NUC-82)."""

    async def replay(
        self, context: ScopeContext, event_id: uuid.UUID, consumer_name: str
    ) -> ReplayReceipt: ...


class OperatorContexts(Protocol):
    """``ScopeContexts.context_from_operator``: la orden administrativa del operador (BR-NUC-03)."""

    async def context_from_operator(
        self, operator_id: uuid.UUID, *, correlation_id: uuid.UUID | None = None
    ) -> ScopeContext: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformHttp:
    """Los puertos que usan las rutas públicas de claves y las de operación."""

    signing: KeyRotator
    dead_letter: DeadLetterReplayer
    operators: OperatorContexts
    authorizer: Authorizer
    audit: AuditWriter
    provider_organization_id: uuid.UUID

    def __post_init__(self) -> None:
        if type(self.provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")


def platform_http(request: Request) -> PlatformHttp:
    services = getattr(request.app.state, PLATFORM_STATE_KEY, None)
    if not isinstance(services, PlatformHttp):
        _log.error("las rutas de operación no tienen servicios instalados")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return services


@dataclass(frozen=True, slots=True)
class VerifierDigest:
    """El SHA-256 de los bytes del verificador de un archivo, tal como se distribuye."""

    sha256: str
    size_bytes: int
    format_version: int = FORMAT_VERSION
    name: str = VERIFIER_FILE_NAME

    @classmethod
    def of(cls, path: Path) -> VerifierDigest:
        """``OSError`` si el archivo no se puede leer (la fábrica no arranca sin él)."""
        data = path.read_bytes()
        return cls(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


def verifier_digest(request: Request) -> VerifierDigest:
    digest = getattr(request.app.state, VERIFIER_STATE_KEY, None)
    if not isinstance(digest, VerifierDigest):
        _log.error("la aplicación no tiene el hash del verificador")
        raise ApiError(ApiErrorCode.INTERNAL_ERROR)
    return digest
