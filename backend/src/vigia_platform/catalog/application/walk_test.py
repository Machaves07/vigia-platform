"""``catalog.commissioning``: sesión de walk-test, pasos, pases y reapertura (LC-GOB-06).

**Apertura** (``open``, ``POST /zones/{zone_id}/walk-tests``, ``commissioning.run`` sobre la
zona), con las guardas en este orden (interfaces §3.3, BR-GOB-35): montaje ``approved`` leído de
``catalog.gates`` (``mounting_gate_pending``), nodo asignado por
``IdentityQueryPort.assigned_node`` (``node_not_assigned``), ninguna sesión ``in_progress`` ni
``reopened`` en la zona (``walk_test_in_progress``) y ``passes_per_cell >= 3``
(``passes_below_minimum``). La matriz sale de ``derive_matrix`` sobre la versión vigente del
catálogo y queda con esa versión. «Una sesión abierta por zona» la garantiza el índice único
parcial: la apertura que pierde la carrera no escribe nada y responde ``walk_test_in_progress``.
Una sesión abierta que ya lleva 7 días sin actividad pasa a ``incomplete`` en la misma
transacción y deja de bloquear.

**Operaciones de la sesión** (pasos, cierre de paso, pases), en una transacción que toma primero
el candado de la fila de la sesión (``lock_session``): sobre una sesión ``incomplete``
``walk_test_incomplete``; sobre una ``closed``, ``conflict``. Toda operación válida actualiza
``last_activity_at``. Si la sesión cumplió 7 días sin actividad, la operación la deja
``incomplete`` (se confirma) y responde ``walk_test_incomplete``.

- ``start_step``: ``started_at`` del servidor; el responsable es el propio usuario de la sesión o
  un usuario de la organización con un rol vigente sobre la zona (si no, ``invalid_request``).
- ``close_step``: ``ended_at`` del servidor una sola vez (condicional a ``ended_at IS NULL``); la
  corrección se anexa con motivo, autor e instante y nunca sustituye las marcas; escribe
  ``commissioning_step`` (``source_key = step_id``). Un paso ya cerrado es ``conflict``.
- ``record_pass``: solo anexar; ``row_id`` de la matriz de la sesión (si no, ``invalid_request``);
  ``evidence_ref`` se guarda tal cual (su clip lo comprueba el cierre del acta, TASK-216).

**Reapertura** (``reopen``): solo desde ``incomplete`` (efectivo: también una abierta con 7 días
sin actividad), con motivo por la política de texto libre; pasa a ``reopened``, conserva todo y
deja el motivo, quién y cuándo en la sesión y en la auditoría (``walk_test_reopened``). Si la zona
ya tiene otra sesión abierta, ``walk_test_in_progress``.

**Lectura** (``current``, ``catalog.read``): la sesión abierta de la zona o la ``incomplete`` más
reciente, con su estado efectivo, matriz, pasos, pases y su conteo por fila, horas (``total_hours``
y ``steps_summary``, nunca por responsable) y pruebas de oclusión de un proveedor inyectable
(``catalog.occlusion``, TASK-215), que antes reevalúa las ``pending``. Las consultas sobre la
sesión no crecen con las filas.

**Candados** (orden único): primero la fila de la sesión; después la cadena de la planta
(``EscritorExpediente``, solo el cierre de paso) y la de auditoría de la organización (solo la
reapertura). La apertura no toma candados: la decide el índice único.

Ninguna operación agrega u ordena horas por responsable (H-53, NFR-GOB-41). Ningún paso lee la
hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

from sqlalchemy import exc as sa_exc
from vigia_contracts.models.enumerations import GateStatus

from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.walk_test_repository import (
    PostgresWalkTestRepository,
    WalkTestWriteConflict,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.application.gates import GateService, LedgerRaceLost, record_id_of
from vigia_platform.catalog.application.scope_record import AssignedNodeLookup
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.catalog_version import ZoneRef
from vigia_platform.catalog.domain.enums import PassResult, StepKind
from vigia_platform.catalog.domain.gates import MAX_REASON_CHARS, MIN_REASON_CHARS
from vigia_platform.catalog.domain.steps import (
    CorrectionRequest,
    StepAlreadyClosed,
    StepHours,
    StepRequestInvalid,
    WalkTestStep,
    close_step,
    steps_summary,
    total_hours,
)
from vigia_platform.catalog.domain.texts import has_content
from vigia_platform.catalog.domain.time_windows import utc_instant
from vigia_platform.catalog.domain.walk_test import (
    PassCounts,
    WalkTestPass,
    WalkTestRuleViolated,
    WalkTestSession,
    WalkTestViolation,
    check_operable,
    check_passes_per_cell,
    expire_if_inactive,
    open_session,
    pass_counts,
    reopen,
)
from vigia_platform.identity.application.hierarchy import Recipient
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    RecordScope,
    violated_constraint,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7

__all__ = [
    "COMMISSIONING_STEP",
    "ONE_OPEN_PER_ZONE",
    "NoOcclusionTests",
    "OcclusionTestsProvider",
    "ResponsibleLookup",
    "WalkTestConflict",
    "WalkTestRequestInvalid",
    "WalkTestService",
    "WalkTestUnavailable",
    "WalkTestView",
]

COMMISSIONING_STEP: Final = "commissioning_step"
"""Tipo de registro del cierre de cada paso (``source_key = step_id``)."""
ONE_OPEN_PER_ZONE: Final = "walk_test_session_one_open_per_zone"
"""Índice único parcial de gob_0017: una sesión ``in_progress`` o ``reopened`` por zona."""
_UNIQUE_VIOLATION: Final = "23505"
_CORRECTION_REASON: Final = FreeTextField(
    COMMISSIONING_STEP, "/correction/reason_es", MIN_REASON_CHARS, MAX_REASON_CHARS
)
_REOPEN_REASON: Final = FreeTextField(
    "walk_test_session", "/reopen_reason_es", MIN_REASON_CHARS, MAX_REASON_CHARS
)


# --- Puertos y errores ---------------------------------------------------------------------------


class ResponsibleLookup(Protocol):
    """``IdentityQueryPort.users_by_role_and_scope`` de U-02 (LC-NUC-05)."""

    async def users_by_role_and_scope(
        self,
        context: ScopeContext,
        roles: Iterable[Role],
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
    ) -> tuple[Recipient, ...]: ...


class OcclusionTestsProvider(Protocol):
    """Las pruebas de oclusión de la sesión para ``GET …/walk-tests/current`` (las entrega
    ``catalog.occlusion``, TASK-215): ``reevaluate_pending`` antes de la lectura, en sus propias
    transacciones (PAT-GOB-REN-07), y ``occlusion_tests`` en la transacción de la lectura, ya en
    su forma JSON."""

    async def reevaluate_pending(
        self, context: ScopeContext, session: WalkTestSession, now: datetime
    ) -> object: ...

    async def occlusion_tests(
        self, transaction: Transaction, session: WalkTestSession
    ) -> tuple[Mapping[str, Any], ...]: ...


@repository
class NoOcclusionTests:
    """Proveedor sin pruebas de oclusión (para montar la sesión sin ``catalog.occlusion``).
    Recibe el contexto o la transacción de la lectura, así que, como todo repositorio, exige su
    contexto (PR-NUC-02)."""

    async def reevaluate_pending(
        self, context: ScopeContext, session: WalkTestSession, now: datetime
    ) -> object:
        return ()

    async def occlusion_tests(
        self, transaction: Transaction, session: WalkTestSession
    ) -> tuple[Mapping[str, Any], ...]:
        return ()


class WalkTestConflict(Exception):
    """La sesión o el paso no están en el estado que la operación exige: ``conflict``."""

    api_code: Final = ApiErrorCode.CONFLICT

    def __init__(self) -> None:
        super().__init__("la sesión o el paso no están en el estado que la operación exige")


class WalkTestRequestInvalid(Exception):
    """El cuerpo es incoherente por sí mismo (fila ajena, responsable, corrección): no escribe."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str = "petición del walk-test fuera de los límites") -> None:
        super().__init__(reason)


