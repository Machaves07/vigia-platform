"""Solicitud y consulta de una exportación (TASK-317; LC-LAZ-20; SUP-U05-21 y 22; BR-LAZ-87, 88
y 100; PAT-LAZ-ESC-02; NFR-LAZ-10 y 25).

``ExportRequests.request(context, scope)`` admite una solicitud en este orden y, si algo falla,
no escribe nada ni publica nada:

1. **Forma**: de 1 a 20 zonas sin repetir (más de 20, ``export_scope_too_large``); periodo con
   ``to > from`` y de a lo sumo 366 días (más, ``export_scope_too_large``); ``package_form`` de
   la lista cerrada. Una forma imposible (cero zonas, repetidas, periodo vacío) es
   ``invalid_request`` sin ``detail_code``.
2. **Alcance**: cada zona existe en la organización del contexto (seguridad a nivel de fila) y el
   contexto tiene ``export.create`` sobre **cada una**; si no, ``not_found``, igual que una zona
   inexistente (BR-NUC-09: nunca ``forbidden`` ni ``export_zone_out_of_scope``, que revelaría que
   la zona existe). Después, todas las zonas deben ser de la planta pedida:
   ``export_zones_span_plants``. Una sola organización por solicitud por construcción (BR-LAZ-88):
   el contexto fija la organización y ninguna zona de otra es visible.
3. **Estimación previa** (forma ``with_clips``): suma de ``ledger.evidence.size_bytes`` de las
   zonas en el periodo, **sin descargar ni tocar ningún byte** del almacén (el servicio no tiene
   puerto de almacén); por encima de 20 GB, ``export_scope_too_large``. La forma
   ``without_clips`` no lleva bytes de clips y es la salida siempre disponible (nota de NFR
   Requirements sobre BR-LAZ-90): no se estima.
4. **Límite exacto** (PAT-LAZ-ESC-02, PR-LAZ-52): en la **transacción de admisión**, candado
   consultivo de la organización (``pg_advisory_xact_lock``), conteo sobre
   ``export_request.requested_at`` de la última hora (5 por usuario y 20 por organización) e
   inserción de la fila. El contador y la huella son **la misma fila**: no hay tabla contadora. Si
   no cabe, ``rate_limited`` con la espera hasta que salga de la ventana la solicitud más antigua.
5. En esa misma transacción: la fila ``requested`` con el ``correlation_id`` de la petición, la
   entrada de auditoría ``export_requested`` (BR-LAZ-100) y el evento ``export_requested`` en la
   partición de la organización para el consumidor ``export_worker``. Si la transacción se
   revierte, no hay solicitud, ni auditoría, ni trabajo encolado.

**Orden de candados** (uno solo): el candado consultivo de la organización
(``accreditation.export_request_rate|<organización>``) y, después, lo que toman la auditoría y la
bandeja al escribir. La adquisición del arrendamiento (``export.lease``) usa otro candado
(``accreditation.export_run|<organización>``) y nunca los dos en la misma transacción.

``ExportRequests.status(context, export_id)``: la solicitud de la organización del contexto con
``export.create`` sobre cada una de sus zonas; si no, ``not_found``. ``ready`` con la descarga
vencida se lee ``expired`` (``state.effective_state``, con el ``Clock`` inyectado).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import text

from vigia_platform.accreditation.detail_codes import AccreditationDetailCode
from vigia_platform.accreditation.events import EXPORT_REQUESTED, ExportRequested
from vigia_platform.accreditation.export.state import (
    DOWNLOAD_VALIDITY,
    MAX_ESTIMATED_BYTES,
    MAX_PERIOD,
    MAX_ZONES,
    ExportStatus,
    PackageSummary,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.loop.domain.enums import ExportState, PackageForm
from vigia_platform.loop.observability.metrics import LoopMetricName, get_loop_metrics
from vigia_platform.shared.api.errors import MAX_RETRY_AFTER_SECONDS
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7_at
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "EXPORTS_PER_ORGANIZATION_PER_HOUR",
    "EXPORTS_PER_USER_PER_HOUR",
    "RATE_LOCK_PREFIX",
    "RATE_WINDOW",
    "ExportRateLimited",
    "ExportRequestInvalid",
    "ExportRequestRejected",
    "ExportRequests",
    "ExportScope",
    "read_status",
]

EXPORTS_PER_USER_PER_HOUR: Final = 5
EXPORTS_PER_ORGANIZATION_PER_HOUR: Final = 20
RATE_WINDOW: Final = timedelta(hours=1)
"""Ventana deslizante de los dos límites exactos (PAT-LAZ-ESC-02)."""
RATE_LOCK_PREFIX: Final = "accreditation.export_request_rate|"
"""Clave del candado consultivo de admisión, seguida de la organización."""

_EXPORT_CREATE: Final = PermissionKey.EXPORT_CREATE

_ZONES: Final = text(
    "SELECT zone_id, plant_id FROM identity.zone"
    " WHERE organization_id = :organization_id AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
)
_ESTIMATE: Final = text(
    "SELECT COALESCE(sum(size_bytes), 0) AS estimated FROM ledger.evidence"
    " WHERE organization_id = :organization_id AND plant_id = :plant_id"
    " AND zone_id = ANY(CAST(:zone_ids AS uuid[]))"
    " AND COALESCE(starts_at, verified_at) >= :period_from"
    " AND COALESCE(starts_at, verified_at) < :period_to"
)
_RATE_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
_RATE_COUNT: Final = text(
    "SELECT count(*) FILTER (WHERE user_id = :user_id) AS by_user, count(*) AS by_organization,"
    " min(requested_at) FILTER (WHERE user_id = :user_id) AS oldest_by_user,"
    " min(requested_at) AS oldest_by_organization"
    " FROM accreditation.export_request"
    " WHERE organization_id = :organization_id AND requested_at > :since"
)
_INSERT: Final = text(
    "INSERT INTO accreditation.export_request (export_id, organization_id, plant_id, zone_ids,"
    " period_from, period_to, requested_by, user_id, requested_at, state, package_form,"
    " expires_at, correlation_id)"
    " VALUES (:export_id, :organization_id, :plant_id, CAST(:zone_ids AS uuid[]), :period_from,"
    " :period_to, CAST(:requested_by AS jsonb), :user_id, :requested_at, 'requested',"
    " :package_form, :expires_at, :correlation_id)"
)
_READ: Final = text(
    "SELECT export_id, organization_id, plant_id, zone_ids, period_from, period_to,"
    " package_form, state, progress_percent, current_artifact, attempt_count, failure_code,"
    " requested_at, started_at, completed_at, expires_at, correlation_id"
    " FROM accreditation.export_request WHERE export_id = :export_id"
)
_PACKAGE: Final = text(
    "SELECT document_sha256, archive_sha256, size_bytes, artifact_count,"
    " cardinality(checkpoint_record_ids) AS checkpoint_count, verifier_version"
    " FROM accreditation.export_package WHERE export_id = :export_id"
)


class ExportRequestInvalid(Exception):
    """Forma imposible de la solicitud: ``invalid_request`` sin ``detail_code``."""

    code: Final = "invalid_request"


class ExportRequestRejected(Exception):
    """Rechazo de negocio de BR-LAZ-87: ``invalid_request`` con su ``detail_code`` (A-68)."""

    def __init__(self, detail_code: AccreditationDetailCode) -> None:
        self.detail_code = detail_code
        super().__init__(detail_code.value)


class ExportRateLimited(Exception):
    """Se superaría uno de los dos límites exactos: ``rate_limited`` con su espera."""

    code: Final = "rate_limited"

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"límite de exportaciones; reintentar en {retry_after_seconds} s")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportScope:
    """Lo que pide ``SUP-U05-21``: planta, zonas, periodo y forma del paquete."""

    plant_id: uuid.UUID
    zone_ids: tuple[uuid.UUID, ...]
    period_from: datetime
    period_to: datetime
    package_form: PackageForm


class TransactionSource(Protocol):
    """``shared.db.Database``: el único camino a la base."""

    def transaction(
        self, context: ScopeContext
    ) -> contextlib.AbstractAsyncContextManager[Transaction]: ...


@repository
class ExportRequests:
    """Admisión (``request``) y consulta (``status``) de exportaciones."""

    def __init__(
        self,
        *,
        database: TransactionSource,
        authorizer: Authorizer,
        audit: AuditWriter,
        outbox: OutboxPort,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._outbox = outbox
        self._clock = clock
        self._random_bytes = random_bytes

    @property
    def clock(self) -> Clock:
        """El reloj con el que se lee ``expired`` (el mismo de la admisión)."""
        return self._clock

    # --- SUP-U05-21 -------------------------------------------------------------------------

    async def request(self, context: ScopeContext, scope: ExportScope) -> ExportStatus:
        if not isinstance(context, ScopeContext):
            raise ResourceNotFound()
        _check_shape(scope)
        period_from = to_millisecond(scope.period_from)
        period_to = to_millisecond(scope.period_to)
        authorized = await self._authorize_zones(context, scope.zone_ids, scope.plant_id)
        if scope.package_form is PackageForm.WITH_CLIPS:
            await self._check_estimate(authorized, scope, period_from, period_to)
        now = to_millisecond(self._clock.now())
        export_id = uuid7_at(now, self._random_bytes(10))
        actor = authorized.actor
        requested_by = {
            "user_id": str(actor.id),
            "display_name": actor.display_name_snapshot,
            "role_in_use": None if actor.role_in_use is None else actor.role_in_use.value,
        }
        expires_at = now + DOWNLOAD_VALIDITY
        zone_ids = [str(zone) for zone in scope.zone_ids]
        async with self._database.transaction(authorized) as transaction:
            await _check_rate(transaction, authorized, now)
            await transaction.execute(
                _INSERT,
                {
                    "export_id": export_id,
                    "organization_id": authorized.organization_id,
                    "plant_id": scope.plant_id,
                    "zone_ids": zone_ids,
                    "period_from": period_from,
                    "period_to": period_to,
                    "requested_by": json.dumps(requested_by, ensure_ascii=False),
                    "user_id": actor.id,
                    "requested_at": now,
                    "package_form": scope.package_form.value,
                    "expires_at": expires_at,
                    "correlation_id": authorized.correlation_id,
                },
            )
            await self._audit.append(
                authorized,
                AuditOperation.EXPORT_REQUESTED,
                plant_id=scope.plant_id,
                resource=ResourceRef("export", export_id),
                filters={
                    "package_form": scope.package_form.value,
                    "zone_count": len(scope.zone_ids),
                    "period_from": format_timestamp(period_from),
                    "period_to": format_timestamp(period_to),
                },
                transaction=transaction,
            )
            await self._outbox.publish(
                transaction,
                NewEvent(
                    event_name=EXPORT_REQUESTED,
                    payload=ExportRequested(
                        export_id=str(export_id),
                        plant_id=str(scope.plant_id),
                        zone_ids=tuple(zone_ids),
                        package_form=scope.package_form,
                    ),
                ),
            )
        get_loop_metrics().counter(LoopMetricName.EXPORT_REQUESTS_TOTAL).add(
            1, {"export_state": "requested", "organization_id": str(authorized.organization_id)}
        )
        return ExportStatus(
            export_id=export_id,
            organization_id=authorized.organization_id,
            plant_id=scope.plant_id,
            zone_ids=scope.zone_ids,
            period_from=period_from,
            period_to=period_to,
            package_form=scope.package_form,
            state=ExportState.REQUESTED,
            progress_percent=0,
            current_artifact=None,
            attempt_count=0,
            failure_code=None,
            requested_at=now,
            started_at=None,
            completed_at=None,
            expires_at=expires_at,
            correlation_id=authorized.correlation_id,
        )

    async def _authorize_zones(
        self, context: ScopeContext, zone_ids: Sequence[uuid.UUID], plant_id: uuid.UUID
    ) -> ScopeContext:
        """``export.create`` sobre **todas** las zonas; luego, que sean de la planta pedida."""
        async with self._database.transaction(context) as transaction:
            rows = (
                await transaction.execute(
                    _ZONES,
                    {
                        "organization_id": context.organization_id,
                        "zone_ids": [str(zone) for zone in zone_ids],
                    },
                )
            ).all()
        plants = {uuid.UUID(str(row.zone_id)): uuid.UUID(str(row.plant_id)) for row in rows}
        if set(plants) != set(zone_ids):
            raise ResourceNotFound()
        # El rol en uso que queda en la solicitud y en la auditoría es el de la primera zona.
        granted = [
            await self._authorizer.authorize(
                context,
                _EXPORT_CREATE,
                Resource.zone(context.organization_id, plants[zone], zone),
            )
            for zone in zone_ids
        ]
        if any(plant != plant_id for plant in plants.values()):
            raise ExportRequestRejected(AccreditationDetailCode.EXPORT_ZONES_SPAN_PLANTS)
        return granted[0]

    async def _check_estimate(
        self,
        context: ScopeContext,
        scope: ExportScope,
        period_from: datetime,
        period_to: datetime,
    ) -> None:
        """Suma los ``size_bytes`` que ya constan en el expediente; nunca lee un objeto."""
        async with self._database.transaction(context) as transaction:
            estimated = (
                await transaction.execute(
                    _ESTIMATE,
                    {
                        "organization_id": context.organization_id,
                        "plant_id": scope.plant_id,
                        "zone_ids": [str(zone) for zone in scope.zone_ids],
                        "period_from": period_from,
                        "period_to": period_to,
                    },
                )
            ).scalar_one()
        if int(estimated) > MAX_ESTIMATED_BYTES:
            raise ExportRequestRejected(AccreditationDetailCode.EXPORT_SCOPE_TOO_LARGE)

    # --- SUP-U05-22 -------------------------------------------------------------------------

    async def status(self, context: ScopeContext, export_id: uuid.UUID) -> ExportStatus:
        if not isinstance(context, ScopeContext) or type(export_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            found = await read_status(transaction, export_id)
        if found is None:
            raise ResourceNotFound()
        for zone in found.zone_ids:
            await self._authorizer.authorize(
                context,
                _EXPORT_CREATE,
                Resource.zone(context.organization_id, found.plant_id, zone),
            )
        return found


def _check_shape(scope: ExportScope) -> None:
    if not isinstance(scope, ExportScope) or not isinstance(scope.package_form, PackageForm):
        raise ExportRequestInvalid()
    zones = scope.zone_ids
    if (
        type(scope.plant_id) is not uuid.UUID
        or not isinstance(zones, tuple)
        or not zones
        or any(type(zone) is not uuid.UUID for zone in zones)
        or len(set(zones)) != len(zones)
    ):
        raise ExportRequestInvalid()
    for moment in (scope.period_from, scope.period_to):
        if not isinstance(moment, datetime) or moment.utcoffset() is None:
            raise ExportRequestInvalid()
    period_from = to_millisecond(scope.period_from)
    period_to = to_millisecond(scope.period_to)
    if period_to <= period_from:
        raise ExportRequestInvalid()
    if len(zones) > MAX_ZONES or period_to - period_from > MAX_PERIOD:
        raise ExportRequestRejected(AccreditationDetailCode.EXPORT_SCOPE_TOO_LARGE)


async def _check_rate(transaction: Transaction, context: ScopeContext, now: datetime) -> None:
    """Los dos límites exactos, contados sobre la tabla con la exclusión de la organización."""
    organization = context.organization_id
    await transaction.execute(_RATE_LOCK, {"key": f"{RATE_LOCK_PREFIX}{organization}"})
    row = (
        await transaction.execute(
            _RATE_COUNT,
            {
                "organization_id": organization,
                "user_id": context.actor.id,
                "since": now - RATE_WINDOW,
            },
        )
    ).one()
    oldest: list[datetime] = []
    if int(row.by_user) >= EXPORTS_PER_USER_PER_HOUR:
        oldest.append(row.oldest_by_user)
    if int(row.by_organization) >= EXPORTS_PER_ORGANIZATION_PER_HOUR:
        oldest.append(row.oldest_by_organization)
    if oldest:
        # Cabe otra en cuanto la más antigua de la ventana salga de ella (``requested_at`` >
        # ahora - 1 h): nunca hay más de las permitidas, así que basta con la más antigua.
        wait = (max(oldest) + RATE_WINDOW - now).total_seconds()
        raise ExportRateLimited(min(MAX_RETRY_AFTER_SECONDS, max(1, math.ceil(wait))))


async def read_status(transaction: Transaction, export_id: uuid.UUID) -> ExportStatus | None:
    """La solicitud (y el resumen de su paquete, si está lista) visible en ``transaction``."""
    row = (await transaction.execute(_READ, {"export_id": export_id})).mappings().first()
    if row is None:
        return None
    package: PackageSummary | None = None
    if row["state"] == ExportState.READY.value:
        found = (await transaction.execute(_PACKAGE, {"export_id": export_id})).mappings().first()
        if found is not None:
            package = _package(dict(found))
    return ExportStatus.from_row(dict(row), package)


def _package(row: Mapping[str, Any]) -> PackageSummary:
    return PackageSummary(
        document_sha256=row["document_sha256"],
        archive_sha256=row["archive_sha256"],
        size_bytes=int(row["size_bytes"]),
        artifact_count=int(row["artifact_count"]),
        checkpoint_count=int(row["checkpoint_count"] or 0),
        verifier_version=row["verifier_version"],
    )
