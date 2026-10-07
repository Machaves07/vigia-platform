"""``catalog.occlusion``: prueba de redundancia por oclusión con evaluación perezosa (LC-GOB-07).

**Alta** (``record``, ``POST /walk-tests/{id}/occlusion-tests``, ``commissioning.run`` sobre la
zona de la sesión), con ``{camera_id, started_at, ended_at, declared_reason_es?}``:

- ``camera_id`` es una cámara del catálogo de la sesión y ``started_at < ended_at ≤ ahora``, con
  una ventana de a lo sumo una hora (si no, ``invalid_request``);
- sin motivo, la prueba nace ``pending`` con ``deadline = ended_at + 5 min`` y se evalúa en el
  acto; solo se admite si la cámara no tiene prueba o su última prueba quedó ``failed``
  (``conflict``);
- con motivo sobre una cámara cuya última prueba sigue ``pending``, la resuelve ``declared`` sin
  crear otra; sin prueba o tras una ``failed``, registra la prueba nueva y la declara en la misma
  transacción. La declaración solo se admite antes de ``deadline`` y sin eventos contados en la
  ventana (BR-GOB-42); si no, ``conflict`` y no queda nada escrito. El motivo pasa la política de
  texto libre (``catalog_free_text_rejected``).

La sesión tiene que estar abierta: ``incomplete`` es ``catalog_walk_test_incomplete`` (la que
cumplió 7 días sin actividad queda ``incomplete``) y ``closed``, ``conflict``; el alta actualiza
``last_activity_at``.

**Reevaluación perezosa** (``reevaluate_pending``, PAT-GOB-REN-07): la invocan ``GET
/zones/{zone_id}/walk-tests/current`` (por ``OcclusionTestsProvider``) y el cierre del acta
(TASK-216). Por cada prueba ``pending`` lee los ``observability_event_received`` de la zona con
``LectorExpediente`` (filtro de zona y ``received_at`` en ``[started_at - 30 s, deadline]``) y
aplica ``resolve``. La resolución se escribe **una vez**: bajo el candado de la fila de la prueba,
solo si sigue ``pending`` (condición «sin resolver»), con su ``occlusion_test_result``
(``source_key = test_id``, cadena de la planta) en la misma transacción. Sin tarea periódica ni
consumidor nuevos.

**Candados** (orden único, también frente a ``catalog.commissioning``): primero la fila de la
sesión (solo el alta y la declaración), después la fila de la prueba y por último la cadena de la
planta (``EscritorExpediente``). La reevaluación no toma el de la sesión.

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.occlusion_repository import (
    OcclusionWriteConflict,
    PostgresOcclusionRepository,
)
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
    WalkTestWriteConflict,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.gates import GateService, LedgerRaceLost, record_id_of
from vigia_platform.catalog.application.walk_test import (
    WalkTestConflict,
    WalkTestRequestInvalid,
    WalkTestUnavailable,
)
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import ZoneRef
from vigia_platform.catalog.domain.coverage import MinimumCoverage
from vigia_platform.catalog.domain.enums import OcclusionVerification
from vigia_platform.catalog.domain.occlusion import (
    WINDOW_TOLERANCE,
    ObservedEvent,
    OcclusionRuleViolated,
    OcclusionTest,
    OcclusionViolation,
    Resolution,
    check_window,
    coverage_of_catalog,
    new_test,
    resolve,
)
from vigia_platform.catalog.domain.texts import has_content
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.catalog.domain.walk_test import (
    WalkTestRuleViolated,
    WalkTestSession,
    WalkTestViolation,
    check_operable,
    expire_if_inactive,
)
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.reader import (
    MAX_PAGE_SIZE,
    LectorExpediente,
    LedgerFilters,
    PageRequest,
)
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    RecordScope,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "OBSERVABILITY_EVENT",
    "OCCLUSION_TEST_RESULT",
    "OcclusionService",
    "occlusion_test_json",
]

OCCLUSION_TEST_RESULT: Final = "occlusion_test_result"
"""Tipo de registro de la resolución (``source_key = test_id``)."""
OBSERVABILITY_EVENT: Final = "observability_event_received"
"""Tipo de registro de los eventos que la prueba correlaciona."""
_DECLARED_REASON: Final = FreeTextField(OCCLUSION_TEST_RESULT, "/declared_reason_es", 10, 500)
_JUST_AFTER: Final = timedelta(microseconds=1)
"""``received_before`` es exclusivo: la lectura incluye lo recibido justo en ``deadline``."""


def occlusion_test_json(test: OcclusionTest) -> dict[str, Any]:
    """La prueba en la forma de la respuesta y de ``occlusion_tests`` de la sesión.

    ``failure_reason`` dice por qué quedó ``failed`` (``no_observability_events_in_window`` o
    ``redundancy_not_verified``); el acta distingue ``verified``, ``declared`` y ``failed``.
    """
    failure = test.failure_reason
    return {
        "test_id": str(test.test_id),
        "camera_id": str(test.camera_id),
        "started_at": format_timestamp(test.started_at),
        "ended_at": format_timestamp(test.ended_at),
        "deadline": format_timestamp(test.deadline),
        "verification": OcclusionVerification(test.verification).value,
        "correlated_event_ids": [str(e) for e in test.correlated_event_ids or ()],
        "declared_reason_es": test.declared_reason_es,
        "failure_reason": None if failure is None else failure.value,
        "recorded_by": str(test.recorded_by),
    }


@dataclass(frozen=True, slots=True)
class _Expired:
    """La sesión cumplió 7 días sin actividad: se confirma ``incomplete`` y se rechaza."""


_EXPIRED: Final = _Expired()


def _rejected(violation: OcclusionViolation) -> Exception:
    if violation in (OcclusionViolation.WINDOW_INVALID, OcclusionViolation.CAMERA_NOT_IN_CATALOG):
        return WalkTestRequestInvalid("prueba de oclusión fuera de los límites")
    return WalkTestConflict()


def _test_id(test: OcclusionTest | None) -> uuid.UUID | None:
    return None if test is None else test.test_id


def _session_rejected(violation: WalkTestViolation) -> Exception:
    if violation is WalkTestViolation.INCOMPLETE:
        return CatalogRejected(CatalogDetailCode.WALK_TEST_INCOMPLETE)
    return WalkTestConflict()


@repository
class OcclusionService:
    """``catalog.occlusion``: alta, declaración y reevaluación perezosa de las pruebas."""

    def __init__(
        self,
        *,
        repository: PostgresOcclusionRepository,
        sessions: PostgresWalkTestRepository,
        catalog: PostgresCatalogRepository,
        gates: GateService,
        reader: LectorExpediente,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._catalog = catalog
        self._gates = gates
        self._reader = reader
        self._database = database
        self._writer = writer
        self._free_text = free_text
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "OcclusionService()"

    def _now(self) -> datetime:
        return utc_instant(self._clock.now())

    def _reason(self, value: object) -> str:
        if not isinstance(value, str):
            raise WalkTestRequestInvalid("el motivo es texto")
        try:
            text = self._free_text.apply(value, _DECLARED_REASON)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None
        if not has_content(text):  # solo espacios, signos o invisibles: no es un motivo
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        return text

    async def _run[T](
        self, context: ScopeContext, body: Callable[[Transaction], Awaitable[T]]
    ) -> T:
        """``body`` en una transacción; traduce reglas y carreras a sus errores."""
        try:
            async with self._database.transaction(context) as transaction:
                return await body(transaction)
        except OcclusionRuleViolated as violation:
            raise _rejected(violation.violation) from None
        except WalkTestRuleViolated as violation:
            raise _session_rejected(violation.violation) from None
        except (OcclusionWriteConflict, WalkTestWriteConflict):
            raise WalkTestConflict from None
        except LedgerRaceLost:
            raise WalkTestUnavailable("occlusion_test_race") from None

    # --- Lectura de la sesión, el catálogo y los eventos -----------------------------------------

    async def _coverage(
        self, context: ScopeContext, session: WalkTestSession
    ) -> MinimumCoverage | None:
        """La cobertura mínima del catálogo con el que se abrió la sesión (o ``None``)."""
        version = await self._catalog.version(context, session.zone_id, session.catalog_version)
        return None if version is None else coverage_of_catalog(version.payload)

    async def _events(
        self, context: ScopeContext, zone_id: uuid.UUID, test: OcclusionTest
    ) -> tuple[ObservedEvent, ...]:
        """Los ``observability_event_received`` **de la zona** recibidos en la ventana de lectura
        (``LectorExpediente``, con su entrada ``ledger_read`` por página)."""
        filters = LedgerFilters(
            record_types=(OBSERVABILITY_EVENT,),
            zone_id=zone_id,
            received_from=test.started_at - WINDOW_TOLERANCE,
            received_before=test.deadline + _JUST_AFTER,
        )
        events: list[ObservedEvent] = []
        page = PageRequest(size=MAX_PAGE_SIZE)
        while True:
            listed = await self._reader.list(context, filters, page)
            events += (
                ObservedEvent.of_content(record.document(), record.received_at)
                for record in listed.items
            )
            if listed.next_cursor is None:
                return tuple(events)
            page = PageRequest(size=MAX_PAGE_SIZE, after=listed.next_cursor)

    # --- Resolución única ------------------------------------------------------------------------

    async def _resolve_in(
        self,
        transaction: Transaction,
        context: ScopeContext,
        zone: tuple[uuid.UUID, uuid.UUID],
        test_id: uuid.UUID,
        decide: Callable[[OcclusionTest], Resolution | None],
    ) -> OcclusionTest:
        """Bajo el candado de la fila: si la prueba sigue ``pending`` y ``decide`` la resuelve, la
        resolución y su ``occlusion_test_result`` en ``transaction``. Si ya estaba resuelta, la
        devuelve tal cual (condición «sin resolver»)."""
        locked = await self._repository.lock(transaction, test_id)
        if locked is None:
            raise ResourceNotFound()
        if locked.resolved:
            return locked
        resolution = decide(locked)
        if resolution is None:
            return locked
        record_id = uuid7(self._clock, self._random_bytes)
        resolved = locked.resolved_with(resolution, record_id)
        await self._repository.resolve(transaction, resolved)
        plant_id, zone_id = zone
        written = await self._writer.write(
            context,
            OCCLUSION_TEST_RESULT,
            resolved.record_content(),
            scope=RecordScope(plant_id=plant_id, zone_id=zone_id),
            transaction=transaction,
            record_id=record_id,
        )
        if record_id_of(written) != record_id:
            raise LedgerRaceLost
        return resolved

    async def _reevaluate(
        self,
        context: ScopeContext,
        session: WalkTestSession,
        test: OcclusionTest,
        coverage: MinimumCoverage | None,
        now: datetime,
    ) -> OcclusionTest:
        events = await self._events(context, session.zone_id, test)
        if resolve(test, events, coverage, now) is None:
            return test

        async def body(transaction: Transaction) -> OcclusionTest:
            return await self._resolve_in(
                transaction,
                context,
                (session.plant_id, session.zone_id),
                test.test_id,
                lambda locked: resolve(locked, events, coverage, now),
            )

        return await self._run(context, body)

    async def reevaluate_pending(
        self, context: ScopeContext, session: WalkTestSession, now: datetime
    ) -> tuple[OcclusionTest, ...]:
        """Reevalúa las pruebas ``pending`` de la sesión en ``now`` (PAT-GOB-REN-07).

        Idempotente y monótona: una prueba resuelta no cambia. Devuelve las pruebas de la sesión
        tal como quedaron, en orden de registro. ``context`` debe estar autorizado sobre la zona
        de la sesión (lo está el de ``GET …/walk-tests/current`` y el del cierre del acta).
        """
        if not isinstance(session, WalkTestSession):
            raise TypeError("session debe ser WalkTestSession")
        writer_context = with_unit(context, ActorUnit.U03)
        moment = utc_instant(now)
        async with self._database.transaction(writer_context) as transaction:
            pending = await self._repository.pending(transaction, session.session_id)
        if pending:
            coverage = await self._coverage(writer_context, session)
            for test in pending:
                await self._reevaluate(writer_context, session, test, coverage, moment)
        async with self._database.transaction(writer_context) as transaction:
            return await self._repository.tests(transaction, session.session_id)

    # --- Proveedor de ``GET …/walk-tests/current`` -----------------------------------------------

    async def occlusion_tests(
        self, transaction: Transaction, session: WalkTestSession
    ) -> tuple[Mapping[str, Any], ...]:
        """Las pruebas de la sesión en su forma JSON, en la transacción de la lectura."""
        tests = await self._repository.tests(transaction, session.session_id)
        return tuple(occlusion_test_json(test) for test in tests)

    # --- Alta y declaración ----------------------------------------------------------------------

    async def _visible(
        self, context: ScopeContext, session_id: uuid.UUID
    ) -> tuple[WalkTestSession, ZoneRef, ScopeContext]:
        """La sesión, su zona y el contexto con ``commissioning.run`` sobre ella; si no, igual que
        inexistente (``ResourceNotFound``)."""
        if not isinstance(context, ScopeContext) or type(session_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            session = await self._sessions.session(transaction, session_id)
        if session is None:
            raise ResourceNotFound()
        zone, authorized = await self._gates.zone(
            context, session.zone_id, PermissionKey.COMMISSIONING_RUN
        )
        return session, zone, authorized

    async def record(
        self,
        context: ScopeContext,
        session_id: uuid.UUID,
        camera_id: object,
        started_at: object,
        ended_at: object,
        declared_reason_es: object = None,
    ) -> OcclusionTest:
        """Registra la prueba de la cámara o declara su prueba ``pending``; la devuelve evaluada.

        ``ResourceNotFound``, ``WalkTestRequestInvalid`` (cámara ajena o ventana inválida),
        ``WalkTestConflict`` (prueba no repetible o declaración no admitida),
        ``CatalogRejected`` (``walk_test_incomplete``, ``free_text_rejected``) o
        ``WalkTestUnavailable``; en un rechazo no queda nada escrito.
        """
        if type(camera_id) is not uuid.UUID:
            raise WalkTestRequestInvalid("camera_id es un UUID")
        camera = camera_id
        reason = None if declared_reason_es is None else self._reason(declared_reason_es)
        try:
            start, end = check_window(started_at, ended_at, self._now())
        except OcclusionRuleViolated as violation:
            raise _rejected(violation.violation) from None
        session, zone, authorized = await self._visible(context, session_id)
        writer_context = with_unit(authorized, ActorUnit.U03)
        version = await self._catalog.version(
            writer_context, session.zone_id, session.catalog_version
        )
        coverage = None if version is None else coverage_of_catalog(version.payload)
        if coverage is None or camera not in coverage.camera_ids:
            raise _rejected(OcclusionViolation.CAMERA_NOT_IN_CATALOG)
        # Primero, lo que la mirada perezosa ya puede resolver (una pending vencida queda failed).
        await self.reevaluate_pending(writer_context, session, self._now())
        async with self._database.transaction(writer_context) as transaction:
            seen = await self._repository.latest(transaction, session.session_id, camera)
        by = uuid.UUID(str(writer_context.actor.id))
        if seen is not None and seen.verification is not OcclusionVerification.FAILED:
            if reason is None or seen.resolved:
                raise WalkTestConflict
            target, created = seen, False
        else:
            target = new_test(
                test_id=uuid7(self._clock, self._random_bytes),
                organization_id=session.organization_id,
                plant_id=session.plant_id,
                session_id=session.session_id,
                camera_id=camera,
                started_at=start,
                ended_at=end,
                recorded_by=by,
            )
            created = True
        events: tuple[ObservedEvent, ...] = ()
        if reason is not None:
            events = await self._events(writer_context, session.zone_id, target)
            now = self._now()
            outcome = resolve(target, events, coverage, now, reason)
            if outcome is None or outcome.verification is not OcclusionVerification.DECLARED:
                raise _rejected(OcclusionViolation.DECLARATION_NOT_ADMITTED)

        async def locked(transaction: Transaction) -> OcclusionTest | _Expired:
            current = await self._sessions.lock_session(transaction, session.session_id)
            if current is None or current.zone_id != zone.zone_id:
                raise ResourceNotFound()
            now = self._now()
            effective = expire_if_inactive(current, now)
            if effective is not current:
                await self._sessions.mark_incomplete(transaction, current)
                return _EXPIRED
            check_operable(effective)
            # Bajo el candado de la sesión: la última prueba de la cámara sigue siendo la vista.
            latest = await self._repository.latest(transaction, session.session_id, camera)
            if _test_id(latest) != _test_id(seen):
                raise _rejected(OcclusionViolation.TEST_NOT_REPEATABLE)
            if created:
                await self._repository.insert(transaction, target)
            result = target
            if reason is not None:
                result = await self._resolve_in(
                    transaction,
                    writer_context,
                    (zone.plant_id, zone.zone_id),
                    target.test_id,
                    lambda pending: resolve(pending, events, coverage, now, reason),
                )
                if result.verification is not OcclusionVerification.DECLARED:
                    raise _rejected(OcclusionViolation.DECLARATION_NOT_ADMITTED)
            await self._sessions.touch(transaction, session.session_id, now)
            return result

        outcome_test = await self._run(writer_context, locked)
        if isinstance(outcome_test, _Expired):
            raise CatalogRejected(CatalogDetailCode.WALK_TEST_INCOMPLETE)
        if outcome_test.resolved:
            return outcome_test
        # Recién nacida pending: se evalúa en el acto (puede salir ya resuelta).
        return await self._reevaluate(writer_context, session, outcome_test, coverage, self._now())
