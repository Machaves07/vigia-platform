"""Publicación del catálogo firmado contra PostgreSQL 16 real (TASK-208, LC-GOB-01, S-PLA-04).

``CatalogPublicationService`` real sobre la base migrada como ``vigia_app``, con
``EscritorExpediente``, la bandeja sincronizada con los eventos del catálogo, la política de texto
libre con el validador mínimo de U-03 y un doble del puerto de firma que delega en el
``SigningService`` real (cuenta llamadas, puede caerse o quedarse retenido):

- **Una transacción** (S-PLA-04): versión con su sobre conservado, ``superseded_at`` de la
  anterior, versiones de estándar, ``catalog_version_published`` (``source_key =
  zone_id:catalog_version``), ``catalog_standard_retired``, ``single_occupancy_declared``,
  ``zone_camera`` y un solo ``catalog_updated``; el sobre guardado verifica con el verificador de
  U-01 (``expected_purpose = catalog``).
- **PR-GOB-05 sobre la base**: para toda secuencia generada (``catalog_versions``) los números van
  de uno en uno sin huecos, exactamente una versión vigente, y el hash de cada sobre emitido no
  cambia tras publicaciones posteriores.
- **Concurrencia**: dos publicaciones simultáneas de la zona dejan ``n+1`` y ``n+2``; la segunda
  espera el candado (se comprueba en ``pg_locks``, sin topes de pared). Sin el candado, las dos
  componen ``n+1`` y una pierde: la prueba falla.
- **Fallo cerrado** (FS-GOB-02 base): con la firma caída o retenida más allá del tope, cero filas,
  cero registros y cero eventos; restablecida, la siguiente toma el número siguiente.
- **NFR-GOB-38**, **NFR-GOB-10** (cero firmas en N lecturas de ``stored_envelope``), **guardas de
  alcance** (organización, planta y zona → ``not_found``; filtro de zona en las sentencias) y
  **solo anexar** para ``vigia_app``.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

import pytest
from hypothesis import given
from sqlalchemy import text
from vigia_contracts.canonical import canonical_sha256
from vigia_contracts.models.enumerations import CameraRoleInZone, PredicateFamily
from vigia_contracts.signing import KeySet, verify

from tests.authz_support import AuthzEnvironment, Site, authz_environment
from tests.examples.test_ledger_routes import StubEvidenceStorage
from tests.integration.conftest import PostgresEndpoint
from tests.outbox_support import app_database
from tests.properties.gob.strategies.catalog import CatalogScenario, catalog_versions, resolve
from tests.signing_support import SigningWorld, bootstrapped_world
from tests.writer_support import save_record_types, unit_context
from vigia_platform.catalog.adapters.postgres.admission_repository import (
    PostgresAdmissionRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import (
    PostgresCatalogRepository,
)
from vigia_platform.catalog.application.admission import AdmissionService, CatalogRejected
from vigia_platform.catalog.application.free_text_validator import (
    register_u03_free_text_validator,
)
from vigia_platform.catalog.application.publication import (
    CatalogPublicationService,
    CatalogPublicationUnavailable,
    CatalogRequestInvalid,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import (
    CatalogState,
    InitialZoneParameters,
    NewStandard,
    NewStandardVersion,
    RetireStandard,
    SetCameras,
    SetMinimumCoverage,
    SetSingleOccupancy,
    StandardDraft,
    ZoneCatalogVersion,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.standard import standard_valid_at
from vigia_platform.catalog.domain.zone_camera import ZoneCamera
from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.ledger.application.writer import EscritorExpediente
from vigia_platform.ledger.evidence import EvidenceVerifier
from vigia_platform.ledger.free_text import FreeTextPolicyRegistry
from vigia_platform.ledger.record_types.u02 import U02_RECORD_TYPES
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.shared.context import ActorKind, ActorUnit, Role, ScopeContext, ScopeLevel
from vigia_platform.shared.db import Database, Transaction
from vigia_platform.shared.outbox.publish import Outbox
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.outbox.store import SqlOutboxCatalogStore
from vigia_platform.shared.outbox.u02_events import register_u02_event_types
from vigia_platform.shared.signing import NODE_PURPOSES, SigningKeyUnavailable, SigningPurpose

pytestmark = pytest.mark.integration

REASON: Final = "Cambio sintético del catálogo de la zona"
PRESENCE: Final = {"presence": True}
ENERGY_ON: Final = {"signal_role": "energy", "value": "asserted"}
COEXISTENCE: Final = {"all_of": [PRESENCE, ENERGY_ON], "min_duration_ms": 0}
CATALOG_TYPES: Final = (
    "catalog_version_published",
    "standard_admission_test",
    "catalog_standard_retired",
    "single_occupancy_declared",
)
WAIT_SECONDS: Final = 30.0
"""Tope generoso de las esperas por evento (la decisión la da el estado, no el tope)."""
POLL_SECONDS: Final = 0.05
TEST_LOCK_TIMEOUT_MS: Final = 60_000
TEST_SIGN_TIMEOUT_SECONDS: Final = 120.0
"""Topes generosos (retro 15) para las pruebas que no tratan de los topes."""
GATE_SECONDS: Final = 60.0
PLATFORM_ONLY: Final = ("single_occupancy", "aggregation_window_minutes")


# --- Doble del puerto de firma -----------------------------------------------------------------


class SignerDouble:
    """Delegado del ``SigningService`` real: cuenta, se cae o retiene cada firma."""

    def __init__(self, world: SigningWorld) -> None:
        self.world = world
        self.calls = 0
        self.down = False
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any:
        with self._lock:
            self.calls += 1
        if self.down:
            raise SigningKeyUnavailable(purpose)
        gate = self.gate
        if gate is not None:
            gate.wait(GATE_SECONDS)
        return self.world.service.sign(purpose, payload)

    def keyset(self) -> KeySet:
        keyset = KeySet(self.world.clock)
        keyset.pin_initial(
            [k.to_contract() for p in NODE_PURPOSES for k in self.world.service.public_keys(p)]
        )
        return keyset


class RecordingMarker:
    """``RegressionMarker`` que anota lo que recibe (y puede fallar)."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, int | None, int, tuple[CatalogChangedField, ...]]] = []
        self.fail = False

    async def lock(self, transaction: Transaction, zone_id: uuid.UUID) -> None:
        assert isinstance(transaction, Transaction)

    async def mark(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        previous: ZoneCatalogVersion | None,
        new: ZoneCatalogVersion,
        changed_fields: tuple[CatalogChangedField, ...],
    ) -> None:
        assert isinstance(transaction, Transaction)
        self.calls.append(
            (
                zone_id,
                None if previous is None else previous.catalog_version,
                new.catalog_version,
                changed_fields,
            )
        )
        if self.fail:
            raise RuntimeError("marca de regresión fallida")


