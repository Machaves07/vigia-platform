"""``accreditation.export.stream`` y ``volumes`` sobre el almacén en memoria (TASK-345).

- El ZIP se escribe sobre un destino no reposicionable: cada entrada lleva descriptor de datos
  (bit 3) y ``zipfile`` lo abre y comprueba entero.
- Volúmenes: una entrada nunca se parte; al no caber abre el siguiente; el manifiesto va en
  ``vol-01`` aunque se escriba el último; una entrada mayor que un volumen se rechaza.
- Determinismo: dos composiciones del mismo trabajo dan los mismos bytes, el mismo
  ``archive_sha256`` y el mismo manifiesto, con cualquier número de intento.
- Ningún objeto existe antes de ``finish``; un fallo dentro del ``async with`` aborta todas las
  subidas; ``abort_previous_upload`` aborta la guardada y barre las demás de la exportación.
- Tamaños: la entrada no puede pasar ni quedarse corta de lo declarado; rutas cerradas.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import uuid
import zipfile
from typing import Any

import pytest

from tests.export_stream_support import (
    MIB,
    MemoryExportStore,
    RecordingSink,
    StreamingComposer,
    SyntheticArtifact,
    SyntheticScope,
    read_volume,
    synthetic_chunks,
    synthetic_job,
)
from vigia_platform.accreditation.export.stream import (
    Artifact,
    ExportStream,
    ExportStreamSettings,
)
from vigia_platform.accreditation.export.volumes import (
    MANIFEST_NAME,
    ExportLocation,
    StoredObjects,
    Volume,
    archive_sha256,
    volume_name,
)
from vigia_platform.ledger.canonical import canonical_bytes_sync

SMALL = ExportStreamSettings(
    part_size=5 * MIB, volume_max_bytes=400 * 1024, manifest_reserve_bytes=64 * 1024
)
SCOPE = SyntheticScope(
    (
        SyntheticArtifact("findings.json", "findings_json", 150 * 1024, True),
        SyntheticArtifact("clips/a.mp4", "clip", 200 * 1024),
        SyntheticArtifact("clips/b.mp4", "clip", 250 * 1024),
        SyntheticArtifact("coverage.json", "coverage", 10 * 1024, True),
    ),
    document_size=20 * 1024,
)


def _compose(
    store: MemoryExportStore, job: Any = None, settings: ExportStreamSettings = SMALL
) -> tuple[StreamingComposer, Any, RecordingSink]:
    composer = StreamingComposer(ExportStream(store=store, settings=settings), SCOPE)
    sink = RecordingSink()
    package = asyncio.run(composer.compose(job or synthetic_job(), sink))
    return composer, package, sink


def test_volumes_open_with_zipfile_and_use_data_descriptors() -> None:
    store = MemoryExportStore()
    job = synthetic_job()
    composer, package, sink = _compose(store, job)
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    result = composer.results[0]
    assert len(result.stored.volumes) >= 2
    names: list[str] = []
    for volume in result.stored.volumes:
        data = store.latest(location.volume_key(volume.number))
        assert hashlib.sha256(data).hexdigest() == volume.sha256
        assert len(data) == volume.size_bytes <= SMALL.volume_max_bytes
        with read_volume(data) as archive:
            assert archive.testzip() is None
            for info in archive.infolist():
                assert info.flag_bits & 0x08, "sin descriptor de datos"
                assert info.date_time == (1980, 1, 1, 0, 0, 0)
            names += archive.namelist()
    # El manifiesto, en el primer volumen y con los bytes canónicos de U-02.
    with read_volume(store.latest(location.volume_key(1))) as first:
        assert MANIFEST_NAME in first.namelist()
        assert first.read(MANIFEST_NAME) == canonical_bytes_sync(composer.manifests[0])
    assert sorted(names) == sorted(
        ["document.pdf", MANIFEST_NAME, *(item.path for item in SCOPE.artifacts)]
    )
    # Cada artefacto, con el hash del manifiesto.
    declared = {item["path"]: item["sha256"] for item in composer.manifests[0]["artifacts"]}
    for volume in result.stored.volumes:
        with read_volume(store.latest(location.volume_key(volume.number))) as archive:
            for name in archive.namelist():
                if name != MANIFEST_NAME:
                    assert hashlib.sha256(archive.read(name)).hexdigest() == declared[name]
    # El documento, también como objeto propio; y la subida guardada es la de vol-01.
    document = store.latest(location.document_key)
    assert hashlib.sha256(document).hexdigest() == package.document_sha256
    assert result.stored.document is not None
    assert package.archive_sha256 == archive_sha256(result.stored.volumes)
    assert len(sink.uploads) == 1
    assert not store.uploads


def test_composition_is_deterministic_across_attempts() -> None:
    job = synthetic_job()
    first_store, second_store = MemoryExportStore(), MemoryExportStore()
    first, _, _ = _compose(first_store, job)
    retaken = synthetic_job(
        organization_id=job.organization_id,
        plant_id=job.plant_id,
        export_id=job.export_id,
        attempt=3,
    )
    second, _, _ = _compose(second_store, retaken)
    assert first.results[0].archive_sha256 == second.results[0].archive_sha256
    assert first.manifests == second.manifests
    assert first_store.objects.keys() == second_store.objects.keys()
    for key in first_store.objects:
        assert first_store.latest(key) == second_store.latest(key)


def test_nothing_is_published_before_finish_and_failure_aborts_everything() -> None:
    store = MemoryExportStore()
    job = synthetic_job()
    stream = ExportStream(store=store, settings=SMALL)
    seen: dict[str, Any] = {}

    async def scenario() -> None:
        async with stream.writer(job, RecordingSink()) as writer:
            await writer.add_bytes(
                Artifact(path="document.pdf", kind="document", size_bytes=10, document=True),
                b"%PDF-1.7\n0",
            )
            for name in ("a", "b", "c"):
                await writer.add(
                    Artifact(path=f"clips/{name}.mp4", kind="clip", size_bytes=200 * 1024),
                    synthetic_chunks(name.encode(), 200 * 1024),
                )
            seen["objects"] = dict(store.objects)
            seen["pending"] = len(store.uploads)
            raise RuntimeError("fallo de la composición")

    with pytest.raises(RuntimeError, match="fallo de la composición"):
        asyncio.run(scenario())
    assert seen["objects"] == {}
    assert seen["pending"] >= 3  # dos volúmenes como mínimo y el documento
    assert store.uploads == {}
    assert len(store.aborted) == seen["pending"]


def test_abort_previous_upload_aborts_the_stored_one_and_sweeps_the_export() -> None:
    store = MemoryExportStore()
    job = synthetic_job()
    other = synthetic_job(organization_id=job.organization_id, plant_id=job.plant_id)
    stream = ExportStream(store=store, settings=SMALL)
    sink = RecordingSink()

    async def die_midway() -> None:
        # El «proceso» abre subidas y muere sin salir del ``async with``.
        writer = stream.writer(job, sink)
        await writer.__aenter__()
        await writer.add_bytes(
            Artifact(path="document.pdf", kind="document", size_bytes=4, document=True), b"%PDF"
        )
        for name in ("a", "b"):
            await writer.add(
                Artifact(path=f"{name}.mp4", kind="clip", size_bytes=300 * 1024),
                synthetic_chunks(name.encode(), 300 * 1024),
            )
        # Una subida de otra exportación de la misma planta no se toca.
        await store.create_multipart(
            ExportLocation(other.organization_id, other.plant_id, other.export_id).volume_key(1),
            "application/zip",
        )

    asyncio.run(die_midway())
    location = ExportLocation(job.organization_id, job.plant_id, job.export_id)
    assert len(asyncio.run(store.pending_multipart_uploads(location.prefix))) == 3
    asyncio.run(stream.abort_previous_upload(location, sink.uploads[0]))
    assert asyncio.run(store.pending_multipart_uploads(location.prefix)) == ()
    assert store.aborted[0] == sink.uploads[0]
    assert len(store.uploads) == 1  # la de la otra exportación
    # Sin identificador guardado (murió antes de guardarlo) también barre; y es idempotente.
    asyncio.run(stream.abort_previous_upload(location, None))
    asyncio.run(stream.abort_previous_upload(location, sink.uploads[0]))


def test_entries_are_never_split_and_oversized_ones_are_rejected() -> None:
    store = MemoryExportStore()
    stream = ExportStream(store=store, settings=SMALL)

    async def scenario(size: int) -> None:
        async with stream.writer(synthetic_job(), RecordingSink()) as writer:
            await writer.add(
                Artifact(path="big.mp4", kind="clip", size_bytes=size),
                synthetic_chunks(b"x", size),
            )
            await writer.finish({"manifest_version": 1})

    asyncio.run(scenario(300 * 1024))
    with pytest.raises(ValueError, match="no cabe"):
        asyncio.run(scenario(SMALL.volume_max_bytes))
    assert store.uploads == {}


def test_full_volumes_are_sealed_so_memory_does_not_grow_with_the_volume_count() -> None:
    """Al abrir un volumen nuevo, el anterior (salvo ``vol-01``) cierra su ZIP y sube su última
    parte: no le queda nada pendiente en memoria y su objeto sigue sin existir."""
    store = MemoryExportStore()
    stream = ExportStream(store=store, settings=SMALL)
    seen: list[list[int]] = []

    async def scenario() -> None:
        async with stream.writer(synthetic_job(), RecordingSink()) as writer:
            for index in range(5):
                await writer.add(
                    Artifact(path=f"clips/{index}.mp4", kind="clip", size_bytes=300 * 1024),
                    synthetic_chunks(str(index).encode(), 300 * 1024),
                )
                seen.append([len(volume.upload._pending) for volume in writer._volumes])
            assert store.objects == {}
            await writer.finish({"manifest_version": 1})

    asyncio.run(scenario())
    last = seen[-1]
    assert len(last) == 5  # un clip por volumen
    # vol-01 espera al manifiesto y el último sigue abierto; los del medio, sin nada pendiente.
    assert last[1:-1] == [0, 0, 0]
    assert len(store.objects) == 5 and store.uploads == {}
    for key in store.objects:
        with read_volume(store.latest(key)) as archive:
            assert archive.testzip() is None


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_an_abandoned_entry_leaves_no_unraisable_exception() -> None:
    """Una entrada que falla a mitad se suelta al abandonar: ``ZipFile.__del__`` no avisa."""
    store = MemoryExportStore()
    stream = ExportStream(store=store, settings=SMALL)

    async def scenario() -> None:
        async with stream.writer(synthetic_job(), RecordingSink()) as writer:
            await writer.add(Artifact(path="a.json", kind="audit", size_bytes=1000), (b"x" * 1001,))

    with pytest.raises(ValueError, match="tamaño declarado"):
        asyncio.run(scenario())
    gc.collect()
    assert store.uploads == {}


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("delta", [-1, 1])
def test_declared_size_is_exact(delta: int) -> None:
    store = MemoryExportStore()
    stream = ExportStream(store=store, settings=SMALL)

    async def scenario() -> None:
        async with stream.writer(synthetic_job(), RecordingSink()) as writer:
            await writer.add(
                Artifact(path="a.json", kind="audit", size_bytes=1000),
                (b"x" * (1000 + delta),),
            )

    with pytest.raises(ValueError, match="tamaño declarado"):
        asyncio.run(scenario())
    assert store.uploads == {}


@pytest.mark.parametrize(
    "path",
    ["", "/abs", "../x", "a/../b", "a//b", "a\\b", MANIFEST_NAME, "x" * 256, "ñ.json", " a"],
)
def test_artifact_paths_are_closed(path: str) -> None:
    with pytest.raises(ValueError):
        Artifact(path=path, kind="audit", size_bytes=1)


def test_settings_and_volume_bounds() -> None:
    with pytest.raises(ValueError):
        ExportStreamSettings(part_size=5 * MIB - 1)
    with pytest.raises(ValueError):
        ExportStreamSettings(volume_max_bytes=2**32)
    with pytest.raises(ValueError):
        ExportStreamSettings(volume_max_bytes=1000, manifest_reserve_bytes=1000)
    assert volume_name(1) == "vol-01.zip" and volume_name(99) == "vol-99.zip"
    for number in (0, 100):
        with pytest.raises(ValueError):
            volume_name(number)


def test_stored_objects_round_trip_and_archive_digest() -> None:
    volumes = (
        Volume(number=1, size_bytes=10, sha256="a" * 64, version_id="v1"),
        Volume(number=2, size_bytes=20, sha256="b" * 64, version_id=None),
    )
    stored = StoredObjects(volumes=volumes)
    assert StoredObjects.from_json(json.loads(json.dumps(stored.to_json()))) == stored
    assert StoredObjects.from_json({}) == StoredObjects()
    expected = hashlib.sha256(
        b'{"volumes":[{"name":"vol-01.zip","sha256":"' + b"a" * 64 + b'","size_bytes":10},'
        b'{"name":"vol-02.zip","sha256":"' + b"b" * 64 + b'","size_bytes":20}]}'
    ).hexdigest()
    assert archive_sha256(volumes) == archive_sha256(volumes[::-1]) == expected
    with pytest.raises(ValueError):
        StoredObjects(volumes=volumes[1:])
    with pytest.raises(ValueError):
        archive_sha256(())
    with pytest.raises(ValueError):
        Volume(number=1, size_bytes=-1, sha256="a" * 64, version_id=None)
    with pytest.raises(ValueError):
        Volume(number=1, size_bytes=1, sha256="A" * 64, version_id=None)


def test_location_keys() -> None:
    org, plant, export = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    location = ExportLocation(org, plant, export)
    assert location.volume_key(2) == f"org/{org}/plant/{plant}/export/{export}/vol-02.zip"
    assert location.document_key == f"org/{org}/plant/{plant}/export/{export}/document.pdf"
    with pytest.raises(TypeError):
        ExportLocation(str(org), plant, export)  # type: ignore[arg-type]


def test_zip_without_seek_is_what_zipfile_writes() -> None:
    """La hipótesis en pequeño: ``zipfile`` sobre un destino sin ``tell`` ni ``seek``."""

    class Sink:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, data: bytes) -> int:
            self.data += data
            return len(data)

        def flush(self) -> None:
            return None

    sink = Sink()
    with zipfile.ZipFile(sink, mode="w") as archive:  # type: ignore[arg-type]
        archive.writestr("a.txt", b"hola")
    with read_volume(bytes(sink.data)) as opened:
        assert opened.read("a.txt") == b"hola"
        assert opened.infolist()[0].flag_bits & 0x08
