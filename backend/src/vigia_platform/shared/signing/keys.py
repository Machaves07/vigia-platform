"""Claves de firma, rotación y conjunto publicado: el dominio puro de ``shared.signing``.

Sin E/S ni hora del sistema: todo instante llega como argumento. ``SigningService``
(``shared.signing.service``) lo usa y las pruebas con estado (PR-NUC-35) lo comparan con un modelo.

Reglas (BR-NUC-84 a 86; ``business-logic-model.md`` §9; ``domain-entities.md`` §2.12):

- Cinco propósitos: ``catalog``, ``gate``, ``live_view_token``, ``key_set`` (los cuatro que conoce
  el nodo, ``KeyPurpose`` del contrato) y ``checkpoint`` (solo de la plataforma).
- Exactamente una clave ``active`` por propósito y a lo sumo una ``overlapping``.
- Una clave nace ``active`` con vigencia de 365 días. Al rotar, la anterior pasa a ``overlapping``
  y deja de firmar; sigue publicada hasta su ``valid_until``, que se acota a **30 días** después de
  la rotación (solapamiento). Si ya había una ``overlapping`` de ese propósito (dos rotaciones en
  menos de 30 días), esa se retira en el acto: nunca hay dos.
- Al pasar ``valid_until``, ``overlapping`` → ``retired``. Una ``active`` vencida sigue ``active``
  hasta que se rote (la tarea ``key_rotation_reminder`` la rota), pero ya no firma.
- Se publica a los nodos el conjunto de claves ``active`` y ``overlapping`` de los cuatro
  propósitos del nodo, firmado por la clave ``key_set`` ``active``; al rotar la propia ``key_set``
  firma la **anterior** (continuidad, BR-NUC-86). Las claves ``checkpoint`` no se publican a los
  nodos, y a los clientes se publican todas, también las retiradas: nunca dejan de publicarse.
- Aviso ``key_rotation_due`` 45 días antes del vencimiento de la clave activa; rotación
  automática cuando faltan 30 días (BR-NUC-85).
"""

from __future__ import annotations

import base64
import binascii
import enum
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from vigia_contracts.canonical import canonical_sha256, canonicalize
from vigia_contracts.models.public_key_set import SignedEnvelope as SignedPublicKeySet

__all__ = [
    "AUTO_ROTATION_BEFORE",
    "KEY_LIFETIME",
    "NODE_PURPOSES",
    "OVERLAP",
    "ROTATION_NOTICE_BEFORE",
    "KeySetPublicationRecord",
    "KeySetSignerUnavailable",
    "KeyStateConflict",
    "KeyStatus",
    "KeyTransition",
    "PlatformSignedEnvelope",
    "PublicationPlan",
    "ReminderDecision",
    "SigningKeyRecord",
    "SigningPurpose",
    "active_key",
    "days_to_expiry",
    "expiry_transitions",
    "format_timestamp",
    "published_keys",
    "reminder_decision",
    "rotation_transitions",
    "to_millisecond",
    "verify_detached",
    "verify_platform_envelope",
]

KEY_LIFETIME: Final = timedelta(days=365)
"""Vigencia de una clave nueva (BR-NUC-85)."""
OVERLAP: Final = timedelta(days=30)
"""Solapamiento: la anterior sigue publicada a lo sumo 30 días tras la rotación."""
ROTATION_NOTICE_BEFORE: Final = timedelta(days=45)
"""``key_rotation_due`` se publica cuando faltan 45 días para el vencimiento."""
AUTO_ROTATION_BEFORE: Final = timedelta(days=30)
"""La tarea rota sola cuando faltan 30 días o menos."""

KEY_ID_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")
"""``PlatformKeyId``: el ``KeyId`` del contrato en minúsculas (``ledger.record_types.u02``)."""
_PUBLIC_KEY: Final = re.compile(r"[A-Za-z0-9+/]{43}=")
_SIGNATURE: Final = re.compile(r"[A-Za-z0-9+/]{86}==")
_PRIVATE_KEY_REF: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/_+=.@-]{0,511}")
"""Mismo patrón que ``identity.signing_key.private_key_ref``: un ARN o nombre, nunca material."""
_MILLISECOND: Final = timedelta(milliseconds=1)
_FIELD_PRIME: Final = 2**255 - 19
_ORDER_8_Y: Final = int.from_bytes(
    bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"), "little"
)
_SMALL_ORDER_Y: Final = frozenset({0, 1, _FIELD_PRIME - 1, _ORDER_8_Y, _FIELD_PRIME - _ORDER_8_Y})
"""``y`` de los ocho puntos de orden que divide a 8 (los mismos que rechaza U-01)."""