# --- Entorno -----------------------------------------------------------------------------------


@dataclass
class Stack:
    """Lo que comparten todos los servicios de la prueba."""

    authz: AuthzEnvironment
    database: Database
    writer: EscritorExpediente
    free_text: FreeTextPolicyRegistry
    signer: SignerDouble
    marker: RecordingMarker
    repository: PostgresCatalogRepository

    def build(self, **changes: Any) -> CatalogPublicationService:
        """El servicio real (con ``admission_for`` real de LC-GOB-02) y ``changes``."""
        sessions = self.authz.sessions
        admissions = AdmissionService(
            repository=PostgresAdmissionRepository(self.database),
            database=self.database,
            writer=self.writer,
            authorizer=self.authz.authorizer,
            audit=sessions.audit,
            free_text=self.free_text,
            clock=sessions.clock,
        )
        fields: dict[str, Any] = {
            "repository": self.repository,
            "database": self.database,
            "writer": self.writer,
            "authorizer": self.authz.authorizer,
            "audit": sessions.audit,
            "free_text": self.free_text,
            "admissions": admissions,
            "signer": self.signer,
            "clock": sessions.clock,
            "regression_marker": self.marker,
            # Retro 15: el tope de firma no es lo que prueban estas pruebas (solo
            # ``test_a_signature_that_exceeds_its_timeout_writes_nothing`` lo acorta).
            "sign_timeout_seconds": TEST_SIGN_TIMEOUT_SECONDS,
        }
        fields.update(changes)
        return CatalogPublicationService(**fields)


@dataclass
class Catalogs:
    stack: Stack
    service: CatalogPublicationService

    @property
    def authz(self) -> AuthzEnvironment:
        return self.stack.authz

    @property
    def database(self) -> Database:
        return self.stack.database

    @property
    def signer(self) -> SignerDouble:
        return self.stack.signer

    @property
    def marker(self) -> RecordingMarker:
        return self.stack.marker

    @property
    def repository(self) -> PostgresCatalogRepository:
        return self.stack.repository

    def build(self, **changes: Any) -> CatalogPublicationService:
        return self.stack.build(**changes)

    def run(self, awaitable: Any) -> Any:
        return self.authz.run(awaitable)

    def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self.authz.fetch(sql, *args)

    # --- Personas y plantas --------------------------------------------------------------------

    def site(self, plants: int = 1, zones: int = 1) -> Site:
        site = self.authz.add_site(plants=plants, zones_per_plant=zones)
        for plant in site.plants:
            for family in PredicateFamily:
                self.authz.execute(
                    "INSERT INTO catalog.family_admission (admission_id, organization_id,"
                    " plant_id, family, answers, result, evaluated_by, role_in_use,"
                    " evaluated_at, ledger_record_id) VALUES ($1, $2, $3, $4,"
                    ' \'{"standard": true, "remedy": true, "subject": true}\', \'admitted\','
                    " $5, 'administrator', $6, $7)",
                    uuid.uuid4(),
                    site.organization_id,
                    plant,
                    family.value,
                    self.authz.operator_id,
                    self.authz.now(),
                    uuid.uuid4(),
                )
        return site

    def member(
        self,
        site: Site,
        role: Role = Role.ADMINISTRATOR,
        level: ScopeLevel = ScopeLevel.ORGANIZATION,
        scope_id: uuid.UUID | None = None,
    ) -> ScopeContext:
        user_id = self.authz.add_user(site.organization_id)
        self.authz.assign(site.organization_id, user_id, role, level, scope_id)
        cookie = self.authz.open_session(site.organization_id, user_id)
        scope = self.run(self.authz.contexts.context_from_session(cookie))
        context: ScopeContext = scope.context
        return context

    def publish(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        change: Any,
        service: CatalogPublicationService | None = None,
    ) -> ZoneCatalogVersion:
        self.authz.sessions.clock.advance(1)
        version: ZoneCatalogVersion = self.run(
            (service or self.service).publish_catalog_version(context, zone_id, change, REASON)
        )
        return version

    # --- Lo que quedó escrito ------------------------------------------------------------------

    def versions(self, zone_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT catalog_version, issued_at, superseded_at, changed_fields, envelope::text AS"
            " envelope, payload::text AS payload, single_occupancy, aggregation_window_minutes,"
            " ledger_record_id, role_in_use, issued_by FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 ORDER BY catalog_version",
            zone_id,
        )

    def standards(self, zone_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT standard_id, version, catalog_version, retired_in_catalog_version, family"
            " FROM catalog.declared_standard_version WHERE zone_id = $1"
            " ORDER BY standard_id, version",
            zone_id,
        )

    def cameras(self, zone_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT camera_id, stream_reference FROM catalog.zone_camera WHERE zone_id = $1"
            " ORDER BY camera_id",
            zone_id,
        )

    def records(self, zone_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT record_id, record_type, plant_id, source_key, actor_role_in_use,"
            " ledger.vigia_bytes_to_jsonb(content) AS content FROM ledger.ledger_record"
            " WHERE scope_zone_id = $1 ORDER BY chain_sequence",
            zone_id,
        )

    def events(self, plant_id: uuid.UUID, zone_id: uuid.UUID) -> list[Any]:
        return self.fetch(
            "SELECT event_name, payload::text AS payload FROM shared.outbox_event"
            " WHERE plant_id = $1 AND payload ->> 'zone_id' = $2 ORDER BY created_at, event_id",
            plant_id,
            str(zone_id),
        )

    def written(self, plant_id: uuid.UUID, zone_id: uuid.UUID) -> tuple[int, ...]:
        """Filas, registros y eventos que dejó la zona."""
        return (
            len(self.versions(zone_id)),
            len(self.standards(zone_id)),
            len(self.cameras(zone_id)),
            len(self.records(zone_id)),
            len(self.events(plant_id, zone_id)),
        )


