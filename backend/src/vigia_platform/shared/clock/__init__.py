"""Reloj inyectable (PAT-NUC-RES-07).

Es el único paquete de ``vigia_platform`` autorizado a leer la hora del sistema
(``datetime.now``, ``time.time``, ``time.monotonic``...); el resto recibe un ``Clock``.
La regla ``TID251`` de ruff (``pyproject.toml``, ``banned-api``) lo verifica.
"""
