"""``shared.tokens``: token de vista en vivo y accesos locales (LC-NUC-26; S-PLA-03).

``business-logic-model.md`` §9 "Vista en vivo"; BR-NUC-88 a 90; domain-entities §2.13; reclamos y
codificación de U-01 §3.6 con su nota de 2026-09-23.

**``emitir_token_vista(context, zone_id)``** (``LiveViewTokenService.issue``), en este orden:

1. ``live_view.open`` sobre la zona (coordinador, administrador, proveedor bajo concesión,
   COPASST). Una zona inexistente o fuera de alcance responde ``not_found``.
2. El nodo vigente de la zona: asignación sin ``unassigned_at`` a un nodo no ``revoked``; sin él,
   ``zone_without_node`` (BR-NUC-88).
3. Límite **exacto** de 30 emisiones por usuario en cualquier ventana deslizante de 10 minutos
   (BR-NUC-90, PAT-NUC-ESC-03): dentro de la transacción de emisión se toma una exclusión por
   usuario (``pg_advisory_xact_lock`` sobre su ``user_id``) y se cuentan sus
   ``LiveViewTokenIssuance`` con ``issued_at`` posterior a ``ahora - 10 min``, también las de
   marca futura (reloj de otro proceso adelantado). Con 30 o más, ``rate_limited`` con
   ``retry_after_seconds`` hasta que salga la más antigua. Contar lo posterior a ``ahora - 10
   min`` acota cualquier ventana que contenga ``ahora``, así que con cualquier número de procesos
   ninguna ventana llega a 31 (PR-NUC-45). La exclusión es un candado consultivo y no la fila de
   ``identity.user_account``: bajo concesión, la seguridad a nivel de fila del cliente no deja ver
   la cuenta del proveedor.
4. Reclamos de U-01 §3.6: ``jti`` UUID v7, ``iss = vigia-platform``, ``aud = node_id``,
   ``sub = user_id``, organización, planta, zona, ``role = role_in_use``, ``iat`` y
   ``exp = iat + 600`` en segundos enteros desde la época, ``purpose = live_view``. Se validan con
   el modelo estricto del contrato y se codifican como **JWS compacta** con cabecera
   ``{"alg": "EdDSA", "kid": <key_id>}``, firmada con la clave ``live_view_token`` activa
   (``SigningService.sign_detached``). El ``kid`` se fija antes de firmar; si una rotación cambia
   la clave entre las dos lecturas, se vuelve a construir con la nueva.
5. ``LiveViewTokenIssuance`` (sin el token) y la auditoría ``live_view_token_issued`` en la misma
   transacción. Devuelve ``{token, live_view_local_url, expires_at}``; ``live_view_local_url`` es
   la de ``NodeIdentity`` y **nula** si el nodo no la anunció (pendiente nº 31).

``issued_at`` se guarda a segundo entero: es el ``iat`` del token, y ``expires_at`` es a la vez el
``exp`` del token y la restricción ``issued_at + 600 s`` de la tabla.

**``incorporar_accesos_locales(node_id, records)``** (``LiveViewTokenService.incorporate``; lo
invoca U-03 al procesar cada latido con el contexto de la organización del nodo): cada
``LiveViewAccess`` del latido se valida en modo estricto (``role`` de la lista cerrada); uno
inválido no se incorpora. Si su ``jti`` es de una emisión de **este** nodo con la misma zona,
usuario y rol, se audita ``live_view_access_local`` con apertura, cierre y resultado; si no
existe (o es de otra organización, que la seguridad a nivel de fila no deja ver), es de otro nodo o
sus reclamos no coinciden, ``unknown_token_reported`` y ``security_alert`` en la bandeja, en la
misma transacción (BR-NUC-89). Los accesos se incorporan **por ``access_id`` sin duplicar**
(adenda A-02): bajo una exclusión por nodo, un ``access_id`` de ese nodo que ya tiene entrada
(incorporada o alertada) no se vuelve a escribir, aunque el latido lo repita o lo traiga con
otro resultado.

**El token completo nunca se persiste ni se registra** (BR-NUC-88): ni en la tabla, ni en la
auditoría, ni en la bandeja, ni en el registro de errores; ``IssuedLiveViewToken`` no lo muestra
en su ``repr``.

No lee la hora del sistema: usa el ``Clock`` inyectado.
"""

