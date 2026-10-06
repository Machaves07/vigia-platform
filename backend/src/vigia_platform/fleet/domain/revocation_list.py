"""Lista de revocación global de ``vigia-node-ca``: contenido y decisión del ciclo (TASK-220).

LC-GOB-11 y LC-GOB-18, PAT-GOB-RES-02 con su nota D-7, NFR-GOB-12 (nota D-7), 14, 46 y 48. Una
sola lista ``ca/crl.pem`` para **todas** las organizaciones (D-7), firmada por ``vigia-node-ca``,
publicada a los 5 minutos como máximo de cada revocación y regenerada a diario. La primera capa
(``node_revoked`` en cada petición, TASK-218 y VIG-144) no depende de ella.

**Contenido** (``revoked_certificates``): una entrada por credencial **no vencida** que ya no debe
autenticar en el balanceador:

- ``revoked``: con su ``revoked_at`` (revocación o re-alta);
- ``superseded``: sustituida por rotación; deja de autenticar en la capa 1 a las 24 h
  (``OVERLAP``) del ``issued_at`` de su sucesora, que es su fecha de revocación en la lista (nota
  de Notes de TASK-220: incluirlas mantiene la defensa en profundidad);
- ``overlapping`` cuyo solapamiento ya terminó: es una ``superseded`` que la plataforma aún no
  materializó (la transición es perezosa, nota de DE §4). El barrido lee en transacciones de solo
  lectura, así que no la materializa: la trata igual que a una ``superseded``.

Las vencidas (``expires_at <= ahora``) se retiran al regenerar: el balanceador ya las rechaza.

**Ciclo** (``cycle_reason``): trabaja si la marca única está puesta
(``dirty_generation > published_generation``), si toca la regeneración diaria (última publicación
hace 24 h o más, o ninguna todavía) o si la orden administrativa la fuerza (NFR-GOB-21).

**Alarma** (``alarm_active``): un ciclo cuya publicación falló, o una lista vigente a menos de
24 h de su ``next_update`` (NFR-GOB-46, 48), cuentan en ``revocation_list_publish_failed``.

``TrustStorePublisherPort`` y ``RevocationListSignerPort`` son los puertos del ciclo: el dominio no
conoce boto3, KMS ni ``cryptography``. Módulo puro: sin la hora del sistema.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from vigia_platform.fleet.domain.enums import CredentialStatus
from vigia_platform.fleet.domain.node_credential import OVERLAP

__all__ = [
    "DAILY_REGENERATION",
    "EXPIRY_ALARM",
    "PUBLISH_STEP_TIMEOUT_SECONDS",
    "VALIDITY",
    "CredentialRevocationFacts",
    "CycleReason",
    "PublishStep",
    "PublishedRevocationList",
    "RevocationListPlan",
    "RevocationListPublishFailed",
    "RevocationListSignerPort",
    "RevocationListStatus",
    "RevocationReason",
    "RevokedCertificate",
    "SignedRevocationList",
    "TrustStorePublisherPort",
    "alarm_active",
    "cycle_reason",
    "plan_revocation_list",
    "revoked_certificates",
    "seconds_to_expiry",
]

VALIDITY: Final = timedelta(days=7)
"""``next_update = last_update + 7 días`` (NFR-GOB-48, tech-stack §2.2)."""
DAILY_REGENERATION: Final = timedelta(hours=24)
"""Sin marca, se regenera si la última publicación tiene 24 h o más (LC-GOB-18)."""
EXPIRY_ALARM: Final = timedelta(hours=24)
"""Una lista vigente a menos de 24 h de vencer levanta la alarma (NFR-GOB-46)."""
PUBLISH_STEP_TIMEOUT_SECONDS: Final = 5.0
"""Tope de cada paso externo de la publicación: firma, depósito y almacén (NFR-GOB-43)."""


class RevocationReason(enum.StrEnum):
    """Por qué la credencial está en la lista (``CRLReason`` de la entrada)."""

    REVOKED = "revoked"
    SUPERSEDED = "superseded"


class PublishStep(enum.StrEnum):
    """El paso que falló (código del registro estructurado; nunca un ARN ni un número de serie).

    Menos de 20 caracteres: la política de atributos toma por token una tira más larga.
    """

    SIGN = "crl_sign"
    PUT_OBJECT = "crl_put_object"
    LIST_REVOCATIONS = "crl_list_previous"
    ADD_REVOCATIONS = "crl_add_revocation"
    VERIFY_REVOCATIONS = "crl_verify_count"
    REMOVE_REVOCATIONS = "crl_remove_previous"


class RevocationListPublishFailed(Exception):
    """Un paso de la firma o de la publicación falló o superó su tope: la marca no se toca.

    El mensaje es constante y en español: nunca lleva el ARN, la clave, el PEM ni un número de
    serie (NFR-GOB-13, 25). ``code`` es el ``last_error_code`` y el ``code`` del registro.
    """

    code: Final = "crl_publish_failed"

    def __init__(self, step: PublishStep) -> None:
        super().__init__("la lista de revocación no se pudo publicar")
        self.step = PublishStep(step)


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialRevocationFacts:
    """Lo que el barrido lee de una credencial (``fleet.node_credential``) y de su sucesora."""

    certificate_serial: str
    status: CredentialStatus
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    successor_issued_at: datetime | None = None
    """``issued_at`` de la primera credencial que rotó desde esta; ``None`` si no hay."""


@dataclass(frozen=True, slots=True, order=True)
class RevokedCertificate:
    """Una entrada de la lista: número de serie, fecha de revocación y motivo."""

    serial_number: int
    revocation_date: datetime
    reason: RevocationReason


def _revocation(
    facts: CredentialRevocationFacts, now: datetime
) -> tuple[datetime, RevocationReason] | None:
    status = CredentialStatus(facts.status)
    if status is CredentialStatus.REVOKED:
        return (facts.revoked_at or now, RevocationReason.REVOKED)
    successor = facts.successor_issued_at
    if status is CredentialStatus.SUPERSEDED:
        ended = successor + OVERLAP if successor is not None else facts.issued_at
        return (min(ended, now), RevocationReason.SUPERSEDED)
    if status is CredentialStatus.OVERLAPPING and successor is not None:
        ended = successor + OVERLAP
        if now >= ended:  # fuera del solapamiento: una superseded aún sin materializar
            return (ended, RevocationReason.SUPERSEDED)
    return None


def revoked_certificates(
    facts: Iterable[CredentialRevocationFacts], now: datetime
) -> tuple[RevokedCertificate, ...]:
    """Las entradas de la lista en ``now``, sin vencidas y en orden de número de serie."""
    if now.utcoffset() is None:
        raise ValueError("now debe llevar zona horaria")
    entries: dict[int, RevokedCertificate] = {}
    for item in facts:
        if item.expires_at <= now:
            continue  # vencida: se retira al regenerar
        revocation = _revocation(item, now)
        if revocation is None:
            continue
        serial = int(item.certificate_serial, 16)
        date, reason = revocation
        entry = RevokedCertificate(serial, _whole_second(min(date, now)), reason)
        current = entries.get(serial)
        if current is None or entry.revocation_date < current.revocation_date:
            entries[serial] = entry
    return tuple(entries[serial] for serial in sorted(entries))


def _whole_second(moment: datetime) -> datetime:
    """Las marcas de una lista X.509 van en segundos (``UTCTime``)."""
    return moment.replace(microsecond=0)


# --- Estado y ciclo -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RevocationListStatus:
    """La fila global ``fleet.revocation_list_state`` (gob_0021 y gob_0025)."""

    dirty_generation: int
    published_generation: int
    published_at: datetime | None
    next_update: datetime | None
    entries: int
    crl_number: int
    object_version_id: str | None = None

    @property
    def dirty(self) -> bool:
        return self.dirty_generation > self.published_generation


class CycleReason(enum.StrEnum):
    """Por qué trabaja un ciclo."""

    DIRTY = "dirty"
    DAILY = "daily"
    FORCED = "forced"


def cycle_reason(
    status: RevocationListStatus, now: datetime, *, force: bool = False
) -> CycleReason | None:
    """``None`` si la lista publicada está al día y no toca la regeneración diaria."""
    if force:
        return CycleReason.FORCED
    if status.dirty:
        return CycleReason.DIRTY
    if status.published_at is None or now - status.published_at >= DAILY_REGENERATION:
        return CycleReason.DAILY
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class RevocationListPlan:
    """La lista que se va a firmar: número, vigencia y entradas."""

    crl_number: int
    last_update: datetime
    next_update: datetime
    entries: tuple[RevokedCertificate, ...]


def plan_revocation_list(
    entries: Iterable[RevokedCertificate], *, crl_number: int, now: datetime
) -> RevocationListPlan:
    """``last_update = now`` (al segundo) y ``next_update = last_update + 7 días``."""
    if isinstance(crl_number, bool) or not isinstance(crl_number, int) or crl_number < 1:
        raise ValueError("crl_number debe ser un entero positivo")
    if now.utcoffset() is None:
        raise ValueError("now debe llevar zona horaria")
    last_update = _whole_second(now)
    return RevocationListPlan(
        crl_number=crl_number,
        last_update=last_update,
        next_update=last_update + VALIDITY,
        entries=tuple(sorted(entries)),
    )


def seconds_to_expiry(next_update: datetime | None, now: datetime) -> float | None:
    """Segundos hasta ``next_update`` de la lista vigente (negativo si venció)."""
    return None if next_update is None else (next_update - now).total_seconds()


def alarm_active(*, failed: bool, seconds_left: float | None) -> bool:
    """La condición de ``revocation-list-publish-failed`` (NFR-GOB-46, 48)."""
    return failed or (seconds_left is not None and seconds_left < EXPIRY_ALARM.total_seconds())


# --- Puertos ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SignedRevocationList:
    """La lista firmada en PEM, ya verificada con la clave pública de la raíz."""

    pem: bytes
    crl_number: int
    last_update: datetime
    next_update: datetime
    entries: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishedRevocationList:
    """Lo publicado: la versión del objeto ``ca/crl.pem`` y la revocación del almacén."""

    object_version_id: str
    revocation_id: int
    removed: int = 0
    """Listas anteriores retiradas del almacén en esta publicación."""


class RevocationListSignerPort(Protocol):
    """Firma con ``vigia-node-ca`` (``kms:Sign``); ``RevocationListPublishFailed(SIGN)`` si no."""

    async def sign(self, plan: RevocationListPlan) -> SignedRevocationList: ...


class TrustStorePublisherPort(Protocol):
    """Publica en ``vigia-edge`` y en el almacén ``vigia-node-trust`` (infraestructura §5.3).

    Cada paso tiene su tope de 5 s; un fallo lanza ``RevocationListPublishFailed`` con el paso.
    Repetir tras un paso parcial no tiene efecto doble: cada publicación añade la lista nueva y
    retira **todas** las que había antes de añadirla.
    """

    async def publish(self, revocation_list: SignedRevocationList) -> PublishedRevocationList: ...
