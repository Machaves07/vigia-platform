"""Política de contraseñas y hash adaptativo (LC-NUC-01; BR-NUC-20, NFR-NUC-26, PR-NUC-08).

Pieza del módulo de autenticación que implementa el puerto ``IdentityProvider`` (BR-NUC-28):
el inicio de sesión (LC-NUC-03), el restablecimiento y la inscripción llaman a este módulo; la
autorización nunca lo ve. Módulo crítico aislado (NFR-NUC-25): no importa FastAPI, SQLAlchemy ni
httpx. La consulta de filtradas llega por el puerto ``BreachChecker``, que implementa
``identity.adapters.hibp`` con el respaldo local.

- ``check_policy(password, email) -> PolicyResult``: de 8 a 128 caracteres, distinta del correo
  y no filtrada (BR-NUC-20). La longitud se cuenta en puntos de código Unicode. La igualdad con
  el correo se decide sobre la forma comparable (NFKC, ``casefold`` y sin espacios ni caracteres
  invisibles): ``Ana@Planta.com`` o ``ana@planta.com`` con un espacio de ancho cero cuentan
  como el correo. La consulta de filtradas solo se hace si la longitud y el correo ya pasaron.
- ``hash(password) -> PasswordHash`` y ``verify(password, hash) -> (ok, needs_rehash)``: Argon2id
  con los parámetros de ``CURRENT_VERSION`` (64 MB, 3 iteraciones, paralelismo 2
  ``[objetivo propio]``). El hash codificado (PHC) lleva sus parámetros, y ``PasswordHash``
  lleva además la versión, que se guarda en ``PasswordCredential.algorithm_version``.
  ``needs_rehash`` es ``True`` solo si la contraseña es correcta y el hash no tiene exactamente
  los parámetros vigentes (otra versión, otro tipo de Argon2 u otra longitud de hash o de sal):
  el inicio de sesión lo recalcula entonces con los vigentes (``business-logic-model.md`` §1,
  paso 4). Un hash ilegible nunca verifica y nunca lanza.
- El trabajo de Argon2id corre en el pool de CPU acotado (PAT-NUC-REN-05), nunca en el bucle de
  eventos: ``PasswordService`` recibe el ``CpuPool``.

La contraseña en claro no se registra, no se persiste y no aparece en ninguna excepción ni
representación de los valores que devuelve este módulo.
"""

from __future__ import annotations

import enum
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, NamedTuple, Protocol

from argon2 import PasswordHasher, Type
from argon2 import exceptions as argon2_exceptions

from vigia_platform.shared.cpu_pool import CpuPool

__all__ = [
    "CURRENT_VERSION",
    "HASH_VERSIONS",
    "PASSWORD_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "Argon2Parameters",
    "BreachCheck",
    "BreachChecker",
    "BreachSource",
    "PasswordHash",
    "PasswordService",
    "PolicyResult",
    "PolicyViolation",
    "VerifyResult",
    "comparable_form",
    "hash_password",
    "length_and_email_violations",
    "password_bytes",
    "verify_password",
]

PASSWORD_MIN_LENGTH: Final = 8
PASSWORD_MAX_LENGTH: Final = 128
"""Límites de BR-NUC-20, en puntos de código Unicode, ambos incluidos."""


@dataclass(frozen=True, slots=True)
class Argon2Parameters:
    """Parámetros de una versión del hash (``memory_kib`` en KiB, como los codifica PHC)."""

    memory_kib: int
    iterations: int
    parallelism: int
    hash_len: int = 32
    salt_len: int = 16

    def hasher(self) -> PasswordHasher:
        return PasswordHasher(
            time_cost=self.iterations,
            memory_cost=self.memory_kib,
            parallelism=self.parallelism,
            hash_len=self.hash_len,
            salt_len=self.salt_len,
            type=Type.ID,
        )


