"""Registro estructurado JSON con redacción (NFR-NUC-17, PAT-NUC-SEG-09; herencia de NFR-CTR-15).

Una línea JSON por evento en la salida estándar con ``timestamp`` (UTC, milisegundos y ``Z``),
``level``, ``component`` y ``message``, más los campos de contexto de NFR-NUC-17 cuando existen:
``correlation_id``, ``organization_id``, ``actor_id``, ``route``, ``status`` y ``duration_ms``.
El formato es el del ``JsonLog`` de U-01, ampliado con esos campos.

Uso::

    log = get_logger("ledger.application")
    log.info("registro escrito", record_type=RecordType.FINDING, duration_ms=12)
    with log_context(correlation_id=cid, organization_id=org):
        ...

Qué nunca sale (el formateador lo garantiza, no la disciplina de quien llama):

- **Campos**: solo los de la lista blanca de ``redaction.AttributePolicy`` (identificadores UUID
  y enumeraciones) más ``status`` y ``duration_ms``; un campo desconocido se descarta y se cuenta
  en ``redacted_fields``; un valor inválido sale como ``[redactado]``.
- **Argumentos** ``%`` (también de bibliotecas de terceros): números, UUID y miembros de
  ``enum.Enum`` se interpolan; cualquier otro valor, cadenas incluidas, sale como ``[redactado]``.
- **Mensaje de ``get_logger``**: solo sale si es una constante del código que llama (un literal
  de su función o una constante en mayúsculas de su módulo, como exige la regla VIG004); si no,
  se sustituye por ``[redactado]`` antes de crear el registro, así que ningún manejador lo ve.
  Después pasa por ``redact_text`` y se corta a 512 caracteres.
- **Mensaje de terceros** (un registrador que no es de ``get_logger``): sin argumentos ``%`` sale
  como ``[redactado]``, porque puede ser un f-string con datos (p. ej. la excepción no recuperada
  de una tarea de asyncio); con argumentos se conserva la plantilla, redactada.
- **Componente**: el de ``get_logger`` si es constante; el de terceros, el módulo cargado más
  largo que prefija el nombre del registrador; si no, ``other``.
- **Excepciones**: solo ``exception_type`` (nombre de la clase); nunca el mensaje ni la traza.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import re
import sys
import types
import uuid
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final, TextIO

from vigia_platform.shared.observability import redaction

__all__ = [
    "LOG_LEVEL_ENV",
    "MAX_MESSAGE_CHARS",
    "JsonFormatter",
    "PlatformLogger",
    "configure_logging",
    "get_logger",
    "log_context",
]

LOG_LEVEL_ENV: Final = "VIGIA_LOG_LEVEL"
MAX_MESSAGE_CHARS: Final = 512
LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

CONTEXT_FIELDS: Final = (
    "correlation_id",
    "organization_id",
    "actor_id",
    "route",
    "status",
    "duration_ms",
)
"""Campos de NFR-NUC-17, en el orden en que salen tras los cuatro fijos."""

QUIET_LOGGERS: Final[Mapping[str, int]] = {
    "uvicorn.access": logging.WARNING,
    "botocore": logging.WARNING,
    "boto3": logging.WARNING,
    "urllib3": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "sqlalchemy": logging.WARNING,
    "asyncio": logging.WARNING,
    "grpc": logging.ERROR,
    "opentelemetry": logging.ERROR,
    "opentelemetry.exporter.otlp": logging.CRITICAL,
}
"""Bibliotecas que registran rutas, peticiones o reintentos del exportador: solo avisos.

Con el colector caído, el exportador OTLP escribiría una línea por lote fallido; se silencia y
``tracing`` escribe una sola línea al empezar la caída y otra al recuperarse, además de contar lo
descartado en ``otel_dropped_total`` (PAT-NUC-RES-03).
"""

_COMPONENT: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENUM_MEMBER_VALUE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_COMPONENT_ATTR: Final = "vigia_component"
_FIELDS_ATTR: Final = "vigia_fields"
_STANDARD_ATTRS: Final = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "taskName", _COMPONENT_ATTR, _FIELDS_ATTR}
)

_context: ContextVar[Mapping[str, object]] = ContextVar("vigia_log_context", default={})  # noqa: B039 — el valor por defecto nunca se muta.


@contextlib.contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Añade ``fields`` a cada línea de registro emitida dentro del bloque (p. ej. por petición)."""
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def _safe_argument(value: object) -> object:
    """Argumento ``%`` que puede interpolarse; cualquier otro sale como ``[redactado]``."""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, enum.Enum):
        member = value.value
        if isinstance(member, int) and not isinstance(member, bool):
            return member
        if isinstance(member, str) and _ENUM_MEMBER_VALUE.fullmatch(member):
            return member
    return redaction.REDACTED


def _is_code_constant(text: str, frame: types.FrameType | None) -> bool:
    """``text`` es un literal del código de ``frame`` o una constante en mayúsculas de su módulo.

    Un texto construido en ejecución (f-string, concatenación, variable con datos) no está entre
    las constantes del código que llama; si coincide con una, es ese mismo texto constante.
    """
    if frame is None:
        return False
    if text in frame.f_code.co_consts:
        return True
    return any(name.isupper() and value == text for name, value in frame.f_globals.items())


