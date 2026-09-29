"""Pila ``vigia-observability``: grupos de registro de la aplicación, alarmas, tablero,
comprobaciones de Route 53 y suscripciones (§9). Depende de ``vigia-compute``. Recursos: TASK-149.

TASK-144 la registra vacía en ``app.py``; sus recursos llegan con la tarea citada.
"""

from __future__ import annotations

from stacks.base import VigiaStack


class ObservabilityStack(VigiaStack):
    """Pila ``vigia-observability`` (infrastructure-design §2.3)."""

    key = "observability"
    summary = "grupos de registro, alarmas, tablero, comprobaciones de salud"
