"""Contraseñas de los roles de base que crea la migración ``nuc_0001`` (NFR-NUC-20, PAT-NUC-SEG-05).

Las contraseñas llegan de los secretos ``db/app`` y ``db/migrate`` (``shared.migration_credentials``
las resuelve). Al servidor nunca llega la contraseña en claro: se envía el **verificador
SCRAM-SHA-256** calculado aquí (el mismo formato que guarda PostgreSQL en ``pg_authid``), de modo
que ni los registros de sentencias ni ``pg_stat_statements`` pueden contenerla.

Solo se admiten contraseñas de ASCII imprimible (``0x20``-``0x7E``), de 16 a 1024 caracteres:
para ellas la normalización SASLprep que aplica el cliente al autenticarse es la identidad, así
que el verificador calculado sin normalizar es exactamente el que PostgreSQL calcularía. Los
generadores de Secrets Manager producen ese alfabeto. Ningún mensaje de error contiene el valor.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from typing import Final

__all__ = [
    "RolePasswordError",
    "role_password_verifier",
    "scram_sha256_verifier",
    "validate_role_password",
]

MIN_PASSWORD_LENGTH: Final = 16
MAX_PASSWORD_LENGTH: Final = 1024
SCRAM_ITERATIONS: Final = 4096
"""Las de PostgreSQL por defecto (``scram_iterations``)."""
SCRAM_SALT_BYTES: Final = 16

_PRINTABLE_ASCII: Final = re.compile(r"[\x20-\x7e]+")


class RolePasswordError(ValueError):
    """Contraseña de rol ausente o fuera de la política; el mensaje nunca incluye el valor."""


def validate_role_password(password: str) -> None:
    """Lanza ``RolePasswordError`` si ``password`` no cumple la política del módulo."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise RolePasswordError(
            f"la contraseña debe tener entre {MIN_PASSWORD_LENGTH} y {MAX_PASSWORD_LENGTH} "
            "caracteres"
        )
    if _PRINTABLE_ASCII.fullmatch(password) is None:
        raise RolePasswordError("la contraseña solo admite ASCII imprimible (0x20 a 0x7E)")


def scram_sha256_verifier(password: str, *, salt: bytes, iterations: int = SCRAM_ITERATIONS) -> str:
    """Verificador ``SCRAM-SHA-256$<iter>:<sal>$<StoredKey>:<ServerKey>`` (RFC 5802, 7677)."""
    validate_role_password(password)
    if len(salt) < SCRAM_SALT_BYTES or iterations < SCRAM_ITERATIONS:
        raise ValueError("sal de al menos 16 bytes e iteraciones de al menos 4096")
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, iterations)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")

    def b64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def role_password_verifier(role: str, passwords: Mapping[str, str]) -> str:
    """Verificador SCRAM, con sal aleatoria, de la contraseña de ``role`` en ``passwords``."""
    value = passwords.get(role)
    if not value:
        raise RolePasswordError(
            f"falta la contraseña de {role}: la migración que crea los roles la recibe de los "
            "secretos db/app y db/migrate (ver vigia_platform.shared.migration_credentials)"
        )
    try:
        return scram_sha256_verifier(value, salt=secrets.token_bytes(SCRAM_SALT_BYTES))
    except RolePasswordError as error:
        raise RolePasswordError(f"contraseña de {role}: {error}") from None
