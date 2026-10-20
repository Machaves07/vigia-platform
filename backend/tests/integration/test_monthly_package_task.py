"""Paquete mensual: ``MonthlyPackageComposer``/``MonthlyPackageTask`` sobre PostgreSQL 16 y
LocalStack reales (TASK-342 a 344; LC-LAZ-24; BR-LAZ-102 y 141; VIG-239).

Sobre ``tests.laz_support.loop_world`` (la ingesta real de U-03 con los diecisiete tipos de U-04 y
dos organizaciones), con un depósito de evidencias de LocalStack (``S3Storage``) y un
``CheckpointService`` real (firmante de ``tests.signing_support.bootstrapped_world``, con su
propio reloj simulado, como en ``test_audit_archive_localstack.py``): la composición y el sellado
nunca inventan un punto de control ni una firma.

- **Sellado exactamente una vez**: una segunda composición del mismo ``(zone_id, period)`` no hace
  nada (``None``, BR-LAZ-102).
- **Recuperación tras una interrupción larga**: ``MonthlyPackageTask`` sella, en una sola pasada,
  todos los meses vencidos y aún no sellados de una zona, del más antiguo al más reciente
  (PAT-LAZ-RES-05).
- **Concurrencia**: dos composiciones a la vez del mismo ``(zone_id, period)`` nunca dejan dos
  filas selladas ni dos registros ``monthly_package_produced``.
- **Arrendamiento vencido** (B2/B3, rework 1 del PR #161): un arrendamiento «zombie» que venció
  deja tomarlo a un segundo proceso sin sellar dos veces, y el objeto de ``vigia-evidence`` es
  siempre el del ganador (nunca el de una perdedora que subiera después, P4/P5, BR-LAZ-108).
- **Zona horaria de la planta, no UTC**: dos plantas con husos distintos calculan periodos
  vencidos distintos para el mismo instante.

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.laz_support import IngestedSubject, LoopOrganization, LoopWorld, loop_world
from tests.signing_support import bootstrapped_world
from vigia_platform.accreditation.monthly.package import ComposeOutcome, MonthlyPackageComposer
from vigia_platform.accreditation.monthly.repository import (
    MonthlyPackageRepository,
    ZoneRef,
    object_key,
)
from vigia_platform.accreditation.monthly.task import MonthlyPackageTask
from vigia_platform.accreditation.verifier_bundle import VERIFIER_FILE, ManifestSigner
from vigia_platform.ledger.adapters.checkpoint_store import SqlCheckpointStore
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.ledger.application.writer import Receipt
from vigia_platform.ledger.chain.checkpoints import CheckpointService
from vigia_platform.loop.calendar import local_date, month_bounds, today
from vigia_platform.loop.config.parameters import LoopSettings, PlantLoopConfiguration
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.shared.clock import Clock, SimulatedClock
from vigia_platform.shared.storage import S3Storage

pytestmark = pytest.mark.integration

BOGOTA = "America/Bogota"
MADRID = "Europe/Madrid"
_BASELINE = datetime(2026, 1, 1, tzinfo=UTC)
"""Instante al que se fija el reloj antes de dar de alta cada organización: el certificado del
nodo sintético se emite con vigencia de 365 días desde ``now - 1 día`` en ese momento
(``tests/node_api_db.py::issue``), así que todos los periodos de estas pruebas (2026-05 a
2026-11) quedan dentro de su vigencia sin tocar la hora real de la máquina."""
_DEFAULTS = LoopSettings.defaults()
_SAMPLE_PERCENT = _DEFAULTS["sample_percent"]
_SAMPLE_MIN = _DEFAULTS["sample_min"]
_SAMPLE_MAX = _DEFAULTS["sample_max"]
_REASON_OF: dict[str, str] = {
    "confirmed": "real_coexistence",
    "false_positive": "no_person_present",
    "authorized_operation": "scheduled_maintenance",
}


@dataclass
class World:
    loop: LoopWorld
    checkpoints: CheckpointService
    config: PostgresLoopConfigurationRepository
    repository: MonthlyPackageRepository
    evidence: S3Storage
    signer: ManifestSigner

    @property
    def database(self) -> Any:
        return self.loop.stack.primary.database

    @property
    def clock(self) -> SimulatedClock:
        clock: SimulatedClock = self.loop.stack.authz.sessions.clock
        return clock

    def composer(self, *, clock: Clock | None = None) -> MonthlyPackageComposer:
        sessions = self.loop.stack.authz.sessions
        composer_clock = sessions.clock if clock is None else clock
        return MonthlyPackageComposer(
            database=self.database,
            reader=LectorExpediente(
                database=self.database, audit=sessions.audit, clock=composer_clock
            ),
            repository=self.repository,
            evidence=self.evidence,
            checkpoints=self.checkpoints,
            writer=self.loop.stack.primary.writer,
            clock=composer_clock,
            signer=self.signer,
        )  # fmt: skip

    def task(self, *, max_catchup_months: int) -> MonthlyPackageTask:
        return MonthlyPackageTask(
            config=self.config,
            repository=self.repository,
            composer=self.composer(),
            clock=self.clock,
            max_catchup_months=max_catchup_months,
        )

    def zone(self, organization: LoopOrganization) -> ZoneRef:
        return ZoneRef(
            organization_id=organization.organization_id,
            plant_id=organization.plant_id,
            zone_id=organization.zone_id,
        )

    def configure(
        self, organization: LoopOrganization, *, timezone: str = BOGOTA, **overrides: int
    ) -> None:
        """Siembra ``loop.plant_loop_configuration`` (la versión 1) de la planta de
        ``organization``."""
        settings = _DEFAULTS.replaced(dict(overrides))
        configuration = PlantLoopConfiguration(
            plant_id=organization.plant_id,
            organization_id=organization.organization_id,
            settings=settings,
            timezone=timezone,
            version=1,
            changed_at=self.clock.now(),
            changed_by={
                "user_id": str(uuid.uuid4()),
                "display_name": "Configuración sintética",
                "role_in_use": "administrator",
            },
            change_reason="Configuración sintética de prueba",
        )
        context = self.loop.event_context(organization)

        async def run() -> None:
            async with self.database.transaction(context) as transaction:
                await self.config.insert(transaction, configuration)

        self.loop.stack.run(run())

    def grant_coordinator(self, organization: LoopOrganization) -> uuid.UUID:
        """Da de alta a una persona nueva y la nombra coordinadora SST de la organización.

        ``audit_sample.reviewer_user_id`` es obligatorio en el esquema: sin un coordinador
        vigente, ``compose_and_seal`` declara ``MonthlyPackageReviewerMissing`` en vez de sellar
        (nunca inventa un revisor). ``identity.role_assignment`` exige un ``user_account`` real de
        **esta** organización (``role_assignment_user_fkey``), no el operador compartido de la
        sesión de pruebas."""
        user = self.loop.stack.authz.sessions.add_user(organization.organization_id)
        self.loop.stack.execute(
            "INSERT INTO identity.role_assignment (assignment_id, organization_id, user_id,"
            " role, scope_level, scope_id, assigned_at, assigned_by)"
            " VALUES ($1, $2, $3, 'coordinator_sst', 'organization', $2, $4, $3)",
            uuid.uuid4(),
            organization.organization_id,
            user.user_id,
            self.clock.now(),
        )
        return user.user_id

    def classify(
        self,
        subject: IngestedSubject,
        *,
        sequence: int = 1,
        outcome: str = "confirmed",
        reason_category: str | None = None,
        previous_outcome: str | None = None,
        previous_record_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Un ``classification`` real (escritor+expediente), sin tocar la proyección del lazo."""
        content = self.loop.classification_content(
            subject,
            outcome=outcome,
            reason_category=reason_category or _REASON_OF[outcome],
            sequence=sequence,
            previous_outcome=previous_outcome,
        )
        if previous_record_id is not None:
            content["previous_classification_record_id"] = str(previous_record_id)
        receipt, _ = self.loop.write(subject.organization, "classification", content, apply=False)
        assert isinstance(receipt, Receipt), receipt
        return receipt.record_id

    def sealed_rows(self, zone_id: uuid.UUID) -> list[Any]:
        return self.loop.stack.fetch(
            "SELECT package_id, period, state FROM accreditation.monthly_package"
            " WHERE zone_id = $1 ORDER BY period",
            zone_id,
        )

    def produced_periods(self, zone_id: uuid.UUID) -> list[str]:
        """Los ``monthly_package_produced`` de ``zone_id``, en orden de cadena (el más viejo
        primero)."""
        rows = self.loop.stack.fetch(
            "SELECT ledger.vigia_bytes_to_jsonb(content)::text AS content FROM ledger.ledger_record"
            " WHERE record_type = 'monthly_package_produced' AND scope_zone_id = $1"
            " ORDER BY chain_sequence",
            zone_id,
        )
        return [json.loads(row["content"])["period"] for row in rows]


