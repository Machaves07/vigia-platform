"""Reloj inyectable (LC-NUC-32, PAT-NUC-RES-07): reexporta el ``Clock`` de U-01 (LC-06).

``SystemClock`` en producción y ``SimulatedClock`` en pruebas; ningún módulo de la plataforma
llama a la hora del sistema, recibe un ``Clock``. Es el único paquete de ``vigia_platform``
autorizado a leer la hora del sistema (``datetime.now``, ``time.time``, ``time.monotonic``...);
la regla ``TID251`` de ruff (``pyproject.toml``, ``banned-api``) lo verifica. El ``received_at``
de la cadena lo fija la base (BR-NUC-47), no este reloj.
"""

from vigia_contracts.clock import Clock, SimulatedClock, SystemClock

__all__ = ["Clock", "SimulatedClock", "SystemClock"]
