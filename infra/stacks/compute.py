"""Pila ``vigia-compute``: registro de imágenes, clúster, definiciones de tarea, servicios,
escalado, roles de tarea y secretos de firma (§5, §8). Depende de ``vigia-foundation``,
``vigia-data`` y ``vigia-edge``. Recursos: TASK-148.

TASK-144 la registra vacía en ``app.py``; sus recursos llegan con la tarea citada.
"""

from __future__ import annotations

from stacks.base import VigiaStack


class ComputeStack(VigiaStack):
    """Pila ``vigia-compute`` (infrastructure-design §2.3)."""

    key = "compute"
    summary = "registro de imagenes, cluster, servicios, tareas puntuales, roles"
