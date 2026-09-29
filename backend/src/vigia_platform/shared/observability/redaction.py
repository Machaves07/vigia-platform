"""Política única de redacción de registros, métricas y trazas (PAT-NUC-SEG-09, MAN-05).

Dos barreras, compartidas por ``logging``, ``metrics`` y ``tracing``:

- ``redact_text`` sustituye por ``[redactado]`` todo lo que tenga forma de bloque PEM, enlace
  (``esquema://…`` o ``www.…``), correo (cualquier trozo sin espacios con ``@``) o token (20 o más
  caracteres seguidos del alfabeto ``A-Z a-z 0-9 _ - + / =``). Se aplica a los mensajes de
  registro y a los nombres de tramos y eventos, que el código fija como constantes.
- ``AttributePolicy`` decide qué atributos se emiten: solo **identificadores** (UUID canónico),
  **enumeraciones** (listas cerradas o miembros de ``enum.Enum``), enteros acotados y booleanos
  declarados, bajo una lista blanca de nombres (NFR-NUC-17, NFR-NUC-41). Un nombre fuera de la
  lista se descarta; un valor fuera de su lista se sustituye (``other`` en métricas y trazas,
  ``[redactado]`` en registros). El texto libre, las contraseñas, los secretos, los tokens y los
  correos nunca son identificadores ni valores de una lista cerrada, así que nunca salen.

Las listas cerradas que dependen de otros módulos (rutas, tipos de registro, consumidores,
propósitos de clave…) se amplían al arrancar con ``AttributePolicy.register``.
"""

from __future__ import annotations

import enum
import math
import re
import uuid
from collections.abc import Collection, Iterable, Mapping
from typing import Final, TypeGuard

__all__ = [
    "DEFAULT_POLICY",
    "OTHER",
    "REDACTED",
    "AttributePolicy",
    "AttributeValue",
    "is_finite_non_negative",
    "known_exception_type",
    "redact_text",
]

REDACTED: Final = "[redactado]"
"""Sustituto de un valor que no puede salir en un registro."""

OTHER: Final = "other"
"""Sustituto de un valor fuera de su lista cerrada en métricas y trazas (cardinalidad acotada)."""

AttributeValue = str | bool | int | float

_PEM = re.compile(r"-----BEGIN[^\n]*?-----.*?(?:-----END[^\n]*?-----|\Z)", re.DOTALL)
_LINK = re.compile(r"\S*://\S*|\S*www\.\S*", re.IGNORECASE)
_EMAIL = re.compile(r"\S*@\S*")
_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ENUM_MEMBER_VALUE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REGISTERED_VALUE = re.compile(r"^[A-Za-z0-9_./{}:-]{1,128}$")


def _token(match: re.Match[str]) -> str:
    run = match.group(0)
    return run if _UUID.fullmatch(run) else REDACTED


def redact_text(text: str) -> str:
    """Sustituye bloques PEM, enlaces, correos y tokens por ``[redactado]``.

    Un UUID canónico (minúsculas con guiones) es un identificador permitido y no cuenta como
    token; cualquier otra tira de 20 o más caracteres del alfabeto de token sí.
    """
    for pattern in (_PEM, _LINK, _EMAIL):
        text = pattern.sub(REDACTED, text)
    return _TOKEN.sub(_token, text)


IDENTIFIER_KEYS: Final = frozenset(
    {
        "correlation_id",
        "organization_id",
        "plant_id",
        "zone_id",
        "node_id",
        "actor_id",
        "record_id",
        "event_id",
    }
)
"""Identificadores de plataforma (UUID). Nunca de sesión, invitación ni token (NFR-NUC-17)."""

_HTTP_METHODS: Final = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "_OTHER")
_SQL_OPERATIONS: Final = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "WITH",
    "SET",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
)

DEFAULT_ENUMERATIONS: Final[Mapping[str, tuple[str, ...]]] = {
    # Propias de la plataforma.
    "status_class": ("1xx", "2xx", "3xx", "4xx", "5xx"),
    "method": _HTTP_METHODS,
    "chain_kind": ("plant", "organization", "audit"),
    "pool_class": ("node", "person", "worker"),
    "signal": ("traces", "metrics"),
    "task": (
        "write_checkpoints",
        "verify_chains_incremental",
        "evidence_sample",
        "create_partitions",
        "archive_audit_partitions",
        "expire_sessions",
        "throttle_window_cleanup",
        "expire_concessions",
    ),
    "operation": (
        "ledger_write",
        "ledger_write_with_evidence",
        "ledger_list",
        "ledger_timeline",
        "login",
        "live_view_token",
        "outbox_delivery",
    ),
    "alert_type": (
        "security_alert",
        "integrity_compromised",
        "unknown_token_reported",
        "context_absent_attempt",
        "authorization_denied",
    ),
    # Se amplían al arrancar (rutas por la fábrica de la aplicación, tipos por su registro…).
    "route": (),
    "code": (),
    "result": (),
    "reason": (),
    "record_type": (),
    "event_type": (),
    "consumer": (),
    "audit_operation": (),
    "purpose": (),
    "table": (),
    # Convenciones semánticas de OpenTelemetry (instrumentación automática).
    "http.route": (),
    "http.method": _HTTP_METHODS,
    "http.request.method": _HTTP_METHODS,
    "db.system": ("postgresql", "sqlite"),
    "db.system.name": ("postgresql", "sqlite"),
    "db.operation": _SQL_OPERATIONS,
    "db.operation.name": _SQL_OPERATIONS,
    "rpc.system": ("aws-api",),
    "rpc.service": ("S3", "SecretsManager", "KMS"),
    "rpc.method": (
        "PutObject",
        "HeadObject",
        "GetObject",
        "GetSecretValue",
        "PutSecretValue",
        "GenerateDataKey",
        "Decrypt",
    ),
    "aws.region": ("us-east-1",),
}
"""Listas cerradas por nombre de atributo. ``route`` y ``http.route`` comparten registro."""