def _message(record: logging.LogRecord) -> str:
    if not isinstance(record.msg, str):
        return redaction.REDACTED
    if not record.args and getattr(record, _COMPONENT_ATTR, None) is None:
        return redaction.REDACTED
    text = record.msg
    if record.args:
        arguments: object
        if isinstance(record.args, Mapping):
            arguments = {key: _safe_argument(value) for key, value in record.args.items()}
        else:
            arguments = tuple(_safe_argument(value) for value in record.args)
        try:
            text = text % arguments
        except (TypeError, ValueError, KeyError):
            text = record.msg
    return redaction.redact_text(text)[:MAX_MESSAGE_CHARS]


def _loaded_module_prefix(name: str) -> str:
    """Prefijo más largo de ``name`` que es un módulo cargado (``uvicorn.error`` → ``uvicorn``)."""
    parts = name.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in sys.modules:
            return candidate
    return redaction.OTHER


def _timestamp(created: float) -> str:
    moment = datetime.fromtimestamp(created, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    """Formateador de una línea JSON por evento con redacción (NFR-NUC-17)."""

    def __init__(self, policy: redaction.AttributePolicy | None = None) -> None:
        super().__init__()
        self._policy = policy

    @property
    def policy(self) -> redaction.AttributePolicy:
        return self._policy if self._policy is not None else redaction.DEFAULT_POLICY

    def _component(self, record: logging.LogRecord) -> str:
        component = getattr(record, _COMPONENT_ATTR, None)
        if component is None:
            component = _loaded_module_prefix(record.name)
        if (
            isinstance(component, str)
            and _COMPONENT.fullmatch(component)
            and redaction.redact_text(component) == component
        ):
            return component
        return redaction.OTHER

    def _raw_fields(self, record: logging.LogRecord) -> dict[str, object]:
        fields: dict[str, object] = dict(_context.get())
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                fields[key] = value
        explicit = getattr(record, _FIELDS_ATTR, None)
        if isinstance(explicit, Mapping):
            fields.update(explicit)
        return fields

    def _clean_field(self, key: str, value: object) -> object | None:
        if key == "duration_ms":
            if redaction.is_finite_non_negative(value):
                return round(value, 3) if isinstance(value, float) else value
            return redaction.REDACTED
        if key in self.policy.keys:
            return self.policy.clean_value(key, value, redaction.REDACTED)
        return None

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname if record.levelname in LEVELS else "WARNING"
        line: dict[str, Any] = {
            "timestamp": _timestamp(record.created),
            "level": level,
            "component": self._component(record),
            "message": _message(record),
        }
        cleaned: dict[str, object] = {}
        dropped = 0
        for key, value in self._raw_fields(record).items():
            safe = self._clean_field(key, value) if isinstance(key, str) else None
            if safe is None:
                dropped += 1
            else:
                cleaned[key] = safe
        for key in CONTEXT_FIELDS:
            if key in cleaned:
                line[key] = cleaned.pop(key)
        line.update(sorted(cleaned.items()))
        if dropped:
            line["redacted_fields"] = dropped
        if record.exc_info and record.exc_info[0] is not None:
            line["exception_type"] = record.exc_info[0].__name__
        return json.dumps(line, ensure_ascii=False)


class PlatformLogger:
    """Registrador de un componente: mensaje constante y campos estructurados por nombre."""

    def __init__(self, component: str) -> None:
        self.component = component
        self._logger = logging.getLogger(f"vigia.{component}")

    def _log(self, level: int, message: str, fields: Mapping[str, object], exc: bool) -> None:
        if self._logger.isEnabledFor(level):
            # Marco 2: quien llamó a info(), warning()…; lo no constante nunca entra al registro.
            if not isinstance(message, str) or not _is_code_constant(message, sys._getframe(2)):
                message = redaction.REDACTED
            self._logger.log(
                level,
                message,
                exc_info=exc,
                stacklevel=3,
                extra={_COMPONENT_ATTR: self.component, _FIELDS_ATTR: dict(fields)},
            )

    def debug(self, message: str, /, **fields: object) -> None:
        self._log(logging.DEBUG, message, fields, False)

    def info(self, message: str, /, **fields: object) -> None:
        self._log(logging.INFO, message, fields, False)

    def warning(self, message: str, /, **fields: object) -> None:
        self._log(logging.WARNING, message, fields, False)

    def error(self, message: str, /, **fields: object) -> None:
        self._log(logging.ERROR, message, fields, False)

    def critical(self, message: str, /, **fields: object) -> None:
        self._log(logging.CRITICAL, message, fields, False)

    def exception(self, message: str, /, **fields: object) -> None:
        """Nivel ``ERROR`` con el tipo de la excepción en curso (nunca su mensaje ni su traza)."""
        self._log(logging.ERROR, message, fields, True)


def get_logger(component: str) -> PlatformLogger:
    """Registrador del componente ``component`` (p. ej. ``"identity.auth"``).

    ``component`` debe ser una constante del código que llama (VIG004); si no, es ``other``.
    """
    if not isinstance(component, str) or not _is_code_constant(component, sys._getframe(1)):
        component = redaction.OTHER
    return PlatformLogger(component)


def configure_logging(
    level: str | None = None,
    *,
    stream: TextIO | None = None,
    policy: redaction.AttributePolicy | None = None,
) -> logging.Handler:
    """Instala el formateador JSON en el registrador raíz, sustituyendo sus manejadores.

    El nivel sale de ``level``, de ``VIGIA_LOG_LEVEL`` o, en su defecto, ``INFO``.
    """
    chosen = (level or os.environ.get(LOG_LEVEL_ENV) or "INFO").upper()
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(policy))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(chosen if chosen in LEVELS else "INFO")
    for name, quiet in QUIET_LOGGERS.items():
        logging.getLogger(name).setLevel(quiet)
    return handler
