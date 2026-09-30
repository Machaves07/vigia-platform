"""``LabelPort.consultar``: PR-NUC-24, sin texto libre, auditada y sin exportación (TASK-119).

Contra PostgreSQL 16 real, como ``vigia_app``, con etiquetas proyectadas por
``EscritorExpediente`` al escribir clasificaciones y resoluciones de revisión:

- **PR-NUC-24** (invariante y oráculo): los sujetos salen de ``classification_sequence`` del kit
  de U-01 (hallazgos y detecciones para revisión con sus clips), repartidos entre tres zonas de
  dos plantas; sobre cada uno se escriben de 0 a 2 decisiones de U-04 (``classification`` o
  ``review_resolution``, con la forma de U-04 §5.3 y el firmante con ``display_name``). Por cada
  registro fuente escrito existe **exactamente una** etiqueta con lo que dice la regla, y la
  consulta con alcances, zona, familia, motivo, periodo y tamaño de página generados, recorrida
  página a página, devuelve **la misma lista** que un filtro por fuerza bruta sobre todas las
  etiquetas de la organización leídas como superusuario, en el mismo orden y sin repetir.
- **Solo identificadores** (criterio 2, BR-NUC-67, NFR-NUC-35): cada campo de la respuesta es un
  identificador, un código cerrado, una marca o una lista de identificadores de evidencia; ni el
  ``display_name`` del firmante ni claves de almacén, URL o bytes de clips.
- **Auditada** (BR-NUC-59): una entrada ``label_read`` por página con los filtros y
  ``result_count``, en la misma transacción; un contexto sin ``labels.read`` queda auditado como
  ``denied`` y no ve nada; una consulta inválida no consulta ni audita.
- **Sin exportación** (BR-NUC-68): el puerto no tiene más operación que ``consultar``.

Solo datos generados: los clips son metadatos de bytes sintéticos que nunca se suben.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import inspect
import json
import os
import re
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import pytest
from hypothesis import event, find, given, target
from hypothesis import strategies as st
from pydantic import Field, StrictStr
from vigia_contracts.conformance.generators import classification_sequence
from vigia_contracts.conformance.generators.stub_commands import record_kind
from vigia_contracts.models.common import UUID
from vigia_contracts.models.detection_for_review import DetectionForReviewSubmission

from tests.factories import uuid7
from tests.identity_db import MigratedDatabase, migrated_database
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import InjectedFault
from tests.writer_support import (
    FINDING_FREE_TEXT,
    FINDING_TYPE,
    Fault,
    Place,
    WriterEnvironment,
    clips_of,
    unit_context,
    writer_environment,
)
from vigia_platform.ledger.application.labels import (
    LABEL_READ_ROLES,
    MAX_PAGE_SIZE,
    LabelCursor,
    LabelPageRequest,
    LabelPeriod,
    LabelPort,
    LabelQueryInvalid,
    LabelReadDenied,
    LabelService,
    LabelView,
)
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.ledger.registry import ChainLevel, ContentModel, LabelRule, RecordType
from vigia_platform.shared.context import (
    Actor,
    ActorKind,
    ActorUnit,
    AllowedScope,
    ContextAbsent,
    ContextOrigin,
    Role,
    ScopeContext,
    ScopeLevel,
    _seal_scope_context,
)

pytestmark = pytest.mark.integration

DETECTION_TYPE = "detection_for_review_received"
CLASSIFICATION_TYPE = "classification_label_probe"
REVIEW_TYPE = "review_resolution_label_probe"

REASONS = ("guard_open", "authorized_maintenance", "sensor_glare", "zone_occupied")
SIGNER_NAMES = ("CANARIO-NOMBRE Ana Pérez", "CANARIO-NOMBRE Luis Gómez")
"""Nombres del firmante: si alguno aparece en una respuesta, la consulta filtra texto libre."""

_ALL_TIME = LabelPeriod(datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
"""Un periodo que contiene toda marca de la base de prueba (su reloj es el real)."""

SnakeCode = Annotated[
    StrictStr, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
]
DisplayName = Annotated[StrictStr, Field(min_length=1, max_length=120)]
Family = Literal["dwell", "coexistence", "startup_transition", "guard_bypass"]


class SignerSnapshot(ContentModel):
    """``signer`` y ``resolved_by`` de U-04 §5.3: identificador, nombre (instantánea) y rol."""

    user_id: UUID
    display_name: DisplayName
    role_in_use: Literal["coordinator_sst", "plant_manager"]


class ClassificationLabelProbe(ContentModel):
    """Las rutas de ``classification`` (U-04 §5.3) que usa su ``label_rule``."""

    classification_id: UUID
    plant_id: UUID
    zone_id: UUID
    anchor_record_id: UUID
    family: Family
    outcome: Literal["confirmed", "authorized_operation", "false_positive"]
    reason_category: SnakeCode
    signer: SignerSnapshot


class ReviewResolutionLabelProbe(ContentModel):
    """Las rutas de ``review_resolution`` (U-04 §5.3) que usa su ``label_rule``."""

    resolution_id: UUID
    plant_id: UUID
    zone_id: UUID
    anchor_record_id: UUID
    family: Family
    outcome: Literal["review_confirmed", "review_discarded"]
    reason_category: SnakeCode
    resolved_by: SignerSnapshot


def _rule(labeled_by: str) -> LabelRule:
    return LabelRule(
        subject_record_path="/anchor_record_id",
        family_path="/family",
        outcome_path="/outcome",
        reason_category_path="/reason_category",
        labeled_by_path=labeled_by,
    )


LABEL_TYPES = (
    RecordType(
        record_type=DETECTION_TYPE,
        writer_unit=ActorUnit.U03,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=DetectionForReviewSubmission,
        source_key_path="/detection_id",
        free_text_paths=FINDING_FREE_TEXT,
        evidence_paths=("/cameras[*]/clips[*]",),
    ),
    RecordType(
        record_type=CLASSIFICATION_TYPE,
        writer_unit=ActorUnit.U04,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=ClassificationLabelProbe,
        source_key_path="/classification_id",
        free_text_paths=("/signer/display_name",),
        label_rule=_rule("/signer"),
    ),
    RecordType(
        record_type=REVIEW_TYPE,
        writer_unit=ActorUnit.U04,
        chain_level=ChainLevel.PLANT,
        schema_version=1,
        content_model=ReviewResolutionLabelProbe,
        source_key_path="/resolution_id",
        free_text_paths=("/resolved_by/display_name",),
        label_rule=_rule("/resolved_by"),
    ),
)


# --- Entorno ----------------------------------------------------------------------------------


@dataclass
class LabelEnvironment:
    env: WriterEnvironment
    labels: LabelService

    def run(self, awaitable: Any) -> Any:
        return self.env.loop.run(awaitable)

    @property
    def migrated(self) -> MigratedDatabase:
        return self.env.migrated


@pytest.fixture(scope="module")
def environment(postgres_endpoint: PostgresEndpoint) -> Iterator[LabelEnvironment]:
    with (
        migrated_database(postgres_endpoint, "labels_query") as migrated,
        writer_environment(migrated, extra_types=LABEL_TYPES) as env,
    ):
        yield LabelEnvironment(env, LabelService(database=env.database, audit=env.audit))


def reader_context(organization_id: uuid.UUID, scopes: Sequence[AllowedScope]) -> ScopeContext:
    """Contexto de sesión de una persona con exactamente ``scopes`` como asignaciones."""
    actor = Actor(
        kind=ActorKind.USER,
        id=uuid.uuid4(),
        display_name_snapshot="Coordinación SST sintética",
        unit=ActorUnit.U02,
        role_in_use=scopes[0].role if scopes else Role.COORDINATOR_SST,
    )
    return _seal_scope_context(
        organization_id=organization_id,
        actor=actor,
        origin=ContextOrigin.SESSION,
        allowed_scopes=list(scopes),
        correlation_id=uuid7(),
        session_id_hash=hashlib.sha256(os.urandom(8)).hexdigest(),
    )


def whole(organization_id: uuid.UUID, role: Role = Role.COORDINATOR_SST) -> ScopeContext:
    return reader_context(
        organization_id, [AllowedScope(ScopeLevel.ORGANIZATION, organization_id, role)]
    )


# --- Escenario: sujetos del kit y decisiones de U-04 --------------------------------------------


@dataclass(frozen=True)
class Tenant:
    organization_id: uuid.UUID
    places: tuple[Place, ...]
    """Tres zonas: dos en la planta A y una en la planta B."""


def new_tenant() -> Tenant:
    organization_id = uuid.uuid4()
    plant_a, plant_b = uuid.uuid4(), uuid.uuid4()
    return Tenant(
        organization_id,
        tuple(
            Place(organization_id, plant, uuid.uuid4(), uuid.uuid4())
            for plant in (plant_a, plant_a, plant_b)
        ),
    )


@dataclass(frozen=True)
class Subject:
    record_id: uuid.UUID
    kind: str
    family: str
    place: Place


@dataclass(frozen=True)
class Decision:
    """Lo que la etiqueta de un registro fuente debe decir (U-04 §5.3)."""

    source_record_id: uuid.UUID
    subject: Subject
    outcome: str
    reason_category: str
    signer_id: uuid.UUID
    signer_role: str
    received_at: datetime


def _localize(record: Mapping[str, Any], place: Place, id_field: str) -> dict[str, Any]:
    """Un registro del kit llevado a ``place``: identificadores nuevos y claves de sus clips."""
    document: dict[str, Any] = json.loads(json.dumps(record))
    document[id_field] = str(uuid7())
    for name in ("organization_id", "plant_id", "zone_id", "node_id"):
        document[name] = str(getattr(place, name))
    for camera in document["cameras"]:
        for clip in camera["clips"]:
            # El kit repite identificadores entre ejemplos; cada clip real tiene su propia clave.
            clip["clip_id"] = str(uuid7())
            clip["storage_key"] = place.storage_key(clip["clip_id"])
    return document


def write(
    environment: LabelEnvironment, context: ScopeContext, record_type: str, document: Any
) -> Receipt:
    receipt = environment.run(environment.env.writer.write(context, record_type, document))
    assert isinstance(receipt, Receipt), receipt
    return receipt


def write_subject(
    environment: LabelEnvironment, record: Mapping[str, Any], place: Place
) -> Subject:
    kind = record_kind(dict(record))
    record_type, id_field = (
        (FINDING_TYPE, "finding_id") if kind == "finding" else (DETECTION_TYPE, "detection_id")
    )
    document = _localize(record, place, id_field)
    for clip in clips_of(document):
        environment.env.storage.put(clip)
    context = unit_context(place.organization_id, ActorUnit.U03, kind=ActorKind.NODE)
    receipt = write(environment, context, record_type, document)
    return Subject(receipt.record_id, kind, str(document["family"]), place)


def write_decision(
    environment: LabelEnvironment,
    subject: Subject,
    *,
    outcome: str,
    reason_category: str,
    signer_role: str,
    signer_name: str,
) -> Decision:
    signer_id = uuid.uuid4()
    signer = {"user_id": str(signer_id), "display_name": signer_name, "role_in_use": signer_role}
    common = {
        "plant_id": str(subject.place.plant_id),
        "zone_id": str(subject.place.zone_id),
        "anchor_record_id": str(subject.record_id),
        "family": subject.family,
        "outcome": outcome,
        "reason_category": reason_category,
    }
    if subject.kind == "finding":
        record_type = CLASSIFICATION_TYPE
        document = {"classification_id": str(uuid7()), **common, "signer": signer}
    else:
        record_type = REVIEW_TYPE
        document = {"resolution_id": str(uuid7()), **common, "resolved_by": signer}
    context = unit_context(subject.place.organization_id, ActorUnit.U04)
    receipt = write(environment, context, record_type, document)
    return Decision(
        receipt.record_id,
        subject,
        outcome,
        reason_category,
        signer_id,
        signer_role,
        receipt.received_at,
    )


@st.composite
def decision_values(draw: st.DrawFn, kind: str) -> dict[str, str]:
    outcomes = (
        ("confirmed", "authorized_operation", "false_positive")
        if kind == "finding"
        else ("review_confirmed", "review_discarded")
    )
    return {
        "outcome": draw(st.sampled_from(outcomes)),
        "reason_category": draw(st.sampled_from(REASONS)),
        "signer_role": draw(st.sampled_from(["coordinator_sst", "plant_manager"])),
        "signer_name": draw(st.sampled_from(SIGNER_NAMES)),
    }


async def _freeze_chain_clocks(migrated: MigratedDatabase, organization_id: uuid.UUID) -> None:
    """Adelanta las cabezas de las cadenas: el disparador toma ``greatest(reloj, updated_at)``,
    así que las decisiones siguientes empatan en ``received_at`` (desempate por ``label_id``)."""
    connection = await migrated.connect()
    try:
        await connection.execute(
            "UPDATE ledger.chain_head SET updated_at = least("
            " date_trunc('milliseconds', clock_timestamp()) + interval '5 minutes',"
            " date_trunc('month', clock_timestamp())"
            " + interval '1 month' - interval '1 millisecond')"
            " WHERE organization_id = $1 AND kind = 'ledger'",
            organization_id,
        )
    finally:
        await connection.close()


# --- Lectura como superusuario (el oráculo) -----------------------------------------------------


@dataclass(frozen=True)
class StoredLabel:
    label_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    source_record_id: uuid.UUID
    subject_record_id: uuid.UUID
    family: str
    outcome: str
    reason_category: str
    evidence_ids: tuple[uuid.UUID, ...]
    labeled_at: datetime


async def _stored_labels(migrated: MigratedDatabase, organization_id: uuid.UUID) -> Any:
    connection = await migrated.connect()
    try:
        labels = await connection.fetch(
            "SELECT * FROM ledger.label WHERE organization_id = $1", organization_id
        )
        evidence = await connection.fetch(
            "SELECT record_id, evidence_id FROM ledger.evidence WHERE organization_id = $1"
            " ORDER BY verified_at, evidence_id",
            organization_id,
        )
    finally:
        await connection.close()
    by_record: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in evidence:
        by_record.setdefault(row["record_id"], []).append(row["evidence_id"])
    stored = [
        StoredLabel(
            label_id=row["label_id"],
            plant_id=row["plant_id"],
            zone_id=row["zone_id"],
            source_record_id=row["source_record_id"],
            subject_record_id=row["subject_record_id"],
            family=row["family"],
            outcome=row["outcome"],
            reason_category=row["reason_category"],
            evidence_ids=tuple(row["evidence_ids"]),
            labeled_at=row["labeled_at"],
        )
        for row in labels
    ]
    return stored, by_record


@dataclass(frozen=True)
class AuditRow:
    operation: str
    outcome: str
    filters: Any
    result_count: int | None
    scope_zone_id: uuid.UUID | None


async def _audit_rows(migrated: MigratedDatabase, organization_id: uuid.UUID) -> list[AuditRow]:
    connection = await migrated.connect()
    try:
        rows = await connection.fetch(
            "SELECT operation, outcome, filters_json, result_count, scope_zone_id"
            " FROM shared.audit_entry WHERE organization_id = $1 ORDER BY chain_sequence",
            organization_id,
        )
    finally:
        await connection.close()
    return [
        AuditRow(
            operation=row["operation"],
            outcome=row["outcome"],
            filters=None if row["filters_json"] is None else json.loads(row["filters_json"]),
            result_count=row["result_count"],
            scope_zone_id=row["scope_zone_id"],
        )
        for row in rows
    ]


def audit_rows(environment: LabelEnvironment, organization_id: uuid.UUID) -> list[AuditRow]:
    rows: list[AuditRow] = environment.run(_audit_rows(environment.migrated, organization_id))
    return rows


# --- La consulta y su oráculo -------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    scopes: tuple[AllowedScope, ...]
    period: LabelPeriod
    zone_id: uuid.UUID | None
    family: str | None
    reason_category: str | None
    page_size: int


def visible(label: StoredLabel, context: ScopeContext) -> bool:
    """El alcance de ``labels.read``: solo las asignaciones con un rol que lo tiene."""
    for scope in context.allowed_scopes:
        if scope.role not in LABEL_READ_ROLES:
            continue
        if scope.scope_level is ScopeLevel.ORGANIZATION:
            if scope.scope_id == context.organization_id:
                return True
        elif scope.scope_level is ScopeLevel.PLANT:
            if scope.scope_id == label.plant_id:
                return True
        elif scope.scope_level is ScopeLevel.ZONE and scope.scope_id == label.zone_id:
            return True
    return False


def brute_force(
    labels: Sequence[StoredLabel], context: ScopeContext, query: Query
) -> list[uuid.UUID]:
    """El filtro por fuerza bruta de PR-NUC-24 sobre todas las etiquetas, en el orden del puerto."""
    kept = [
        label
        for label in labels
        if visible(label, context)
        and query.period.start <= label.labeled_at < query.period.end
        and query.zone_id in (None, label.zone_id)
        and query.family in (None, label.family)
        and query.reason_category in (None, label.reason_category)
    ]
    kept.sort(key=lambda label: (label.labeled_at, label.label_id), reverse=True)
    return [label.label_id for label in kept]


def walk(
    environment: LabelEnvironment, context: ScopeContext, query: Query
) -> list[tuple[LabelView, ...]]:
    """Todas las páginas de la consulta, siguiendo ``next_cursor``."""
    pages: list[tuple[LabelView, ...]] = []
    after: LabelCursor | None = None
    while True:
        page = environment.run(
            environment.labels.consultar(
                context,
                query.period,
                zone_id=query.zone_id,
                family=query.family,
                reason_category=query.reason_category,
                page=LabelPageRequest(size=query.page_size, after=after),
            )
        )
        pages.append(page.items)
        if page.next_cursor is None:
            return pages
        assert len(pages) <= 1 + 60 // query.page_size, "la paginación no termina"
        after = page.next_cursor


def whole_walk(tenant: Tenant, labels: Sequence[StoredLabel]) -> Query:
    """Toda la organización, todo el periodo, sin filtros y de una en una: tantas páginas como
    etiquetas, así que cada ejemplo con dos o más recorre la paginación de verdad."""
    stamps = [label.labeled_at for label in labels]
    return Query(
        scopes=(
            AllowedScope(ScopeLevel.ORGANIZATION, tenant.organization_id, Role.COORDINATOR_SST),
        ),
        period=LabelPeriod(min(stamps), max(stamps) + timedelta(microseconds=1)),
        zone_id=None,
        family=None,
        reason_category=None,
        page_size=1,
    )


@st.composite
def queries(
    draw: st.DrawFn, tenant: Tenant, labels: Sequence[StoredLabel], families: Sequence[str]
) -> Query:
    """Una consulta generada; la mitad de las veces amplia (resultados y varias páginas)."""
    if draw(st.booleans(), label="broad"):
        return draw(broad_queries(tenant, labels, families))
    return draw(narrow_queries(tenant, labels, families))


@st.composite
def broad_queries(
    draw: st.DrawFn, tenant: Tenant, labels: Sequence[StoredLabel], families: Sequence[str]
) -> Query:
    """Alcance de toda la organización (o de su planta A), periodo que cubre todas las marcas,
    a lo sumo un filtro y páginas de 1 o 2: el caso que más páginas con resultados recorre."""
    organization_id = tenant.organization_id
    grant = draw(
        st.sampled_from(
            [
                AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.COORDINATOR_SST),
                AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.PLANT_MANAGER),
                AllowedScope(ScopeLevel.PLANT, tenant.places[0].plant_id, Role.PLANT_MANAGER),
            ]
        )
    )
    # Ruido: una asignación sin ``labels.read`` que no debe ampliar el alcance.
    noise = AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.LINE_MANAGER)
    scopes = (grant, noise) if draw(st.booleans()) else (grant,)
    stamps = sorted(label.labeled_at for label in labels)
    period = LabelPeriod(stamps[0], stamps[-1] + timedelta(microseconds=1))
    zone_id: uuid.UUID | None = None
    family: str | None = None
    reason: str | None = None
    which = draw(st.sampled_from(["none", "none", "zone", "family", "reason"]))
    if which == "zone":
        zone_id = draw(st.sampled_from([p.zone_id for p in tenant.places]))
    elif which == "family":
        family = draw(st.sampled_from(families))
    elif which == "reason":
        reason = draw(st.sampled_from(REASONS))
    return Query(scopes, period, zone_id, family, reason, draw(st.integers(1, 2)))


@st.composite
def narrow_queries(
    draw: st.DrawFn, tenant: Tenant, labels: Sequence[StoredLabel], families: Sequence[str]
) -> Query:
    """Alcances mezclados y ajenos, periodos entre marcas vecinas y filtros sin coincidencias."""
    organization_id = tenant.organization_id
    places = tenant.places
    role = st.sampled_from(sorted(Role, key=str))
    scope = st.one_of(
        st.builds(
            AllowedScope,
            st.just(ScopeLevel.ORGANIZATION),
            st.sampled_from([organization_id, uuid.uuid4()]),
            role,
        ),
        st.builds(
            AllowedScope,
            st.just(ScopeLevel.PLANT),
            st.sampled_from([places[0].plant_id, places[2].plant_id, uuid.uuid4()]),
            role,
        ),
        st.builds(
            AllowedScope,
            st.just(ScopeLevel.ZONE),
            st.sampled_from([*(p.zone_id for p in places), uuid.uuid4()]),
            role,
        ),
    )
    # Al menos una asignación con ``labels.read``: el caso sin ninguna es ``denied`` (aparte).
    granted = draw(
        scope.filter(lambda s: s.role in LABEL_READ_ROLES)
        if draw(st.booleans())
        else st.just(AllowedScope(ScopeLevel.ORGANIZATION, organization_id, Role.PLANT_MANAGER))
    )
    scopes = (granted, *draw(st.lists(scope, max_size=3)))
    # Bordes del periodo en las marcas reales: inicio inclusivo y fin exclusivo, exactos.
    stamps = sorted({label.labeled_at for label in labels})
    earliest = stamps[0] if stamps else _ALL_TIME.start
    latest = stamps[-1] if stamps else earliest
    edges = [earliest - timedelta(seconds=1), *stamps, latest + timedelta(seconds=1)]
    start_index = draw(st.integers(0, len(edges) - 2))
    end_index = draw(st.integers(start_index + 1, len(edges) - 1))
    return Query(
        scopes=scopes,
        period=LabelPeriod(edges[start_index], edges[end_index]),
        zone_id=draw(st.none() | st.sampled_from([*(p.zone_id for p in places), uuid.uuid4()])),
        family=draw(st.none() | st.sampled_from([*families, "guard_bypass", "dwell"])),
        reason_category=draw(st.none() | st.sampled_from([*REASONS, "unused_reason"])),
        page_size=draw(st.integers(1, 3)),
    )


# --- Solo identificadores -----------------------------------------------------------------------

_CLOSED = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def assert_identifiers_only(view: LabelView) -> None:
    """Criterio 2: cada campo es identificador, código cerrado, marca o lista de evidencias."""
    for field in dataclasses.fields(view):
        value = getattr(view, field.name)
        if value is None or isinstance(value, uuid.UUID | datetime | Role):
            continue
        if isinstance(value, tuple):
            assert all(isinstance(item, uuid.UUID) for item in value), field.name
            continue
        assert isinstance(value, str), (field.name, value)
        assert _CLOSED.fullmatch(value), (field.name, value)
    rendered = repr(view)
    assert all(name not in rendered for name in SIGNER_NAMES)
    assert "org/" not in rendered and "http" not in rendered


# --- PR-NUC-24 ---------------------------------------------------------------------------------


@given(data=st.data())
def test_one_label_per_source_and_query_equals_brute_force(
    environment: LabelEnvironment, data: st.DataObject
) -> None:
    scenario = data.draw(classification_sequence(max_size=0), label="scenario")
    tenant = new_tenant()
    records = [r for r in scenario.records if record_kind(r) != "observability_event"]
    subjects = [
        write_subject(environment, record, data.draw(st.sampled_from(tenant.places)))
        for record in records
    ]
    decisions: list[Decision] = []
    for index, subject in enumerate(subjects):
        if index and data.draw(st.booleans(), label="tie"):
            environment.run(_freeze_chain_clocks(environment.migrated, tenant.organization_id))
        # El primer sujeto lleva al menos dos decisiones: toda consulta amplia tiene 2 o más.
        for _ in range(data.draw(st.integers(2 if index == 0 else 0, 3), label="decisions")):
            values = data.draw(decision_values(subject.kind))
            decisions.append(write_decision(environment, subject, **values))

    labels, evidence_by_record = environment.run(
        _stored_labels(environment.migrated, tenant.organization_id)
    )
    # Invariante: exactamente una etiqueta por registro fuente, con lo que dice la regla.
    per_source = Counter(label.source_record_id for label in labels)
    assert per_source == Counter(d.source_record_id for d in decisions)
    by_source = {label.source_record_id: label for label in labels}
    for decision in decisions:
        label = by_source[decision.source_record_id]
        subject = decision.subject
        assert label.subject_record_id == subject.record_id
        assert (label.plant_id, label.zone_id) == (subject.place.plant_id, subject.place.zone_id)
        assert (label.family, label.outcome, label.reason_category) == (
            subject.family,
            decision.outcome,
            decision.reason_category,
        )
        assert label.evidence_ids == tuple(evidence_by_record.get(subject.record_id, ()))
        assert label.labeled_at == decision.received_at

    # Oráculo: la consulta recorrida página a página es el filtro por fuerza bruta. La primera,
    # obligatoria, recorre todas las etiquetas de una en una (2 o más páginas en cada ejemplo).
    assert len(labels) >= 2
    families = sorted({s.family for s in subjects})
    signers = {d.source_record_id: (d.signer_id, d.signer_role) for d in decisions}
    pages = check_query(environment, tenant, labels, by_source, signers, whole_walk(tenant, labels))
    assert len(pages) == len(labels) >= 2
    most_pages = 0
    for _ in range(data.draw(st.integers(1, 3), label="queries")):
        query = data.draw(queries(tenant, labels, families), label="query")
        pages = check_query(environment, tenant, labels, by_source, signers, query)
        results = sum(len(page) for page in pages)
        event(f"consulta generada: {min(len(pages), 4)} páginas")
        event(f"consulta generada: {'con' if results else 'sin'} resultados")
        most_pages = max(most_pages, len(pages))
    target(float(most_pages), label="páginas de la consulta generada más larga")


def check_query(
    environment: LabelEnvironment,
    tenant: Tenant,
    labels: Sequence[StoredLabel],
    by_source: Mapping[uuid.UUID, StoredLabel],
    signers: Mapping[uuid.UUID, tuple[uuid.UUID, str]],
    query: Query,
) -> list[tuple[LabelView, ...]]:
    """Recorre ``query`` y la compara con la fuerza bruta, vista a vista y entrada a entrada."""
    context = reader_context(tenant.organization_id, query.scopes)
    audit_before = len(audit_rows(environment, tenant.organization_id))
    pages = walk(environment, context, query)
    got = [view for page in pages for view in page]
    assert [view.label_id for view in got] == brute_force(labels, context, query)
    assert all(0 < len(page) <= query.page_size for page in pages[:-1])
    assert len(pages[-1]) <= query.page_size
    for view in got:
        assert_identifiers_only(view)
        stored = by_source[view.source_record_id]
        assert view.label_id == stored.label_id
        assert view.evidence_ids == stored.evidence_ids
        assert (view.labeled_by_user_id, view.labeled_by_role) == signers[view.source_record_id]
    entries = audit_rows(environment, tenant.organization_id)[audit_before:]
    assert [(e.operation, e.outcome) for e in entries] == [("label_read", "success")] * len(pages)
    assert [e.result_count for e in entries] == [len(page) for page in pages]
    assert all(e.scope_zone_id == query.zone_id for e in entries)
    return pages


# --- Casos con nombre ---------------------------------------------------------------------------


@functools.cache
def _kit_scenario() -> Any:
    """Un escenario mínimo del kit con al menos un hallazgo (determinista: ``find``)."""
    return find(
        classification_sequence(max_size=0),
        lambda drawn: any(record_kind(r) == "finding" for r in drawn.records),
    )


@pytest.fixture
def seeded(environment: LabelEnvironment) -> tuple[Tenant, list[Decision]]:
    """Un hallazgo con dos clasificaciones y una detección con una resolución."""
    tenant = new_tenant()
    finding = next(r for r in _kit_scenario().records if record_kind(r) == "finding")
    subject = write_subject(environment, finding, tenant.places[0])
    decisions = [
        write_decision(
            environment,
            subject,
            outcome="confirmed",
            reason_category="guard_open",
            signer_role="coordinator_sst",
            signer_name=SIGNER_NAMES[0],
        ),
        write_decision(
            environment,
            subject,
            outcome="false_positive",
            reason_category="sensor_glare",
            signer_role="plant_manager",
            signer_name=SIGNER_NAMES[1],
        ),
    ]
    return tenant, decisions


def _all_time() -> LabelPeriod:
    return _ALL_TIME


def test_a_label_carries_evidence_ids_and_signer_id_but_never_the_name(
    environment: LabelEnvironment, seeded: tuple[Tenant, list[Decision]]
) -> None:
    tenant, decisions = seeded
    page = environment.run(environment.labels.consultar(whole(tenant.organization_id), _all_time()))
    assert {view.source_record_id for view in page.items} == {d.source_record_id for d in decisions}
    for view in page.items:
        assert_identifiers_only(view)
        assert view.evidence_ids, "el hallazgo del kit trae clips"
    assert {view.labeled_by_role for view in page.items} == {
        Role.COORDINATOR_SST,
        Role.PLANT_MANAGER,
    }


def test_filters_by_zone_family_and_reason(
    environment: LabelEnvironment, seeded: tuple[Tenant, list[Decision]]
) -> None:
    tenant, decisions = seeded
    context = whole(tenant.organization_id)
    family = decisions[0].subject.family

    def ids(**filters: Any) -> set[uuid.UUID]:
        page = environment.run(environment.labels.consultar(context, _all_time(), **filters))
        return {view.source_record_id for view in page.items}

    everything = {d.source_record_id for d in decisions}
    assert ids(zone_id=tenant.places[0].zone_id) == everything
    assert ids(zone_id=tenant.places[1].zone_id) == set()
    assert ids(family=family) == everything
    assert ids(family="not_a_family") == set()
    assert ids(reason_category="guard_open") == {decisions[0].source_record_id}
    assert ids(reason_category="sensor_glare", family=family) == {decisions[1].source_record_id}


def test_period_start_is_inclusive_and_end_exclusive(
    environment: LabelEnvironment, seeded: tuple[Tenant, list[Decision]]
) -> None:
    tenant, decisions = seeded
    context = whole(tenant.organization_id)
    first = decisions[0].received_at
    one_us = timedelta(microseconds=1)

    def ids(start: datetime, end: datetime) -> set[uuid.UUID]:
        page = environment.run(environment.labels.consultar(context, LabelPeriod(start, end)))
        return {view.source_record_id for view in page.items}

    assert decisions[0].source_record_id in ids(first, first + one_us)
    assert decisions[0].source_record_id not in ids(first - one_us, first)
    assert decisions[0].source_record_id not in ids(first + one_us, first + 2 * one_us)


def test_tied_labels_are_walked_one_by_one_in_key_order(environment: LabelEnvironment) -> None:
    """Empates en ``labeled_at``: de una en una, en orden ``(labeled_at, label_id)`` descendente,
    sin repetir ni omitir, y una entrada ``label_read`` con ``result_count`` 1 por página."""
    tenant = new_tenant()
    finding = next(r for r in _kit_scenario().records if record_kind(r) == "finding")
    subject = write_subject(environment, finding, tenant.places[0])
    environment.run(_freeze_chain_clocks(environment.migrated, tenant.organization_id))
    tied = [
        write_decision(
            environment,
            subject,
            outcome="confirmed",
            reason_category=REASONS[index % len(REASONS)],
            signer_role="coordinator_sst",
            signer_name=SIGNER_NAMES[0],
        )
        for index in range(4)
    ]
    assert len({decision.received_at for decision in tied}) == 1, "las cuatro deben empatar"
    labels, _ = environment.run(_stored_labels(environment.migrated, tenant.organization_id))
    expected = [
        label.label_id
        for label in sorted(labels, key=lambda label: (label.labeled_at, label.label_id))[::-1]
    ]
    before = len(audit_rows(environment, tenant.organization_id))
    context = whole(tenant.organization_id)
    walked: list[uuid.UUID] = []
    after: LabelCursor | None = None
    for _ in range(len(expected) + 1):
        page = environment.run(
            environment.labels.consultar(
                context, _all_time(), page=LabelPageRequest(size=1, after=after)
            )
        )
        walked.extend(view.label_id for view in page.items)
        if page.next_cursor is None:
            break
        after = page.next_cursor
    assert walked == expected
    assert len(set(walked)) == len(walked) == 4
    entries = audit_rows(environment, tenant.organization_id)[before:]
    assert [e.result_count for e in entries] == [1, 1, 1, 1]
    assert [e.filters.get("page_size") for e in entries] == [1, 1, 1, 1]
    assert "after" not in entries[0].filters
    assert all("after" in e.filters for e in entries[1:])


def test_a_context_without_labels_read_is_denied_and_audited(
    environment: LabelEnvironment, seeded: tuple[Tenant, list[Decision]]
) -> None:
    tenant, _ = seeded
    organization_id = tenant.organization_id
    without = [role for role in Role if role not in LABEL_READ_ROLES]
    for role in without:
        context = reader_context(
            organization_id, [AllowedScope(ScopeLevel.ORGANIZATION, organization_id, role)]
        )
        before = len(audit_rows(environment, organization_id))
        with pytest.raises(LabelReadDenied) as denied:
            environment.run(environment.labels.consultar(context, _all_time()))
        assert denied.value.code == "not_found", "como authorize: nunca forbidden"
        entries = audit_rows(environment, organization_id)[before:]
        assert [(e.operation, e.outcome, e.result_count) for e in entries] == [
            ("label_read", "denied", 0)
        ]
    # Sin asignaciones, igual.
    with pytest.raises(LabelReadDenied):
        environment.run(
            environment.labels.consultar(reader_context(organization_id, []), _all_time())
        )


def test_a_scope_of_another_zone_or_organization_sees_nothing(
    environment: LabelEnvironment, seeded: tuple[Tenant, list[Decision]]
) -> None:
    tenant, _ = seeded
    organization_id = tenant.organization_id
    other_zone = reader_context(
        organization_id,
        [AllowedScope(ScopeLevel.ZONE, tenant.places[1].zone_id, Role.COORDINATOR_SST)],
    )
    for context, zone in (
        (other_zone, None),
        (other_zone, tenant.places[0].zone_id),
        (whole(uuid.uuid4()), None),
    ):
        page = environment.run(environment.labels.consultar(context, _all_time(), zone_id=zone))
        assert page.items == () and page.next_cursor is None


def test_the_audit_entry_records_the_filters_as_requested(
    environment: LabelEnvironment, seeded: tuple[Tenant, list[Decision]]
) -> None:
    tenant, decisions = seeded
    organization_id = tenant.organization_id
    period = LabelPeriod(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2099, 1, 1, 5, 0, 0, 123456, tzinfo=UTC)
    )
    zone = tenant.places[0].zone_id
    before = len(audit_rows(environment, organization_id))
    page = environment.run(
        environment.labels.consultar(
            whole(organization_id),
            period,
            zone_id=zone,
            family=decisions[0].subject.family,
            reason_category="guard_open",
            page=LabelPageRequest(size=7),
        )
    )
    (entry,) = audit_rows(environment, organization_id)[before:]
    assert entry.filters == {
        "labeled_from": "2026-09-01T00:00:00.000000Z",
        "labeled_before": "2099-01-01T05:00:00.123456Z",
        "zone_id": str(zone),
        "family": decisions[0].subject.family,
        "reason_category": "guard_open",
        "page_size": 7,
    }
    assert entry.result_count == len(page.items) == 1
    assert entry.scope_zone_id == zone


def test_the_audit_entry_is_in_the_same_transaction_as_the_query(
    environment: LabelEnvironment, seeded: tuple[Tenant, list[Decision]]
) -> None:
    tenant, _ = seeded
    organization_id = tenant.organization_id
    before = len(audit_rows(environment, organization_id))
    # Sentencias de la transacción: la consulta y la entrada de auditoría.
    environment.env.database.next_fault = Fault(statement=2)
    with pytest.raises(InjectedFault):
        environment.run(environment.labels.consultar(whole(organization_id), _all_time()))
    environment.env.database.next_fault = Fault(commit=True)
    with pytest.raises(Exception) as raised:
        environment.run(environment.labels.consultar(whole(organization_id), _all_time()))
    assert not isinstance(raised.value, AssertionError)
    environment.env.database.next_fault = None
    assert len(audit_rows(environment, organization_id)) == before


_NOW = datetime(2026, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("period", "filters"),
    [
        (None, {}),
        (LabelPeriod(_NOW, _NOW), {}),
        (LabelPeriod(_NOW, _NOW - timedelta(microseconds=1)), {}),
        (LabelPeriod(_NOW.replace(tzinfo=None), _NOW + timedelta(days=1)), {}),
        (LabelPeriod(_NOW, _NOW.replace(tzinfo=None) + timedelta(days=1)), {}),
        (LabelPeriod("2026-09-30", _NOW), {}),  # type: ignore[arg-type]
        (_all_time(), {"zone_id": str(uuid.uuid4())}),
        (_all_time(), {"family": ""}),
        (_all_time(), {"family": "Guard_Bypass"}),
        (_all_time(), {"family": "dwell%"}),
        (_all_time(), {"family": "a" * 65}),
        (_all_time(), {"family": "dwell​"}),
        (_all_time(), {"reason_category": "motivo libre"}),
        (_all_time(), {"reason_category": 7}),
        (_all_time(), {"page": LabelPageRequest(size=0)}),
        (_all_time(), {"page": LabelPageRequest(size=MAX_PAGE_SIZE + 1)}),
        (_all_time(), {"page": LabelPageRequest(size=True)}),
        (_all_time(), {"page": {"size": 5}}),
        (_all_time(), {"page": LabelPageRequest(after=LabelCursor(_NOW, None))}),  # type: ignore[arg-type]
        (
            _all_time(),
            {"page": LabelPageRequest(after=LabelCursor(_NOW.replace(tzinfo=None), uuid.uuid4()))},
        ),
        (_all_time(), {"page": LabelPageRequest(after=(_NOW, uuid.uuid4()))}),  # type: ignore[arg-type]
    ],
)
def test_an_invalid_query_neither_reads_nor_audits(
    environment: LabelEnvironment, period: Any, filters: dict[str, Any]
) -> None:
    organization_id = uuid.uuid4()
    before = len(audit_rows(environment, organization_id))
    opened = environment.env.database.probe.opened
    with pytest.raises(LabelQueryInvalid):
        environment.run(environment.labels.consultar(whole(organization_id), period, **filters))
    assert environment.env.database.probe.opened == opened
    assert len(audit_rows(environment, organization_id)) == before


def test_without_context_nothing_is_read(environment: LabelEnvironment) -> None:
    with pytest.raises(ContextAbsent):
        environment.run(environment.labels.consultar(None, _all_time()))  # type: ignore[arg-type]


async def _insert_label(migrated: MigratedDatabase, tenant: Tenant, labeled_by: Any) -> None:
    place = tenant.places[2]
    connection = await migrated.connect()
    try:
        await connection.execute(
            "INSERT INTO ledger.label (label_id, organization_id, plant_id, zone_id,"
            " source_record_id, subject_record_id, family, outcome, reason_category,"
            " evidence_ids, labeled_at, labeled_by) VALUES ($1, $2, $3, $4, $5, $6, 'dwell',"
            " 'confirmed', 'guard_open', '{}', now(), $7::jsonb)",
            uuid.uuid4(),
            tenant.organization_id,
            place.plant_id,
            place.zone_id,
            uuid.uuid4(),
            uuid.uuid4(),
            json.dumps(labeled_by),
        )
    finally:
        await connection.close()


@pytest.mark.parametrize(
    "labeled_by",
    [
        {"user_id": "Ana Pérez", "role_in_use": "Coordinadora SST"},
        {"user_id": 7, "role": ["coordinator_sst"]},
        {"display_name": SIGNER_NAMES[0], "email": "ana@example.com", "face": "base64"},
        {"user_id": " " + str(uuid.uuid4()), "role_in_use": "coordinator_sst​"},
        {"user_id": str(uuid.uuid4()), "role_in_use": "root"},
        {},
    ],
)
def test_a_hostile_signer_snapshot_yields_no_text(
    environment: LabelEnvironment, labeled_by: Any
) -> None:
    """Una instantánea con otra forma no filtra nada: solo sale un UUID o un rol cerrado."""
    tenant = new_tenant()
    environment.run(_insert_label(environment.migrated, tenant, labeled_by))
    (view,) = environment.run(
        environment.labels.consultar(whole(tenant.organization_id), _all_time())
    ).items
    assert_identifiers_only(view)
    assert view.labeled_by_role is None
    assert view.labeled_by_user_id is None or isinstance(view.labeled_by_user_id, uuid.UUID)
    assert "Pérez" not in repr(view) and "example.com" not in repr(view)


# --- BR-NUC-68: sin exportación -----------------------------------------------------------------


def test_the_port_offers_no_export_operation() -> None:
    """``consultar`` es la única operación pública del puerto y de su implementación."""
    for owner in (LabelPort, LabelService):
        public = {
            name
            for name, member in inspect.getmembers(owner)
            if not name.startswith("_") and callable(member)
        }
        assert public == {"consultar"}, (owner, public)
    # Ni almacén ni URL: la consulta no puede entregar imágenes.
    source = inspect.getsource(inspect.getmodule(LabelService))  # type: ignore[arg-type]
    assert "storage_key" not in source and "presign" not in source
