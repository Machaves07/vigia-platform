"""PR-GOB-14 (TASK-209 y TASK-216; BR-GOB-51 a 56; LC-GOB-09).

«Un cambio en una fila afectada implica regresión ``pending``; el cierre de un
``regression_rerun`` que cubre las filas afectadas la devuelve a ``current``»
(business-logic-model §6).

- **Cierre de la reejecución** (``RegressionRerun``, TASK-216): la máquina de marca de abajo más
  las reglas «abrir reejecución», «registrar pases» y «cerrar», con los servicios reales
  (``RegressionRerunService`` y ``CloseRecordService``; lo que el cierre exige de otras tareas, por
  SQL). Invariantes: el cierre de una reejecución que cubre las filas afectadas deja ``current``
  (un ``walk_test_regression_cleared`` por cierre así); una marca nueva durante la reejecución
  deja ``pending``; la reejecución abre exactamente las filas pendientes (o la matriz completa);
  la zona sigue ``productive`` en todo momento.

- **Máquina de estados sobre la base** (``RegressionMarking``, perfil ``ci`` con su semilla fija y
  la de la sesión, ``_seeds_for_profile``): una zona ``productive`` con su versión 1 de
  ``catalog_versions``; las reglas publican cambios de cada tipo (``catalog_changes``), cambian
  solo el texto de un estándar, marcan la zona unipersonal, informan otro ``model_version`` y
  cambian solo ``software_version``, siempre por los servicios reales
  (``CatalogPublicationService`` con el ``RegressionService`` real). Un modelo independiente lleva
  las filas pendientes como ``(standard_id, postura)`` o ``all``, calculadas con un oráculo propio
  (no con ``publication_rows``). Invariantes tras cada paso: la fila de la base es la del modelo
  (``pending`` con esas filas, o ``all``; ``current`` si nada marcó), un registro
  ``walk_test_regression_marked`` por marca, y ``resulting_mode`` sigue ``productive``.
- **Propiedades puras sobre ``walk_test_matrices``**: un cambio solo de texto no marca y lleva
  cada fila pendiente a la versión nueva de su estándar; la unión nunca pierde una fila; las filas
  de una marca son siempre de la matriz nueva.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar, Final

import pytest
from hypothesis import given, settings
from hypothesis import seed as hypothesis_seed
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from tests.catalog_routes_support import (
    TEST_SIGN_TIMEOUT_SECONDS,
    CatalogRoutes,
    catalog_routes_world,
)
from tests.close_record_support import ClipStore
from tests.conftest import _seeds_for_profile
from tests.integration.conftest import PostgresEndpoint
from tests.properties.gob.strategies.catalog import (
    TITLES,
    CatalogScenario,
    Intent,
    catalog_changes,
    catalog_versions,
    resolve,
)
from tests.properties.gob.strategies.walk_test import (
    DECLARED_BY,
    REASON,
    T0,
    WalkTestMatrix,
    state_of,
    walk_test_matrices,
)
from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.commissioning_record_repository import (
    PostgresCommissioningRecordRepository,
)
from vigia_platform.catalog.adapters.postgres.gate_repository import PostgresGateRepository
from vigia_platform.catalog.adapters.postgres.occlusion_repository import (
    PostgresOcclusionRepository,
)
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.close_record import CloseRecordService, CloseRequest
from vigia_platform.catalog.application.gates import GateService
from vigia_platform.catalog.application.occlusion import OcclusionService
from vigia_platform.catalog.application.publication import CatalogRequestInvalid
from vigia_platform.catalog.application.regression import requires_model_regression
from vigia_platform.catalog.application.regression_rerun import RegressionRerunService
from vigia_platform.catalog.application.walk_test import WalkTestConflict, WalkTestService
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import (
    AGGREGATION_WINDOW_MAX,
    AGGREGATION_WINDOW_MIN,
    CatalogChange,
    CatalogRuleViolated,
    CatalogState,
    NewStandard,
    NewStandardVersion,
    RetireStandard,
    SetCameras,
    SetMinimumCoverage,
    SetSignals,
    SetSingleOccupancy,
    SetThresholds,
    SetWindows,
    ZoneCatalogVersion,
    plan_publication,
)
from vigia_platform.catalog.domain.commissioning_record import CommissioningRecord
from vigia_platform.catalog.domain.enums import CatalogChangedField, RegressionCause, WalkTestKind
from vigia_platform.catalog.domain.matrix import POSTURES, derive_matrix
from vigia_platform.catalog.domain.regression import (
    ALL_ROWS,
    RegressionMark,
    WalkTestRegression,
    carried_forward,
    merged,
    publication_rows,
)
from vigia_platform.catalog.domain.walk_test import WalkTestSession
from vigia_platform.catalog.record_types import CATALOG_RECORD_TYPES
from vigia_platform.fleet.adapters.postgres.commissioning_queries import (
    PostgresCommissioningQueries,
)
from vigia_platform.fleet.adapters.s3.clip_storage import ClipObjectStore
from vigia_platform.identity.application.common import IdentityDependencies
from vigia_platform.identity.application.hierarchy import HierarchyService
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.shared.context import ScopeContext

Key = tuple[str, str]
"""``(standard_id, postura)``: la fila sin la versión del estándar."""
Keys = frozenset[Key] | str
MODELS: Final = ("detector-v1", "detector-v2", "detector-v3")
SOFTWARE: Final = ("1.0.0", "1.1.0", "2.0.0")
STEPS: Final = 8
ZONE_WIDE: Final = (SetCameras, SetMinimumCoverage, SetSignals, SetThresholds, SetWindows)


# --- Oráculo (independiente de ``publication_rows``) -------------------------------------------


def _predicate(standard: dict[str, Any]) -> tuple[tuple[str, ...], int]:
    predicate = standard["predicate"]
    conditions = sorted(json.dumps(c, sort_keys=True) for c in predicate["all_of"])
    return tuple(conditions), int(predicate["min_duration_ms"])


def _standard_keys(standard_id: str) -> frozenset[Key]:
    return frozenset((standard_id, posture.value) for posture in POSTURES)


def expected_keys(
    change: CatalogChange, before: dict[str, Any], after: dict[str, Any]
) -> Keys | None:
    """Lo que la versión debe marcar según la tabla de TASK-209 (``None``: no marca)."""
    if isinstance(change, (*ZONE_WIDE, RetireStandard)):
        return ALL_ROWS
    if isinstance(change, SetSingleOccupancy):
        return None
    old = {s["standard_id"]: s for s in before["standards"]}
    new = {s["standard_id"]: s for s in after["standards"]}
    if isinstance(change, NewStandard):
        (added,) = set(new) - set(old)
        return _standard_keys(added)
    assert isinstance(change, NewStandardVersion)
    standard_id = str(change.standard_id)
    if _predicate(old[standard_id]) == _predicate(new[standard_id]):
        return None
    return _standard_keys(standard_id)


def _union(current: Keys | None, added: Keys) -> Keys:
    if current == ALL_ROWS or added == ALL_ROWS or current is None:
        return ALL_ROWS if ALL_ROWS in (current, added) else added
    assert isinstance(current, frozenset) and isinstance(added, frozenset)
    return current | added


def stored_keys(stored: Any, catalog: dict[str, Any]) -> Keys:
    """Las filas guardadas como ``(standard_id, postura)`` de la matriz del catálogo vigente."""
    if stored == ALL_ROWS:
        return ALL_ROWS
    matrix = {
        str(row.row_id): (str(row.standard_id), row.posture.value) for row in derive_matrix(catalog)
    }
    missing = [row for row in stored if row not in matrix]
    assert not missing, f"filas que no son de la matriz vigente: {missing}"
    return frozenset(matrix[row] for row in stored)


# --- Máquina de estados sobre la base ------------------------------------------------------------


@dataclass
class Model:
    keys: Keys | None = None
    """``None``: ``current``; si no, las filas pendientes."""
    marks: int = 0
    inventory: tuple[str, str] = (MODELS[0], SOFTWARE[0])
    """``(model_version, software_version)`` que el nodo informó por última vez."""
    model_version: str | None = None
    first_marked_at: Any = None
    versions: list[int] = field(default_factory=list)


class RegressionMarking(RuleBasedStateMachine):
    world: ClassVar[CatalogRoutes]

    def __init__(self) -> None:
        super().__init__()
        world = self.world
        self.site = world.site()
        ((plant, zone),) = self.site.zones()
        world.productive(self.site, plant, zone)
        self.zone = zone
        self.admin: ScopeContext = world.context(world.member(self.site))
        self.state: CatalogState | None = None
        self.model = Model()

    # --- Publicación ---------------------------------------------------------------------------

    def _publish(self, change: CatalogChange) -> ZoneCatalogVersion | None:
        self.world.tick()
        try:
            version: ZoneCatalogVersion = self.world.run(
                self.world.publication.publish_catalog_version(
                    self.admin, self.zone, change, REASON
                )
            )
        except (CatalogRejected, CatalogRequestInvalid):
            return None  # rechazada: nada escrito, la regresión no cambia
        self.state = CatalogState(
            catalog=version.payload,
            single_occupancy=version.single_occupancy,
            aggregation_window_minutes=version.aggregation_window_minutes,
        )
        self.model.versions.append(version.catalog_version)
        return version

    def _published(self, change: CatalogChange) -> None:
        assert self.state is not None
        before = dict(self.state.catalog)
        version = self._publish(change)
        if version is None:
            return
        added = expected_keys(change, before, dict(version.payload))
        if added is None:
            return
        if self.model.keys is None:
            self.model.first_marked_at = version.issued_at
        self.model.keys = _union(self.model.keys, added)
        self.model.marks += 1

    @initialize(scenario=catalog_versions(max_changes=0))
    def first_version(self, scenario: CatalogScenario) -> None:
        version = self._publish(scenario.first)
        assert version is not None and version.catalog_version == 1

    @rule(intent=catalog_changes())
    def publish_a_change(self, intent: Intent) -> None:
        assert self.state is not None
        self._published(resolve(intent, self.state))

    @rule(index=st.integers(0, 64), title=st.sampled_from(TITLES))
    def change_only_the_text(self, index: int, title: str) -> None:
        assert self.state is not None
        standards = self.state.catalog["standards"]
        target = standards[index % len(standards)]
        before = self.model.keys
        self._published(
            NewStandardVersion(standard_id=uuid.UUID(target["standard_id"]), title_es=title)
        )
        assert self.model.keys == before  # el texto nunca cambia el estado

    @rule(flag=st.booleans(), minutes=st.integers(AGGREGATION_WINDOW_MIN, AGGREGATION_WINDOW_MAX))
    def mark_single_occupancy(self, flag: bool, minutes: int) -> None:
        before = self.model.keys
        self._published(
            SetSingleOccupancy(single_occupancy=flag, aggregation_window_minutes=minutes)
        )
        assert self.model.keys == before

    @rule(model_version=st.sampled_from(MODELS))
    def report_a_model_version(self, model_version: str) -> None:
        previous, software = self.model.inventory
        self.model.inventory = (model_version, software)
        if not requires_model_regression(previous, model_version):
            return
        self.world.tick()
        (marked,) = self.world.run(
            self.world.regression.mark_model_version_change(
                self.world.system_context(self.site), (self.zone,), model_version
            )
        )
        if self.model.keys is None:
            self.model.first_marked_at = marked.marked_at
        self.model.keys = ALL_ROWS
        self.model.model_version = model_version
        self.model.marks += 1

    @rule(software=st.sampled_from(SOFTWARE))
    def change_only_the_software_version(self, software: str) -> None:
        model_version, _ = self.model.inventory
        self.model.inventory = (model_version, software)
        # El latido solo marca si cambió ``model_version``: aquí nunca llama al servicio.
        assert not requires_model_regression(model_version, model_version)

    # --- Invariantes ---------------------------------------------------------------------------

    @invariant()
    def the_row_is_the_model(self) -> None:
        if self.state is None:
            return
        row = self.world.regression_row(self.zone)
        if self.model.keys is None:
            assert row is None or row["state"] == "current"
        else:
            assert row is not None and row["state"] == "pending"
            stored = json.loads(row["affected_row_ids"])
            assert stored_keys(stored, dict(self.state.catalog)) == self.model.keys
            assert row["marked_at"] == self.model.first_marked_at
            if self.model.model_version is not None:
                assert row["model_version"] == self.model.model_version
        assert len(self.world.records(self.zone, "walk_test_regression_marked")) == self.model.marks

    @invariant()
    def the_zone_keeps_operating(self) -> None:
        # BR-GOB-53: la marca no toca las compuertas ni el modo resultante.
        assert self.world.resulting_mode(self.zone) == "productive"

    @invariant()
    def versions_stay_consecutive(self) -> None:
        assert self.model.versions == list(range(1, len(self.model.versions) + 1))


@pytest.fixture(scope="module")
def marking_world(postgres_endpoint: PostgresEndpoint) -> Iterator[CatalogRoutes]:
    with catalog_routes_world(postgres_endpoint, "regression_marking") as world:
        yield world


@pytest.mark.integration
def test_pr_gob_14_marking_follows_the_changes_and_never_stops_the_zone(
    marking_world: CatalogRoutes,
) -> None:
    RegressionMarking.world = marking_world
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(RegressionMarking)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=STEPS))


# --- Cierre de la reejecución (TASK-216) ---------------------------------------------------------


RERUN_TYPES: Final = tuple(
    d
    for d in CATALOG_RECORD_TYPES
    if d.record_type
    in ("walk_test_result", "walk_test_regression_cleared", "occlusion_test_result")
)
RERUN_STEPS: Final = 16
"""Más pasos que la marca: abrir, registrar y cerrar caben con marcas antes y entre medias."""
MIN_REPETITIONS: Final = 100


@dataclass
class Rerunning:
    """Los servicios reales de la reejecución y del cierre sobre ``CatalogRoutes``."""

    store: ClipStore
    reruns: RegressionRerunService
    closing: CloseRecordService


def rerun_services(routes: CatalogRoutes) -> Rerunning:
    sessions = routes.authz.sessions
    database, clock = routes.database, sessions.clock
    catalog = routes.catalog_repository
    gates = GateService(
        repository=PostgresGateRepository(database),
        catalog=catalog,
        agreements=PostgresAgreementRepository(),
        database=database,
        writer=routes.writer,
        authorizer=routes.authz.authorizer,
        audit=sessions.audit,
        free_text=routes.free_text,
        signer=routes.signer,
        clock=clock,
        sign_timeout_seconds=TEST_SIGN_TIMEOUT_SECONDS,
    )
    hierarchy = HierarchyService(
        IdentityDependencies(
            database=database,
            writer=routes.writer,
            audit=sessions.audit,
            outbox=sessions.outbox,
            authorizer=routes.authz.authorizer,
            free_text=routes.free_text,
            clock=clock,
            provider_organization_id=routes.authz.provider_organization_id,
        )
    )
    occlusions = OcclusionService(
        repository=PostgresOcclusionRepository(),
        sessions=PostgresWalkTestRepository(),
        catalog=catalog,
        gates=gates,
        reader=LectorExpediente(database=database, audit=sessions.audit),
        database=database,
        writer=routes.writer,
        free_text=routes.free_text,
        clock=clock,
    )
    walk_tests = WalkTestService(
        repository=PostgresWalkTestRepository(),
        catalog=catalog,
        gates=gates,
        nodes=hierarchy,
        identity=hierarchy,
        database=database,
        writer=routes.writer,
        audit=sessions.audit,
        free_text=routes.free_text,
        clock=clock,
        occlusions=occlusions,
    )
    store = ClipStore()
    closing = CloseRecordService(
        repository=PostgresCommissioningRecordRepository(),
        sessions=PostgresWalkTestRepository(),
        occlusion_tests=PostgresOcclusionRepository(),
        occlusions=occlusions,
        regressions=PostgresRegressionRepository(database),
        catalog=catalog,
        fleet=PostgresCommissioningQueries(),
        clips=ClipObjectStore(store),  # type: ignore[arg-type]
        gates=gates,
        identity=hierarchy,
        database=database,
        writer=routes.writer,
        audit=sessions.audit,
        free_text=routes.free_text,
        clock=clock,
    )
    reruns = RegressionRerunService(
        walk_tests=walk_tests, regressions=PostgresRegressionRepository(database), database=database
    )
    return Rerunning(store, reruns, closing)


class RegressionRerun(RegressionMarking):
    """La máquina de marca de TASK-209 con la reejecución y su cierre (TASK-216)."""

    world: ClassVar[CatalogRoutes]
    services: ClassVar[Rerunning]
    seen: ClassVar[Counter[str]] = Counter()
    """Cierres por desenlace en toda la corrida: la prueba exige haber visto los dos."""

    def __init__(self) -> None:
        super().__init__()
        world = self.world
        ((plant, _),) = self.site.zones()
        self.plant = plant
        self.node = uuid.uuid4()
        before = world.authz.now() - timedelta(hours=1)
        world.authz.execute(
            "INSERT INTO identity.node_identity (node_id, organization_id, plant_id, code, status,"
            " created_at) VALUES ($1, $2, $3, $4, 'enrolled', $5)",
            self.node,
            self.site.organization_id,
            plant,
            f"ND-{self.node.hex[:6].upper()}",
            before,
        )
        world.authz.execute(
            "INSERT INTO identity.zone_node_assignment (assignment_id, organization_id, plant_id,"
            " zone_id, node_id, assigned_at, assigned_by) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            uuid.uuid4(),
            self.site.organization_id,
            plant,
            self.zone,
            self.node,
            before,
            world.authz.operator_id,
        )
        # Las dos compuertas aprobadas con su instante, su respaldo y su autor: la apertura de la
        # reejecución lee el montaje con las guardas de VIG-150.
        decided = {
            "status": "approved",
            "decided_at": before.isoformat(),
            "record_id": str(uuid.uuid4()),
            "decided_by": str(world.authz.operator_id),
        }
        world.authz.execute(
            "UPDATE catalog.zone_gate_state SET mounting = $2, usage = $2 WHERE zone_id = $1",
            self.zone,
            json.dumps(decided),
        )
        cookie, concession = world.installer(self.site)
        scope = world.run(
            world.authz.contexts.context_from_session(cookie, concession_id=concession)
        )
        self.installer: ScopeContext = scope.context
        self.signer = uuid.UUID(str(self.admin.actor.id))
        self.session: WalkTestSession | None = None
        self.recorded = False
        self.marks_at_open = 0
        self.clears = 0

    def _all_keys(self) -> frozenset[Key]:
        assert self.state is not None
        return frozenset(
            (str(row.standard_id), row.posture.value) for row in derive_matrix(self.state.catalog)
        )

    @precondition(lambda self: self.state is not None)
    @rule(passes=st.integers(3, 4))
    def open_a_rerun(self, passes: int) -> None:
        if self.state is None:
            return
        self.world.tick()
        try:
            session: WalkTestSession = self.world.run(
                self.services.reruns.open(self.installer, self.zone, passes)
            )
        except CatalogRejected as rejected:
            # Las guardas de apertura de VIG-150: una sesión ya abierta en la zona.
            assert self.session is not None, rejected
            assert rejected.detail_code is CatalogDetailCode.WALK_TEST_IN_PROGRESS
            return
        except WalkTestConflict:
            assert self.session is None and self.model.keys is None
            return
        assert self.session is None and self.model.keys is not None
        opened = frozenset((str(r.standard_id), r.posture.value) for r in session.matrix_rows)
        expected = self._all_keys() if self.model.keys == ALL_ROWS else self.model.keys
        assert opened == expected  # solo las filas afectadas, o la matriz completa
        assert session.kind is WalkTestKind.REGRESSION_RERUN
        self.session, self.recorded, self.marks_at_open = session, False, self.model.marks

    @precondition(lambda self: self.session is not None and not self.recorded)
    @rule()
    def record_the_passes(self) -> None:
        session = self.session
        if session is None:
            return
        rows = [row.row_id for row in session.matrix_rows]
        per_row = max(session.passes_per_cell, -(-MIN_REPETITIONS // len(rows)))
        self.world.authz.execute(
            "INSERT INTO catalog.walk_test_pass (pass_id, organization_id, plant_id, session_id,"
            " row_id, result, recorded_by, recorded_at)"
            " SELECT gen_random_uuid(), $1, $2, $3, r.row_id, 'detected', $4,"
            " $5::timestamptz + g * interval '1 millisecond'"
            " FROM unnest($6::uuid[]) AS r(row_id), generate_series(1, $7) AS g",
            session.organization_id,
            session.plant_id,
            session.session_id,
            uuid.UUID(str(self.installer.actor.id)),
            session.started_at,
            rows,
            per_row,
        )
        self.recorded = True

    def _ready_to_close(self, session: WalkTestSession) -> None:
        """Lo que el cierre exige de otras tareas: oclusiones resueltas y un clip verificable."""
        world = self.world
        version = world.run(
            world.catalog_repository.version(self.installer, self.zone, session.catalog_version)
        )
        for camera in version.payload["cameras"]:
            world.authz.execute(
                "INSERT INTO catalog.occlusion_test (test_id, organization_id, plant_id,"
                " session_id, camera_id, started_at, ended_at, deadline, verification,"
                " declared_reason_es, recorded_by, ledger_record_id)"
                " VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $5,"
                " $5::timestamptz + interval '5 minutes', 'declared', $6, $7, gen_random_uuid())",
                session.organization_id,
                session.plant_id,
                session.session_id,
                uuid.UUID(camera["camera_id"]),
                session.started_at,
                "El nodo no envió eventos mientras se tapaba la cámara",
                uuid.UUID(str(self.installer.actor.id)),
            )
        clip_id, data = uuid.uuid4(), b"clip de verificacion sintetico"
        key = (
            f"org/{session.organization_id}/plant/{session.plant_id}/zone/{self.zone}"
            f"/node/{self.node}/{clip_id}.mp4"
        )
        received = session.started_at + timedelta(milliseconds=1)
        world.authz.execute(
            "INSERT INTO fleet.clip_upload_grant (clip_id, organization_id, plant_id, zone_id,"
            " node_id, purpose, storage_key, content_type, max_size_bytes, required_headers,"
            " issued_at, expires_at, status, used_at) VALUES ($1, $2, $3, $4, $5, 'verification',"
            " $6, 'video/mp4', $7, $8, $9, $9::timestamptz + interval '15 minutes', 'used', $9)",
            clip_id,
            session.organization_id,
            session.plant_id,
            self.zone,
            self.node,
            key,
            len(data),
            json.dumps({"x-amz-checksum-sha256": "x", "x-amz-meta-vigia-anonymized": "1"}),
            received,
        )
        world.authz.execute(
            "INSERT INTO fleet.verification_clip (clip_id, organization_id, plant_id, zone_id,"
            " node_id, received_at, sha256) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            clip_id,
            session.organization_id,
            session.plant_id,
            self.zone,
            self.node,
            received,
            hashlib.sha256(data).hexdigest(),
        )
        self.services.store.put(key, data)

    @precondition(lambda self: self.session is not None)
    @rule()
    def close_the_rerun(self) -> None:
        session = self.session
        if session is None:
            return
        if self.recorded:
            self._ready_to_close(session)
        self.world.tick()
        request = CloseRequest(signatures=[self.signer], beacon_latency_ms_p95=150)
        try:
            record: CommissioningRecord = self.world.run(
                self.services.closing.close(self.installer, session.session_id, request)
            )
        except CatalogRejected as rejected:
            assert not self.recorded, rejected
            assert rejected.detail_code is CatalogDetailCode.MATRIX_INCOMPLETE
            RegressionRerun.seen["matrix_incomplete"] += 1
            return
        assert self.recorded
        covering = self.model.marks == self.marks_at_open
        # La reejecución cubre sus filas: vuelve a current salvo que otra marca llegara después.
        assert record.regression_cleared == covering
        RegressionRerun.seen["current" if covering else "still_pending"] += 1
        if covering:
            self.model.keys = None
            self.clears += 1
        self.session = None

    @invariant()
    def one_clearance_per_covering_close(self) -> None:
        cleared = self.world.records(self.zone, "walk_test_regression_cleared")
        assert len(cleared) == self.clears
        events = self.world.events(self.zone, "regression_cleared")
        assert len(events) == self.clears


@pytest.fixture(scope="module")
def rerun_world(postgres_endpoint: PostgresEndpoint) -> Iterator[CatalogRoutes]:
    with catalog_routes_world(postgres_endpoint, "regression_rerun", RERUN_TYPES) as world:
        yield world


@pytest.mark.integration
def test_pr_gob_14_a_covering_rerun_returns_current_and_a_later_mark_keeps_pending(
    rerun_world: CatalogRoutes,
) -> None:
    RegressionRerun.world = rerun_world
    RegressionRerun.services = rerun_services(rerun_world)
    for value in _seeds_for_profile():
        seeded = hypothesis_seed(value)(RegressionRerun)
        run_state_machine_as_test(seeded, settings=settings(stateful_step_count=RERUN_STEPS))
    assert RegressionRerun.services.store.gets == 0
    # La corrida vio los dos desenlaces del cierre: no es una propiedad vacía.
    seen = RegressionRerun.seen
    assert seen["current"] > 0 and seen["still_pending"] > 0, seen


# --- Propiedades puras sobre la matriz -----------------------------------------------------------


def _renamed(matrix: WalkTestMatrix, index: int, title: str) -> tuple[dict[str, Any], str]:
    standards = matrix.catalog["standards"]
    target = standards[index % len(standards)]
    state = CatalogState(
        catalog=matrix.catalog, single_occupancy=False, aggregation_window_minutes=60
    )
    plan = plan_publication(
        state,
        NewStandardVersion(standard_id=uuid.UUID(target["standard_id"]), title_es=title),
        zone=matrix.zone,
        issued_at=T0,
        declared_by=DECLARED_BY,
        reason_es=REASON,
        new_standard_id=uuid.uuid4(),
    )
    return state_of(plan).catalog, str(target["standard_id"])


@given(matrix=walk_test_matrices(), index=st.integers(0, 64), title=st.sampled_from(TITLES))
def test_a_text_change_does_not_mark_and_carries_every_pending_row(
    matrix: WalkTestMatrix, index: int, title: str
) -> None:
    after, _ = _renamed(matrix, index, title)

    assert publication_rows(matrix.catalog, after, (CatalogChangedField.STANDARDS,)) is None
    pending = tuple(sorted(matrix.pending))
    carried = carried_forward(pending, matrix.catalog, after)

    assert carried != ALL_ROWS
    assert stored_keys([str(r) for r in carried], after) == stored_keys(
        [str(r) for r in pending], matrix.catalog
    )
    assert len(carried) == len(pending)


@given(matrix=walk_test_matrices(), extra=st.data())
def test_the_union_never_loses_a_row_and_keeps_the_first_instant(
    matrix: WalkTestMatrix, extra: st.DataObject
) -> None:
    rows = [row.row_id for row in matrix.rows]
    second = frozenset(extra.draw(st.lists(st.sampled_from(rows), min_size=1)))
    current = WalkTestRegression.initial(
        matrix.zone.organization_id, matrix.zone.plant_id, matrix.zone.zone_id
    )
    first_mark = RegressionMark(
        cause=RegressionCause.CATALOG_CHANGE,
        affected_row_ids=tuple(matrix.pending),
        marked_at=T0,
        catalog_version=2,
    )
    later = RegressionMark(
        cause=RegressionCause.CATALOG_CHANGE,
        affected_row_ids=tuple(second),
        marked_at=T0.replace(hour=9),
        catalog_version=3,
    )

    once = merged(current, first_mark)
    twice = merged(once, later)

    assert set(twice.affected_row_ids or ()) == set(matrix.pending) | second
    assert twice.marked_at == T0 and twice.catalog_version == 3
    absorbed = merged(
        twice,
        RegressionMark(
            cause=RegressionCause.FRAMING_RECAPTURED, affected_row_ids=ALL_ROWS, marked_at=T0
        ),
    )
    assert absorbed.affected_row_ids == ALL_ROWS
    assert merged(absorbed, later).affected_row_ids == ALL_ROWS


@given(scenario=catalog_versions(max_changes=6))
def test_the_rows_of_a_mark_are_rows_of_the_new_matrix(scenario: CatalogScenario) -> None:
    state: CatalogState | None = None
    for index, intent in enumerate([None, *scenario.intents]):
        change = scenario.first if intent is None else resolve(intent, state)  # type: ignore[arg-type]
        try:
            plan = plan_publication(
                state,
                change,
                zone=scenario.zone,
                issued_at=T0,
                declared_by=DECLARED_BY,
                reason_es=REASON,
                new_standard_id=uuid.UUID(int=index + 1, version=4),
            )
        except CatalogRuleViolated:  # los cambios rechazados no publican
            continue
        rows = publication_rows(
            None if state is None else state.catalog, plan.catalog, plan.changed_fields
        )
        if state is not None:
            expected = expected_keys(change, dict(state.catalog), plan.catalog)
            if expected is None:
                assert rows is None
            elif expected == ALL_ROWS:
                assert rows == ALL_ROWS
            else:
                assert rows is not None and rows != ALL_ROWS
                assert stored_keys([str(r) for r in rows], plan.catalog) == expected
        else:
            assert rows is None  # la versión 1 no marca
        state = state_of(plan)
