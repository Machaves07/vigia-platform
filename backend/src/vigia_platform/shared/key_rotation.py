"""Conexión de ``shared.signing`` con el expediente, la bandeja y las tareas periódicas.

``shared.signing`` es un módulo crítico aislado (NFR-NUC-25): no conoce SQLAlchemy. Lo que sí la
necesita vive aquí:

- ``LedgerKeyEventWriter``: el ``KeyEventWriter`` sobre ``EscritorExpediente``. Escribe
  ``key_rotated`` y ``key_set_published`` en la cadena de la organización proveedora; el segundo
  publica en la misma transacción el evento ``key_set_published`` de la bandeja, que U-03 entrega
  en el latido (BR-NUC-86). Un rechazo del expediente sube como ``KeyEventRejected``.
- ``LedgerRotationRecorder``: lo mismo más la auditoría ``key_rotated``, **dentro** de la
  transacción en que ``SqlSigningKeyStore`` confirma la clave (rotación y auditoría atómicas).
- La tarea periódica ``key_rotation_reminder`` (diaria, BR-NUC-85): en la organización
  proveedora publica ``key_rotation_due`` para cada clave activa que acaba de entrar en los 45
  días, rota las que vencen en 30 días o menos, retira las ``overlapping`` vencidas y publica
  ``signing_key_days_to_expiry``. En las demás organizaciones no hace nada.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.outbox.registries import (
    PeriodicHandler,
    PeriodicTask,
    PeriodicTaskRegistry,
    Schedule,
)
from vigia_platform.shared.signing import RotationCommit, SigningService
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "KEY_ROTATION_REMINDER",
    "KEY_ROTATION_REMINDER_PERIOD",
    "KEY_ROTATION_REMINDER_SCHEDULE",
    "KeyEventRejected",
    "LedgerKeyEventWriter",
    "LedgerRotationRecorder",
    "key_rotation_reminder_handler",
    "register_key_rotation_reminder",
]

KEY_ROTATION_REMINDER: Final = "key_rotation_reminder"
KEY_ROTATION_REMINDER_SCHEDULE: Final = Schedule.daily(hour=2)
"""Diaria a las 02:00 UTC ``[objetivo propio]``, fuera de la de puntos de control (00:00)."""
KEY_ROTATION_REMINDER_PERIOD: Final = timedelta(days=1)

_log = get_logger("shared.key_rotation")


class KeyEventRejected(Exception):
    """El expediente rechazó ``key_rotated`` o ``key_set_published`` (código cerrado)."""

    def __init__(self, record_type: str, rejection: LedgerRejection) -> None:
        super().__init__(f"{record_type} rechazado por el expediente: {rejection.code.value}")
        self.record_type = record_type
        self.rejection = rejection


@repository
class LedgerKeyEventWriter:
    """``KeyEventWriter`` sobre ``EscritorExpediente``."""

    def __init__(self, writer: EscritorExpediente) -> None:
        self._writer = writer

    async def key_rotated(self, context: ScopeContext, content: Mapping[str, Any]) -> None:
        await self._write(context, "key_rotated", content, ())

    async def key_set_published(
        self, context: ScopeContext, content: Mapping[str, Any], event: Mapping[str, Any]
    ) -> None:
        await self._write(
            context,
            "key_set_published",
            content,
            (NewEvent(event_name="key_set_published", payload=dict(event)),),
        )

    async def _write(
        self,
        context: ScopeContext,
        record_type: str,
        content: Mapping[str, Any],
        events: tuple[NewEvent, ...],
    ) -> None:
        result = await self._writer.write(context, record_type, dict(content), events=events)
        if isinstance(result, LedgerRejection):
            _log.error("registro de rotación de claves rechazado por el expediente")
            raise KeyEventRejected(record_type, result)


class LedgerRotationRecorder:
    """``RotationRecorder``: registros y auditoría de una rotación en **su** transacción.

    ``SqlSigningKeyStore`` lo llama dentro de la transacción que confirma la clave: escribe
    ``key_rotated`` y, si se publica conjunto, ``key_set_published`` (con su evento de la bandeja)
    con ``EscritorExpediente``, y la entrada de auditoría ``key_rotated`` con ``AuditWriter``. Un
    rechazo o un fallo revierte también la clave (revisión de VIG-93). Sin transacción (dobles en
    memoria) abre una propia antes de que el doble aplique nada.
    """

    def __init__(
        self, *, database: LedgerDatabase, writer: EscritorExpediente, audit: AuditWriter
    ) -> None:
        self._database = database
        self._writer = writer
        self._audit = audit

    def __repr__(self) -> str:
        return "LedgerRotationRecorder()"

    async def record(self, commit: RotationCommit, transaction: Any | None) -> None:
        context = commit.context
        if context is None or commit.rotated is None:
            raise ValueError("la rotación llega sin su contexto ni su contenido")
        if transaction is None:
            async with self._database.transaction(context) as own:
                await self._record(commit, context, commit.rotated, own)
            return
        if not isinstance(transaction, Transaction):
            raise TypeError("transaction debe ser la de shared.db")
        await self._record(commit, context, commit.rotated, transaction)

    async def _record(
        self,
        commit: RotationCommit,
        context: ScopeContext,
        rotated: Mapping[str, Any],
        transaction: Transaction,
    ) -> None:
        await self._write(context, transaction, "key_rotated", rotated, ())
        if commit.published is not None:
            content, event = commit.published
            await self._write(
                context,
                transaction,
                "key_set_published",
                content,
                (NewEvent(event_name="key_set_published", payload=dict(event)),),
            )
        await self._audit.append(
            context,
            AuditOperation.KEY_ROTATED,
            filters=dict(commit.audit_filters),
            transaction=transaction,
        )

    async def _write(
        self,
        context: ScopeContext,
        transaction: Transaction,
        record_type: str,
        content: Mapping[str, Any],
        events: tuple[NewEvent, ...],
    ) -> None:
        result = await self._writer.write(
            context, record_type, dict(content), events=events, transaction=transaction
        )
        if isinstance(result, LedgerRejection):
            _log.error("registro de rotación de claves rechazado por el expediente")
            raise KeyEventRejected(record_type, result)


def key_rotation_reminder_handler(
    service: SigningService,
    outbox: OutboxPort,
    clock: Clock,
    *,
    provider_organization_id: uuid.UUID,
) -> PeriodicHandler:
    """Manejador de ``key_rotation_reminder`` para ``PeriodicTaskRegistry``."""

    async def handler(transaction: Transaction) -> None:
        context = transaction.context
        if context.organization_id != provider_organization_id:
            return
        # Decide sobre el estado confirmado: otro proceso pudo haber rotado desde el último
        # refresco. Si la base o el gestor no responden, decide con lo de memoria y cada
        # rotación vuelve a leer la base de todos modos.
        await service.refresh()
        now = clock.now()
        decision = service.reminder_decision(now, period=KEY_ROTATION_REMINDER_PERIOD)
        for key in decision.notices:
            await outbox.publish(
                transaction,
                NewEvent(
                    event_name="key_rotation_due",
                    payload={
                        "key_id": key.key_id,
                        "purpose": key.purpose.value,
                        "valid_until": format_timestamp(key.valid_until),
                    },
                ),
            )
        for purpose in decision.rotations:
            await service.rotate(purpose, context=context)
        await service.retire_expired()
        service.report_days_to_expiry(clock.now())

    return handler


def register_key_rotation_reminder(
    registry: PeriodicTaskRegistry, handler: PeriodicHandler
) -> PeriodicTask:
    """Registra la tarea diaria ``key_rotation_reminder`` de U-02 (``domain-entities.md`` §4.3)."""
    return registry.register(
        KEY_ROTATION_REMINDER, KEY_ROTATION_REMINDER_SCHEDULE, handler, unit=ActorUnit.U02
    )
