"""Foto del episodio de la sombra: dominio y orden de BR-GOB-84 (TASK-572; A-112).

- **Imagen** (paso 6): cada defecto da su código y su ``field``, los mismos que la plataforma
  simulada del contrato v1.5.0 (precisiones del 12-11 de A-112): ``image.size_bytes``,
  ``image.sha256``, ``image.data_base64``, ``image.width`` e ``image.height``. Un ``APP1`` (EXIF),
  un ``APP0`` ``JFXX`` o bytes tras ``EOI`` son ``clip_not_anonymized``; sin la marca, también; el
  ``APP0`` ``JFIF`` de 16 bytes se admite. Solo JPEG sintéticos.
- **Catálogo** (paso 7): el estándar vigente con su versión y la cámara declarada.
- **Vencimiento**: ``expires_at`` exactamente ``received_at`` más los días; con 0 o con 91 ni el
  servicio ni la configuración del proceso arrancan.
- **Señal ``no_data``** (seguimiento de VIG-449): la ingesta real rechaza una lectura válida de una
  señal declarada sin dato, como la plataforma simulada.
- **Orden** (propiedad sobre combinaciones generadas): con el servicio real sobre una base en
  memoria, un registro que rompe varias reglas de los pasos 4 a 8 responde siempre el código del
  paso de menor índice; con la misma foto ya aceptada, ``accepted_duplicate`` con el recibo
  original aunque después hayan cambiado el catálogo o la compuerta.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.models import api
from vigia_contracts.models.enumerations import RejectionCode

from tests.episode_photo_support import JFIF16, JFXX, image, jpeg, submission
from vigia_platform.fleet.adapters.postgres.episode_photo_store import StoredPhoto
from vigia_platform.fleet.application.episode_photos import (
    EpisodePhotoDependencies,
    EpisodePhotoService,
)
from vigia_platform.fleet.application.ingest import IngestRejected
from vigia_platform.fleet.domain.clock_tolerance import Window
from vigia_platform.fleet.domain.episode_photo import (
    STEP_CODES,
    EpisodePhotoRow,
    EpisodePhotoStep,
    catalog_violation,
    check_retention_days,
    decode_image,
    expires_at,
    image_violation,
)
from vigia_platform.fleet.domain.ingest_order import (
    CatalogVersionView,
    GateSpan,
    IngestKind,
)
from vigia_platform.fleet.domain.ingest_order import catalog_violation as ingest_violation
from vigia_platform.identity.authz.context import NodeScope
from vigia_platform.node_api.limits import NODE_BUDGETS, RouteBudget
from vigia_platform.node_api.rejections import NodeRejection
from vigia_platform.node_api.routes.episode_photos import scope_before_schema
from vigia_platform.shared.api.declarations import NodeRoute
from vigia_platform.shared.clock import SimulatedClock
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.ratelimit import Budget
from vigia_platform.shared.runtime.config import RuntimeConfig, RuntimeConfigInvalid

UTC: Final = dt.UTC
NOW: Final = dt.datetime(2026, 11, 12, 15, 30, 0, 123_000, tzinfo=UTC)
ORGANIZATION: Final = uuid.UUID("0190a8a0-0000-7000-8000-000000000001")
PLANT: Final = uuid.UUID("0190a8a0-0000-7000-8000-000000000002")
ZONE: Final = uuid.UUID("0190a8a0-0000-7000-8000-000000000003")
NODE: Final = uuid.UUID("0190a8a0-0000-7000-8000-000000000004")
STANDARD: Final = uuid.UUID("0190a8a0-0000-7000-8000-000000000005")
CAMERA: Final = uuid.UUID("0190a8a0-0000-7000-8000-000000000006")
SIGNAL: Final = uuid.UUID("0190a8a0-0000-7000-8000-000000000007")
ENVIRONMENT: Final = {
    "VIGIA_ENVIRONMENT": "test",
    "AWS_REGION": "eu-west-1",
    "VIGIA_DB_APP_SECRET": "vigia/test/db/app",
    "VIGIA_SIGNING_SECRET_PREFIX": "vigia/test/signing/",
    "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
}


def _view(
    *,
    standards: frozenset[tuple[uuid.UUID, int]] = frozenset({(STANDARD, 1)}),
    cameras: frozenset[uuid.UUID] = frozenset({CAMERA}),
    since: dt.datetime = NOW - dt.timedelta(days=400),
    until: dt.datetime | None = None,
    no_data: frozenset[uuid.UUID] = frozenset(),
) -> CatalogVersionView:
    return CatalogVersionView(
        1, since, until, standards, {SIGNAL: "energy"}, 0.4, 0.7, cameras, no_data
    )


def _document(**changes: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "organization_id": ORGANIZATION,
        "plant_id": PLANT,
        "zone_id": ZONE,
        "node_id": NODE,
        "standard_id": STANDARD,
        "camera_id": CAMERA,
        "started": NOW - dt.timedelta(minutes=10),
    }
    arguments.update(changes)
    return submission(**arguments)


# --- Paso 6: la imagen ---------------------------------------------------------------------------


def _verdict(data: bytes, **declared: Any) -> tuple[RejectionCode, str] | None:
    declared_image = image(data, **declared)
    found = image_violation(declared_image, decode_image(declared_image))
    return None if found is None else (found.code, found.field)


def test_a_marked_jpeg_with_its_declared_facts_passes() -> None:
    assert _verdict(jpeg()) is None
    # El único APP0 admitido: el JFIF de 16 bytes sin miniatura que escribe OpenCV.
    assert _verdict(jpeg(app0=JFIF16)) is None


@pytest.mark.parametrize(
    ("data", "declared", "expected"),
    [
        (jpeg(marked=False), {}, (RejectionCode.CLIP_NOT_ANONYMIZED, "image.data_base64")),
        (jpeg(exif=True), {}, (RejectionCode.CLIP_NOT_ANONYMIZED, "image.data_base64")),
        (
            jpeg(trailing=b"\xff\xd8cola"),
            {},
            (RejectionCode.CLIP_NOT_ANONYMIZED, "image.data_base64"),
        ),
        (jpeg(app0=JFXX), {}, (RejectionCode.CLIP_NOT_ANONYMIZED, "image.data_base64")),
        (jpeg(app0=JFIF16 + JFIF16), {}, (RejectionCode.CLIP_NOT_ANONYMIZED, "image.data_base64")),
        (
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
            {},
            (RejectionCode.CLIP_HASH_MISMATCH, "image.data_base64"),
        ),
        (jpeg(), {"size_bytes": 7}, (RejectionCode.CLIP_HASH_MISMATCH, "image.size_bytes")),
        (jpeg(), {"sha256": "ab" * 32}, (RejectionCode.CLIP_HASH_MISMATCH, "image.sha256")),
        (jpeg(width=321), {}, (RejectionCode.CLIP_HASH_MISMATCH, "image.width")),
        (jpeg(height=241), {}, (RejectionCode.CLIP_HASH_MISMATCH, "image.height")),
    ],
    ids=[
        "sin-marca",
        "app1-exif",
        "cola-tras-eoi",
        "app0-jfxx",
        "dos-app0",
        "no-es-jpeg",
        "tamano",
        "suma",
        "ancho",
        "alto",
    ],
)
def test_each_image_defect_has_its_code_and_field(
    data: bytes, declared: dict[str, Any], expected: tuple[RejectionCode, str]
) -> None:
    assert _verdict(data, **declared) == expected


def test_the_image_checks_go_in_the_order_of_the_contract() -> None:
    # Tamaño antes que la suma; la suma antes que la forma; la forma antes que las dimensiones; y
    # las dimensiones antes que la marca (ingest.yaml, paso 6).
    bad = jpeg(width=321, marked=False, exif=True)
    assert _verdict(bad, size_bytes=1, sha256="cd" * 32) == (
        RejectionCode.CLIP_HASH_MISMATCH,
        "image.size_bytes",
    )
    assert _verdict(bad, sha256="cd" * 32) == (RejectionCode.CLIP_HASH_MISMATCH, "image.sha256")
    assert _verdict(bad) == (RejectionCode.CLIP_NOT_ANONYMIZED, "image.data_base64")
    assert _verdict(jpeg(width=321, marked=False)) == (
        RejectionCode.CLIP_HASH_MISMATCH,
        "image.width",
    )


def test_invalid_base64_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        decode_image({"data_base64": "no*es*base64"})


# --- Paso 7: catálogo y cámara -------------------------------------------------------------------


def test_the_standard_and_the_camera_must_be_in_the_catalog_in_force() -> None:
    window = Window(NOW - dt.timedelta(minutes=11), NOW - dt.timedelta(minutes=9))
    document = _document()
    assert catalog_violation([_view()], document, window) is None
    assert catalog_violation([_view(standards=frozenset({(STANDARD, 2)}))], document, window) == (
        "standard"
    )
    assert catalog_violation([_view(cameras=frozenset())], document, window) == "camera_id"
    expired = _view(until=NOW - dt.timedelta(days=1))
    assert catalog_violation([expired], document, window) == "standard"
    # Sin cámara (foto sin imagen y sin cuadro), solo cuenta el estándar.
    absent = _document(absent_reason="pending_cap")
    assert catalog_violation([_view(cameras=frozenset())], absent, window) is None


def test_a_valid_reading_of_a_no_data_signal_is_rejected_by_the_real_ingest() -> None:
    # Seguimiento de VIG-449: como la plataforma simulada desde v1.2.0 (A-77).
    window = Window(NOW - dt.timedelta(minutes=11), NOW - dt.timedelta(minutes=9))
    view = _view(no_data=frozenset({SIGNAL}))
    reading = {"signal_id": str(SIGNAL), "role": "energy", "value": "asserted"}
    base = {
        "standard": {"standard_id": str(STANDARD), "version": 1},
        "review_threshold": 0.4,
        "publication_threshold": 0.7,
    }
    valid = {**base, "signals": [{**reading, "valid": True}]}
    invalid = {**base, "signals": [{**reading, "valid": False}]}
    kind = IngestKind.DETECTION_FOR_REVIEW
    assert ingest_violation([view], valid, kind, window) == "signals[0].valid"
    assert ingest_violation([view], invalid, kind, window) is None
    assert ingest_violation([_view()], valid, kind, window) is None


def test_the_catalog_view_reads_cameras_and_no_data_signals() -> None:
    payload = {
        "standards": [{"standard_id": str(STANDARD), "version": 1}],
        "signals": [
            {"signal_id": str(SIGNAL), "role": "energy", "no_data": True},
            {"signal_id": str(CAMERA), "role": "guard"},
        ],
        "cameras": [{"camera_id": str(CAMERA)}],
        "thresholds": {"review": 0.4, "publication": 0.7},
    }
    view = CatalogVersionView.from_payload(1, NOW, None, payload)
    assert view.cameras == frozenset({CAMERA})
    assert view.no_data == frozenset({SIGNAL})


# --- Vencimiento ---------------------------------------------------------------------------------


def test_expires_at_is_exactly_received_at_plus_the_days() -> None:
    assert expires_at(NOW, 30) == NOW + dt.timedelta(days=30)
    assert expires_at(NOW, 1) == NOW + dt.timedelta(days=1)
    assert expires_at(NOW, 90) == NOW + dt.timedelta(days=90)


@pytest.mark.parametrize("days", [0, 91, -1, True, 30.0, "30"])
def test_retention_days_out_of_range_do_not_start(days: Any) -> None:
    with pytest.raises(ValueError):
        check_retention_days(days)
    if type(days) is int:
        with pytest.raises(ValueError):
            EpisodePhotoService(
                EpisodePhotoDependencies(
                    database=_Database(_Store()),
                    clock=SimulatedClock(NOW),
                    store=_Store(),
                    retention_days=days,
                )
            )


@pytest.mark.parametrize("value", ["0", "91", "treinta", "-1"])
def test_the_process_does_not_start_with_retention_outside_1_to_90(value: str) -> None:
    with pytest.raises(RuntimeConfigInvalid) as raised:
        RuntimeConfig.from_environ({**ENVIRONMENT, "VIGIA_EPISODE_PHOTO_RETENTION_DAYS": value})
    assert raised.value.variable == "VIGIA_EPISODE_PHOTO_RETENTION_DAYS"


def test_the_process_reads_the_retention_or_takes_thirty_days() -> None:
    assert RuntimeConfig.from_environ(ENVIRONMENT).episode_photo_retention_days == 30
    for value in (1, 90):
        config = RuntimeConfig.from_environ(
            {**ENVIRONMENT, "VIGIA_EPISODE_PHOTO_RETENTION_DAYS": str(value)}
        )
        assert config.episode_photo_retention_days == value


# --- El orden, con el servicio real sobre una base en memoria -------------------------------------


@dataclass
class _Store:
    """``fleet.episode_photo`` en memoria con la semántica del ``INSERT … ON CONFLICT``."""

    rows: dict[tuple[uuid.UUID, uuid.UUID], EpisodePhotoRow] = field(default_factory=dict)
    pending: list[EpisodePhotoRow] = field(default_factory=list)
    versions: list[CatalogVersionView] = field(default_factory=lambda: [_view()])
    spans: list[GateSpan] = field(
        default_factory=lambda: [GateSpan(True, NOW - dt.timedelta(days=400), None)]
    )
    retention: int | None = None

    async def claim(self, transaction: object, row: EpisodePhotoRow) -> bool:
        key = (row.organization_id, row.photo_id)
        if key in self.rows or any((p.organization_id, p.photo_id) == key for p in self.pending):
            return False
        self.pending.append(row)
        return True

    async def stored(self, transaction: object, photo_id: uuid.UUID) -> StoredPhoto | None:
        row = self.rows.get((ORGANIZATION, photo_id))
        if row is None:
            return None
        return StoredPhoto(row.canonical_sha256, row.platform_record_id, row.received_at)

    async def retention_days(self, transaction: object, node_id: uuid.UUID) -> int | None:
        return self.retention

    async def catalog_versions(
        self, transaction: object, zone_id: uuid.UUID, window: Window
    ) -> Sequence[CatalogVersionView]:
        return self.versions

    async def mounting_spans(
        self, transaction: object, zone_id: uuid.UUID, window: Window
    ) -> Sequence[GateSpan]:
        return self.spans


class _Database:
    """Una transacción que confirma lo pendiente al salir bien y lo descarta si falla."""

    def __init__(self, store: _Store) -> None:
        self.store = store

    @contextlib.asynccontextmanager
    async def transaction(self, context: object) -> AsyncIterator[object]:
        try:
            yield object()
        except BaseException:
            self.store.pending.clear()
            raise
        for row in self.store.pending:
            self.store.rows[(row.organization_id, row.photo_id)] = row
        self.store.pending.clear()

    async def read(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - no se usa
        raise AssertionError

    async def read_many(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - no se usa
        raise AssertionError


def _node() -> NodeScope:
    return NodeScope(
        context=object.__new__(ScopeContext),
        node_id=NODE,
        plant_id=PLANT,
        zone_ids=frozenset({ZONE}),
        certificate_serial="ab",
        credential_status="active",
    )


def _service(store: _Store) -> EpisodePhotoService:
    return EpisodePhotoService(
        EpisodePhotoDependencies(database=_Database(store), clock=SimulatedClock(NOW), store=store)
    )


def _accept(
    service: EpisodePhotoService,
    document: dict[str, Any],
    *,
    version: str | None = None,
    key: str | None = None,
) -> Any:
    model = api.parse_episode_photo_submission(json.dumps(document).encode())

    async def run() -> Any:
        return await service.accept(
            _node(),
            model,
            received_at=NOW,
            idempotency_key=key if key is not None else document["photo_id"],
            contract_version=version if version is not None else document["contract_version"],
        )

    return asyncio.run(run())


def _code(service: EpisodePhotoService, document: dict[str, Any], **options: Any) -> str:
    try:
        reply = _accept(service, document, **options)
    except IngestRejected as rejection:
        return rejection.code.value
    return "accepted_duplicate" if reply.duplicate else "accepted"


SCHEMA, IDEMPOTENCY, IMAGE, CATALOG, GATE = (
    EpisodePhotoStep.SCHEMA,
    EpisodePhotoStep.IDEMPOTENCY,
    EpisodePhotoStep.IMAGE,
    EpisodePhotoStep.CATALOG,
    EpisodePhotoStep.GATE,
)
SERVICE_STEPS: Final = (SCHEMA, IDEMPOTENCY, IMAGE, CATALOG, GATE)
IMAGE_DEFECTS: Final = {
    "sin-marca": lambda: image(jpeg(marked=False)),
    "exif": lambda: image(jpeg(exif=True)),
    "cola": lambda: image(jpeg(trailing=b"cola")),
    "suma": lambda: image(jpeg(), sha256="ef" * 32),
    "ancho": lambda: image(jpeg(width=400)),
}
CATALOG_DEFECTS: Final = ("estandar", "camara", "antiguo")


@st.composite
def violations(draw: st.DrawFn) -> tuple[frozenset[EpisodePhotoStep], str, str, str]:
    steps = draw(st.sets(st.sampled_from(SERVICE_STEPS), min_size=1).map(frozenset))
    schema = draw(st.sampled_from(["contract_version", "Idempotency-Key"]))
    defect = draw(st.sampled_from(sorted(IMAGE_DEFECTS)))
    catalog = draw(st.sampled_from(CATALOG_DEFECTS))
    return steps, schema, defect, catalog


@given(case=violations())
def test_several_broken_rules_answer_the_lowest_step(
    case: tuple[frozenset[EpisodePhotoStep], str, str, str],
) -> None:
    steps, schema, defect, catalog = case
    store = _Store()
    service = _service(store)
    photo = IMAGE_DEFECTS[defect]() if IMAGE in steps else None
    old = CATALOG in steps and catalog == "antiguo"
    started = NOW - (dt.timedelta(days=3) if old else dt.timedelta(minutes=10))
    document = _document(photo=photo, started=started)
    if IDEMPOTENCY in steps:
        # Otra foto con el mismo photo_id ya aceptada (antes de romper nada más).
        other = _document(photo_id=uuid.UUID(document["photo_id"]), absent_reason="pending_cap")
        assert _code(service, other) == "accepted"
    if CATALOG in steps:
        if catalog == "estandar":
            store.versions = [_view(standards=frozenset({(STANDARD, 9)}))]
        elif catalog == "camara":
            store.versions = [_view(cameras=frozenset())]
        else:
            store.retention = 1
    if GATE in steps:
        store.spans = []
    options: dict[str, Any] = {}
    if SCHEMA in steps:
        options = {"version": "1.4.0"} if schema == "contract_version" else {"key": "otra"}
    found = _code(service, document, **options)
    first = min(steps)
    assert RejectionCode(found) in STEP_CODES[first], (sorted(steps), found)
    assert len(store.rows) == (1 if IDEMPOTENCY in steps else 0)


@given(changed=st.sets(st.sampled_from(["catalogo", "camara", "compuerta"]), min_size=1))
def test_a_resent_photo_is_a_duplicate_even_after_the_catalog_or_the_gate_changed(
    changed: set[str],
) -> None:
    store = _Store()
    service = _service(store)
    document = _document()
    first = _accept(service, document)
    assert not first.duplicate
    if "catalogo" in changed:
        store.versions = [_view(standards=frozenset({(STANDARD, 2)}))]
    if "camara" in changed:
        store.versions = [_view(cameras=frozenset())]
    if "compuerta" in changed:
        store.spans = []
    again = _accept(service, document)
    assert again.duplicate
    assert again.receipt.platform_record_id == first.receipt.platform_record_id
    assert again.receipt.received_at == first.receipt.received_at
    assert len(store.rows) == 1


# --- Casos aislados de cada guarda del servicio ---------------------------------------------------


def _rejected(service: EpisodePhotoService, document: dict[str, Any], **options: Any) -> Any:
    with pytest.raises(IngestRejected) as raised:
        _accept(service, document, **options)
    return raised.value.code.value, raised.value.field


def test_the_body_must_declare_the_header_version_and_the_key_must_be_the_photo_id() -> None:
    store = _Store()
    service = _service(store)
    document = _document()
    assert _rejected(service, document, version="1.4.0") == ("schema_invalid", "contract_version")
    assert _rejected(service, document, key=str(uuid.uuid4())) == (
        "schema_invalid",
        "Idempotency-Key",
    )
    assert store.rows == {}


def test_the_same_photo_id_with_other_bytes_is_an_idempotency_conflict() -> None:
    store = _Store()
    service = _service(store)
    document = _document()
    assert not _accept(service, document).duplicate
    other = {**document, "max_confidence": 0.5}
    assert _rejected(service, other) == ("idempotency_conflict", "photo_id")
    assert len(store.rows) == 1


def test_an_old_photo_is_timestamp_out_of_window() -> None:
    store = _Store(retention=1)
    service = _service(store)
    document = _document(started=NOW - dt.timedelta(days=3))
    assert _rejected(service, document) == ("timestamp_out_of_window", "node_time")
    assert store.rows == {}
    # Dentro de la ventana del nodo, la misma foto se acepta.
    assert not _accept(service, _document()).duplicate


def test_a_photo_without_the_mounting_gate_is_zone_gate_not_approved() -> None:
    store = _Store(spans=[GateSpan(False, NOW - dt.timedelta(days=400), None)])
    service = _service(store)
    assert _rejected(service, _document()) == ("zone_gate_not_approved", "zone_id")
    assert store.rows == {}


def test_a_photo_with_a_bad_image_writes_nothing() -> None:
    store = _Store()
    service = _service(store)
    document = _document(photo=image(jpeg(marked=False)))
    assert _rejected(service, document) == ("clip_not_anonymized", "image.data_base64")
    assert store.rows == {} and store.pending == []


# --- Alcance antes que el esquema (before_schema) -------------------------------------------------


@pytest.mark.parametrize("name", ["organization_id", "plant_id", "node_id", "zone_id"])
def test_a_foreign_scope_field_is_node_zone_mismatch_before_the_schema(name: str) -> None:
    body = {
        "organization_id": str(ORGANIZATION),
        "plant_id": str(PLANT),
        "node_id": str(NODE),
        "zone_id": str(ZONE),
        "image": "no es un objeto",
    }
    context: Any = _OrganizationContext(ORGANIZATION)
    node = NodeScope(
        context=context,
        node_id=NODE,
        plant_id=PLANT,
        zone_ids=frozenset({ZONE}),
        certificate_serial="ab",
        credential_status="active",
    )
    scope_before_schema(node, body)  # todo propio: decide el esquema después
    with pytest.raises(NodeRejection) as raised:
        scope_before_schema(node, {**body, name: str(uuid.uuid4())})
    assert raised.value.code is RejectionCode.NODE_ZONE_MISMATCH
    assert raised.value.field == name
    # Un valor que no es un UUID canónico no decide aquí: lo rechaza el esquema.
    scope_before_schema(node, {**body, name: str(uuid.uuid4()).upper()})


class _OrganizationContext:
    def __init__(self, organization_id: uuid.UUID) -> None:
        self.organization_id = organization_id


def test_the_photo_has_its_own_bucket_of_120_per_minute_with_a_burst_of_30() -> None:
    budget = NODE_BUDGETS[NodeRoute.EPISODE_PHOTO]
    assert budget == RouteBudget("episode_photo", Budget(120, 60, burst=30))
    assert budget is not NODE_BUDGETS[NodeRoute.FINDING]
