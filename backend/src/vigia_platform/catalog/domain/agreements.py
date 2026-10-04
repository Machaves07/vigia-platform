"""Política de firmantes, acuerdo de uso y confirmaciones (DE §2.7 a §2.9; LC-GOB-04).

Sin acuerdo firmado no hay registro (P9). Este módulo es puro: las guardas de BR-GOB-25 a 32 como
funciones sin base ni reloj, que la aplicación evalúa con lo que leyó.

- **Política de firmantes** (BR-GOB-25): ``minimum`` ≥ 3 (si no, ``fewer_than_three``) y
  ``copasst`` en ``required_roles`` (si no, ``workers_representation_missing``); ``workers_role``
  es la constante ``copasst``.
- **Acuerdo** (BR-GOB-26, 31): cada rol firmante en la política de la planta (una planta sin
  política o un rol ajeno, ``signatory_role_not_in_policy``); cada usuario con ese rol sobre la
  zona (``signatory_user_role_mismatch``); un firmante ``copasst``
  (``workers_representation_missing``); al menos ``minimum`` firmantes (``fewer_than_three``). Un
  usuario firma una sola vez por acuerdo (el registro ``use_agreement_signed`` lo exige).
- **Confirmación** (BR-GOB-26, 27): el firmante esperado confirma con el rol con el que se le
  espera; ``copasst`` desde la transparencia (``transparency``), el resto desde la gestión
  (``management``).
- **Aprobación** (BR-GOB-29): ``first_missing`` devuelve la primera guarda que falta en el orden
  montaje, acta, firmas y política de planta.

Estados (BL §3.2): ``pending_signatures → approved → (superseded | revoked)``, solo hacia adelante;
cada uno se materializa con su cierre de nulo a valor (``approved_at``, ``superseded_at``,
``revoked_at``). El acuerdo no caduca (BR-GOB-32).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from vigia_platform.catalog.domain.documents import DocumentRef
from vigia_platform.catalog.domain.enums import AgreementStatus, ConfirmationOrigin
from vigia_platform.shared.context import Role
from vigia_platform.shared.signing.keys import format_timestamp

__all__ = [
    "MAX_SIGNATORIES",
    "MIN_SIGNATORIES",
    "WORKERS_ROLE",
    "AgreementConfirmation",
    "AgreementRuleViolated",
    "AgreementViolation",
    "ApprovalFacts",
    "Signatory",
    "SignatoryPolicy",
    "UseAgreement",
    "check_policy",
    "check_signatories",
    "confirmation_origin",
    "first_missing",
    "signatory_from_json",
    "signatures_complete",
    "use_agreement_signed_content",
]

MIN_SIGNATORIES: Final = 3
"""Mínimo de firmantes del acuerdo (RF-PLA-09, H-50): no es configurable."""
MAX_SIGNATORIES: Final = 32
"""Tope de firmantes de un acuerdo `[estimación propia]` (``record_types.MAX_SIGNATORIES``)."""
WORKERS_ROLE: Final = Role.COPASST
"""La representación de los trabajadores: constante, no configurable (DE §2.7)."""


class AgreementViolation(enum.StrEnum):
    """Por qué el dominio rechaza una política, un acuerdo, una confirmación o una aprobación.

    Los valores con nombre son el ``catalog_<valor>`` de ``CatalogDetailCode``.
    """

    FEWER_THAN_THREE = "fewer_than_three"
    WORKERS_REPRESENTATION_MISSING = "workers_representation_missing"
    SIGNATORY_ROLE_NOT_IN_POLICY = "signatory_role_not_in_policy"
    SIGNATORY_USER_ROLE_MISMATCH = "signatory_user_role_mismatch"
    SIGNATORY_NOT_EXPECTED = "signatory_not_expected"
    AGREEMENT_REUSED_FROM_OTHER_ZONE = "agreement_reused_from_other_zone"
    MOUNTING_GATE_PENDING = "mounting_gate_pending"
    COMMISSIONING_RECORD_MISSING = "commissioning_record_missing"
    SIGNATURES_INCOMPLETE = "signatures_incomplete"
    PLANT_POLICY_MISSING = "plant_policy_missing"


class AgreementRuleViolated(Exception):
    def __init__(self, violation: AgreementViolation) -> None:
        super().__init__(f"acuerdo de uso rechazado: {violation.value}")
        self.violation = AgreementViolation(violation)


# --- Política de firmantes (§2.7) ---------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SignatoryPolicy:
    """§2.7 ``PlantSignatoryPolicy`` 🔒: una fila por planta."""

    organization_id: uuid.UUID
    plant_id: uuid.UUID
    required_roles: tuple[Role, ...]
    minimum: int
    updated_by: uuid.UUID
    updated_at: datetime

    @property
    def workers_role(self) -> Role:
        return WORKERS_ROLE


def check_policy(required_roles: Sequence[Role], minimum: int) -> tuple[Role, ...]:
    """Los roles de una política válida, sin repetir y en su orden; si no, la violación.

    ``minimum`` menor que 3 es ``fewer_than_three`` (se comprueba primero); sin ``copasst``,
    ``workers_representation_missing`` (BR-GOB-25). ``ValueError`` si la forma es incoherente
    (roles repetidos o un mínimo mayor que el tope), que la aplicación traduce a
    ``invalid_request``.
    """
    roles = tuple(Role(role) for role in required_roles)
    if type(minimum) is not int:
        raise ValueError("minimum debe ser un entero")
    if minimum < MIN_SIGNATORIES:
        raise AgreementRuleViolated(AgreementViolation.FEWER_THAN_THREE)
    if WORKERS_ROLE not in roles:
        raise AgreementRuleViolated(AgreementViolation.WORKERS_REPRESENTATION_MISSING)
    if len(set(roles)) != len(roles):
        raise ValueError("required_roles no admite roles repetidos")
    if minimum > MAX_SIGNATORIES:
        raise ValueError("minimum supera el tope de firmantes de un acuerdo")
    return roles


# --- Acuerdo (§2.8) y confirmaciones (§2.9) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signatory:
    """Un firmante esperado: ``{role, user_id, display_name}`` (el nombre, de identidad)."""

    role: Role
    user_id: uuid.UUID
    display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", Role(self.role))
        if type(self.user_id) is not uuid.UUID:
            raise TypeError("user_id debe ser uuid.UUID")

    def as_json(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role.value, "user_id": str(self.user_id)}
        if self.display_name is not None:
            value["display_name"] = self.display_name
        return value


@dataclass(frozen=True, slots=True, kw_only=True)
class AgreementConfirmation:
    """§2.9 ``AgreementConfirmation`` ⛓: una por ``(agreement_id, user_id)``."""

    agreement_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    role_in_use: Role
    confirmed_at: datetime
    origin: ConfirmationOrigin

    def record_json(self) -> dict[str, Any]:
        """La confirmación en el contenido de ``use_agreement_signed``."""
        return {
            "user_id": str(self.user_id),
            "role_in_use": Role(self.role_in_use).value,
            "confirmed_at": format_timestamp(self.confirmed_at),
            "origin": ConfirmationOrigin(self.origin).value,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class UseAgreement:
    """§2.8 ``UseAgreement`` ⛓: el acuerdo que abre la compuerta de uso de **una** zona."""

    agreement_id: uuid.UUID
    organization_id: uuid.UUID
    plant_id: uuid.UUID
    zone_id: uuid.UUID
    status: AgreementStatus
    signatories: tuple[Signatory, ...]
    document_ref: DocumentRef | None
    replaces_agreement_id: uuid.UUID | None
    created_by: uuid.UUID
    created_at: datetime
    approved_at: datetime | None = None
    approved_by: uuid.UUID | None = None
    ledger_record_id: uuid.UUID | None = None
    superseded_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AgreementStatus(self.status))

    def expected(self, user_id: uuid.UUID) -> Signatory | None:
        """El firmante esperado ``user_id`` (a lo sumo uno), o ``None``."""
        for signatory in self.signatories:
            if signatory.user_id == user_id:
                return signatory
        return None


def check_signatories(
    signatories: Sequence[Signatory],
    policy: SignatoryPolicy | None,
    holders: Collection[tuple[uuid.UUID, Role]],
) -> tuple[Signatory, ...]:
    """Los firmantes esperados de un acuerdo nuevo, o la primera guarda que falla.

    Orden (TASK-212): rol en la política (sin política, también ``signatory_role_not_in_policy``),
    usuario con ese rol sobre la zona (``holders``: los pares ``(user_id, role)`` que devolvió
    ``IdentityQueryPort.users_by_role_and_scope``), un ``copasst`` y al menos ``minimum``.
    ``ValueError`` si un usuario aparece dos veces o se pasa del tope (``invalid_request``).
    """
    signed = tuple(signatories)
    if len({signatory.user_id for signatory in signed}) != len(signed):
        raise ValueError("cada usuario firma una sola vez el acuerdo")
    if len(signed) > MAX_SIGNATORIES:
        raise ValueError("demasiados firmantes")
    allowed = frozenset(() if policy is None else policy.required_roles)
    if policy is None or any(signatory.role not in allowed for signatory in signed):
        raise AgreementRuleViolated(AgreementViolation.SIGNATORY_ROLE_NOT_IN_POLICY)
    held = frozenset(holders)
    if any((signatory.user_id, signatory.role) not in held for signatory in signed):
        raise AgreementRuleViolated(AgreementViolation.SIGNATORY_USER_ROLE_MISMATCH)
    if not any(signatory.role is WORKERS_ROLE for signatory in signed):
        raise AgreementRuleViolated(AgreementViolation.WORKERS_REPRESENTATION_MISSING)
    if len(signed) < max(policy.minimum, MIN_SIGNATORIES):
        raise AgreementRuleViolated(AgreementViolation.FEWER_THAN_THREE)
    return signed


def confirmation_origin(role: Role) -> ConfirmationOrigin:
    """``copasst`` confirma desde la transparencia; el resto, desde la gestión (BR-GOB-27)."""
    return (
        ConfirmationOrigin.TRANSPARENCY
        if Role(role) is WORKERS_ROLE
        else ConfirmationOrigin.MANAGEMENT
    )


def signatures_complete(
    signatories: Iterable[Signatory], confirmations: Iterable[AgreementConfirmation]
) -> bool:
    """¿Confirmó cada firmante esperado con el rol con el que se le espera?"""
    confirmed = {(c.user_id, Role(c.role_in_use)) for c in confirmations}
    return all((s.user_id, s.role) in confirmed for s in signatories)


# --- Aprobación (BR-GOB-29) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalFacts:
    """Lo que la aprobación lee de la zona, en el orden de BR-GOB-29."""

    mounting_approved: bool
    commissioning_record_closed: bool
    signatures_complete: bool
    plant_policy_loaded: bool


_GUARDS: Final[tuple[tuple[str, AgreementViolation], ...]] = (
    ("mounting_approved", AgreementViolation.MOUNTING_GATE_PENDING),
    ("commissioning_record_closed", AgreementViolation.COMMISSIONING_RECORD_MISSING),
    ("signatures_complete", AgreementViolation.SIGNATURES_INCOMPLETE),
    ("plant_policy_loaded", AgreementViolation.PLANT_POLICY_MISSING),
)


def first_missing(facts: ApprovalFacts) -> AgreementViolation | None:
    """La primera guarda que falta, en el orden de BR-GOB-29, o ``None`` si están las cuatro.

    La política de planta es la última: es el único punto donde bloquea (BR-GOB-22).
    """
    for name, violation in _GUARDS:
        if getattr(facts, name) is not True:
            return violation
    return None


def use_agreement_signed_content(
    agreement: UseAgreement, confirmations: Sequence[AgreementConfirmation]
) -> dict[str, Any]:
    """Contenido de ``use_agreement_signed`` (DE §5): firmantes y confirmaciones, sin nombres."""
    content: dict[str, Any] = {
        "agreement_id": str(agreement.agreement_id),
        "zone_id": str(agreement.zone_id),
        "signatories": [
            {"role": s.role.value, "user_id": str(s.user_id)} for s in agreement.signatories
        ],
        "confirmations": [
            c.record_json()
            for c in sorted(confirmations, key=lambda c: (c.confirmed_at, c.user_id))
        ],
    }
    if agreement.document_ref is not None:
        content["document_ref"] = agreement.document_ref.to_json()
    if agreement.replaces_agreement_id is not None:
        content["replaces_agreement_id"] = str(agreement.replaces_agreement_id)
    return content


def signatory_from_json(value: Mapping[str, Any]) -> Signatory:
    """Un firmante guardado (``signatories`` de la fila)."""
    name = value.get("display_name")
    return Signatory(
        Role(value["role"]),
        uuid.UUID(str(value["user_id"])),
        name if isinstance(name, str) else None,
    )
