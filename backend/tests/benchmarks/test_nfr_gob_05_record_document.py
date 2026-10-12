"""Banco de NFR-GOB-05: documento legible del acta de la matriz máxima (TASK-217; LC-GOB-08).

Mide ``RecordDocumentRenderer.render`` tal como lo llama la ruta
``GET /commissioning-records/{id}/document``: plantilla HTML y PDF de WeasyPrint en el pool de
CPU (``shared.cpu_pool``, 4 hilos) con el tiempo de espera de 10 s, sobre la vista de un acta de la
**matriz máxima** (32 estándares por 4 posturas: 128 filas con 3 pases por celda y 3 evidencias sin
clip por fila, 8 cámaras, los cuatro tramos medidos, aceptación de falsas alarmas y cuatro firmas).
La lectura del acta estructurada es la de ``GET /commissioning-records/{id}``, con su propio
objetivo (300 ms p95, NFR-GOB-03): aquí solo cuenta la generación.

Objetivo ``[objetivo propio]`` de NFR-GOB-05: p95 ≤ 3 s; el informe dice si se cumple. Cada ronda
comprueba que el PDF está completo y pesa menos de 2 MB. 100 rondas de una iteración con el marco
de ``tests/benchmarks/conftest.py`` (mediana y p95 frente a ``baseline.json``). Solo corre con
``--hypothesis-profile=nightly``. Solo datos generados.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Final

import pytest

from tests.benchmarks.conftest import Measure
from tests.record_document_support import (
    MAX_CAMERAS,
    MAX_PASSES,
    MAX_STANDARDS,
    record_body,
)
from vigia_platform.catalog.adapters.rendering import RecordDocumentRenderer
from vigia_platform.shared.api.labels import PlatformLabels
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.cpu_pool import CPU_POOL_MAX_WORKERS, CpuPool

pytestmark = pytest.mark.nightly

OBJECTIVE_MS: Final = 3_000.0
"""NFR-GOB-05: documento legible del acta en ≤ 3 s p95 ``[objetivo propio]``."""
MAX_DOCUMENT_BYTES: Final = 2 * 1024 * 1024
NAME: Final = "gob_record_document"


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    created = asyncio.new_event_loop()
    yield created
    created.close()


@pytest.fixture
def pool() -> Iterator[CpuPool]:
    created = CpuPool(SystemClock(), max_workers=CPU_POOL_MAX_WORKERS)
    yield created
    created.shutdown(wait=True)


def test_nfr_gob_05_record_document_of_the_maximum_matrix(
    measure: Measure, loop: asyncio.AbstractEventLoop, pool: CpuPool
) -> None:
    record = record_body(MAX_STANDARDS, MAX_CAMERAS, MAX_PASSES, seed="banco")
    assert len(record["matrix_results"]) == 128
    renderer = RecordDocumentRenderer(pool=pool, labels=PlatformLabels.load())
    sizes: list[int] = []

    def generate() -> None:
        pdf = loop.run_until_complete(renderer.render(record))
        assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
        assert len(pdf) < MAX_DOCUMENT_BYTES
        sizes.append(len(pdf))

    result = measure(
        NAME,
        "Documento legible del acta de la matriz máxima (NFR-GOB-05)",
        generate,
        objective_ms=OBJECTIVE_MS,
        details={
            "matrix_rows": 128,
            "cameras": MAX_CAMERAS,
            "passes_per_cell": MAX_PASSES,
            "timeout_s": renderer.timeout_seconds,
            "pool_threads": pool.max_workers,
        },
    )
    result.details["max_document_bytes"] = max(sizes)
    assert result.p95 is not None
