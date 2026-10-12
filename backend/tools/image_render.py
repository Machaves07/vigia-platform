"""Render del acta dentro de la imagen, sin red (TASK-217; NFR-GOB-05, 32 y 69; LC-GOB-08).

Corre **dentro** de la imagen que se prueba, montado de solo lectura (no forma parte de la
imagen), con la raíz de solo lectura, ``/tmp`` aparte, sin capacidades y **sin red**::

    docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --network none \\
        -v tools/image_render.py:/boot/image_render.py:ro <imagen> python /boot/image_render.py

``tools/run_boot_check.py`` lo lanza así cuando la imagen se prueba a sí misma (el trabajo
«imagen arm64» de ``ci.yml``). Usa solo el código de la imagen: ``RecordDocumentRenderer`` en el
pool de CPU sobre un acta sintética validada por ``CommissioningRecordOut`` con 8 estándares
(32 filas de 3 pases), 8 cámaras, los cuatro tramos y una aceptación con ``<`` y ``&``. La
imagen arm64 corre emulada en ``ci.yml``: la matriz máxima (128 filas) tarda allí más de dos
minutos, y su tamaño y su tiempo ya los prueban la prueba unitaria y el banco de NFR-GOB-05.
Comprueba:

- las fuentes empaquetadas están en ``/app/resources/fonts/noto-sans`` y no hay fuentes del
  sistema;
- el usuario no es root;
- el PDF es completo (``%PDF-`` … ``%%EOF``) y pesa menos de 2 MB;
- el ``url_fetcher`` solo pidió las dos fuentes empaquetadas;
- ningún intento de abrir un socket de red (además de ``--network none``).

Sale con 0 si todo cumple y con 1 si no, nombrando el fallo. Solo datos generados.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Any, Final

from vigia_platform.catalog.adapters.http.commissioning_records import CommissioningRecordOut
from vigia_platform.catalog.adapters.rendering.record_document import (
    FONT_DIRECTORY,
    FONT_FILES,
    DocumentTimedOut,
    PackagedFontFetcher,
    RecordDocumentRenderer,
    render_pdf,
)
from vigia_platform.catalog.domain.enums import StepKind
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.cpu_pool import CpuPool

MAX_DOCUMENT_BYTES: Final = 2 * 1024 * 1024
ROWS: Final = 32
"""Filas de la matriz del acta sintética (8 estándares por 4 posturas)."""
RENDER_TIMEOUT_SECONDS: Final = 300.0
"""Tope de esta comprobación: la imagen arm64 corre emulada (QEMU) en ``ci.yml`` y en un equipo
x86, donde el render tarda mucho más que en producción. El tope de 10 s y el objetivo de 3 s de
NFR-GOB-05 los prueban ``tests/unit/catalog/test_record_document.py``, la prueba de la ruta y el
banco; aquí se comprueba que las bibliotecas nativas, las fuentes y el aislamiento de red
funcionan dentro de la imagen."""
SYSTEM_FONT_DIRECTORIES: Final = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"))
_NAMESPACE: Final = uuid.UUID("3f0d6a3e-2b8f-4c1d-9b61-0e6c1f5a7d22")


def _id(*parts: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, "/".join(str(part) for part in parts)))


def synthetic_record() -> dict[str, Any]:
    """El cuerpo de ``GET /commissioning-records/{id}`` de un acta sintética (32 filas)."""
    cameras = [_id("camera", index) for index in range(8)]
    zone = _id("zone")
    steps = [{"step_kind": kind.value, "duration_ms": 60_000} for kind in StepKind]
    tranche = {"median_ms": 150, "p95_ms": 300, "max_ms": 450, "repetitions": 100}
    body = {
        "commissioning_record_id": _id("record"),
        "session_id": _id("session"),
        "zone_id": zone,
        "catalog_version": 1,
        "kind": "initial",
        "passes_per_cell": 3,
        "matrix_results": [
            {
                "row_id": _id("row", row),
                "detected": 3,
                "missed": 0,
                "false_alarms": 0,
                "unverifiable_evidence_refs": [_id("evidence", row, n) for n in range(3)],
            }
            for row in range(ROWS)
        ],
        "false_negatives_total": 0,
        "false_alarm_rate_observed": 0.0,
        "false_alarm_threshold": 0.0,
        "false_alarm_acceptance": {
            "reason_es": "Prueba de imagen: altura < 2 m & sin red",
            "accepted_by": _id("signer"),
            "accepted_at": "2026-10-07T10:15:00.000Z",
        },
        "latency": {
            "node_tranche": {**tranche, "measured_by": "installer"},
            "platform_tranche": {**tranche, "measured_by": "platform"},
            "exposure_tranche": {**tranche, "measured_by": "browser"},
            "served_tranche": {**tranche, "measured_by": "platform"},
            "not_measured": [],
            "indicative_sum_median_ms": 600,
            "indicative_sum_p95_ms": 1200,
            "indicative": True,
            "repetitions_counted": 100,
        },
        "cameras_measured": [
            {"camera_id": camera, "measured_fps": 15.0, "declared_min_fps": 10.0}
            for camera in cameras
        ],
        "occlusion_summary": [
            {"camera_id": camera, "verification": "declared", "test_id": _id("test", camera)}
            for camera in cameras
        ],
        "total_duration_ms": 60_000 * len(steps),
        "steps_summary": steps,
        "installer_measurements": {
            "beacon_latency_ms_p95": 180,
            "measured_by": "installer",
            "baselines": [
                {"camera_id": camera, "zone_id": zone, "captured_at": "2026-10-01T08:00:00.000Z"}
                for camera in cameras
            ],
        },
        "signatures": [
            {
                "user_id": _id("signer"),
                "role_in_use": "coordinator_sst",
                "signed_at": "2026-10-07T10:20:00.000Z",
            }
        ],
        "closed_at": "2026-10-07T10:30:00.000Z",
    }
    record: dict[str, Any] = CommissioningRecordOut.model_validate_json(
        json.dumps(body)
    ).model_dump(mode="json")
    return record


class _NetworkAttempt(OSError):
    pass


def _forbid_network(attempts: list[str]) -> None:
    original = socket.socket

    class Guarded(original):  # type: ignore[misc, valid-type]
        def __init__(self, family: int = -1, *args: Any, **kwargs: Any) -> None:
            if family in (socket.AF_INET, socket.AF_INET6):
                attempts.append(f"socket({family})")
                raise _NetworkAttempt("red prohibida en el render del acta")
            super().__init__(family, *args, **kwargs)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append("resolución o conexión")
        raise _NetworkAttempt("red prohibida en el render del acta")

    socket.socket = Guarded  # type: ignore[misc]
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def check() -> list[str]:
    """Los fallos encontrados (vacío si el render cumple)."""
    problems: list[str] = []
    for name in FONT_FILES:
        if not (FONT_DIRECTORY / name).is_file():
            problems.append(f"falta la fuente empaquetada {FONT_DIRECTORY / name}")
    if not (FONT_DIRECTORY / "OFL.txt").is_file():
        problems.append("falta OFL.txt junto a las fuentes")
    for directory in SYSTEM_FONT_DIRECTORIES:
        if directory.exists() and any(path.is_file() for path in directory.rglob("*")):
            problems.append(f"hay fuentes del sistema en {directory}")
    if os.getuid() == 0:
        problems.append("el render corre como root")
    attempts: list[str] = []
    _forbid_network(attempts)
    fetcher = PackagedFontFetcher()
    clock = SystemClock()
    pool = CpuPool(clock)
    started = clock.monotonic()
    try:
        renderer = RecordDocumentRenderer(
            pool=pool,
            timeout_seconds=RENDER_TIMEOUT_SECONDS,
            pdf=lambda html: render_pdf(html, fetcher),
        )
        pdf = asyncio.run(renderer.render(synthetic_record()))
    except DocumentTimedOut:
        problems.append(f"el render no terminó en {RENDER_TIMEOUT_SECONDS:.0f} s")
        return problems
    finally:
        pool.shutdown(wait=False)
    elapsed = clock.monotonic() - started
    if not pdf.startswith(b"%PDF-") or not pdf.rstrip().endswith(b"%%EOF"):
        problems.append("el documento no es un PDF completo")
    if not 0 < len(pdf) < MAX_DOCUMENT_BYTES:
        problems.append(f"el documento pesa {len(pdf)} bytes (máximo 2 MB)")
    expected = {(FONT_DIRECTORY / name).as_uri() for name in FONT_FILES}
    if set(fetcher.requested) != expected:
        problems.append(f"peticiones de recursos inesperadas: {fetcher.requested}")
    if attempts:
        problems.append(f"intentos de red durante el render: {attempts}")
    print(
        f"image_render: acta de {ROWS} filas en {len(pdf)} bytes y {elapsed:.1f} s"
        f" ({os.uname().machine}); recursos pedidos:"
        f" {sorted(Path(url).name for url in fetcher.requested)}; intentos de red: {len(attempts)}"
    )
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(f"image_render: FALLA: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("image_render: el acta se renderiza en la imagen sin red y solo con sus fuentes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
