"""Carga de exportación de U-04 (TASK-353; NFR-LAZ-49 y 10; PAT-LAZ-REN-03 y ESC-05;
infraestructura §6.2; A-65).

Valida una decisión de diseño, no un algoritmo: **el paquete se escribe en flujo**, así que la
memoria máxima del proceso no crece con su tamaño. Por perfil (``tests.load.laz_profiles``), una
zona y 31 días a 77 hallazgos por día (2 387 hallazgos con un clip cada uno) se exportan
``with_clips`` dos veces, con un paquete pequeño y otro diez veces mayor (``nightly``: 1 GB y
10 GB), cada una en su organización:

1. La solicitud entra por la ruta real (``POST /exports``, ``export_stack``) y la reserva de clips
   sintéticos se sube a LocalStack.
2. Un **proceso hijo** (``tests.load.export_load_child``) ejecuta el intento del consumidor real
   ``export_worker`` con el compositor real y el flujo real sobre LocalStack (partes de 16 MiB y
   volúmenes de 2 GB) y mide la memoria máxima, el ``/tmp`` máximo y la duración; después repite
   la composición del mismo trabajo hacia un almacén que solo cuenta.
3. **Lo funcional bloquea**: la solicitud ``ready`` con su ``export_package``; el verificador
   endurecido del paquete (``export_verifier.verify_export``) responde ``intact``, anclado por la
   firma del manifiesto, leyendo los volúmenes **por rangos** desde LocalStack (nunca se copian a
   disco); el manifiesto y el ``archive_sha256`` de la segunda corrida son los mismos; cada volumen
   no pasa de 2 GB y sube en partes de 16 MiB (``PartsCount`` y la primera parte); ``/tmp`` nunca
   pasa de 32 MiB; y la **memoria que retiene el intento** (el máximo de Python vivo a la vez,
   ``tracemalloc``: trozos, partes, clips y búferes del ZIP) con el paquete grande no supera la
   del pequeño en más de ``MEMORY_GROWTH_LIMIT_KIB``. Es lo que falla si el paquete deja de
   escribirse en flujo (sonda: sin subir partes hasta cerrar el volumen, +460 MiB).
4. **Tendencia** (A-65): duración, memoria máxima del proceso (RSS) y su crecimiento entre los dos
   paquetes, memoria de la composición frente al presupuesto de 512 MB por encima de la base y
   tamaño del paquete van al informe ``export-load-<perfil>.json`` de ``VIGIA_LOAD_REPORT_DIR``
   (el artefacto ``informe-carga`` de ``nightly``) y a la salida. El RSS suma lo nativo y lo que
   el asignador no devuelve: con el número de hallazgos fijo, el paquete diez veces mayor tiene
   clips diez veces mayores, y cada clip se lee entero (``BlobSource.clip_bytes``), así que el RSS
   máximo sigue al tamaño del clip y no al del paquete (primera medición, 2026-10-21: 1 GB 320 MiB,
   10 GB 441 MiB, con la memoria de Python plana).

``ci`` corre en el check «backend (carga ci)»; ``small`` en local (``-k small``); ``nightly``
solo con ``--hypothesis-profile=nightly`` (trabajo ``carga`` de ``nightly.yml``).

Solo datos generados.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest

from tests.export_support import ExportStack, export_body, export_stack
from tests.integration.conftest import LocalStackEndpoint, PostgresEndpoint, versioned_bucket
from tests.load.export_load_child import ClipPool
from tests.load.laz_profiles import (
    ABSOLUTE_MEMORY_BUDGET_MIB,
    EXPORT_PROFILES,
    MEMORY_GROWTH_LIMIT_KIB,
    TMP_LIMIT_BYTES,
    ExportLoadProfile,
)
from tests.load.report import report_directory
from vigia_platform.accreditation.export.volumes import (
    PART_SIZE,
    VOLUME_MAX_BYTES,
    ExportLocation,
    StoredObjects,
)
from vigia_platform.accreditation.export_verifier import verify_export
from vigia_platform.ledger.canonical import canonical_bytes_sync

pytestmark = pytest.mark.integration

BACKEND: Final = Path(__file__).resolve().parents[2]
CHILD: Final = "tests.load.export_load_child"
RANGE_BLOCK: Final = 16 * 1024 * 1024
"""Bloque de cada lectura por rangos del verificador (una petición a LocalStack por bloque)."""


@pytest.fixture(scope="module")
def stack(postgres_endpoint: PostgresEndpoint) -> Iterator[ExportStack]:
    with export_stack(postgres_endpoint, "export_load") as built:
        yield built


@pytest.fixture(scope="module")
def s3_bucket(localstack_endpoint: LocalStackEndpoint) -> Iterator[tuple[LocalStackEndpoint, str]]:
    with versioned_bucket(localstack_endpoint.aws_client("s3"), "vigia-export-load") as name:
        yield localstack_endpoint, name


# --- El verificador por rangos ----------------------------------------------------------------


class _RangeReader:
    """Un volumen leído por rangos de ``RANGE_BLOCK`` (``zipfile`` y el verificador piden
    ``seek``, ``tell`` y ``read``); guarda solo el último bloque."""

    def __init__(self, s3: Any, bucket: str, key: str, version_id: str, size: int) -> None:
        self._s3, self._bucket, self._key, self._version = s3, bucket, key, version_id
        self._size = size
        self._position = 0
        self._block_start = -1
        self._block = b""
        self.requests = 0

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}[whence]
        self._position = base + offset
        return self._position

    def _load(self, start: int) -> None:
        end = min(self._size, start + RANGE_BLOCK) - 1
        response = self._s3.get_object(
            Bucket=self._bucket,
            Key=self._key,
            VersionId=self._version,
            Range=f"bytes={start}-{end}",
        )
        self._block = response["Body"].read()
        self._block_start = start
        self.requests += 1

    def read(self, size: int = -1) -> bytes:
        end = self._size if size < 0 else min(self._size, self._position + size)
        pieces: list[bytes] = []
        while self._position < end:
            start = self._position - self._position % RANGE_BLOCK
            if start != self._block_start:
                self._load(start)
            offset = self._position - start
            piece = self._block[offset : offset + end - self._position]
            if not piece:
                break
            pieces.append(piece)
            self._position += len(piece)
        return b"".join(pieces)

    def close(self) -> None:
        self._block = b""

    def __enter__(self) -> _RangeReader:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _RangeVolume:
    """Lo que ``find_volumes`` y ``_Package`` usan de una ruta: un volumen en LocalStack."""

    def __init__(self, s3: Any, bucket: str, key: str, version_id: str, size: int) -> None:
        self.name = key.rsplit("/", 1)[-1]
        self._args = (s3, bucket, key, version_id, size)
        self.readers: list[_RangeReader] = []

    def is_symlink(self) -> bool:
        return False

    def is_dir(self) -> bool:
        return False

    def is_file(self) -> bool:
        return True

    def open(self, mode: str = "rb") -> _RangeReader:
        assert mode == "rb"
        reader = _RangeReader(*self._args)
        self.readers.append(reader)
        return reader


class _RangePackage:
    """El directorio del paquete: sus ``vol-NN.zip`` en LocalStack."""

    name = "paquete"

    def __init__(self, volumes: list[_RangeVolume]) -> None:
        self._volumes = {volume.name: volume for volume in volumes}

    def __str__(self) -> str:
        return "s3://paquete"

    def is_symlink(self) -> bool:
        return False

    def is_dir(self) -> bool:
        return True

    def is_file(self) -> bool:
        return False

    def iterdir(self) -> Iterator[_RangeVolume]:
        return iter(self._volumes.values())

    def __truediv__(self, name: str) -> _RangeVolume:
        return self._volumes[name]

    def requests(self) -> int:
        return sum(
            reader.requests for volume in self._volumes.values() for reader in volume.readers
        )


# --- Una exportación medida -------------------------------------------------------------------


def _delete_prefix(s3: Any, bucket: str, prefix: str) -> None:
    """Borra todas las versiones bajo ``prefix`` (libera el disco de LocalStack entre corridas)."""
    for listing in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=prefix):
        for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
            s3.delete_object(Bucket=bucket, Key=item["Key"], VersionId=item["VersionId"])


def _seed_pool(s3: Any, bucket: str, prefix: str, size: int, count: int) -> ClipPool:
    keys, digests = [], []
    for index in range(count):
        data = hashlib.shake_256(f"{prefix}{index}".encode()).digest(size)
        key = f"{prefix}{index:03d}.mp4"
        s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="video/mp4")
        keys.append(key)
        digests.append(hashlib.sha256(data).hexdigest())
    return ClipPool(tuple(keys), tuple(digests), size)


def _child(spec: dict[str, Any], tmp: Path, timeout: float) -> dict[str, Any]:
    environment = {**os.environ, "TMPDIR": str(tmp), "PYTHONPATH": str(BACKEND)}
    finished = subprocess.run(
        [sys.executable, "-m", CHILD, json.dumps(spec)],
        cwd=BACKEND,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr[-4000:]
    (line,) = [line for line in finished.stdout.splitlines() if line.startswith("{")]
    result: dict[str, Any] = json.loads(line)
    assert result["tmpdir"] == str(tmp)
    return result


def _export(
    stack: ExportStack,
    bucket: tuple[LocalStackEndpoint, str],
    profile: ExportLoadProfile,
    package_bytes: int,
    tmp: Path,
) -> dict[str, Any]:
    endpoint, name = bucket
    s3 = endpoint.aws_client("s3")
    site = stack.site(plants=1, zones=1)
    plant = next(iter(site.plants))
    person = stack.member(site)
    start = stack.now() - timedelta(days=profile.days, hours=1)
    response = stack.request_export(
        person,
        export_body(
            plant, list(site.plants[plant]), start=start, days=profile.days, form="with_clips"
        ),
    )
    assert response.status_code == 202, response.text
    export_id = uuid.UUID(response.json()["export_id"])
    location = ExportLocation(site.organization_id, plant, export_id)
    pool_prefix = f"carga/clips/{export_id}/"
    pool = _seed_pool(s3, name, pool_prefix, profile.clip_bytes(package_bytes), profile.clip_pool)
    try:
        measured = _child(
            {
                "database_url": stack.authz.sessions.migrated.as_role("vigia_app").sqlalchemy_url,
                "provider_organization_id": str(stack.authz.provider_organization_id),
                "organization_id": str(site.organization_id),
                "export_id": str(export_id),
                "endpoint": endpoint.url,
                "bucket": name,
                "findings": profile.findings,
                "clips": {"keys": pool.keys, "sha256": pool.sha256, "size": pool.size},
            },
            tmp,
            profile.child_timeout_seconds,
        )
        row = stack.export_row(export_id)
        assert (row["state"], row["attempt_count"], row["progress_percent"]) == ("ready", 1, 100)
        package = stack.package_row(export_id)
        assert package is not None, "ready sin export_package"
        stored_json = package["stored_objects"]
        stored = StoredObjects.from_json(
            stored_json if not isinstance(stored_json, str) else json.loads(stored_json)
        )
        manifest_json = package["manifest"]
        manifest = (
            manifest_json if not isinstance(manifest_json, str) else json.loads(manifest_json)
        )
        rerun = measured["rerun"]
        # El mismo manifiesto y el mismo paquete en dos corridas del mismo trabajo (PR-LAZ-49).
        assert package["archive_sha256"] == rerun["archive_sha256"]
        assert (
            hashlib.sha256(canonical_bytes_sync(manifest)).hexdigest() == rerun["manifest_sha256"]
        )
        assert package["document_sha256"] == rerun["document_sha256"]
        assert (
            package["size_bytes"]
            == rerun["size_bytes"]
            == sum(volume.size_bytes for volume in stored.volumes)
        )
        clips = [item for item in manifest["artifacts"] if item["kind"] == "clip"]
        assert len(clips) == profile.findings == measured["clip_reads"]
        assert package["size_bytes"] >= profile.findings * pool.size
        # Volúmenes de 2 GB como mucho, subidos en partes de 16 MiB.
        parts = 0
        volumes: list[_RangeVolume] = []
        for volume in stored.volumes:
            assert volume.size_bytes <= VOLUME_MAX_BYTES and volume.version_id
            key = location.volume_key(volume.number)
            expected = math.ceil(volume.size_bytes / PART_SIZE)
            first = s3.head_object(Bucket=name, Key=key, VersionId=volume.version_id, PartNumber=1)
            assert first["PartsCount"] == expected
            assert first["ContentLength"] == min(PART_SIZE, volume.size_bytes)
            parts += expected
            volumes.append(_RangeVolume(s3, name, key, volume.version_id, volume.size_bytes))
        assert parts == rerun["parts"] - math.ceil(
            (stored.document.size_bytes if stored.document else 0) / PART_SIZE
        )
        # El verificador endurecido del paquete, por rangos: íntegro y anclado por la firma.
        directory = _RangePackage(volumes)
        report = verify_export(cast(Path, directory))
        assert report.failure is None, report.failure
        assert report.status == "intact" and report.anchored_by, report.status
        assert report.manifest_sha256 == rerun["manifest_sha256"]
        assert measured["tmp_max_bytes"] < TMP_LIMIT_BYTES, measured
        return {
            "package_bytes_target": package_bytes,
            "package_bytes": package["size_bytes"],
            "volumes": [(volume.number, volume.size_bytes) for volume in stored.volumes],
            "parts": parts,
            "clip_bytes": pool.size,
            "findings": profile.findings,
            "duration_seconds": measured["duration_seconds"],
            "world_seconds": measured["world_seconds"],
            "rerun_seconds": measured["rerun_seconds"],
            "base_rss_mib": round(measured["base_rss_kib"] / 1024, 1),
            "max_rss_mib": round(measured["max_rss_kib"] / 1024, 1),
            "composition_mib": round(
                (measured["max_rss_kib"] - measured["base_rss_kib"]) / 1024, 1
            ),
            "max_rss_kib": measured["max_rss_kib"],
            "python_peak_mib": round(measured["python_peak_kib"] / 1024, 1),
            "python_peak_kib": measured["python_peak_kib"],
            "tmp_max_bytes": measured["tmp_max_bytes"],
            "verifier": report.status,
            "verifier_range_requests": directory.requests(),
            "archive_sha256": package["archive_sha256"],
            "manifest_sha256": rerun["manifest_sha256"],
        }
    finally:
        _delete_prefix(s3, name, pool_prefix)
        _delete_prefix(s3, name, location.prefix)


def _run_profile(
    stack: ExportStack,
    bucket: tuple[LocalStackEndpoint, str],
    profile: ExportLoadProfile,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    small = _export(
        stack, bucket, profile, profile.small_bytes, tmp_path_factory.mktemp("tmp-pequeno")
    )
    large = _export(
        stack, bucket, profile, profile.large_bytes, tmp_path_factory.mktemp("tmp-grande")
    )
    retained_growth = large["python_peak_kib"] - small["python_peak_kib"]
    rss_growth = large["max_rss_kib"] - small["max_rss_kib"]
    budget = ABSOLUTE_MEMORY_BUDGET_MIB
    report = {
        "nfr": "NFR-LAZ-49",
        "profile": profile.name,
        "zone_days": profile.days,
        "findings_per_day": profile.findings_per_day,
        "part_size_bytes": PART_SIZE,
        "volume_max_bytes": VOLUME_MAX_BYTES,
        "small": small,
        "large": large,
        "retained_growth_mib": round(retained_growth / 1024, 1),
        "retained_growth_limit_mib": MEMORY_GROWTH_LIMIT_KIB // 1024,
        "tmp_limit_bytes": TMP_LIMIT_BYTES,
        "trend": {
            "rss_growth_mib": round(rss_growth / 1024, 1),
            "absolute_budget_mib": budget,
            "composition_mib": large["composition_mib"],
            "within_budget": large["composition_mib"] <= budget,
            "note": (
                "tendencia (A-65): el crecimiento del RSS y el presupuesto absoluto no deciden el"
                " resultado; el RSS incluye lo nativo y lo que el asignador no devuelve, que sigue"
                " al tamaño de cada clip (se lee entero), no al del paquete"
            ),
        },
    }
    directory = report_directory(tmp_path_factory.getbasetemp())
    path = directory / f"export-load-{profile.name}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"\nNFR-LAZ-49 ({profile.name}): {json.dumps(report, ensure_ascii=False)}\ninforme: {path}"
    )
    assert retained_growth <= MEMORY_GROWTH_LIMIT_KIB, report
    return report


def test_export_load_ci(
    stack: ExportStack,
    s3_bucket: tuple[LocalStackEndpoint, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _run_profile(stack, s3_bucket, EXPORT_PROFILES["ci"], tmp_path_factory)


def test_export_load_small(
    stack: ExportStack,
    s3_bucket: tuple[LocalStackEndpoint, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _run_profile(stack, s3_bucket, EXPORT_PROFILES["small"], tmp_path_factory)


@pytest.mark.nightly
def test_export_load_nightly_one_gb_against_ten_gb(
    stack: ExportStack,
    s3_bucket: tuple[LocalStackEndpoint, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _run_profile(stack, s3_bucket, EXPORT_PROFILES["nightly"], tmp_path_factory)
