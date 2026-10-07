"""Alta del nodo: código, huella, firma de los dos certificados y una sola transacción (TASK-219;
BR-GOB-58, 61 a 64, 68; BLM §2.3, §3.4 y su nota de re-alta, §3.5; LC-GOB-11).

La ruta del contrato (``node_api.routes.enrollment``) ya validó versión, tamaño, esquema y las dos
CSR, sacó el ``node_id`` del nombre común y resolvió el **contexto de la organización del nodo
declarado** (``EnrollmentScope``, A-51). ``EnrollmentService.enroll`` sigue el orden del diseño:

1. **código** (``EnrollmentCodeService.verify`` de TASK-218, en tiempo constante sobre los códigos
   del nodo): inválido, usado o vencido → intento registrado con su resultado y rechazo
   permanente. Va primero: sin el código, ninguna respuesta depende de que el nodo exista
   (PR-GOB-12; VIG-165);
2. **dirección de la vista en vivo** (``csr.announced_host``): la del nodo si ya la anunció y es
   local, o el único nombre alternativo local de la CSR de servidor (``schema_invalid`` si no, sin
   intento y sin consumir el código). Una URL guardada que no es local no bloquea la re-alta
   (VIG-185);
3. **huella de hardware**: en la primera alta se fija; en la re-alta (``re_enrollment_pending``)
   tiene que ser la registrada. Con otra, ``enrollment_code_invalid`` **sin consumir** el código y
   con el intento registrado (G-2; el cambio de equipo es un reemplazo con ``node_id`` nuevo);
4. **configuración inicial**: los sobres ya almacenados de cada zona asignada, sus cámaras y la
   ``NodeConfiguration`` (``PostgresBootstrapEnvelopes``), validada con el modelo estricto de
   U-01; las claves públicas de la plataforma desde la caché de ``SigningService``. **Cero**
   llamadas a ``SigningPort.sign``, salvo el sobre de compuerta inicial (A-60, VIG-180) de la zona
   anterior a él que aún no tiene ninguno: ``GateService.ensure_initial_envelopes`` lo firma una
   sola vez por zona, fuera de toda transacción del alta, y se vuelve a leer lo guardado. Una zona
   sin catálogo (o un nodo sin cámaras), o la firma de ese sobre inicial caída, es
   **transitorio** y no consume el código (decisión del redactor de TASK-219);
5. **firma** de los dos certificados (``NodeCaIssuer``, ``kms:Sign`` con plazo): sin respuesta,
   transitorio y nada escrito;
6. **una transacción** (BR-GOB-61): la ficha del nodo bloqueada (primer candado del orden de
   ``fleet.application.common``), el consumo del código (``UPDATE`` condicional de una fila), las
   ``overlapping`` vencidas a ``superseded``, la ``NodeCredential`` ``active``, ``enrolled_at`` y
   la huella, ``IdentityCommandPort.update_node(status = enrolled)`` en la misma transacción, el
   ``EnrollmentAttempt`` ``accepted`` y el registro ``node_enrolled`` con su evento (cadena de la
   planta, el último candado). Si algo falla, nada se confirma y el código sigue ``active``.

**Concurrencia**: dos altas con el mismo código pasan las dos la verificación, pero la ficha
bloqueada las ordena y solo la condición del consumo deja pasar una: la otra se revierte entera,
vuelve a verificar fuera de la transacción (``enrollment_code_used``) y deja su intento. Una
revocación o una reemisión simultáneas también toman la ficha primero.

``source_key`` de ``node_enrolled`` es ``node_id`` + ``credential_id`` (A-55): la re-alta del mismo
nodo escribe otro registro sin chocar como ``idempotency_conflict``.

El código, el PEM y la huella en claro nunca llegan a un registro estructurado, una métrica, una
traza ni un evento (NFR-GOB-25): la huella solo va al expediente (``node_enrolled``) y a la fila
del intento, como exige el diseño.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Protocol

from pydantic import ValidationError
from vigia_contracts.models.node_enrollment import NodeInitialConfiguration

from vigia_platform.catalog.application.gates import GateUnavailable
from vigia_platform.fleet.adapters.ca.certificate_profiles import (
    IssuedCertificates,
    NodeCaIssuer,
    NodeCaUnavailable,
)
from vigia_platform.fleet.adapters.ca.csr import NodeCsr, announced_host
from vigia_platform.fleet.adapters.postgres.bootstrap_envelopes import PostgresBootstrapEnvelopes
from vigia_platform.fleet.adapters.postgres.credential_store import PostgresCredentialStore
from vigia_platform.fleet.application.common import FleetDependencies, write
from vigia_platform.fleet.application.enrollment_codes import (
    ENROLLABLE_STATUSES,
    AttemptRequest,
    EnrollmentCodeService,
)
from vigia_platform.fleet.application.node_revocation import lifecycle_payload
from vigia_platform.fleet.domain.enrollment_attempt import SourceIpHasher
from vigia_platform.fleet.domain.enums import CredentialStatus, EnrollmentAttemptResult
from vigia_platform.fleet.domain.node_configuration import (
    BootstrapZone,
    ConfigurationUnavailable,
    NodeConfiguration,
    initial_configuration,
)
from vigia_platform.fleet.domain.node_credential import NodeCredential, stale_overlapping
from vigia_platform.fleet.domain.node_fleet_record import FleetNode, NodeFleetRecord
from vigia_platform.fleet.domain.node_subject import NodeSubject, serial_hex
from vigia_platform.fleet.record_types import enrollment_source_key
from vigia_platform.identity.authz.context import EnrollmentScope
from vigia_platform.shared.context import ScopeContext
from vigia_platform.shared.db import Transaction
from vigia_platform.shared.ids import uuid7
from vigia_platform.shared.observability.logging import get_logger
from vigia_platform.shared.outbox.publish import NewEvent
from vigia_platform.shared.secrets import SecretsPort
from vigia_platform.shared.signing.keys import (
    NODE_PURPOSES,
    SigningKeyRecord,
    SigningPurpose,
    format_timestamp,
)
from vigia_platform.shared.signing.service import SigningKeyUnavailable, SigningNotReady

__all__ = [
    "BOOTSTRAP_RETRY_AFTER_SECONDS",
    "CA_RETRY_AFTER_SECONDS",
    "ENROLLED_EVENT",
    "ENROLLED_RECORD_TYPE",
    "CredentialUnavailable",
    "EnrollmentPresentation",
    "EnrollmentRejected",
    "EnrollmentService",
    "FixedSourceKey",
    "InitialGateEnvelopes",
    "IssuedCredential",
    "PlatformKeys",
    "SecretSourceKey",
    "SourceKeyProvider",
    "SourceKeyUnavailable",
    "UnconfiguredSourceKey",
    "fingerprint_matches",
    "platform_public_keys",
]

ENROLLED_RECORD_TYPE: Final = "node_enrolled"
ENROLLED_EVENT: Final = "node_enrolled"
BOOTSTRAP_RETRY_AFTER_SECONDS: Final = 60
"""Reintento del alta sin catálogo o sin sobre de compuerta ``[objetivo propio]``."""
CA_RETRY_AFTER_SECONDS: Final = 5
"""Reintento con la autoridad sin respuesta (el de U-02, PAT-GOB-RES-03)."""

_log = get_logger("fleet.credentials")


class EnrollmentRejected(Exception):
    """Rechazo permanente del alta con su ``rejection_code`` (el intento ya quedó registrado)."""

    def __init__(self, result: EnrollmentAttemptResult) -> None:
        super().__init__(f"alta rechazada: {result.value}")
        self.result = EnrollmentAttemptResult(result)


class CredentialUnavailable(Exception):
    """Transitorio: la autoridad, la configuración inicial o la clave del origen no están
    disponibles ahora. Nada se escribió y el código sigue ``active``."""

    def __init__(self, cause: str, retry_after_seconds: int) -> None:
        super().__init__(f"credencial no disponible: {cause}")
        self.cause = cause
        self.retry_after_seconds = retry_after_seconds


class _CodeLost(Exception):
    """Otra transacción consumió, sustituyó o venció el código (o revocó el nodo) antes."""


class _FingerprintChanged(Exception):
    """La ficha bloqueada tiene otra huella que la presentada."""


# --- Clave estable del hash de origen -------------------------------------------------------------


class SourceKeyUnavailable(Exception):
    """No hay clave estable del hash de origen: el alta no puede registrar sus intentos."""


class SourceKeyProvider(Protocol):
    """La clave HMAC del origen de red, la misma en todas las instancias (TASK-218)."""

    async def hasher(self) -> SourceIpHasher: ...


@dataclass(frozen=True, slots=True)
class FixedSourceKey:
    """Una clave ya leída (pruebas y arranques que la tienen en memoria)."""

    key: bytes = field(repr=False)

    async def hasher(self) -> SourceIpHasher:
        return SourceIpHasher(self.key)


class SecretSourceKey:
    """La clave del secreto ``secret_id`` del gestor de secretos (caché de 5 min del puerto)."""

    def __init__(self, secrets: SecretsPort, secret_id: str) -> None:
        self._secrets = secrets
        self._secret_id = secret_id

    def __repr__(self) -> str:
        return "SecretSourceKey()"

    async def hasher(self) -> SourceIpHasher:
        try:
            return SourceIpHasher(await self._secrets.get(self._secret_id))
        except ValueError:
            _log.error("la clave del hash de origen del alta no es válida")
            raise SourceKeyUnavailable("clave del hash de origen no válida") from None
        except Exception:
            _log.error("no se pudo leer la clave del hash de origen del alta")
            raise SourceKeyUnavailable("clave del hash de origen no disponible") from None


class UnconfiguredSourceKey:
    """Sin ``VIGIA_ENROLLMENT_SOURCE_KEY_SECRET``: el alta falla cerrada (no registra intentos
    con una clave por proceso, que no se podrían cruzar entre instancias)."""

    async def hasher(self) -> SourceIpHasher:
        _log.error("el alta no tiene la clave estable del hash de origen")
        raise SourceKeyUnavailable("falta la clave estable del hash de origen")


class _UnusedRoots:
    """El alta nunca emite códigos: las huellas de las raíces no se leen aquí."""

    async def fingerprints(self) -> tuple[str, ...]:  # pragma: no cover - nunca se llama
        raise RuntimeError("el alta no emite códigos")


# --- Piezas comunes con la rotación ---------------------------------------------------------------


class InitialGateEnvelopes(Protocol):
    """``GateService.ensure_initial_envelopes`` (A-60): el sobre inicial de la zona sin ninguno."""

    async def ensure_initial_envelopes(
        self, context: ScopeContext, zone_ids: Iterable[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]: ...


class PlatformKeys(Protocol):
    """``SigningService.public_keys``: las claves publicadas, desde la caché del proceso."""

    def public_keys(self, purpose: SigningPurpose) -> tuple[SigningKeyRecord, ...]: ...


def platform_public_keys(keys: PlatformKeys) -> list[dict[str, str]]:
    """``platform_public_keys``: las ``PublicKey`` de los propósitos del nodo, sin firmar nada.

    ``catalog``, ``gate`` y ``live_view_token``, más ``key_set``: el contrato las describe como
    las que el nodo fija «para verificar catálogos, compuertas, tokens y conjuntos de claves»
    (continuidad de claves, PAT-SEG-01). Sin ninguna, transitorio.
    """
    try:
        records = [record for purpose in NODE_PURPOSES for record in keys.public_keys(purpose)]
    except (SigningNotReady, SigningKeyUnavailable):
        raise CredentialUnavailable("platform_keys", CA_RETRY_AFTER_SECONDS) from None
    if not records:
        raise CredentialUnavailable("platform_keys", CA_RETRY_AFTER_SECONDS)
    return [record.to_contract() for record in sorted(records, key=lambda r: r.key_id)]


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """Lo que la ruta convierte en ``NodeCredential`` del contrato (alta o rotación)."""

    credential: NodeCredential
    certificates: IssuedCertificates
    platform_public_keys: tuple[Mapping[str, str], ...]
    initial_configuration: Mapping[str, Any] | None = None
    """Solo en el alta (``NodeEnrollmentResponse``)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrollmentPresentation:
    """Lo que el nodo presentó (``NodeEnrollmentRequest`` ya validado) y su origen."""

    code: str = field(repr=False)
    hardware_fingerprint: str = field(repr=False)
    software_version: str
    contract_version: str
    client: NodeCsr = field(repr=False)
    server: NodeCsr = field(repr=False)
    correlation_id: uuid.UUID
    source_address: str | None = field(default=None, repr=False)

    def attempt(self) -> AttemptRequest:
        return AttemptRequest(
            presented_code=self.code,
            hardware_fingerprint=self.hardware_fingerprint,
            software_version=self.software_version,
            contract_version=self.contract_version,
            source_address=self.source_address,
            correlation_id=self.correlation_id,
        )


