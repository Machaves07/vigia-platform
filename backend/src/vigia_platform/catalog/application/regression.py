"""``catalog.regression``: la marca de regresión del walk-test (LC-GOB-09; BR-GOB-51 a 56).

``RegressionService`` es el ``RegressionMarker`` real de la publicación (TASK-208): ``mark`` corre
**dentro** de la transacción que publica la versión, con el contexto de U-03 de esa transacción,
así que la versión, la marca, su registro y su evento se confirman o se revierten juntos. Además
expone las otras dos causas y la lectura:

- ``mark_model_version_change(context, zone_ids, model_version)``: ``model_version_change`` con
  la matriz completa en cada zona. Lo llama el latido (TASK-223) cuando el nodo informa otro
  ``model_version``; un cambio solo de ``software_version`` nunca lo invoca
  (``requires_model_regression``).
- ``mark_framing_recaptured(context, zone_id, camera_id, reason_es)``: ``framing_recaptured`` con
  la matriz completa (nota U03-H-14), desde ``POST /zones/{zone_id}/framing-recaptures``
  (``commissioning.run``, A-55). No crea versión del catálogo: el encuadre no es campo suyo.
- ``regression_state(context, zone_id)`` (``catalog.read``): la fila, o ``current`` si la zona
  nunca se marcó. Bajo concesión, auditada (A-56).

Cada marca toma el **candado de la fila de regresión** de la zona, lee la fila, la une con la
marca (``merged``), escribe ``walk_test_regression_marked`` con ``regression_marked`` por la
bandeja y guarda la fila con ese registro. Nada de esto toca las compuertas ni ``resulting_mode``:
la zona sigue operando (BR-GOB-53, 54). Ningún paso lee la hora del sistema.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import (
    MAX_REASON_CHARS,
    MIN_REASON_CHARS,
    ZoneCatalogVersion,
    ZoneRef,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField, RegressionCause
from vigia_platform.catalog.domain.regression import (
    ALL_ROWS,
    RegressionMark,
    WalkTestRegression,
    carried_forward,
    merged,
    publication_rows,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.api.errors import ApiErrorCode
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "CAPTURE_TOLERANCE",
    "MARKED_RECORD_TYPE",
    "MAX_MODEL_ZONES",
    "REGRESSION_MARKED",
    "RegressionRequestInvalid",
    "RegressionService",
    "RegressionWriteFailed",
    "requires_model_regression",
]

MARKED_RECORD_TYPE: Final = "walk_test_regression_marked"
REGRESSION_MARKED: Final = "regression_marked"
MAX_MODEL_ZONES: Final = 64
"""Zonas de un nodo en una sola marca de ``model_version`` `[estimación propia]`."""
CAPTURE_TOLERANCE: Final = timedelta(minutes=5)
"""Desfase admitido entre el reloj de la consola y el de la plataforma `[estimación propia]`: una
recaptura «del futuro» más allá de este margen es un error de la petición."""
_TECHNICAL_ID_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")
_REASON: Final = FreeTextField(MARKED_RECORD_TYPE, "/reason_es", MIN_REASON_CHARS, MAX_REASON_CHARS)


class RegressionRequestInvalid(Exception):
    """La petición incumple un límite (cámara ajena a la zona, instante futuro…):
    ``invalid_request``, sin escribir nada."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self) -> None:
        super().__init__("petición de regresión fuera de los límites")


class RegressionWriteFailed(Exception):
    """El expediente rechazó el registro de la marca: la transacción se revierte entera."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"registro rechazado: {rejection.code.value}")
        self.rejection = rejection


def requires_model_regression(previous_model_version: str | None, model_version: str) -> bool:
    """¿El latido debe marcar? Solo si cambió ``model_version`` (BR-GOB-51, RF-BOR-15).

    ``software_version`` ni siquiera es argumento: un cambio solo de ella nunca marca.
    """
    return previous_model_version is not None and previous_model_version != model_version


def _model_version(value: object) -> str:
    """``TechnicalId`` del contrato: minúscula inicial y ``[a-z0-9_.-]``, hasta 64."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not "a" <= value[0] <= "z"
        or any(char not in _TECHNICAL_ID_CHARS for char in value)
    ):
        raise RegressionRequestInvalid
    return value


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