@pytest.fixture(scope="module")
def world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[World]:
    with loop_world(postgres_endpoint, "monthly_task") as loop:
        signing = loop.stack.run(bootstrapped_world())
        sessions = loop.stack.authz.sessions
        store = SqlCheckpointStore(
            database=loop.stack.primary.database,
            writer=loop.stack.primary.writer,
            audit=sessions.audit,
            outbox=loop.stack.outbox,
        )
        checkpoints = CheckpointService(
            store=store,
            signer=signing.service,
            clock=signing.clock,
            writer_context=loop.stack.authz.contexts.checkpoint_writer,
        )
        with versioned_bucket(localstack_endpoint.aws_client("s3"), "vigia-monthly") as bucket:
            evidence = S3Storage(localstack_endpoint.storage_settings(bucket), sessions.clock)
            yield World(
                loop=loop,
                checkpoints=checkpoints,
                config=PostgresLoopConfigurationRepository(loop.stack.primary.database),
                repository=MonthlyPackageRepository(database=loop.stack.primary.database),
                evidence=evidence,
                signer=signing.service,
            )


def _seed_subjects(
    world: World, organization: LoopOrganization, count: int
) -> list[IngestedSubject]:
    subjects = []
    for _ in range(count):
        world.clock.advance(3_600)
        subjects.append(world.loop.ingest_finding(organization))
    return subjects


