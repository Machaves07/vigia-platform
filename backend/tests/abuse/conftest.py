"""Un solo mundo de la plataforma para los escenarios de abuso (TASK-140 y TASK-229).

La aplicación completa contra PostgreSQL 16 real se monta una vez por paquete: cada escenario crea
sus propias organizaciones, así que no se pisan.

- ``platform``: el mundo de U-02 de los dieciséis escenarios N-1 a N-16
  (``tests/platform_support.py``);
- ``gob``: el de U-03 de los quince escenarios G-1 a G-15, con **todas** las unidades de
  ``platform_units()`` y LocalStack (``tests/gob_platform_support.py``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.gob_platform_support import GobPlatform, gob_platform
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint
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


@pytest.fixture(scope="package")
def _gob_world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[GobPlatform]:
    with gob_platform(postgres_endpoint, localstack_endpoint, "abuse_gob") as world:
        yield world


@pytest.fixture
def gob(_gob_world: GobPlatform) -> GobPlatform:
    """El mundo de U-03, con la hora simulada en la de la base al empezar cada escenario."""
    _gob_world.resync()
    return _gob_world
