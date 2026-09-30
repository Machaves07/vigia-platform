"""Fixtures de las propiedades que necesitan PostgreSQL 16 real (PR-NUC-18, PR-NUC-52).

Reexporta ``postgres_endpoint`` de ``tests/integration/conftest.py``: solo se levanta el
contenedor si una propiedad lo pide, y esas propiedades llevan la marca ``integration``.
"""

from __future__ import annotations

from tests.integration.conftest import postgres_endpoint

__all__ = ["postgres_endpoint"]