def _camera(n: int, tag: str = "") -> ZoneCamera:
    camera_id = uuid.UUID(int=10_000 + n, version=4)
    return ZoneCamera(
        camera_id=camera_id,
        code=f"CM{tag}-{n}",
        role_in_zone=CameraRoleInZone.PRIMARY if n == 0 else CameraRoleInZone.REDUNDANT,
        declared_min_fps=5.0,
        stream_reference=f"cam{tag.lower()}-{n}",
    )


def _draft(family: str = "coexistence", **changes: Any) -> StandardDraft:
    fields: dict[str, Any] = {
        "family": PredicateFamily(family),
        "title_es": "Coexistencia en la celda",
        "declared_text": "Nadie permanece en la celda mientras la máquina está energizada.",
        "predicate": COEXISTENCE,
    }
    fields.update(changes)
    return StandardDraft(**fields)


def _initial(**changes: Any) -> InitialZoneParameters:
    cameras = (_camera(0), _camera(1))
    fields: dict[str, Any] = {
        "cameras": cameras,
        "required_count": 1,
        "required_camera_ids": (cameras[0].camera_id,),
        "signals": (
            {
                "signal_id": str(uuid.UUID(int=20_000, version=4)),
                "code": "SG-1",
                "role": "energy",
                "asserted_level": "high",
                "source": {"reader": "plc-1", "channel": 1},
                "description_es": "Energía de la prensa",
            },
        ),
        "thresholds": {"review": 0.4, "publication": 0.8},
        "clip_window": {"pre_seconds": 10, "post_seconds": 10},
        "episode": {"grouping_window_ms": 3000, "max_segment_ms": 900000},
    }
    fields.update(changes)
    return InitialZoneParameters(**fields)


def _first(**changes: Any) -> NewStandard:
    return NewStandard(draft=_draft(), initial=_initial(**changes))


def _digest(envelope: Mapping[str, Any]) -> str:
    """Hash canónico de un sobre emitido, tal como se devolvió."""
    return canonical_sha256(dict(envelope))


def _zone(site: Site, index: int = 0) -> tuple[uuid.UUID, uuid.UUID]:
    return site.zones()[index]


@pytest.fixture(scope="module")
def catalogs(postgres_endpoint: PostgresEndpoint) -> Iterator[Catalogs]:
    world = asyncio.run(bootstrapped_world())
    with authz_environment(postgres_endpoint, "catalog_publication") as authz:
        sessions = authz.sessions
        # Las vigencias que compara la base (concesiones) salen del mismo reloj (retro 14).
        (now,) = authz.fetch("SELECT now() AS now")
        sessions.clock.set(now["now"])
        registry = RecordTypeRegistry()
        for definition in (
            *U02_RECORD_TYPES,
            *(d for d in CATALOG_RECORD_TYPES if d.record_type in CATALOG_TYPES),
        ):
            registry.register(definition)
        outbox_catalog = OutboxCatalog()
        register_u02_event_types(outbox_catalog.event_types)
        register_catalog_event_types(outbox_catalog.event_types)

        async def synchronize() -> None:
            system = unit_context(uuid.uuid4(), ActorUnit.U02, kind=ActorKind.SYSTEM)
            async with sessions.database.transaction(system) as transaction:
                await save_record_types(transaction, registry)
                await outbox_catalog.synchronize(SqlOutboxCatalogStore(transaction), sessions.clock)
            registry.seal()

        authz.run(synchronize())
        free_text = FreeTextPolicyRegistry()
        register_u03_free_text_validator(free_text)
        free_text.seal()
        # Retro 15: la espera del candado de la zona no depende del ``lock_timeout`` de 2 s,
        # que no es lo que se prueba; la prueba concurrente decide por eventos.
        database = app_database(
            sessions.migrated, worker_pool_size=8, lock_timeout_ms=TEST_LOCK_TIMEOUT_MS
        )
        writer = EscritorExpediente(
            database=database,
            registry=registry,
            free_text=free_text,
            evidence=EvidenceVerifier(StubEvidenceStorage(), sessions.clock),
            outbox=Outbox(outbox_catalog, sessions.clock),
            clock=sessions.clock,
        )
        stack = Stack(
            authz,
            database,
            writer,
            free_text,
            SignerDouble(world),
            RecordingMarker(),
            PostgresCatalogRepository(database),
        )
        try:
            yield Catalogs(stack, stack.build())
        finally:
            authz.run(database.dispose())


@pytest.fixture
def faults(catalogs: Catalogs) -> Iterator[Catalogs]:
    """Para las pruebas que tumban la firma o la marca: lo deja sano al terminar."""
    try:
        yield catalogs
    finally:
        gate = catalogs.signer.gate
        if gate is not None:
            gate.set()
        catalogs.signer.gate = None
        catalogs.signer.down = False
        catalogs.marker.fail = False


# --- S-PLA-04: la publicación en una transacción ------------------------------------------------


