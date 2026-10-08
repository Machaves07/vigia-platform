"""Documento legible del acta (TASK-217; LC-GOB-08; PAT-GOB-REN-06; NFR-GOB-05, 32, 69).

- **Campo a campo** (NFR-GOB-69): el HTML intermedio tiene un ``data-field`` por cada hoja de la
  respuesta de ``GET /commissioning-records/{id}``, ni una más ni una menos, con el texto que
  calcula esta prueba por su cuenta (etiqueta en español, número, ``—`` para nulo, texto tal
  cual). Sobre actas generadas (Hypothesis) y sobre la matriz máxima.
- **PDF**: válido, de la matriz máxima, de menos de 2 MB.
- **Escape**: ningún valor se interpreta como marcado; sin JavaScript.
- **Recursos** (NFR-GOB-32): el ``url_fetcher`` registra cada petición; un render solo pide las
  dos fuentes empaquetadas y no abre ningún socket; cualquier otra URL se deniega y detiene el
  render.
- **Tiempo de espera** (NFR-GOB-05): un render que no termina responde ``DocumentTimedOut`` en el
  tope reducido, sin documento; el render corre en un hilo del pool de CPU.
- **Etiquetas** (NFR-GOB-67): cada reloj y cada tramo tiene su rótulo; un valor sin etiqueta
  detiene el render (fallo cerrado).

Solo datos generados (NFR-CTR-43).
"""

from __future__ import annotations

import asyncio
import re
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import given
from hypothesis import strategies as st
from weasyprint.urls import FatalURLFetchingError

from tests.record_document_support import (
    HOSTILE_REASON,
    MAX_CAMERAS,
    MAX_PASSES,
    MAX_STANDARDS,
    assert_field_by_field,
    expected_text,
    html_fields,
    record_body,
)
from vigia_platform.catalog.adapters.rendering.record_document import (
    DOCUMENT_LABELS,
    DOCUMENT_MAX_CONCURRENT,
    DOCUMENT_TIMEOUT_SECONDS,
    FONT_DIRECTORY,
    FONT_FILES,
    DocumentBusy,
    DocumentRenderFailed,
    DocumentTimedOut,
    PackagedFontFetcher,
    RecordDocumentRenderer,
    default_concurrency,
    record_html,
    render_pdf,
)
from vigia_platform.catalog.domain.latency import MeasuredBy, TrancheName
from vigia_platform.shared.api.labels import MissingLabel, PlatformLabels
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.cpu_pool import CPU_POOL_THREAD_PREFIX, CpuPool

MAX_DOCUMENT_BYTES: Final = 2 * 1024 * 1024
"""NFR-GOB-05: tamaño esperado inferior a 2 MB."""
REDUCED_TIMEOUT: Final = 1.0
"""Tope reducido de la prueba: el doble no termina nunca, así que el tope siempre se agota."""
RELEASE_SECONDS: Final = 60.0
FONT_URLS: Final = {(FONT_DIRECTORY / name).as_uri() for name in FONT_FILES}


@pytest.fixture(scope="module")
def labels() -> PlatformLabels:
    return PlatformLabels.load()


@pytest.fixture(scope="module")
def maximum() -> dict[str, Any]:
    return record_body(MAX_STANDARDS, MAX_CAMERAS, MAX_PASSES, seed="maxima")


# --- Campo a campo -------------------------------------------------------------------------------


def test_the_maximum_matrix_shows_every_field(
    labels: PlatformLabels, maximum: dict[str, Any]
) -> None:
    assert len(maximum["matrix_results"]) == 4 * MAX_STANDARDS == 128
    assert len(maximum["cameras_measured"]) == MAX_CAMERAS
    assert_field_by_field(maximum, record_html(maximum, labels))


_REASONS = st.one_of(
    st.none(),
    st.text(
        alphabet=st.one_of(
            st.sampled_from("<>&\"'/=;\n\t "),
            st.characters(exclude_categories=("Cs", "Cc")),
        ),
        min_size=1,
        max_size=200,
    ),
)


