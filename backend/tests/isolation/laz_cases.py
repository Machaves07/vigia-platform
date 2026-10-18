"""Casos de aislamiento por ruta de U-04 (TASK-302; PR-LAZ-62 parcial; LC-LAZ-43).

``test_route_isolation.py`` concatena ``LAZ_CASES`` a su ``CASES``, y ``uncovered`` exige un caso
por cada ruta registrada: una ruta de ``/loop/*``, ``/accreditation/*`` o ``/notifications/*``
sin su caso aquí deja la prueba en rojo. **Ninguna tarea de U-04 edita
``test_route_isolation.py``**: cada tarea que registra una ruta añade aquí su caso, con los tipos
de ``tests.isolation.route_cases``::

    (("GET", "/loop/findings/{finding_state_id}"), Case(Kind.RESOURCE, call=...)),

Hoy U-04 no registra ninguna ruta: la lista está vacía.
"""

from __future__ import annotations

from tests.isolation.route_cases import Case

__all__ = ["LAZ_CASES"]

LAZ_CASES: list[tuple[tuple[str, str], Case]] = []
"""``((MÉTODO, /plantilla), Case)`` de cada ruta de U-04."""
