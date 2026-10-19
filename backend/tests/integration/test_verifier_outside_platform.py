"""El verificador fuera de la plataforma (TASK-346; NFR-LAZ-53; NFR-NUC-53; escenario L-6).

Lo que hace un tercero con un paquete real del compositor de TASK-318: saca **solo**
``vigia_verify.py`` del paquete a un directorio vacío y lo ejecuta con un intérprete Python 3.10
**sin la aplicación ni ningún paquete instalado**: un contenedor ``python:3.10-slim`` (fijado por
digest) sin red, con ``-I -S`` (sin ``site``), el verificador y el paquete montados en solo
lectura. El archivo que viaja en el paquete es el versionado (``load_verifier_bundle``), solo
importa la biblioteca estándar y su versión es la que declara el manifiesto.

- Sobre el paquete real responde ``intact`` (código 0) y extrae lo verificado.
- Con un bit alterado en un artefacto responde ``broken`` (código 1) con ese artefacto, su
  volumen y su desplazamiento, y no extrae nada.

Se compara por código de salida y por los campos estructurados del resultado (VIG-194). Solo
datos generados.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, assume, given, settings

from tests.export_contents_support import Composed
from tests.properties.laz.strategies.export import ExportWorld, exported_packages
from tests.verifier_bundle_support import (
    compose_to_directory,
    flip_bit,
    rewrite_volume,
    seal_world,
    volume_paths,
)
from vigia_platform.accreditation.verifier_bundle import (
    VERIFIER_PATH,
    VERIFIER_VERSION,
    embedded_version,
    load_verifier_bundle,
)

pytestmark = pytest.mark.integration

PYTHON_310_IMAGE: Final = (
    "python:3.10-slim@sha256:6ff506466f8b1e981719b468cc0023b8ef000b983cdec0eafe119d01f379e31d"
)
"""Intérprete limpio de Python 3.10 (``docker buildx imagetools inspect python:3.10-slim``)."""
_VERSION_LINE: Final = re.compile(r"Python 3\.10\.[0-9]+")
_SETTINGS: Any = settings(
    max_examples=1,
    deadline=None,
    database=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.function_scoped_fixture,
    ],
)


def _run_python_310(
    tercero: Path, package: Path, result: Path, *extra: str
) -> tuple[int, str, dict[str, Any]]:
    """``vigia_verify.py`` de ``tercero`` sobre ``package`` en Python 3.10 limpio y sin red."""
    from testcontainers.core.container import DockerContainer

    script = (
        "python --version && exec python -I -S /tercero/vigia_verify.py /paquete"
        " --out /resultado/resultado.json " + " ".join(extra)
    )
    container = (
        DockerContainer(PYTHON_310_IMAGE)
        .with_volume_mapping(str(tercero), "/tercero", "ro")
        .with_volume_mapping(str(package), "/paquete", "ro")
        .with_volume_mapping(str(result), "/resultado", "rw")
        .with_kwargs(
            user=f"{os.getuid()}:{os.getgid()}", network_disabled=True, working_dir="/resultado"
        )
        .with_command(["sh", "-c", script])
    )
    container.start()
    try:
        wrapped = container.get_wrapped_container()
        status = int(wrapped.wait(timeout=300)["StatusCode"])
        logs = wrapped.logs().decode("utf-8")
    finally:
        container.stop()
    sys.stdout.write(logs)
    document: dict[str, Any] = json.loads((result / "resultado.json").read_text("utf-8"))
    return status, logs, document


def _extract_verifier(package: Path, tercero: Path) -> bytes:
    """Lo único que el tercero saca a mano del paquete: ``vigia_verify.py``."""
    for volume in volume_paths(package):
        with zipfile.ZipFile(io.BytesIO(volume.read_bytes())) as archive:
            if VERIFIER_PATH in archive.namelist():
                data = archive.read(VERIFIER_PATH)
                (tercero / VERIFIER_PATH).write_bytes(data)
                return data
    raise AssertionError("el paquete no lleva vigia_verify.py")


def _with_records_and_audit(composed: Composed) -> bool:
    """El paquete lleva registros con contenido y entradas de auditoría que comprobar."""
    records = any(
        "content" in record
        for path in composed.artifacts
        if path.startswith("findings/")
        for finding in composed.json(path)["findings"]
        for record in finding["records"]
    )
    return records and bool(composed.json("audit/audit-log.json")["entries"])


@_SETTINGS
@given(world=exported_packages(max_zones=2))
def test_the_packaged_verifier_runs_on_python_310_without_the_platform(
    world: ExportWorld,
) -> None:
    sealed = seal_world(world)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        package, tercero = root / "paquete", root / "tercero"
        tercero.mkdir()
        composed = compose_to_directory(sealed, package)
        assume(_with_records_and_audit(composed))

        data = _extract_verifier(package, tercero)
        bundle = load_verifier_bundle()
        assert data == bundle.data
        assert embedded_version(data) == composed.manifest["verifier_version"] == VERIFIER_VERSION
        imported = {
            (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
            for node in ast.walk(ast.parse(data))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        assert {name.split(".")[0] for name in imported} <= set(sys.stdlib_module_names)

        intact = root / "intacto"
        intact.mkdir()
        status, logs, document = _run_python_310(tercero, package, intact, "--extract extraido")
        assert _VERSION_LINE.search(logs) is not None, logs
        assert status == 0, logs
        assert document["status"] == "intact"
        assert document["broken"] is None
        assert document["artifacts_verified"] == len(composed.manifest["artifacts"])
        assert document["checkpoints_verified"] == 3
        assert document["records_checked"] > 0
        assert document["audit_entries_checked"] > 0
        assert (intact / "extraido" / "manifest.json").is_file()

        # Un bit alterado en el primer archivo de hallazgos, manifiesto intacto.
        altered = root / "alterado"
        shutil.copytree(package, altered)
        target = next(path for path in sorted(composed.artifacts) if path.startswith("findings/"))
        expected_volume = ""
        for volume in volume_paths(altered):
            content = volume.read_bytes()
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if target not in archive.namelist():
                    continue
            changed = rewrite_volume(content, {target: flip_bit(composed.artifacts[target], 13)})
            volume.write_bytes(changed)
            with zipfile.ZipFile(io.BytesIO(changed)) as archive:
                expected_offset = archive.getinfo(target).header_offset
            expected_volume = volume.name
        broken_dir = root / "roto"
        broken_dir.mkdir()
        status, logs, document = _run_python_310(tercero, altered, broken_dir, "--extract extraido")
        assert status == 1, logs
        assert document["status"] == "broken"
        broken = document["broken"]
        assert broken["reason"] == "artifact_hash_mismatch"
        assert (broken["volume"], broken["artifact"], broken["offset"]) == (
            expected_volume,
            target,
            expected_offset,
        )
        assert sorted(child.name for child in broken_dir.iterdir()) == ["resultado.json"]