class WalkTestUnavailable(ExternalDependencyDown):
    """Transitorio: el expediente ya tenía el registro del paso (carrera perdida)."""


# --- Resultados ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkTestView:
    """``GET …/walk-tests/current``: la sesión con su estado efectivo y lo registrado."""

    session: WalkTestSession
    steps: tuple[WalkTestStep, ...]
    passes: tuple[WalkTestPass, ...]
    counts: Mapping[uuid.UUID, PassCounts]
    total_duration_ms: int
    summary: tuple[StepHours, ...]
    occlusion_tests: tuple[Mapping[str, Any], ...]

    @classmethod
    def of(
        cls,
        session: WalkTestSession,
        steps: tuple[WalkTestStep, ...] = (),
        passes: tuple[WalkTestPass, ...] = (),
        occlusion_tests: tuple[Mapping[str, Any], ...] = (),
    ) -> WalkTestView:
        return cls(
            session=session,
            steps=steps,
            passes=passes,
            counts=pass_counts(session.matrix_rows, passes),
            total_duration_ms=total_hours(steps),
            summary=steps_summary(steps),
            occlusion_tests=occlusion_tests,
        )


@dataclass(frozen=True, slots=True)
class _Expired:
    """La operación encontró la sesión vencida: se confirma ``incomplete`` y se rechaza."""