HASH_VERSIONS: Final[Mapping[int, Argon2Parameters]] = MappingProxyType(
    {1: Argon2Parameters(memory_kib=64 * 1024, iterations=3, parallelism=2)}
)
"""Parámetros por versión (NFR-NUC-26). Subir los parámetros es añadir una versión nueva y
apuntar ``CURRENT_VERSION`` a ella; las versiones anteriores no se editan ni se borran."""

CURRENT_VERSION: Final = 1
"""Versión con la que se calculan los hashes nuevos."""

_CURRENT_HASHER: Final = HASH_VERSIONS[CURRENT_VERSION].hasher()
_ARGON2ID_PREFIX: Final = "$argon2id$"


@dataclass(frozen=True, slots=True)
class PasswordHash:
    """Hash Argon2id codificado (PHC) y la versión de parámetros con que se calculó."""

    encoded: str = field(repr=False)
    algorithm_version: int


class VerifyResult(NamedTuple):
    """Resultado de ``verify``: se desempaqueta como ``ok, needs_rehash``."""

    ok: bool
    needs_rehash: bool


def hash_password(password: str, *, version: int = CURRENT_VERSION) -> PasswordHash:
    """Argon2id de ``password`` con los parámetros de ``version`` (síncrono: en el pool de CPU).

    Raises:
        TypeError: ``password`` no es ``str``.
        KeyError: ``version`` no existe en ``HASH_VERSIONS``.
    """
    if not isinstance(password, str):
        raise TypeError("la contraseña debe ser str")
    hasher = _CURRENT_HASHER if version == CURRENT_VERSION else HASH_VERSIONS[version].hasher()
    return PasswordHash(encoded=hasher.hash(password_bytes(password)), algorithm_version=version)


def password_bytes(password: str) -> bytes:
    """UTF-8 de ``password``; un sustituto suelto (``"\\ud800"`` llega por JSON) se codifica
    con ``surrogatepass`` en lugar de lanzar, igual en el hash, la verificación y la consulta
    de filtradas."""
    return password.encode("utf-8", "surrogatepass")


def verify_password(password: str, encoded: str) -> VerifyResult:
    """Comprueba ``password`` contra el hash ``encoded`` (síncrono: en el pool de CPU).

    Un hash que no es Argon2id, ilegible o de otro tipo, o una contraseña que no es ``str``,
    devuelve ``(False, False)`` sin lanzar ni revelar el motivo.
    """
    if (
        not isinstance(password, str)
        or not isinstance(encoded, str)
        or not encoded.startswith(_ARGON2ID_PREFIX)
    ):
        return VerifyResult(ok=False, needs_rehash=False)
    try:
        _CURRENT_HASHER.verify(encoded, password_bytes(password))
    except (argon2_exceptions.VerificationError, argon2_exceptions.InvalidHashError, ValueError):
        # ``ValueError`` cubre también un hash con base64 o parámetros inválidos.
        return VerifyResult(ok=False, needs_rehash=False)
    return VerifyResult(ok=True, needs_rehash=_needs_rehash(encoded))


def _needs_rehash(encoded: str) -> bool:
    """``True`` si ``encoded`` no tiene exactamente los parámetros de ``CURRENT_VERSION``.

    ``check_needs_rehash`` compara todos los parámetros que codifica el hash: tipo, versión de
    Argon2, memoria, iteraciones, paralelismo y longitudes del hash y de la sal.
    """
    try:
        return _CURRENT_HASHER.check_needs_rehash(encoded)
    except (argon2_exceptions.InvalidHashError, ValueError):
        return True


class PolicyViolation(enum.StrEnum):
    """Motivos de rechazo de una contraseña nueva (BR-NUC-20)."""

    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    EQUALS_EMAIL = "equals_email"
    BREACHED = "breached"


