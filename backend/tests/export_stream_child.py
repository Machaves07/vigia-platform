"""Proceso hijo del flujo de exportación contra LocalStack (TASK-345; PR-LAZ-49; NFR-LAZ-49).

``python -m tests.export_stream_child '<json>'`` compone el paquete sintético determinista
(``tests.export_stream_support.StreamingComposer``) con ``ExportStream`` sobre ``S3Storage``:

- ``mode = "kill"``: tras cada artefacto escribe ``{"progress": p}``; al llegar a
  ``kill_percent`` escribe ``{"paused": …}`` con la subida guardada y **se queda esperando** en la
  entrada estándar. El padre comprueba el almacén en ese instante y lo mata con ``SIGKILL`` (una
  señal que no se captura: no corre ningún ``finally`` ni se aborta nada). El **gancho de
  progreso** solo existe en este proceso de prueba (LC-LAZ-42).
- ``mode = "measure"``: compone entero y escribe el resultado con la memoria máxima del proceso
  (``ru_maxrss``) y el uso máximo de su directorio temporal (``TMPDIR``, que el padre crea vacío),
  medido tras cada artefacto.

Una línea JSON por mensaje en la salida estándar. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import sys
import tempfile
from pathlib import Path
from typing import Any

from tests.export_stream_support import (
    RecordingSink,
    StreamingComposer,
    job_from_json,
    scope_from_json,
)
from vigia_platform.accreditation.export.stream import ExportStream, ExportStreamSettings
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.storage import (
    AddressingStyle,
    S3Storage,
    StorageCredentials,
    StorageSettings,
)


def _emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _tmp_usage() -> int:
    root = Path(tempfile.gettempdir())
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


async def _main(spec: dict[str, Any]) -> None:
    storage = S3Storage(
        StorageSettings(
            bucket=spec["bucket"],
            endpoint_url=spec["endpoint"],
            region="us-east-1",
            addressing_style=AddressingStyle.PATH,
            read_timeout_seconds=60.0,
            credentials=StorageCredentials("test", "test"),
        ),
        SystemClock(),
    )
    stream = ExportStream(store=storage, settings=ExportStreamSettings(**spec["settings"]))
    composer = StreamingComposer(stream, scope_from_json(spec["scope"]))
    kill_percent: int | None = spec.get("kill_percent")
    tmp_max = _tmp_usage()
    sink = RecordingSink()

    async def hook(percent: int) -> None:
        nonlocal tmp_max
        tmp_max = max(tmp_max, _tmp_usage())
        _emit({"progress": percent})
        if kill_percent is not None and percent >= kill_percent:
            _emit({"paused": percent, "upload_id": sink.uploads[0]})
            # Espera a la señal del padre; no hay ``sleep`` ni tope de pared.
            await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
            raise SystemExit("el padre no mató el proceso")

    sink.hook = hook
    package = await composer.compose(job_from_json(spec["job"]), sink)
    result = composer.results[0]
    _emit(
        {
            "result": {
                "archive_sha256": package.archive_sha256,
                "document_sha256": package.document_sha256,
                "size_bytes": package.size_bytes,
                "volumes": [volume.to_json() for volume in result.stored.volumes],
                "manifest": composer.manifests[0],
            },
            "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "tmp_max_bytes": max(tmp_max, _tmp_usage()),
            "tmpdir": tempfile.gettempdir(),
            "pid": os.getpid(),
        }
    )


if __name__ == "__main__":
    asyncio.run(_main(json.loads(sys.argv[1])))