_EXPIRED: Final = _Expired()


def _rejected(violation: WalkTestViolation) -> Exception:
    if violation is WalkTestViolation.PASSES_BELOW_MINIMUM:
        return CatalogRejected(CatalogDetailCode.PASSES_BELOW_MINIMUM)
    if violation is WalkTestViolation.INCOMPLETE:
        return CatalogRejected(CatalogDetailCode.WALK_TEST_INCOMPLETE)
    if violation in (WalkTestViolation.CLOSED, WalkTestViolation.NOT_INCOMPLETE):
        return WalkTestConflict()
    return WalkTestRequestInvalid()


# --- Servicio ------------------------------------------------------------------------------------


@repository
class WalkTestService:
    """``catalog.commissioning``: sesión, pasos, pases y reapertura del walk-test."""

    def __init__(
        self,
        *,
        repository: PostgresWalkTestRepository,
        catalog: PostgresCatalogRepository,
        gates: GateService,
        nodes: AssignedNodeLookup,
        identity: ResponsibleLookup,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        audit: AuditWriter,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        occlusions: OcclusionTestsProvider | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._catalog = catalog
        self._gates = gates
        self._nodes = nodes
        self._identity = identity
        self._database = database
        self._writer = writer
        self._audit = audit
        self._free_text = free_text
        self._clock = clock
        self._occlusions: OcclusionTestsProvider = occlusions or NoOcclusionTests()
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "WalkTestService()"

    def _now(self) -> datetime:
        return utc_instant(self._clock.now())

    def _text(self, value: object, field: FreeTextField) -> str:
        if not isinstance(value, str):
            raise WalkTestRequestInvalid("el motivo es texto")
        try:
            text = self._free_text.apply(value, field)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None
        if not has_content(text):  # solo espacios, signos o invisibles: no es un motivo
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        return text

    async def _run[T](
        self, context: ScopeContext, body: Callable[[Transaction], Awaitable[T]]
    ) -> T:
        """``body`` en una transacción; traduce reglas, carreras y el índice a sus errores."""
        try:
            async with self._database.transaction(context) as transaction:
                return await body(transaction)
        except WalkTestRuleViolated as violation:
            raise _rejected(violation.violation) from None
        except (WalkTestWriteConflict, StepAlreadyClosed):
            raise WalkTestConflict from None
        except StepRequestInvalid:
            raise WalkTestRequestInvalid() from None
        except LedgerRaceLost:
            raise WalkTestUnavailable("walk_test_step_race") from None
        except sa_exc.IntegrityError as error:
            if violated_constraint(error, _UNIQUE_VIOLATION) == ONE_OPEN_PER_ZONE:
                raise CatalogRejected(CatalogDetailCode.WALK_TEST_IN_PROGRESS) from None
            raise

    async def _visible(
        self, context: ScopeContext, session_id: uuid.UUID, key: PermissionKey
    ) -> tuple[WalkTestSession, ZoneRef, ScopeContext]:
        """La sesión, su zona y el contexto autorizado con ``key`` sobre ella; si no, igual que
        inexistente (``ResourceNotFound``)."""
        if not isinstance(context, ScopeContext) or type(session_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            session = await self._repository.session(transaction, session_id)
        if session is None:
            raise ResourceNotFound()
        zone, authorized = await self._gates.zone(context, session.zone_id, key)
        return session, zone, authorized

    # --- Apertura --------------------------------------------------------------------------------

    async def open(
        self, context: ScopeContext, zone_id: uuid.UUID, passes_per_cell: object
    ) -> WalkTestSession:
        """Abre la sesión ``initial`` de la zona; la devuelve tras confirmar.

        ``ResourceNotFound``, ``CatalogRejected`` (``mounting_gate_pending``,
        ``node_not_assigned``, ``walk_test_in_progress``, ``passes_below_minimum``),
        ``WalkTestRequestInvalid`` o ``WalkTestConflict`` (zona sin estándares en su catálogo);
        en todos, nada queda escrito.
        """
        zone, authorized = await self._gates.zone(context, zone_id, PermissionKey.COMMISSIONING_RUN)
        async with self._database.transaction(authorized) as transaction:
            state = await self._gates.state_in(transaction, zone)
        if state.mounting.status is not GateStatus.APPROVED:
            raise CatalogRejected(CatalogDetailCode.MOUNTING_GATE_PENDING)
        node = await self._nodes.assigned_node(authorized, zone.zone_id)
        if node is None:
            raise CatalogRejected(CatalogDetailCode.NODE_NOT_ASSIGNED)
        writer_context = with_unit(authorized, ActorUnit.U03)
        session_id = uuid7(self._clock, self._random_bytes)

        async def body(transaction: Transaction) -> WalkTestSession:
            now = self._now()
            existing = await self._repository.open_for_zone(transaction, zone.zone_id)
            if existing is not None:
                if expire_if_inactive(existing, now).is_open:
                    raise CatalogRejected(CatalogDetailCode.WALK_TEST_IN_PROGRESS)
                # Vencida (7 días sin actividad): incomplete, sin borrar nada, y deja de bloquear.
                if not await self._repository.mark_incomplete(transaction, existing):
                    raise CatalogRejected(CatalogDetailCode.WALK_TEST_IN_PROGRESS)
            passes = check_passes_per_cell(passes_per_cell)
            version = await self._catalog.current(transaction, zone.zone_id)
            if version is None:
                raise WalkTestConflict
            session = open_session(
                session_id=session_id,
                organization_id=zone.organization_id,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                node_id=node.node_id,
                catalog_version=version.catalog_version,
                catalog=version.payload,
                passes_per_cell=passes,
                at=now,
            )
            if not session.matrix_rows:  # un catálogo sin estándares no tiene nada que medir
                raise WalkTestConflict
            if not await self._repository.insert_open(transaction, session):
                raise CatalogRejected(CatalogDetailCode.WALK_TEST_IN_PROGRESS)
            return session

        return await self._run(writer_context, body)

    # --- Operaciones de la sesión ----------------------------------------------------------------

    async def _in_session[T](
        self,
        context: ScopeContext,
        session_id: uuid.UUID,
        body: Callable[
            [Transaction, ScopeContext, ZoneRef, WalkTestSession, datetime], Awaitable[T]
        ],
        before: Callable[[ZoneRef, ScopeContext], Awaitable[None]] | None = None,
    ) -> T:
        """``body`` bajo el candado de la sesión abierta, con ``last_activity_at`` actualizado.

        ``before`` corre tras autorizar y antes de la transacción (consultas a otros módulos).
        """
        _, zone, authorized = await self._visible(
            context, session_id, PermissionKey.COMMISSIONING_RUN
        )
        if before is not None:
            await before(zone, authorized)
        writer_context = with_unit(authorized, ActorUnit.U03)

        async def locked(transaction: Transaction) -> T | _Expired:
            session = await self._repository.lock_session(transaction, session_id)
            if session is None or session.zone_id != zone.zone_id:
                raise ResourceNotFound()
            now = self._now()
            effective = expire_if_inactive(session, now)
            if effective is not session:
                await self._repository.mark_incomplete(transaction, session)
                return _EXPIRED
            check_operable(effective)
            result = await body(transaction, writer_context, zone, effective, now)
            await self._repository.touch(transaction, session_id, now)
            return result

        outcome = await self._run(writer_context, locked)
        if isinstance(outcome, _Expired):
            raise CatalogRejected(CatalogDetailCode.WALK_TEST_INCOMPLETE)
        return outcome

    async def start_step(
        self,
        context: ScopeContext,
        session_id: uuid.UUID,
        step_kind: object,
        responsible_user_id: object,
    ) -> WalkTestStep:
        """Abre un paso con ``started_at`` del servidor (BR-GOB-44)."""
        try:
            kind = StepKind(str(step_kind))
        except ValueError:
            raise WalkTestRequestInvalid("step_kind fuera de la lista cerrada") from None
        if type(responsible_user_id) is not uuid.UUID:
            raise WalkTestRequestInvalid("responsible_user_id es un UUID")
        responsible = responsible_user_id

        async def check_responsible(zone: ZoneRef, authorized: ScopeContext) -> None:
            if responsible == uuid.UUID(str(authorized.actor.id)):
                return  # el propio usuario: su alcance sobre la zona ya está autorizado
            holders = await self._identity.users_by_role_and_scope(
                authorized, tuple(Role), ScopeLevel.ZONE, zone.zone_id
            )
            if responsible not in {holder.user_id for holder in holders}:
                raise WalkTestRequestInvalid("el responsable no tiene alcance sobre la zona")

        async def body(
            transaction: Transaction,
            writer_context: ScopeContext,
            zone: ZoneRef,
            session: WalkTestSession,
            now: datetime,
        ) -> WalkTestStep:
            step = WalkTestStep(
                step_id=uuid7(self._clock, self._random_bytes),
                organization_id=session.organization_id,
                plant_id=session.plant_id,
                session_id=session.session_id,
                step_kind=kind,
                responsible_user_id=responsible,
                started_at=now,
            )
            await self._repository.insert_step(transaction, step)
            return step

        return await self._in_session(context, session_id, body, before=check_responsible)

    async def close_step(
        self,
        context: ScopeContext,
        session_id: uuid.UUID,
        step_id: uuid.UUID,
        correction: CorrectionRequest | None = None,
    ) -> WalkTestStep:
        """Cierra el paso con ``ended_at`` del servidor, una vez, y escribe ``commissioning_step``.

        La corrección se anexa (BR-GOB-45): su motivo pasa la política de texto libre
        (``free_text_rejected``) y una corrección incoherente es ``invalid_request``.
        """
        if type(step_id) is not uuid.UUID:
            raise ResourceNotFound()
        request: CorrectionRequest | None = None
        if correction is not None:
            if not isinstance(correction, CorrectionRequest):
                raise TypeError("correction debe ser CorrectionRequest")
            request = CorrectionRequest(
                reason_es=self._text(correction.reason_es, _CORRECTION_REASON),
                started_at=correction.started_at,
                ended_at=correction.ended_at,
            )

        async def body(
            transaction: Transaction,
            writer_context: ScopeContext,
            zone: ZoneRef,
            session: WalkTestSession,
            now: datetime,
        ) -> WalkTestStep:
            step = await self._repository.step(transaction, session.session_id, step_id)
            if step is None:
                raise ResourceNotFound()
            closed = close_step(
                step,
                at=now,
                corrected_by=uuid.UUID(str(writer_context.actor.id)),
                correction=request,
            )
            # Primero el cierre condicional (una sola vez), después el registro del expediente.
            await self._repository.close_step(transaction, closed)
            written = await self._writer.write(
                writer_context,
                COMMISSIONING_STEP,
                closed.record_content(),
                scope=RecordScope(plant_id=zone.plant_id, zone_id=zone.zone_id),
                occurred_at=closed.ended_at,
                transaction=transaction,
            )
            record_id_of(written)
            return closed

        return await self._in_session(context, session_id, body)

    async def record_pass(
        self,
        context: ScopeContext,
        session_id: uuid.UUID,
        row_id: object,
        result: object,
        evidence_ref: object = None,
    ) -> WalkTestPass:
        """Anexa un pase de una fila de la matriz (BR-GOB-38): corregir es registrar otro."""
        if type(row_id) is not uuid.UUID:
            raise WalkTestRequestInvalid("row_id es un UUID")
        if evidence_ref is not None and type(evidence_ref) is not uuid.UUID:
            raise WalkTestRequestInvalid("evidence_ref es un UUID")
        try:
            outcome = PassResult(str(result))
        except ValueError:
            raise WalkTestRequestInvalid("result fuera de la lista cerrada") from None
        row = row_id
        evidence = evidence_ref

        async def body(
            transaction: Transaction,
            writer_context: ScopeContext,
            zone: ZoneRef,
            session: WalkTestSession,
            now: datetime,
        ) -> WalkTestPass:
            if session.row(row) is None:
                raise WalkTestRuleViolated(WalkTestViolation.ROW_NOT_IN_MATRIX)
            recorded = WalkTestPass(
                pass_id=uuid7(self._clock, self._random_bytes),
                organization_id=session.organization_id,
                plant_id=session.plant_id,
                session_id=session.session_id,
                row_id=row,
                result=outcome,
                evidence_ref=evidence,
                recorded_by=uuid.UUID(str(writer_context.actor.id)),
                recorded_at=now,
            )
            await self._repository.insert_pass(transaction, recorded)
            return recorded

        return await self._in_session(context, session_id, body)

    # --- Reapertura ------------------------------------------------------------------------------

    async def reopen(
        self, context: ScopeContext, session_id: uuid.UUID, reason_es: object
    ) -> WalkTestView:
        """``incomplete → reopened`` con motivo; conserva pases, pasos y matriz (BL §3.3).

        Devuelve la sesión reabierta con lo que ya tenía registrado.
        """
        reason = self._text(reason_es, _REOPEN_REASON)
        _, zone, authorized = await self._visible(
            context, session_id, PermissionKey.COMMISSIONING_RUN
        )
        writer_context = with_unit(authorized, ActorUnit.U03)
        by = uuid.UUID(str(authorized.actor.id))

        async def body(transaction: Transaction) -> WalkTestView:
            session = await self._repository.lock_session(transaction, session_id)
            if session is None or session.zone_id != zone.zone_id:
                raise ResourceNotFound()
            now = self._now()
            reopened = reopen(expire_if_inactive(session, now), at=now, by=by, reason_es=reason)
            other = await self._repository.open_for_zone(transaction, zone.zone_id)
            if other is not None and other.session_id != session_id:
                raise CatalogRejected(CatalogDetailCode.WALK_TEST_IN_PROGRESS)
            await self._repository.reopen(transaction, session, reopened)
            await self._audit.append(
                authorized,
                AuditOperation.WALK_TEST_REOPENED,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                resource=ResourceRef("walk_test_session", session_id),
                filters={"reason_es": reason},
                transaction=transaction,
            )
            return WalkTestView.of(
                reopened,
                await self._repository.steps(transaction, session_id),
                await self._repository.passes(transaction, session_id),
                await self._occlusions.occlusion_tests(transaction, reopened),
            )

        return await self._run(writer_context, body)

    # --- Lectura ---------------------------------------------------------------------------------

    async def current(self, context: ScopeContext, zone_id: uuid.UUID) -> WalkTestView | None:
        """La sesión abierta de la zona o la ``incomplete`` más reciente (``None`` si no hay).

        Antes de leer, el proveedor reevalúa las pruebas de oclusión ``pending`` de la sesión
        (PAT-GOB-REN-07, cada vez que alguien mira). Bajo concesión, la lectura queda auditada en
        la misma transacción (BR-NUC-38, A-56).
        """
        zone, authorized = await self._gates.zone(context, zone_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            looked = await self._repository.current_for_zone(transaction, zone.zone_id)
        if looked is not None:
            await self._occlusions.reevaluate_pending(authorized, looked, self._now())
        async with self._database.transaction(authorized) as transaction:
            session = await self._repository.current_for_zone(transaction, zone.zone_id)
            view: WalkTestView | None = None
            if session is not None:
                view = WalkTestView.of(
                    expire_if_inactive(session, self._now()),
                    await self._repository.steps(transaction, session.session_id),
                    await self._repository.passes(transaction, session.session_id),
                    await self._occlusions.occlusion_tests(transaction, session),
                )
            if authorized.concession_id is not None:
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=zone.plant_id,
                    zone_id=zone.zone_id,
                    result_count=0 if view is None else 1,
                    transaction=transaction,
                )
        return view