# --- 1. compose_and_seal, dos veces: sella exactamente una vez -------------------------------


def test_compose_and_seal_twice_seals_exactly_once(world: World) -> None:
    world.clock.set(_BASELINE)
    organization = world.loop.organization()
    world.configure(organization)
    world.grant_coordinator(organization)
    zone = world.zone(organization)
    period = "2026-09"
    period_from, period_to = month_bounds(BOGOTA, period)

    world.clock.set(period_from + timedelta(days=5))
    subjects = _seed_subjects(world, organization, 8)
    outcomes = [
        "confirmed", "confirmed", "false_positive", "false_positive",
        "authorized_operation", "confirmed", "false_positive", "confirmed",
    ]  # fmt: skip
    first_ids = []
    for subject, outcome in zip(subjects, outcomes, strict=True):
        world.clock.advance(60)
        first_ids.append(world.classify(subject, outcome=outcome))

    # Cuatro reclasificaciones: la población de la muestra auditada (BR-LAZ-105).
    for subject, previous_outcome, previous_id in zip(
        subjects[:4], outcomes[:4], first_ids[:4], strict=True
    ):
        world.clock.advance(60)
        world.classify(
            subject,
            sequence=2,
            outcome="confirmed",
            previous_outcome=previous_outcome,
            previous_record_id=previous_id,
        )

    world.clock.set(period_to + timedelta(days=5))
    context = world.loop.event_context(organization)
    composer = world.composer()

    first = world.loop.stack.run(
        composer.compose_and_seal(
            context, zone, period, BOGOTA,
            sample_percent=_SAMPLE_PERCENT, sample_min=_SAMPLE_MIN, sample_max=_SAMPLE_MAX,
        )
    )  # fmt: skip
    assert isinstance(first, ComposeOutcome)
    rows = world.sealed_rows(zone.zone_id)
    assert [row["state"] for row in rows] == ["sealed"]
    assert rows[0]["package_id"] == first.package_id
    assert world.produced_periods(zone.zone_id) == [period]

    second = world.loop.stack.run(
        composer.compose_and_seal(
            context, zone, period, BOGOTA,
            sample_percent=_SAMPLE_PERCENT, sample_min=_SAMPLE_MIN, sample_max=_SAMPLE_MAX,
        )
    )  # fmt: skip
    assert second is None
    assert world.sealed_rows(zone.zone_id) == rows
    assert world.produced_periods(zone.zone_id) == [period]


