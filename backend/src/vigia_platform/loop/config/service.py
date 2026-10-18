"""``loop.config`` (LC-LAZ-19): la configuración del lazo por planta (SUP-U05-38 y 39).

``configuration`` (``consultar``, ``GET /loop/plants/{plant_id}/configuration``,
``hierarchy.read`` sobre la planta): la fila vigente. La primera vez que se lee una planta sin
fila se crean sus **valores por defecto** (``LoopSettings.defaults``, la zona de
``Plant.timezone``) con su registro de origen ``loop_configuration_changed`` (versión 1, todos los
parámetros como nuevos, motivo fijo ``initial_defaults``).

``change`` (``modificar``, ``PUT``, ``organization.settings`` sobre la planta): los parámetros que
cambian y el motivo.

1. **Antes de la transacción**: planta de la organización y dentro del alcance (si no,
   ``ResourceNotFound``); motivo de 10 a 500 caracteres que pasa la política de texto libre (sin
   motivo, en blanco o más corto, ``configuration_change_reason_required``; más largo o con
   caracteres rechazados, ``invalid_request``); cada entero en su rango (si no,
   ``deadline_out_of_range``; si no es un entero o el nombre no existe, ``invalid_request``) y el
   calendario en su forma (``invalid_request``).
2. **En una sola transacción** (BR-LAZ-146): exclusión de la planta; la fila vigente (si no
   existe, la de los valores por defecto con su registro, como en la lectura); las reglas entre
   parámetros sobre el resultado (``deadline_out_of_range``); si nada cambia, la fila vigente sin
   escribir; si no, **exactamente un** ``loop_configuration_changed`` con los valores anterior y
   nuevo de lo que cambia y la versión siguiente, y la fila (``UPDATE`` condicional a la versión
   leída). Si algo falla, no queda ni registro ni fila.

**Un cambio no altera nada ya fijado** (BR-LAZ-145, PR-LAZ-47): este módulo solo escribe su fila
y su registro; un plazo ya asignado lo guarda el módulo que lo asignó, calculado con la versión que
leyó (``PostgresLoopConfigurationRepository.settings_in``, con la guarda de contexto).
``applies_to_new_items_only`` es constante ``true`` en el registro.

**Orden de candados**: primero el de configuración de la planta (``loop_configuration|<planta>``)
y después la cadena de la planta del expediente (la toma el escritor al anexar). Ninguna operación
toma la cadena antes que este candado.

**Bajo concesión de proveedor** las dos operaciones responden como inexistentes (con su
``authorization_denied``): ``organization.settings`` no está en la columna ``provider_installer``
y ``hierarchy.read`` sí, pero la configuración del lazo no es de la puesta en marcha y su primera
lectura escribe en el expediente del cliente.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final

from vigia_contracts.models.enumerations import AcceptanceStatus

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import (
    FreeTextField,
    FreeTextPolicyRegistry,
    FreeTextRejected,
    FreeTextRejection,
)
from vigia_platform.loop.calendar import BusinessCalendar, CalendarInvalid
from vigia_platform.loop.config.parameters import (
    INITIAL_DEFAULTS_REASON,
    INTEGER_PARAMETERS,
    ConfigurationRejected,
    LoopSettings,
    PlantLoopConfiguration,
    changed,
    check_values,
    setting_documents,
)
from vigia_platform.loop.config.repository import PostgresLoopConfigurationRepository
from vigia_platform.loop.detail_codes import LoopDetailCode
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.signing.keys import format_timestamp, to_millisecond

__all__ = [
    "LOOP_CONFIGURATION_CHANGED",
    "REASON_MAX_CHARS",
    "REASON_MIN_CHARS",
    "ConfigurationRequestInvalid",
    "ConfigurationUnavailable",
    "LoopConfigurationService",
]

LOOP_CONFIGURATION_CHANGED: Final = "loop_configuration_changed"
REASON_MIN_CHARS: Final = 10
REASON_MAX_CHARS: Final = 500
"""Límites de ``ChangeReason`` (``loop.record_types``)."""
_REASON: Final = FreeTextField(
    LOOP_CONFIGURATION_CHANGED, "/reason", REASON_MIN_CHARS, REASON_MAX_CHARS
)


class ConfigurationRequestInvalid(Exception):
    """El cuerpo no tiene la forma del esquema estricto: ``invalid_request`` sin ``detail_code``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class ConfigurationUnavailable(ExternalDependencyDown):
    """Transitorio: la fila ya no tenía la versión leída (respaldo del candado)."""


