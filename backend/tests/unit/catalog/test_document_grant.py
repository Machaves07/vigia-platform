"""``catalog.documents`` sin red ni base (TASK-210, LC-GOB-05).

- **Límites** (BR §8, nota del 2026-09-20; NFR-GOB-23): tamaño de 1 a 20 971 520 bytes (y al
  configurado), tipo y ``kind`` de la lista cerrada, suma hexadecimal en minúsculas; fuera de eso,
  ``DocumentRequestInvalid`` (``invalid_request``) sin concesión. Bordes justo dentro y fuera, y
  valores hostiles (mayúsculas, espacios, homoglifos, invisibles, booleanos, flotantes).
- **Clave** (infraestructura §4.1): exactamente
  ``org/{organization_id}/plant/{plant_id}/documents/{document_id}.{pdf|jpg|png}``, disjunta de
  ``node/`` y de ``closure/`` (propiedad con Hypothesis); prefijo reservado rechazado.
- **Vencimiento**: ``expires_at = issued_at + 15 min``; ``expired`` se deriva solo sin objeto.
- **``document_ref``**: exactamente sus cinco campos, sin coerción.
- **Almacén** (``DocumentObjectStore``, NFR-GOB-43): firma con tope de 5 s y consulta con tope de
  10 s; colgado, caído o con fallo de ``botocore`` → ``StorageUnavailable`` con
  ``retry_after_seconds``; las consultas van **en paralelo**; nunca se llama a ``get_object``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore import exceptions as botocore_exceptions  # type: ignore[import-untyped]
from hypothesis import given
from hypothesis import strategies as st

from vigia_platform.catalog.adapters.s3.documents import (
    HEAD_TIMEOUT_SECONDS,
    PRESIGN_TIMEOUT_SECONDS,
    DocumentKeyTaken,
    DocumentObjectStore,
)
from vigia_platform.catalog.domain.documents import (
    DOCUMENT_GRANT_TTL,
    DOCUMENTS_MAX_BYTES,
    MAX_DOCUMENT_REFS,
    DocumentContentType,
    DocumentRef,
    DocumentRequestInvalid,
    DocumentSettings,
    DocumentUploadGrant,
    document_storage_key,
)
from vigia_platform.catalog.domain.enums import DocumentKind
from vigia_platform.fleet.domain.enums import UploadGrantStatus
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid
from vigia_platform.shared.storage import (
    CHECKSUM_HEADER,
    CONTENT_TYPE_HEADER,
    RETRY_AFTER_SECONDS,
    ChecksumType,
    ObjectHead,
    PresignedRequest,
    StorageUnavailable,
    sha256_hex_to_b64,
)

T0 = datetime(2026, 10, 3, 12, tzinfo=UTC)
ORGANIZATION = uuid.UUID("0192a000-0000-7000-8000-000000000001")
PLANT = uuid.UUID("0192a000-0000-7000-8000-000000000002")
DOCUMENT = uuid.UUID("0192a000-0000-7000-8000-000000000003")
BODY = b"%PDF-1.7 acta sintetica"
SHA = hashlib.sha256(BODY).hexdigest()
SETTINGS = DocumentSettings()
UUID_TEXT = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KEY_PATTERN = re.compile(
    rf"org/(?P<o>{UUID_TEXT})/plant/(?P<p>{UUID_TEXT})/documents/(?P<d>{UUID_TEXT})"
    r"\.(?P<ext>pdf|jpg|png)"
)


def _issue(**changes: Any) -> DocumentUploadGrant:
    values: dict[str, Any] = {
        "document_id": DOCUMENT,
        "organization_id": ORGANIZATION,
        "plant_id": PLANT,
        "kind": "scope_record",
        "content_type": "application/pdf",
        "size_bytes": len(BODY),
        "sha256": SHA,
        "issued_at": T0,
        "settings": SETTINGS,
    }
    values.update(changes)
    return DocumentUploadGrant.issue(**values)


# --- Concesión: forma y límites -----------------------------------------------------------------


def test_a_grant_is_issued_with_the_exact_key_and_fifteen_minutes() -> None:
    grant = _issue()
    assert grant.storage_key == f"org/{ORGANIZATION}/plant/{PLANT}/documents/{DOCUMENT}.pdf"
    assert grant.status is UploadGrantStatus.ISSUED
    assert grant.kind is DocumentKind.SCOPE_RECORD
    assert grant.content_type is DocumentContentType.PDF
    assert grant.expires_at - grant.issued_at == DOCUMENT_GRANT_TTL == timedelta(minutes=15)
    assert grant.ref.to_json() == {
        "document_id": str(DOCUMENT),
        "storage_key": grant.storage_key,
        "sha256": SHA,
        "content_type": "application/pdf",
        "size_bytes": len(BODY),
    }


@pytest.mark.parametrize(
    ("content_type", "ext"),
    [("application/pdf", "pdf"), ("image/jpeg", "jpg"), ("image/png", "png")],
)
def test_the_extension_comes_from_the_type(content_type: str, ext: str) -> None:
    assert _issue(content_type=content_type).storage_key.endswith(f"/{DOCUMENT}.{ext}")


@pytest.mark.parametrize("kind", [kind.value for kind in DocumentKind])
def test_every_document_kind_is_admitted(kind: str) -> None:
    assert _issue(kind=kind).kind.value == kind


@pytest.mark.parametrize("size", [1, DOCUMENTS_MAX_BYTES])
def test_size_edges_inside_are_accepted(size: int) -> None:
    assert _issue(size_bytes=size).size_bytes == size


@pytest.mark.parametrize(
    "size",
    [0, -1, DOCUMENTS_MAX_BYTES + 1, 2**63, True, False, 1.0, "1", None],
    ids=["cero", "negativo", "max_mas_1", "enorme", "true", "false", "float", "texto", "nulo"],
)
def test_sizes_outside_or_of_another_type_are_invalid(size: object) -> None:
    with pytest.raises(DocumentRequestInvalid):
        _issue(size_bytes=size)


def test_the_configured_maximum_applies() -> None:
    small = DocumentSettings(max_bytes=10)
    assert _issue(size_bytes=10, settings=small).size_bytes == 10
    with pytest.raises(DocumentRequestInvalid):
        _issue(size_bytes=11, settings=small)


HOSTILE_TYPES = [
    "image/gif",
    "text/html",
    "application/octet-stream",
    "Application/PDF",
    "application/pdf ",
    " application/pdf",
    "application/pdf; charset=utf-8",
    "application/pdf" + chr(0x200B),
    "applic" + chr(0x0430) + "tion/pdf",  # a cirilica
    "image/jpg",
    "",
]


@pytest.mark.parametrize("content_type", [*HOSTILE_TYPES, None, 1, b"application/pdf"])
def test_types_outside_the_closed_list_are_invalid(content_type: object) -> None:
    with pytest.raises(DocumentRequestInvalid):
        _issue(content_type=content_type)


@pytest.mark.parametrize(
    "kind",
    [
        "",
        "Scope_Record",
        "scope_record ",
        "scope" + chr(0x200B) + "record",
        "sc" + chr(0x043E) + "pe_record",
        "x",
        None,
    ],
)
def test_kinds_outside_the_closed_list_are_invalid(kind: object) -> None:
    with pytest.raises(DocumentRequestInvalid):
        _issue(kind=kind)


@pytest.mark.parametrize(
    "sha256",
    [SHA.upper(), SHA[:-1], SHA + "0", "g" * 64, f" {SHA[1:]}", "", None, 0],
    ids=["mayusculas", "63", "65", "no_hex", "espacio", "vacia", "nula", "entero"],
)
def test_malformed_checksums_are_invalid(sha256: object) -> None:
    with pytest.raises(DocumentRequestInvalid):
        _issue(sha256=sha256)


def test_issued_at_without_timezone_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="zona horaria"):
        _issue(issued_at=datetime(2026, 10, 3, 12))  # noqa: DTZ001 - el caso que se rechaza


# --- Clave del objeto ---------------------------------------------------------------------------


@given(
    organization=st.uuids(),
    plant=st.uuids(),
    document=st.uuids(),
    content_type=st.sampled_from(list(DocumentContentType)),
)
def test_the_key_follows_the_plant_pattern_and_is_disjoint_from_clips_and_closures(
    organization: uuid.UUID,
    plant: uuid.UUID,
    document: uuid.UUID,
    content_type: DocumentContentType,
) -> None:
    key = document_storage_key(organization, plant, document, content_type, SETTINGS)
    match = KEY_PATTERN.fullmatch(key)
    assert match is not None, key
    assert (match["o"], match["p"], match["d"]) == (str(organization), str(plant), str(document))
    assert match["ext"] == content_type.extension
    segments = key.split("/")
    # Ningún segmento de otra clase de objeto: ni clips (zone/…/node/) ni cierres (closure/).
    assert not {"node", "closure", "zone"} & set(segments)
    assert len(key) <= 512


@pytest.mark.parametrize("prefix", ["node/", "closure/", "zone/", "documents", "/documents/", ""])
def test_a_reserved_or_malformed_prefix_is_refused(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefijo"):
        DocumentSettings(prefix=prefix)


@pytest.mark.parametrize("maximum", [0, DOCUMENTS_MAX_BYTES + 1, True])
def test_a_configured_maximum_outside_the_schema_is_refused(maximum: object) -> None:
    with pytest.raises(ValueError, match="tamaño"):
        DocumentSettings(max_bytes=maximum)  # type: ignore[arg-type]


def test_runtime_config_reads_the_document_variables() -> None:
    base = {
        "VIGIA_ENVIRONMENT": "test",
        "AWS_REGION": "us-east-1",
        "VIGIA_DB_APP_SECRET": "vigia/test/db/app",
        "VIGIA_SIGNING_SECRET_PREFIX": "vigia/test/signing/",
        "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
    }
    defaults = RuntimeConfig.from_environ(base)
    assert (defaults.documents_prefix, defaults.documents_max_bytes) == ("documents/", 20_971_520)
    read = RuntimeConfig.from_environ(
        {**base, "VIGIA_DOCUMENTS_PREFIX": "documents/", "VIGIA_DOCUMENTS_MAX_BYTES": "1024"}
    )
    assert read.documents_max_bytes == 1024
    for name, value in (
        ("VIGIA_DOCUMENTS_PREFIX", "node/"),
        ("VIGIA_DOCUMENTS_PREFIX", "otros/"),
        ("VIGIA_DOCUMENTS_MAX_BYTES", "0"),
        ("VIGIA_DOCUMENTS_MAX_BYTES", "20971521"),
    ):
        with pytest.raises(RuntimeConfigInvalid, match=name):
            RuntimeConfig.from_environ({**base, name: value})


# --- Estado derivado ----------------------------------------------------------------------------


def test_expired_is_derived_only_without_an_uploaded_object() -> None:
    grant = _issue()
    just_before = grant.expires_at - timedelta(microseconds=1)
    assert grant.effective_status(just_before, uploaded=False) is UploadGrantStatus.ISSUED
    assert grant.effective_status(grant.expires_at, uploaded=False) is UploadGrantStatus.EXPIRED
    # Subido mientras la URL valía: sigue usable al registrar después del vencimiento.
    assert grant.effective_status(grant.expires_at, uploaded=True) is UploadGrantStatus.ISSUED
    used = DocumentUploadGrant(**{**_fields(grant), "status": UploadGrantStatus.USED})
    assert used.effective_status(grant.expires_at, uploaded=False) is UploadGrantStatus.USED


def _fields(grant: DocumentUploadGrant) -> dict[str, Any]:
    return {name: getattr(grant, name) for name in grant.__slots__}


# --- Metadatos del objeto -----------------------------------------------------------------------


def test_the_object_matches_only_with_size_type_and_checksum_all_equal() -> None:
    grant = _issue()
    same = {"size_bytes": len(BODY), "content_type": "application/pdf", "sha256_hex": SHA}
    assert grant.object_matches(**same)
    # Cada campo por separado: con un almacén real, otro tamaño cambia también la suma, así que
    # solo un doble prueba la comparación del tamaño por sí sola.
    for name, value in (
        ("size_bytes", len(BODY) + 1),
        ("size_bytes", len(BODY) - 1),
        ("content_type", "image/png"),
        ("content_type", "application/pdf; charset=binary"),
        ("content_type", None),
        ("sha256_hex", "0" * 64),
        ("sha256_hex", SHA.upper()),
        ("sha256_hex", None),
    ):
        assert not grant.object_matches(**{**same, name: value}), (name, value)


# --- document_ref -------------------------------------------------------------------------------


def test_a_ref_round_trips_and_matches_only_its_own_grant() -> None:
    grant = _issue()
    parsed = DocumentRef.parse(grant.ref.to_json())
    assert parsed == grant.ref and grant.matches(parsed)
    for name, value in (
        ("sha256", "0" * 64),
        ("size_bytes", len(BODY) + 1),
        ("content_type", "image/png"),
        ("storage_key", grant.storage_key.replace(".pdf", ".png")),
        ("document_id", str(uuid.UUID(int=DOCUMENT.int + 1))),
    ):
        other = DocumentRef.parse({**grant.ref.to_json(), name: value})
        assert not grant.matches(other), name


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"document_id": str(DOCUMENT).upper()},
        {"document_id": "{" + str(DOCUMENT) + "}"},
        {"document_id": DOCUMENT.hex},
        {"document_id": 7},
        {"size_bytes": True},
        {"size_bytes": float(len(BODY))},
        {"size_bytes": 0},
        {"content_type": "image/gif"},
        {"sha256": SHA.upper()},
        {"storage_key": ""},
        {"storage_key": "x" * 513},
    ],
    ids=lambda change: next(iter(change)) + "=" + repr(next(iter(change.values())))[:12],
)
def test_malformed_refs_are_invalid(change: dict[str, Any]) -> None:
    with pytest.raises(DocumentRequestInvalid):
        DocumentRef.parse({**_issue().ref.to_json(), **change})


@pytest.mark.parametrize("missing", ["document_id", "storage_key", "sha256"])
def test_a_ref_without_a_field_is_invalid(missing: str) -> None:
    ref = _issue().ref.to_json()
    del ref[missing]
    with pytest.raises(DocumentRequestInvalid):
        DocumentRef.parse(ref)


def test_at_most_ten_refs_is_the_design_limit() -> None:
    assert MAX_DOCUMENT_REFS == 10


# --- Almacén (dobles) ---------------------------------------------------------------------------


def _head(grant: DocumentUploadGrant, **changes: Any) -> ObjectHead:
    values: dict[str, Any] = {
        "key": grant.storage_key,
        "size_bytes": grant.size_bytes,
        "checksum_sha256": sha256_hex_to_b64(grant.sha256),
        "checksum_type": ChecksumType.FULL_OBJECT,
        "content_type": grant.content_type.value,
        "metadata": {},
        "version_id": "v1",
    }
    values.update(changes)
    return ObjectHead(**values)


@dataclass
class CountingStorage:
    """``head_object`` y ``presign_put``; cuenta toda llamada, también las prohibidas."""

    objects: dict[str, ObjectHead] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    hang_head: bool = False
    hang_presign: bool = False
    head_error: BaseException | None = None
    presign_error: BaseException | None = None
    in_flight: int = 0
    peak: int = 0
    expected_parallel: int = 0
    _all_in: asyncio.Event | None = None

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def __getattr__(self, name: str) -> Any:
        # get_object, put_object, presign_get…: cualquier otra operación queda contada y falla.
        if name.startswith("_"):
            raise AttributeError(name)

        async def forbidden(*_: Any, **__: Any) -> Any:
            self._count(name)
            raise AssertionError(f"operación prohibida: {name}")

        return forbidden

    async def head_object(self, key: str) -> ObjectHead | None:
        self._count("head_object")
        if self.head_error is not None:
            raise self.head_error
        if self.hang_head:
            await asyncio.Event().wait()
        if self.expected_parallel:
            if self._all_in is None:
                self._all_in = asyncio.Event()
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            if self.in_flight == self.expected_parallel:
                self._all_in.set()
            # Solo avanza cuando todas las consultas están en curso a la vez: en serie, nunca.
            await self._all_in.wait()
            self.in_flight -= 1
        return self.objects.get(key)

    async def presign_put(
        self,
        key: str,
        content_type: str,
        checksum_sha256: str,
        required_headers: Mapping[str, str],
        ttl: timedelta = timedelta(minutes=15),
    ) -> PresignedRequest:
        self._count("presign_put")
        if self.presign_error is not None:
            raise self.presign_error
        if self.hang_presign:
            await asyncio.Event().wait()
        assert dict(required_headers) == {}
        assert ttl == DOCUMENT_GRANT_TTL
        return PresignedRequest(
            "PUT",
            f"https://almacen.vigia.test/{key}?X-Amz-Expires=900",
            {
                CONTENT_TYPE_HEADER: content_type,
                CHECKSUM_HEADER: sha256_hex_to_b64(checksum_sha256),
            },
            T0 + ttl,
        )


def test_the_timeouts_are_those_of_nfr_gob_43() -> None:
    assert (PRESIGN_TIMEOUT_SECONDS, HEAD_TIMEOUT_SECONDS) == (5.0, 10.0)


def test_prepare_upload_checks_the_fresh_key_then_signs_type_and_checksum() -> None:
    storage = CountingStorage()
    grant = _issue()
    upload = asyncio.run(DocumentObjectStore(storage).prepare_upload(grant))
    assert upload.method == "PUT"
    assert dict(upload.headers) == {
        "content-type": "application/pdf",
        "x-amz-checksum-sha256": sha256_hex_to_b64(SHA),
    }
    assert storage.calls == {"head_object": 1, "presign_put": 1}


def test_prepare_upload_never_signs_over_an_existing_object() -> None:
    grant = _issue()
    storage = CountingStorage(objects={grant.storage_key: _head(grant)})
    with pytest.raises(DocumentKeyTaken):
        asyncio.run(DocumentObjectStore(storage).prepare_upload(grant))
    assert "presign_put" not in storage.calls


@pytest.mark.parametrize(
    "storage",
    [
        CountingStorage(hang_head=True),
        CountingStorage(hang_presign=True),
        CountingStorage(head_error=StorageUnavailable("head_object")),
        CountingStorage(presign_error=botocore_exceptions.NoCredentialsError()),
        CountingStorage(
            presign_error=botocore_exceptions.EndpointConnectionError(endpoint_url="x")
        ),
    ],
    ids=["consulta_colgada", "firma_colgada", "consulta_caida", "sin_credenciales", "sin_red"],
)
def test_a_down_or_slow_store_is_storage_unavailable_with_retry(storage: CountingStorage) -> None:
    # Dobles que nunca responden: el tope es el sujeto de la prueba y siempre vence (1 s).
    store = DocumentObjectStore(storage, presign_timeout_seconds=1.0, head_timeout_seconds=1.0)
    with pytest.raises(StorageUnavailable) as raised:
        asyncio.run(store.prepare_upload(_issue()))
    assert raised.value.code == "storage_unavailable"
    assert raised.value.retry_after_seconds == RETRY_AFTER_SECONDS


def test_other_errors_are_not_swallowed() -> None:
    storage = CountingStorage(presign_error=ValueError("clave no válida"))
    with pytest.raises(ValueError, match="clave"):
        asyncio.run(DocumentObjectStore(storage).prepare_upload(_issue()))


def test_heads_run_in_parallel_and_never_download() -> None:
    grants = [_issue(document_id=uuid.UUID(int=DOCUMENT.int + n)) for n in range(MAX_DOCUMENT_REFS)]
    storage = CountingStorage(
        objects={grant.storage_key: _head(grant) for grant in grants},
        expected_parallel=len(grants),
    )
    heads = asyncio.run(DocumentObjectStore(storage).heads([g.storage_key for g in grants]))
    assert storage.peak == len(grants)
    assert set(heads) == {grant.storage_key for grant in grants}
    assert storage.calls == {"head_object": len(grants)}


def test_a_hung_head_among_many_is_storage_unavailable() -> None:
    grant = _issue()
    storage = CountingStorage(hang_head=True)
    store = DocumentObjectStore(storage, head_timeout_seconds=1.0)
    with pytest.raises(StorageUnavailable):
        asyncio.run(store.heads([grant.storage_key, grant.storage_key + "x"]))