# --- 2. recuperación tras varios meses de interrupción, del más viejo al más nuevo -----------


def test_catchup_seals_every_due_month_oldest_first(world: World) -> None:
    world.clock.set(_BASELINE)
    organization = world.loop.organization()
    world.configure(organization)
    world.grant_coordinator(organization)
    zone = world.zone(organization)

    # Un hallazgo cualquiera para que la cadena de la planta no esté vacía
    # (``MonthlyPackageUnsealable``).
    world.clock.set(datetime(2026, 5, 10, tzinfo=UTC))
    subject = world.loop.ingest_finding(organization)
    world.classify(subject, outcome="confirmed")

    # "Hoy" muy por delante: varios meses vencidos y aún sin sellar.
    world.clock.set(datetime(2026, 9, 20, tzinfo=UTC))
    task = world.task(max_catchup_months=4)
    context = world.loop.event_context(organization)

    async def run_once() -> None:
        async with world.database.transaction(context) as transaction:
            await task(transaction)

    world.loop.stack.run(run_once())

    expected = ["2026-05", "2026-06", "2026-07", "2026-08"]
    rows = world.sealed_rows(zone.zone_id)
    periods = [row["period"].strftime("%Y-%m") for row in rows]
    assert sorted(set(periods) & set(expected)) == expected
    assert all(
        row["state"] == "sealed" for row in rows if row["period"].strftime("%Y-%m") in expected
    )

    # El expediente los recibió del más viejo al más nuevo, de uno en uno (PAT-LAZ-RES-05).
    assert world.produced_periods(zone.zone_id) == expected


# --- 3. concurrencia: nunca sellado dos veces -------------------------------------------------


@dataclass(frozen=True)
class _SkewedClock:
    """Adelanta ``base`` una desviación fija: dos composidores con el mismo ``base`` pero
    desviaciones distintas producen un ``generated_at`` (y por tanto un zip) distinguible, sin
    que ninguno de los dos pierda la hora real que ``world.clock`` gobierna (``advance``/``set``
    del reloj subyacente los mueve a los dos por igual, ronda 2 del PR #161: la carrera antes
    producía bytes idénticos porque ambos compositores leían el mismo reloj simulado sin mover
    la hora entre ellos, y la comparación de SHA-256 del objeto subido nunca podía distinguir al
    ganador de una perdedora que hubiera subido después)."""

    base: Clock
    offset: timedelta

    def now(self) -> datetime:
        return self.base.now() + self.offset

    def monotonic(self) -> float:
        return self.base.monotonic()

    def sleep(self, seconds: float) -> None:
        self.base.sleep(seconds)


async def _race(
    world: World, context: Any, zone: ZoneRef, period: str
) -> list[ComposeOutcome | BaseException | None]:
    first = world.composer()
    second = world.composer(clock=_SkewedClock(world.clock, timedelta(seconds=5)))
    kwargs = dict(sample_percent=_SAMPLE_PERCENT, sample_min=_SAMPLE_MIN, sample_max=_SAMPLE_MAX)
    outcomes = await asyncio.gather(
        first.compose_and_seal(context, zone, period, BOGOTA, **kwargs),
        second.compose_and_seal(context, zone, period, BOGOTA, **kwargs),
        return_exceptions=True,
    )
    return list(outcomes)


