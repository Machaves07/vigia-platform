"""El mundo de U-03 de las pruebas de ejemplo por historia (TASK-229; NFR-GOB-62; PBT-10).

``gob``: la aplicación completa con **todas** las unidades de ``platform_units()``, PostgreSQL y
LocalStack (``tests/gob_platform_support.py``), montada una vez por paquete y solo si alguna
prueba la pide; cada historia crea su propia organización.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.gob_platform_support import GobPlatform, gob_platform
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint


@pytest.fixture(scope="package")
def _gob_world(
    postgres_endpoint: PostgresEndpoint, localstack_endpoint: LocalStackEndpoint
) -> Iterator[GobPlatform]:
    with gob_platform(postgres_endpoint, localstack_endpoint, "examples_gob") as world:
        yield world


@pytest.fixture
def gob(_gob_world: GobPlatform) -> GobPlatform:
    """El mundo de U-03, con la hora simulada en la de la base al empezar cada historia."""
    _gob_world.resync()
    return _gob_world
