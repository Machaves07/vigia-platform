"""Escritura de ``AuditEntry`` por la misma vía que el expediente (LC-NUC-10; BR-NUC-59 a 62).

``AuditWriter.append(context, operation, …)`` inserta una entrada en ``shared.audit_entry``; el
disparador ``vigia_chain_link('audit')`` la encadena igual que un registro (BR-NUC-60): toma la
exclusión de la cabeza de auditoría de la organización, fija ``chain_sequence``, ``occurred_at``
(no decreciente), ``previous_hash``, ``filters_hash`` y ``entry_hash`` sobre su sobre de forma
fija. Lo que la aplicación aporta:

- la instantánea del actor del contexto y su ``correlation_id`` (BR-NUC-49, 62);
- ``operation`` y ``outcome`` de sus listas cerradas (``audit_operation``, ``audit_outcome``);
- ``scope`` (planta y zona), ``resource_ref`` (``{kind, id}``), ``result_count`` y ``filters``:
  los filtros de una consulta tal como se pidieron, en bytes canónicos RFC 8785 de a lo sumo
  4 KB (BR-NUC-62). Nada de eso admite contraseñas, tokens ni texto libre sobre personas: son
  identificadores, códigos y los filtros de la consulta.

Con ``transaction`` la entrada va en la transacción del llamador (p. ej. la lectura que audita,
LC-NUC-11) y solo se confirma con ella; sin ella, el escritor abre y confirma la suya.

**Eventos sin organización** (cuenta desconocida, retardo por origen, intento sin contexto;
BR-NUC-61): ``append_without_organization(provider_context, …)`` los escribe en la cadena de la
**organización proveedora**, y rechaza cualquier contexto de otra organización.

Todo se valida antes de tocar la base y un fallo lanza ``AuditRejected`` (o ``ContextAbsent``
sin contexto): la auditoría no se degrada en silencio.
"""

from __future__ import annotations

import enum
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import JsonValue
from sqlalchemy import text

from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.ledger.canonical import (
    CanonicalFormError,
    canonical_bytes_sync,
    exceeds_canonical_size,
)
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ContextAbsent, ScopeContext, repository
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7

__all__ = [
    "MAX_FILTERS_BYTES",
    "AuditOperation",
    "AuditOutcome",
    "AuditReceipt",
    "AuditRejected",
    "AuditWriter",
    "ResourceRef",
]

MAX_FILTERS_BYTES: Final = 4 * 1024
"""Tope de ``filters`` (BR-NUC-62; restricción ``audit_entry_filters_size``)."""

_FILTERS_BOUND_FACTOR: Final = 25
"""Cuántas veces puede pasar la cota de ``exceeds_canonical_size`` del tamaño canónico real: un
doble cuenta 25 bytes y puede ocupar uno; un texto no imprimible, seis por carácter."""

MAX_RESULT_COUNT: Final = 2**31 - 1
_SNAKE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AuditOperation(enum.StrEnum):
    """``audit_operation`` (domain-entities §1)."""

    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_THROTTLED = "login_throttled"
    SECOND_FACTOR_ENROLLED = "second_factor_enrolled"
    SECOND_FACTOR_RESET = "second_factor_reset"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 - operación, no un secreto
    SESSION_CLOSED = "session_closed"
    SESSIONS_CLOSED_OTHERS = "sessions_closed_others"
    USER_INVITED = "user_invited"
    INVITATION_LINK_DISCLOSED = "invitation_link_disclosed"
    USER_ACTIVATED = "user_activated"
    USER_DEACTIVATED = "user_deactivated"
    USER_REACTIVATED = "user_reactivated"
    USER_PROFILE_CHANGED = "user_profile_changed"
    ROLE_ASSIGNED = "role_assigned"
    ROLE_REMOVED = "role_removed"
    ROLE_ASSIGNMENT_REJECTED = "role_assignment_rejected"
    ORGANIZATION_SETTINGS_CHANGED = "organization_settings_changed"
    PLANT_CREATED = "plant_created"
    ZONE_CREATED = "zone_created"
    NODE_ZONE_ASSIGNED = "node_zone_assigned"
    NODE_ZONE_UNASSIGNED = "node_zone_unassigned"
    LEDGER_READ = "ledger_read"
    LEDGER_DETAIL_READ = "ledger_detail_read"
    EVIDENCE_READ_GRANTED = "evidence_read_granted"
    LABEL_READ = "label_read"
    COVERAGE_READ = "coverage_read"
    AUDIT_READ = "audit_read"
    EXPORT_REQUESTED = "export_requested"
    INTEGRITY_VERIFICATION = "integrity_verification"
    CHECKPOINT = "checkpoint"
    KEY_ROTATED = "key_rotated"
    KEY_SET_PUBLISHED = "key_set_published"
    LIVE_VIEW_TOKEN_ISSUED = "live_view_token_issued"  # noqa: S105 - operación, no un secreto
    LIVE_VIEW_ACCESS_LOCAL = "live_view_access_local"
    AUTHORIZATION_DENIED = "authorization_denied"
    CSRF_REJECTED = "csrf_rejected"
    """Solo auditoría: la respuesta es ``forbidden`` (PAT-NUC-SEG-02, nota del 2026-09-23)."""
    CONTEXT_ABSENT_ATTEMPT = "context_absent_attempt"
    DEAD_LETTER_REPLAYED = "dead_letter_replayed"
    UNKNOWN_TOKEN_REPORTED = "unknown_token_reported"  # noqa: S105 - operación, no un secreto


