"""Política única de redacción de registros, métricas y trazas (PAT-NUC-SEG-09, MAN-05).

Dos barreras, compartidas por ``logging``, ``metrics`` y ``tracing``:

- ``redact_text`` sustituye por ``[redactado]`` todo lo que tenga forma de bloque PEM, enlace
  (``esquema://…`` o ``www.…``), correo (cualquier trozo sin espacios con ``@``), credencial con
  esquema (``Bearer …``, ``Basic …``), JWT (``eyJ…`` con sus segmentos, aunque sean cortos),
  token con puntos (segmentos de dos o más caracteres con letras y dígitos, 10 o más en total) o
  token (20 o más caracteres seguidos del alfabeto ``A-Z a-z 0-9 _ - + / =``). Se aplica a los
  mensajes de registro, que el código fija como constantes (regla VIG004); es la segunda barrera.
- **Nombres de tramo y de evento**: solo salen los de una lista cerrada (``register_span_names``
  y ``register_event_names``) y las formas de la instrumentación automática reconstruidas desde
  listas cerradas (método HTTP y ruta registrada, verbo SQL, servicio y operación de AWS); todo
  lo demás sale como ``other``. Un texto libre o un secreto corto nunca es uno de ellos.
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
_AUTH = re.compile(r"\b(?:bearer|basic|digest|token|apikey)[ \t:=]+\S+", re.IGNORECASE)
_JWT = re.compile(r"\S*eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*){1,2}\S*")
_DOTTED = re.compile(r"[A-Za-z0-9_\-+/=]{2,}(?:\.[A-Za-z0-9_\-+/=]{2,})+")
_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{20,}")
_DIGIT = re.compile(r"\d")
_LETTER = re.compile(r"[A-Za-z]")

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ENUM_MEMBER_VALUE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REGISTERED_VALUE = re.compile(r"^[A-Za-z0-9_./{}:-]{1,128}$")
_REGISTERED_NAME = re.compile(r"^[A-Za-z0-9_./{}: -]{1,128}$")
_HTTP_SPAN = re.compile(
    r"(?:HTTP )?(?P<method>[A-Z_]+)(?: (?P<route>/\S*))?(?P<suffix> http (?:send|receive))?"
)
_RPC_SPAN = re.compile(r"(?P<service>[A-Za-z0-9]+)\.(?P<method>[A-Za-z0-9]+)")


def _token(match: re.Match[str]) -> str:
    run = match.group(0)
    return run if _UUID.fullmatch(run) else REDACTED


def _dotted(match: re.Match[str]) -> str:
    run = match.group(0)
    if len(run) >= 10 and _DIGIT.search(run) and _LETTER.search(run):
        return REDACTED
    return run


def redact_text(text: str) -> str:
    """Sustituye bloques PEM, enlaces, correos y tokens por ``[redactado]``.

    Un UUID canónico (minúsculas con guiones) es un identificador permitido y no cuenta como
    token; cualquier otra tira de 20 o más caracteres del alfabeto de token sí.
    """
    for pattern in (_PEM, _LINK, _EMAIL, _AUTH, _JWT):
        text = pattern.sub(REDACTED, text)
    text = _DOTTED.sub(_dotted, text)
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
    "rate_limit": ("session", "origin", "public", "node", "brake"),
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
        "key_rotation_reminder",
        # U-03 (TASK-220): fija aquí porque, con 26 caracteres, ``register`` la tomaría por un
        # token; la alarma revocation-list-publish-failed la vigila por este valor.
        "regenerate_revocation_list",
        # U-03 (TASK-227): las demás de 20 o más caracteres, por lo mismo; NFR-GOB-56 y la alarma
        # periodic-task-stale-<tarea> (VIG-167) las miden por este valor.
        "evaluate_fleet_alarms",
        "expire_enrollment_codes",
        "expire_walk_test_sessions",
        "alert_expiring_certificates",
    ),
    "dependency": ("secrets_manager", "kms"),
    "purpose": ("catalog", "gate", "live_view_token", "key_set", "checkpoint"),
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
    # ``rejection_code`` del contrato de U-01 (NFR-GOB-25, 54): fijos aquí porque varios
    # (``contract_version_unsupported``…) tienen forma de token y ``register`` los rechazaría.
    "rejection_code": (
        "schema_invalid",
        "payload_too_large",
        "contract_version_unsupported",
        "contract_version_retired",
        "node_not_enrolled",
        "node_revoked",
        "node_zone_mismatch",
        "zone_gate_not_approved",
        "clip_missing",
        "clip_hash_mismatch",
        "clip_too_large",
        "clip_not_anonymized",
        "enrollment_code_invalid",
        "enrollment_code_expired",
        "enrollment_code_used",
        "signature_invalid",
        "idempotency_conflict",
        "timestamp_out_of_window",
        "temporarily_unavailable",
        "rate_limited",
        "storage_unavailable",
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
    "table": (),
    "app_version": ("unknown",),
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

HASH_KEYS: Final = frozenset({"source_ip_tag"})
"""Etiqueta del origen de un intento de alta (TASK-218, BR-GOB-61): los **16** primeros
hexadecimales de su ``source_ip_hash`` (HMAC con la clave estable), para cruzar el registro con
las filas de ``enrollment_attempt``. Exactamente 16: un valor de 64 (una huella de hardware, una
firma de URL) se parece a un hash y nunca pasa por aquí (PR-GOB-31). Solo para registros
estructurados: ninguna métrica la declara (cardinalidad)."""
_SOURCE_TAG: Final = re.compile(r"^[0-9a-f]{16}$")


class AttributePolicy:
    """Lista blanca de atributos con sus validadores (identificadores y enumeraciones)."""

    def __init__(self) -> None:
        self._enumerations: dict[str, set[str]] = {
            key: set(values) for key, values in DEFAULT_ENUMERATIONS.items()
        }
        self._span_names: set[str] = set()
        self._event_names: set[str] = set()

    @property
    def keys(self) -> frozenset[str]:
        """Todos los nombres de atributo permitidos."""
        return frozenset(
            IDENTIFIER_KEYS
            | self._enumerations.keys()
            | INTEGER_KEYS.keys()
            | BOOLEAN_KEYS
            | HASH_KEYS
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

    @staticmethod
    def _check_names(names: Iterable[str]) -> list[str]:
        accepted = list(names)
        for name in accepted:
            if not _REGISTERED_NAME.fullmatch(name) or redact_text(name) != name:
                raise ValueError("nombre de tramo o de evento no admitido")
        return accepted

    def register_span_names(self, names: Iterable[str]) -> None:
        """Amplía la lista cerrada de nombres de tramo (constantes del código, al arrancar)."""
        self._span_names.update(self._check_names(names))

    def register_event_names(self, names: Iterable[str]) -> None:
        """Amplía la lista cerrada de nombres de evento de tramo."""
        self._event_names.update(self._check_names(names))

    def clean_span_name(self, name: object) -> str:
        """Nombre registrado, forma de la instrumentación automática o ``other``."""
        if not isinstance(name, str):
            return OTHER
        if name in self._span_names:
            return name
        http = _HTTP_SPAN.fullmatch(name)
        if http and http["method"] in _HTTP_METHODS:
            route = http["route"]
            kept = f" {route}" if route and route in self._enumerations["route"] else ""
            return f"{http['method']}{kept}{http['suffix'] or ''}"
        first = name.split(" ", 1)[0]
        if first in _SQL_OPERATIONS or first == "connect":
            return first
        rpc = _RPC_SPAN.fullmatch(name)
        if (
            rpc
            and rpc["service"] in self._enumerations["rpc.service"]
            and rpc["method"] in self._enumerations["rpc.method"]
        ):
            return name
        return OTHER

    def clean_event_name(self, name: object) -> str:
        """Nombre de evento registrado u ``other``."""
        return name if isinstance(name, str) and name in self._event_names else OTHER

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
        if key in HASH_KEYS:
            return value if isinstance(value, str) and _SOURCE_TAG.fullmatch(value) else replacement
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