@given(
    standards=st.integers(min_value=0, max_value=MAX_STANDARDS),
    cameras=st.integers(min_value=0, max_value=MAX_CAMERAS),
    passes=st.integers(min_value=1, max_value=6),
    reason=_REASONS,
    measured=st.booleans(),
    seed=st.text(alphabet="abcdef0123456789", min_size=1, max_size=8),
)
def test_generated_records_match_field_by_field(
    labels: PlatformLabels,
    standards: int,
    cameras: int,
    passes: int,
    reason: str | None,
    measured: bool,
    seed: str,
) -> None:
    record = record_body(standards, cameras, passes, seed=seed, reason_es=reason, measured=measured)
    assert_field_by_field(record, record_html(record, labels))


def test_a_template_that_drops_a_field_is_caught(
    labels: PlatformLabels, maximum: dict[str, Any]
) -> None:
    # La comprobación no es vacía: quitar un elemento del HTML la hace fallar.
    html = record_html(maximum, labels)
    dropped = re.sub(r'<td data-field="matrix_results\.5\.missed">[^<]*</td>', "<td></td>", html)
    assert dropped != html
    with pytest.raises(AssertionError):
        assert_field_by_field(maximum, dropped)


# --- PDF ------------------------------------------------------------------------------------------


def test_the_maximum_matrix_pdf_is_valid_and_below_2_mb(
    labels: PlatformLabels, maximum: dict[str, Any]
) -> None:
    fetcher = PackagedFontFetcher()
    pdf = render_pdf(record_html(maximum, labels), fetcher)
    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert b"startxref" in pdf
    assert 0 < len(pdf) < MAX_DOCUMENT_BYTES, len(pdf)
    assert set(fetcher.requested) == FONT_URLS


# --- Escape ---------------------------------------------------------------------------------------


def test_no_value_is_interpreted_as_markup(labels: PlatformLabels) -> None:
    record = record_body(seed="hostil", reason_es=HOSTILE_REASON)
    html = record_html(record, labels)
    fields = html_fields(html)
    assert fields["false_alarm_acceptance.reason_es"] == HOSTILE_REASON
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;b&gt;&amp; &#34;brillo&#34;&lt;/b&gt;" in html
    assert "&amp;amp;" in html  # el «&amp;» del texto, escapado otra vez
    for raw in ("<script", "<b>", "<img", 'http://e/x.png">'):
        assert raw not in html
    assert_field_by_field(record, html)


def test_the_template_has_no_javascript(labels: PlatformLabels, maximum: dict[str, Any]) -> None:
    html = record_html(maximum, labels).lower()
    assert "<script" not in html
    assert "javascript:" not in html
    assert not re.search(r"\son[a-z]+\s*=", html)


# --- Recursos -------------------------------------------------------------------------------------


@pytest.fixture
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[object]]:
    """Cuenta todo intento de abrir un socket de red durante la prueba (y lo impide)."""
    attempts: list[object] = []
    original = socket.socket

    class Guarded(original):  # type: ignore[misc, valid-type]
        def __init__(self, family: int = -1, *args: Any, **kwargs: Any) -> None:
            if family in (socket.AF_INET, socket.AF_INET6):
                attempts.append(family)
                raise OSError("red prohibida en el render del acta")
            super().__init__(family, *args, **kwargs)

    def refuse(*args: Any, **kwargs: Any) -> socket.socket:
        attempts.append(args)
        raise OSError("red prohibida en el render del acta")

    monkeypatch.setattr(socket, "socket", Guarded)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield attempts


def test_a_render_only_requests_the_packaged_fonts_and_opens_no_socket(
    labels: PlatformLabels, no_sockets: list[object]
) -> None:
    # Con un valor que, sin escapar, pediría una imagen por la red.
    record = record_body(MAX_STANDARDS, MAX_CAMERAS, seed="red", reason_es=HOSTILE_REASON)
    fetcher = PackagedFontFetcher()
    pdf = render_pdf(record_html(record, labels), fetcher)
    assert pdf.startswith(b"%PDF-")
    assert fetcher.requested, "el render no registró ninguna petición"
    assert set(fetcher.requested) == FONT_URLS
    assert all(url.startswith("file:///") for url in fetcher.requested)
    assert no_sockets == []


