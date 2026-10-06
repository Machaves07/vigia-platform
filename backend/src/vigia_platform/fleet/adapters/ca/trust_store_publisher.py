"""``TrustStorePublisherPort`` hacia ``vigia-edge`` y ``vigia-node-trust`` (TASK-220).

Tech-stack §2.2 y su nota del 2026-09-21; infraestructura §4.4 y §5.3 (con su nota D-7);
deployment-architecture §3.3. Una publicación, en este orden:

1. ``PutObject`` de ``ca/crl.pem`` en el depósito ``vigia-edge`` (versionado) y su ``VersionId``;
   sin versión (depósito sin versionado) no se sigue: el almacén necesita la versión exacta;
2. ``DescribeTrustStoreRevocations`` del almacén ``vigia-node-trust``: las listas que había antes;
3. ``AddTrustStoreRevocations`` con el depósito, la clave y la versión;
4. ``DescribeTrustStoreRevocations`` de la lista añadida: su número de entradas debe ser el de la
   lista firmada;
5. ``RemoveTrustStoreRevocations`` de **todas** las que había antes del paso 3. Se añade antes de
   retirar: el almacén nunca queda sin lista (procedimiento 6.2).

Los nombres de las tres operaciones del servicio de balanceo son ``[hipótesis (b)]`` del diseño
hasta la comprobación nº 10 del primer despliegue (VIG-174, A-47).

**Repetible sin efecto doble**: si un paso falla después de escribir el objeto o de añadir la lista
(paso parcial), el ciclo siguiente escribe una versión nueva, la añade y retira todas las
anteriores, también la que quedó a medias. Una lista que ya no existe al retirarla
(``RevocationNotFound``) cuenta como retirada.

**Topes** (NFR-GOB-43): cada paso termina en ``step_timeout_seconds`` (5 s) como mucho; el
cliente de boto3 lleva ``connect_timeout``, ``read_timeout`` y dos intentos (reintentos acotados).
Al vencer, la llamada se abandona sin esperarla. Un fallo lanza
``RevocationListPublishFailed(paso)``: el mensaje nunca lleva el ARN, el depósito ni el PEM.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Executor
from typing import Any, Final, Protocol

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from vigia_platform.fleet.domain.revocation_list import (
    PUBLISH_STEP_TIMEOUT_SECONDS,
    PublishedRevocationList,
    PublishStep,
    RevocationListPublishFailed,
    SignedRevocationList,
)
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.storage import ObjectHead

__all__ = [
    "CRL_CONTENT_TYPE",
    "CRL_OBJECT_KEY",
    "CrlStorage",
    "TrustStorePublisher",
    "build_elbv2_client",
]

CRL_OBJECT_KEY: Final = "ca/crl.pem"
"""``VIGIA_CRL_KEY`` por omisión (infraestructura §6)."""
CRL_CONTENT_TYPE: Final = "application/x-pem-file"
_REVOCATION_TYPE: Final = "CRL"
_NOT_FOUND: Final = frozenset({"RevocationIdNotFound", "RevocationIdNotFoundException"})
_MAX_PAGES: Final = 20
"""Tope de páginas al listar las revocaciones del almacén (una vigente, ``[hipótesis (c)]``)."""
_CONNECT_TIMEOUT_SECONDS: Final = 2.0
_READ_TIMEOUT_SECONDS: Final = 3.0

_log = get_logger("fleet.revocation_list")

# El paso va al registro como ``code``: solo valores de esta lista cerrada (NFR-NUC-41).
with contextlib.suppress(ValueError):
    redaction.DEFAULT_POLICY.register("code", [step.value for step in PublishStep])


class CrlStorage(Protocol):
    """La parte de ``StoragePort`` que usa la publicación (depósito ``vigia-edge``)."""

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead: ...


def build_elbv2_client(*, region: str, endpoint_url: str | None = None) -> Any:
    """Cliente del servicio de balanceo con tiempos de espera fijos y dos intentos."""
    return boto3.client(
        "elbv2",
        endpoint_url=endpoint_url,
        region_name=region,
        config=Config(
            region_name=region,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            read_timeout=_READ_TIMEOUT_SECONDS,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    code = response.get("Error", {}).get("Code")
    return code if isinstance(code, str) else None


class TrustStorePublisher:
    """``TrustStorePublisherPort`` sobre S3 (``CrlStorage``) y el cliente ``elbv2`` de boto3."""

    def __init__(
        self,
        *,
        storage: CrlStorage,
        elb: Any,
        trust_store_arn: str,
        bucket: str,
        object_key: str = CRL_OBJECT_KEY,
        step_timeout_seconds: float = PUBLISH_STEP_TIMEOUT_SECONDS,
        executor: Executor | None = None,
    ) -> None:
        if step_timeout_seconds <= 0:
            raise ValueError("step_timeout_seconds debe ser positivo")
        self._storage = storage
        self._elb = elb
        self._arn = trust_store_arn
        self._bucket = bucket
        self._key = object_key
        self._timeout = step_timeout_seconds
        self._executor = executor

    def __repr__(self) -> str:
        return "TrustStorePublisher()"

    async def publish(self, revocation_list: SignedRevocationList) -> PublishedRevocationList:
        head = await self._step(
            PublishStep.PUT_OBJECT,
            lambda: self._storage.put_object(self._key, revocation_list.pem, CRL_CONTENT_TYPE),
        )
        version = head.version_id
        if not version:
            _log.error("ca/crl.pem sin versión: el depósito vigia-edge debe estar versionado")
            raise RevocationListPublishFailed(PublishStep.PUT_OBJECT)
        previous = await self._call(PublishStep.LIST_REVOCATIONS, self._revocation_ids)
        added = await self._call(PublishStep.ADD_REVOCATIONS, lambda: self._add(version))
        entries = await self._call(PublishStep.VERIFY_REVOCATIONS, lambda: self._entries(added))
        if entries != revocation_list.entries:
            _log.error("el almacén de confianza no cuenta las entradas de la lista publicada")
            raise RevocationListPublishFailed(PublishStep.VERIFY_REVOCATIONS)
        stale = sorted(previous - {added})
        if stale:
            await self._call(PublishStep.REMOVE_REVOCATIONS, lambda: self._remove(stale))
        _log.info("lista de revocación publicada en el almacén de confianza")
        return PublishedRevocationList(
            object_version_id=version, revocation_id=added, removed=len(stale)
        )

    # --- pasos --------------------------------------------------------------------------------

    async def _step[T](self, step: PublishStep, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            async with asyncio.timeout(self._timeout):
                return await operation()
        except TimeoutError:
            _log.warning("paso de la publicación sin respuesta en su tope", code=step.value)
        except Exception:
            _log.error("paso de la publicación fallido", code=step.value)
        raise RevocationListPublishFailed(step)

    async def _call[T](self, step: PublishStep, function: Callable[[], T]) -> T:
        """``function`` (boto3, síncrona) en el pool de hilos con el tope del paso."""

        async def run() -> T:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, function)

        return await self._step(step, run)

    def _revocation_ids(self) -> set[int]:
        found: set[int] = set()
        marker: str | None = None
        for _ in range(_MAX_PAGES):
            arguments: dict[str, Any] = {"TrustStoreArn": self._arn}
            if marker is not None:
                arguments["Marker"] = marker
            response = self._elb.describe_trust_store_revocations(**arguments)
            for item in response.get("TrustStoreRevocations", ()):
                found.add(int(item["RevocationId"]))
            marker = response.get("NextMarker")
            if not marker:
                return found
        raise RuntimeError("demasiadas páginas de revocaciones en el almacén")

    def _add(self, version: str) -> int:
        response = self._elb.add_trust_store_revocations(
            TrustStoreArn=self._arn,
            RevocationContents=[
                {
                    "S3Bucket": self._bucket,
                    "S3Key": self._key,
                    "S3ObjectVersion": version,
                    "RevocationType": _REVOCATION_TYPE,
                }
            ],
        )
        (revocation,) = response["TrustStoreRevocations"]
        return int(revocation["RevocationId"])

    def _entries(self, revocation_id: int) -> int:
        response = self._elb.describe_trust_store_revocations(
            TrustStoreArn=self._arn, RevocationIds=[revocation_id]
        )
        (revocation,) = response["TrustStoreRevocations"]
        return int(revocation["NumberOfRevokedEntries"])

    def _remove(self, revocation_ids: list[int]) -> None:
        try:
            self._elb.remove_trust_store_revocations(
                TrustStoreArn=self._arn, RevocationIds=revocation_ids
            )
        except Exception as error:
            if _error_code(error) not in _NOT_FOUND:
                raise
            # Alguna ya no estaba: se retiran una a una las que queden.
            for revocation_id in revocation_ids:
                try:
                    self._elb.remove_trust_store_revocations(
                        TrustStoreArn=self._arn, RevocationIds=[revocation_id]
                    )
                except Exception as single:
                    if _error_code(single) not in _NOT_FOUND:
                        raise
