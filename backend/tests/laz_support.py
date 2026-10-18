"""Mundo de prueba compartido de U-04 (``LoopWorld``, TASK-305) sobre PostgreSQL 16 real.

Se apoya en el mundo de la ingesta de U-03 (``tests/fleet_ingest_support.py``: escritor, bandeja,
verificador de evidencias e ingesta reales como ``vigia_app``) con los diecisiete tipos de U-04
y sus eventos registrados (``register_u04``), y añade:

- **dos organizaciones** (``a`` y ``b``), cada una con su planta, su zona asignada al nodo, el
  catálogo publicado y la compuerta de uso aprobada (``IngestStack.site``);
- ``ingest_finding`` e ``ingest_detection``: hallazgos y detecciones **por las rutas del contrato
  de U-03** (``POST`` del nodo con su certificado), con el recibo del expediente;
- ``project``: aplica un registro de U-03 a la proyección como lo hará la vía de eventos
  (TASK-341), en una transacción con contexto de evento;
- ``write``: escribe un registro de U-04 por ``EscritorExpediente`` en una transacción del
  llamador y, en la misma, ``LoopProjectionStore.apply_in_transaction`` (BR-LAZ-146); con
  ``rollback=True`` la transacción se revierte después de aplicar la proyección;
- constructores de contenidos válidos (``classification_content``) y lecturas como superusuario
  (``finding_state``, ``episode``, ``checkpoint``, ``ledger_records``).

**Archivo caliente**: las tareas de U-04 lo amplían con funciones nuevas, sin cambiar las firmas
existentes. Solo datos generados; reloj simulado desde la hora de la base (retro 14) y topes de
la base de 60 s (retro 15).

TASK-341 añade ``InstrumentedPorts`` (el doble instrumentado de los puertos de U-03 y de
``CoveragePort``, NFR-LAZ-03) y, en ``LoopWorld``, el consumidor ``loop_projection`` sobre los
puertos reales (``projection_consumer``), los eventos de la bandeja (``outbox_event``,
``synthetic_event``) y la sembradura de lo que el consumidor lee (``set_marker``,
``regression_record``, ``set_single_occupancy``, ``configure_default_window``, ``clone_row``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import secrets
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from vigia_contracts.models import api
from vigia_contracts.models.enumerations import GateStatus

from tests.fleet_ingest_support import IngestSite, IngestStack, ingest_stack
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import unit_context
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.record_types import register_accreditation_record_types
from vigia_platform.catalog.adapters.postgres.query_ports import catalog_query_ports
from vigia_platform.catalog.domain.enums import GateKind
from vigia_platform.catalog.domain.gates import GateInterval
from vigia_platform.catalog.domain.ports import (
    MAX_SINGLE_OCCUPANCY_ZONES,
    PortLimitExceeded,
    SingleOccupancy,
)
from vigia_platform.catalog.events import register_catalog_event_types
from vigia_platform.catalog.record_types import register_catalog_record_types
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.ledger.application.coverage import CoverageService
from vigia_platform.ledger.application.evidence_read import (
    EvidenceService,
    MarkerResult,
    MarkerVerification,
)
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import LedgerRejection, Receipt, RecordScope
from vigia_platform.ledger.domain.coverage import (
    ClockBasis,
    CoverageLayer,
    CoverageState,
    CoverageStatus,
)
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.loop.config.parameters import LoopSettings
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.loop.projection.consumer import LoopProjectionConsumer
from vigia_platform.loop.projection.enrichment import ProjectionEnrichment
from vigia_platform.loop.projection.fold import (
    FindingStateRow,
    ProjectionInput,
    Step,
    aggregation_bucket,
)
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.loop.record_types import register_loop_record_types
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.outbox.publish import OutboxEvent, partition_key
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "REASON_TEXT",
    "FakeMarkers",
    "FakeProjectionTransaction",
    "FakeSettings",
    "IngestedSubject",
    "InstrumentedPorts",
    "LoopOrganization",
    "LoopWorld",
    "PortUnavailable",
    "RecordingStore",
    "Rollback",
    "enrichment_with",
    "loop_world",
    "register_u04",
    "register_u04_and_catalog",
    "regression_row",
    "synthetic_row",
]


class PortUnavailable(RuntimeError):
    """El doble simula un puerto que no responde."""


@dataclass
class InstrumentedPorts:
    """Doble instrumentado de ``CatalogQueryPort``, ``GateQueryPort`` y ``CoveragePort`` (TASK-341;
    NFR-LAZ-03; PAT-LAZ-REN-05).

    - ``calls`` cuenta las llamadas **por operación** (``single_occupancy_many``, ``state_at``…).
      «Ninguna llamada dentro de un bucle sobre elementos» se comprueba con él: el recuento no
      puede crecer con el número de filas o de hallazgos de la zona.
    - La historia de cada zona es una **partición** del tiempo: cada cambio de ``gate_changes`` o
      de ``coverage_changes`` abre un intervalo que cierra el cambio siguiente, así que los
      intervalos **no se solapan por construcción**, como la restricción de exclusión de la
      implementación real; antes del primer cambio, la compuerta no tiene intervalo (``pending``)
      y la zona no es observable.
    - ``single_occupancy_many`` respeta el tope de 50 zonas (``PortLimitExceeded``) y, con
      ``fail_single_occupancy``, no responde (``PortUnavailable``, también sobre los puertos
      reales). Una zona sin ``occupancy`` es ``ResourceNotFound`` en la implementación real: aquí,
      ``PortUnavailable`` para el lote entero.
    - ``wrapped`` delega en los puertos reales (``catalog``, ``gates``, ``coverage``) contando
      igual: es la forma de la prueba de integración.
    """

    organization_id: uuid.UUID = field(default_factory=uuid.uuid4)
    plant_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occupancy: dict[uuid.UUID, tuple[bool, int]] = field(default_factory=dict)
    gate_changes: dict[uuid.UUID, list[tuple[datetime, GateStatus]]] = field(default_factory=dict)
    """La compuerta de **uso**."""
    mounting_changes: dict[uuid.UUID, list[tuple[datetime, GateStatus]]] = field(
        default_factory=dict
    )
    """La compuerta de montaje: otra historia, para que consultar la compuerta equivocada se vea."""
    coverage_changes: dict[uuid.UUID, list[tuple[datetime, CoverageState]]] = field(
        default_factory=dict
    )
    fail_single_occupancy: bool = False
    wrapped: Any = None
    calls: Counter[str] = field(default_factory=Counter)

    def _count(self, operation: str) -> None:
        self.calls[operation] += 1

    # --- historia generada --------------------------------------------------------------------

    def gate_intervals(
        self, zone_id: uuid.UUID, gate: GateKind = GateKind.USAGE
    ) -> tuple[GateInterval, ...]:
        source = self.gate_changes if gate is GateKind.USAGE else self.mounting_changes
        changes = sorted(source.get(zone_id, ()), key=lambda change: change[0])
        return tuple(
            GateInterval(
                organization_id=self.organization_id,
                plant_id=self.plant_id,
                zone_id=zone_id,
                gate=gate,
                status=status,
                effective_from=start,
                effective_until=changes[index + 1][0] if index + 1 < len(changes) else None,
                decided_by=self.organization_id,
                reason_es=None,
                record_id=None,
                ledger_record_id=uuid.uuid5(zone_id, f"{gate.value}{index}"),
            )
            for index, (start, status) in enumerate(changes)
        )

    # --- CatalogQueryPort ---------------------------------------------------------------------

    async def single_occupancy_many(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], at: datetime | None = None
    ) -> tuple[SingleOccupancy, ...]:
        self._count("single_occupancy_many")
        if len(zone_ids) > MAX_SINGLE_OCCUPANCY_ZONES:
            raise PortLimitExceeded("single_occupancy_many", "50 zonas")
        if self.fail_single_occupancy:
            raise PortUnavailable("single_occupancy_many")
        if self.wrapped is not None:
            result: tuple[SingleOccupancy, ...] = await self.wrapped.catalog.single_occupancy_many(
                context, zone_ids, at
            )
            return result
        if any(zone not in self.occupancy for zone in zone_ids):
            raise PortUnavailable("single_occupancy_many")
        return tuple(
            SingleOccupancy(
                zone_id=zone,
                single_occupancy=self.occupancy[zone][0],
                aggregation_window_minutes=self.occupancy[zone][1],
                catalog_version=1,
                issued_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for zone in zone_ids
        )

    # --- GateQueryPort ------------------------------------------------------------------------

    async def state_at(
        self, context: ScopeContext, zone_id: uuid.UUID, gate: GateKind, at: datetime
    ) -> GateInterval | None:
        self._count("state_at")
        if self.wrapped is not None:
            found: GateInterval | None = await self.wrapped.gates.state_at(
                context, zone_id, gate, at
            )
            return found
        for interval in self.gate_intervals(zone_id, gate):
            if interval.effective_from <= at and (
                interval.effective_until is None or at < interval.effective_until
            ):
                return interval
        return None

    async def gate_history(
        self, context: ScopeContext, zone_id: uuid.UUID, start: datetime, end: datetime
    ) -> tuple[GateInterval, ...]:
        self._count("gate_history")
        return tuple(
            interval
            for gate in (GateKind.MOUNTING, GateKind.USAGE)
            for interval in self.gate_intervals(zone_id, gate)
            if interval.effective_from <= end
            and (interval.effective_until is None or interval.effective_until > start)
        )

    # --- CoveragePort -------------------------------------------------------------------------

    async def estado_en(
        self, context: ScopeContext, zone_id: uuid.UUID, instant: datetime
    ) -> CoverageStatus:
        self._count("estado_en")
        if self.wrapped is not None:
            status: CoverageStatus = await self.wrapped.coverage.estado_en(
                context, zone_id, instant
            )
            return status
        state = CoverageState.NOT_OBSERVABLE
        for start, value in sorted(self.coverage_changes.get(zone_id, ()), key=lambda c: c[0]):
            if start <= instant:
                state = value
        return CoverageStatus(
            instant=instant,
            state=state,
            causes=(),
            layer=CoverageLayer.NODE_REPORT,
            clock_basis=ClockBasis.PLATFORM,
            source_record_ids=(),
        )

    # --- el resto de las operaciones no las usa el consumidor ---------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def other(*args: Any, **kwargs: Any) -> Any:
            self._count(name)
            if self.wrapped is not None:
                for port in (self.wrapped.catalog, self.wrapped.gates, self.wrapped.coverage):
                    if hasattr(port, name):
                        return await getattr(port, name)(*args, **kwargs)
            raise NotImplementedError(name)

        return other


@dataclass(frozen=True)
class RealPorts:
    """Los puertos reales que ``InstrumentedPorts(wrapped=...)`` cuenta."""

    catalog: Any
    gates: Any
    coverage: Any


# --- dobles sin base para el consumidor (TASK-341) --------------------------------------------


@dataclass
class _Rows:
    rows: list[Any]

    def all(self) -> list[Any]:
        return self.rows


@dataclass
class FakeProjectionTransaction:
    """Una transacción sin base: ``regression`` son las filas que devuelve la lectura de la
    historia de regresión (``walk_test_regression_*``); cuenta las sentencias."""

    context: ScopeContext
    regression: list[Any] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)

    async def execute(self, statement: Any, parameters: Any = None) -> _Rows:
        self.statements.append(" ".join(str(statement).split()))
        return _Rows(list(self.regression))


def regression_row(
    zone_id: uuid.UUID, *, marked: bool, at: datetime, sequence: int, cause: str = "catalog_change"
) -> Any:
    """Una fila de la historia de regresión como la devuelve ``ledger.ledger_record``."""
    content: dict[str, Any] = {"zone_id": str(zone_id)}
    if marked:
        content.update(cause=cause, affected_row_ids="all", marked_at=format_timestamp(at))
    else:
        content.update(cleared_by_session_id=str(uuid.uuid4()), cleared_at=format_timestamp(at))
    return _RegressionRow(
        record_type="walk_test_regression_marked" if marked else "walk_test_regression_cleared",
        chain_sequence=sequence,
        content=json.dumps(content).encode(),
    )


@dataclass(frozen=True)
class _RegressionRow:
    record_type: str
    chain_sequence: int
    content: bytes


@dataclass
class FakeSettings:
    """``SettingsSource`` con la ventana por defecto ``window`` y el resto del diseño."""

    window: int = 60

    async def settings_in(self, transaction: Any, plant_id: uuid.UUID) -> tuple[Any, int | None]:
        defaults = LoopSettings.defaults()
        integers = {**defaults.integers, "single_occupancy_default_window_minutes": self.window}
        return dataclasses.replace(defaults, integers=integers), 1


@dataclass
class FakeMarkers:
    """``MarkerPort``: las evidencias ``broken`` y cuántas llamadas hubo (y de qué tamaño)."""

    broken: set[uuid.UUID] = field(default_factory=set)
    batches: list[int] = field(default_factory=list)

    async def resultados_marca(
        self, context: ScopeContext, evidence_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, MarkerVerification]:
        requested = list(evidence_ids)
        self.batches.append(len(requested))
        return {
            evidence: MarkerVerification(
                result=MarkerResult.BROKEN if evidence in self.broken else MarkerResult.INTACT,
                marker_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
                container_marker_sampled_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for evidence in requested
        }


@dataclass
class RecordingStore:
    """``LoopProjectionStore`` sin base para los eventos por zona: aplica ``change`` a ``rows`` de
    las zonas pedidas y cuenta las llamadas (la sentencia única la prueba la integración)."""

    rows: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def update_enrichment(self, transaction: Any, **arguments: Any) -> int:
        self.calls.append(arguments)
        zones = arguments.get("zone_ids")
        change = arguments["change"]
        selected = [
            index
            for index, row in enumerate(self.rows)
            if zones is None or row.zone_id in set(zones)
        ]
        for index in selected:
            self.rows[index] = change(self.rows[index])
        return len(selected)


def synthetic_row(
    organization_id: uuid.UUID, plant_id: uuid.UUID, zone_id: uuid.UUID, occurred_at: datetime
) -> FindingStateRow:
    """Una fila ``received`` recién creada por el pliegue, con sus valores conservadores."""
    record_id = uuid.uuid4()
    row = FindingStateRow(
        finding_state_id=record_id,
        organization_id=organization_id,
        plant_id=plant_id,
        zone_id=zone_id,
        subject_kind="finding",
        subject_id=uuid.uuid4(),
        anchor_record_id=record_id,
        family="coexistence",
        tier="tier_1",
        standard_id=uuid.uuid4(),
        standard_version=1,
        lifecycle_state="received",
        previous_lifecycle_state=None,
        current_classification_outcome=None,
        current_reason_category=None,
        classification_count=0,
        first_classified_at=None,
        last_classified_at=None,
        classified_within_deadline=None,
        current_action_id=None,
        current_action_type=None,
        assignee_user_id=None,
        assignee_inactive=False,
        due_at=None,
        overdue_since=None,
        overdue_days=0,
        closed_at=None,
        closure_record_id=None,
        unclosable_since=None,
        unclosable_review_due_at=None,
        unclosable_pending_review=False,
        closed_with_later_different_classification=False,
        reopening_count=0,
        has_signed_root_cause=False,
        observability_state_at_event="observable",
        zone_gate_status_at_event="approved",
        zone_agreement_revoked=False,
        zone_agreement_revoked_at=None,
        single_occupancy_zone=True,
        aggregation_window_minutes=60,
        walk_test_regression_pending=False,
        walk_test_regression_cause=None,
        occurred_at_exact=occurred_at,
        occurred_at_bucket=aggregation_bucket(occurred_at, 60),
        age_days=0,
        evidence_flagged=False,
        missing_segments_declared=False,
        last_event_id=record_id,
        last_record_id=record_id,
        projection_updated_at=occurred_at,
        projection_digest="0" * 64,
    )
    return dataclasses.replace(row, projection_digest=row.computed_digest())


def enrichment_with(
    ports: InstrumentedPorts,
    *,
    settings: FakeSettings | None = None,
    markers: FakeMarkers | None = None,
) -> ProjectionEnrichment:
    """``ProjectionEnrichment`` sobre el doble instrumentado (sin base)."""
    return ProjectionEnrichment(
        catalog=ports,
        gates=ports,
        coverage=ports,
        markers=markers if markers is not None else FakeMarkers(),
        settings=settings if settings is not None else FakeSettings(),
    )


REASON_TEXT: Final = "Motivo sintético de la clasificación del hallazgo en la zona"
"""Motivo neutro (20 a 1000 caracteres) que pasa la política de texto libre."""


def register_u04(registry: RecordTypeRegistry, catalog: OutboxCatalog) -> None:
    """Los diecisiete tipos de registro y los veinticinco eventos de U-04."""
    register_loop_record_types(registry)
    register_accreditation_record_types(registry)
    register_loop_event_types(catalog.event_types)
    register_accreditation_event_types(catalog.event_types)


class Rollback(Exception):
    """Revierte la transacción de ``LoopWorld.write`` después de aplicar la proyección."""


@dataclass(frozen=True)
class LoopOrganization:
    """Una organización del mundo: su sitio de ingesta (planta, zona, nodo, catálogo)."""

    site: IngestSite

    @property
    def organization_id(self) -> uuid.UUID:
        return self.site.organization_id

    @property
    def plant_id(self) -> uuid.UUID:
        return self.site.plant_id

    @property
    def zone_id(self) -> uuid.UUID:
        return self.site.zone_id


@dataclass(frozen=True)
class IngestedSubject:
    """Un hallazgo (su episodio) o una detección ingresados por la ruta del contrato."""

    organization: LoopOrganization
    kind: IngestKind
    document: Mapping[str, Any]
    record_id: uuid.UUID
    """``platform_record_id`` del registro de U-03."""

    @property
    def subject_kind(self) -> str:
        return "finding" if self.kind is IngestKind.FINDING else "detection"

    @property
    def subject_id(self) -> uuid.UUID:
        if self.kind is IngestKind.FINDING:
            return uuid.UUID(str(self.document["episode"]["episode_id"]))
        return uuid.UUID(str(self.document["detection_id"]))


@dataclass
class LoopWorld:
    stack: IngestStack
    store: LoopProjectionStore
    a: LoopOrganization
    b: LoopOrganization

    # --- contextos ---------------------------------------------------------------------------

    def context(self, organization: LoopOrganization) -> ScopeContext:
        """Contexto de sesión de un coordinador SST de U-04 sobre toda la organización."""
        return unit_context(organization.organization_id, ActorUnit.U04)

    def event_context(self, organization: LoopOrganization) -> ScopeContext:
        """Contexto de evento (sistema) de U-04, como el del consumidor de la proyección."""
        return unit_context(organization.organization_id, ActorUnit.U04, kind=ActorKind.SYSTEM)

    def organization(self) -> LoopOrganization:
        """Otra organización con su planta, zona, catálogo y compuerta aprobada."""
        return LoopOrganization(self.stack.site())

    def add_plant(self, organization: LoopOrganization) -> uuid.UUID:
        """Otra planta (sin zonas) de ``organization``."""
        plant_id = uuid.uuid4()
        self.stack.execute(
            "INSERT INTO identity.plant (plant_id, organization_id, code, name, country,"
            " data_region, timezone, created_at, created_by) VALUES ($1, $2, $3,"
            " 'Planta sintética', 'CO', 'us-east-1', 'America/Bogota', $4, $5)",
            plant_id,
            organization.organization_id,
            f"PL-{secrets.token_hex(8).upper()}",
            self.stack.now(),
            self.stack.authz.operator_id,
        )
        return plant_id

    # --- ingesta de U-03 ---------------------------------------------------------------------

    def _ingest(
        self, organization: LoopOrganization, kind: IngestKind, document: Mapping[str, Any]
    ) -> IngestedSubject:
        self.stack.tick()
        response = self.stack.post(organization.site, kind, document)
        if response.status_code != 200:
            raise AssertionError(response.text)
        receipt = api.parse_receipt(response.content)
        return IngestedSubject(organization, kind, document, uuid.UUID(receipt.platform_record_id))

    def ingest_finding(self, organization: LoopOrganization, **changes: Any) -> IngestedSubject:
        """Un hallazgo por ``POST`` del nodo (``FindingSubmission`` válido de la zona)."""
        return self._ingest(
            organization, IngestKind.FINDING, self.stack.finding(organization.site, **changes)
        )

    def ingest_detection(self, organization: LoopOrganization) -> IngestedSubject:
        """Una detección para revisión por ``POST`` del nodo."""
        return self._ingest(
            organization,
            IngestKind.DETECTION_FOR_REVIEW,
            self.stack.detection(organization.site),
        )

    # --- proyección --------------------------------------------------------------------------

    def project(self, subject: IngestedSubject, *, event_id: uuid.UUID | None = None) -> Step:
        """Aplica el registro de U-03 de ``subject`` como la vía de eventos (TASK-341)."""
        context = self.event_context(subject.organization)
        database = self.stack.primary.database

        async def apply() -> Step:
            async with database.transaction(context) as transaction:
                item = await self.store.record_input(
                    transaction, subject.record_id, event_id=event_id
                )
                return await self.store.apply_in_transaction(transaction, item)

        result: Step = self.stack.run(apply())
        return result

    def apply(self, organization: LoopOrganization, item: ProjectionInput | Receipt) -> Step:
        """``apply_in_transaction`` en una transacción propia con contexto de evento."""
        context = self.event_context(organization)
        database = self.stack.primary.database

        async def apply() -> Step:
            async with database.transaction(context) as transaction:
                return await self.store.apply_in_transaction(transaction, item)

        result: Step = self.stack.run(apply())
        return result

    def write(
        self,
        organization: LoopOrganization,
        record_type: str,
        content: Mapping[str, Any],
        *,
        apply: bool = True,
        rollback: bool = False,
    ) -> tuple[Receipt | LedgerRejection, Step | None]:
        """Un registro de U-04 por el escritor real y su proyección en la **misma** transacción."""
        context = self.context(organization)
        database = self.stack.primary.database
        writer = self.stack.primary.writer
        scope = RecordScope(plant_id=organization.plant_id, zone_id=organization.zone_id)
        outcome: list[Any] = []

        async def run() -> None:
            async with database.transaction(context) as transaction:
                if apply:
                    receipt, step = await self.store.write_and_apply(
                        transaction, writer, context, record_type, content, scope=scope
                    )
                    outcome.extend((receipt, step))
                else:
                    outcome.append(
                        await writer.write(
                            context, record_type, content, scope=scope, transaction=transaction
                        )
                    )
                if rollback:
                    raise Rollback

        with contextlib.suppress(Rollback):
            self.stack.run(run())
        receipt = outcome[0]
        return receipt, (outcome[1] if len(outcome) > 1 else None)

    def scope(self, organization: LoopOrganization) -> RecordScope:
        """Planta y zona del sitio de ``organization`` (los registros de U-04 no las llevan)."""
        return RecordScope(plant_id=organization.plant_id, zone_id=organization.zone_id)

    # --- contenidos válidos ------------------------------------------------------------------

    def classification_content(
        self,
        subject: IngestedSubject,
        *,
        outcome: str = "confirmed",
        reason_category: str = "real_coexistence",
        sequence: int = 1,
        previous_outcome: str | None = None,
        effect_on_loop: str = "loop_opened",
    ) -> dict[str, Any]:
        """Contenido válido de ``classification`` sobre ``subject`` (DE §5.2)."""
        document = subject.document
        content: dict[str, Any] = {
            "classification_id": str(self.stack.uuid7()),
            "subject_kind": subject.subject_kind,
            "subject_id": str(subject.subject_id),
            "anchor_record_id": str(subject.record_id),
            "sequence_in_subject": sequence,
            "outcome": outcome,
            "reason_category": reason_category,
            "reason_text": REASON_TEXT,
            "is_reclassification": sequence > 1,
            "reinforced_reason": sequence > 1,
            "effect_on_loop": effect_on_loop,
            "standard": dict(document["standard"]),
            "family": document["family"],
            "observability_state_at_event": document["observability_state"],
            "zone_id": str(subject.organization.zone_id),
            "signer": {
                "user_id": str(self.stack.authz.operator_id),
                "display_name": "Coordinación SST sintética",
                "role_in_use": "coordinator_sst",
            },
            "classified_at": format_timestamp(self.stack.now()),
        }
        if previous_outcome is not None:
            content["previous_outcome"] = previous_outcome
        return content

    # --- lecturas como superusuario -----------------------------------------------------------

    def finding_state(self, subject: IngestedSubject) -> dict[str, Any] | None:
        rows = self.stack.fetch(
            "SELECT * FROM loop.finding_state WHERE organization_id = $1 AND subject_id = $2",
            subject.organization.organization_id,
            subject.subject_id,
        )
        return dict(rows[0]) if rows else None

    def episode(self, subject: IngestedSubject) -> dict[str, Any] | None:
        rows = self.stack.fetch(
            "SELECT * FROM loop.episode WHERE organization_id = $1 AND episode_id = $2",
            subject.organization.organization_id,
            subject.subject_id,
        )
        return dict(rows[0]) if rows else None

    def checkpoint(self, organization: LoopOrganization) -> dict[str, Any] | None:
        rows = self.stack.fetch(
            "SELECT * FROM loop.loop_projection_checkpoint"
            " WHERE organization_id = $1 AND plant_id = $2",
            organization.organization_id,
            organization.plant_id,
        )
        return dict(rows[0]) if rows else None

    def ledger_records(self, organization: LoopOrganization) -> list[ProjectionInput]:
        """Los registros de la cadena de la planta, en orden, como los lee el pliegue (§9)."""
        rows = self.stack.fetch(
            "SELECT record_id, organization_id, plant_id, chain_sequence, record_type,"
            " received_at, ledger.vigia_bytes_to_jsonb(content)::text AS content"
            " FROM ledger.ledger_record WHERE organization_id = $1 AND plant_id = $2"
            " ORDER BY chain_sequence",
            organization.organization_id,
            organization.plant_id,
        )
        return [
            ProjectionInput(
                record_id=uuid.UUID(str(row["record_id"])),
                organization_id=uuid.UUID(str(row["organization_id"])),
                plant_id=uuid.UUID(str(row["plant_id"])),
                chain_sequence=int(row["chain_sequence"]),
                record_type=str(row["record_type"]),
                received_at=row["received_at"],
                document=json.loads(row["content"]),
            )
            for row in rows
        ]

    def record_exists(self, record_id: uuid.UUID) -> bool:
        return bool(
            self.stack.fetch("SELECT 1 FROM ledger.ledger_record WHERE record_id = $1", record_id)
        )

    # --- consumidor loop_projection (TASK-341) ------------------------------------------------

    def real_ports(self, database: Any | None = None) -> RealPorts:
        """``CatalogQueryPort``, ``GateQueryPort`` y ``CoveragePort`` reales sobre ``database``."""
        database = database if database is not None else self.stack.primary.database
        sessions = self.stack.authz.sessions
        ports = catalog_query_ports(database)
        coverage = CoverageService(database=database, audit=sessions.audit, clock=sessions.clock)
        return RealPorts(ports.catalog, ports.gates, coverage)

    def projection_consumer(
        self,
        *,
        ports: InstrumentedPorts | None = None,
        settings: Any | None = None,
        database: Any | None = None,
    ) -> tuple[LoopProjectionConsumer, InstrumentedPorts]:
        """``loop_projection`` como lo compone ``loop.registration``, con los puertos contados
        (por defecto, ``InstrumentedPorts`` sobre los puertos reales)."""
        database = database if database is not None else self.stack.primary.database
        sessions = self.stack.authz.sessions
        if ports is None:
            ports = InstrumentedPorts(wrapped=self.real_ports(database))
        enrichment = ProjectionEnrichment(
            catalog=ports,
            gates=ports,
            coverage=ports,
            markers=EvidenceService(
                database=database, audit=sessions.audit, storage=self.stack.storage
            ),
            settings=settings
            if settings is not None
            else PostgresLoopConfigurationRepository(database),
        )
        consumer = LoopProjectionConsumer(
            store=self.store,
            reader=LectorExpediente(database=database, audit=sessions.audit, clock=sessions.clock),
            enrichment=enrichment,
            writer=self.stack.primary.writer,
            clock=sessions.clock,
        )
        return consumer, ports

    def deliver(
        self,
        consumer: LoopProjectionConsumer,
        event: OutboxEvent,
        *,
        database: Any | None = None,
    ) -> int:
        """Una entrega de ``event`` en una transacción propia con el contexto del evento."""
        database = database if database is not None else self.stack.primary.database
        context = unit_context(event.organization_id, ActorUnit.U04, kind=ActorKind.SYSTEM)

        async def run() -> int:
            async with database.transaction(context) as transaction:
                return await consumer.apply(event, transaction)

        written: int = self.stack.run(run())
        return written

    def outbox_event(self, subject: IngestedSubject) -> OutboxEvent:
        """El ``finding_received`` que la ingesta publicó para ``subject``."""
        (row,) = self.stack.fetch(
            "SELECT event_id, organization_id, plant_id, event_name, partition_key,"
            " ledger_sequence, payload::text AS payload, correlation_id, created_at"
            " FROM shared.outbox_event WHERE organization_id = $1 AND event_name = $2"
            " AND payload->>'platform_record_id' = $3",
            subject.organization.organization_id,
            "finding_received" if subject.kind is IngestKind.FINDING else "other",
            str(subject.record_id),
        )
        return OutboxEvent(
            event_id=uuid.UUID(str(row["event_id"])),
            organization_id=uuid.UUID(str(row["organization_id"])),
            plant_id=uuid.UUID(str(row["plant_id"])),
            event_name=str(row["event_name"]),
            partition_key=str(row["partition_key"]),
            ledger_sequence=row["ledger_sequence"],
            payload=json.loads(row["payload"]),
            correlation_id=uuid.UUID(str(row["correlation_id"])),
            created_at=row["created_at"],
        )

    def synthetic_event(
        self,
        organization: LoopOrganization,
        event_name: str,
        payload: Mapping[str, Any],
        *,
        plant: bool = True,
    ) -> OutboxEvent:
        """Un evento de la bandeja con la forma de U-03 o de U-02, con hora del reloj simulado."""
        self.stack.tick()
        plant_id = organization.plant_id if plant else None
        return OutboxEvent(
            event_id=self.stack.uuid7(),
            organization_id=organization.organization_id,
            plant_id=plant_id,
            event_name=event_name,
            partition_key=partition_key(organization.organization_id, plant_id),
            ledger_sequence=None,
            payload=dict(payload),
            correlation_id=uuid.uuid4(),
            created_at=self.stack.now(),
        )

    def gate_event(self, organization: LoopOrganization, status: str) -> OutboxEvent:
        """La compuerta de uso de la zona del sitio pasa a ``status`` en la historia (en
        ``stack.now()``) y su ``gate_state_changed``."""
        self.stack.usage(organization.site, approved=status == "approved", at=self.stack.now())
        return self.synthetic_event(
            organization,
            "gate_state_changed",
            {
                "zone_id": str(organization.zone_id),
                "gate": "usage",
                "status": status,
                "resulting_mode": "productive" if status == "approved" else "commissioning",
                "record_id": str(uuid.uuid4()),
                "reason_es_present": status == "revoked",
            },
        )

    def set_marker(self, subject: IngestedSubject, result: str) -> None:
        """La verificación diferida de las evidencias de ``subject`` termina en ``result``."""
        self.stack.execute(
            "UPDATE ledger.evidence SET marker_verification_result = $2,"
            " marker_verified_at = $3, container_marker_sampled_at = $3 WHERE record_id = $1",
            subject.record_id,
            result,
            self.stack.now(),
        )

    def set_single_occupancy(
        self, organization: LoopOrganization, single: bool, window: int
    ) -> None:
        """Una versión nueva del catálogo de la zona del sitio con la marca unipersonal (el
        catálogo es de solo anexar: la anterior queda sustituida en este instante)."""
        self.stack.tick()
        now = self.stack.now()
        (current,) = self.stack.fetch(
            "SELECT catalog_version FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 AND superseded_at IS NULL",
            organization.zone_id,
        )
        version = int(current["catalog_version"])
        self.stack.execute(
            "UPDATE catalog.zone_catalog_version SET superseded_at = $3"
            " WHERE zone_id = $1 AND catalog_version = $2",
            organization.zone_id,
            version,
            now,
        )
        self.stack.execute(
            "INSERT INTO catalog.zone_catalog_version (organization_id, plant_id, zone_id,"
            " catalog_version, issued_at, issued_by, role_in_use, reason_es, changed_fields,"
            " payload, envelope, single_occupancy, aggregation_window_minutes, ledger_record_id)"
            " SELECT organization_id, plant_id, zone_id, catalog_version + 1, $3, issued_by,"
            " role_in_use, reason_es, ARRAY['single_occupancy'],"
            " jsonb_set(payload, '{version}', to_jsonb(catalog_version + 1)),"
            " jsonb_set(envelope, '{payload,version}', to_jsonb(catalog_version + 1)),"
            " $4, $5, $6 FROM catalog.zone_catalog_version"
            " WHERE zone_id = $1 AND catalog_version = $2",
            organization.zone_id,
            version,
            now,
            single,
            window,
            uuid.uuid4(),
        )

    def configure_default_window(self, organization: LoopOrganization, minutes: int) -> None:
        """La configuración del lazo de la planta con otra ventana por defecto (SUP-U03-04)."""
        self.stack.execute(
            "INSERT INTO loop.plant_loop_configuration (plant_id, organization_id,"
            " single_occupancy_default_window_minutes, business_calendar, timezone, version,"
            " changed_at, changed_by, change_reason) VALUES ($1, $2, $3,"
            ' \'{"working_weekdays": [1, 2, 3, 4, 5], "non_working_dates": []}\'::jsonb,'
            " 'America/Bogota', 1, $4, '{}'::jsonb, 'Configuración sintética')"
            " ON CONFLICT (plant_id) DO UPDATE"
            " SET single_occupancy_default_window_minutes = EXCLUDED."
            "single_occupancy_default_window_minutes",
            organization.plant_id,
            organization.organization_id,
            minutes,
            self.stack.now(),
        )

    def regression_record(
        self,
        organization: LoopOrganization,
        *,
        marked: bool,
        at: datetime,
        cause: str = "catalog_change",
    ) -> Receipt:
        """Un ``walk_test_regression_marked`` (o ``_cleared``) de U-03 por el escritor real."""
        zone = str(organization.zone_id)
        content: dict[str, Any]
        if marked:
            record_type = "walk_test_regression_marked"
            content = {
                "zone_id": zone,
                "cause": cause,
                "catalog_version": 1,
                "affected_row_ids": "all",
                "marked_at": format_timestamp(at),
            }
        else:
            record_type = "walk_test_regression_cleared"
            content = {
                "zone_id": zone,
                "cleared_by_session_id": str(self.stack.uuid7()),
                "cleared_at": format_timestamp(at),
            }
        receipt = self.stack.run(
            self.stack.primary.writer.write(
                unit_context(organization.organization_id, ActorUnit.U03),
                record_type,
                content,
                scope=self.scope(organization),
            )
        )
        assert isinstance(receipt, Receipt), receipt
        return receipt

    def clone_row(
        self, subject: IngestedSubject, *, zone_id: uuid.UUID | None = None, **columns: Any
    ) -> uuid.UUID:
        """Otra fila de ``finding_state`` copiada de la de ``subject`` (como superusuario), en
        ``zone_id`` y con ``columns`` cambiadas; devuelve su ``subject_id``."""
        row = self.finding_state(subject)
        assert row is not None
        subject_id = uuid.uuid4()
        values = {
            **row,
            "finding_state_id": uuid.uuid4(),
            "subject_id": subject_id,
            "zone_id": zone_id if zone_id is not None else row["zone_id"],
            **columns,
        }
        names = list(values)
        placeholders = ", ".join(f"${index}" for index in range(1, len(names) + 1))
        self.stack.execute(
            f"INSERT INTO loop.finding_state ({', '.join(names)}) VALUES ({placeholders})",  # noqa: S608
            *values.values(),
        )
        return subject_id

    def rows(self, organization: LoopOrganization, **where: Any) -> list[dict[str, Any]]:
        """Las filas de ``finding_state`` de la organización (como superusuario)."""
        conditions = ["organization_id = $1"]
        arguments: list[Any] = [organization.organization_id]
        for name, value in where.items():
            arguments.append(value)
            conditions.append(f"{name} = ${len(arguments)}")
        return [
            dict(row)
            for row in self.stack.fetch(
                "SELECT * FROM loop.finding_state WHERE "  # noqa: S608
                + " AND ".join(conditions)
                + " ORDER BY finding_state_id",
                *arguments,
            )
        ]


def register_u04_and_catalog(registry: RecordTypeRegistry, catalog: OutboxCatalog) -> None:
    """``register_u04`` más los tipos y eventos del catálogo de U-03 (la historia de regresión
    que lee ``loop_projection``, TASK-341)."""
    register_u04(registry, catalog)
    register_catalog_record_types(registry)
    register_catalog_event_types(catalog.event_types)


@contextlib.contextmanager
def loop_world(
    endpoint: PostgresEndpoint,
    prefix: str,
    *,
    register: Callable[[RecordTypeRegistry, OutboxCatalog], None] = register_u04,
) -> Iterator[LoopWorld]:
    """El mundo de U-04: la ingesta de U-03 con los tipos de U-04 y dos organizaciones."""
    with ingest_stack(endpoint, prefix, register=register) as stack:
        yield LoopWorld(
            stack=stack,
            store=LoopProjectionStore(),
            a=LoopOrganization(stack.site()),
            b=LoopOrganization(stack.site()),
        )
