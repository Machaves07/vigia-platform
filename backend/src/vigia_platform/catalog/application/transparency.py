"""Vista de transparencia del COPASST (SCR-16; interfaces §3.5; LC-GOB-04, H-55).

``view`` (``GET /zones/{zone_id}/transparency``, ``transparency.read`` sobre la zona): lo que la
planta declaró y acordó sobre la zona, en una sola transacción de lectura:

- ``declared_scope``: el texto y los encuadres del acta de alcance que cita la compuerta de montaje
  (sin acta, vacíos) y la ``minimum_coverage`` de la versión vigente del catálogo;
- ``standards``: ``{standard_id, version, title_es, declared_text}`` de la versión vigente del
  catálogo (la misma tabla de TASK-202 que lee ``catalog.versions``);
- ``current_agreement``: el acuerdo ``approved`` de la zona con sus firmantes y cuándo confirmó
  cada uno (interfaces §1.2), o ``None``;
- ``gates``: las dos compuertas y el modo resultante;
- ``pending_confirmation_for_me``: el acuerdo pendiente que espera la firma del usuario de la
  sesión, o ``None``. Confirmarla es la única acción de esta vista (``catalog.agreements``).

Una zona inexistente, de otra organización o fuera del alcance es ``ResourceNotFound``. Ninguna
aprobación condiciona esta lectura (H-55). Bajo concesión, auditada en la misma transacción
(A-56, ``catalog_read``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from vigia_platform.catalog.adapters.postgres.agreement_repository import (
    PostgresAgreementRepository,
)
from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.application.gates import GateService
from vigia_platform.catalog.domain.agreements import AgreementConfirmation, UseAgreement
from vigia_platform.catalog.domain.gates import ZoneGateState
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.context import ScopeContext, repository

__all__ = ["TransparencyService", "TransparencyView"]


@dataclass(frozen=True, slots=True)
class TransparencyView:
    zone_id: uuid.UUID
    scope_text_es: str | None
    cameras: tuple[Mapping[str, Any], ...]
    minimum_coverage: Mapping[str, Any] | None
    standards: tuple[Mapping[str, Any], ...]
    current_agreement: UseAgreement | None
    confirmations: tuple[AgreementConfirmation, ...]
    gates: ZoneGateState
    pending_confirmation_for_me: uuid.UUID | None


def _standard(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "standard_id": value["standard_id"],
        "version": value["version"],
        "title_es": value["title_es"],
        "declared_text": value["declared_text"],
    }


@repository
class TransparencyService:
    """``catalog.agreements`` (transparencia): la vista de solo lectura de la zona."""

    def __init__(
        self,
        *,
        repository: PostgresAgreementRepository,
        gates: GateService,
        catalog: PostgresCatalogRepository,
        database: LedgerDatabase,
        audit: AuditWriter,
    ) -> None:
        self._repository = repository
        self._gates = gates
        self._catalog = catalog
        self._database = database
        self._audit = audit

    def __repr__(self) -> str:
        return "TransparencyService()"

    async def view(self, context: ScopeContext, zone_id: uuid.UUID) -> TransparencyView:
        """La vista de transparencia de la zona (``transparency.read``)."""
        zone, authorized = await self._gates.zone(context, zone_id, PermissionKey.TRANSPARENCY_READ)
        user_id = uuid.UUID(str(authorized.actor.id))
        async with self._database.transaction(authorized) as transaction:
            state = await self._gates.state_in(transaction, zone)
            version = await self._catalog.version_in(transaction, zone.zone_id)
            record_id = state.mounting.record_id
            scope = (
                None
                if record_id is None
                else await self._repository.scope_record(transaction, zone.zone_id, record_id)
            )
            current = await self._repository.current(transaction, zone.zone_id)
            confirmations = (
                ()
                if current is None
                else await self._repository.confirmations(transaction, current.agreement_id)
            )
            pending = await self._repository.pending_for(transaction, zone.zone_id, user_id)
            if authorized.concession_id is not None:
                # BR-NUC-38 y A-56: la lectura del proveedor, auditada (fallo cerrado).
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=zone.plant_id,
                    zone_id=zone.zone_id,
                    result_count=1,
                    transaction=transaction,
                )
        payload: Mapping[str, Any] = {} if version is None else version.payload
        coverage = payload.get("minimum_coverage")
        return TransparencyView(
            zone_id=zone.zone_id,
            scope_text_es=None if scope is None else scope.scope_text_es,
            cameras=() if scope is None else scope.cameras,
            minimum_coverage=coverage if isinstance(coverage, Mapping) else None,
            standards=tuple(_standard(s) for s in payload.get("standards", ())),
            current_agreement=current,
            confirmations=confirmations,
            gates=state,
            pending_confirmation_for_me=pending,
        )
