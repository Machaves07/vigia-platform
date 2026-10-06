"""``alert_expiring_certificates``: ``certificate_expiring`` a 15 días del vencimiento (TASK-225).

BR-GOB-65 (``alert_before_days``; SECURITY-14, NFR-CTR-13), BR-GOB-76 y 81; nota de cadencias de
BL §2.6 (antes ``certificate_expiring``, diaria). Una vez al día, por organización
(``register_alert_expiring_certificates``; el registro en la raíz es de TASK-227), por lotes de
``batch_size`` nodos:

- los nodos no revocados ni dados de baja cuya credencial **vigente** (``active`` u
  ``overlapping``) de vencimiento más lejano vence en 15 días o menos (o ya venció) y que no tienen
  ``certificate_expiring`` abierta (la misma condición que el aviso de ``GET /fleet/nodes``);
- ``certificate_expiring`` por transición, en la primera evaluación (sin histéresis), con ``since``
  el instante en que la condición empezó: 15 días antes del vencimiento.

La baja, tras la rotación, la hace ``evaluate_fleet_alarms``. Una segunda ejecución ya no encuentra
el nodo (su alarma está abierta); una ejecución **solapada** que lo leyó antes de que la primera
confirmara choca con la ranura de ``open_fleet_alarm`` (``AlarmAlreadyOpen``) y se deshace entera:
nunca dos alarmas abiertas de la misma clase y nodo.

Ningún paso lee la hora del sistema: ``Clock`` inyectado.
"""

from __future__ import annotations

import uuid
from typing import Final

from vigia_platform.fleet.application.fleet_alarms import (
    BATCH_SIZE,
    AlarmDependencies,
    AlarmReport,
    now_of,
    raise_in,
)
from vigia_platform.fleet.domain.enums import FleetAlarmKind
from vigia_platform.fleet.domain.fleet_alarm import NewAlarm
from vigia_platform.fleet.domain.fleet_warnings import CERTIFICATE_ALERT_BEFORE
from vigia_platform.shared.context import ActorUnit, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.outbox.registries import PeriodicTask, PeriodicTaskRegistry, Schedule

__all__ = [
    "ALERT_EXPIRING_CERTIFICATES",
    "ALERT_EXPIRING_CERTIFICATES_SCHEDULE",
    "ExpiringCertificateAlerter",
    "register_alert_expiring_certificates",
]

ALERT_EXPIRING_CERTIFICATES: Final = "alert_expiring_certificates"
ALERT_EXPIRING_CERTIFICATES_SCHEDULE: Final = Schedule.daily(hour=3)
"""Diaria (nota de cadencias de BL §2.6; NFR-GOB-12), a las 03:00 UTC `[objetivo propio]`."""


@repository
class ExpiringCertificateAlerter:
    """El manejador de ``alert_expiring_certificates`` para la transacción de una organización."""

    def __init__(self, deps: AlarmDependencies, *, batch_size: int = BATCH_SIZE) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size debe ser al menos 1")
        self._deps = deps
        self._batch_size = batch_size

    def __repr__(self) -> str:
        return "ExpiringCertificateAlerter()"

    async def alert(self, transaction: Transaction) -> AlarmReport:
        """Una pasada sobre los nodos de la organización de ``transaction``."""
        deps = self._deps
        now = now_of(deps.clock)
        report = AlarmReport()
        after: uuid.UUID | None = None
        while True:
            expiring = await deps.store.expiring_certificates(
                transaction,
                until=now + CERTIFICATE_ALERT_BEFORE,
                after=after,
                limit=self._batch_size,
            )
            report.raised += await raise_in(
                deps,
                transaction,
                [
                    NewAlarm(
                        alarm_kind=FleetAlarmKind.CERTIFICATE_EXPIRING,
                        plant_id=node.plant_id,
                        node_id=node.node_id,
                        since=min(node.expires_at - CERTIFICATE_ALERT_BEFORE, now),
                    )
                    for node in expiring
                ],
                now,
            )
            if len(expiring) < self._batch_size:
                return report
            after = expiring[-1].node_id


def register_alert_expiring_certificates(
    registry: PeriodicTaskRegistry, alerter: ExpiringCertificateAlerter
) -> PeriodicTask:
    """Registra ``alert_expiring_certificates`` una vez al día, por organización (TASK-227)."""

    async def handler(transaction: Transaction) -> None:
        await alerter.alert(transaction)

    return registry.register(
        ALERT_EXPIRING_CERTIFICATES,
        ALERT_EXPIRING_CERTIFICATES_SCHEDULE,
        handler,
        unit=ActorUnit.U03,
    )
