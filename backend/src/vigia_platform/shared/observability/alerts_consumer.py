"""Consumidor ``alert_metrics`` (``observability.alerts``): alertas de seguridad como métricas
(PAT-NUC-SEG-08).

Las alertas de NFR-NUC-28 se escriben en la cadena de auditoría y se publican en la bandeja; este
consumidor del núcleo las convierte en ``security_alert_total`` con dos dimensiones,
``alert_type`` (lista cerrada) y ``organization_id`` (la organización del evento), que vigilan las
alarmas de Infrastructure Design. Así la latencia de una alerta es la de la bandeja.

``alert_type`` por evento:

- ``integrity_compromised`` → ``integrity_compromised``;
- ``security_alert`` según ``alert_kind``: ``context_absent_attempt`` →
  ``context_absent_attempt``, ``unknown_token_reported`` → ``unknown_token_reported``,
  ``authorization_denied_repeated`` (más de 20 por actor en 10 min) → ``authorization_denied``,
  y el resto (fallos de inicio de sesión, marca de evidencia) → ``security_alert``.

Idempotente por ``event_id`` dentro del proceso: si la transacción de la entrega no llega a
confirmar después de contar (p. ej. se pierde la conexión al marcarla), la reentrega no vuelve a
sumar. Si el proceso muere entre contar y confirmar, la métrica puede sumar una vez de más: la
alarma salta con cualquier valor mayor que cero, así que no cambia nada.

El consumidor se llama ``alert_metrics`` y no ``observability.alerts`` como en el diseño: el
nombre de ``shared.consumer`` es ``snake_case`` sin puntos, y ``observability_alerts`` tiene 20
caracteres, que la política de atributos trata como un token y no admite como dimensión
``consumer`` de las métricas (NFR-NUC-41).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from vigia_platform.shared.context import ActorUnit
from vigia_platform.shared.observability.metrics import PlatformMetrics, get_metrics
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry

if TYPE_CHECKING:
    from vigia_platform.shared.db import Transaction
    from vigia_platform.shared.outbox.publish import OutboxEvent

__all__ = [
    "ALERTS_CONSUMER",
    "ALERT_EVENTS",
    "AlertsConsumer",
    "alert_type",
    "register_alerts_consumer",
]

ALERTS_CONSUMER: Final = "alert_metrics"
SECURITY_ALERT: Final = "security_alert"
INTEGRITY_COMPROMISED: Final = "integrity_compromised"
ALERT_EVENTS: Final = (INTEGRITY_COMPROMISED, SECURITY_ALERT)

_ALERT_KIND_TYPES: Final[Mapping[str, str]] = {
    "context_absent_attempt": "context_absent_attempt",
    "unknown_token_reported": "unknown_token_reported",
    "authorization_denied_repeated": "authorization_denied",
}
_REMEMBERED_EVENTS: Final = 10_000
"""Eventos ya contados que se recuerdan para no volver a sumar una reentrega."""


def alert_type(event_name: str, payload: Mapping[str, object]) -> str:
    """El ``alert_type`` (lista cerrada de la métrica) de un evento de alerta."""
    if event_name == INTEGRITY_COMPROMISED:
        return INTEGRITY_COMPROMISED
    kind = payload.get("alert_kind")
    return _ALERT_KIND_TYPES.get(kind, SECURITY_ALERT) if isinstance(kind, str) else SECURITY_ALERT


class AlertsConsumer:
    """El manejador: una suma de ``security_alert_total`` por evento."""

    def __init__(self, metrics: PlatformMetrics | None = None) -> None:
        self._metrics = metrics
        self._counted: OrderedDict[object, None] = OrderedDict()

    async def __call__(self, event: OutboxEvent, transaction: Transaction) -> None:
        if event.event_id in self._counted:
            return
        metrics = self._metrics if self._metrics is not None else get_metrics()
        metrics.security_alert_total.add(
            1,
            {
                "alert_type": alert_type(event.event_name, event.payload),
                "organization_id": str(event.organization_id),
            },
        )
        self._counted[event.event_id] = None
        if len(self._counted) > _REMEMBERED_EVENTS:
            self._counted.popitem(last=False)


def register_alerts_consumer(
    registry: ConsumerRegistry, metrics: PlatformMetrics | None = None
) -> Consumer:
    """Registra ``alert_metrics`` al arrancar (U-02, sin dependencia externa)."""
    return registry.register(
        Consumer(
            consumer_name=ALERTS_CONSUMER,
            unit=ActorUnit.U02,
            subscribed_events=ALERT_EVENTS,
            handler=AlertsConsumer(metrics),
        )
    )
