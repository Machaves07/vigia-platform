"""Observaciones de correo por estado, sin contenido ni destinatario (NFR-LAZ-41).

Son contadores de observaciones operativas, no una contabilidad transaccional de entregas: una
repetición del aviso del proveedor puede volver a observar un rebote. La tasa de reputación se
vigila con las métricas de SES, no con este contador.
"""

import uuid

from vigia_platform.loop.observability.metrics import LoopMetricName, get_loop_metrics


def record_email(state: str, organization_id: uuid.UUID, amount: int = 1) -> None:
    """Solo el estado cerrado y la organización llegan al instrumento del catálogo."""
    get_loop_metrics().counter(LoopMetricName.EMAIL_MESSAGES_TOTAL).add(
        amount, {"email_state": state, "organization_id": str(organization_id)}
    )
