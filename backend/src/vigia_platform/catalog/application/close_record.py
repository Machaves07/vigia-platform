"""``catalog.records``: cierre del acta de comisionamiento y su lectura (LC-GOB-08, LC-GOB-09).

**Cierre** (``close``, ``POST /walk-tests/{id}/close``, ``commissioning.run`` sobre la zona de la
sesión), con ``{signatures[{user_id}], false_alarm_acceptance?: {reason_es},
installer_measurements: {beacon_latency_ms_p95, baselines[{camera_id, zone_id, captured_at}]}}``
(interfaces v1.5, precisión (b)):

1. el cuerpo, coherente por sí mismo (si no, ``invalid_request``): de 1 a 32 firmantes distintos,
   cada uno de la organización con un rol vigente sobre la zona (``IdentityQueryPort``; su
   ``role_in_use`` es el primero de la lista cerrada que tiene); el p95 del instalador en
   milisegundos enteros; una línea base por par cámara-zona de la zona y del catálogo de la
   sesión. El motivo de la aceptación pasa la política de texto libre
   (``catalog_free_text_rejected``);
2. las **siete guardas** de ``commissioning_record.first_failing_guard`` en su orden; la primera
   que falla es el error (``catalog_<guarda>``) y no se escribe nada. Se descarta pronto: las tres
   primeras antes de reevaluar la oclusión (``catalog.occlusion``, PAT-GOB-REN-07), las seis
   primeras antes de consultar el almacén. La del difuminado consulta con ``head_object`` (nunca
   ``get_object``) los clips de verificación de la zona sin comprobar o aprobados; el almacén
   caído es ``StorageUnavailable`` (transitorio) y nada se escribe;
3. una transacción bajo el **candado de la sesión** que vuelve a leer todo y vuelve a evaluar las
   guardas (con el difuminado ya comprobado), y escribe: el ``blur_check_result`` aprobado del clip
   que lo verificó (si seguía nulo), ``CommissioningRecord`` con el umbral aplicado,
   ``walk_test_result`` (``source_key = commissioning_record_id``), la sesión ``closed`` con su
   acta y, si la sesión es ``regression_rerun`` que cubre las filas afectadas y la regresión no
   se volvió a marcar después de abrirla (``clears_regression``), la regresión ``current`` con
   ``walk_test_regression_cleared`` y ``regression_cleared`` (BR-GOB-55).

N cierres simultáneos de la misma sesión: el primero que toma el candado cierra; los demás la
encuentran ``closed`` y responden ``conflict`` sin escribir nada.

**Candados** (orden único, también frente a la marca de regresión, la oclusión y el walk-test):
primero la fila de la sesión, después la regresión de la zona (solo una reejecución), después la
fila del clip de verificación cuyo difuminado se cierra y por último la cadena de la planta
(``EscritorExpediente``). La marca de regresión toma regresión → cadena; nunca hay ciclo.

**Latencia** (``latency.latency_report``): los cuatro tramos por separado con su reloj, sobre los
clips de verificación de la zona recibidos en ``[started_at, ahora]`` y las muestras de exposición
de la sesión; la suma orientativa, marcada. Las cámaras medidas salen del último latido aceptado
(``fleet.camera_inventory``): sin latido, ``measured_fps`` nulo (BR-GOB-49).

**Lectura** (``record``, ``GET /commissioning-records/{id}``, ``catalog.read`` sobre la zona del
acta): el acta estructurada tal como se guardó, con los pases por celda de su sesión; nunca un
responsable (H-53). Bajo concesión, la lectura se audita en la misma transacción (A-56).

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final, Protocol

from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.catalog.adapters.postgres.catalog_repository import PostgresCatalogRepository
from vigia_platform.catalog.adapters.postgres.commissioning_record_repository import (
    PostgresCommissioningRecordRepository,
    StoredRecord,
)
from vigia_platform.catalog.adapters.postgres.occlusion_repository import (
    PostgresOcclusionRepository,
)
from vigia_platform.catalog.adapters.postgres.regression_repository import (
    PostgresRegressionRepository,
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
from vigia_platform.catalog.domain.commissioning_record import (
    FALSE_ALARM_THRESHOLD,
    BlurCheck,
    CameraMeasured,
    CloseFacts,
    CloseGuard,
    CommissioningRecord,
    FalseAlarmAcceptance,
    Signature,
    blur_check,
    camera_ids_of,
    clears_regression,
    declared_min_fps_of,
    false_alarm_rate,
    first_failing_guard,
    hours_of,
    matrix_results,
    occlusion_summary,
)
from vigia_platform.catalog.domain.enums import RegressionState, WalkTestKind, WalkTestStatus
from vigia_platform.catalog.domain.latency import (
    MAX_LATENCY_MS,
    MeasuredBy,
    Stamp,
    latency_report,
)
from vigia_platform.catalog.domain.occlusion import OcclusionTest
from vigia_platform.catalog.domain.regression import ALL_ROWS, WalkTestRegression
from vigia_platform.catalog.domain.texts import has_content
from vigia_platform.catalog.domain.walk_test import WalkTestSession, expire_if_inactive
from vigia_platform.catalog.record_types import MAX_CAMERAS, MAX_SIGNATORIES
from vigia_platform.fleet.adapters.postgres.commissioning_queries import (
    CommissioningClip,
    PostgresCommissioningQueries,
)
from vigia_platform.fleet.domain.verification_clip import ObjectFacts
from vigia_platform.identity.application.hierarchy import Recipient
from vigia_platform.identity.authz.authorize import ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext, ScopeLevel, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "CLEARED_RECORD_TYPE",
    "REGRESSION_CLEARED",
    "WALK_TEST_RESULT",
    "Baseline",
    "ClipHeads",
    "CloseRecordService",
    "CloseRequest",
    "OcclusionReevaluation",
    "SignerLookup",
]

WALK_TEST_RESULT: Final = "walk_test_result"
CLEARED_RECORD_TYPE: Final = "walk_test_regression_cleared"
REGRESSION_CLEARED: Final = "regression_cleared"
_ACCEPTANCE_REASON: Final = FreeTextField(
    WALK_TEST_RESULT, "/false_alarm_acceptance/reason_es", 10, 500
)
_GUARD_CODES: Final[Mapping[CloseGuard, CatalogDetailCode]] = {
    CloseGuard.STEPS_STILL_OPEN: CatalogDetailCode.STEPS_STILL_OPEN,
    CloseGuard.MATRIX_INCOMPLETE: CatalogDetailCode.MATRIX_INCOMPLETE,
    CloseGuard.FALSE_NEGATIVE_PRESENT: CatalogDetailCode.FALSE_NEGATIVE_PRESENT,
    CloseGuard.REDUNDANCY_NOT_VERIFIED: CatalogDetailCode.REDUNDANCY_NOT_VERIFIED,
    CloseGuard.FALSE_ALARM_RATE_ABOVE_THRESHOLD: (
        CatalogDetailCode.FALSE_ALARM_RATE_ABOVE_THRESHOLD
    ),
    CloseGuard.LATENCY_NOT_MEASURED: CatalogDetailCode.LATENCY_NOT_MEASURED,
    CloseGuard.BLUR_NOT_VERIFIED: CatalogDetailCode.BLUR_NOT_VERIFIED,
}


# --- Puertos -------------------------------------------------------------------------------------


class ClipHeads(Protocol):
    """``ClipObjectStore.heads`` de ``fleet``: solo ``head_object`` y en paralelo con tope;
    ``StorageUnavailable`` si alguna consulta falla (fallo cerrado, nunca parcial)."""

    async def heads(self, keys: Sequence[str]) -> dict[str, ObjectFacts | None]: ...


class OcclusionReevaluation(Protocol):
    """``OcclusionService.reevaluate_pending`` (LC-GOB-07): resuelve las ``pending`` que ya se
    pueden resolver y devuelve las pruebas de la sesión."""

    async def reevaluate_pending(
        self, context: ScopeContext, session: WalkTestSession, now: datetime
    ) -> tuple[OcclusionTest, ...]: ...


class SignerLookup(Protocol):
    """``IdentityQueryPort.users_by_role_and_scope`` de U-02 (LC-NUC-05)."""

    async def users_by_role_and_scope(
        self,
        context: ScopeContext,
        roles: Iterable[Role],
        scope_level: ScopeLevel,
        scope_id: uuid.UUID,
    ) -> tuple[Recipient, ...]: ...


# --- Petición ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Baseline:
    """Fecha de la línea base de un par cámara-zona, leída del ``LocalStatus`` por el instalador."""

    camera_id: object
    zone_id: object
    captured_at: object


@dataclass(frozen=True, slots=True)
class CloseRequest:
    """El cuerpo de ``POST /walk-tests/{id}/close``, sin validar."""

    signatures: Sequence[object]
    beacon_latency_ms_p95: object
    baselines: Sequence[Baseline] = ()
    false_alarm_reason_es: object = None


@dataclass(frozen=True, slots=True)
class _Body:
    """El cuerpo ya validado."""

    signers: tuple[uuid.UUID, ...]
    beacon_latency_ms_p95: int
    baselines: tuple[tuple[uuid.UUID, uuid.UUID, datetime], ...]
    reason_es: str | None


def _rejected(guard: CloseGuard) -> CatalogRejected:
    return CatalogRejected(_GUARD_CODES[guard])


def _ids(values: Iterable[object]) -> tuple[uuid.UUID, ...]:
    return tuple(value for value in values if isinstance(value, uuid.UUID))


@dataclass(frozen=True, slots=True)
class _Observed:
    """Lo que las guardas miran en un instante (fuera o dentro del candado)."""

    facts: CloseFacts
    clips: tuple[CommissioningClip, ...]


# --- Servicio ------------------------------------------------------------------------------------


@repository
class CloseRecordService:
    """Cierre del acta de comisionamiento y su lectura estructurada."""

    def __init__(
        self,
        *,
        repository: PostgresCommissioningRecordRepository,
        sessions: PostgresWalkTestRepository,
        occlusion_tests: PostgresOcclusionRepository,
        occlusions: OcclusionReevaluation,
        regressions: PostgresRegressionRepository,
        catalog: PostgresCatalogRepository,
        fleet: PostgresCommissioningQueries,
        clips: ClipHeads,
        gates: GateService,
        identity: SignerLookup,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        audit: AuditWriter,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._sessions = sessions
        self._occlusion_tests = occlusion_tests
        self._occlusions = occlusions
        self._regressions = regressions
        self._catalog = catalog
        self._fleet = fleet
        self._clips = clips
        self._gates = gates
        self._identity = identity
        self._database = database
        self._writer = writer
        self._audit = audit
        self._free_text = free_text
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "CloseRecordService()"

    def _now(self) -> datetime:
        return to_millisecond(self._clock.now())

    # --- Cuerpo ----------------------------------------------------------------------------------

    def _reason(self, value: object) -> str:
        if not isinstance(value, str):
            raise WalkTestRequestInvalid("el motivo de la aceptación es texto")
        try:
            text = self._free_text.apply(value, _ACCEPTANCE_REASON)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None
        if not has_content(text):  # solo espacios, signos o invisibles: no es un motivo
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        return text

    def _body(
        self, request: CloseRequest, session: WalkTestSession, cameras: frozenset[Any]
    ) -> _Body:
        if not isinstance(request, CloseRequest):
            raise TypeError("request debe ser CloseRequest")
        signers = tuple(request.signatures)
        if (
            not 1 <= len(signers) <= MAX_SIGNATORIES
            or len(_ids(signers)) != len(signers)
            or len(set(signers)) != len(signers)
        ):
            raise WalkTestRequestInvalid("de 1 a 32 firmantes distintos")
        beacon = request.beacon_latency_ms_p95
        if type(beacon) is not int or not 0 <= beacon <= MAX_LATENCY_MS:
            raise WalkTestRequestInvalid("beacon_latency_ms_p95 en milisegundos enteros")
        baselines: list[tuple[uuid.UUID, uuid.UUID, datetime]] = []
        for baseline in request.baselines:
            camera, zone, captured = baseline.camera_id, baseline.zone_id, baseline.captured_at
            if (
                type(camera) is not uuid.UUID
                or zone != session.zone_id
                or camera not in cameras
                or not isinstance(captured, datetime)
                or captured.utcoffset() is None
            ):
                raise WalkTestRequestInvalid("línea base de una cámara de la zona de la sesión")
            baselines.append((camera, session.zone_id, to_millisecond(captured)))
        if len(baselines) > MAX_CAMERAS or len({b[:2] for b in baselines}) != len(baselines):
            raise WalkTestRequestInvalid("una línea base por par cámara-zona")
        reason = (
            None
            if request.false_alarm_reason_es is None
            else self._reason(request.false_alarm_reason_es)
        )
        return _Body(
            signers=_ids(signers),
            beacon_latency_ms_p95=beacon,
            baselines=tuple(baselines),
            reason_es=reason,
        )

    async def _signatures(
        self, authorized: ScopeContext, zone: ZoneRef, signers: tuple[uuid.UUID, ...]
    ) -> dict[uuid.UUID, Role]:
        """El rol con el que firma cada usuario: de la organización y con un rol vigente sobre la
        zona (el primero de la lista cerrada); si alguno no lo tiene, ``invalid_request``."""
        holders = await self._identity.users_by_role_and_scope(
            authorized, tuple(Role), ScopeLevel.ZONE, zone.zone_id
        )
        roles: dict[uuid.UUID, list[Role]] = {}
        for holder in holders:
            roles.setdefault(holder.user_id, []).append(Role(holder.role))
        order = list(Role)
        signed: dict[uuid.UUID, Role] = {}
        for user_id in signers:
            held = roles.get(user_id)
            if not held:
                raise WalkTestRequestInvalid("el firmante no tiene alcance sobre la zona")
            signed[user_id] = min(held, key=order.index)
        return signed

    # --- Lecturas --------------------------------------------------------------------------------

    async def _visible(
        self, context: ScopeContext, session_id: uuid.UUID
    ) -> tuple[WalkTestSession, ZoneRef, ScopeContext]:
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

    async def _catalogs(
        self, transaction: Transaction, session: WalkTestSession
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        """El catálogo de la sesión y el vigente de la zona."""
        own = await self._catalog.version_in(transaction, session.zone_id, session.catalog_version)
        current = await self._catalog.current(transaction, session.zone_id)
        if own is None:
            raise WalkTestConflict
        return own.payload, (own if current is None else current).payload

    async def _observe(
        self,
        transaction: Transaction,
        session: WalkTestSession,
        catalog: Mapping[str, Any],
        now: datetime,
        *,
        tests: Sequence[OcclusionTest] | None = None,
        accepted: bool = False,
        blur_verified: bool = False,
    ) -> _Observed:
        steps = await self._sessions.steps(transaction, session.session_id)
        passes = await self._sessions.passes(transaction, session.session_id)
        if tests is None:
            tests = await self._occlusion_tests.tests(transaction, session.session_id)
        count, clips = await self._fleet.window_clips(
            transaction, session.zone_id, session.started_at, now
        )
        return _Observed(
            CloseFacts(
                steps=steps,
                rows=session.matrix_rows,
                passes=passes,
                occlusion_tests=tests,
                cameras=camera_ids_of(catalog),
                now=now,
                false_alarm_accepted=accepted,
                verification_clips_in_window=count,
                blur_verified=blur_verified,
            ),
            clips,
        )

    @staticmethod
    def _operable(session: WalkTestSession, now: datetime) -> None:
        effective = expire_if_inactive(session, now)
        if effective.status is WalkTestStatus.INCOMPLETE:
            raise CatalogRejected(CatalogDetailCode.WALK_TEST_INCOMPLETE)
        if not effective.is_open:
            raise WalkTestConflict

    async def _blur(
        self, authorized: ScopeContext, zone: ZoneRef, now: datetime
    ) -> tuple[CommissioningClip, BlurCheck] | None:
        """El clip de verificación de la zona cuyo difuminado comprueba ``head_object``, o
        ``None``. ``StorageUnavailable`` si el almacén falla (nada escrito)."""
        async with self._database.transaction(authorized) as transaction:
            candidates = await self._fleet.blur_candidates(transaction, zone.zone_id)
        if not candidates:
            return None
        facts = await self._clips.heads([clip.storage_key for clip in candidates])
        for clip in candidates:
            check = blur_check(clip.sha256, facts.get(clip.storage_key), now)
            if check.approved:
                return clip, check
        return None

    # --- Cierre ----------------------------------------------------------------------------------

    async def close(
        self, context: ScopeContext, session_id: uuid.UUID, request: CloseRequest
    ) -> CommissioningRecord:
        """Cierra el acta de la sesión; la devuelve tras confirmar.

        ``ResourceNotFound``, ``WalkTestRequestInvalid``, ``CatalogRejected`` (la guarda que
        falla, ``walk_test_incomplete`` o ``free_text_rejected``), ``WalkTestConflict`` (sesión
        ya cerrada), ``StorageUnavailable`` o ``WalkTestUnavailable``; en todos, nada escrito.
        """
        session, zone, authorized = await self._visible(context, session_id)
        writer_context = with_unit(authorized, ActorUnit.U03)
        async with self._database.transaction(authorized) as transaction:
            catalog, _ = await self._catalogs(transaction, session)
        body = self._body(request, session, frozenset(camera_ids_of(catalog)))
        signed = await self._signatures(authorized, zone, body.signers)
        accepted = body.reason_es is not None

        # Fuera del candado, para descartar pronto en el orden de las guardas.
        now = self._now()
        async with self._database.transaction(authorized) as transaction:
            self._operable(session, now)
            early = await self._observe(transaction, session, catalog, now, tests=())
        failed = first_failing_guard(early.facts, last=CloseGuard.FALSE_NEGATIVE_PRESENT)
        if failed is not None:
            raise _rejected(failed)
        tests = await self._occlusions.reevaluate_pending(writer_context, session, now)
        async with self._database.transaction(authorized) as transaction:
            seen = await self._observe(
                transaction, session, catalog, now, tests=tests, accepted=accepted
            )
        failed = first_failing_guard(seen.facts, last=CloseGuard.LATENCY_NOT_MEASURED)
        if failed is not None:
            raise _rejected(failed)
        blur = await self._blur(authorized, zone, now)
        if blur is None:
            raise _rejected(CloseGuard.BLUR_NOT_VERIFIED)

        async def locked(transaction: Transaction) -> CommissioningRecord:
            return await self._close_locked(
                transaction, writer_context, zone, session_id, body, signed, blur
            )

        try:
            async with self._database.transaction(writer_context) as transaction:
                return await locked(transaction)
        except WalkTestWriteConflict:
            raise WalkTestConflict from None
        except LedgerRaceLost:
            raise WalkTestUnavailable("walk_test_result_race") from None

    async def _close_locked(
        self,
        transaction: Transaction,
        writer_context: ScopeContext,
        zone: ZoneRef,
        session_id: uuid.UUID,
        body: _Body,
        signed: Mapping[uuid.UUID, Role],
        blur: tuple[CommissioningClip, BlurCheck],
    ) -> CommissioningRecord:
        # 1. El candado de la sesión, y lo que había bajo él.
        session = await self._sessions.lock_session(transaction, session_id)
        if session is None or session.zone_id != zone.zone_id:
            raise ResourceNotFound()
        now = self._now()
        self._operable(session, now)
        catalog, current_catalog = await self._catalogs(transaction, session)
        # 2. El de la regresión (solo una reejecución), antes de cualquier registro.
        regression: WalkTestRegression | None = None
        if session.kind is WalkTestKind.REGRESSION_RERUN:
            await self._regressions.lock(transaction, zone.zone_id)
            regression = await self._regressions.get(transaction, zone.zone_id)
        observed = await self._observe(
            transaction,
            session,
            catalog,
            now,
            accepted=body.reason_es is not None,
            blur_verified=True,
        )
        failed = first_failing_guard(observed.facts)
        if failed is not None:
            raise _rejected(failed)
        facts = observed.facts
        # 3. El cierre del difuminado del clip que lo verificó (si seguía sin escribir).
        clip, check = blur
        if BlurCheck.approved_in(clip.blur_check_result) is None:
            await self._fleet.record_blur_check(
                transaction, zone.zone_id, clip.clip_id, check.to_json()
            )
        record = await self._record(
            transaction, session, zone, catalog, facts, observed.clips, body, signed, now
        )
        cleared = clears_regression(session, regression, (catalog, current_catalog))
        record = replace(record, regression_cleared=cleared)
        # 4. Acta, registro, sesión y, si procede, la regresión resuelta.
        await self._repository.insert(transaction, record)
        written = await self._writer.write(
            writer_context,
            WALK_TEST_RESULT,
            record.record_content(),
            scope=RecordScope(plant_id=zone.plant_id, zone_id=zone.zone_id),
            occurred_at=now,
            transaction=transaction,
            record_id=record.ledger_record_id,
        )
        if record_id_of(written) != record.ledger_record_id:
            raise LedgerRaceLost
        await self._sessions.close(transaction, session_id, now, record.commissioning_record_id)
        if cleared and regression is not None:
            await self._clear(transaction, writer_context, regression, session_id, now)
        return record

    async def _record(
        self,
        transaction: Transaction,
        session: WalkTestSession,
        zone: ZoneRef,
        catalog: Mapping[str, Any],
        facts: CloseFacts,
        clips: Sequence[CommissioningClip],
        body: _Body,
        signed: Mapping[uuid.UUID, Role],
        now: datetime,
    ) -> CommissioningRecord:
        """El acta con lo leído bajo el candado (sin escribir nada)."""
        samples = await self._repository.samples(transaction, session.session_id)
        cameras = facts.cameras
        inventory = await self._fleet.cameras_measured(transaction, session.node_id, cameras)
        declared = declared_min_fps_of(catalog)
        refs = sorted({p.evidence_ref for p in facts.passes if p.evidence_ref is not None})
        verifiable = await self._fleet.zone_clip_ids(transaction, zone.zone_id, refs)
        total, summary = hours_of(facts.steps)
        latency = latency_report(
            beacon_latency_ms_p95=body.beacon_latency_ms_p95,
            upload=[
                (Stamp(MeasuredBy.PLATFORM, c.issued_at), Stamp(MeasuredBy.PLATFORM, c.received_at))
                for c in clips
            ],
            exposure=[sample.interval() for sample in samples],
            served=[
                (
                    Stamp(MeasuredBy.PLATFORM, c.received_at),
                    Stamp(MeasuredBy.PLATFORM, c.first_served_at),
                )
                for c in clips
                if c.first_served_at is not None
            ],
        )
        rate = false_alarm_rate(facts.passes)
        acceptance = None
        if rate > FALSE_ALARM_THRESHOLD and body.reason_es is not None:
            acceptance = FalseAlarmAcceptance(
                reason_es=body.reason_es,
                accepted_by=uuid.UUID(str(transaction.context.actor.id)),
                accepted_at=now,
            )
        record_id = uuid7(self._clock, self._random_bytes)
        return CommissioningRecord(
            commissioning_record_id=record_id,
            organization_id=session.organization_id,
            plant_id=session.plant_id,
            zone_id=session.zone_id,
            session=session,
            matrix_results=tuple(matrix_results(session.matrix_rows, facts.passes, verifiable)),
            false_alarm_rate_observed=rate,
            false_alarm_threshold=FALSE_ALARM_THRESHOLD,
            false_alarm_acceptance=acceptance,
            latency=latency,
            latency_repetitions=len(facts.passes) + facts.verification_clips_in_window,
            cameras_measured=tuple(
                CameraMeasured(
                    camera_id=camera,
                    measured_fps=None
                    if camera not in inventory
                    else inventory[camera].measured_fps,
                    declared_min_fps=(
                        inventory[camera].declared_min_fps
                        if camera in inventory
                        else declared.get(camera)
                    ),
                )
                for camera in dict.fromkeys(cameras)
            ),
            occlusion_summary=occlusion_summary(facts.occlusion_tests, cameras),
            total_duration_ms=total,
            steps_summary=summary,
            installer_measurements={
                "beacon_latency_ms_p95": body.beacon_latency_ms_p95,
                "measured_by": MeasuredBy.INSTALLER.value,
                "baselines": [
                    {
                        "camera_id": str(camera),
                        "zone_id": str(zone_id),
                        "captured_at": format_timestamp(captured),
                    }
                    for camera, zone_id, captured in body.baselines
                ],
            },
            signatures=tuple(
                Signature(user_id=user_id, role_in_use=Role(role).value, signed_at=now)
                for user_id, role in signed.items()
            ),
            closed_at=now,
            ledger_record_id=uuid7(self._clock, self._random_bytes),
        )

    async def _clear(
        self,
        transaction: Transaction,
        writer_context: ScopeContext,
        regression: WalkTestRegression,
        session_id: uuid.UUID,
        now: datetime,
    ) -> None:
        """``pending → current`` con ``walk_test_regression_cleared`` y ``regression_cleared``."""
        rows = regression.affected_row_ids
        event: dict[str, Any] = {
            "zone_id": str(regression.zone_id),
            "cleared_by_session_id": str(session_id),
            "cause": None if regression.cause is None else regression.cause.value,
            "affected_row_ids": ALL_ROWS
            if rows == ALL_ROWS or rows is None
            else [str(row) for row in rows],
        }
        if regression.catalog_version is not None:
            event["catalog_version"] = regression.catalog_version
        if regression.model_version is not None:
            event["model_version"] = regression.model_version
        written = await self._writer.write(
            writer_context,
            CLEARED_RECORD_TYPE,
            {
                "zone_id": str(regression.zone_id),
                "cleared_by_session_id": str(session_id),
                "cleared_at": format_timestamp(now),
            },
            scope=RecordScope(plant_id=regression.plant_id, zone_id=regression.zone_id),
            events=(NewEvent(event_name=REGRESSION_CLEARED, payload=event),),
            occurred_at=now,
            transaction=transaction,
        )
        await self._regressions.save(
            transaction,
            WalkTestRegression(
                organization_id=regression.organization_id,
                plant_id=regression.plant_id,
                zone_id=regression.zone_id,
                state=RegressionState.CURRENT,
                cleared_at=now,
                cleared_by_session_id=session_id,
                ledger_record_id=_written_id(written),
            ),
        )

    # --- Lectura ---------------------------------------------------------------------------------

    async def record(self, context: ScopeContext, record_id: uuid.UUID) -> StoredRecord:
        """``GET /commissioning-records/{id}`` (``catalog.read`` sobre la zona del acta)."""
        if not isinstance(context, ScopeContext) or type(record_id) is not uuid.UUID:
            raise ResourceNotFound()
        async with self._database.transaction(context) as transaction:
            seen = await self._repository.record(transaction, record_id)
        if seen is None:
            raise ResourceNotFound()
        zone, authorized = await self._gates.zone(context, seen.zone_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            stored = await self._repository.record(transaction, record_id)
            if stored is None or stored.zone_id != zone.zone_id:
                raise ResourceNotFound()
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
        return stored


def _written_id(written: Receipt | LedgerRejection) -> uuid.UUID:
    """El registro de la resolución de la regresión (sin ``source_key``: nunca duplicado)."""
    if isinstance(written, LedgerRejection) or written.status is not AcceptanceStatus.ACCEPTED:
        raise LedgerRaceLost
    return uuid.UUID(str(written.record_id))