class BreachSource(enum.StrEnum):
    """De dónde salió la respuesta de filtradas."""

    LOCAL_LIST = "local_list"
    """La contraseña está en el respaldo local: se rechaza sin consultar el servicio."""
    REMOTE = "remote"
    """Respondió el servicio por rango de anonimato k."""
    LOCAL_FALLBACK = "local_fallback"
    """El servicio no respondió a tiempo, falló o su circuito está abierto: decide la lista
    local (PAT-NUC-RES-03). Se cuenta en ``hibp_fallback_used``."""


@dataclass(frozen=True, slots=True)
class BreachCheck:
    breached: bool
    source: BreachSource


class BreachChecker(Protocol):
    """Puerto de la verificación de filtradas (``identity.adapters.hibp``).

    Nunca lanza por una dependencia caída: responde con el respaldo local
    (``BreachSource.LOCAL_FALLBACK``). Recibe la contraseña y no la conserva.
    """

    async def check(self, password: str) -> BreachCheck: ...


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Resultado de ``check_policy``: aceptada si no hay ninguna violación."""

    violations: tuple[PolicyViolation, ...]
    breach_source: BreachSource | None
    """Fuente de la consulta de filtradas; ``None`` si no se consultó (longitud o correo)."""

    @property
    def ok(self) -> bool:
        return not self.violations


_INVISIBLE_CATEGORIES: Final = frozenset({"Cf", "Cc", "Zs", "Zl", "Zp"})


def comparable_form(text: str) -> str:
    """Forma para comparar contraseña y correo: NFKC, ``casefold`` y sin espacios ni invisibles.

    Quita los caracteres de formato (``Cf``: ancho cero, marcas de dirección), de control y
    todo separador Unicode, y vuelve a normalizar tras ``casefold``.
    """
    folded = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())
    return "".join(
        char
        for char in folded
        if not char.isspace() and unicodedata.category(char) not in _INVISIBLE_CATEGORIES
    )


def length_and_email_violations(password: str, email: str) -> tuple[PolicyViolation, ...]:
    """Violaciones que no necesitan la consulta de filtradas (longitud e igualdad con el correo).

    Raises:
        TypeError: ``password`` o ``email`` no son ``str``.
    """
    if not isinstance(password, str) or not isinstance(email, str):
        raise TypeError("la contraseña y el correo deben ser str")
    violations: list[PolicyViolation] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        violations.append(PolicyViolation.TOO_SHORT)
    elif len(password) > PASSWORD_MAX_LENGTH:
        violations.append(PolicyViolation.TOO_LONG)
    comparable_email = comparable_form(email)
    if comparable_email and comparable_form(password) == comparable_email:
        violations.append(PolicyViolation.EQUALS_EMAIL)
    return tuple(violations)


class PasswordService:
    """Política, hash y verificación con Argon2id en el pool de CPU (LC-NUC-01)."""

    def __init__(self, breach_checker: BreachChecker, cpu_pool: CpuPool) -> None:
        self._breach_checker = breach_checker
        self._cpu_pool = cpu_pool

    async def check_policy(self, password: str, email: str) -> PolicyResult:
        """Aplica BR-NUC-20 a una contraseña nueva (alta, cambio o restablecimiento)."""
        violations = length_and_email_violations(password, email)
        if violations:
            return PolicyResult(violations=violations, breach_source=None)
        breach = await self._breach_checker.check(password)
        if breach.breached:
            return PolicyResult(violations=(PolicyViolation.BREACHED,), breach_source=breach.source)
        return PolicyResult(violations=(), breach_source=breach.source)

    async def hash(self, password: str) -> PasswordHash:
        """Hash Argon2id con los parámetros vigentes, calculado en el pool de CPU."""
        return await self._cpu_pool.run(hash_password, password)

    async def verify(self, password: str, encoded: str) -> VerifyResult:
        """``(ok, needs_rehash)`` de ``password`` contra ``encoded``, en el pool de CPU."""
        return await self._cpu_pool.run(verify_password, password, encoded)
