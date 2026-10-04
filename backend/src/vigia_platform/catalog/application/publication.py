"""Publicación del catálogo firmado por zona (LC-GOB-01; S-PLA-04; BR-GOB-01 a 12).

``publish_catalog_version(context, zone_id, change, reason_es)`` es el **único** camino por el que
nace una versión del catálogo; lo usan todas las variantes (estándar nuevo, versión nueva de un
estándar, retiro, cámaras, cobertura mínima, señales, umbrales, ventanas y marca unipersonal).

1. **Antes de la transacción**, sin escribir nada: zona dentro del alcance (``catalog.manage``;
   fuera de alcance o inexistente responden igual, ``ResourceNotFound``), y textos por la
   política base de U-02 más el validador mínimo de U-03 (``free_text_rejected``): ``reason_es``
   de 10 a 500, ``title_es`` ≤ 120, ``declared_text`` ≤ 4 000, la descripción de cada señal y el
   nombre visible de quien declara. Se guardan en NFC: así el escritor del expediente no cambia
   ni una letra del sobre ya firmado.
2. **En una sola transacción**, en este orden: exclusión de la zona (``pg_advisory_xact_lock``);
   versión vigente; familia admitida en la planta (``admission_for`` de LC-GOB-02, si no
   ``family_not_admitted``); plan puro (``plan_publication``: versión de estándar + 1,
   ``ZoneCatalog`` canónico con ``version = catalog_version``, ``catalog_version`` + 1,
   predicado, cámaras y cobertura satisfacible); ``SigningPort.sign(purpose=catalog)`` con
   tope de espera; ``catalog_version_published`` (``source_key = zone_id:catalog_version``)
   con el sobre y el evento ``catalog_updated {zone_id, catalog_version, changed_fields}`` por
   la bandeja;
   inserción de la versión con su sobre conservado; cierre ``superseded_at`` de la anterior;
   versiones de estándar que nacen y que se cierran; ``catalog_standard_retired`` y
   ``single_occupancy_declared`` si tocan; proyección de ``zone_camera``; ``RegressionMarker``.
   El recibo (la versión) se devuelve **después** de confirmar.

**Fallo cerrado** (BR-GOB-06, NFR-GOB-35, FS-GOB-02): si la firma no responde (clave no
disponible, servicio sin arrancar o tope agotado) la transacción se revierte entera: no queda
versión, ni registro, ni evento, y la persona recibe ``temporarily_unavailable``. Como el
número solo existe si la transacción confirma, la publicación siguiente toma el número siguiente
sin hueco.

**Concurrencia**: dos publicaciones de la misma zona se ordenan por el candado; la segunda lee la
versión que dejó la primera y publica la siguiente. Si la espera supera ``lock_timeout`` o si la
clave ``(zone_id, catalog_version)`` choca (respaldo), la perdedora recibe un error transitorio:
nunca un número repetido ni un hueco.

``stored_envelope`` (``catalog.read``) devuelve el sobre guardado tal cual: **nunca** canonicaliza
ni firma (PAT-GOB-REN-02, NFR-GOB-10). ``single_occupancy`` y ``aggregation_window_minutes`` nunca
entran en el sobre ni en el evento (NFR-GOB-38). Ningún paso lee la hora del sistema.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol

from sqlalchemy import exc as sa_exc
from vigia_contracts.models.enumerations import AcceptanceStatus, PredicateFamily

from vigia_platform.catalog.adapters.postgres.catalog_repository import (
    CatalogWriteConflict,
    PostgresCatalogRepository,
)
from vigia_platform.catalog.application.admission import CatalogRejected
from vigia_platform.catalog.detail_codes import CatalogDetailCode
from vigia_platform.catalog.domain.admission import FamilyAdmission
from vigia_platform.catalog.domain.catalog_version import (
    MAX_REASON_CHARS,
    MIN_REASON_CHARS,
    CatalogChange,
    CatalogRuleViolated,
    CatalogState,
    CatalogViolation,
    InitialZoneParameters,
    NewStandard,
    NewStandardVersion,
    SetSignals,
    StandardDraft,
    ZoneCatalogVersion,
    ZoneRef,
    plan_publication,
)
from vigia_platform.catalog.domain.enums import CatalogChangedField
from vigia_platform.catalog.domain.standard import (
    MAX_DECLARED_TEXT_CHARS,
    MAX_TITLE_CHARS,
    DeclaredBy,
    DeclaredStandardVersion,
)
from vigia_platform.identity.authz.authorize import Authorizer, Resource, ResourceNotFound
from vigia_platform.identity.authz.context import with_unit
from vigia_platform.identity.authz.matrix import PermissionKey
from vigia_platform.ledger.application.audit_writer import AuditOperation, AuditWriter
from vigia_platform.ledger.application.writer import (
    EscritorExpediente,
    LedgerDatabase,
    LedgerRejection,
    LedgerRejectionCode,
    Receipt,
    RecordScope,
)
from vigia_platform.ledger.free_text import FreeTextField, FreeTextPolicyRegistry, FreeTextRejected
from vigia_platform.shared.api.errors import ApiErrorCode, ExternalDependencyDown
from vigia_platform.shared.clock import Clock
from vigia_platform.shared.context import ActorUnit, Role, ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.signing import SigningKeyUnavailable, SigningNotReady, SigningPurpose
from vigia_platform.shared.signing.keys import to_millisecond

__all__ = [
    "PUBLISHED_RECORD_TYPE",
    "RETIRED_RECORD_TYPE",
    "SIGN_TIMEOUT_SECONDS",
    "SINGLE_OCCUPANCY_RECORD_TYPE",
    "AdmissionLookup",
    "CatalogPublicationFailed",
    "CatalogPublicationService",
    "CatalogPublicationUnavailable",
    "CatalogRequestInvalid",
    "CatalogSigner",
    "NullRegressionMarker",
    "RegressionMarker",
]

PUBLISHED_RECORD_TYPE: Final = "catalog_version_published"
RETIRED_RECORD_TYPE: Final = "catalog_standard_retired"
SINGLE_OCCUPANCY_RECORD_TYPE: Final = "single_occupancy_declared"
CATALOG_UPDATED: Final = "catalog_updated"
SIGN_TIMEOUT_SECONDS: Final = 5.0
"""Tope de ``SigningPort.sign`` (PAT-GOB-RES-03: 5 s, ``kms:Sign``)."""

_REASON: Final = FreeTextField(
    PUBLISHED_RECORD_TYPE, "/reason_es", MIN_REASON_CHARS, MAX_REASON_CHARS
)
_TITLE: Final = FreeTextField(
    PUBLISHED_RECORD_TYPE, "/envelope/payload/standards[*]/title_es", 1, MAX_TITLE_CHARS
)
_DECLARED_TEXT: Final = FreeTextField(
    PUBLISHED_RECORD_TYPE,
    "/envelope/payload/standards[*]/declared_text",
    1,
    MAX_DECLARED_TEXT_CHARS,
)
_DISPLAY_NAME: Final = FreeTextField(
    PUBLISHED_RECORD_TYPE, "/envelope/payload/standards[*]/declared_by/display_name", 1, 120
)
_SIGNAL_DESCRIPTION: Final = FreeTextField(
    PUBLISHED_RECORD_TYPE, "/envelope/payload/signals[*]/description_es", 1, 120
)
"""Los campos de texto libre de ``catalog_version_published`` (límites de su esquema)."""

_DETAIL_CODES: Final[Mapping[CatalogViolation, CatalogDetailCode]] = {
    CatalogViolation.PREDICATE_INVALID: CatalogDetailCode.PREDICATE_INVALID,
    CatalogViolation.UNSATISFIABLE_COVERAGE: CatalogDetailCode.UNSATISFIABLE_COVERAGE,
    CatalogViolation.ZONE_WITHOUT_CAMERAS: CatalogDetailCode.ZONE_WITHOUT_CAMERAS,
    CatalogViolation.LAST_STANDARD_IN_ZONE: CatalogDetailCode.LAST_STANDARD_IN_ZONE,
}


# --- Puertos ---------------------------------------------------------------------------------


class CatalogSigner(Protocol):
    """La parte de ``SigningPort`` (``SigningService``) que usa la publicación."""

    def sign(self, purpose: SigningPurpose, payload: Any) -> Any: ...


class AdmissionLookup(Protocol):
    """``admission_for`` de LC-GOB-02 (``AdmissionService``)."""

    async def admission_for(
        self, context: ScopeContext, plant_id: uuid.UUID, family: PredicateFamily
    ) -> FamilyAdmission | None: ...


class RegressionMarker(Protocol):
    """Punto de extensión: la marca de regresión, dentro de la transacción de la publicación.

    La implementación real llega con TASK-209; aquí la nula. Lo que lance revierte la publicación.
    """

    async def mark(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        previous: ZoneCatalogVersion | None,
        new: ZoneCatalogVersion,
        changed_fields: tuple[CatalogChangedField, ...],
    ) -> None: ...


class NullRegressionMarker:
    """No marca nada (TASK-209 trae la marca real)."""

    async def mark(
        self,
        transaction: Transaction,
        zone_id: uuid.UUID,
        previous: ZoneCatalogVersion | None,
        new: ZoneCatalogVersion,
        changed_fields: tuple[CatalogChangedField, ...],
    ) -> None:
        return None


# --- Errores ---------------------------------------------------------------------------------


class CatalogPublicationUnavailable(ExternalDependencyDown):
    """Transitorio: la firma no respondió o otra publicación de la zona ganó la carrera.

    Hacia la persona es ``temporarily_unavailable``; no queda nada escrito.
    """


class CatalogRequestInvalid(Exception):
    """La petición incumple un límite del contrato o de la entidad: ``invalid_request``."""

    api_code: Final = ApiErrorCode.INVALID_REQUEST

    def __init__(self) -> None:
        super().__init__("petición de catálogo fuera de los límites del contrato")


class CatalogPublicationFailed(Exception):
    """El expediente rechazó el registro por una causa que no es del cuerpo: se revierte."""

    def __init__(self, rejection: LedgerRejection) -> None:
        super().__init__(f"registro rechazado: {rejection.code.value}")
        self.rejection = rejection


class _RaceLost(Exception):
    """Otra publicación confirmó el mismo número: se traduce a transitorio fuera de la
    transacción (que así se revierte)."""


# --- Servicio --------------------------------------------------------------------------------


class CatalogPublicationService:
    """``catalog.versions``: publicar una versión y leer el sobre almacenado."""

    def __init__(
        self,
        *,
        repository: PostgresCatalogRepository,
        database: LedgerDatabase,
        writer: EscritorExpediente,
        authorizer: Authorizer,
        audit: AuditWriter,
        free_text: FreeTextPolicyRegistry,
        admissions: AdmissionLookup,
        signer: CatalogSigner,
        clock: Clock,
        regression_marker: RegressionMarker | None = None,
        sign_timeout_seconds: float = SIGN_TIMEOUT_SECONDS,
        random_bytes: Callable[[int], bytes] = os.urandom,
    ) -> None:
        if not sign_timeout_seconds > 0:
            raise ValueError("sign_timeout_seconds debe ser positivo")
        self._repository = repository
        self._database = database
        self._writer = writer
        self._authorizer = authorizer
        self._audit = audit
        self._free_text = free_text
        self._admissions = admissions
        self._signer = signer
        self._clock = clock
        self._regression = regression_marker or NullRegressionMarker()
        self._sign_timeout = sign_timeout_seconds
        self._random_bytes = random_bytes

    def __repr__(self) -> str:
        return "CatalogPublicationService()"

    # --- Alcance y textos ------------------------------------------------------------------

    async def _zone(
        self, context: ScopeContext, zone_id: uuid.UUID, key: PermissionKey
    ) -> tuple[ZoneRef, ScopeContext]:
        """La zona y el contexto autorizado sobre ella; inexistente o fuera de alcance, igual."""
        if not isinstance(context, ScopeContext):
            raise ResourceNotFound()
        if type(zone_id) is not uuid.UUID:
            raise ResourceNotFound()
        zone = await self._repository.zone(context, zone_id)
        if zone is None:
            raise ResourceNotFound()
        authorized = await self._authorizer.authorize(
            context, key, Resource.zone(context.organization_id, zone.plant_id, zone.zone_id)
        )
        return zone, authorized

    def _text(self, value: object, field: FreeTextField) -> str:
        if not isinstance(value, str):
            raise CatalogRequestInvalid
        try:
            return self._free_text.apply(value, field)
        except FreeTextRejected:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED) from None

    def _signals(self, signals: tuple[Mapping[str, Any], ...]) -> tuple[dict[str, Any], ...]:
        normalized: list[dict[str, Any]] = []
        for signal in signals:
            if not isinstance(signal, Mapping):
                raise CatalogRequestInvalid
            copy = dict(signal)
            if "description_es" in copy:
                copy["description_es"] = self._text(copy["description_es"], _SIGNAL_DESCRIPTION)
            normalized.append(copy)
        return tuple(normalized)

    def _normalized(self, change: CatalogChange) -> CatalogChange:
        """El cambio con sus textos en NFC, tras la política (``free_text_rejected``)."""
        if isinstance(change, NewStandard):
            draft = change.draft
            if not isinstance(draft, StandardDraft):
                raise CatalogRequestInvalid
            initial = change.initial
            if initial is not None:
                if not isinstance(initial, InitialZoneParameters):
                    raise CatalogRequestInvalid
                initial = dataclasses.replace(initial, signals=self._signals(initial.signals))
            return dataclasses.replace(
                change,
                draft=dataclasses.replace(
                    draft,
                    title_es=self._text(draft.title_es, _TITLE),
                    declared_text=self._text(draft.declared_text, _DECLARED_TEXT),
                ),
                initial=initial,
            )
        if isinstance(change, NewStandardVersion):
            return dataclasses.replace(
                change,
                title_es=None if change.title_es is None else self._text(change.title_es, _TITLE),
                declared_text=(
                    None
                    if change.declared_text is None
                    else self._text(change.declared_text, _DECLARED_TEXT)
                ),
            )
        if isinstance(change, SetSignals):
            return dataclasses.replace(change, signals=self._signals(change.signals))
        return change

    # --- Publicación -----------------------------------------------------------------------

    async def publish_catalog_version(
        self,
        context: ScopeContext,
        zone_id: uuid.UUID,
        change: CatalogChange,
        reason_es: str,
    ) -> ZoneCatalogVersion:
        """Publica la versión siguiente del catálogo de la zona; la devuelve tras confirmar.

        ``ResourceNotFound`` (zona o estándar inexistente o fuera de alcance),
        ``CatalogRejected`` con su ``detail_code``, ``CatalogRequestInvalid`` o
        ``CatalogPublicationUnavailable`` (transitorio); en todos, nada queda escrito.
        """
        reason = self._text(reason_es, _REASON)
        zone, authorized = await self._zone(context, zone_id, PermissionKey.CATALOG_MANAGE)
        role = authorized.actor.role_in_use
        if role is None:  # ``authorize`` siempre lo fija; sin él no se registra autor.
            raise ResourceNotFound()
        normalized = self._normalized(change)
        declared_by = DeclaredBy(
            _uuid(authorized.actor.id),
            self._text(authorized.actor.display_name_snapshot, _DISPLAY_NAME),
            Role(role).value,
        )
        writer_context = with_unit(authorized, ActorUnit.U03)
        try:
            async with self._database.transaction(writer_context) as transaction:
                version = await self._publish(
                    transaction,
                    writer_context,
                    zone,
                    normalized,
                    reason,
                    declared_by,
                    Role(role),
                )
        except CatalogRuleViolated as violation:
            raise _translated(violation) from None
        except (_RaceLost, CatalogWriteConflict):
            raise CatalogPublicationUnavailable("catalog_version_race") from None
        except sa_exc.IntegrityError:
            # Clave primaria (zone_id, catalog_version), source_key del registro o la versión
            # vigente única de un estándar: otra publicación confirmó antes (respaldo del
            # candado). Nunca un número repetido: esta se revierte entera.
            raise CatalogPublicationUnavailable("catalog_version_race") from None
        return version

    async def _publish(
        self,
        transaction: Transaction,
        context: ScopeContext,
        zone: ZoneRef,
        change: CatalogChange,
        reason: str,
        declared_by: DeclaredBy,
        role: Role,
    ) -> ZoneCatalogVersion:
        await self._repository.lock_zone(transaction, zone.zone_id)
        previous = await self._repository.current(transaction, zone.zone_id)
        await self._require_admission(context, zone, change, previous)
        now = to_millisecond(self._clock.now())
        # Un reloj que retrocede no deja una vigencia al revés: nunca antes de la vigente.
        issued_at = now if previous is None else max(now, previous.issued_at)
        plan = plan_publication(
            None if previous is None else _state(previous),
            change,
            zone=zone,
            issued_at=issued_at,
            declared_by=declared_by,
            reason_es=reason,
            new_standard_id=uuid7(self._clock, self._random_bytes),
        )
        envelope = await self._sign(plan.catalog)
        version_key = f"{zone.zone_id}:{plan.catalog_version}"
        scope = RecordScope(plant_id=zone.plant_id, zone_id=zone.zone_id)
        changed = [field.value for field in plan.changed_fields]
        written = await self._writer.write(
            context,
            PUBLISHED_RECORD_TYPE,
            {
                "source_key": version_key,
                "zone_id": str(zone.zone_id),
                "catalog_version": plan.catalog_version,
                "changed_fields": changed,
                "reason_es": reason,
                "envelope": envelope,
            },
            scope=scope,
            events=(
                NewEvent(
                    event_name=CATALOG_UPDATED,
                    payload={
                        "zone_id": str(zone.zone_id),
                        "catalog_version": plan.catalog_version,
                        "changed_fields": changed,
                    },
                ),
            ),
            occurred_at=issued_at,
            transaction=transaction,
        )
        record_id = _record_id(written)
        version = ZoneCatalogVersion(
            organization_id=zone.organization_id,
            plant_id=zone.plant_id,
            zone_id=zone.zone_id,
            catalog_version=plan.catalog_version,
            issued_at=issued_at,
            issued_by=declared_by.user_id,
            role_in_use=role,
            reason_es=reason,
            changed_fields=plan.changed_fields,
            payload=plan.catalog,
            envelope=envelope,
            single_occupancy=plan.single_occupancy,
            aggregation_window_minutes=plan.aggregation_window_minutes,
            ledger_record_id=record_id,
        )
        await self._repository.insert_version(transaction, version)
        if previous is not None:
            await self._repository.supersede(
                transaction, zone.zone_id, previous.catalog_version, issued_at
            )
        for standard_id, standard_version in plan.closed:
            await self._repository.close_standard(
                transaction, zone.zone_id, standard_id, standard_version, plan.catalog_version
            )
        for standard in plan.born:
            await self._repository.insert_standard(transaction, standard)
        for standard_id, standard_version in plan.retired:
            _record_id(
                await self._writer.write(
                    context,
                    RETIRED_RECORD_TYPE,
                    {
                        "source_key": f"{standard_id}:{standard_version}",
                        "zone_id": str(zone.zone_id),
                        "standard_id": str(standard_id),
                        "version": standard_version,
                        "retired_in_catalog_version": plan.catalog_version,
                        "reason_es": reason,
                    },
                    scope=scope,
                    occurred_at=issued_at,
                    transaction=transaction,
                )
            )
        if plan.single_occupancy_declared:
            _record_id(
                await self._writer.write(
                    context,
                    SINGLE_OCCUPANCY_RECORD_TYPE,
                    {
                        "source_key": version_key,
                        "zone_id": str(zone.zone_id),
                        "catalog_version": plan.catalog_version,
                        "single_occupancy": plan.single_occupancy,
                        "aggregation_window_minutes": plan.aggregation_window_minutes,
                        "reason_es": reason,
                    },
                    scope=scope,
                    occurred_at=issued_at,
                    transaction=transaction,
                )
            )
        if plan.cameras is not None:
            await self._repository.upsert_cameras(transaction, zone, plan.cameras, issued_at)
        await self._regression.mark(
            transaction, zone.zone_id, previous, version, plan.changed_fields
        )
        return version

    async def _require_admission(
        self,
        context: ScopeContext,
        zone: ZoneRef,
        change: CatalogChange,
        previous: ZoneCatalogVersion | None,
    ) -> None:
        """La familia del estándar que se declara está admitida en la planta (BR-GOB-16)."""
        family: PredicateFamily | None = None
        if isinstance(change, NewStandard):
            family = change.draft.family
        elif isinstance(change, NewStandardVersion) and previous is not None:
            for standard in previous.payload["standards"]:
                if standard["standard_id"] == str(change.standard_id):
                    family = PredicateFamily(standard["family"])
        if family is None:
            return
        admission = await self._admissions.admission_for(context, zone.plant_id, family)
        if admission is None or not admission.admitted:
            raise CatalogRejected(CatalogDetailCode.FAMILY_NOT_ADMITTED)

    async def _sign(self, catalog: Mapping[str, Any]) -> dict[str, Any]:
        """El sobre firmado del catálogo, o ``CatalogPublicationUnavailable`` (fallo cerrado)."""
        try:
            async with asyncio.timeout(self._sign_timeout):
                signed = await asyncio.to_thread(self._signer.sign, SigningPurpose.CATALOG, catalog)
        except (SigningKeyUnavailable, SigningNotReady, TimeoutError):
            raise CatalogPublicationUnavailable("catalog_signing") from None
        envelope: dict[str, Any] = signed.to_json_value()
        return envelope

    # --- Lecturas --------------------------------------------------------------------------

    async def stored_envelope(
        self, context: ScopeContext, zone_id: uuid.UUID, catalog_version: int | None = None
    ) -> Mapping[str, Any]:
        """El ``SignedEnvelope<ZoneCatalog>`` guardado (la vigente si no se pide versión).

        Nunca canonicaliza ni firma (PAT-GOB-REN-02). ``ResourceNotFound`` si la zona o la
        versión no existen o están fuera del alcance (``catalog.read``).
        """
        if catalog_version is not None and (
            type(catalog_version) is not int or catalog_version < 1
        ):
            raise ResourceNotFound()
        zone, authorized = await self._zone(context, zone_id, PermissionKey.CATALOG_READ)
        async with self._database.transaction(authorized) as transaction:
            envelope = await self._repository.envelope(transaction, zone.zone_id, catalog_version)
            if envelope is None:
                raise ResourceNotFound()
            if authorized.concession_id is not None:
                # BR-NUC-38 y A-56: la lectura del proveedor, auditada en la misma transacción.
                await self._audit.append(
                    authorized,
                    AuditOperation.CATALOG_READ,
                    plant_id=zone.plant_id,
                    zone_id=zone.zone_id,
                    result_count=1,
                    transaction=transaction,
                )
        return envelope

    async def standard_history(
        self, context: ScopeContext, zone_id: uuid.UUID
    ) -> tuple[DeclaredStandardVersion, ...]:
        """Las versiones de los estándares de la zona con su vigencia (``standard_valid_at``)."""
        zone, authorized = await self._zone(context, zone_id, PermissionKey.CATALOG_READ)
        return await self._repository.standard_history(authorized, zone.zone_id)


def _state(version: ZoneCatalogVersion) -> CatalogState:
    return CatalogState(
        catalog=version.payload,
        single_occupancy=version.single_occupancy,
        aggregation_window_minutes=version.aggregation_window_minutes,
    )


def _record_id(written: Receipt | LedgerRejection) -> uuid.UUID:
    """El identificador del registro escrito; un rechazo o un duplicado revierten."""
    if isinstance(written, LedgerRejection):
        if written.code is LedgerRejectionCode.FREE_TEXT_REJECTED:
            raise CatalogRejected(CatalogDetailCode.FREE_TEXT_REJECTED)
        if written.code is LedgerRejectionCode.IDEMPOTENCY_CONFLICT:
            raise _RaceLost
        raise CatalogPublicationFailed(written)
    if written.status is not AcceptanceStatus.ACCEPTED:
        # ``accepted_duplicate``: el mismo número ya está confirmado (otra publicación).
        raise _RaceLost
    return _uuid(written.record_id)


def _uuid(value: object) -> uuid.UUID:
    """asyncpg devuelve su propio tipo de UUID; el dominio exige ``uuid.UUID``."""
    return uuid.UUID(str(value))


def _translated(violation: CatalogRuleViolated) -> Exception:
    if violation.violation is CatalogViolation.STANDARD_NOT_FOUND:
        return ResourceNotFound()
    if violation.violation is CatalogViolation.REQUEST_INVALID:
        return CatalogRequestInvalid()
    return CatalogRejected(_DETAIL_CODES[violation.violation])