_SHARED_REGISTRATION: Final = {"route": "http.route", "http.route": "route"}

INTEGER_KEYS: Final[Mapping[str, tuple[int, int]]] = {
    "status": (100, 599),
    "http.status_code": (100, 599),
    "http.response.status_code": (100, 599),
    "partition": (0, 100_000),
    "attempt": (0, 1_000),
}
"""Enteros permitidos con su rango cerrado."""

BOOLEAN_KEYS: Final = frozenset({"exception.escaped"})


class AttributePolicy:
    """Lista blanca de atributos con sus validadores (identificadores y enumeraciones)."""

    def __init__(self) -> None:
        self._enumerations: dict[str, set[str]] = {
            key: set(values) for key, values in DEFAULT_ENUMERATIONS.items()
        }

    @property
    def keys(self) -> frozenset[str]:
        """Todos los nombres de atributo permitidos."""
        return frozenset(
            IDENTIFIER_KEYS | self._enumerations.keys() | INTEGER_KEYS.keys() | BOOLEAN_KEYS
        )

    def register(self, key: str, values: Iterable[str]) -> None:
        """Amplía la lista cerrada de ``key`` (se llama al arrancar, nunca con datos).

        Raises:
            KeyError: ``key`` no es una enumeración de la política.
            ValueError: un valor no tiene forma de constante del código.
        """
        if key not in self._enumerations:
            raise KeyError(f"{key!r} no es un atributo de enumeración")
        accepted = list(values)
        for value in accepted:
            if (
                not _REGISTERED_VALUE.fullmatch(value)
                or "://" in value
                or redact_text(value) != value
            ):
                raise ValueError(f"valor de enumeración no admitido para {key!r}")
        self._enumerations[key].update(accepted)
        shared = _SHARED_REGISTRATION.get(key)
        if shared is not None:
            self._enumerations[shared].update(accepted)

    def values(self, key: str) -> frozenset[str]:
        """Valores registrados de la enumeración ``key``."""
        return frozenset(self._enumerations.get(key, ()))

    def clean_value(self, key: str, value: object, replacement: str) -> AttributeValue | None:
        """Valor permitido de ``key``, ``replacement`` si no lo es, o ``None`` para descartarlo."""
        if key in IDENTIFIER_KEYS:
            if isinstance(value, uuid.UUID):
                return str(value)
            if isinstance(value, str) and _UUID.fullmatch(value):
                return value
            return replacement
        if key in self._enumerations:
            if isinstance(value, enum.Enum):
                member = value.value
                if isinstance(member, str) and (
                    _ENUM_MEMBER_VALUE.fullmatch(member) or member in self._enumerations[key]
                ):
                    return member
                return replacement
            if isinstance(value, str) and value in self._enumerations[key]:
                return value
            return replacement
        if key in INTEGER_KEYS:
            low, high = INTEGER_KEYS[key]
            if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                return value
            return None
        if key in BOOLEAN_KEYS:
            return value if isinstance(value, bool) else None
        return None

    def clean(
        self,
        attributes: Mapping[str, object] | None,
        *,
        allowed: Collection[str] | None = None,
        replacement: str = OTHER,
    ) -> dict[str, AttributeValue]:
        """Atributos permitidos y validados; ``allowed`` restringe más la lista blanca."""
        if not attributes:
            return {}
        cleaned: dict[str, AttributeValue] = {}
        for key, value in attributes.items():
            if not isinstance(key, str) or (allowed is not None and key not in allowed):
                continue
            safe = self.clean_value(key, value, replacement)
            if safe is not None:
                cleaned[key] = safe
        return cleaned


DEFAULT_POLICY: Final = AttributePolicy()
"""Política del proceso; la fábrica de la aplicación y los registros la amplían al arrancar."""


def is_finite_non_negative(value: object) -> TypeGuard[int | float]:
    """``value`` es un número real finito y no negativo (``bool`` no cuenta)."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


_exception_names: frozenset[str] = frozenset()


def _collect_exception_names() -> frozenset[str]:
    names: set[str] = set()
    pending: list[type[BaseException]] = [BaseException]
    seen: set[type[BaseException]] = set()
    while pending:
        cls = pending.pop()
        if cls in seen:
            continue
        seen.add(cls)
        names.update({cls.__name__, cls.__qualname__, f"{cls.__module__}.{cls.__qualname__}"})
        pending.extend(cls.__subclasses__())
    return frozenset(names)


def known_exception_type(name: object) -> bool:
    """``name`` es el nombre de una clase de excepción cargada en el proceso.

    Así el tipo de una excepción registrada en un tramo sale solo si viene del código, nunca
    de un dato: un texto cualquiera no nombra una clase existente.
    """
    global _exception_names
    if not isinstance(name, str) or not name:
        return False
    if name in _exception_names:
        return True
    _exception_names = _collect_exception_names()
    return name in _exception_names
