"""``regenerate_revocation_list``: la lista de revocación global de ``vigia-node-ca`` (TASK-220).

LC-GOB-11 (publicación) y LC-GOB-18; PAT-GOB-RES-02 con su nota D-7; NFR-GOB-12 (nota D-7), 21,
43, 46 y 48; FS-GOB-09 en comportamiento; PR-GOB-23. La **segunda capa** de la revocación: la
primera (``node_revoked`` en cada petición) ya surtió efecto y nunca espera a esta.

**Una tarea ``global``** (``Schedule.every(60)``, la excepción documentada a la iteración por
organización): un ciclo por ejecución del planificador, bajo su arrendamiento. Cada ciclo:

1. abre la transacción de **control** (actor del sistema en la proveedora) y toma el candado de
   publicación (``try_lock_publication``): si otro publicador lo tiene (``vigia-admin`` o un
   worker), no hace nada;
2. lee la fila global y decide (``cycle_reason``): marca puesta, regeneración diaria o forzada;
   sin motivo, solo actualiza las métricas;
3. **un** barrido: las credenciales de cada organización, activa o suspendida
   (``fleet.vigia_revocation_list_organizations``), en **su** transacción de solo
   lectura con ``context_for_organization`` (la RLS intacta; ningún contexto nuevo: es la lectura
   del «contexto de operador» del diseño, nota de TASK-220), agregadas en memoria;
4. reserva el ``CRLNumber`` (transacción corta propia), firma con ``kms:Sign`` y comprueba la
   firma, confirma el arrendamiento y publica (``TrustStorePublisherPort``), todo fuera de las
   transacciones de lectura y con tope de 5 s por paso;
5. registra lo publicado con la generación **leída en el paso 2** (escritura condicional): una
   revocación llegada durante la publicación deja la marca para el ciclo siguiente.

**Fallo** (PAT-GOB-RES-02): cualquier paso que falle o supere su tope deja la marca intacta, suma
``revocation_list_publish_failed`` y el ciclo siguiente reintenta. Como manejador del planificador
lanza ``RevocationListPublishFailed``: la ejecución queda ``partial_failure`` y
``periodic_task_last_success_age_seconds`` sigue creciendo. ``revocation_list_seconds_to_expiry``
sale del ``next_update`` de la lista vigente; por debajo de 24 h suma también al contador.

**Orden administrativa** (NFR-GOB-21): ``RevocationListCommand`` ejecuta el mismo ciclo forzado
(ignora la marca) con las mismas lecturas por organización, fuera del planificador; la usa
``vigia-admin regenerate-revocation-list`` tras una restauración.

Registros sin PEM, sin números de serie y sin ARN; métricas sin atributos (NFR-GOB-13, 25).
"""

from __future__ import annotations

import contextlib
import enum
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from vigia_platform.fleet.domain.revocation_list import (
    CredentialRevocationFacts,
    CycleReason,
    PublishStep,
    RevocationListPublishFailed,
    RevocationListSignerPort,
    RevocationListStatus,
    RevokedCertificate,
    TrustStorePublisherPort,
    alarm_active,
    cycle_reason,
    plan_revocation_list,
    revoked_certificates,
    seconds_to_expiry,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability import redaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import (
    GlobalTaskScope,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
    TaskIteration,
)
from vigia_platform.shared.worker.leases import LeaseLost

__all__ = [
    "SCHEDULE",
    "TASK_NAME",
    "CycleOutcome",
    "RevocationListCommand",
    "RevocationListCycle",
    "RevocationListService",
    "register_regenerate_revocation_list",
]

TASK_NAME: Final = "regenerate_revocation_list"
SCHEDULE: Final = Schedule.every(60)
"""Cada 60 s (LC-GOB-18): una revocación queda publicada en ≤ 5 min (NFR-GOB-48)."""

_log = get_logger("fleet.revocation_list")

# ``task`` ya trae ``regenerate_revocation_list`` en la lista cerrada; el paso que falló va al
# registro como ``code``: solo valores de esta lista (NFR-NUC-41).
with contextlib.suppress(ValueError):
    redaction.DEFAULT_POLICY.register("code", [step.value for step in PublishStep])


class CycleOutcome(enum.StrEnum):
    """Lo que hizo un ciclo."""

    PUBLISHED = "published"
    UP_TO_DATE = "up_to_date"
    """Sin marca y sin regeneración diaria pendiente: nada que publicar."""
    BUSY = "busy"
    """Otro publicador tenía el candado de publicación."""
    FAILED = "failed"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True, kw_only=True)