def test_concurrent_compose_never_seals_twice(world: World) -> None:
    """FS-LAZ-10: dos composiciones a la vez del mismo ``(zone_id, period)`` (como si dos
    ejecuciones creyeran tener, ambas, el arrendamiento de la tarea periódica). Se repite tres
    veces, con una zona y un periodo nuevos cada vez, para no depender de una sola carrera
    (AGENTS.md, «la prueba tiene que fallar con la mutación en ejecuciones repetidas»)."""
    for _ in range(3):
        world.clock.set(_BASELINE)
        organization = world.loop.organization()
        world.configure(organization)
        world.grant_coordinator(organization)
        zone = world.zone(organization)
        period = "2026-09"
        period_from, period_to = month_bounds(BOGOTA, period)

        world.clock.set(period_from + timedelta(days=5))
        subject = world.loop.ingest_finding(organization)
        world.classify(subject, outcome="confirmed")
        world.clock.set(period_to + timedelta(days=5))

        context = world.loop.event_context(organization)
        outcomes = world.loop.stack.run(_race(world, context, zone, period))

        winners = [outcome for outcome in outcomes if isinstance(outcome, ComposeOutcome)]
        losers = [outcome for outcome in outcomes if not isinstance(outcome, ComposeOutcome)]
        assert len(winners) == 1, outcomes
        assert len(losers) == 1, outcomes
        loser = losers[0]
        # El perdedor nunca sella: desde el rework 1 (B2/B3), el arrendamiento de
        # ``accreditation.monthly_package_lease`` es la primera valla real — de las dos
        # composiciones concurrentes, solo una gana la fila del arrendamiento (``acquire_lease``,
        # fenced ``INSERT ... ON CONFLICT``) y la otra devuelve ``None`` sin construir ni subir
        # ningún zip. ``seal``'s ``WHERE ... AND state != 'sealed'`` sigue como vallado simétrico
        # por si dos procesos llegaran a resolver el mismo ``package_id`` con arrendamientos
        # distintos (``_SealLost`` revierte su transacción entera, expediente incluido). Se
        # tolera también una excepción (``BaseException``) por si alguna otra escritura
        # concurrente del intento perdedor choca antes de perder el arrendamiento o el sellado.
        assert loser is None or isinstance(loser, BaseException), outcomes

        rows = world.sealed_rows(zone.zone_id)
        sealed = [row for row in rows if row["state"] == "sealed"]
        assert len(sealed) == 1, rows
        assert sealed[0]["package_id"] == winners[0].package_id
        assert world.produced_periods(zone.zone_id) == [period]

        # Ronda 2: el objeto en vigia-evidence es el del ganador, nunca uno distinguible de una
        # perdedora (los dos compositores de _race ya no comparten el mismo generated_at).
        key = object_key(
            organization_id=zone.organization_id,
            plant_id=zone.plant_id,
            zone_id=zone.zone_id,
            period=period,
        )
        package_bytes = world.loop.stack.run(world.evidence.get_object(key))
        assert hashlib.sha256(package_bytes).hexdigest() == winners[0].package_sha256


def test_expired_lease_lets_a_second_composer_take_over_without_double_sealing(
    world: World,
) -> None:
    """FS-LAZ-10 al pie de la letra (B2/B3 del PR #161): un primer proceso toma el arrendamiento
    de la composición y «muere» sin liberarlo ni sellar nada; el reloj inyectado lo deja vencer
    (``LEASE_DURATION``); dos composiciones nuevas corren a la vez por el mismo
    ``(zone_id, period)``. Solo una sella, y el objeto que queda en ``vigia-evidence`` es
    exactamente el suyo (nunca el de una perdedora que hubiera subido después, P4/P5,
    BR-LAZ-108)."""
    world.clock.set(_BASELINE)
    organization = world.loop.organization()
    world.configure(organization)
    world.grant_coordinator(organization)
    zone = world.zone(organization)
    period = "2026-09"
    period_from, period_to = month_bounds(BOGOTA, period)

    world.clock.set(period_from + timedelta(days=5))
    subject = world.loop.ingest_finding(organization)
    world.classify(subject, outcome="confirmed")
    world.clock.set(period_to + timedelta(days=5))
    context = world.loop.event_context(organization)

    async def acquire_stale_lease() -> None:
        async with world.database.transaction(context) as transaction:
            lease = await world.repository.acquire_lease(
                transaction,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                period=period,
                owner="proceso-zombie",
                now=world.clock.now(),
                duration=timedelta(minutes=10),
            )
            assert lease is not None, "el primer proceso debe poder tomar el arrendamiento"

    world.loop.stack.run(acquire_stale_lease())
    world.clock.advance(11 * 60)  # el arrendamiento del «zombie» vence a los 10 minutos.

    outcomes = world.loop.stack.run(_race(world, context, zone, period))
    winners = [outcome for outcome in outcomes if isinstance(outcome, ComposeOutcome)]
    assert len(winners) == 1, outcomes

    rows = world.sealed_rows(zone.zone_id)
    sealed = [row for row in rows if row["state"] == "sealed"]
    assert len(sealed) == 1, rows
    assert sealed[0]["package_id"] == winners[0].package_id
    assert world.produced_periods(zone.zone_id) == [period]

    key = object_key(
        organization_id=zone.organization_id,
        plant_id=zone.plant_id,
        zone_id=zone.zone_id,
        period=period,
    )
    package_bytes = world.loop.stack.run(world.evidence.get_object(key))
    assert hashlib.sha256(package_bytes).hexdigest() == winners[0].package_sha256


