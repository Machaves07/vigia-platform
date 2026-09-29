"""Pila ``vigia-foundation``: red y puntos privados (§3), claves KMS (§7.1), tema ``vigia-alerts``,
presupuestos y grupo de registro de flujos. Sin dependencias. Recursos: TASK-145.

TASK-144 la registra vacía en ``app.py``; sus recursos llegan con la tarea citada.
"""

from __future__ import annotations

from stacks.base import VigiaStack


class FoundationStack(VigiaStack):
    """Pila ``vigia-foundation`` (infrastructure-design §2.3)."""

    key = "foundation"
    summary = "red, puntos privados, claves KMS, tema de alertas, presupuestos"