from __future__ import annotations

import base64
import enum
import json
import math
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from pydantic import JsonValue, ValidationError
from sqlalchemy import text
from vigia_contracts.models.heartbeat import LiveViewAccess
from vigia_contracts.models.live_view_token import LiveViewToken as LiveViewTokenClaims

from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import (
    AuditOperation,
    AuditOutcome,
    AuditWriter,
    ResourceRef,
)
from vigia_platform.ledger.application.writer import LedgerDatabase
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import (
    ActorKind,
    ActorUnit,
    ContextOrigin,
    Role,
    ScopeContext,
    repository,
)
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.publish import NewEvent, OutboxPort
from vigia_platform.shared.signing.keys import KeyStatus, SigningKeyRecord, SigningPurpose
from vigia_platform.shared.signing.keys import format_timestamp as _format_timestamp
from vigia_platform.shared.signing.service import DetachedSignature, SigningKeyUnavailable

__all__ = [
    "ISSUER",
    "JWS_ALGORITHM",
    "RATE_LIMIT_TOKENS",
    "RATE_LIMIT_WINDOW",
    "TOKEN_LIFETIME_SECONDS",
    "TOKEN_PURPOSE",
    "AccessReport",
    "IssuedLiveViewToken",
    "LiveViewRejection",
    "LiveViewSigner",
    "LiveViewTokenRejected",
    "LiveViewTokenService",
    "UnknownTokenReason",
    "compact_jws",
]

_log = get_logger("shared.tokens")

TOKEN_LIFETIME_SECONDS: Final = 600
"""``exp - iat`` del token (BR-NUC-88) y ``expires_at - issued_at`` de la emisión."""
RATE_LIMIT_TOKENS: Final = 30
"""Emisiones por usuario en una ventana deslizante (BR-NUC-90, ``[objetivo propio]``)."""
RATE_LIMIT_WINDOW: Final = timedelta(minutes=10)
ISSUER: Final = "vigia-platform"
TOKEN_PURPOSE: Final = "live_view"  # noqa: S105 - propósito del token, no un secreto
JWS_ALGORITHM: Final = "EdDSA"
_SIGN_ATTEMPTS: Final = 3
"""Rotaciones toleradas entre leer el ``kid`` y firmar antes de rendirse."""
_TOKEN_RESOURCE: Final = "live_view_token"  # noqa: S105 - clase de recurso, no un secreto
_UNKNOWN_TOKEN_ALERT: Final = "unknown_token_reported"  # noqa: S105 - código de alerta
_PERSON_ACTORS: Final = frozenset({ActorKind.USER, ActorKind.PROVIDER_USER})


# --- Errores y resultados ----------------------------------------------------------------------


class LiveViewRejection(enum.StrEnum):
    """Por qué no se emite el token; el valor es también el ``api_error_code``."""

    ZONE_WITHOUT_NODE = "zone_without_node"
    RATE_LIMITED = "rate_limited"


_MESSAGES: Final[Mapping[LiveViewRejection, str]] = {
    LiveViewRejection.ZONE_WITHOUT_NODE: "La zona no tiene un nodo asignado.",
    LiveViewRejection.RATE_LIMITED: (
        "Se pidieron demasiadas vistas en vivo en los últimos 10 minutos."
    ),
}


