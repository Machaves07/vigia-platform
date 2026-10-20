"""El catálogo del arnés de resiliencia arranca contra la base que deja ``gob_platform``.

``gob_platform`` sincroniza los eventos de todas las ``registered_units()``; el arnés de
resiliencia (``resilience_catalog``) arma el suyo aparte. Si una unidad nueva no entra en el del
arnés, la API y el worker de FS-GOB-03/08 no arrancan (``OutboxStartupError``) y solo lo veía el
nightly (18 al 20-10, tras VIG-201). Esta prueba lo trae al CI de los PR, sin Docker.
"""

from __future__ import annotations

import asyncio

from tests.resilience.processes import EffectHandler, resilience_catalog
from tests.worker_support import ProbeTask
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.outbox.registries import InMemoryOutboxCatalogStore, OutboxCatalog
from vigia_platform.shared.runtime.units import registered_units


def test_resilience_catalog_starts_against_the_full_platform_catalog() -> None:
    async def run() -> None:
        store = InMemoryOutboxCatalogStore()
        full = OutboxCatalog()
        for unit in registered_units():
            unit.event_types(full.event_types)
        await full.synchronize(store, SystemClock())
        await resilience_catalog(ProbeTask(), EffectHandler("api", {})).synchronize(
            store, SystemClock()
        )

    asyncio.run(run())
