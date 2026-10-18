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
"""

from __future__ import annotations

import contextlib
import json
import secrets
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from vigia_contracts.models import api

from tests.fleet_ingest_support import IngestSite, IngestStack, ingest_stack
from tests.integration.conftest import PostgresEndpoint
from tests.writer_support import unit_context
from vigia_platform.accreditation.events import register_accreditation_event_types
from vigia_platform.accreditation.record_types import register_accreditation_record_types
from vigia_platform.fleet.domain.ingest_order import IngestKind
from vigia_platform.ledger.application.writer import LedgerRejection, Receipt, RecordScope
from vigia_platform.ledger.registry import RecordTypeRegistry
from vigia_platform.loop.events import register_loop_event_types
from vigia_platform.loop.projection.fold import ProjectionInput, Step
from vigia_platform.loop.projection.store import LoopProjectionStore
from vigia_platform.loop.record_types import register_loop_record_types
from vigia_platform.shared.context import ActorKind, ActorUnit, ScopeContext
from vigia_platform.shared.outbox.registries import OutboxCatalog
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "REASON_TEXT",
    "IngestedSubject",
    "LoopOrganization",
    "LoopWorld",
    "Rollback",
    "loop_world",
    "register_u04",
]

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


@contextlib.contextmanager
def loop_world(endpoint: PostgresEndpoint, prefix: str) -> Iterator[LoopWorld]:
    """El mundo de U-04: la ingesta de U-03 con los tipos de U-04 y dos organizaciones."""
    with ingest_stack(endpoint, prefix, register=register_u04) as stack:
        yield LoopWorld(
            stack=stack,
            store=LoopProjectionStore(),
            a=LoopOrganization(stack.site()),
            b=LoopOrganization(stack.site()),
        )