# --- 4. zona horaria de la planta, nunca UTC ---------------------------------------------------


def test_timezone_of_the_plant_decides_the_due_period_not_utc(world: World) -> None:
    """Un instante en el que Bogotá sigue en el mes M y Madrid ya está en M+1 (local): la tarea
    calcula periodos vencidos distintos para cada zona, con el huso de **su propia** planta."""
    world.clock.set(_BASELINE)
    bogota_org = world.loop.organization()
    madrid_org = world.loop.organization()
    world.configure(bogota_org, timezone=BOGOTA)
    world.configure(madrid_org, timezone=MADRID)
    world.grant_coordinator(bogota_org)
    world.grant_coordinator(madrid_org)

    instant = datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
    assert local_date(instant, BOGOTA) == datetime(2026, 10, 31, tzinfo=UTC).date()
    assert local_date(instant, MADRID) == datetime(2026, 11, 1, tzinfo=UTC).date()

    for organization in (bogota_org, madrid_org):
        world.clock.set(datetime(2026, 9, 10, tzinfo=UTC))
        subject = world.loop.ingest_finding(organization)
        world.classify(subject, outcome="confirmed")

    world.clock.set(instant)
    assert today(world.clock, BOGOTA) == datetime(2026, 10, 31, tzinfo=UTC).date()
    assert today(world.clock, MADRID) == datetime(2026, 11, 1, tzinfo=UTC).date()

    task = world.task(max_catchup_months=2)
    for organization in (bogota_org, madrid_org):
        context = world.loop.event_context(organization)

        async def run_once(ctx: Any = context) -> None:
            async with world.database.transaction(ctx) as transaction:
                await task(transaction)

        world.loop.stack.run(run_once())

    bogota_periods = {
        row["period"].strftime("%Y-%m") for row in world.sealed_rows(bogota_org.zone_id)
    }
    madrid_periods = {
        row["period"].strftime("%Y-%m") for row in world.sealed_rows(madrid_org.zone_id)
    }
    # Octubre ya venció en Madrid (hoy local = 1 de noviembre) pero todavía no en Bogotá (hoy
    # local = 31 de octubre): la tarea usa la zona horaria de la planta, no UTC ni la del proceso.
    assert "2026-10" in madrid_periods
    assert "2026-10" not in bogota_periods


def test_lease_still_valid_reports_false_once_it_expires(world: World) -> None:
    """B3 del PR #161: ``lease_still_valid`` (la comprobación justo antes de subir el zip) deja
    de confirmar al dueño en cuanto pasa ``lease_until``, aunque nadie más lo haya tomado todavía
    — así un proceso que se queda componiendo más de la cuenta nunca sube un zip sin dueño
    vigente que responda por él (P4/P5, BR-LAZ-108). Prueba directa y determinista del
    repositorio: solo el paso del reloj inyectado, sin concurrencia real."""
    world.clock.set(_BASELINE)
    organization = world.loop.organization()
    world.configure(organization)
    zone = world.zone(organization)
    period = "2026-09"
    context = world.loop.event_context(organization)

    async def scenario() -> tuple[bool, bool]:
        async with world.database.transaction(context) as transaction:
            lease = await world.repository.acquire_lease(
                transaction,
                plant_id=zone.plant_id,
                zone_id=zone.zone_id,
                period=period,
                owner="composer-a",
                now=world.clock.now(),
                duration=timedelta(minutes=10),
            )
        assert lease is not None
        async with world.database.transaction(context) as transaction:
            still_fresh = await world.repository.lease_still_valid(
                transaction,
                zone_id=zone.zone_id,
                period=period,
                owner="composer-a",
                now=world.clock.now(),
            )
        world.clock.advance(11 * 60)
        async with world.database.transaction(context) as transaction:
            after_expiry = await world.repository.lease_still_valid(
                transaction,
                zone_id=zone.zone_id,
                period=period,
                owner="composer-a",
                now=world.clock.now(),
            )
        return still_fresh, after_expiry

    still_fresh, after_expiry = world.loop.stack.run(scenario())
    assert still_fresh is True
    assert after_expiry is False