def fingerprint_matches(record: NodeFleetRecord, presented: str) -> bool:
    """Primera alta: cualquiera (se fija). Re-alta: la registrada, y solo esa (U03-H-04)."""
    return record.hardware_fingerprint is None or record.hardware_fingerprint == presented


@dataclass(frozen=True, slots=True)
class _Bootstrap:
    zones: tuple[uuid.UUID, ...]
    configuration: Mapping[str, Any]


class EnrollmentService:
    """``POST enrollment`` del contrato sobre los servicios de TASK-218 y ``vigia-node-ca``."""

    def __init__(
        self,
        deps: FleetDependencies,
        *,
        issuer: NodeCaIssuer,
        keys: PlatformKeys,
        source_key: SourceKeyProvider,
        ingest_base_url: str | None,
        gates: InitialGateEnvelopes,
        credentials: PostgresCredentialStore | None = None,
        envelopes: PostgresBootstrapEnvelopes | None = None,
    ) -> None:
        self._deps = deps
        self._issuer = issuer
        self._keys = keys
        self._source_key = source_key
        self._ingest_base_url = ingest_base_url
        self._gates = gates
        self._credentials = credentials if credentials is not None else PostgresCredentialStore()
        self._envelopes = envelopes if envelopes is not None else PostgresBootstrapEnvelopes()

    def __repr__(self) -> str:
        return "EnrollmentService()"

    async def _codes(self) -> EnrollmentCodeService:
        try:
            hasher = await self._source_key.hasher()
        except SourceKeyUnavailable:
            raise CredentialUnavailable("source_key", CA_RETRY_AFTER_SECONDS) from None
        return EnrollmentCodeService(self._deps, roots=_UnusedRoots(), source_hasher=hasher)

    async def unknown_node(self, presentation: EnrollmentPresentation) -> None:
        """El nombre común no es de ningún nodo declarado: sin organización, el intento deja solo
        la métrica y un registro estructurado con la etiqueta del origen (TASK-218)."""
        codes = await self._codes()
        await codes.register_attempt(
            None, presentation.attempt(), EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
        )

    async def enroll(
        self, enrollment: EnrollmentScope, presentation: EnrollmentPresentation
    ) -> IssuedCredential:
        """El alta del nodo de ``enrollment`` (ver el módulo). ``CsrRejected`` (esquema),
        ``EnrollmentRejected`` (permanente) o ``CredentialUnavailable`` (transitorio)."""
        if not isinstance(enrollment, EnrollmentScope):
            raise TypeError("enrollment debe ser el EnrollmentScope del alta")
        deps = self._deps
        context = enrollment.context
        node = await deps.nodes.node(context, enrollment.node_id)
        codes = await self._codes()
        attempt = presentation.attempt()
        check = await codes.verify(enrollment, presentation.code)
        if not check.valid or check.code is None or node is None:
            result = (
                check.result if not check.valid else EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
            )
            await codes.register_attempt(enrollment, attempt, result)
            raise EnrollmentRejected(result)
        # Solo con el código aceptado: nada que dependa del nodo encontrado responde antes, así
        # que sin código un node_id existente y uno inexistente reciben el mismo rechazo
        # (PR-GOB-12, NFR-GOB-30; VIG-165). Una CSR de servidor sin un único nombre local sigue
        # siendo schema_invalid, sin intento y sin consumir el código.
        host = announced_host(presentation.server, node.live_view_local_url)
        if not fingerprint_matches(node.record, presentation.hardware_fingerprint):
            # G-2: otro equipo con el código del nodo. El código no se consume.
            await codes.register_attempt(
                enrollment, attempt, EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
            )
            raise EnrollmentRejected(EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID)
        bootstrap = await self._bootstrap(enrollment, node)
        public_keys = platform_public_keys(self._keys)
        now = deps.clock.now()
        subject = NodeSubject(node.node_id, node.organization_id, node.plant_id)
        try:
            issued = await self._issuer.issue(
                subject,
                presentation.client.public_key,
                presentation.server.public_key,
                host,
                now=now,
            )
        except NodeCaUnavailable:
            raise CredentialUnavailable("node_ca", CA_RETRY_AFTER_SECONDS) from None
        credential = NodeCredential(
            credential_id=_uuid7(deps),
            organization_id=node.organization_id,
            plant_id=node.plant_id,
            node_id=node.node_id,
            certificate_serial=serial_hex(issued.client.serial_number),
            issued_at=now,
            expires_at=issued.client.not_valid_after_utc,
        )
        try:
            await self._commit(
                enrollment, codes, attempt, presentation, check.code.code_id, credential, bootstrap
            )
        except _CodeLost:
            recheck = await codes.verify(enrollment, presentation.code)
            lost = (
                recheck.result
                if not recheck.valid
                else EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
            )
            await codes.register_attempt(enrollment, attempt, lost)
            raise EnrollmentRejected(lost) from None
        except _FingerprintChanged:
            await codes.register_attempt(
                enrollment, attempt, EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID
            )
            raise EnrollmentRejected(EnrollmentAttemptResult.ENROLLMENT_CODE_INVALID) from None
        return IssuedCredential(
            credential=credential,
            certificates=issued,
            platform_public_keys=tuple(public_keys),
            initial_configuration=bootstrap.configuration,
        )

    async def _bootstrap(self, enrollment: EnrollmentScope, node: FleetNode) -> _Bootstrap:
        """Los sobres guardados, las cámaras y la configuración; solo firma el sobre inicial de
        la zona que aún no tiene ninguno (A-60)."""
        base_url = self._ingest_base_url
        if base_url is None:
            _log.error("el alta no tiene VIGIA_NODES_BASE_URL")
            raise CredentialUnavailable("ingest_base_url", BOOTSTRAP_RETRY_AFTER_SECONDS)
        zones, stored, configuration = await self._stored(enrollment, node)
        missing = [zone.zone_id for zone in stored if zone.gate_envelope is None]
        if missing:
            # A-60: la zona anterior al sobre inicial lo recibe ahora (una sola vez por zona).
            try:
                await self._gates.ensure_initial_envelopes(enrollment.context, missing)
            except GateUnavailable:
                _log.warning("alta aplazada: la firma del sobre inicial de compuertas no responde")
                raise CredentialUnavailable("bootstrap", BOOTSTRAP_RETRY_AFTER_SECONDS) from None
            zones, stored, configuration = await self._stored(enrollment, node)
        try:
            document = initial_configuration(configuration, stored, ingest_base_url=base_url)
        except ConfigurationUnavailable:
            _log.warning("alta sin catálogo o sin sobre de compuerta en una zona del nodo")
            raise CredentialUnavailable("bootstrap", BOOTSTRAP_RETRY_AFTER_SECONDS) from None
        if not _admitted(document):
            _log.error("la configuración inicial no cumple el contrato")
            raise CredentialUnavailable("bootstrap", BOOTSTRAP_RETRY_AFTER_SECONDS)
        return _Bootstrap(zones=tuple(zones), configuration=document)

    async def _stored(
        self, enrollment: EnrollmentScope, node: FleetNode
    ) -> tuple[Sequence[uuid.UUID], Sequence[BootstrapZone], NodeConfiguration]:
        """Las zonas del nodo, lo guardado de cada una y su configuración (una transacción)."""
        deps = self._deps
        async with deps.database.transaction(enrollment.context) as transaction:
            zones = await deps.nodes.current_zones(transaction, node.node_id)
            stored = await self._envelopes.zones(transaction, node.plant_id, zones)
            try:
                configuration = await self._envelopes.configuration(
                    transaction, node.plant_id, node.node_id
                )
            except ConfigurationUnavailable:
                _log.error("la configuración guardada del nodo no cumple el contrato")
                raise CredentialUnavailable(
                    "configuration", BOOTSTRAP_RETRY_AFTER_SECONDS
                ) from None
        return zones, stored, configuration

    async def _commit(
        self,
        enrollment: EnrollmentScope,
        codes: EnrollmentCodeService,
        attempt: AttemptRequest,
        presentation: EnrollmentPresentation,
        code_id: uuid.UUID,
        credential: NodeCredential,
        bootstrap: _Bootstrap,
    ) -> None:
        deps = self._deps
        context = enrollment.context
        node_id = enrollment.node_id
        now = credential.issued_at
        fingerprint = presentation.hardware_fingerprint
        async with deps.database.transaction(context) as transaction:
            # Primer candado del orden de ``common``: la ficha del nodo.
            locked = await deps.nodes.lock(transaction, node_id)
            if (
                locked is None
                or locked.status not in ENROLLABLE_STATUSES
                or locked.record.revoked
                or locked.record.decommissioned
            ):
                raise _CodeLost()
            if not fingerprint_matches(locked.record, fingerprint):
                raise _FingerprintChanged()
            if not await codes.consume(transaction, code_id, now):
                raise _CodeLost()
            zones = await deps.nodes.current_zones(transaction, node_id)
            if tuple(zones) != bootstrap.zones:
                # Una reasignación entre la lectura y la transacción: la configuración ya no es la
                # del nodo. Transitorio, nada escrito.
                raise CredentialUnavailable("zones_changed", CA_RETRY_AFTER_SECONDS)
            await self._supersede_stale(transaction, node_id, now)
            await self._credentials.insert(transaction, credential)
            if not await self._credentials.mark_enrolled(transaction, node_id, now, fingerprint):
                raise _FingerprintChanged()
            await deps.identity.update_node(
                context, node_id, "enrolled", locked.live_view_local_url, transaction=transaction
            )
            await codes.register_attempt(
                enrollment, attempt, EnrollmentAttemptResult.ACCEPTED, transaction=transaction
            )
            await write(
                deps,
                context,
                transaction,
                ENROLLED_RECORD_TYPE,
                {
                    "source_key": enrollment_source_key(
                        str(node_id), str(credential.credential_id)
                    ),
                    "node_id": str(node_id),
                    "credential_id": str(credential.credential_id),
                    "zone_ids": [str(zone) for zone in zones],
                    "hardware_fingerprint": fingerprint,
                    "certificate_serial": credential.certificate_serial,
                    "enrolled_at": format_timestamp(now),
                },
                plant_id=locked.plant_id,
                occurred_at=now,
                events=(
                    NewEvent(event_name=ENROLLED_EVENT, payload=lifecycle_payload(locked, zones)),
                ),
            )

    async def _supersede_stale(
        self, transaction: Transaction, node_id: uuid.UUID, now: datetime
    ) -> None:
        """Las ``overlapping`` que ya no autentican pasan a ``superseded`` (nota de §4)."""
        stored = await self._credentials.credentials(transaction, node_id)
        for credential_id in stale_overlapping(stored, now):
            await self._credentials.transition(
                transaction,
                credential_id,
                CredentialStatus.OVERLAPPING,
                CredentialStatus.SUPERSEDED,
            )


def _admitted(document: Mapping[str, Any]) -> bool:
    """¿Cumple ``NodeInitialConfiguration`` de U-01 (16 zonas, 8 cámaras, un sobre de compuerta
    por zona, ``mute_after_seconds`` cinco veces el intervalo…)? Se valida **antes** de consumir el
    código; lo que viaja es ``document`` tal cual, con los sobres como se guardaron."""
    try:
        NodeInitialConfiguration.model_validate_json(json.dumps(document, allow_nan=False))
    except (ValidationError, ValueError):
        return False
    return True


def _uuid7(deps: FleetDependencies) -> uuid.UUID:
    return uuid7(deps.clock, deps.random_bytes)