class AuditOutcome(enum.StrEnum):
    """``audit_outcome`` (domain-entities §1)."""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditRejected(ValueError):
    """La entrada no se escribe; nada se insertó. ``code`` es una lista cerrada."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """``resource_ref``: qué se leyó, exportó o concedió (``{kind, id}``)."""

    kind: str
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    """La entrada tal como la encadenó el disparador."""

    entry_id: uuid.UUID
    chain_sequence: int
    occurred_at: datetime


_INSERT_ENTRY: Final = text(
    "INSERT INTO shared.audit_entry (entry_id, organization_id, actor_kind, actor_id,"
    " actor_display_name_snapshot, actor_role_in_use, actor_concession_id, actor_unit,"
    " operation, scope_plant_id, scope_zone_id, resource_kind, resource_id, filters,"
    " result_count, outcome, correlation_id)"
    " VALUES (:entry_id, :organization_id, :actor_kind, :actor_id,"
    " :actor_display_name_snapshot, :actor_role_in_use, :actor_concession_id, :actor_unit,"
    " :operation, :scope_plant_id, :scope_zone_id, :resource_kind, :resource_id, :filters,"
    " :result_count, :outcome, :correlation_id)"
    " RETURNING entry_id, chain_sequence, occurred_at"
)


def _optional_uuid(value: object, name: str) -> uuid.UUID | None:
    if value is not None and type(value) is not uuid.UUID:
        raise AuditRejected("audit_entry_invalid", f"{name} debe ser uuid.UUID")
    return value


@repository
class AuditWriter:
    """``audit_writer.append``: la cadena de auditoría por organización (BR-NUC-60)."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        clock: Clock,
        provider_organization_id: uuid.UUID,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        if type(provider_organization_id) is not uuid.UUID:
            raise TypeError("provider_organization_id debe ser uuid.UUID")
        self._database = database
        self._clock = clock
        self._random_bytes = random_bytes
        self._provider_organization_id = provider_organization_id

    @property
    def provider_organization_id(self) -> uuid.UUID:
        return self._provider_organization_id

    async def append(
        self,
        context: ScopeContext,
        operation: AuditOperation | str,
        *,
        outcome: AuditOutcome | str = AuditOutcome.SUCCESS,
        plant_id: uuid.UUID | None = None,
        zone_id: uuid.UUID | None = None,
        resource: ResourceRef | None = None,
        filters: Mapping[str, JsonValue] | None = None,
        result_count: int | None = None,
        transaction: Transaction | None = None,
    ) -> AuditReceipt:
        """Anexa una entrada a la cadena de auditoría de la organización del contexto."""
        if not isinstance(context, ScopeContext):
            raise ContextAbsent()
        parameters = self._parameters(
            context, operation, outcome, plant_id, zone_id, resource, filters, result_count
        )
        if transaction is not None:
            if not isinstance(transaction, Transaction):
                raise ContextAbsent()
            if transaction.context.organization_id != context.organization_id:
                raise AuditRejected("audit_entry_invalid", "la transacción es de otra organización")
            return await self._insert(transaction, parameters)
        async with self._database.transaction(context) as own:
            receipt = await self._insert(own, parameters)
        return receipt

    async def append_without_organization(
        self,
        provider_context: ScopeContext,
        operation: AuditOperation | str,
        **fields: Any,
    ) -> AuditReceipt:
        """Evento sin organización: a la cadena de la organización proveedora (BR-NUC-61)."""
        if not isinstance(provider_context, ScopeContext):
            raise ContextAbsent()
        if provider_context.organization_id != self._provider_organization_id:
            raise AuditRejected(
                "audit_entry_invalid",
                "un evento sin organización solo se audita en la cadena de la proveedora",
            )
        return await self.append(provider_context, operation, **fields)

    def _parameters(
        self,
        context: ScopeContext,
        operation: str,
        outcome: str,
        plant_id: object,
        zone_id: object,
        resource: object,
        filters: object,
        result_count: object,
    ) -> dict[str, Any]:
        try:
            operation_value = AuditOperation(operation).value
        except ValueError:
            raise AuditRejected("audit_operation_unknown", "operación fuera de la lista") from None
        try:
            outcome_value = AuditOutcome(outcome).value
        except ValueError:
            raise AuditRejected("audit_entry_invalid", "resultado fuera de la lista") from None
        resource_kind: str | None = None
        resource_id: uuid.UUID | None = None
        if resource is not None:
            if not isinstance(resource, ResourceRef) or not isinstance(resource.kind, str):
                raise AuditRejected("audit_entry_invalid", "resource debe ser ResourceRef")
            if not _SNAKE.fullmatch(resource.kind):
                raise AuditRejected("audit_entry_invalid", "resource.kind debe ser snake_case")
            resource_kind = resource.kind
            resource_id = _optional_uuid(resource.id, "resource.id")
            if resource_id is None:
                raise AuditRejected("audit_entry_invalid", "resource.id es obligatorio")
        if result_count is not None and (
            type(result_count) is not int or not 0 <= result_count <= MAX_RESULT_COUNT
        ):
            raise AuditRejected("audit_entry_invalid", "result_count fuera de rango")
        filters_bytes: bytes | None = None
        if filters is not None:
            if not isinstance(filters, Mapping):
                raise AuditRejected("audit_entry_invalid", "filters debe ser un objeto JSON")
            # Sin serializar: la cota de ``exceeds_canonical_size`` es a lo sumo 25 veces el
            # tamaño canónico (un doble), así que por encima de 25 veces 4 KB seguro que no cabe.
            if exceeds_canonical_size(dict(filters), _FILTERS_BOUND_FACTOR * MAX_FILTERS_BYTES):
                raise AuditRejected("filters_too_large", f"filters supera {MAX_FILTERS_BYTES} B")
            try:
                filters_bytes = canonical_bytes_sync(dict(filters))
            except CanonicalFormError:
                raise AuditRejected("audit_entry_invalid", "filters no es JSON canónico") from None
            if len(filters_bytes) > MAX_FILTERS_BYTES:
                raise AuditRejected("filters_too_large", f"filters supera {MAX_FILTERS_BYTES} B")
        actor = context.actor
        return {
            "entry_id": uuid7(self._clock, self._random_bytes),
            "organization_id": context.organization_id,
            "actor_kind": actor.kind.value,
            "actor_id": actor.id,
            "actor_display_name_snapshot": actor.display_name_snapshot,
            "actor_role_in_use": None if actor.role_in_use is None else actor.role_in_use.value,
            "actor_concession_id": actor.concession_id,
            "actor_unit": actor.unit.value,
            "operation": operation_value,
            "scope_plant_id": _optional_uuid(plant_id, "plant_id"),
            "scope_zone_id": _optional_uuid(zone_id, "zone_id"),
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "filters": filters_bytes,
            "result_count": result_count,
            "outcome": outcome_value,
            "correlation_id": context.correlation_id,
        }

    @staticmethod
    async def _insert(transaction: Transaction, parameters: Mapping[str, Any]) -> AuditReceipt:
        row = (await transaction.execute(_INSERT_ENTRY, dict(parameters))).one()
        return AuditReceipt(
            entry_id=row.entry_id,
            chain_sequence=int(row.chain_sequence),
            occurred_at=row.occurred_at,
        )