def test_the_packaged_fonts_are_sil_ofl_and_local() -> None:
    assert sorted(path.name for path in FONT_DIRECTORY.iterdir()) == sorted(
        [*FONT_FILES, "OFL.txt"]
    )
    licence = (FONT_DIRECTORY / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License, Version 1.1" in licence
    for name in FONT_FILES:
        assert (FONT_DIRECTORY / name).read_bytes()[:4] == b"\x00\x01\x00\x00"  # TrueType


@pytest.mark.parametrize(
    "url",
    [
        "http://fonts.example/NotoSans-Regular.ttf",
        "https://fonts.example/NotoSans-Regular.ttf",
        "ftp://fonts.example/NotoSans-Regular.ttf",
        "data:font/ttf;base64,AAEAAA==",
        "file:///etc/passwd",
        (FONT_DIRECTORY / "OFL.txt").as_uri(),
        (FONT_DIRECTORY / "NotoSans-Regular.ttf").as_uri() + "?v=1",
        (FONT_DIRECTORY / "NotoSans-Regular.ttf").as_uri() + "#x",
        (FONT_DIRECTORY / "NotoSans-Regular.ttf").as_uri().replace("file:///", "file://evil/"),
        (FONT_DIRECTORY / ".." / "noto-sans" / "NotoSans-Regular.ttf").as_uri(),
        (FONT_DIRECTORY.parents[1] / "labels.platform.es.json").as_uri(),
        Path("/tmp/NotoSans-Regular.ttf").as_uri(),  # noqa: S108 - otra carpeta, mismo nombre
        "NotoSans-Regular.ttf",
    ],
)
def test_any_other_url_is_denied_and_recorded(url: str) -> None:
    fetcher = PackagedFontFetcher()
    with pytest.raises(FatalURLFetchingError):
        fetcher.fetch(url)
    assert fetcher.requested == [url]


def test_the_packaged_fonts_are_served_from_the_package() -> None:
    fetcher = PackagedFontFetcher()
    for name in FONT_FILES:
        url = (FONT_DIRECTORY / name).as_uri()
        response = fetcher.fetch(url)
        assert response.read() == (FONT_DIRECTORY / name).read_bytes()
        response.close()
    assert fetcher.requested == [(FONT_DIRECTORY / name).as_uri() for name in FONT_FILES]


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="http://e.example/x.png">',
        '<link rel="stylesheet" href="https://e.example/x.css">',
        '<img src="file:///etc/hostname">',
        '<div style="background: url(data:image/png;base64,iVBORw0KGgo=)">x</div>',
        '<style>@import url("http://e.example/y.css");</style>',
    ],
)
def test_a_template_that_asks_for_another_resource_stops_the_render(
    markup: str, no_sockets: list[object]
) -> None:
    fetcher = PackagedFontFetcher()
    html = f'<!DOCTYPE html><html lang="es"><body><p>acta</p>{markup}</body></html>'
    with pytest.raises(DocumentRenderFailed):
        render_pdf(html, fetcher)
    assert set(fetcher.requested) - FONT_URLS, fetcher.requested
    assert no_sockets == []


# --- Pool y tiempo de espera --------------------------------------------------------------------


@pytest.fixture
def pool() -> Iterator[CpuPool]:
    created = CpuPool(SystemClock())
    yield created
    created.shutdown(wait=True)


def test_the_default_timeout_is_10_seconds(pool: CpuPool, labels: PlatformLabels) -> None:
    assert DOCUMENT_TIMEOUT_SECONDS == 10.0
    assert RecordDocumentRenderer(pool=pool, labels=labels).timeout_seconds == 10.0
    for bad in (0, -1.0, True):
        with pytest.raises(ValueError):
            RecordDocumentRenderer(pool=pool, labels=labels, timeout_seconds=bad)


def test_the_render_runs_in_the_cpu_pool_and_returns_the_whole_pdf(
    pool: CpuPool, labels: PlatformLabels
) -> None:
    threads: list[str] = []
    html_seen: list[str] = []

    def pdf(html: str) -> bytes:
        threads.append(threading.current_thread().name)
        html_seen.append(html)
        return b"%PDF-1.7 documento completo %%EOF"

    renderer = RecordDocumentRenderer(pool=pool, labels=labels, pdf=pdf)
    record = record_body(seed="pool")
    assert asyncio.run(renderer.render(record)) == b"%PDF-1.7 documento completo %%EOF"
    assert threads and threads[0].startswith(CPU_POOL_THREAD_PREFIX)
    assert html_seen == [record_html(record, labels)]


