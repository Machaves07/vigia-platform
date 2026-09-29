"""Pila ``vigia-data``: base gestionada y su grupo de parámetros, secretos de base, depósitos,
bóveda y plan de copias (§6, §11). Depende de ``vigia-foundation``. Recursos: TASK-146.

TASK-144 la registra vacía en ``app.py``; sus recursos llegan con la tarea citada.
"""

from __future__ import annotations

from stacks.base import VigiaStack


class DataStack(VigiaStack):
    """Pila ``vigia-data`` (infrastructure-design §2.3)."""

    key = "data"
    summary = "base gestionada, depositos, boveda y plan de copias"
