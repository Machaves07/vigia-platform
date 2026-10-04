"""``catalog.documents`` de extremo a extremo: PostgreSQL 16 y LocalStack (TASK-210, LC-GOB-05).

La aplicación real (``create_app`` con la cadena de middleware y las rutas de ``platform_units()``)
como ``vigia_app``, con sesiones, contextos y ``ContextAuthorizer`` reales, y ``DocumentService``
sobre un ``S3Storage`` real contra un depósito versionado de LocalStack. El almacén va envuelto en
un doble que **cuenta** cada operación (y hace fallar cualquiera que no sea ``head_object`` o
``presign_put``) y que puede apuntar a un almacén detenido o colgado.

``commissioning.run`` solo está en la columna ``provider_installer``: toda petición es de un
instalador bajo concesión del cliente (``X-Vigia-Concession``).

- Criterio 1: el ``PUT`` con exactamente ``required_headers`` sube; ``verify_document_refs`` acepta
  sin ``get_object``.
- Criterio 2: otro tamaño, tipo o suma, objeto inexistente, otra planta, otro ``kind``, más de 10
  referencias: ``DocumentRequestInvalid`` y la concesión sigue ``issued``.
- Criterio 3: dos registros simultáneos que citan el mismo documento: exactamente uno ``used``.
- Criterio 4: almacén detenido o colgado: ``storage_unavailable`` con ``retry_after_seconds`` y
  nada escrito; al restablecerse, el reintento funciona.
- Criterio 5: cuerpos fuera de los límites: ``invalid_request`` sin concesión.
- Criterio 6: la clave sigue el patrón de planta.
- Criterio 7: planta de otra organización, de otra planta o inexistente: ``not_found``; una
  referencia de otra organización o de otra planta no se verifica.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest

from tests.api_support import World
from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.adapters.http import CATALOG_DOCUMENTS_STATE_KEY
from vigia_platform.catalog.adapters.s3.documents import DocumentObjectStore
from vigia_platform.catalog.application.documents import DocumentService, VerifiedDocuments
from vigia_platform.catalog.domain.documents import (
    MAX_DOCUMENT_REFS,
    DocumentRef,
    DocumentRequestInvalid,
)
from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.identity.adapters.authz_store import LedgerProviderQueryLedger
from vigia_platform.identity.auth.sessions import SESSION_COOKIE_NAME, SessionCookie
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.api.middleware import ContextAuthorizer
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.storage import (
    ObjectHead,
    PresignedRequest,
    S3Storage,
    StorageUnavailable,
    sha256_b64,
)

pytestmark = pytest.mark.integration

ORIGIN: Final = "https://app.vigia.test"
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CONCESSION_HEADER: Final = "X-Vigia-Concession"
TIMEOUT_SECONDS: Final = 30.0
STORE_TIMEOUT_SECONDS: Final = 2.0
"""Topes del almacén en esta prueba: el doble colgado nunca responde, así que vencen siempre."""
LOCK_WAIT_CAP_SECONDS: Final = 30.0
HOUR: Final = timedelta(hours=1)
PDF: Final = b"%PDF-1.7\n% acta de alcance sintetica\n" + bytes(range(256))
PNG: Final = b"\x89PNG\r\n\x1a\n captura sintetica del difuminado" + bytes(range(64))
UUID_TEXT: Final = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- Almacén con contador ----------------------------------------------------------------------


class Hung:
    """Almacén colgado: nunca responde."""

    async def head_object(self, key: str) -> ObjectHead | None:
        await asyncio.Event().wait()
        raise AssertionError("inalcanzable")  # pragma: no cover

    async def presign_put(self, *_: Any, **__: Any) -> PresignedRequest:
        await asyncio.Event().wait()
        raise AssertionError("inalcanzable")  # pragma: no cover


class CountingStorage:
    """Delegado conmutable que cuenta cada operación; cualquier otra que ``head_object`` y
    ``presign_put`` (``get_object`` incluida) queda contada y falla."""

    def __init__(self, target: Any) -> None:
        self.target = target
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def forbidden(*_: Any, **__: Any) -> Any:
            self.calls[name] += 1
            raise AssertionError(f"operación prohibida en documentos: {name}")

        return forbidden

    async def head_object(self, key: str) -> ObjectHead | None:
        self.calls["head_object"] += 1
        result: ObjectHead | None = await self.target.head_object(key)
        return result

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = timedelta(minutes=15),
    ) -> PresignedRequest:
        self.calls["presign_put"] += 1
        presigned: PresignedRequest = await self.target.presign_put(
            key, content_type, checksum_sha256, required_headers, ttl
        )
        return presigned


# --- Entorno ------------------------------------------------------------------------------------


@dataclass
class Documents:
    authz: AuthzEnvironment
    s3: Any
    bucket: str
    real: S3Storage
    stopped: S3Storage
    storage: CountingStorage
    service: DocumentService
    client: httpx.AsyncClient
    a: Site
    b: Site

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    def plants(self, site: Site) -> list[uuid.UUID]:
        return list(site.plants)

    # --- Personas -------------------------------------------------------------------------------

    def installer(
        self,
        site: Site,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        plant_id: uuid.UUID | None = None,
    ) -> tuple[SessionCookie, uuid.UUID, ScopeContext]:
        """Instalador del proveedor con una concesión vigente sobre ``site`` y su contexto."""
        authz = self.authz
        installer = authz.add_provider_user()
        concession = authz.add_concession(
            site.organization_id,
            installer,
            level=level,
            scope_id=plant_id,
            granted_at=authz.now() - HOUR,
        )
        cookie = authz.open_session(authz.provider_organization_id, installer)
        scope = self.run(authz.contexts.context_from_session(cookie, concession_id=concession))
        context: ScopeContext = scope.context
        return cookie, concession, context

    def member(self, site: Site, role: Role) -> SessionCookie:
        user = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user, role)
        cookie: SessionCookie = self.authz.open_session(site.organization_id, user)
        return cookie

    # --- Peticiones -----------------------------------------------------------------------------

    def post(
        self, cookie: SessionCookie, concession: uuid.UUID | None, body: Any
    ) -> httpx.Response:
        headers = {**SAME_ORIGIN, "Cookie": f"{SESSION_COOKIE_NAME}={cookie.value}"}
        if concession is not None:
            headers[CONCESSION_HEADER] = str(concession)
        response: httpx.Response = self.run(
            self.client.post("/documents", json=body, headers=headers)
        )
        return response

    def grants(self, organization_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT * FROM catalog.document_upload_grant WHERE organization_id = $1"
            " ORDER BY document_id",
            organization_id,
        )

    def status(self, document_id: uuid.UUID) -> str:
        (row,) = self.fetch(
            "SELECT status FROM catalog.document_upload_grant WHERE document_id = $1", document_id
        )
        return str(row["status"])

    def audit(self, organization_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT operation, outcome, actor_concession_id, scope_plant_id, resource_kind,"
            " resource_id FROM shared.audit_entry WHERE organization_id = $1"
            " AND operation = 'document_upload_granted' ORDER BY chain_sequence",
            organization_id,
        )

    # --- Documentos -----------------------------------------------------------------------------

    def issue(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        data: bytes = PDF,
        *,
        kind: DocumentKind = DocumentKind.SCOPE_RECORD,
        content_type: str = "application/pdf",
    ) -> tuple[DocumentRef, PresignedRequest]:
        issued = self.run(
            self.service.issue(
                context,
                plant_id=plant_id,
                kind=kind.value,
                content_type=content_type,
                size_bytes=len(data),
                sha256=_sha(data),
            )
        )
        return issued.grant.ref, issued.upload

    def upload(self, url: str, headers: Mapping[str, str], data: bytes) -> httpx.Response:
        return httpx.put(url, content=data, headers=dict(headers), timeout=TIMEOUT_SECONDS)

    def uploaded(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        data: bytes = PDF,
        *,
        kind: DocumentKind = DocumentKind.SCOPE_RECORD,
        content_type: str = "application/pdf",
    ) -> DocumentRef:
        ref, upload = self.issue(context, plant_id, data, kind=kind, content_type=content_type)
        response = self.upload(upload.url, upload.headers, data)
        assert response.status_code == 200, response.text
        return ref

    def put_directly(self, key: str, data: bytes, content_type: str, *, checksum: bool) -> None:
        """Un objeto escrito por otra vía en la clave (lo que la verificación debe rechazar)."""
        extra = {"ChecksumSHA256": sha256_b64(data)} if checksum else {}
        self.s3.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, **extra
        )

    def verify(
        self,
        context: ScopeContext,
        refs: list[Any],
        plant_id: uuid.UUID,
        kinds: tuple[DocumentKind, ...] = (DocumentKind.SCOPE_RECORD,),
    ) -> VerifiedDocuments:
        verified: VerifiedDocuments = self.run(
            self.service.verify_document_refs(context, refs, kinds, plant_id)
        )
        return verified

    def register(self, context: ScopeContext, verified: VerifiedDocuments) -> None:
        """Lo que hará el registro del acta: ``mark_used`` en su transacción."""

        async def write() -> None:
            async with self.authz.sessions.database.transaction(context) as transaction:
                await self.service.mark_used(transaction, verified)

        self.run(write())


@pytest.fixture(scope="module")
def documents(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[Documents]:
    s3 = localstack_endpoint.aws_client("s3")
    with (
        authz_environment(postgres_endpoint, "catalog_documents") as authz,
        versioned_bucket(s3, "vigia-documents") as bucket,
    ):
        # La RLS de las concesiones compara con la hora de la base: el reloj simulado arranca en
        # ella (VIG-135) y todo lo que se compara sale de él.
        clock = authz.sessions.clock
        (row,) = authz.fetch("SELECT now() AS now")
        clock.set(row["now"])
        sessions = authz.sessions
        registry = RecordTypeRegistry()
        for definition in U02_RECORD_TYPES:
            registry.register(definition)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
            registry.seal()

        authz.run(synchronize())
        real = S3Storage(localstack_endpoint.storage_settings(bucket), clock)
        stopped = S3Storage(
            replace(
                localstack_endpoint.storage_settings(bucket),
                endpoint_url="http://127.0.0.1:9",
                connect_timeout_seconds=STORE_TIMEOUT_SECONDS,
                read_timeout_seconds=STORE_TIMEOUT_SECONDS,
            ),
            clock,
        )
        storage = CountingStorage(real)
        service = DocumentService(
            database=sessions.database,
            audit=sessions.audit,
            authorizer=authz.authorizer,
            store=DocumentObjectStore(
                storage,
                presign_timeout_seconds=STORE_TIMEOUT_SECONDS,
                head_timeout_seconds=STORE_TIMEOUT_SECONDS,
            ),
            clock=clock,
        )
        writer = EscritorExpediente(
            database=sessions.database,
            registry=registry,
            free_text=FreeTextPolicyRegistry(),
            evidence=EvidenceVerifier(real, clock),
            outbox=sessions.outbox,
            clock=clock,
        )
        app = World(clock=clock).app(
            units=None,
            runtime={
                "sessions": authz.contexts,
                "authorizer": ContextAuthorizer(
                    audit=authz.audit,
                    provider_organization_id=authz.provider_organization_id,
                    provider_queries=LedgerProviderQueryLedger(writer),
                    clock=clock,
                ),
                "state": {CATALOG_DOCUMENTS_STATE_KEY: service},
            },
            public_origin=ORIGIN,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            timeout=TIMEOUT_SECONDS,
        )
        world = Documents(
            authz,
            s3,
            bucket,
            real,
            stopped,
            storage,
            service,
            client,
            authz.add_site(plants=2, zones_per_plant=1),
            authz.add_site(plants=2, zones_per_plant=1),
        )
        try:
            yield world
        finally:
            authz.run(client.aclose())


@pytest.fixture(autouse=True)
def _real_store(documents: Documents) -> Iterator[None]:
    documents.storage.target = documents.real
    start = documents.authz.sessions.clock.now()
    yield
    documents.storage.target = documents.real
    documents.authz.sessions.clock.set(start)


def _body(plant: uuid.UUID, data: bytes = PDF, /, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "plant_id": str(plant),
        "kind": "scope_record",
        "content_type": "application/pdf",
        "size_bytes": len(data),
        "sha256": _sha(data),
    }
    body.update(changes)
    return body


def _code(response: httpx.Response) -> str | None:
    body = response.json()
    return body.get("code") if isinstance(body, dict) else None


# --- Criterios 1 y 6: concesión, subida y verificación sin descarga -----------------------------


def test_a_put_with_exactly_the_required_headers_uploads_and_verifies_without_download(
    documents: Documents,
) -> None:
    site = documents.a
    plant = documents.plants(site)[0]
    cookie, concession, context = documents.installer(site)
    audited = len(documents.audit(site.organization_id))
    response = documents.post(cookie, concession, _body(plant))
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"document_id", "upload", "document_ref"}
    document_id = uuid.UUID(body["document_id"])
    ref = body["document_ref"]
    # Criterio 6: la clave exacta del patrón de planta, disjunta de node/ y closure/.
    assert ref["storage_key"] == (
        f"org/{site.organization_id}/plant/{plant}/documents/{document_id}.pdf"
    )
    pattern = rf"org/{UUID_TEXT}/plant/{UUID_TEXT}/documents/{UUID_TEXT}\.pdf"
    assert re.fullmatch(pattern, ref["storage_key"])
    assert not {"node", "closure", "zone"} & set(ref["storage_key"].split("/"))
    assert ref == {
        "document_id": str(document_id),
        "storage_key": ref["storage_key"],
        "sha256": _sha(PDF),
        "content_type": "application/pdf",
        "size_bytes": len(PDF),
    }
    upload = body["upload"]
    assert upload["method"] == "PUT"
    assert upload["required_headers"] == {
        "content-type": "application/pdf",
        "x-amz-checksum-sha256": sha256_b64(PDF),
    }
    (row,) = documents.grants(site.organization_id)[-1:]
    assert row["document_id"] == document_id and row["status"] == "issued"
    assert row["expires_at"] - row["issued_at"] == timedelta(minutes=15)
    assert upload["expires_at"].startswith(row["expires_at"].strftime("%Y-%m-%dT%H:%M:%S"))
    # La concesión del proveedor queda auditada en el cliente, con su concesión (BR-NUC-38).
    entries = documents.audit(site.organization_id)[audited:]
    assert [(e["outcome"], e["actor_concession_id"], e["scope_plant_id"]) for e in entries] == [
        ("success", concession, plant)
    ]
    assert entries[0]["resource_kind"] == "document" and entries[0]["resource_id"] == document_id

    # La URL firma el tipo y la suma: otros bytes u otro tipo no suben.
    headers = upload["required_headers"]
    tampered = documents.upload(upload["url"], headers, PDF[:-1] + b"X")
    assert tampered.status_code == 400 and "BadDigest" in tampered.text
    other_type = documents.upload(upload["url"], {**headers, "content-type": "image/png"}, PDF)
    assert other_type.status_code == 403, other_type.text
    no_checksum = documents.upload(upload["url"], {"content-type": "application/pdf"}, PDF)
    assert no_checksum.status_code in (400, 403), no_checksum.text
    # Con exactamente required_headers, sube.
    assert documents.upload(upload["url"], headers, PDF).status_code == 200

    before = Counter(documents.storage.calls)
    verified = documents.verify(context, [ref], plant)
    assert verified.document_ids == (document_id,)
    calls = documents.storage.calls - before
    assert calls == Counter({"head_object": 1})
    assert documents.storage.calls["get_object"] == 0
    assert documents.status(document_id) == "issued"  # verificar no escribe
    documents.register(context, verified)
    assert documents.status(document_id) == "used"
    # Ya usada: una segunda cita no se verifica, y se rechaza sin consultar el almacén.
    before = Counter(documents.storage.calls)
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(context, [ref], plant)
    assert documents.storage.calls == before


@pytest.mark.parametrize(
    ("content_type", "data", "ext"),
    [("image/jpeg", b"\xff\xd8\xff jpeg sintetico", "jpg"), ("image/png", PNG, "png")],
)
def test_images_upload_with_their_extension(
    documents: Documents, content_type: str, data: bytes, ext: str
) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    ref = documents.uploaded(
        context, plant, data, kind=DocumentKind.BLUR_CHECK_CAPTURE, content_type=content_type
    )
    assert ref.storage_key.endswith(f"/{ref.document_id}.{ext}")
    verified = documents.verify(context, [ref], plant, (DocumentKind.BLUR_CHECK_CAPTURE,))
    assert verified.refs == (ref,)


# --- Criterio 2: discrepancias, sin cambiar la concesión ----------------------------------------


MISMATCHES: Final = {
    "otro_tamano": lambda key: (key, PDF + b"!", "application/pdf", True),
    "otro_tipo": lambda key: (key, PDF, "image/png", True),
    "otra_suma_mismo_tamano": lambda key: (key, PDF[:-1] + b"?", "application/pdf", True),
    "sin_suma_sha256": lambda key: (key, PDF, "application/pdf", False),
}


@pytest.mark.parametrize("case", sorted(MISMATCHES))
def test_an_object_that_is_not_the_granted_one_is_invalid(documents: Documents, case: str) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    ref, _ = documents.issue(context, plant)
    key, data, content_type, checksum = MISMATCHES[case](ref.storage_key)
    documents.put_directly(key, data, content_type, checksum=checksum)
    with pytest.raises(DocumentRequestInvalid) as raised:
        documents.verify(context, [ref], plant)
    # Mensaje genérico: ni la clave, ni la suma, ni el campo que no coincide.
    assert ref.storage_key not in str(raised.value) and ref.sha256 not in str(raised.value)
    assert documents.status(ref.document_id) == "issued"


def test_a_missing_object_is_invalid_and_the_grant_stays_issued(documents: Documents) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    ref, _ = documents.issue(context, plant)
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(context, [ref], plant)
    assert documents.status(ref.document_id) == "issued"


def test_another_kind_or_an_altered_ref_is_invalid(documents: Documents) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    ref = documents.uploaded(context, plant)
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(context, [ref], plant, (DocumentKind.USE_AGREEMENT,))
    for altered in (
        replace(ref, size_bytes=ref.size_bytes + 1),
        replace(ref, sha256="0" * 64),
        replace(ref, storage_key=ref.storage_key.replace(".pdf", ".png")),
        replace(ref, document_id=uuid.uuid4()),
    ):
        with pytest.raises(DocumentRequestInvalid):
            documents.verify(context, [altered], plant)
    assert documents.status(ref.document_id) == "issued"
    # Con el kind esperado y la referencia exacta, sí.
    assert documents.verify(context, [ref], plant, (DocumentKind.SCOPE_RECORD,)).refs == (ref,)


def test_ten_refs_are_accepted_and_eleven_are_invalid_before_any_lookup(
    documents: Documents,
) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    refs = [documents.uploaded(context, plant, PDF + bytes([n])) for n in range(MAX_DOCUMENT_REFS)]
    before = Counter(documents.storage.calls)
    verified = documents.verify(context, refs, plant)
    assert len(verified.refs) == MAX_DOCUMENT_REFS
    assert (documents.storage.calls - before)["head_object"] == MAX_DOCUMENT_REFS
    extra = documents.uploaded(context, plant, PDF + b"once")
    before = Counter(documents.storage.calls)
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(context, [*refs, extra], plant)
    assert documents.storage.calls == before
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(context, [refs[0], refs[0]], plant)
    assert {documents.status(ref.document_id) for ref in [*refs, extra]} == {"issued"}


def test_an_expired_grant_without_object_is_expired_and_not_rewritten(
    documents: Documents,
) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    clock = documents.authz.sessions.clock
    missing, _ = documents.issue(context, plant)
    in_time = documents.uploaded(context, plant, PDF + b"a tiempo")
    clock.advance(15 * 60)
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(context, [missing], plant)
    assert documents.status(missing.document_id) == "issued"  # derivado, no escrito
    # Subido mientras la URL valía: se acepta aunque ya haya vencido.
    assert documents.verify(context, [in_time], plant).refs == (in_time,)


# --- Criterio 7: guardas de alcance -------------------------------------------------------------


def test_a_ref_of_another_plant_or_organization_is_not_verified(documents: Documents) -> None:
    a_plant, a_other = documents.plants(documents.a)
    # Concesión de toda la organización: la RLS deja ver las dos plantas; el filtro de planta no.
    _, _, context = documents.installer(documents.a)
    ref = documents.uploaded(context, a_plant)
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(context, [ref], a_other)
    assert documents.status(ref.document_id) == "issued"
    # La misma referencia desde la otra organización, con la planta de A: tampoco.
    _, _, other = documents.installer(documents.b)
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(other, [ref], a_plant)
    # Y con su propia planta, la de B.
    with pytest.raises(DocumentRequestInvalid):
        documents.verify(other, [ref], documents.plants(documents.b)[0])
    assert documents.verify(context, [ref], a_plant).refs == (ref,)


def test_post_documents_out_of_scope_answers_not_found_and_grants_nothing(
    documents: Documents,
) -> None:
    a, b = documents.a, documents.b
    a_plant, a_other = documents.plants(a)
    b_plant = documents.plants(b)[0]
    cookie, concession, _ = documents.installer(a, ScopeLevel.PLANT, a_plant)
    organization_cookie, organization_concession, _ = documents.installer(a)
    before = (len(documents.grants(a.organization_id)), len(documents.grants(b.organization_id)))
    missing = documents.post(organization_cookie, organization_concession, _body(uuid.uuid4()))
    cases = {
        "planta_de_otra_organizacion": documents.post(
            organization_cookie, organization_concession, _body(b_plant)
        ),
        "otra_planta_fuera_de_la_concesion": documents.post(cookie, concession, _body(a_other)),
        "planta_inexistente": missing,
        "concesion_de_planta_planta_inexistente": documents.post(
            cookie, concession, _body(uuid.uuid4())
        ),
        "miembro_sin_commissioning_run": documents.post(
            documents.member(a, Role.ADMINISTRATOR), None, _body(a_plant)
        ),
    }
    for name, response in cases.items():
        assert response.status_code == 404, (name, response.text)
        assert _code(response) == "not_found", name
        body = {k: v for k, v in response.json().items() if k != "correlation_id"}
        expected = {k: v for k, v in missing.json().items() if k != "correlation_id"}
        assert body == expected, name
        assert str(b_plant) not in response.text and str(a_other) not in response.text
    after = (len(documents.grants(a.organization_id)), len(documents.grants(b.organization_id)))
    assert after == before
    # La concesión de planta sí concede en su planta.
    assert documents.post(cookie, concession, _body(a_plant)).status_code == 201


# --- Criterio 5: cuerpos fuera de los límites ---------------------------------------------------


INVALID_BODIES: Final = {
    "tamano_cero": {"size_bytes": 0},
    "tamano_maximo_mas_uno": {"size_bytes": 20_971_521},
    "tamano_booleano": {"size_bytes": True},
    "tamano_texto": {"size_bytes": "10"},
    "tamano_flotante": {"size_bytes": 10.0},
    "tipo_gif": {"content_type": "image/gif"},
    "tipo_mayusculas": {"content_type": "Application/PDF"},
    "tipo_con_parametro": {"content_type": "application/pdf; charset=binary"},
    "tipo_invisible": {"content_type": "application/pdf" + chr(0x200B)},
    "kind_desconocido": {"kind": "evidence"},
    "kind_homoglifo": {"kind": "scope_rec" + chr(0x043E) + "rd"},
    "suma_mayusculas": {"sha256": _sha(PDF).upper()},
    "suma_corta": {"sha256": _sha(PDF)[:-1]},
    "campo_de_mas": {"zone_id": str(uuid.uuid4())},
    "planta_no_uuid": {"plant_id": "planta-1"},
}


@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
def test_bodies_out_of_the_limits_are_invalid_request_without_grant(
    documents: Documents, case: str
) -> None:
    site = documents.a
    plant = documents.plants(site)[0]
    cookie, concession, _ = documents.installer(site)
    before = len(documents.grants(site.organization_id))
    presigned = documents.storage.calls["presign_put"]
    response = documents.post(cookie, concession, _body(plant, **INVALID_BODIES[case]))
    assert response.status_code == 400, response.text
    assert _code(response) == "invalid_request"
    assert len(documents.grants(site.organization_id)) == before
    assert documents.storage.calls["presign_put"] == presigned


def test_a_missing_field_is_invalid_request(documents: Documents) -> None:
    plant = documents.plants(documents.a)[0]
    cookie, concession, _ = documents.installer(documents.a)
    for field in ("plant_id", "kind", "content_type", "size_bytes", "sha256"):
        body = _body(plant)
        del body[field]
        assert _code(documents.post(cookie, concession, body)) == "invalid_request", field


def test_the_largest_document_is_granted(documents: Documents) -> None:
    plant = documents.plants(documents.a)[0]
    cookie, concession, _ = documents.installer(documents.a)
    response = documents.post(cookie, concession, _body(plant, size_bytes=20_971_520))
    assert response.status_code == 201, response.text
    assert response.json()["document_ref"]["size_bytes"] == 20_971_520


# --- Criterio 4: almacén detenido o colgado -----------------------------------------------------


@pytest.mark.parametrize("state", ["detenido", "colgado"])
def test_a_down_or_slow_store_grants_nothing_and_recovers(documents: Documents, state: str) -> None:
    site = documents.a
    plant = documents.plants(site)[0]
    cookie, concession, _ = documents.installer(site)
    organization = site.organization_id
    before = (len(documents.grants(organization)), len(documents.audit(organization)))
    documents.storage.target = documents.stopped if state == "detenido" else Hung()
    response = documents.post(cookie, concession, _body(plant))
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "storage_unavailable"
    assert body["retry_after_seconds"] == 5
    assert response.headers["retry-after"] == "5"
    after = (len(documents.grants(organization)), len(documents.audit(organization)))
    assert after == before
    documents.storage.target = documents.real
    assert documents.post(cookie, concession, _body(plant)).status_code == 201


@pytest.mark.parametrize("state", ["detenido", "colgado"])
def test_verification_with_the_store_down_fails_closed_and_recovers(
    documents: Documents, state: str
) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    ref = documents.uploaded(context, plant, PDF + state.encode())
    documents.storage.target = documents.stopped if state == "detenido" else Hung()
    with pytest.raises(StorageUnavailable) as raised:
        documents.verify(context, [ref], plant)
    assert raised.value.retry_after_seconds == 5
    assert documents.status(ref.document_id) == "issued"
    documents.storage.target = documents.real
    verified = documents.verify(context, [ref], plant)
    documents.register(context, verified)
    assert documents.status(ref.document_id) == "used"


# --- Criterio 3: una sola transición issued → used ----------------------------------------------


_WAITING_UPDATE: Final = (
    "SELECT count(*) AS n FROM pg_catalog.pg_stat_activity"
    " WHERE wait_event_type = 'Lock' AND query LIKE 'UPDATE catalog.document_upload_grant%'"
)


def test_two_simultaneous_registrations_leave_exactly_one_used(documents: Documents) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    ref = documents.uploaded(context, plant, PDF + b"concurrente")
    first_verified = documents.verify(context, [ref], plant)
    second_verified = documents.verify(context, [ref], plant)  # los dos ven issued
    database = documents.authz.sessions.database
    admin = documents.authz.sessions.admin
    first_marked = asyncio.Event()

    async def first() -> str:
        async with database.transaction(context) as transaction:
            await documents.service.mark_used(transaction, first_verified)
            first_marked.set()
            # No confirma hasta que el segundo registro espera el bloqueo de la misma fila.
            async with asyncio.timeout(LOCK_WAIT_CAP_SECONDS):
                while True:
                    (row,) = await admin.fetch(_WAITING_UPDATE)
                    if row["n"] >= 1:
                        break
                    await asyncio.sleep(0.05)
        return "used"

    async def second() -> str:
        await first_marked.wait()
        try:
            async with database.transaction(context) as transaction:
                await documents.service.mark_used(transaction, second_verified)
        except DocumentRequestInvalid:
            return "rejected"
        return "used"

    async def both() -> list[str]:
        return list(await asyncio.gather(first(), second()))

    outcomes = documents.run(both())
    assert sorted(outcomes) == ["rejected", "used"]
    assert documents.status(ref.document_id) == "used"


def test_simultaneous_registrations_without_choreography_never_both_win(
    documents: Documents,
) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    database = documents.authz.sessions.database
    for attempt in range(5):
        ref = documents.uploaded(context, plant, PDF + f"carrera {attempt}".encode())
        verified = documents.verify(context, [ref], plant)

        async def register() -> str:
            try:
                async with database.transaction(context) as transaction:
                    await documents.service.mark_used(transaction, verified)  # noqa: B023
            except DocumentRequestInvalid:
                return "rejected"
            return "used"

        async def race() -> list[str]:
            return list(await asyncio.gather(register(), register(), register()))

        outcomes = documents.run(race())
        assert sorted(outcomes) == ["rejected", "rejected", "used"], (attempt, outcomes)
        assert documents.status(ref.document_id) == "used"


def test_a_registration_of_another_organization_marks_nothing(documents: Documents) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    ref = documents.uploaded(context, plant, PDF + b"ajeno")
    verified = documents.verify(context, [ref], plant)
    _, _, other = documents.installer(documents.b)
    with pytest.raises(DocumentRequestInvalid):
        documents.register(other, verified)
    # Forzado a la organización del otro contexto: la RLS y el filtro no tocan la fila de A.
    forged = VerifiedDocuments(other.organization_id, plant, verified.refs)
    with pytest.raises(DocumentRequestInvalid):
        documents.register(other, forged)
    assert documents.status(ref.document_id) == "issued"


def test_a_registration_for_another_plant_marks_nothing(documents: Documents) -> None:
    plant, other_plant = documents.plants(documents.a)
    _, _, context = documents.installer(documents.a)
    ref = documents.uploaded(context, plant, PDF + b"otra planta")
    verified = documents.verify(context, [ref], plant)
    # El registro de un acta de la otra planta que citara este documento: el UPDATE filtra por la
    # planta del registro y no lo toca (la RLS no lo impide: la concesión es de la organización).
    forged = VerifiedDocuments(verified.organization_id, other_plant, verified.refs)
    with pytest.raises(DocumentRequestInvalid):
        documents.register(context, forged)
    assert documents.status(ref.document_id) == "issued"
    documents.register(context, verified)
    assert documents.status(ref.document_id) == "used"


def test_the_grant_is_issued_at_the_clock_time(documents: Documents) -> None:
    plant = documents.plants(documents.a)[0]
    _, _, context = documents.installer(documents.a)
    now: datetime = documents.authz.sessions.clock.now()
    ref, upload = documents.issue(context, plant)
    (row,) = documents.fetch(
        "SELECT issued_at, expires_at FROM catalog.document_upload_grant WHERE document_id = $1",
        ref.document_id,
    )
    assert row["issued_at"] == now and row["expires_at"] == now + timedelta(minutes=15)
    assert upload.method == "PUT"