def test_the_first_publication_writes_version_standard_records_cameras_and_one_event(
    catalogs: Catalogs,
) -> None:
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)

    version = catalogs.publish(admin, zone, _first(single_occupancy=True))

    assert version.catalog_version == 1 and version.changed_fields == tuple(CatalogChangedField)
    (row,) = catalogs.versions(zone)
    assert row["superseded_at"] is None and row["single_occupancy"] is True
    assert row["aggregation_window_minutes"] == 60 and row["role_in_use"] == "administrator"
    envelope = json.loads(row["envelope"])
    assert envelope == dict(version.envelope)
    assert json.loads(row["payload"]) == envelope["payload"] == dict(version.payload)
    # El sobre guardado verifica con el verificador de U-01 y su carga es la versión 1.
    payload = verify(envelope, catalogs.signer.keyset(), "catalog", catalogs.signer.world.clock)
    assert payload["version"] == 1 and payload["zone_id"] == str(zone)
    assert payload["organization_id"] == str(site.organization_id)
    assert envelope["payload_canonical_sha256"] == canonical_sha256(payload)
    # NFR-GOB-38: ni la marca ni la ventana viajan en el catálogo firmado.
    for key in PLATFORM_ONLY:
        assert key not in row["envelope"]
    (standard,) = catalogs.standards(zone)
    assert (standard["version"], standard["catalog_version"]) == (1, 1)
    assert standard["retired_in_catalog_version"] is None
    assert [r["stream_reference"] for r in catalogs.cameras(zone)] == ["cam-0", "cam-1"]
    assert "cam-0" not in row["envelope"]  # stream_reference no entra en el ZoneCatalog
    records = catalogs.records(zone)
    assert [r["record_type"] for r in records] == [
        "catalog_version_published",
        "single_occupancy_declared",
    ]
    published, declared = (json.loads(r["content"]) for r in records)
    assert records[0]["record_id"] == row["ledger_record_id"]
    assert records[0]["source_key"] == f"{zone}:1" == records[1]["source_key"]
    assert records[0]["plant_id"] == plant and records[0]["actor_role_in_use"] == "administrator"
    assert published["envelope"] == envelope and published["reason_es"] == REASON
    assert declared["single_occupancy"] is True and declared["aggregation_window_minutes"] == 60
    (event,) = catalogs.events(plant, zone)
    assert event["event_name"] == "catalog_updated"
    assert json.loads(event["payload"]) == {
        "zone_id": str(zone),
        "catalog_version": 1,
        "changed_fields": [f.value for f in CatalogChangedField],
    }
    assert catalogs.marker.calls[-1] == (zone, None, 1, tuple(CatalogChangedField))