class LiveViewTokenRejected(Exception):
    """No se emite el token: ``code`` cerrado; ``retry_after_seconds`` solo en ``rate_limited``."""

    def __init__(self, code: LiveViewRejection, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.retry_after_seconds = retry_after_seconds

    @property
    def api_code(self) -> str:
        return self.code.value

    @property
    def message_es(self) -> str:
        return _MESSAGES[self.code]


@dataclass(frozen=True, slots=True)
class IssuedLiveViewToken:
    """La respuesta de ``POST /zones/{id}/live-view-token``; el token no sale en el ``repr``."""

    token: str = field(repr=False)
    live_view_local_url: str | None
    expires_at: datetime
    jti: uuid.UUID
    node_id: uuid.UUID

    def to_response(self) -> dict[str, str | None]:
        """``{token, live_view_local_url, expires_at}`` (marca ISO 8601 UTC con ``Z``)."""
        return {
            "token": self.token,
            "live_view_local_url": self.live_view_local_url,
            "expires_at": _format_timestamp(self.expires_at),
        }


class UnknownTokenReason(enum.StrEnum):
    """Por qué un acceso local se audita como ``unknown_token_reported``."""

    NOT_ISSUED = "not_issued"
    """Ninguna emisión con ese ``jti`` en la organización del nodo."""
    OTHER_NODE = "other_node"
    """El ``jti`` se emitió para otro nodo."""
    CLAIMS_MISMATCH = "claims_mismatch"
    """Zona, usuario o rol del acceso distintos de los de la emisión."""


@dataclass(frozen=True, slots=True)
class AccessReport:
    """Lo que hizo ``incorporate`` con los accesos de un latido."""

    incorporated: int = 0
    unknown: int = 0
    duplicates: int = 0
    invalid: int = 0


# --- Firma -------------------------------------------------------------------------------------


class LiveViewSigner(Protocol):
    """La parte de ``SigningService`` que firma el token."""

    def public_keys(self, purpose: SigningPurpose) -> tuple[SigningKeyRecord, ...]: ...

    def sign_detached(self, purpose: SigningPurpose, message: bytes) -> DetachedSignature: ...


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode(
        "ascii"
    )


def _active_key_id(signer: LiveViewSigner) -> str:
    active = [
        key
        for key in signer.public_keys(SigningPurpose.LIVE_VIEW_TOKEN)
        if key.status is KeyStatus.ACTIVE
    ]
    if len(active) != 1:
        raise SigningKeyUnavailable(SigningPurpose.LIVE_VIEW_TOKEN)
    return active[0].key_id


def compact_jws(claims: Mapping[str, Any], signer: LiveViewSigner) -> str:
    """JWS compacta ``EdDSA`` de ``claims`` con ``kid`` = la clave ``live_view_token`` activa.

    La cabecera lleva el ``kid`` de la clave activa leída antes de firmar; si la firma sale con
    otra (una rotación en medio), se reconstruye con la nueva. Sin clave activa vigente,
    ``SigningKeyUnavailable``.
    """
    payload = _b64url(_json_bytes(claims))
    for _ in range(_SIGN_ATTEMPTS):
        key_id = _active_key_id(signer)
        header = _b64url(_json_bytes({"alg": JWS_ALGORITHM, "kid": key_id}))
        signing_input = f"{header}.{payload}"
        signature = signer.sign_detached(
            SigningPurpose.LIVE_VIEW_TOKEN, signing_input.encode("ascii")
        )
        if signature.key_id == key_id:
            return f"{signing_input}.{_b64url(signature.signature)}"
    raise SigningKeyUnavailable(SigningPurpose.LIVE_VIEW_TOKEN)


# --- Sentencias --------------------------------------------------------------------------------

_ZONE: Final = text("SELECT plant_id FROM identity.zone WHERE zone_id = :zone_id")
_LOCK_USER: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('live_view_token|' || :subject, 0))"
)
_LOCK_NODE: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('live_view_access|' || :subject, 0))"
)
_CURRENT_NODE: Final = text(
    "SELECT n.node_id, n.live_view_local_url FROM identity.zone_node_assignment AS a"
    " JOIN identity.node_identity AS n ON n.node_id = a.node_id"
    " WHERE a.zone_id = :zone_id AND a.unassigned_at IS NULL AND n.status <> 'revoked'"
)
_RECENT_ISSUANCES: Final = text(
    "SELECT count(*) AS issued, min(issued_at) AS oldest"
    " FROM identity.live_view_token_issuance"
    " WHERE user_id = :user_id AND issued_at > :window_start"
)
_INSERT_ISSUANCE: Final = text(
    "INSERT INTO identity.live_view_token_issuance (jti, organization_id, plant_id, zone_id,"
    " node_id, user_id, role_in_use, issued_at, expires_at, correlation_id)"
    " VALUES (:jti, :organization_id, :plant_id, :zone_id, :node_id, :user_id, :role_in_use,"
    " :issued_at, :expires_at, :correlation_id)"
)
_NODE: Final = text("SELECT plant_id FROM identity.node_identity WHERE node_id = :node_id")
_ISSUANCE: Final = text(
    "SELECT node_id, plant_id, zone_id, user_id, role_in_use"
    " FROM identity.live_view_token_issuance WHERE jti = :jti"
)
_ALREADY_INCORPORATED: Final = text(
    "SELECT 1 FROM shared.audit_entry WHERE organization_id = :organization_id"
    " AND operation IN ('live_view_access_local', 'unknown_token_reported')"
    " AND resource_kind = :resource_kind"
    " AND filters_json ->> 'node_id' = :node_id AND filters_json ->> 'access_id' = :access_id"
    " LIMIT 1"
)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _retry_after(oldest: datetime, now: datetime) -> int:
    """Segundos hasta que la emisión más antigua de la ventana sale de ella (1 a 600)."""
    remaining = (oldest + RATE_LIMIT_WINDOW - now).total_seconds()
    return min(max(math.ceil(remaining), 1), int(RATE_LIMIT_WINDOW.total_seconds()))


