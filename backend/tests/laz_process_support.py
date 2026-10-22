"""U-04 cargado en los procesos del arnés de dos procesos (TASK-353; PAT-LAZ-ESC-05; NFR-NUC-06).

``tests/resilience/api_process.py`` y ``tests/resilience/worker_process.py`` cargan U-03; con
``VIGIA_TEST_U04`` cargan además **U-04** (``loop``, ``accreditation`` y ``notifications``):

- ``add_u04``: los consumidores y las tareas periódicas de U-04 con los registradores reales y
  sus servicios (``UnitServices`` del proceso), así que cada consumidor sale con **su mamparo**
  (``loop.worker.bulkheads``, ``export_worker`` con concurrencia 1). Sus eventos ya los pone
  ``gob_support.add_u03``. La API los registra igual (no despacha): su bandeja crea las entregas
  de los eventos que publica, como ``vigia-api``.
- ``u04_api_state``: el estado de las rutas de U-04 en ``app.state``.
- **Gancho de exportación**, solo en el proceso de prueba (LC-LAZ-42): el manejador de
  ``export_worker`` es el ``ExportWorker`` real (arrendamiento, cierre ``ready`` con
  ``export_package`` y ``export_produced``, alarma) con un compositor que **tarda**
  ``VIGIA_TEST_EXPORT_SECONDS`` y anota en ``VIGIA_TEST_EXPORT_LOG`` (una línea JSON por suceso,
  con el proceso, la organización, la exportación y la hora) el comienzo y el final de cada
  composición. Así la prueba ve qué compuso cada proceso y cuándo, sin depender del almacén. El
  mamparo, el despachador y el arrendamiento son los de producción; el código de la plataforma no
  tiene ningún gancho.

Solo datos generados.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from collections.abc import Mapping
from typing import Any, Final

from tests.export_support import (
    CountingCheckpoints,
    RecordingAlarm,
    RecordingUploads,
    synthetic_package,
)
from tests.resilience.gob_support import U02_UNIT_NAMES, U03_UNIT_NAMES
from vigia_platform.accreditation.export.composer_port import (
    ExportJob,
    ExportPackage,
    ExportSink,
)
from vigia_platform.accreditation.export.lease import SqlExportLeaseStore
from vigia_platform.accreditation.export.worker import EXPORT_WORKER, ExportWorker
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.outbox.registries import Consumer, ConsumerRegistry, OutboxCatalog
from vigia_platform.shared.runtime.units import (
    PlatformUnit,
    UnitServices,
    api_state,
    registered_units,
)

__all__ = [
    "EXPORT_LOG_VARIABLE",
    "EXPORT_SECONDS_VARIABLE",
    "U04_VARIABLE",
    "add_u04",
    "export_hook",
    "u04_api_state",
    "u04_units",
]

U04_VARIABLE: Final = "VIGIA_TEST_U04"
EXPORT_LOG_VARIABLE: Final = "VIGIA_TEST_EXPORT_LOG"
EXPORT_SECONDS_VARIABLE: Final = "VIGIA_TEST_EXPORT_SECONDS"

_CLOCK: Final = SystemClock()


def u04_units() -> tuple[PlatformUnit, ...]:
    """Las unidades de U-04 del registro (las que no son de U-02 ni de U-03)."""
    return tuple(
        unit for unit in registered_units() if unit.name not in U02_UNIT_NAMES | U03_UNIT_NAMES
    )


def u04_api_state(services: UnitServices) -> dict[str, object]:
    return api_state(u04_units(), services)


class _ExportSwap(ConsumerRegistry):
    """El registro del catálogo con el manejador de ``export_worker`` cambiado por el del gancho;
    el mamparo (``admission``) que le puso ``bulkheaded_registry`` se conserva."""

    def __init__(self, inner: ConsumerRegistry, handler: Any) -> None:
        super().__init__()
        self._inner = inner
        self._handler = handler

    def register(self, consumer: Consumer) -> Consumer:
        if consumer.consumer_name == EXPORT_WORKER:
            consumer = dataclasses.replace(consumer, handler=self._handler)
        return self._inner.register(consumer)

    def get(self, consumer_name: str) -> Consumer | None:
        return self._inner.get(consumer_name)

    def consumers(self) -> tuple[Consumer, ...]:
        return self._inner.consumers()

    def subscribers(self, event_name: str) -> tuple[str, ...]:
        return self._inner.subscribers(event_name)

    def seal(self) -> None:
        self._inner.seal()

    @property
    def sealed(self) -> bool:
        return self._inner.sealed


def add_u04(
    catalog: OutboxCatalog, services: UnitServices, *, export_handler: Any = None
) -> OutboxCatalog:
    """Los consumidores y las tareas de U-04 en ``catalog`` (sin sellar), con
    ``export_handler`` como manejador de ``export_worker`` si se da."""
    consumers: ConsumerRegistry = catalog.consumers
    if export_handler is not None:
        consumers = _ExportSwap(catalog.consumers, export_handler)
    for unit in u04_units():
        unit.consumers(consumers, services)
        unit.periodic_tasks(catalog.periodic_tasks, services)
    return catalog


class LoggedComposer:
    """Un compositor que tarda ``seconds`` y anota el comienzo y el final en ``log_path``."""

    def __init__(self, log_path: str, seconds: float, owner: str) -> None:
        self._log_path = log_path
        self._seconds = seconds
        self._owner = owner

    def _note(self, event: str, job: ExportJob) -> None:
        line = {
            "event": event,
            "owner": self._owner,
            "pid": os.getpid(),
            "organization_id": str(job.organization_id),
            "export_id": str(job.export_id),
            "at": _CLOCK.now().isoformat(),
        }
        with open(self._log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps(line) + "\n")
            log.flush()
            os.fsync(log.fileno())

    async def compose(self, job: ExportJob, sink: ExportSink) -> ExportPackage:
        self._note("start", job)
        await asyncio.sleep(self._seconds)  # la composición; el acompañante renueva mientras
        await sink.artifact_finished("document", 1, 1)
        self._note("end", job)
        return synthetic_package(job)


def export_hook(environ: Mapping[str, str], services: UnitServices, owner: str) -> Any:
    """El ``ExportWorker`` real con el compositor anotado, o ``None`` sin el gancho."""
    log_path = environ.get(EXPORT_LOG_VARIABLE)
    if not log_path:
        return None
    return ExportWorker(
        leases=SqlExportLeaseStore(database=services.database, writer=services.writer),  # type: ignore[arg-type]
        checkpoints=CountingCheckpoints(),
        uploads=RecordingUploads(),
        composer=LoggedComposer(log_path, float(environ[EXPORT_SECONDS_VARIABLE]), owner),
        alarm=RecordingAlarm(),
        clock=services.clock,
        owner=owner,
    )