def test_a_sequence_of_changes_numbers_without_gaps_and_closes_exactly_the_previous(
    catalogs: Catalogs,
) -> None:
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    first = catalogs.publish(admin, zone, _first())
    (born,) = catalogs.standards(zone)
    standard_id = uuid.UUID(str(born["standard_id"]))
    digests = {1: _digest(first.envelope)}

    second = catalogs.publish(
        admin,
        zone,
        NewStandard(
            draft=_draft(
                "dwell", predicate={"all_of": [ENERGY_ON, PRESENCE], "min_duration_ms": 5000}
            )
        ),
    )
    third = catalogs.publish(
        admin, zone, NewStandardVersion(standard_id=standard_id, title_es="Celda de soldadura")
    )
    fourth = catalogs.publish(admin, zone, RetireStandard(standard_id=standard_id))
    fifth = catalogs.publish(admin, zone, SetCameras(cameras=(_camera(0), _camera(1), _camera(2))))
    sixth = catalogs.publish(
        admin, zone, SetSingleOccupancy(single_occupancy=True, aggregation_window_minutes=120)
    )
    for version in (second, third, fourth, fifth, sixth):
        digests[version.catalog_version] = _digest(version.envelope)

    rows = catalogs.versions(zone)
    assert [r["catalog_version"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert [r["superseded_at"] for r in rows[:-1]] == [r["issued_at"] for r in rows[1:]]
    assert rows[-1]["superseded_at"] is None
    # El hash del sobre de cada versión emitida no cambia tras las publicaciones posteriores.
    assert {r["catalog_version"]: canonical_sha256(json.loads(r["envelope"])) for r in rows} == (
        digests
    )
    assert [r["changed_fields"] for r in rows[1:]] == [
        ["standards"],
        ["standards"],
        ["standards"],
        ["cameras"],
        ["single_occupancy"],
    ]
    standards = {
        (uuid.UUID(str(r["standard_id"])), r["version"]): r for r in catalogs.standards(zone)
    }
    assert standards[(standard_id, 1)]["retired_in_catalog_version"] == 3
    assert standards[(standard_id, 2)]["catalog_version"] == 3
    assert standards[(standard_id, 2)]["retired_in_catalog_version"] == 4
    assert sum(1 for r in standards.values() if r["retired_in_catalog_version"] is None) == 1
    records = catalogs.records(zone)
    retired = [
        json.loads(r["content"]) for r in records if r["record_type"] == "catalog_standard_retired"
    ]
    assert retired == [
        {
            "source_key": f"{standard_id}:2",
            "zone_id": str(zone),
            "standard_id": str(standard_id),
            "version": 2,
            "retired_in_catalog_version": 4,
            "reason_es": REASON,
        }
    ]
    events = catalogs.events(plant, zone)
    assert [json.loads(e["payload"])["catalog_version"] for e in events] == [1, 2, 3, 4, 5, 6]
    assert len([c for c in catalogs.cameras(zone)]) == 3
    assert rows[-1]["single_occupancy"] is True and rows[-1]["aggregation_window_minutes"] == 120
    # PR-GOB-06 sobre la historia guardada: la versión 1 rige hasta que nace la 2.
    history = catalogs.run(catalogs.service.standard_history(admin, zone))
    v1 = next(v for v in history if v.standard_id == standard_id and v.version == 1)
    v2 = next(v for v in history if v.standard_id == standard_id and v.version == 2)
    assert v1.effective_until == v2.effective_from == third.issued_at
    assert v2.effective_until == fourth.issued_at
    assert standard_valid_at(history, standard_id, third.issued_at) is v2
    assert (
        standard_valid_at(history, standard_id, third.issued_at - timedelta(microseconds=1)) is v1
    )
    assert standard_valid_at(history, standard_id, fourth.issued_at) is None


@given(scenario=catalog_versions(max_changes=4))
def test_pr_gob_05_on_postgres_versions_grow_by_one_and_issued_envelopes_never_change(
    catalogs: Catalogs, scenario: CatalogScenario
) -> None:
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    issued: dict[int, str] = {}
    state: CatalogState | None = None
    for intent in (None, *scenario.intents):
        change = scenario.first if intent is None else resolve(intent, state)  # type: ignore[arg-type]
        try:
            version = catalogs.publish(admin, zone, change)
        except (CatalogRejected, CatalogRequestInvalid):
            continue
        assert version.catalog_version == len(issued) + 1
        issued[version.catalog_version] = _digest(version.envelope)
        state = CatalogState(
            catalog=version.payload,
            single_occupancy=version.single_occupancy,
            aggregation_window_minutes=version.aggregation_window_minutes,
        )
    rows = catalogs.versions(zone)
    assert [r["catalog_version"] for r in rows] == list(range(1, len(issued) + 1))
    assert [r["superseded_at"] is None for r in rows].count(True) == 1
    assert {r["catalog_version"]: canonical_sha256(json.loads(r["envelope"])) for r in rows} == (
        issued
    )
    assert len(catalogs.events(plant, zone)) == len(issued)


# --- Concurrencia ------------------------------------------------------------------------------


async def _advisory_waiters(admin: Any) -> int:
    value: int = await admin.fetchval(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    return value


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_two_simultaneous_publications_leave_n_plus_1_and_n_plus_2(
    faults: Catalogs, attempt: int
) -> None:
    catalogs = faults
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.publish(admin, zone, _first())
    gate = threading.Event()
    catalogs.signer.gate = gate
    calls_before = catalogs.signer.calls
    database_admin = catalogs.authz.sessions.admin
    catalogs.authz.sessions.clock.advance(1)

    async def race() -> list[Any]:
        tasks = [
            asyncio.create_task(
                catalogs.service.publish_catalog_version(
                    admin, zone, SetSingleOccupancy(single_occupancy=flag), REASON
                )
            )
            for flag in (True, False)
        ]
        # La primera retiene la firma con el candado tomado; la segunda espera el candado (con
        # el candado) o llega también a la firma (sin él). Se suelta al ver cualquiera de las dos.
        async with asyncio.timeout(WAIT_SECONDS):
            while not (
                await _advisory_waiters(database_admin) >= 1
                or catalogs.signer.calls - calls_before >= 2
            ):
                await asyncio.sleep(POLL_SECONDS)
        gate.set()
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    results = catalogs.run(race())

    assert all(isinstance(r, ZoneCatalogVersion) for r in results), results
    assert sorted(r.catalog_version for r in results) == [2, 3]
    rows = catalogs.versions(zone)
    assert [r["catalog_version"] for r in rows] == [1, 2, 3]
    assert rows[0]["superseded_at"] == rows[1]["issued_at"]
    assert rows[1]["superseded_at"] == rows[2]["issued_at"]
    assert rows[2]["superseded_at"] is None
    sources = [
        r["source_key"]
        for r in catalogs.records(zone)
        if r["record_type"] == "catalog_version_published"
    ]
    assert sources == [f"{zone}:1", f"{zone}:2", f"{zone}:3"]
    assert len(catalogs.events(plant, zone)) == 3


def test_a_lost_race_on_the_key_is_transient_and_never_repeats_a_number(
    catalogs: Catalogs,
) -> None:
    # Respaldo del candado: si una publicación compone n+1 cuando otra ya lo confirmó, la clave
    # primaria (y la source_key) la rechazan como transitoria, sin dejar nada.
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.publish(admin, zone, _first())

    class StaleRepository(PostgresCatalogRepository):
        """Lee la versión vigente como estaba antes de la última confirmación."""

        stale: ZoneCatalogVersion | None = None

        async def lock_zone(self, transaction: Transaction, zone_id: uuid.UUID) -> None:
            return None

        async def current(
            self, transaction: Transaction, zone_id: uuid.UUID
        ) -> ZoneCatalogVersion | None:
            found = await super().current(transaction, zone_id)
            if StaleRepository.stale is None:
                StaleRepository.stale = found
            return StaleRepository.stale

    stale_service = catalogs.build(repository=StaleRepository(catalogs.database))
    catalogs.publish(admin, zone, SetSingleOccupancy(single_occupancy=True), stale_service)
    before = catalogs.written(plant, zone)

    with pytest.raises(CatalogPublicationUnavailable):
        catalogs.publish(admin, zone, SetSingleOccupancy(single_occupancy=False), stale_service)

    assert catalogs.written(plant, zone) == before
    assert [r["catalog_version"] for r in catalogs.versions(zone)] == [1, 2]


# --- Fallo cerrado -----------------------------------------------------------------------------


def test_with_signing_down_nothing_is_written_and_the_next_one_takes_the_next_number(
    faults: Catalogs,
) -> None:
    catalogs = faults
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.signer.down = True

    with pytest.raises(CatalogPublicationUnavailable):
        catalogs.publish(admin, zone, _first())

    assert catalogs.written(plant, zone) == (0, 0, 0, 0, 0)
    catalogs.signer.down = False
    assert catalogs.publish(admin, zone, _first()).catalog_version == 1
    before = catalogs.written(plant, zone)
    catalogs.signer.down = True

    with pytest.raises(CatalogPublicationUnavailable):
        catalogs.publish(admin, zone, NewStandard(draft=_draft()))

    assert catalogs.written(plant, zone) == before
    catalogs.signer.down = False
    assert catalogs.publish(admin, zone, NewStandard(draft=_draft())).catalog_version == 2
    assert [r["catalog_version"] for r in catalogs.versions(zone)] == [1, 2]


def test_a_signature_that_exceeds_its_timeout_writes_nothing(faults: Catalogs) -> None:
    catalogs = faults
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.publish(admin, zone, _first())
    before = catalogs.written(plant, zone)
    # Firma retenida hasta que la prueba la suelta: el tope (2 s) se agota sin carrera.
    gate = threading.Event()
    catalogs.signer.gate = gate
    service = catalogs.build(sign_timeout_seconds=2.0)

    with pytest.raises(CatalogPublicationUnavailable):
        catalogs.publish(admin, zone, NewStandard(draft=_draft()), service)

    gate.set()
    catalogs.signer.gate = None
    assert catalogs.written(plant, zone) == before
    assert catalogs.publish(admin, zone, NewStandard(draft=_draft())).catalog_version == 2


def test_a_failing_regression_marker_rolls_the_whole_publication_back(faults: Catalogs) -> None:
    catalogs = faults
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.marker.fail = True

    with pytest.raises(RuntimeError):
        catalogs.publish(admin, zone, _first())

    assert catalogs.written(plant, zone) == (0, 0, 0, 0, 0)


# --- Validaciones previas: ninguna escribe nada --------------------------------------------------


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        (
            NewStandard(
                draft=_draft(
                    predicate={"all_of": [{"presence": False}, ENERGY_ON], "min_duration_ms": 0}
                ),
                initial=_initial(),
            ),
            CatalogDetailCode.PREDICATE_INVALID,
        ),
        (
            NewStandard(draft=_draft("dwell"), initial=_initial()),  # plantilla de otra familia
            CatalogDetailCode.PREDICATE_INVALID,
        ),
        (NewStandard(draft=_draft()), CatalogDetailCode.ZONE_WITHOUT_CAMERAS),
        (
            NewStandard(draft=_draft(), initial=_initial(required_count=3)),
            CatalogDetailCode.UNSATISFIABLE_COVERAGE,
        ),
        (
            NewStandard(draft=_draft(title_es="<b>Celda</b>"), initial=_initial()),
            CatalogDetailCode.FREE_TEXT_REJECTED,
        ),
        (
            NewStandard(draft=_draft(declared_text="x" * 4001), initial=_initial()),
            CatalogDetailCode.FREE_TEXT_REJECTED,
        ),
        (
            NewStandard(
                draft=_draft(declared_text="Hubo sabotaje en la celda"), initial=_initial()
            ),
            CatalogDetailCode.FREE_TEXT_REJECTED,
        ),
        (
            NewStandard(draft=_draft(title_es="t" * 121), initial=_initial()),
            CatalogDetailCode.FREE_TEXT_REJECTED,
        ),
    ],
    ids=[
        "presence-false",
        "other-template",
        "no-cameras",
        "unsatisfiable",
        "markup",
        "text-4001",
        "intent",
        "title-121",
    ],
)
def test_a_rejected_first_publication_writes_nothing(
    catalogs: Catalogs, change: Any, detail: CatalogDetailCode
) -> None:
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    calls = catalogs.signer.calls

    with pytest.raises(CatalogRejected) as raised:
        catalogs.publish(admin, zone, change)

    assert raised.value.detail_code is detail
    assert catalogs.written(plant, zone) == (0, 0, 0, 0, 0)
    assert catalogs.signer.calls == calls  # nada se firma antes de validar


def test_edges_of_texts_are_accepted(catalogs: Catalogs) -> None:
    site = catalogs.site()
    _, zone = _zone(site)
    admin = catalogs.member(site)
    change = NewStandard(
        draft=_draft(title_es="t" * 120, declared_text="d" * 4000), initial=_initial()
    )
    catalogs.authz.sessions.clock.advance(1)
    version = catalogs.run(catalogs.service.publish_catalog_version(admin, zone, change, "r" * 500))
    assert version.catalog_version == 1 and version.reason_es == "r" * 500
    for reason in ("r" * 9, "r" * 501, "Motivo <i>con</i> marcado"):
        with pytest.raises(CatalogRejected) as raised:
            catalogs.run(
                catalogs.service.publish_catalog_version(
                    admin, zone, NewStandard(draft=_draft()), reason
                )
            )
        assert raised.value.detail_code is CatalogDetailCode.FREE_TEXT_REJECTED
    assert len(catalogs.versions(zone)) == 1


def test_a_family_not_admitted_in_the_plant_is_rejected(catalogs: Catalogs) -> None:
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    # Una planta sin admisiones (la admisión no se hereda entre plantas, BR-GOB-16).
    bare = catalogs.authz.add_site(plants=1, zones_per_plant=1)
    bare_plant, bare_zone = _zone(bare)
    bare_admin = catalogs.member(bare)

    with pytest.raises(CatalogRejected) as raised:
        catalogs.publish(bare_admin, bare_zone, _first())

    assert raised.value.detail_code is CatalogDetailCode.FAMILY_NOT_ADMITTED
    assert catalogs.written(bare_plant, bare_zone) == (0, 0, 0, 0, 0)
    assert catalogs.publish(admin, zone, _first()).catalog_version == 1
    assert catalogs.written(plant, zone)[0] == 1


def test_retiring_the_last_standard_and_unknown_standards_write_nothing(catalogs: Catalogs) -> None:
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.publish(admin, zone, _first())
    (standard,) = catalogs.standards(zone)
    before = catalogs.written(plant, zone)

    with pytest.raises(CatalogRejected) as raised:
        catalogs.publish(
            admin, zone, RetireStandard(standard_id=uuid.UUID(str(standard["standard_id"])))
        )
    assert raised.value.detail_code is CatalogDetailCode.LAST_STANDARD_IN_ZONE
    with pytest.raises(ResourceNotFound):
        catalogs.publish(admin, zone, RetireStandard(standard_id=uuid.uuid4()))
    with pytest.raises(CatalogRejected) as unsatisfiable:
        catalogs.publish(
            admin,
            zone,
            SetMinimumCoverage(required_count=3, required_camera_ids=(_camera(0).camera_id,)),
        )
    assert unsatisfiable.value.detail_code is CatalogDetailCode.UNSATISFIABLE_COVERAGE

    assert catalogs.written(plant, zone) == before


def test_a_clock_that_goes_back_never_issues_before_the_current_version(
    catalogs: Catalogs,
) -> None:
    site = catalogs.site()
    _, zone = _zone(site)
    admin = catalogs.member(site)
    first = catalogs.publish(admin, zone, _first())
    clock = catalogs.authz.sessions.clock
    clock.set(first.issued_at - timedelta(minutes=10))
    try:
        second = catalogs.run(
            catalogs.service.publish_catalog_version(
                admin, zone, NewStandard(draft=_draft()), REASON
            )
        )
    finally:
        clock.set(first.issued_at + timedelta(seconds=1))

    assert second.catalog_version == 2 and second.issued_at == first.issued_at
    rows = catalogs.versions(zone)
    assert rows[0]["superseded_at"] == rows[1]["issued_at"] == first.issued_at


# --- NFR-GOB-38 y NFR-GOB-10 --------------------------------------------------------------------


def test_nfr_gob_38_the_flag_lives_in_the_row_never_in_the_envelope_or_the_event(
    catalogs: Catalogs,
) -> None:
    site = catalogs.site()
    plant, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.publish(admin, zone, _first())

    catalogs.publish(
        admin, zone, SetSingleOccupancy(single_occupancy=True, aggregation_window_minutes=480)
    )

    rows = catalogs.versions(zone)
    assert (rows[-1]["single_occupancy"], rows[-1]["aggregation_window_minutes"]) == (True, 480)
    assert json.loads(rows[-1]["payload"]) | {"version": 1, "issued_at": None} == json.loads(
        rows[0]["payload"]
    ) | {"version": 1, "issued_at": None}
    for row in rows:
        envelope = json.loads(row["envelope"])
        for key in PLATFORM_ONLY:
            assert key not in json.dumps(envelope)
    for event in catalogs.events(plant, zone):
        payload = json.loads(event["payload"])
        assert set(payload) == {"zone_id", "catalog_version", "changed_fields"}
        assert "aggregation_window_minutes" not in event["payload"]
        assert "480" not in event["payload"]


def test_nfr_gob_10_reading_the_stored_envelope_never_signs(faults: Catalogs) -> None:
    catalogs = faults
    site = catalogs.site()
    _, zone = _zone(site)
    admin = catalogs.member(site)
    reader = catalogs.member(site, Role.COORDINATOR_SST)
    first = catalogs.publish(admin, zone, _first())
    second = catalogs.publish(admin, zone, NewStandard(draft=_draft()))
    calls = catalogs.signer.calls
    catalogs.signer.down = True  # ni siquiera hace falta la firma para leer

    for _ in range(50):
        current = catalogs.run(catalogs.service.stored_envelope(reader, zone))
        old = catalogs.run(catalogs.service.stored_envelope(reader, zone, 1))
        assert current == dict(second.envelope) and old == dict(first.envelope)

    assert catalogs.signer.calls == calls
    with pytest.raises(ResourceNotFound):
        catalogs.run(catalogs.service.stored_envelope(reader, zone, 3))


# --- Guardas de alcance --------------------------------------------------------------------------


def test_another_organization_or_an_out_of_scope_plant_or_zone_answer_not_found(
    catalogs: Catalogs,
) -> None:
    site = catalogs.site(plants=2, zones=2)
    other = catalogs.site()
    (plant_a, zone_a1), (_, zone_a2), (_, zone_b1), _ = site.zones()
    admin = catalogs.member(site)
    catalogs.publish(admin, zone_a1, _first())
    catalogs.publish(admin, zone_b1, _first())
    foreign = catalogs.member(other)
    plant_admin = catalogs.member(site, Role.ADMINISTRATOR, ScopeLevel.PLANT, plant_a)
    zone_reader = catalogs.member(site, Role.COORDINATOR_SST, ScopeLevel.ZONE, zone_a2)
    before = {z: catalogs.written(p, z) for p, z in site.zones()}

    for context, zone in (
        (foreign, zone_a1),  # otra organización
        (plant_admin, zone_b1),  # otra planta
        (admin, uuid.uuid4()),  # inexistente
    ):
        with pytest.raises(ResourceNotFound):
            catalogs.publish(context, zone, NewStandard(draft=_draft()))
        with pytest.raises(ResourceNotFound):
            catalogs.run(catalogs.service.stored_envelope(context, zone))
    with pytest.raises(ResourceNotFound):  # zona fuera del alcance de la sesión
        catalogs.run(catalogs.service.stored_envelope(zone_reader, zone_a1))
    with pytest.raises(ResourceNotFound):  # leer no da permiso de publicar
        catalogs.publish(zone_reader, zone_a2, _first())

    assert {z: catalogs.written(p, z) for p, z in site.zones()} == before
    assert catalogs.publish(plant_admin, zone_a1, NewStandard(draft=_draft())).catalog_version == 2


def test_the_statements_filter_the_zone_and_the_organization(catalogs: Catalogs) -> None:
    # Dos zonas de la misma planta con catálogo: la versión 1 de B (vigente) se escribe antes que
    # las de A, así que es la primera fila de la organización. Sin el filtro de zona en las
    # sentencias, leer la zona A devolvería la de B.
    site = catalogs.site(plants=1, zones=2)
    other = catalogs.site()
    (_, zone_a), (_, zone_b) = site.zones()
    admin = catalogs.member(site)
    catalogs.publish(admin, zone_b, _first())
    catalogs.publish(admin, zone_a, _first())
    catalogs.publish(admin, zone_a, NewStandard(draft=_draft()))
    repository = catalogs.repository
    service_read = catalogs.run(catalogs.service.stored_envelope(admin, zone_a))
    assert service_read["payload"]["zone_id"] == str(zone_a)
    assert service_read["payload"]["version"] == 2
    own = unit_context(site.organization_id, ActorUnit.U03)
    foreign = unit_context(other.organization_id, ActorUnit.U03)

    async def read(context: ScopeContext, zone: uuid.UUID, version: int | None) -> Any:
        async with catalogs.database.transaction(context) as transaction:
            envelope = await repository.envelope(transaction, zone, version)
            current = await repository.current(transaction, zone)
            return envelope, current

    for version in (None, 1):
        envelope, current = catalogs.run(read(own, zone_a, version))
        assert envelope["payload"]["zone_id"] == str(zone_a)
        assert envelope["payload"]["version"] == (2 if version is None else 1)
        assert current.zone_id == zone_a and current.catalog_version == 2
    assert catalogs.run(read(own, zone_b, None))[1].catalog_version == 1
    assert catalogs.run(read(foreign, zone_a, None)) == (None, None)
    assert catalogs.run(repository.version(own, zone_a)).zone_id == zone_a
    history = catalogs.run(repository.standard_history(own, zone_a))
    assert {v.zone_id for v in history} == {zone_a}


def test_under_concession_the_installer_reads_audited_and_never_publishes(
    catalogs: Catalogs,
) -> None:
    authz = catalogs.authz
    site = catalogs.site(plants=2, zones=1)
    (plant_a, zone_a), (_, zone_b) = site.zones()
    admin = catalogs.member(site)
    first = catalogs.publish(admin, zone_a, _first())
    catalogs.publish(admin, zone_b, _first())
    installer = authz.add_provider_user()
    concession = authz.add_concession(
        site.organization_id,
        installer,
        level=ScopeLevel.PLANT,
        scope_id=plant_a,
        granted_at=authz.now() - timedelta(hours=1),
    )
    cookie = authz.open_session(authz.provider_organization_id, installer)
    scope = catalogs.run(authz.contexts.context_from_session(cookie, concession_id=concession))
    context: ScopeContext = scope.context

    envelope = catalogs.run(catalogs.service.stored_envelope(context, zone_a))

    assert envelope == dict(first.envelope)
    with pytest.raises(ResourceNotFound):  # la otra planta no está concedida
        catalogs.run(catalogs.service.stored_envelope(context, zone_b))
    with pytest.raises(ResourceNotFound):  # catalog.manage no está en la columna del proveedor
        catalogs.publish(context, zone_a, NewStandard(draft=_draft()))
    audited = catalogs.fetch(
        "SELECT scope_plant_id, scope_zone_id, result_count FROM shared.audit_entry"
        " WHERE organization_id = $1 AND actor_concession_id = $2 AND operation = 'catalog_read'",
        site.organization_id,
        concession,
    )
    assert [(r["scope_plant_id"], r["scope_zone_id"], r["result_count"]) for r in audited] == [
        (plant_a, zone_a, 1)
    ]
    assert [r["catalog_version"] for r in catalogs.versions(zone_a)] == [1]


def test_the_organization_filter_holds_even_without_rls(catalogs: Catalogs) -> None:
    # Como superusuario la RLS no aplica: lo que separa las organizaciones es el filtro de cada
    # sentencia (defensa en profundidad; sin él, esta prueba falla).
    site = catalogs.site()
    other = catalogs.site()
    _, zone = _zone(site)
    catalogs.publish(catalogs.member(site), zone, _first())
    migrated = catalogs.authz.sessions.migrated
    database = app_database(migrated, url=migrated.as_role().sqlalchemy_url, worker_pool_size=1)
    repository = PostgresCatalogRepository(database)
    foreign = unit_context(other.organization_id, ActorUnit.U03)

    async def read() -> Any:
        async with database.transaction(foreign) as transaction:
            return await repository.envelope(transaction, zone), await repository.current(
                transaction, zone
            )

    try:
        assert catalogs.run(read()) == (None, None)
        assert catalogs.run(repository.zone(foreign, zone)) is None
        assert catalogs.run(repository.standard_history(foreign, zone)) == ()
    finally:
        catalogs.run(database.dispose())


# --- Solo anexar para vigia_app ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "sqlstate"),
    [
        ("UPDATE catalog.zone_catalog_version SET payload = '{}' WHERE zone_id = :zone", "42501"),
        ("UPDATE catalog.zone_catalog_version SET envelope = '{}' WHERE zone_id = :zone", "42501"),
        (
            "UPDATE catalog.zone_catalog_version SET catalog_version = 9 WHERE zone_id = :zone",
            "42501",
        ),
        ("DELETE FROM catalog.zone_catalog_version WHERE zone_id = :zone", "42501"),
        (
            "UPDATE catalog.zone_catalog_version SET superseded_at = superseded_at"
            " + interval '1 hour' WHERE zone_id = :zone AND superseded_at IS NOT NULL",
            "23001",
        ),
        (
            "UPDATE catalog.declared_standard_version SET predicate = '{}' WHERE zone_id = :zone",
            "42501",
        ),
        ("DELETE FROM catalog.declared_standard_version WHERE zone_id = :zone", "42501"),
        (
            "UPDATE catalog.declared_standard_version SET retired_in_catalog_version = 99"
            " WHERE zone_id = :zone AND retired_in_catalog_version IS NOT NULL",
            "23001",
        ),
    ],
    ids=[
        "payload",
        "envelope",
        "number",
        "delete",
        "superseded-twice",
        "standard-predicate",
        "standard-delete",
        "standard-retired-twice",
    ],
)
def test_vigia_app_cannot_rewrite_an_issued_version(
    catalogs: Catalogs, statement: str, sqlstate: str
) -> None:
    site = catalogs.site()
    _, zone = _zone(site)
    admin = catalogs.member(site)
    catalogs.publish(admin, zone, _first())
    standard_id = uuid.UUID(str(catalogs.standards(zone)[0]["standard_id"]))
    catalogs.publish(admin, zone, NewStandardVersion(standard_id=standard_id, title_es="Otro"))
    before = (catalogs.versions(zone), catalogs.standards(zone))
    context = unit_context(site.organization_id, ActorUnit.U03)

    async def attempt() -> None:
        async with catalogs.database.transaction(context) as transaction:
            await transaction.execute(text(statement), {"zone": zone})

    with pytest.raises(Exception) as raised:
        catalogs.run(attempt())

    assert _sqlstate(raised.value) == sqlstate, raised.value
    assert (catalogs.versions(zone), catalogs.standards(zone)) == before


def test_vigia_app_only_closes_the_current_version_from_null_to_a_value(
    catalogs: Catalogs,
) -> None:
    site = catalogs.site()
    _, zone = _zone(site)
    catalogs.publish(catalogs.member(site), zone, _first())
    context = unit_context(site.organization_id, ActorUnit.U03)

    async def close() -> int:
        async with catalogs.database.transaction(context) as transaction:
            result = await transaction.execute(
                text(
                    "UPDATE catalog.zone_catalog_version SET superseded_at = issued_at"
                    " WHERE zone_id = :zone AND superseded_at IS NULL"
                ),
                {"zone": zone},
            )
            count: int = result.rowcount  # type: ignore[attr-defined]
            return count

    assert catalogs.run(close()) == 1
    (row,) = catalogs.versions(zone)
    assert row["superseded_at"] == row["issued_at"]


def _sqlstate(error: BaseException) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "orig", None)):
            state = getattr(candidate, "sqlstate", None)
            if isinstance(state, str):
                return state
        current = current.__cause__ or current.__context__
    return None
