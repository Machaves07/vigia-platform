"""Un solo mundo de la plataforma para los dieciséis escenarios de abuso (TASK-140).

La aplicación completa contra PostgreSQL 16 real (``tests/platform_support.py``) se monta una vez
por paquete: cada escenario crea sus propias organizaciones, así que no se pisan.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.integration.conftest import PostgresEndpoint
from tests.platform_support import Platform, platform_world


@pytest.fixture(scope="package")
def _platform_world(postgres_endpoint: PostgresEndpoint) -> Iterator[Platform]:
    with platform_world(postgres_endpoint, "abuse") as world:
        yield world


@pytest.fixture
def platform(_platform_world: Platform) -> Platform:
    """El mundo, con la hora simulada en la de la base al empezar cada escenario."""
    _platform_world.resync()
    return _platform_world
