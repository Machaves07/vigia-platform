"""Guardas de la foto del hallazgo sin base (TASK-589; A-128 §6; NFR-GOB-25).

- **Minimización** (metapropiedad sobre el esquema publicado, como PR-LAZ-01): ``FindingPhotoOut``
  tiene exactamente los campos de la tarea y ninguno es una URL, una suma, un tamaño en bytes o la
  imagen, por nombre (palabra a palabra) ni por formato; el registro del detalle y el hito de la
  ficha lo citan (``m_suma_en_el_detalle``). La sentencia de los metadatos nunca selecciona
  ``image``, ``sha256`` ni ``size_bytes``.
- ``FindingPhotoReader.image`` con dobles:
  - sin ``evidence.read`` en la zona (``findings.read`` sí): ``not_found``, ``denied`` con la zona y
    la foto **nunca** se lee (``m_sin_evidence_read``);
  - hallazgo invisible: ``denied`` sin zona; compuerta ilegible: ``error`` y 503;
  - la entrada falla dentro de la transacción: la lectura falla y no devuelve ningún byte
    (``m_bytes_sin_auditoria``);
  - bytes que no tienen su suma: ``error`` y ``FindingPhotoIntegrityFailed``.
- ``photo_key``: el ``record_id`` del nodo es el ``finding_id`` o el ``detection_id`` del
  documento, con su episodio y su tramo; sin ellos, ninguna clave.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Final

import pytest

from tests.factories import make_context
from vigia_platform.fleet.adapters.postgres import episode_photo_queries
from vigia_platform.fleet.adapters.postgres.episode_photo_queries import (
    FindingPhoto,
    FindingPhotoKey,
    StoredImage,
)
from vigia_platform.identity.authz.authorize import Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditOutcome
from vigia_platform.loop.application.detail import FindingGateUnavailable, photo_key
from vigia_platform.loop.application.finding_photo import (
    FindingPhotoIntegrityFailed,
    FindingPhotoReader,
)
from vigia_platform.shared.api.app import build_openapi_app
from vigia_platform.shared.context import ScopeContext

DATA: Final = b"\xff\xd8jpeg sintetico del hallazgo\xff\xd9"
ORGANIZATION: Final = uuid.UUID(int=10)
PLANT: Final = uuid.UUID(int=11)
ZONE: Final = uuid.UUID(int=12)
FINDING: Final = uuid.UUID(int=13)
PHOTO: Final = uuid.UUID(int=14)
RECORD: Final = uuid.UUID(int=15)

PHOTO_FIELDS: Final = frozenset(
    {
        "photo_id",
        "photo_index",
        "photo_role",
        "classified_as",
        "camera_id",
        "frame_at",
        "family",
        "standard",
        "max_confidence",
        "started_at",
        "ended_at",
        "episode_duration_ms",
        "received_at",
        "expires_at",
        "has_image",
        "image_absent_reason",
        "width",
        "height",
    }
)
"""Los campos de ``FindingPhotoOut`` que fija la tarea, exactamente."""
FORBIDDEN_WORDS: Final = frozenset(
    {"url", "uri", "href", "link", "sha256", "sha", "hash", "digest", "size", "bytes", "base64"}
)
"""Palabras (separadas por ``_``) que delatan una URL, una suma, un tamaño o la imagen."""
FORBIDDEN_FORMATS: Final = frozenset({"uri", "url", "binary", "byte", "base64"})


# --- minimización --------------------------------------------------------------------------------


def _schemas() -> Mapping[str, Any]:
    document = build_openapi_app().openapi()
    schemas: Mapping[str, Any] = document["components"]["schemas"]
    return schemas


def _words(name: str) -> set[str]:
    return set(name.lower().split("_"))


def test_the_photo_schema_has_exactly_the_task_fields_and_none_is_a_url_sum_size_or_image() -> None:
    schemas = _schemas()
    photo = schemas["FindingPhotoOut"]
    properties: Mapping[str, Any] = photo["properties"]
    assert set(properties) == PHOTO_FIELDS
    assert photo.get("additionalProperties") is False
    for name, definition in properties.items():
        assert not _words(name) & FORBIDDEN_WORDS, name
        assert name != "image" and name != "data", name
        options = definition.get("anyOf", [definition])
        for option in options:
            assert option.get("format") not in FORBIDDEN_FORMATS, name
            assert "contentEncoding" not in option and "contentMediaType" not in option, name
    standard = schemas["FindingPhotoStandardOut"]["properties"]
    assert set(standard) == {"standard_id", "version"}


def test_the_record_and_the_milestone_cite_the_photo_schema() -> None:
    schemas = _schemas()
    reference = "#/components/schemas/FindingPhotoOut"
    for owner in ("RecordOut", "TimelineMilestoneOut"):
        photos = schemas[owner]["properties"]["photos"]
        assert photos["items"] == {"$ref": reference}, owner
        assert "photos" in schemas[owner]["required"], owner


def _selected(statement: Any) -> list[str]:
    sql = str(statement.text)
    columns = sql[sql.index("SELECT ") + len("SELECT ") : sql.index(" FROM ")]
    return [column.strip() for column in columns.split(",")]


def test_the_metadata_statement_never_selects_the_image_its_sum_or_its_size() -> None:
    selected = _selected(episode_photo_queries._RECORD_PHOTOS)
    names = {re.split(r"\s+AS\s+|\s+IS\s+", c)[-1].removeprefix("p.") for c in selected}
    assert not {"image", "sha256", "size_bytes"} & names
    assert "p.image IS NOT NULL AS has_image" in selected
    reads_image = [c for c in selected if c.split()[0] == "p.image"]
    assert reads_image == ["p.image IS NOT NULL AS has_image"]


# --- la clave del tramo ---------------------------------------------------------------------------


def test_the_photo_key_is_the_node_record_with_its_episode_and_segment() -> None:
    episode = uuid.UUID(int=20)
    segment = {"episode_id": str(episode), "segment_index": 2}
    assert photo_key({"finding_id": str(RECORD), "episode": segment}) == FindingPhotoKey(
        RECORD, episode, 2
    )
    assert photo_key({"detection_id": str(RECORD), "episode": segment}) == FindingPhotoKey(
        RECORD, episode, 2
    )
    assert photo_key({"finding_id": str(RECORD)}) is None
    assert photo_key({"episode": segment}) is None
    assert (
        photo_key({"finding_id": str(RECORD), "episode": {**segment, "segment_index": "2"}}) is None
    )
    assert photo_key({"finding_id": "no", "episode": segment}) is None
    assert photo_key(None) is None


# --- el servicio con dobles ----------------------------------------------------------------------


class _Subjects:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def visible_subject(
        self, context: ScopeContext, finding_state_id: uuid.UUID
    ) -> tuple[ScopeContext, Mapping[str, Any]]:
        if self.error is not None:
            raise self.error
        return context, {
            "organization_id": ORGANIZATION,
            "plant_id": PLANT,
            "zone_id": ZONE,
            "subject_kind": "finding",
        }

    async def segment_record_ids(
        self, transaction: Any, subject: Mapping[str, Any]
    ) -> tuple[uuid.UUID, ...]:
        return (RECORD,)


class _Authorizer:
    """Concede todo menos las claves de ``denied``."""

    def __init__(self, denied: frozenset[str] = frozenset()) -> None:
        self.denied = denied
        self.asked: list[tuple[str, Resource]] = []

    async def authorize(
        self, context: ScopeContext, key: PermissionKey | str, resource: Resource
    ) -> ScopeContext:
        name = key.value if isinstance(key, PermissionKey) else key
        self.asked.append((name, resource))
        if name in self.denied:
            raise ResourceNotFound()
        return context


class _AuditDown(RuntimeError):
    """La entrada de auditoría no se pudo escribir."""


class _Audit:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.entries: list[dict[str, Any]] = []

    async def append(self, context: ScopeContext, operation: AuditOperation, **values: Any) -> Any:
        if self.fail and values.get("transaction") is not None:
            raise _AuditDown()
        self.entries.append({"operation": operation, **values})
        return None


class _Database:
    def __init__(self) -> None:
        self.committed = 0
        self.rolled_back = 0

    @contextlib.asynccontextmanager
    async def transaction(self, context: ScopeContext) -> AsyncIterator[Any]:
        try:
            yield object()
        except BaseException:
            self.rolled_back += 1
            raise
        self.committed += 1


class _Photos:
    def __init__(self, stored: StoredImage | None) -> None:
        self.stored = stored
        self.reads: list[uuid.UUID] = []

    async def record_photos(
        self, *args: Any, **kwargs: Any
    ) -> Mapping[FindingPhotoKey, tuple[FindingPhoto, ...]]:
        raise AssertionError("la imagen no lee metadatos")

    async def finding_image(
        self,
        transaction: Any,
        photo_id: uuid.UUID,
        *,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        record_ids: Sequence[uuid.UUID],
    ) -> StoredImage | None:
        assert (plant_id, zone_id, tuple(record_ids)) == (PLANT, ZONE, (RECORD,))
        self.reads.append(photo_id)
        return self.stored


def _stored(data: bytes = DATA, digest: str | None = None) -> StoredImage:
    return StoredImage(
        plant_id=PLANT,
        zone_id=ZONE,
        size_bytes=len(data),
        sha256=digest if digest is not None else hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _reader(
    *,
    subjects: _Subjects | None = None,
    authorizer: _Authorizer | None = None,
    audit: _Audit | None = None,
    photos: _Photos | None = None,
    database: _Database | None = None,
) -> tuple[FindingPhotoReader, _Audit, _Photos, _Database]:
    audit = audit if audit is not None else _Audit()
    photos = photos if photos is not None else _Photos(_stored())
    database = database if database is not None else _Database()
    reader = FindingPhotoReader(
        database=database,  # type: ignore[arg-type]
        subjects=subjects if subjects is not None else _Subjects(),  # type: ignore[arg-type]
        authorizer=authorizer if authorizer is not None else _Authorizer(),
        audit=audit,  # type: ignore[arg-type]
        photos=photos,
    )
    return reader, audit, photos, database


def _outcomes(audit: _Audit) -> list[tuple[AuditOutcome, uuid.UUID | None]]:
    assert all(e["operation"] is AuditOperation.FINDING_PHOTO_READ for e in audit.entries)
    return [(e["outcome"], e["zone_id"]) for e in audit.entries]


@pytest.mark.asyncio
async def test_with_evidence_read_the_exact_bytes_come_out_after_a_committed_success() -> None:
    authorizer = _Authorizer()
    reader, audit, _photos, database = _reader(authorizer=authorizer)
    served = await reader.image(make_context(), FINDING, PHOTO)
    assert served.data == DATA
    assert _outcomes(audit) == [(AuditOutcome.SUCCESS, ZONE)]
    assert audit.entries[0]["transaction"] is not None  # en la transacción de la lectura
    assert database.committed == 1
    ((key, resource),) = authorizer.asked
    assert key == PermissionKey.EVIDENCE_READ.value
    assert (resource.zone_id, resource.plant_id, resource.id) == (ZONE, PLANT, PHOTO)
    assert repr(DATA) not in repr(served)


@pytest.mark.asyncio
async def test_without_evidence_read_nothing_is_read_and_the_request_is_denied() -> None:
    """``m_sin_evidence_read``: con solo ``findings.read`` (el del detalle), la imagen no sale."""
    authorizer = _Authorizer(denied=frozenset({PermissionKey.EVIDENCE_READ.value}))
    reader, audit, photos, database = _reader(authorizer=authorizer)
    with pytest.raises(ResourceNotFound):
        await reader.image(make_context(), FINDING, PHOTO)
    assert photos.reads == []
    assert database.committed == database.rolled_back == 0
    assert _outcomes(audit) == [(AuditOutcome.DENIED, ZONE)]
    assert audit.entries[0].get("transaction") is None


@pytest.mark.asyncio
async def test_an_invisible_finding_is_denied_without_zone_and_never_read() -> None:
    reader, audit, photos, _ = _reader(subjects=_Subjects(error=ResourceNotFound()))
    with pytest.raises(ResourceNotFound):
        await reader.image(make_context(), FINDING, PHOTO)
    assert photos.reads == []
    assert _outcomes(audit) == [(AuditOutcome.DENIED, None)]


@pytest.mark.asyncio
async def test_an_unreadable_gate_is_an_error_and_never_read() -> None:
    reader, audit, photos, _ = _reader(subjects=_Subjects(error=FindingGateUnavailable()))
    with pytest.raises(FindingGateUnavailable):
        await reader.image(make_context(), FINDING, PHOTO)
    assert photos.reads == []
    assert _outcomes(audit) == [(AuditOutcome.ERROR, None)]


@pytest.mark.asyncio
async def test_when_the_audit_fails_no_byte_comes_out() -> None:
    """``m_bytes_sin_auditoria``: la entrada va en la transacción de la lectura y antes de servir;
    si no se escribe, la lectura falla (y se revierte) sin devolver los bytes."""
    reader, _audit, photos, database = _reader(audit=_Audit(fail=True))
    with pytest.raises(_AuditDown):
        await reader.image(make_context(), FINDING, PHOTO)
    assert photos.reads == [PHOTO]
    assert (database.committed, database.rolled_back) == (0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        _stored(digest=hashlib.sha256(DATA + b"x").hexdigest()),
        StoredImage(PLANT, ZONE, len(DATA) + 1, hashlib.sha256(DATA).hexdigest(), DATA),
    ],
    ids=["otra-suma", "otro-tamano"],
)
async def test_bytes_without_their_sum_are_an_error_and_never_served(stored: StoredImage) -> None:
    reader, audit, _, _ = _reader(photos=_Photos(stored))
    with pytest.raises(FindingPhotoIntegrityFailed):
        await reader.image(make_context(), FINDING, PHOTO)
    assert _outcomes(audit) == [(AuditOutcome.ERROR, ZONE)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [None, StoredImage(PLANT, ZONE, None, None, None)],
    ids=["no-es-de-sus-registros", "sin-imagen"],
)
async def test_a_photo_that_is_not_there_is_not_found_and_denied(
    stored: StoredImage | None,
) -> None:
    reader, audit, _, _ = _reader(photos=_Photos(stored))
    with pytest.raises(ResourceNotFound):
        await reader.image(make_context(), FINDING, PHOTO)
    assert _outcomes(audit) == [(AuditOutcome.DENIED, ZONE)]


@pytest.mark.asyncio
async def test_identifiers_that_are_not_uuids_are_not_found_before_anything() -> None:
    reader, audit, photos, _ = _reader(subjects=_Subjects(error=AssertionError("tocado")))
    with pytest.raises(ResourceNotFound):
        await reader.image(make_context(), "hallazgo", PHOTO)  # type: ignore[arg-type]
    with pytest.raises(ResourceNotFound):
        await reader.image(make_context(), FINDING, "foto")  # type: ignore[arg-type]
    assert audit.entries == [] and photos.reads == []


def _predicates(statement: Any) -> set[str]:
    """Los predicados de primer nivel del ``WHERE`` principal (separados por `` AND ``)."""
    sql = str(statement.text)
    where = sql[sql.index(" WHERE ") + len(" WHERE ") :]
    where = where.split(" ORDER BY ")[0].split(" AND EXISTS ")[0]
    return {predicate.strip() for predicate in where.split(" AND ")}


@pytest.mark.parametrize("name", ["_RECORD_PHOTOS", "_FINDING_IMAGE"])
def test_both_statements_only_read_productive_photos_of_the_finding_place(name: str) -> None:
    """``m_incluye_comisionamiento``: el ``CHECK episode_photo_record_by_mode`` ya impide que una
    foto ``commissioning`` lleve ``record_id`` (la base no deja sembrar el caso), así que el filtro
    explícito del modo es la defensa en profundidad que esta prueba sostiene, con la planta y la
    zona del hallazgo."""
    predicates = _predicates(getattr(episode_photo_queries, name))
    assert {
        "p.organization_id = :organization_id",
        "p.plant_id = :plant_id",
        "p.zone_mode = \x27productive\x27",
    } <= predicates
    assert "p.zone_id = :zone_id" in predicates
