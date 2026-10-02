"""Rutas públicas de verificación: claves ``checkpoint`` y hash del verificador (TASK-137).

Las dos están en la lista pública cerrada (``UnauthenticatedRoute``; BR-NUC-91): un cliente o un
auditor las consulta sin cuenta para verificar un paquete sin depender del proveedor (H-36,
BR-NUC-57). Llevan el límite de tasa por origen de las rutas públicas.

- ``GET /.well-known/vigia-checkpoint-keys`` (BR-NUC-55): **todas** las claves públicas de
  propósito ``checkpoint``, también las ``overlapping`` y las ``retired``: ninguna se retira jamás
  de la publicación, porque un paquete antiguo se firmó con ella. Solo la parte pública
  (``key_id``, ``algorithm``, ``public_key`` en base64, estado y vigencia); nunca la referencia al
  secreto. Sin el servicio de firma cargado, ``temporarily_unavailable``.
- ``GET /.well-known/vigia-verifier``: el SHA-256 de los bytes de ``tools/vigia_verify.py``, el
  verificador de un archivo que genera ``tools/build_verifier.py`` (VIG-54) y que U-04 incluye en
  cada paquete. La fábrica lo calcula una vez al construir la aplicación sobre el mismo archivo
  que se distribuye; ``build_verifier.py --check`` garantiza que ese archivo es el generado.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from vigia_platform.ledger.chain.checkpoints import CheckpointPublicKey
from vigia_platform.shared.adapters.http.services import (
    PLATFORM_STATE_KEY,
    PlatformHttp,
    VerifierDigest,
    verifier_digest,
)
from vigia_platform.shared.api.declarations import UnauthenticatedRoute, unauthenticated
from vigia_platform.shared.api.errors import ApiError, ApiErrorCode
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.signing.keys import SigningPurpose, format_timestamp
from vigia_platform.shared.signing.service import SigningNotReady

__all__ = ["PUBLIC_CACHE_CONTROL", "CheckpointKeysOut", "VerifierOut", "wellknown_router"]

PUBLIC_CACHE_CONTROL: Final = "public, max-age=300"
"""Caché de 5 minutos ``[objetivo propio]``: una rotación llega a los clientes en ese plazo."""

_log = get_logger("shared.http")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CheckpointKeyOut(_Strict):
    key_id: str
    algorithm: Literal["Ed25519"]
    public_key: str
    status: str
    valid_from: str
    valid_until: str


class CheckpointKeysOut(_Strict):
    purpose: Literal["checkpoint"]
    keys: tuple[CheckpointKeyOut, ...]


class VerifierOut(_Strict):
    name: str
    sha256: str
    size_bytes: int
    format_version: int


def _signing(request: Request) -> PlatformHttp:
    services = getattr(request.app.state, PLATFORM_STATE_KEY, None)
    if not isinstance(services, PlatformHttp):
        _log.error("las claves públicas no tienen servicio de firma instalado")
        raise ApiError(ApiErrorCode.TEMPORARILY_UNAVAILABLE)
    return services


def wellknown_router() -> APIRouter:
    router = APIRouter(tags=["verificación pública"])

    @router.get(
        UnauthenticatedRoute.CHECKPOINT_KEYS.path,
        dependencies=[unauthenticated(UnauthenticatedRoute.CHECKPOINT_KEYS)],
        summary="Claves públicas de los puntos de control, también las retiradas (público)",
    )
    async def checkpoint_keys(
        response: Response, services: Annotated[PlatformHttp, Depends(_signing)]
    ) -> CheckpointKeysOut:
        try:
            records = services.signing.public_keys(SigningPurpose.CHECKPOINT)
        except SigningNotReady:
            raise ApiError(ApiErrorCode.TEMPORARILY_UNAVAILABLE) from None
        keys = sorted(
            (
                CheckpointPublicKey.of(record)
                for record in records
                if record.purpose is SigningPurpose.CHECKPOINT
            ),
            key=lambda key: key.key_id,
        )
        response.headers["Cache-Control"] = PUBLIC_CACHE_CONTROL
        return CheckpointKeysOut(
            purpose="checkpoint",
            keys=tuple(
                CheckpointKeyOut(
                    key_id=key.key_id,
                    algorithm="Ed25519",
                    public_key=key.public_key,
                    status=key.status.value,
                    valid_from=format_timestamp(key.valid_from),
                    valid_until=format_timestamp(key.valid_until),
                )
                for key in keys
            ),
        )

    @router.get(
        UnauthenticatedRoute.VERIFIER_HASH.path,
        dependencies=[unauthenticated(UnauthenticatedRoute.VERIFIER_HASH)],
        summary="SHA-256 del verificador de paquetes vigia_verify.py (público)",
    )
    async def verifier(
        response: Response, digest: Annotated[VerifierDigest, Depends(verifier_digest)]
    ) -> VerifierOut:
        response.headers["Cache-Control"] = PUBLIC_CACHE_CONTROL
        return VerifierOut(
            name=digest.name,
            sha256=digest.sha256,
            size_bytes=digest.size_bytes,
            format_version=digest.format_version,
        )

    return router