def test_a_render_that_does_not_finish_times_out_without_a_document(
    pool: CpuPool, labels: PlatformLabels
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished: list[bytes] = []

    def stuck(html: str) -> bytes:
        started.set()
        release.wait(RELEASE_SECONDS)  # nunca antes del tope: lo suelta la prueba al final
        finished.append(b"%PDF tarde")
        return b"%PDF tarde"

    renderer = RecordDocumentRenderer(
        pool=pool, labels=labels, timeout_seconds=REDUCED_TIMEOUT, pdf=stuck
    )
    try:
        with pytest.raises(DocumentTimedOut):
            asyncio.run(renderer.render(record_body(seed="lento")))
        assert started.is_set()
        assert finished == []  # el render seguía en marcha al agotarse el tope
    finally:
        release.set()


def test_a_real_render_with_a_too_short_timeout_times_out(
    pool: CpuPool, labels: PlatformLabels, maximum: dict[str, Any]
) -> None:
    # Plantilla real de la matriz máxima con el pool ocupado: la espera en cola cuenta.
    blocker = threading.Event()
    single = CpuPool(SystemClock(), max_workers=1)
    try:
        single_busy = single._executor.submit(blocker.wait, RELEASE_SECONDS)
        renderer = RecordDocumentRenderer(
            pool=single, labels=labels, timeout_seconds=REDUCED_TIMEOUT
        )
        with pytest.raises(DocumentTimedOut):
            asyncio.run(renderer.render(maximum))
    finally:
        blocker.set()
        single_busy.result(timeout=RELEASE_SECONDS)
        single.shutdown(wait=True)


# --- Limitador de renders (revisión de VIG-160) --------------------------------------------------


class Held:
    """Doble del PDF: cada render espera a que la prueba lo suelte y cuenta los simultáneos."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0
        self.running = 0
        self.peak = 0

    def __call__(self, html: str) -> bytes:
        with self._lock:
            self.calls += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            self.release.wait(RELEASE_SECONDS)
            return b"%PDF-1.7 doble %%EOF"
        finally:
            with self._lock:
                self.running -= 1


def _slot_comes_back(renderer: RecordDocumentRenderer) -> bool:
    """Espera (sin decidir por un tope corto) a que el limitador tenga un puesto libre."""
    slots: threading.BoundedSemaphore = renderer._slots
    if not slots.acquire(timeout=RELEASE_SECONDS):
        return False
    slots.release()
    return True


def test_the_limiter_defaults_below_the_pool_and_is_validated(
    pool: CpuPool, labels: PlatformLabels
) -> None:
    assert pool.max_workers == 4
    assert DOCUMENT_MAX_CONCURRENT == 2
    assert RecordDocumentRenderer(pool=pool, labels=labels).max_concurrent == 2
    assert [default_concurrency(n) for n in (1, 2, 3, 4, 64)] == [1, 1, 2, 2, 2]
    for bad in (0, 4, 5, True, 1.5):
        with pytest.raises(ValueError):
            RecordDocumentRenderer(pool=pool, labels=labels, max_concurrent=bad)  # type: ignore[arg-type]


def test_an_orphaned_render_keeps_its_slot_and_the_next_request_is_busy_at_once(
    pool: CpuPool, labels: PlatformLabels
) -> None:
    held = Held()
    renderer = RecordDocumentRenderer(
        pool=pool, labels=labels, timeout_seconds=REDUCED_TIMEOUT, max_concurrent=1, pdf=held
    )
    record = record_body(seed="huerfano")
    try:
        with pytest.raises(DocumentTimedOut) as first:
            asyncio.run(renderer.render(record))
        assert not isinstance(first.value, DocumentBusy)
        assert held.running == 1  # el render agotado sigue en su hilo
        # El siguiente no llega al pool: sin puesto, transitorio al instante.
        with pytest.raises(DocumentBusy):
            asyncio.run(renderer.render(record))
        assert held.calls == 1
        # El pool del proceso sigue libre para otra tarea (p. ej. un hash de contraseña).
        assert asyncio.run(asyncio.wait_for(pool.run(sum, [1, 2, 3]), RELEASE_SECONDS)) == 6
    finally:
        held.release.set()
    # Cuando el render huérfano termina de verdad, el puesto vuelve.
    assert _slot_comes_back(renderer)
    assert held.calls == 1


def test_concurrent_requests_never_render_more_than_the_limit(
    pool: CpuPool, labels: PlatformLabels
) -> None:
    held = Held()
    renderer = RecordDocumentRenderer(
        pool=pool, labels=labels, timeout_seconds=RELEASE_SECONDS, max_concurrent=2, pdf=held
    )
    record = record_body(seed="rafaga")

    async def burst() -> list[bytes | BaseException]:
        tasks = [asyncio.create_task(renderer.render(record)) for _ in range(8)]
        # Fases por eventos, nunca por un tope corto: esperar a que las 8 hayan pedido puesto.
        for _ in range(int(RELEASE_SECONDS * 100)):
            if held.calls >= 2 and sum(task.done() for task in tasks) >= 6:
                break
            await asyncio.sleep(0.01)
        held.release.set()
        return await asyncio.gather(*tasks, return_exceptions=True)

    try:
        results = asyncio.run(burst())
    finally:
        held.release.set()
    rendered = [r for r in results if isinstance(r, bytes)]
    busy = [r for r in results if isinstance(r, DocumentBusy)]
    assert (len(rendered), len(busy)) == (2, 6), results
    assert held.peak == 2 and held.calls == 2
    assert _slot_comes_back(renderer)


def test_a_render_abandoned_in_the_queue_gives_its_slot_back(labels: PlatformLabels) -> None:
    # Un pool de un hilo ocupado: el render queda en cola y la petición se cancela.
    single = CpuPool(SystemClock(), max_workers=1)
    blocker = threading.Event()
    calls: list[str] = []

    def pdf(html: str) -> bytes:
        calls.append(html)
        return b"%PDF-1.7 doble %%EOF"

    renderer = RecordDocumentRenderer(
        pool=single, labels=labels, timeout_seconds=RELEASE_SECONDS, pdf=pdf
    )
    record = record_body(seed="cola")
    occupied = single._executor.submit(blocker.wait, RELEASE_SECONDS)
    try:
        with pytest.raises(TimeoutError):
            asyncio.run(asyncio.wait_for(renderer.render(record), REDUCED_TIMEOUT))
    finally:
        blocker.set()
        occupied.result(timeout=RELEASE_SECONDS)
    try:
        # El puesto volvió aunque el render nunca empezó; el abandonado no se ejecuta.
        assert asyncio.run(renderer.render(record)).startswith(b"%PDF")
        assert len(calls) == 1
    finally:
        single.shutdown(wait=True)


# --- Etiquetas ------------------------------------------------------------------------------------


def test_every_clock_and_tranche_has_its_document_label() -> None:
    for enumeration in (MeasuredBy, TrancheName):
        names = DOCUMENT_LABELS[enumeration]
        assert set(names) == {member.value for member in enumeration}
        assert all(text.strip() and text != value for value, text in names.items())
    for clock in MeasuredBy:
        assert DOCUMENT_LABELS[MeasuredBy][clock] == expected_text("x.measured_by", clock.value)
    for tranche in TrancheName:
        shown = expected_text("latency.not_measured.0", tranche.value)
        assert DOCUMENT_LABELS[TrancheName][tranche] == shown


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("signatures", 0, "role_in_use"), "intruder"),
        (("kind",), "unknown_kind"),
        (("installer_measurements", "measured_by"), "satellite"),
        (("latency", "not_measured"), ["warp_tranche"]),
    ],
)
def test_a_value_without_label_stops_the_render(
    labels: PlatformLabels, path: tuple[Any, ...], value: Any
) -> None:
    record = record_body(seed="etiquetas")
    target: Any = record
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(MissingLabel):
        record_html(record, labels)
