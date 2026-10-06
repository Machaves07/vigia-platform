"""Dobles de la lista de revocación global (TASK-220).

- ``FakeTrustStore``: el servicio de balanceo con la **misma interfaz** que el cliente ``elbv2`` de
  boto3 para ``AddTrustStoreRevocations``, ``DescribeTrustStoreRevocations`` y
  ``RemoveTrustStoreRevocations`` (LocalStack comunitario no implementa los almacenes de
  confianza; nunca una cuenta real, A-47). Cuenta las entradas leyendo la lista del depósito por
  su versión exacta (``fetch``), como el almacén real; cada operación puede **fallar** o
  **colgarse** (espera hasta que la prueba la suelte).
- ``MemoryEdge``: el depósito ``vigia-edge`` versionado en memoria (``put_object`` con
  ``VersionId`` y ``get_object``), que también falla o se cuelga a voluntad.
- ``MemoryStates``, ``MemoryCredentials`` y ``MemoryScope``: la fila global, las credenciales por
  organización y el ``GlobalTaskScope`` en memoria, con la misma semántica que los adaptadores de
  PostgreSQL (escritura condicional por generación, candado de publicación).
- ``crl_of``: la lista publicada leída con ``cryptography``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import itertools
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from cryptography import x509
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader

from tests.factories import make_context
from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.fleet.domain.revocation_list import (
    CredentialRevocationFacts,
    RevocationListStatus,
)
from vigia_platform.shared.context import ActorKind
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.storage import ChecksumType, ObjectHead

TRUST_STORE_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:000000000000"
    ":truststore/vigia-node-trust/0123456789abcdef"
)
"""ARN sintético (cuenta 000000000000 de LocalStack): nunca uno real (A-47)."""
HANG_LIMIT_SECONDS = 60.0
"""Una operación colgada se suelta sola a los 60 s si la prueba no la soltó antes."""


class Switch:
    """Fallo o cuelgue a voluntad de una operación de un doble."""

    def __init__(self) -> None:
        self.fail: set[str] = set()
        self.hang: set[str] = set()
        self.released = threading.Event()
        self.calls: list[str] = []

    def check(self, operation: str) -> None:
        self.calls.append(operation)
        if operation in self.hang:
            self.released.wait(HANG_LIMIT_SECONDS)
        if operation in self.fail:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "doble"}}, operation
            )

    def release(self) -> None:
        self.released.set()


@dataclass
class StoredRevocation:
    revocation_id: int
    bucket: str
    key: str
    version: str
    entries: int


class FakeTrustStore:
    """``elbv2`` en memoria: solo las tres operaciones de revocaciones del almacén."""

    def __init__(self, fetch: Callable[[str, str, str], bytes], *, arn: str = TRUST_STORE_ARN):
        self._fetch = fetch
        self._arn = arn
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.revocations: dict[int, StoredRevocation] = {}
        self.switch = Switch()
        self.page_size = 2
        self.added: list[StoredRevocation] = []
        self.removed: list[int] = []

    def _same(self, arn: str) -> None:
        if arn != self._arn:
            raise ClientError({"Error": {"Code": "TrustStoreNotFound"}}, "trust_store")

    def add_trust_store_revocations(
        self, *, TrustStoreArn: str, RevocationContents: Sequence[Mapping[str, str]]
    ) -> dict[str, Any]:
        self.switch.check("add")
        self._same(TrustStoreArn)
        (content,) = RevocationContents
        assert content["RevocationType"] == "CRL"
        data = self._fetch(content["S3Bucket"], content["S3Key"], content["S3ObjectVersion"])
        entries = len(x509.load_pem_x509_crl(data))
        with self._lock:
            stored = StoredRevocation(
                next(self._ids),
                content["S3Bucket"],
                content["S3Key"],
                content["S3ObjectVersion"],
                entries,
            )
            self.revocations[stored.revocation_id] = stored
            self.added.append(stored)
        return {"TrustStoreRevocations": [self._shape(stored)]}

    def describe_trust_store_revocations(
        self,
        *,
        TrustStoreArn: str,
        RevocationIds: Sequence[int] | None = None,
        Marker: str | None = None,
    ) -> dict[str, Any]:
        self.switch.check("describe" if RevocationIds is None else "describe_ids")
        self._same(TrustStoreArn)
        with self._lock:
            if RevocationIds is not None:
                missing = [i for i in RevocationIds if i not in self.revocations]
                if missing:
                    raise ClientError(
                        {"Error": {"Code": "RevocationIdNotFound"}}, "DescribeTrustStoreRevocations"
                    )
                return {
                    "TrustStoreRevocations": [
                        self._shape(self.revocations[i]) for i in RevocationIds
                    ]
                }
            ordered = sorted(self.revocations)
        start = int(Marker) if Marker else 0
        page = ordered[start : start + self.page_size]
        response: dict[str, Any] = {
            "TrustStoreRevocations": [self._shape(self.revocations[i]) for i in page]
        }
        if start + self.page_size < len(ordered):
            response["NextMarker"] = str(start + self.page_size)
        return response

    def remove_trust_store_revocations(
        self, *, TrustStoreArn: str, RevocationIds: Sequence[int]
    ) -> dict[str, Any]:
        self.switch.check("remove")
        self._same(TrustStoreArn)
        with self._lock:
            if any(i not in self.revocations for i in RevocationIds):
                raise ClientError(
                    {"Error": {"Code": "RevocationIdNotFound"}}, "RemoveTrustStoreRevocations"
                )
            for revocation_id in RevocationIds:
                del self.revocations[revocation_id]
                self.removed.append(revocation_id)
        return {}

    def _shape(self, stored: StoredRevocation) -> dict[str, Any]:
        return {
            "TrustStoreArn": self._arn,
            "RevocationId": stored.revocation_id,
            "RevocationType": "CRL",
            "NumberOfRevokedEntries": stored.entries,
        }

    def current(self) -> list[StoredRevocation]:
        with self._lock:
            return [self.revocations[i] for i in sorted(self.revocations)]


class MemoryEdge:
    """``vigia-edge`` versionado en memoria (``CrlStorage`` y ``RootObjects``)."""

    def __init__(self, bucket: str = "vigia-edge-prueba") -> None:
        self.bucket = bucket
        self.versions: dict[str, list[tuple[str, bytes]]] = {}
        self.switch = Switch()

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        kms_key_id: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectHead:
        await _in_thread(lambda: self.switch.check("put_object"))
        version = uuid.uuid4().hex
        self.versions.setdefault(key, []).append((version, body))
        return ObjectHead(
            key=key,
            size_bytes=len(body),
            checksum_sha256=None,
            checksum_type=ChecksumType.FULL_OBJECT,
            content_type=content_type,
            metadata={},
            version_id=version,
        )

    async def get_object(self, key: str, *, version_id: str | None = None) -> bytes:
        return self.fetch(self.bucket, key, version_id)

    def fetch(self, bucket: str, key: str, version_id: str | None) -> bytes:
        assert bucket == self.bucket
        stored = self.versions[key]
        if version_id is None:
            return stored[-1][1]
        return next(body for version, body in stored if version == version_id)

    def put_root(self, body: bytes) -> None:
        self.versions.setdefault("ca/root.pem", []).append((uuid.uuid4().hex, body))


async def _in_thread(function: Callable[[], None]) -> None:
    import asyncio

    await asyncio.get_running_loop().run_in_executor(None, function)


def crl_of(data: bytes) -> x509.CertificateRevocationList:
    return x509.load_pem_x509_crl(data)


def histogram_points(reader: InMemoryMetricReader, name: MetricName) -> list[dict[str, Any]]:
    """Los atributos de cada punto del histograma ``name``."""
    data = reader.get_metrics_data()
    points: list[dict[str, Any]] = []
    if data is None:
        return points
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name.value:
                    points.extend(
                        dict(point.attributes or {})
                        for point in metric.data.data_points
                        if isinstance(point, HistogramDataPoint)
                    )
    return points


# --- Estado, credenciales y alcance en memoria -------------------------------------------------


class MemoryStates:
    """``RevocationListStateStore`` con la semántica de ``PostgresRevocationListStateStore``."""

    def __init__(self, organization_ids: list[uuid.UUID] | None = None) -> None:
        self.organization_ids = list(organization_ids or [])
        """Todas las organizaciones, suspendidas incluidas (como la función de gob_0025)."""
        self.dirty_generation = 0
        self.published_generation = 0
        self.dirty_since: dt.datetime | None = None
        self.published_at: dt.datetime | None = None
        self.object_version_id: str | None = None
        self.next_update: dt.datetime | None = None
        self.entries = 0
        self.crl_number = 0
        self.locked = False

    def mark_dirty(self, now: dt.datetime) -> int:
        """Lo que hace la revocación en su transacción (``PostgresRevocationMarkStore``)."""
        self.dirty_generation += 1
        if self.dirty_since is None:
            self.dirty_since = now
        return self.dirty_generation

    @property
    def dirty(self) -> bool:
        return self.dirty_generation > self.published_generation

    async def try_lock_publication(self, transaction: Any) -> bool:
        if self.locked:
            return False
        self.locked = True
        transaction.on_close.append(self._unlock)
        return True

    def _unlock(self) -> None:
        self.locked = False

    async def organizations(self, transaction: Any) -> tuple[uuid.UUID, ...]:
        return tuple(self.organization_ids)

    async def status(self, transaction: Any) -> RevocationListStatus:
        return RevocationListStatus(
            dirty_generation=self.dirty_generation,
            published_generation=self.published_generation,
            published_at=self.published_at,
            next_update=self.next_update,
            entries=self.entries,
            crl_number=self.crl_number,
            object_version_id=self.object_version_id,
        )

    async def reserve_crl_number(self, transaction: Any) -> int:
        self.crl_number += 1
        return self.crl_number

    async def record_publication(
        self,
        transaction: Any,
        *,
        generation: int,
        started_at: dt.datetime,
        published_at: dt.datetime,
        object_version_id: str,
        next_update: dt.datetime,
        entries: int,
    ) -> bool:
        assert generation <= self.dirty_generation
        self.published_generation = max(self.published_generation, generation)
        self.dirty_since = started_at if self.dirty else None
        self.published_at = published_at
        self.object_version_id = object_version_id
        self.next_update = next_update
        self.entries = entries
        return not self.dirty


@dataclass
class MemoryCredential:
    organization_id: uuid.UUID
    serial: str
    status: CredentialStatus
    issued_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None = None
    successor_issued_at: dt.datetime | None = None


@dataclass
class MemoryCredentials:
    """``RevocationFactsReader``: solo las credenciales de la organización de la transacción."""

    rows: list[MemoryCredential] = field(default_factory=list)
    reads: list[uuid.UUID] = field(default_factory=list)

    async def revocation_facts(
        self, transaction: Any, now: dt.datetime
    ) -> tuple[CredentialRevocationFacts, ...]:
        organization_id = transaction.context.organization_id
        self.reads.append(organization_id)
        return tuple(
            CredentialRevocationFacts(
                certificate_serial=row.serial,
                status=row.status,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
                successor_issued_at=row.successor_issued_at,
            )
            for row in self.rows
            if row.organization_id == organization_id
            and row.status is not CredentialStatus.ACTIVE
            and row.expires_at > now
        )

    def revoke(self, serial: str, now: dt.datetime) -> None:
        for index, row in enumerate(self.rows):
            if row.serial == serial:
                self.rows[index] = replace(row, status=CredentialStatus.REVOKED, revoked_at=now)


@dataclass
class FakeTransaction:
    context: Any
    on_close: list[Callable[[], None]] = field(default_factory=list)


@dataclass
class MemoryScope:
    """``GlobalTaskScope`` en memoria: una lectura por organización, en su contexto."""

    organization_ids: list[uuid.UUID]
    task_name: str = "regenerate_revocation_list"
    lease_lost: bool = False
    read_contexts: list[uuid.UUID] = field(default_factory=list)

    async def organizations(self) -> tuple[uuid.UUID, ...]:
        return tuple(self.organization_ids)

    @contextlib.asynccontextmanager
    async def read(self, organization_id: uuid.UUID) -> AsyncIterator[FakeTransaction]:
        self.read_contexts.append(organization_id)
        yield FakeTransaction(make_context(organization_id=organization_id, kind=ActorKind.SYSTEM))

    @contextlib.asynccontextmanager
    async def control(self) -> AsyncIterator[FakeTransaction]:
        transaction = FakeTransaction(make_context(kind=ActorKind.SYSTEM))
        try:
            yield transaction
        finally:
            for close in transaction.on_close:
                close()

    async def ensure_lease(self) -> None:
        if self.lease_lost:
            from vigia_platform.shared.worker.leases import LeaseLost

            raise LeaseLost()