def _written(written: Receipt | LedgerRejection) -> None:
    """Un rechazo o un duplicado revierten la transacción del llamador."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            raise ConfigurationRequestInvalid("texto libre rechazado por el expediente")
        if written.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT:
            raise ConfigurationUnavailable("loop_configuration_race")
        raise RuntimeError(f"loop_configuration_changed rechazado: {written.code.value}")
    if written.status is not AcceptanceStatus.ACCEPTED:
        raise ConfigurationUnavailable("loop_configuration_race")


@repository
class LoopConfigurationService:
    """``loop.config``: consultar y modificar la configuración del lazo de una planta."""

    def __init__(
        self,
        *,
        repository: PostgresLoopConfigurationRepository,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        free_text: FreeTextPolicyRegistry,
        clock: Clock,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._repository = repository
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._free_text = free_text
        self._clock = clock
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "LoopConfigurationService()"

    @property
    def repository(self) -> PostgresLoopConfigurationRepository:
        return self._repository

    async def _plant(
        self, context: ScopeContext, plant_id: uuid.UUID, key: PermissionKey
    ) -> tuple[ScopeContext, str]:
        """El contexto autorizado sobre la planta y su zona; inexistente o fuera de alcance,
        igual. Bajo concesión, denegado (``authorization_denied``)."""
        if not isinstance(context, ScopeContext) or type(plant_id) is not uuid.UUID:
            raise ResourceNotFound()
        timezone = await self._repository.plant_timezone(context, plant_id)
        if timezone is None:
            raise ResourceNotFound()
        resource = Resource.plant(context.organization_id, plant_id)
        authorized = await self._authorizer.authorize(context, key, resource)
        if authorized.concession_id is not None:
            await self._authorizer.deny(authorized, key, resource)
        return authorized, timezone

    # --- consultar -------------------------------------------------------------------------------

    async def configuration(
        self, context: ScopeContext, plant_id: uuid.UUID
    ) -> PlantLoopConfiguration:
        """La configuración vigente; la de los valores por defecto si la planta no tenía."""
        authorized, timezone = await self._plant(context, plant_id, PermissionKey.HIERARCHY_READ)
        existing = await self._repository.read(authorized, plant_id)
        if existing is not None:
            return existing
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await self._repository.lock_plant(transaction, plant_id)
            return await self._current_or_defaults(transaction, plant_id, timezone)

    # --- modificar -------------------------------------------------------------------------------

    async def change(
        self,
        context: ScopeContext,
        plant_id: uuid.UUID,
        *,
        integers: Mapping[str, object],
        business_calendar: object | None,
        reason: object,
    ) -> PlantLoopConfiguration:
        """Aplica el cambio con su registro; devuelve la configuración que queda."""
        authorized, timezone = await self._plant(
            context, plant_id, PermissionKey.ORGANIZATION_SETTINGS
        )
        reason_text = self._reason(reason)
        new_integers = _integers(integers)
        calendar = _calendar(business_calendar)
        if not new_integers and calendar is None:
            raise ConfigurationRequestInvalid("el cambio no nombra ningún parámetro")
        writer_context = with_unit(authorized, ActorUnit.U04)
        async with self._database.transaction(writer_context) as transaction:
            await self._repository.lock_plant(transaction, plant_id)
            current = await self._current_or_defaults(transaction, plant_id, timezone)
            settings = check_values(current.settings.replaced(new_integers, calendar))
            names = changed(current.settings, settings)
            if not names:
                return current
            following = current.next(
                settings,
                changed_at=self._now(),
                changed_by=_snapshot(writer_context),
                change_reason=reason_text,
            )
            await self._record(
                writer_context, transaction, following, previous=current, names=names
            )
            if not await self._repository.update(transaction, following):
                raise ConfigurationUnavailable("loop_configuration_race")
        return following

    # --- piezas ----------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return to_millisecond(self._clock.now())

    def _reason(self, reason: object) -> str:
        if reason is None or (isinstance(reason, str) and not reason.strip()):
            raise ConfigurationRejected(LoopDetailCode.CONFIGURATION_CHANGE_REASON_REQUIRED)
        if not isinstance(reason, str):
            raise ConfigurationRequestInvalid("el motivo es una cadena")
        if len(reason.strip()) < REASON_MIN_CHARS:
            raise ConfigurationRejected(LoopDetailCode.CONFIGURATION_CHANGE_REASON_REQUIRED)
        try:
            return self._free_text.apply(reason, _REASON)
        except FreeTextRejected as rejected:
            if rejected.reason == FreeTextRejection.TOO_SHORT:
                raise ConfigurationRejected(
                    LoopDetailCode.CONFIGURATION_CHANGE_REASON_REQUIRED
                ) from None
            raise ConfigurationRequestInvalid("motivo rechazado por la política") from None

    async def _current_or_defaults(
        self, transaction: Transaction, plant_id: uuid.UUID, timezone: str
    ) -> PlantLoopConfiguration:
        """La fila vigente; sin ella, crea la de los valores por defecto con su registro.

        Se llama con el candado de la planta tomado: dos primeras lecturas simultáneas crean una
        sola fila y un solo registro de origen.
        """
        current = await self._repository.current(transaction, plant_id)
        if current is not None:
            return current
        context = transaction.context
        initial = PlantLoopConfiguration(
            plant_id=plant_id,
            organization_id=context.organization_id,
            settings=LoopSettings.defaults(),
            timezone=timezone,
            version=1,
            changed_at=self._now(),
            changed_by=_snapshot(context),
            change_reason=INITIAL_DEFAULTS_REASON,
        )
        names = changed(None, initial.settings)
        await self._record(context, transaction, initial, previous=None, names=names)
        await self._repository.insert(transaction, initial)
        return initial

    async def _record(
        self,
        context: ScopeContext,
        transaction: Transaction,
        configuration: PlantLoopConfiguration,
        *,
        previous: PlantLoopConfiguration | None,
        names: tuple[str, ...],
    ) -> None:
        content = {
            "change_id": str(uuid7(self._clock, self._random_bytes)),
            "plant_id": str(configuration.plant_id),
            "version": configuration.version,
            "previous_values": (
                [] if previous is None else setting_documents(names, previous.settings)
            ),
            "new_values": setting_documents(names, configuration.settings),
            "reason": configuration.change_reason,
            "changed_by": dict(configuration.changed_by),
            "changed_at": format_timestamp(configuration.changed_at),
            "applies_to_new_items_only": True,
        }
        written = await self._writer.write(
            context,
            LOOP_CONFIGURATION_CHANGED,
            content,
            scope=RecordScope(plant_id=configuration.plant_id),
            occurred_at=configuration.changed_at,
            transaction=transaction,
        )
        _written(written)


def _snapshot(context: ScopeContext) -> dict[str, Any]:
    """``UserSnapshot`` del actor: usuario, nombre en el instante y rol con el que actúa."""
    actor = context.actor
    if actor.role_in_use is None:
        raise ResourceNotFound()
    return {
        "user_id": str(actor.id),
        "display_name": actor.display_name_snapshot,
        "role_in_use": actor.role_in_use.value,
    }


def _integers(values: Mapping[str, object]) -> dict[str, int]:
    if not isinstance(values, Mapping):
        raise ConfigurationRequestInvalid("los parámetros son un objeto")
    checked: dict[str, int] = {}
    for name, value in values.items():
        parameter = INTEGER_PARAMETERS.get(name) if isinstance(name, str) else None
        if parameter is None:
            raise ConfigurationRequestInvalid("parámetro desconocido")
        if type(value) is not int:
            raise ConfigurationRequestInvalid("el valor de un parámetro es un entero")
        if not parameter.accepts(value):
            raise ConfigurationRejected(LoopDetailCode.DEADLINE_OUT_OF_RANGE)
        checked[name] = value
    return checked


def _calendar(document: object | None) -> BusinessCalendar | None:
    if document is None:
        return None
    try:
        return BusinessCalendar.from_document(document)
    except CalendarInvalid:
        raise ConfigurationRequestInvalid("calendario fuera de su forma o de sus límites") from None