def _checked_access(record: object) -> LiveViewAccess | None:
    """El acceso en modo estricto, con ``role`` de la lista cerrada; ``None`` si es inválido.

    Se valida su forma JSON (la del latido) con el modelo del contrato: un acceso ya modelado
    pasa otra vez por el esquema, por si se construyó sin validar.
    """
    if isinstance(record, LiveViewAccess):
        data: object = record.to_json_value()
    elif isinstance(record, Mapping):
        data = dict(record)
    else:
        return None
    try:
        document = json.dumps(data, allow_nan=False, ensure_ascii=False)
        access = LiveViewAccess.model_validate_json(document)
        Role(access.role.value)
    except (ValidationError, ValueError, TypeError, RecursionError):
        return None
    return access


# --- Servicio ----------------------------------------------------------------------------------


@repository
class LiveViewTokenService:
    """``LiveViewTokenPort`` (``business-logic-model.md`` §10.1): emisión e incorporación."""

    def __init__(
        self,
        *,
        database: LedgerDatabase,
        authorizer: Authorizer,
        audit: AuditWriter,
        outbox: OutboxPort,
        signer: LiveViewSigner,
        clock: Clock,
        metrics: PlatformMetrics | None = None,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._audit = audit
        self._outbox = outbox
        self._signer = signer
        self._clock = clock
        self._metrics = metrics
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "LiveViewTokenService()"

    # --- emitir_token_vista --------------------------------------------------------------------

    async def issue(self, context: ScopeContext, zone_id: uuid.UUID) -> IssuedLiveViewToken:
        """``emitir_token_vista``: token de 10 minutos para la vista difuminada de ``zone_id``."""
        if type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        rows = await self._database.read(context, _ZONE, {"zone_id": zone_id})
        if not rows:
            raise ResourceNotFound()
        plant_id = _uuid(rows[0].plant_id)
        authorized = await self._authorizer.authorize(
            context,
            PermissionKey.LIVE_VIEW_OPEN,
            Resource.zone(context.organization_id, plant_id, zone_id),
        )
        actor = authorized.actor
        role = actor.role_in_use
        if (
            authorized.origin is not ContextOrigin.SESSION
            or actor.kind not in _PERSON_ACTORS
            or role is None
        ):
            # Solo una persona con sesión abre la vista: el token nombra a su usuario (``sub``).
            raise ResourceNotFound()
        issued_at = self._clock.now().replace(microsecond=0)
        expires_at = issued_at + timedelta(seconds=TOKEN_LIFETIME_SECONDS)
        async with self._database.transaction(authorized) as transaction:
            await transaction.execute(_LOCK_USER, {"subject": str(actor.id)})
            node = (await transaction.execute(_CURRENT_NODE, {"zone_id": zone_id})).first()
            if node is None:
                raise LiveViewTokenRejected(LiveViewRejection.ZONE_WITHOUT_NODE)
            node_id = _uuid(node.node_id)
            recent = (
                await transaction.execute(
                    _RECENT_ISSUANCES,
                    {"user_id": actor.id, "window_start": issued_at - RATE_LIMIT_WINDOW},
                )
            ).one()
            if int(recent.issued) >= RATE_LIMIT_TOKENS:
                raise LiveViewTokenRejected(
                    LiveViewRejection.RATE_LIMITED,
                    retry_after_seconds=_retry_after(recent.oldest, issued_at),
                )
            jti = uuid7(self._clock, self._random_bytes)
            claims = self._claims(
                authorized, jti, node_id, plant_id, zone_id, role, issued_at, expires_at
            )
            token = compact_jws(claims, self._signer)
            await transaction.execute(
                _INSERT_ISSUANCE,
                {
                    "jti": jti,
                    "organization_id": authorized.organization_id,
                    "plant_id": plant_id,
                    "zone_id": zone_id,
                    "node_id": node_id,
                    "user_id": actor.id,
                    "role_in_use": role.value,
                    "issued_at": issued_at,
                    "expires_at": expires_at,
                    "correlation_id": authorized.correlation_id,
                },
            )
            await self._audit.append(
                authorized,
                AuditOperation.LIVE_VIEW_TOKEN_ISSUED,
                plant_id=plant_id,
                zone_id=zone_id,
                resource=ResourceRef(_TOKEN_RESOURCE, jti),
                filters={"node_id": str(node_id), "expires_at": _format_timestamp(expires_at)},
                transaction=transaction,
            )
        self._metrics_port().live_view_tokens_issued_total.add(1)
        return IssuedLiveViewToken(
            token=token,
            live_view_local_url=node.live_view_local_url,
            expires_at=expires_at,
            jti=jti,
            node_id=node_id,
        )

    @staticmethod
    def _claims(
        context: ScopeContext,
        jti: uuid.UUID,
        node_id: uuid.UUID,
        plant_id: uuid.UUID,
        zone_id: uuid.UUID,
        role: Role,
        issued_at: datetime,
        expires_at: datetime,
    ) -> dict[str, Any]:
        """Los reclamos de U-01 §3.6, validados con el modelo estricto del contrato."""
        claims: dict[str, Any] = {
            "jti": str(jti),
            "iss": ISSUER,
            "aud": str(node_id),
            "sub": str(context.actor.id),
            "organization_id": str(context.organization_id),
            "plant_id": str(plant_id),
            "zone_id": str(zone_id),
            "role": role.value,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            "purpose": TOKEN_PURPOSE,
        }
        return LiveViewTokenClaims.model_validate_json(_json_bytes(claims)).to_json_value()

    # --- incorporar_accesos_locales ------------------------------------------------------------

    async def incorporate(
        self, context: ScopeContext, node_id: uuid.UUID, records: Iterable[object]
    ) -> AccessReport:
        """``incorporar_accesos_locales``: los accesos que ``node_id`` reportó en su latido.

        ``context`` es el de la organización del nodo (U-03). Un nodo inexistente o de otra
        organización responde ``ResourceNotFound`` sin escribir nada.
        """
        if type(node_id) is not uuid.UUID:
            raise ResourceNotFound()
        accesses: list[LiveViewAccess] = []
        invalid = 0
        for record in records:
            access = _checked_access(record)
            if access is None:
                invalid += 1
            else:
                accesses.append(access)
        if invalid:
            _log.warning("accesos locales inválidos en el latido: no se incorporan")
        writer = with_unit(context, ActorUnit.U02)
        incorporated = unknown = duplicates = 0
        async with self._database.transaction(writer) as transaction:
            node = (await transaction.execute(_NODE, {"node_id": node_id})).first()
            if node is None:
                raise ResourceNotFound()
            node_plant = _uuid(node.plant_id)
            await transaction.execute(_LOCK_NODE, {"subject": str(node_id)})
            for access in accesses:
                outcome = await self._incorporate_one(transaction, node_id, node_plant, access)
                if outcome is None:
                    duplicates += 1
                elif outcome:
                    incorporated += 1
                else:
                    unknown += 1
        if unknown:
            _log.warning("acceso local con un token desconocido: alerta de seguridad publicada")
        return AccessReport(incorporated, unknown, duplicates, invalid)

    async def _incorporate_one(
        self,
        transaction: Transaction,
        node_id: uuid.UUID,
        node_plant: uuid.UUID,
        access: LiveViewAccess,
    ) -> bool | None:
        """``True`` incorporado, ``False`` token desconocido, ``None`` ya estaba."""
        if await self._already_incorporated(transaction, node_id, access):
            return None
        jti = uuid.UUID(access.jti)
        issuance = (await transaction.execute(_ISSUANCE, {"jti": jti})).first()
        reason = self._unknown_reason(issuance, node_id, access)
        details: dict[str, JsonValue] = {
            "access_id": access.access_id,
            "node_id": str(node_id),
            "outcome": access.outcome.value,
            "opened_at": access.opened_at,
        }
        if access.closed_at is not None:
            details["closed_at"] = access.closed_at
        if reason is None:
            operation = AuditOperation.LIVE_VIEW_ACCESS_LOCAL
            details |= {"zone_id": access.zone_id, "sub": access.sub, "role": access.role.value}
        else:
            operation = AuditOperation.UNKNOWN_TOKEN_REPORTED
            details["reason"] = reason.value
        if reason is None and issuance is not None:
            await self._audit.append(
                transaction.context,
                operation,
                plant_id=_uuid(issuance.plant_id),
                zone_id=_uuid(issuance.zone_id),
                resource=ResourceRef(_TOKEN_RESOURCE, jti),
                filters=details,
                transaction=transaction,
            )
            return True
        await self._audit.append(
            transaction.context,
            operation,
            outcome=AuditOutcome.DENIED,
            plant_id=node_plant,
            resource=ResourceRef(_TOKEN_RESOURCE, jti),
            filters=details,
            transaction=transaction,
        )
        await self._outbox.publish(
            transaction,
            NewEvent(
                event_name="security_alert",
                payload={
                    "alert_kind": _UNKNOWN_TOKEN_ALERT,
                    "resource_kind": "node",
                    "resource_id": str(node_id),
                    "occurred_at": _format_timestamp(self._clock.now()),
                },
            ),
        )
        return False

    @staticmethod
    def _unknown_reason(
        issuance: Any, node_id: uuid.UUID, access: LiveViewAccess
    ) -> UnknownTokenReason | None:
        if issuance is None:
            return UnknownTokenReason.NOT_ISSUED
        if _uuid(issuance.node_id) != node_id:
            return UnknownTokenReason.OTHER_NODE
        if (
            _uuid(issuance.zone_id) != uuid.UUID(access.zone_id)
            or _uuid(issuance.user_id) != uuid.UUID(access.sub)
            or issuance.role_in_use != access.role.value
        ):
            return UnknownTokenReason.CLAIMS_MISMATCH
        return None

    @staticmethod
    async def _already_incorporated(
        transaction: Transaction, node_id: uuid.UUID, access: LiveViewAccess
    ) -> bool:
        """¿Ya se incorporó (o se alertó) el ``access_id`` de este nodo? (adenda A-02)."""
        row = (
            await transaction.execute(
                _ALREADY_INCORPORATED,
                {
                    "organization_id": transaction.context.organization_id,
                    "resource_kind": _TOKEN_RESOURCE,
                    "node_id": str(node_id),
                    "access_id": access.access_id,
                },
            )
        ).first()
        return row is not None

    def _metrics_port(self) -> PlatformMetrics:
        return self._metrics if self._metrics is not None else get_metrics()
