"""Pila ``vigia-datasets``: depósito del conjunto sellado y depósito de registros ``vigia-logs``
(U-01 §4.2). Única en la cuenta, sin sufijo de entorno y excluida de ``staging`` (D-8).
Se traslada de ``vigia-contracts/infra/`` con los mismos identificadores lógicos
(deployment-architecture §8). Sin dependencias. Traslado: TASK-150.

TASK-144 la registra vacía en ``app.py``; sus recursos llegan con la tarea citada.
"""

from __future__ import annotations

from stacks.base import VigiaStack


class DatasetsStack(VigiaStack):
    """Pila ``vigia-datasets`` (infrastructure-design §2.3)."""

    key = "datasets"
    summary = "deposito del conjunto sellado y de registros (U-01)"