class SigningPurpose(enum.StrEnum):
    """``signing_purpose`` (``domain-entities.md`` §1)."""

    CATALOG = "catalog"
    GATE = "gate"
    LIVE_VIEW_TOKEN = "live_view_token"  # noqa: S105 - nombre de propósito, no una credencial
    KEY_SET = "key_set"
    CHECKPOINT = "checkpoint"


NODE_PURPOSES: Final = frozenset(
    {
        SigningPurpose.CATALOG,
        SigningPurpose.GATE,
        SigningPurpose.LIVE_VIEW_TOKEN,
        SigningPurpose.KEY_SET,
    }
)
"""Propósitos que conoce el nodo (``KeyPurpose`` del contrato): los del conjunto publicado."""


class KeyStatus(enum.StrEnum):
    """``key_status``."""

    ACTIVE = "active"
    OVERLAPPING = "overlapping"
    RETIRED = "retired"


# --- Tiempo ------------------------------------------------------------------------------------


def to_millisecond(moment: datetime) -> datetime:
    """``moment`` en UTC truncado al milisegundo, la precisión de ``Timestamp`` del contrato."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError("el instante debe llevar zona horaria")
    moment = moment.astimezone(UTC)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def format_timestamp(moment: datetime) -> str:
    """``Timestamp`` del contrato: ISO 8601 UTC con milisegundos y ``Z``."""
    return to_millisecond(moment).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- Registros ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SigningKeyRecord:
    """``SigningKey`` (§2.12): la parte pública y la **referencia** al secreto; nunca material."""

    key_id: str
    purpose: SigningPurpose
    public_key: str
    """32 bytes crudos en base64 estándar con relleno."""
    private_key_ref: str
    """ARN o nombre del secreto en el gestor de secretos."""
    valid_from: datetime
    valid_until: datetime
    status: KeyStatus
    created_at: datetime
    rotated_by: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or KEY_ID_PATTERN.fullmatch(self.key_id) is None:
            raise ValueError("key_id no válido")
        if not isinstance(self.purpose, SigningPurpose):
            raise TypeError("purpose debe ser SigningPurpose")
        if not isinstance(self.status, KeyStatus):
            raise TypeError("status debe ser KeyStatus")
        if not isinstance(self.public_key, str) or _PUBLIC_KEY.fullmatch(self.public_key) is None:
            raise ValueError("public_key no es una clave Ed25519 en base64")
        if (
            not isinstance(self.private_key_ref, str)
            or _PRIVATE_KEY_REF.fullmatch(self.private_key_ref) is None
        ):
            raise ValueError("private_key_ref no válido")
        for name in ("valid_from", "valid_until", "created_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise ValueError(f"{name} debe llevar zona horaria")
        if not self.valid_until > self.valid_from:
            raise ValueError("valid_until debe ser posterior a valid_from")
        if not isinstance(self.rotated_by, uuid.UUID):
            raise TypeError("rotated_by debe ser un UUID")

    def is_valid_at(self, moment: datetime) -> bool:
        """``valid_from ≤ moment < valid_until`` (la misma regla que ``KeySet`` del nodo)."""
        return self.valid_from <= moment < self.valid_until

    def public_key_bytes(self) -> bytes:
        return base64.b64decode(self.public_key, validate=True)

    def to_contract(self) -> dict[str, str]:
        """``PublicKey`` del contrato (solo propósitos del nodo)."""
        if self.purpose not in NODE_PURPOSES:
            raise ValueError("las claves checkpoint no se publican a los nodos")
        return {
            "key_id": self.key_id,
            "algorithm": "Ed25519",
            "public_key": self.public_key,
            "purpose": self.purpose.value,
            "valid_from": format_timestamp(self.valid_from),
            "valid_until": format_timestamp(self.valid_until),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyTransition:
    """Cambio de estado de una clave existente: las dos columnas que ``vigia_app`` actualiza.

    ``expected_status`` es el estado que la clave tenía cuando se calculó el cambio: el
    almacén solo lo aplica si la clave sigue en él (``UPDATE … WHERE key_id = :id AND
    status = :expected_status`` afecta exactamente una fila); si no, ``KeyStateConflict``. Así
    dos procesos no confirman cambios calculados desde el mismo estado ya superado.
    """

    key_id: str
    status: KeyStatus
    valid_until: datetime
    expected_status: KeyStatus


class KeyStateConflict(Exception):
    """Otra rotación cambió las claves entre la lectura y la confirmación: no se aplicó nada."""

    def __init__(self) -> None:
        super().__init__("las claves de firma cambiaron durante la rotación; vuelve a intentarlo")


@dataclass(frozen=True, slots=True, kw_only=True)
class KeySetPublicationRecord:
    """``KeySetPublication`` ⛓ (§2.12): lo que U-03 entrega en ``platform_public_keys``."""

    publication_id: uuid.UUID
    issued_at: datetime
    keys: tuple[Mapping[str, str], ...]
    """``PublicKey`` del contrato, ordenadas por ``key_id``."""
    signed_by_key_id: str
    envelope: SignedPublicKeySet

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(key["key_id"] for key in self.keys)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformSignedEnvelope:
    """Sobre de los propósitos sin sobre del contrato (``checkpoint``).

    Misma forma que ``SignedEnvelope`` de U-01: ``signature`` es la firma Ed25519 de la
    serialización canónica RFC 8785 de ``payload`` (``sign_bytes`` de U-01).
    """

    payload: Any = field(repr=False)
    payload_canonical_sha256: str
    signature: str
    key_id: str
    signed_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "payload": self.payload,
            "payload_canonical_sha256": self.payload_canonical_sha256,
            "signature": self.signature,
            "key_id": self.key_id,
            "signed_at": self.signed_at,
        }


# --- Consultas ---------------------------------------------------------------------------------


def active_key(
    keys: Iterable[SigningKeyRecord], purpose: SigningPurpose
) -> SigningKeyRecord | None:
    """La clave ``active`` de ``purpose``; si hubiera más de una, ``ValueError`` (estado roto)."""
    found = [k for k in keys if k.purpose is purpose and k.status is KeyStatus.ACTIVE]
    if len(found) > 1:
        raise ValueError(f"más de una clave activa para {purpose.value}")
    return found[0] if found else None


def _overlapping(
    keys: Iterable[SigningKeyRecord], purpose: SigningPurpose
) -> SigningKeyRecord | None:
    found = [k for k in keys if k.purpose is purpose and k.status is KeyStatus.OVERLAPPING]
    if len(found) > 1:
        raise ValueError(f"más de una clave en solapamiento para {purpose.value}")
    return found[0] if found else None


def published_keys(
    keys: Iterable[SigningKeyRecord], purpose: SigningPurpose
) -> tuple[SigningKeyRecord, ...]:
    """Claves publicadas de ``purpose``, ordenadas por ``key_id``.

    Propósitos del nodo: ``active`` y ``overlapping``. ``checkpoint``: todas, también las
    retiradas, para verificar paquetes antiguos (§9).
    """
    if purpose is SigningPurpose.CHECKPOINT:
        selected = [k for k in keys if k.purpose is purpose]
    else:
        selected = [
            k
            for k in keys
            if k.purpose is purpose and k.status in (KeyStatus.ACTIVE, KeyStatus.OVERLAPPING)
        ]
    return tuple(sorted(selected, key=lambda k: k.key_id))


def node_key_set(keys: Iterable[SigningKeyRecord]) -> tuple[SigningKeyRecord, ...]:
    """Las claves del conjunto para nodos: ``active`` y ``overlapping`` de los cuatro propósitos."""
    listed = list(keys)
    selected = [k for purpose in NODE_PURPOSES for k in published_keys(listed, purpose)]
    return tuple(sorted(selected, key=lambda k: k.key_id))


# --- Transiciones ------------------------------------------------------------------------------


def rotation_transitions(
    keys: Iterable[SigningKeyRecord], purpose: SigningPurpose, now: datetime
) -> tuple[KeyTransition, ...]:
    """Cambios de las claves existentes de ``purpose`` al rotar en ``now``.

    La ``overlapping`` (si la hay) se retira; la ``active`` (si la hay) pasa a ``overlapping`` con
    ``valid_until`` acotado a ``now + 30 días``. Ningún ``valid_until`` queda en o antes de su
    ``valid_from`` (restricción ``signing_key_validity``).
    """
    listed = list(keys)
    transitions: list[KeyTransition] = []
    overlapping = _overlapping(listed, purpose)
    if overlapping is not None:
        until = max(min(overlapping.valid_until, now), overlapping.valid_from + _MILLISECOND)
        transitions.append(
            KeyTransition(
                key_id=overlapping.key_id,
                status=KeyStatus.RETIRED,
                valid_until=until,
                expected_status=KeyStatus.OVERLAPPING,
            )
        )
    current = active_key(listed, purpose)
    if current is not None:
        until = max(min(current.valid_until, now + OVERLAP), current.valid_from + _MILLISECOND)
        transitions.append(
            KeyTransition(
                key_id=current.key_id,
                status=KeyStatus.OVERLAPPING,
                valid_until=until,
                expected_status=KeyStatus.ACTIVE,
            )
        )
    return tuple(transitions)


def expiry_transitions(
    keys: Iterable[SigningKeyRecord], now: datetime
) -> tuple[KeyTransition, ...]:
    """``overlapping`` → ``retired`` para toda clave cuyo ``valid_until`` ya pasó en ``now``."""
    return tuple(
        KeyTransition(
            key_id=k.key_id,
            status=KeyStatus.RETIRED,
            valid_until=k.valid_until,
            expected_status=KeyStatus.OVERLAPPING,
        )
        for k in sorted(keys, key=lambda k: k.key_id)
        if k.status is KeyStatus.OVERLAPPING and k.valid_until <= now
    )


def apply_transitions(
    keys: Mapping[str, SigningKeyRecord],
    transitions: Sequence[KeyTransition],
    new_key: SigningKeyRecord | None = None,
) -> dict[str, SigningKeyRecord]:
    """El estado tras aplicar ``transitions`` y dar de alta ``new_key``."""
    updated = dict(keys)
    for transition in transitions:
        current = updated[transition.key_id]
        updated[transition.key_id] = replace(
            current, status=transition.status, valid_until=transition.valid_until
        )
    if new_key is not None:
        if new_key.key_id in updated:
            raise ValueError("un key_id nunca se reutiliza")
        updated[new_key.key_id] = new_key
    check_invariants(updated.values())
    return updated


def check_invariants(keys: Iterable[SigningKeyRecord]) -> None:
    """A lo sumo una ``active`` y una ``overlapping`` por propósito (``ValueError`` si no)."""
    listed = list(keys)
    for purpose in SigningPurpose:
        active_key(listed, purpose)
        _overlapping(listed, purpose)


class KeySetSignerUnavailable(ValueError):
    """La clave ``key_set`` activa ya no está vigente: no se puede publicar un conjunto válido."""


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    """Quién firma el conjunto que sigue a una rotación.

    ``signer_key_id`` es una clave existente, o ``None`` para la clave ``key_set`` nueva: el alta
    (no había ninguna) o la continuidad ya rota (la anterior venció; los nodos se vuelven a dar de
    alta, BR-NUC-86).
    """

    signer_key_id: str | None


def publication_plan(
    keys_before: Iterable[SigningKeyRecord], purpose: SigningPurpose, now: datetime
) -> PublicationPlan | None:
    """Plan de publicación al rotar ``purpose`` en ``now``; ``None`` si no se publica.

    Toda publicación la firma una ``key_set`` vigente en ``now`` (PR-NUC-35): la ``active``; al
    rotar la propia ``key_set``, la anterior (continuidad, BR-NUC-86). ``checkpoint`` no se
    publica a los nodos. Sin ninguna ``key_set`` todavía (alta) tampoco: la primera publicación
    sale al crear la ``key_set`` y ya incluye las demás claves. Si la ``key_set`` activa venció,
    rotar otro propósito lanza ``KeySetSignerUnavailable`` (hay que rotar antes la ``key_set``).
    """
    if purpose not in NODE_PURPOSES:
        return None
    signer = active_key(keys_before, SigningPurpose.KEY_SET)
    if purpose is SigningPurpose.KEY_SET:
        continuity = signer is not None and signer.is_valid_at(now)
        return PublicationPlan(signer.key_id if continuity and signer is not None else None)
    if signer is None:
        return None
    if not signer.is_valid_at(now):
        raise KeySetSignerUnavailable("la clave key_set activa no está vigente")
    return PublicationPlan(signer.key_id)


# --- Aviso y rotación automática ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReminderDecision:
    """Lo que la tarea ``key_rotation_reminder`` debe hacer en ``now``."""

    notices: tuple[SigningKeyRecord, ...]
    """Claves activas que acaban de entrar en los 45 días: un ``key_rotation_due`` por clave."""
    rotations: tuple[SigningPurpose, ...]
    """Propósitos cuya clave activa vence en 30 días o menos: se rotan, ``key_set`` primero
    (firma las publicaciones de las demás rotaciones)."""


def reminder_decision(
    keys: Iterable[SigningKeyRecord], now: datetime, *, period: timedelta = timedelta(days=1)
) -> ReminderDecision:
    """Aviso cuando el umbral de 45 días cayó en ``(now - period, now]``; rotación a 30 días.

    ``period`` es el de la tarea (diaria): cada clave recibe el aviso en una sola corrida.
    """
    if period <= timedelta(0):
        raise ValueError("period debe ser positivo")
    listed = list(keys)
    notices: list[SigningKeyRecord] = []
    rotations: list[SigningPurpose] = []
    ordered = sorted(SigningPurpose, key=lambda p: p is not SigningPurpose.KEY_SET)
    for purpose in ordered:
        key = active_key(listed, purpose)
        if key is None:
            continue
        remaining = key.valid_until - now
        if remaining <= AUTO_ROTATION_BEFORE:
            rotations.append(purpose)
        elif ROTATION_NOTICE_BEFORE - period < remaining <= ROTATION_NOTICE_BEFORE:
            notices.append(key)
    return ReminderDecision(tuple(notices), tuple(rotations))


def days_to_expiry(keys: Iterable[SigningKeyRecord], now: datetime) -> dict[SigningPurpose, int]:
    """Días enteros (hacia abajo; negativos si ya venció) hasta el vencimiento de cada activa."""
    listed = list(keys)
    result: dict[SigningPurpose, int] = {}
    for purpose in SigningPurpose:
        key = active_key(listed, purpose)
        if key is not None:
            result[purpose] = (key.valid_until - now) // timedelta(days=1)
    return result


# --- Verificación de los propósitos de la plataforma -------------------------------------------


def _public_key(public_key: object) -> Ed25519PublicKey | None:
    """La clave si es aceptable: 32 bytes en base64 canónico, ``y`` canónico y punto que no es de
    orden pequeño (con uno de esos, una firma trivial verifica cualquier mensaje). Es la misma
    regla con la que U-01 fija claves (``KeySet``)."""
    if not isinstance(public_key, str) or _PUBLIC_KEY.fullmatch(public_key) is None:
        return None
    try:
        raw = base64.b64decode(public_key, validate=True)
    except (binascii.Error, ValueError):
        return None
    if base64.b64encode(raw).decode("ascii") != public_key:
        return None
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    if y >= _FIELD_PRIME or y in _SMALL_ORDER_Y:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        return None


def verify_detached(public_key: str, message: bytes, signature: str) -> bool:
    """Firma Ed25519 en base64 de ``message`` con ``public_key``; ``False`` ante cualquier fallo."""
    key = _public_key(public_key)
    if key is None or not isinstance(signature, str) or _SIGNATURE.fullmatch(signature) is None:
        return False
    try:
        key.verify(base64.b64decode(signature, validate=True), message)
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True


def verify_platform_envelope(
    envelope: object,
    keys: Iterable[SigningKeyRecord],
    purpose: SigningPurpose,
) -> bool:
    """Verifica un sobre ``checkpoint`` contra las claves publicadas de ``purpose``.

    Falla cerrado: ``key_id`` desconocido o de otro propósito, resumen que no corresponde a la
    carga o firma inválida dan ``False``; nunca una excepción.
    """
    document = envelope.to_json() if isinstance(envelope, PlatformSignedEnvelope) else envelope
    if not isinstance(document, Mapping):
        return False
    key_id = document.get("key_id")
    signature = document.get("signature")
    digest = document.get("payload_canonical_sha256")
    if not isinstance(key_id, str) or not isinstance(signature, str) or not isinstance(digest, str):
        return False
    matching = [k for k in keys if k.key_id == key_id and k.purpose is purpose]
    if len(matching) != 1:
        return False
    try:
        message = canonicalize(document.get("payload"))
        expected = canonical_sha256(document.get("payload"))
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False
    if expected != digest:
        return False
    return verify_detached(matching[0].public_key, message, signature)
