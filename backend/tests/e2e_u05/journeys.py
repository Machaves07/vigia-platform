"""Precondiciones sintéticas de los recorridos SST y mando, sobre servicios reales."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast
from zoneinfo import ZoneInfo

from tests.conformance.platform_target import WALL
from tests.integration.test_u04_end_to_end import (
    _console,
    _installer_session,
    _load_plant_policy,
    _projection,
)
from tests.load.conftest import LoadTarget
from tests.load.provision import Fleet
from vigia_platform.accreditation.monthly.package import MonthlyPackageComposer
from vigia_platform.accreditation.monthly.repository import MonthlyPackageRepository, ZoneRef
from vigia_platform.accreditation.monthly.task import MonthlyPackageTask, register_monthly_package
from vigia_platform.accreditation.registration import _u04_checkpoints, metrics_service
from vigia_platform.ledger.application.reader import LectorExpediente
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.shared.observability.metrics import get_metrics
from vigia_platform.shared.outbox.registries import PeriodicTaskRegistry
from vigia_platform.shared.runtime.config import RuntimeConfig
from vigia_platform.shared.runtime.core import build_core, s3_storage
from vigia_platform.shared.runtime.units import RuntimeDatabase, registered_units


def prepare_journeys(
    target: LoadTarget, fleet: Fleet, users: dict[str, Any], *, profile: str, seed: int
) -> dict[str, Any]:
    """Política por HTTP, paquete por su compositor y hallazgos distintos por motor.

    Solo la identidad de las cuentas se configura con el bootstrap del arnés. Ninguna
    clasificación, firma o compromiso del recorrido se siembra ni se sustituye su API.
    """
    coordinator = uuid.UUID(users["coordinator_sst"]["user_id"])
    target.stack.admin(
        [
            (
                "UPDATE identity.user_account SET professional_license = $2 WHERE user_id = $1",
                (coordinator, "SST-SINTETICA-E2E"),
            ),
        ]
    )
    installer, cookie = _installer_session(target, fleet.console.concession_id)
    console = _console(target, installer, cookie, fleet.console.concession_id)
    try:
        for plant in fleet.plant_ids:
            _load_plant_policy(console, plant)
    finally:
        console.client.close()

    # Un mes ya vencido: ni adelantar el reloj de la plataforma ni sellar un mes incompleto.
    now = WALL.now().astimezone(ZoneInfo("America/Bogota"))
    previous_month = (now.date().replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    period = previous_month.strftime("%Y-%m")
    # Precondición histórica sintética, como en test_metrics_routes: la zona ya existía
    # durante el mes. Solo metadatos de identidad; no se reescribe ningún registro del lazo.
    target.stack.admin(
        [
            (
                "UPDATE identity.zone SET created_at = $2 WHERE zone_id = $1",
                (
                    fleet.zone_ids[0],
                    dt.datetime.combine(previous_month, dt.time(), tzinfo=now.tzinfo),
                ),
            )
        ]
    )

    async def seal_monthly() -> None:
        assert target.api.runtime is not None
        runtime = target.api.runtime
        config = RuntimeConfig.from_environ(target.stack.environ)
        core = build_core(
            config,
            database=cast(RuntimeDatabase, runtime.database),
            clock=runtime.clock,
            metrics=get_metrics(),
            provider_organization_id=target.stack.provider_organization_id,
            units=registered_units(),
            evidence=s3_storage(config, config.require("evidence_bucket"), runtime.clock),
        )
        # El compositor necesita el mismo arranque que API/worker: catálogos sellados y
        # firmante listo. No se inventan puntos de control ni se elude su verificación.
        for synchronize in core.synchronizers:
            await synchronize()
        services = core.services
        await services.signing.start()
        repository = MonthlyPackageRepository(database=services.database)
        composer = MonthlyPackageComposer(
            database=services.database,
            reader=LectorExpediente(
                database=services.database, audit=services.audit, clock=services.clock
            ),
            repository=repository,
            evidence=services.require_evidence(),
            checkpoints=_u04_checkpoints(services),
            writer=services.writer,
            clock=services.clock,
            signer=services.signing,
            program_metrics=metrics_service(services),
        )
        task = register_monthly_package(
            PeriodicTaskRegistry(),
            MonthlyPackageTask(
                config=PostgresLoopConfigurationRepository(services.database),
                repository=repository,
                composer=composer,
                clock=services.clock,
            ),
        )
        context = services.contexts.context_for_organization(task, fleet.organization_id)
        result = await composer.compose_and_seal(
            context,
            ZoneRef(
                organization_id=fleet.organization_id,
                plant_id=fleet.plant_ids[0],
                zone_id=fleet.zone_ids[0],
            ),
            period,
            "America/Bogota",
            sample_percent=10,
            sample_min=1,
            sample_max=10,
        )
        assert result is not None

    target.api.call(seal_monthly())
    findings = sorted(
        str(row["finding_state_id"])
        for row in _projection(target, fleet.organization_id)
        if row["subject_kind"] == "finding"
    )
    assert len(findings) >= 6, "Se necesitan dos hallazgos distintos por motor"
    return {
        "profile": profile,
        "seed": seed,
        "period": period,
        "today": now.date().isoformat(),
        "review_date": (now.date() + dt.timedelta(days=30)).isoformat(),
        "line_finding_id": next(
            str(row["finding_state_id"])
            for row in _projection(target, fleet.organization_id)
            if row["subject_kind"] == "finding" and row["zone_id"] == fleet.zone_ids[0]
        ),
        "findings": {
            engine: {"closure": findings[index * 2], "unclosable": findings[index * 2 + 1]}
            for index, engine in enumerate(("chromium", "firefox", "webkit"))
        },
    }
