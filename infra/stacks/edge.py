"""Pila ``vigia-edge``: zona alojada, certificados públicos, balanceadores, grupos de destino,
almacén de confianza y cortafuegos (§4). Depende de ``vigia-foundation`` y de ``vigia-data``
(depósito ``vigia-edge``). Recursos: TASK-147.

TASK-144 la registra vacía en ``app.py``; sus recursos llegan con la tarea citada.
"""

from __future__ import annotations

from stacks.base import VigiaStack


class EdgeStack(VigiaStack):
    """Pila ``vigia-edge`` (infrastructure-design §2.3)."""

    key = "edge"
    summary = "zona, certificados, balanceadores, almacen de confianza, cortafuegos"