class RevocationListCycle:
    """El resultado de un ciclo (y de la orden administrativa)."""

    outcome: CycleOutcome
    reason: CycleReason | None = None
    crl_number: int | None = None
    entries: int = 0
    object_version_id: str | None = None
    next_update: datetime | None = None
    generation: int | None = None
    """La generación leída al empezar y publicada (``published_generation`` ≥ esta)."""
    mark_cleared: bool = False
    """``False`` si una revocación llegó mientras se publicaba: la marca sigue para el siguiente."""
    failed_step: PublishStep | None = None


class RevocationListStateStore(Protocol):
    """``PostgresRevocationListStateStore``."""

    async def try_lock_publication(self, transaction: Transaction) -> bool: ...

    async def organizations(self, transaction: Transaction) -> Sequence[uuid.UUID]:
        """**Todas** las organizaciones, también las suspendidas (D-7)."""
        ...

    async def status(self, transaction: Transaction) -> RevocationListStatus: ...

    async def reserve_crl_number(self, transaction: Transaction) -> int: ...

    async def record_publication(
        self,
        transaction: Transaction,
        *,
        generation: int,
        started_at: datetime,
        published_at: datetime,
        object_version_id: str,
        next_update: datetime,
        entries: int,
    ) -> bool: ...


class RevocationFactsReader(Protocol):
    """``PostgresCredentialStore.revocation_facts``."""

    async def revocation_facts(
        self, transaction: Transaction, now: datetime
    ) -> Sequence[CredentialRevocationFacts]: ...