# --- 5. el zip sellado verifica íntegro con el verificador embebido (rework 1, VIG-239) ------


def _run_verifier(package: Path, out: Path) -> tuple[subprocess.CompletedProcess[str], Any]:
    """``vigia_verify.py`` (el mismo archivo endurecido que el paquete incluye) sobre
    ``package``, igual que lo invocaría un tercero sin la plataforma (sin ``--manifest-sha256``
    ni ``--extract``: solo lo que el propio zip trae debe bastar para anclarlo)."""
    completed = subprocess.run(
        [sys.executable, str(VERIFIER_FILE), str(package), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    document = json.loads(out.read_text("utf-8")) if out.is_file() else None
    return completed, document


def test_sealed_package_verifies_intact_with_the_embedded_verifier(world: World) -> None:
    """VIG-239 (rework 1 del PR #161): ``compose_and_seal`` sube un zip verificable de verdad.

    La revisión encontró que el manifiesto de ``_build_zip`` no declaraba ``manifest_version``,
    ni firma, ni punto de control ni claves públicas: ``vigia_verify.py`` lo rechazaba con
    ``broken``/``manifest_invalid`` en ``/manifest_version``. Esta prueba sella un paquete real
    sobre PostgreSQL y LocalStack, descarga sus bytes de ``vigia-evidence`` tal cual quedaron
    subidos y ejecuta el verificador endurecido real (el mismo ``vigia_verify.py`` que el zip
    incluye) sobre ellos, sin tocar la aplicación: ``intact`` (código 0) y anclado por la firma
    del manifiesto, no solo coherente consigo mismo."""
    world.clock.set(_BASELINE)
    organization = world.loop.organization()
    world.configure(organization)
    world.grant_coordinator(organization)
    zone = world.zone(organization)
    period = "2026-09"
    period_from, period_to = month_bounds(BOGOTA, period)

    world.clock.set(period_from + timedelta(days=5))
    subject = world.loop.ingest_finding(organization)
    world.classify(subject, outcome="confirmed")

    world.clock.set(period_to + timedelta(days=5))
    context = world.loop.event_context(organization)
    composer = world.composer()
    outcome = world.loop.stack.run(
        composer.compose_and_seal(
            context, zone, period, BOGOTA,
            sample_percent=_SAMPLE_PERCENT, sample_min=_SAMPLE_MIN, sample_max=_SAMPLE_MAX,
        )
    )  # fmt: skip
    assert isinstance(outcome, ComposeOutcome)

    key = object_key(
        organization_id=zone.organization_id,
        plant_id=zone.plant_id,
        zone_id=zone.zone_id,
        period=period,
    )
    package_bytes = world.loop.stack.run(world.evidence.get_object(key))
    assert hashlib.sha256(package_bytes).hexdigest() == outcome.package_sha256

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        package_path = root / "package.zip"
        package_path.write_bytes(package_bytes)
        completed, document = _run_verifier(package_path, root / "result.json")

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert document is not None, completed.stdout + completed.stderr
    assert document["status"] == "intact", document
    assert document["broken"] is None, document
    assert document["anchored_by"] == ["manifest_signature"], document
    assert document["artifacts_verified"] == 6, document
    assert document["checkpoints_verified"] == 1, document

    # Ronda 2: AC #4 exige metrics/exposure/coverage_summary declarados no calculables también en
    # el manifiesto firmado (no solo en el contenido del expediente, que ya lo hacía).
    with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["metrics"] and all(
        item["status"] == "not_computable" and item["value"] is None for item in manifest["metrics"]
    ), manifest["metrics"]
    assert manifest["exposure"] == {
        "status": "not_computable",
        "confirmed_minutes": None,
        "pending_classification_minutes": None,
        "observable_coverage_percent": None,
    }, manifest["exposure"]
    assert manifest["coverage_summary"] == {"status": "not_computable"}, manifest[
        "coverage_summary"
    ]
