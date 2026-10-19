"""``AssistantProviderPort``: lo único de U-04 que habla con el proveedor del asistente
(LC-LAZ-13 y 14; tech-stack-decisions §2.3 y §2.4; TASK-316).

``complete(context, *, timeout)`` recibe **solo** el objeto de la frontera cerrada
(``loop.assistant.context.AssistantContext``) y el tope del intento, y devuelve un
``AssistantOutcome``; nunca lanza por un fallo del proveedor:

- ``AssistantCompletion``: el proveedor respondió (código 200). Lleva el **motivo de parada**, el
  texto JSON del contenido, el modelo **de la respuesta** (con el respaldo del servidor puede no
  ser el configurado, nota fechada de DE §2.11), el uso (fichas de entrada, salida y caché, y la
  región de inferencia) y el identificador de la petición del proveedor;
- ``AssistantFailure``: el proveedor no respondió con contenido, clasificado en
  ``transient`` (429 con su espera, 500, 529, 408, 409, conexión), ``permanent`` (400, 401, 402,
  403, 404, 413) o ``timeout`` (el intento llegó a su tope).

``completion_failure`` comprueba el motivo de parada **antes** de leer el contenido: un rechazo
del clasificador (200 con ``refusal``) es ``refusal`` y un tope de fichas (``max_tokens``)
truncaría el borrador, ``budget_exceeded``; ninguno produce borrador (``provider_error``).

``configured`` es ``False`` sin clave del proveedor: el consumidor responde ``no_api_key`` sin
llamar (BR-LAZ-74). El puerto no tiene ningún método para cerrar, firmar ni escribir en el
expediente (BR-LAZ-72).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, Protocol, runtime_checkable

from vigia_platform.loop.assistant.context import AssistantContext

__all__ = [
    "END_TURN",
    "MAX_TOKENS",
    "PROVIDER_CREDENTIAL_VARIABLES",
    "REFUSAL",
    "AssistantCompletion",
    "AssistantFailure",
    "AssistantOutcome",
    "AssistantProviderPort",
    "FailureKind",
    "TokenUsage",
    "completion_failure",
    "provider_credentials_present",
]

END_TURN: Final = "end_turn"
"""El único motivo de parada con un contenido que se lee."""
REFUSAL: Final = "refusal"
"""Rechazo del clasificador de seguridad: llega con código 200 (tech-stack §2.3)."""
MAX_TOKENS: Final = "max_tokens"
"""Tope de fichas de la petición: el contenido estaría truncado."""


class FailureKind(enum.StrEnum):
    """Clasificación de un fallo del proveedor (tech-stack §2.4)."""

    TRANSIENT = "transient"
    """Reintentable con el retroceso propio (429 respetando su espera, 500, 529, 408, 409)."""
    PERMANENT = "permanent"
    """Sin reintento (400, 401, 402, 403, 404, 413): ``provider_error``."""
    TIMEOUT = "timeout"
    """El intento llegó a su tope (60 s o lo que quede de los 120 s)."""
    REFUSAL = "refusal"
    """Respuesta 200 con ``stop_reason = refusal``: ``provider_error``, nunca borrador."""
    BUDGET_EXCEEDED = "budget_exceeded"
    """Respuesta con ``stop_reason = max_tokens``: truncada, ``provider_error``, nunca borrador."""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """El bloque de uso de una respuesta: fichas y región donde se ejecutó (NFR-LAZ-31)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    inference_geo: str | None = None

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens, self.cache_read_tokens):
            if type(value) is not int or not 0 <= value <= 2**40:
                raise ValueError("las fichas son enteros no negativos")


@dataclass(frozen=True, slots=True)
class AssistantCompletion:
    """El proveedor respondió: se mira ``stop_reason`` antes que ``content_json``."""

    stop_reason: str
    content_json: str | None
    model_name: str
    model_version: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssistantFailure:
    """El proveedor no dio contenido; ``retry_after`` es la espera de la cabecera de un 429."""

    kind: FailureKind
    status_code: int | None = None
    retry_after: timedelta | None = None
    usage: TokenUsage | None = None
    model_name: str | None = None
    provider_request_id: str | None = None


type AssistantOutcome = AssistantCompletion | AssistantFailure


@runtime_checkable
class AssistantProviderPort(Protocol):
    """El proveedor del asistente (``FakeAssistantProvider`` aquí; el real, TASK-344)."""

    @property
    def configured(self) -> bool:
        """``False`` sin credencial del proveedor: no se llama (``no_api_key``)."""
        ...

    async def complete(self, context: AssistantContext, *, timeout: timedelta) -> AssistantOutcome:
        """Un intento con ``timeout`` como tope; nunca lanza por un fallo del proveedor."""
        ...


PROVIDER_CREDENTIAL_VARIABLES: Final = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
"""Las variables de entorno con que el paquete del proveedor toma su credencial (NFR-LAZ-26): en
producción solo existen en ``vigia-worker``; en pruebas, **nunca** (NFR-LAZ-47)."""


def provider_credentials_present(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Las variables de ``PROVIDER_CREDENTIAL_VARIABLES`` con valor en ``environ`` (solo los
    nombres: el valor nunca se lee más allá de saber que no está vacío)."""
    return tuple(name for name in PROVIDER_CREDENTIAL_VARIABLES if environ.get(name, "").strip())


def completion_failure(completion: AssistantCompletion) -> AssistantFailure | None:
    """El fallo que el motivo de parada declara, **sin mirar el contenido**; ``None`` si es
    ``end_turn``. Cualquier otro motivo (también uno desconocido) no produce borrador."""
    reason = completion.stop_reason
    if reason == END_TURN:
        return None
    kind = FailureKind.BUDGET_EXCEEDED if reason == MAX_TOKENS else FailureKind.REFUSAL
    if reason not in (REFUSAL, MAX_TOKENS):
        kind = FailureKind.PERMANENT
    return AssistantFailure(
        kind=kind,
        status_code=200,
        usage=completion.usage,
        model_name=completion.model_name,
        provider_request_id=completion.provider_request_id,
    )