class RevocationListService:
    """El ciclo de la lista de revocación global; como manejador, una ejecución ``global``."""

    def __init__(
        self,
        *,
        states: RevocationListStateStore,
        credentials: RevocationFactsReader,
        signer: RevocationListSignerPort,
        publisher: TrustStorePublisherPort,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
    ) -> None:
        self._states = states
        self._credentials = credentials
        self._signer = signer
        self._publisher = publisher
        self._clock = clock
        self._metrics = metrics

    def __repr__(self) -> str:
        return "RevocationListService()"

    def _platform_metrics(self) -> PlatformMetrics:
        return self._metrics if self._metrics is not None else get_metrics()

    async def __call__(self, scope: GlobalTaskScope) -> None:
        """El manejador de ``regenerate_revocation_list``: un fallo cuenta en la ejecución.

        Un ciclo cortado a mitad (el tope del planificador lo cancela o la base cae) tampoco
        publicó: suma ``revocation_list_publish_failed`` como uno fallido. La pérdida del
        arrendamiento no: otro worker tiene el ciclo.
        """
        try:
            cycle = await self.run_cycle(scope)
        except LeaseLost:
            raise
        except BaseException:
            self._platform_metrics().revocation_list_publish_failed.add(1)
            raise
        if cycle.outcome is CycleOutcome.FAILED and cycle.failed_step is not None:
            raise RevocationListPublishFailed(cycle.failed_step)

    async def run_cycle(
        self, scope: GlobalTaskScope, *, force: bool = False, dry_run: bool = False
    ) -> RevocationListCycle:
        """Un ciclo; ``force`` ignora la marca (NFR-GOB-21), ``dry_run`` no firma ni escribe."""
        started = self._clock.now()
        async with scope.control() as control:
            if not await self._states.try_lock_publication(control):
                _log.info("otro proceso publica la lista de revocación: ciclo omitido")
                return RevocationListCycle(outcome=CycleOutcome.BUSY)
            status = await self._states.status(control)
            reason = cycle_reason(status, started, force=force)
            if reason is None:
                self._report(status.next_update, status.entries, started, failed=False)
                return RevocationListCycle(
                    outcome=CycleOutcome.UP_TO_DATE,
                    crl_number=status.crl_number,
                    entries=status.entries,
                    object_version_id=status.object_version_id,
                    next_update=status.next_update,
                    generation=status.published_generation,
                    mark_cleared=True,
                )
            entries = await self._sweep(scope, control, started)
            if dry_run:
                return RevocationListCycle(
                    outcome=CycleOutcome.DRY_RUN,
                    reason=reason,
                    crl_number=status.crl_number + 1,
                    entries=len(entries),
                    generation=status.dirty_generation,
                )
            return await self._publish(scope, control, status, reason, entries, started)

    async def _sweep(
        self, scope: GlobalTaskScope, control: Transaction, now: datetime
    ) -> tuple[RevokedCertificate, ...]:
        """Un barrido: cada organización en su transacción de solo lectura, en memoria.

        Todas las organizaciones, no solo las activas de ``scope.organizations()``: las revocadas
        de una organización suspendida siguen en la lista (D-7).
        """
        facts: list[CredentialRevocationFacts] = []
        for organization_id in await self._states.organizations(control):
            async with scope.read(organization_id) as transaction:
                facts.extend(await self._credentials.revocation_facts(transaction, now))
        return revoked_certificates(facts, now)

    async def _publish(
        self,
        scope: GlobalTaskScope,
        control: Transaction,
        status: RevocationListStatus,
        reason: CycleReason,
        entries: tuple[RevokedCertificate, ...],
        started: datetime,
    ) -> RevocationListCycle:
        async with scope.control() as reservation:
            crl_number = await self._states.reserve_crl_number(reservation)
        plan = plan_revocation_list(entries, crl_number=crl_number, now=started)
        try:
            signed = await self._signer.sign(plan)
            # Ningún efecto externo con una ejecución que ya no es del proceso.
            await scope.ensure_lease()
            published = await self._publisher.publish(signed)
        except RevocationListPublishFailed as failure:
            # La marca no se toca: el ciclo siguiente reintenta (PAT-GOB-RES-02).
            self._report(status.next_update, status.entries, self._clock.now(), failed=True)
            _log.error(
                "la lista de revocación no se publicó: la marca persiste",
                task=TASK_NAME,
                code=failure.step.value,
            )
            return RevocationListCycle(
                outcome=CycleOutcome.FAILED,
                reason=reason,
                crl_number=crl_number,
                entries=len(entries),
                generation=status.dirty_generation,
                failed_step=failure.step,
            )
        cleared = await self._states.record_publication(
            control,
            generation=status.dirty_generation,
            started_at=started,
            published_at=self._clock.now(),
            object_version_id=published.object_version_id,
            next_update=plan.next_update,
            entries=len(entries),
        )
        self._report(plan.next_update, len(entries), self._clock.now(), failed=False)
        _log.info("lista de revocación global publicada", task=TASK_NAME)
        return RevocationListCycle(
            outcome=CycleOutcome.PUBLISHED,
            reason=reason,
            crl_number=crl_number,
            entries=len(entries),
            object_version_id=published.object_version_id,
            next_update=plan.next_update,
            generation=status.dirty_generation,
            mark_cleared=cleared,
        )

    def _report(
        self, next_update: datetime | None, entries: int, now: datetime, *, failed: bool
    ) -> None:
        metrics = self._platform_metrics()
        left = seconds_to_expiry(next_update, now)
        if left is not None:
            metrics.revocation_list_seconds_to_expiry.set(left)
            metrics.revocation_list_entries.set(entries)
        if alarm_active(failed=failed, seconds_left=left):
            metrics.revocation_list_publish_failed.add(1)


class RevocationListCommand:
    """El ciclo forzado de ``vigia-admin`` (NFR-GOB-21) sobre las lecturas por organización."""

    def __init__(self, service: RevocationListService, sweep: GlobalTaskScope) -> None:
        self._service = service
        self._sweep = sweep

    def __repr__(self) -> str:
        return "RevocationListCommand()"

    async def regenerate(self, *, dry_run: bool) -> RevocationListCycle:
        """Publica desde el estado de la base aunque no haya marca (o lo muestra con dry_run)."""
        return await self._service.run_cycle(self._sweep, force=True, dry_run=dry_run)


def register_regenerate_revocation_list(
    registry: PeriodicTaskRegistry, service: RevocationListService
) -> PeriodicTask:
    """Registra ``regenerate_revocation_list`` (``global``, cada 60 s); la llama la raíz."""
    return registry.register(
        TASK_NAME, SCHEDULE, service, unit=ActorUnit.U03, iteration=TaskIteration.GLOBAL
    )