@repository
class RegressionService:
    """La marca de regresión y su lectura."""

    def __init__(
        self,
        *,
        repository: PostgresRegressionRepository,
        catalog: PostgresCatalogRepository,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        audit: AuditWriter,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._audit = audit
        self._free_text = free_text
        self._clock = clock

    def __repr__(self) -> str:
        return "RegressionService()"

    # --- Marca de la publicación (RegressionMarker) -------------------------------------------

    async def lock(self, transaction: Transaction, zone_id: uuid.UUID) -> None:
        """El candado de la fila de regresión, antes de que la publicación escriba nada."""
        await self._repository.lock(transaction, zone_id)

    async def mark(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        previous: ZoneCatalogVersion | None,
        new: ZoneCatalogVersion,
        changed_fields: tuple[CatalogChangedField, ...],
    ) -> None:
        """La marca que deja la versión ``new``, en la transacción de su publicación."""
        if new.zone_id != zone_id or (previous is not None and previous.zone_id != zone_id):
            raise ValueError("las versiones son de otra zona")
        await self._repository.lock(transaction, zone_id)
        current = await self._repository.get(transaction, zone_id)
        if current is None:
            current = WalkTestRegression.initial(new.organization_id, new.plant_id, zone_id)
        pending_rows = current.affected_row_ids if current.pending else None
        if previous is not None and pending_rows is not None and pending_rows != ALL_ROWS:
            # Las filas pendientes siguen a la versión nueva de sus estándares, marque o no.
            carried = carried_forward(pending_rows, previous.payload, new.payload)
            if carried != pending_rows:
                current = replace(current, affected_row_ids=carried)
                await self._repository.save(transaction, current)
        rows = publication_rows(
            None if previous is None else previous.payload, new.payload, changed_fields
        )
        if rows is None:
            return
        await self._apply(
            transaction,
            current,
            RegressionMark(
                cause=RegressionCause.CATALOG_CHANGE,
                affected_row_ids=rows,
                marked_at=new.issued_at,
                catalog_version=new.catalog_version,
            ),
        )

    # --- Cambio de modelo (latido, TASK-223) ---------------------------------------------------

    async def mark_model_version_change(
        self,
        context: ScopeContext,
        zone_ids: Iterable[uuid.UUID],
        model_version: str,
        *,
        transaction: Transaction | None = None,
    ) -> tuple[WalkTestRegression, ...]:
        """``model_version_change`` con la matriz completa en cada zona del nodo.

        Cada zona tiene que existir y estar dentro del alcance del contexto (las zonas asignadas
        al nodo); si no, ``ResourceNotFound`` y nada escrito. Con ``transaction`` (abierta con un
        contexto de U-03) corre dentro de ella, en la transacción corta del latido.
        """
        version = _model_version(model_version)
        zones = tuple(sorted(set(zone_ids)))
        if not 1 <= len(zones) <= MAX_MODEL_ZONES or any(type(z) is not uuid.UUID for z in zones):
            raise RegressionRequestInvalid
        if transaction is not None:
            return await self._model_marks(transaction, zones, version)
        if not isinstance(context, ScopeContext):
            raise ResourceNotFound()
        async with self._database.transaction(with_unit(context, ActorUnit.U03)) as opened:
            return await self._model_marks(opened, zones, version)

    async def _model_marks(
        self, transaction: Transaction, zones: tuple[uuid.UUID, ...], model_version: str
    ) -> tuple[WalkTestRegression, ...]:
        context = transaction.context
        if context.actor.unit is not ActorUnit.U03:
            raise TypeError("la marca de modelo se escribe con un contexto de U-03")
        marked_at = to_millisecond(self._clock.now())
        refs: list[ZoneRef] = []
        for zone_id in zones:
            zone = await self._repository.zone(transaction, zone_id)
            if zone is None or not context.covers(zone.plant_id, zone.zone_id):
                raise ResourceNotFound()
            refs.append(zone)
        results: list[WalkTestRegression] = []
        for zone in refs:  # en orden de zona: dos latidos nunca se interbloquean
            await self._repository.lock(transaction, zone.zone_id)
            current = await self._repository.get(transaction, zone.zone_id)
            results.append(
                await self._apply(
                    transaction,
                    current or WalkTestRegression.initial(*_ids(zone)),
                    RegressionMark(
                        cause=RegressionCause.MODEL_VERSION_CHANGE,
                        affected_row_ids=ALL_ROWS,
                        marked_at=marked_at,
                        model_version=model_version,
                    ),
                )
            )
        return tuple(results)

    # --- Recaptura del encuadre (SCR-07, A-55) -------------------------------------------------

    async def mark_framing_recaptured(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        camera_id: uuid.UUID,
        reason_es: str,
        *,
        captured_at: datetime | None = None,
    ) -> WalkTestRegression:
        """``framing_recaptured`` con la matriz completa, sin versión nueva del catálogo.

        ``commissioning.run`` sobre la zona (inexistente o fuera de alcance: ``ResourceNotFound``).
        La cámara tiene que estar en el catálogo vigente (si no, ``RegressionRequestInvalid``); una
        zona sin catálogo no tiene cámaras (``catalog_zone_without_cameras``). ``reason_es`` pasa
        la política de texto libre y queda en el registro, con su autor y su fecha.
        """
        reason = self._reason(reason_es)
        zone, authorized = await self._zone(context, zone_id, PermissionKey.COMMISSIONING_RUN)
        # Tras autorizar: fuera del alcance responde ``not_found`` sea cual sea el cuerpo.
        if type(camera_id) is not uuid.UUID:
            raise RegressionRequestInvalid
        now = to_millisecond(self._clock.now())
        captured = self._captured(captured_at, now)
        writer_context = with_unit(authorized, ActorUnit.U03)
        async with self._database.transaction(writer_context) as transaction:
            await self._repository.lock(transaction, zone.zone_id)
            catalog = await self._catalog.current(transaction, zone.zone_id)
            if catalog is None:
                raise CatalogRejected(CatalogDetailCode.ZONE_WITHOUT_CAMERAS)
            cameras = {str(camera["camera_id"]) for camera in catalog.payload["cameras"]}
            if str(camera_id) not in cameras:
                raise RegressionRequestInvalid
            current = await self._repository.get(transaction, zone.zone_id)
            extra: dict[str, Any] = {"camera_id": str(camera_id), "reason_es": reason}
            if captured is not None:
                extra["captured_at"] = format_timestamp(captured)
            return await self._apply(
                transaction,
                current or WalkTestRegression.initial(*_ids(zone)),
                RegressionMark(
                    cause=RegressionCause.FRAMING_RECAPTURED,
                    affected_row_ids=ALL_ROWS,
                    marked_at=now,
                ),
                extra=extra,
            )

    # --- Lectura ------------------------------------------------------------------------------

    async def regression_state(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> WalkTestRegression:
        """``GET /zones/{zone_id}/regression`` (``catalog.read``)."""
        zone, authorized = await self._zone(context, zone_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            current = await self._repository.get(transaction, zone.zone_id)
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
        return current or WalkTestRegression.initial(*_ids(zone))

    # --- Común --------------------------------------------------------------------------------

    async def _zone(
        self, context: ScopeContext, zone_id: uuid.UUID, key: PermissionKey
    ) -> tuple[ZoneRef, ScopeContext]:
        """La zona y el contexto autorizado sobre ella; inexistente o fuera de alcance, igual."""
        if not isinstance(context, ScopeContext) or type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        zone = await self._catalog.zone(context, zone_id)
        if zone is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context, key, Resource.zone(context.organization_id, zone.plant_id, zone.zone_id)
        )
        return zone, authorized

    def _reason(self, value: object) -> str:
        if not isinstance(value, str):
            raise RegressionRequestInvalid
        try:
            return self._free_text.apply(value, _REASON)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None

    @staticmethod
    def _captured(value: object, now: datetime) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise RegressionRequestInvalid
        captured = to_millisecond(value)
        if captured > now + CAPTURE_TOLERANCE:
            raise RegressionRequestInvalid
        return captured

    async def _apply(
        self,
        transaction: Transaction,
        current: WalkTestRegression,
        mark: RegressionMark,
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> WalkTestRegression:
        """Registro y evento de ``mark`` y la fila unida, en ``transaction`` (con el candado)."""
        rows: Any = (
            ALL_ROWS
            if mark.affected_row_ids == ALL_ROWS
            else [str(row) for row in mark.affected_row_ids]
        )
        event: dict[str, Any] = {
            "zone_id": str(current.zone_id),
            "cause": mark.cause.value,
            "affected_row_ids": rows,
        }
        if mark.catalog_version is not None:
            event["catalog_version"] = mark.catalog_version
        if mark.model_version is not None:
            event["model_version"] = mark.model_version
        content = {**event, "marked_at": format_timestamp(mark.marked_at), **(extra or {})}
        written = await self._writer.write(
            transaction.context,
            MARKED_RECORD_TYPE,
            content,
            scope=RecordScope(plant_id=current.plant_id, zone_id=current.zone_id),
            events=(NewEvent(event_name=REGRESSION_MARKED, payload=event),),
            occurred_at=mark.marked_at,
            transaction=transaction,
        )
        regression = replace(merged(current, mark), ledger_record_id=_record_id(written))
        await self._repository.save(transaction, regression)
        return regression


def _ids(zone: ZoneRef) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return zone.organization_id, zone.plant_id, zone.zone_id


def _record_id(written: Receipt | LedgerRejection) -> uuid.UUID:
    """El registro de la marca; un rechazo revierte (no hay ``source_key``: nunca duplicado)."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        raise RegressionWriteFailed(written)
    if written.status is not AcceptanceStatus.ACCEPTED:
        raise RegressionWriteFailed(LedgerRejection.of(LedgerRejectionCode.IDEMPOTENCY_CONFLICT))
    return _uuid(written.record_id)
