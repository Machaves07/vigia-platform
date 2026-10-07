"""``catalog.records``: muestras de exposición del acta (LC-GOB-08; tramo 3a de NFR-GOB-70).

``POST /walk-tests/{id}/exposure-samples`` (``commissioning.run`` sobre la zona de la sesión),
con ``{pass_id, fetched_at, displayed_at}`` medidos por U-05 con el **reloj del navegador**, una
muestra por pase mostrado:

- ``displayed_at >= fetched_at``, con zona horaria (si no, ``invalid_request``);
- un pase que no es de la sesión: ``catalog_pass_not_found``;
- la **primera** muestra del pase se guarda (``catalog.exposure_sample`` ⛓); repetirla devuelve la
  primera **sin efecto** (ni fila nueva ni actividad), también si llegan a la vez
  (``ON CONFLICT (pass_id) DO NOTHING``);
- una sesión ``incomplete`` (o que ya cumplió 7 días sin actividad) es
  ``catalog_walk_test_incomplete`` y una ``closed``, ``conflict``: su acta ya no cambia.

La muestra no toma el candado de la sesión ni toca ``last_activity_at``: la pinta la consola, no
la registra el instalador. Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from vigia_platform.catalog.adapters.postgres.commissioning_record_repository import (
    PostgresCommissioningRecordRepository,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.gates import GateService
from vigia_platform.catalog.application.walk_test import WalkTestConflict, WalkTestRequestInvalid
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.enums import WalkTestStatus
from vigia_platform.catalog.domain.latency import ExposureSample
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.catalog.domain.walk_test import expire_if_inactive
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = ["ExposureRecorded", "ExposureService"]


@dataclass(frozen=True, slots=True)
class ExposureRecorded:
    """La muestra del pase y si esta petición la creó (``False``: era la primera, sin efecto)."""

    sample: ExposureSample
    created: bool


def _instant(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise WalkTestRequestInvalid("las marcas del navegador llevan zona horaria")
    return to_millisecond(utc_instant(value))


@repository
class ExposureService:
    """Muestras de exposición de una sesión de walk-test."""

    def __init__(
        self,
        *,
        repository: PostgresCommissioningRecordRepository,
        sessions: PostgresWalkTestRepository,
        gates: GateService,
        database: LedgerDatabase,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._gates = gates
        self._database = database
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "ExposureService()"

    async def record(
        self,
        context: ScopeContext,
        session_id: uuid.UUID,
        pass_id: object,
        fetched_at: object,
        displayed_at: object,
    ) -> ExposureRecorded:
        """Guarda la primera muestra del pase o devuelve la que ya tenía.

        ``ResourceNotFound`` (sesión inexistente o fuera de alcance), ``WalkTestRequestInvalid``,
        ``CatalogRejected`` (``pass_not_found``, ``walk_test_incomplete``) o ``WalkTestConflict``
        (sesión cerrada); en un rechazo no queda nada escrito.
        """
        if not isinstance(context, ScopeContext) or type(session_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            session = await self._sessions.session(transaction, session_id)
        if session is None:
            raise ResourceNotFound()
        zone, authorized = await self._gates.zone(
            context, session.zone_id, PermissionKey.COMMISSIONING_RUN
        )
        # Tras autorizar: fuera del alcance responde ``not_found`` sea cual sea el cuerpo.
        if type(pass_id) is not uuid.UUID:
            raise WalkTestRequestInvalid("pass_id es un UUID")
        fetched, displayed = _instant(fetched_at), _instant(displayed_at)
        if displayed < fetched:
            raise WalkTestRequestInvalid("displayed_at no es anterior a fetched_at")
        writer_context = with_unit(authorized, ActorUnit.U03)
        async with self._database.transaction(writer_context) as transaction:
            current = await self._sessions.session(transaction, session_id)
            if current is None or current.zone_id != zone.zone_id:
                raise ResourceNotFound()
            existing = await self._repository.sample(transaction, session_id, pass_id)
            if existing is not None:
                return ExposureRecorded(existing, created=False)
            if not await self._repository.pass_in_session(transaction, session_id, pass_id):
                raise CatalogRejected(CatalogDetailCode.PASS_NOT_FOUND)
            now = to_millisecond(self._clock.now())
            effective = expire_if_inactive(current, now)
            if effective.status is WalkTestStatus.INCOMPLETE:
                raise CatalogRejected(CatalogDetailCode.WALK_TEST_INCOMPLETE)
            if not effective.is_open:
                raise WalkTestConflict
            sample = ExposureSample(
                sample_id=uuid7(self._clock, self._random_bytes),
                organization_id=current.organization_id,
                plant_id=current.plant_id,
                session_id=session_id,
                pass_id=pass_id,
                fetched_at=fetched,
                displayed_at=displayed,
                recorded_by=uuid.UUID(str(writer_context.actor.id)),
                recorded_at=now,
            )
            if await self._repository.insert_sample(transaction, sample):
                return ExposureRecorded(sample, created=True)
            # Otra petición del mismo pase llegó antes: la suya es la primera.
            first = await self._repository.sample(transaction, session_id, pass_id)
        if first is None:  # pragma: no cover - la clave única garantiza que existe
            raise WalkTestConflict
        return ExposureRecorded(first, created=False)
