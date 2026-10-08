"""Documento legible del acta de comisionamiento, generado a demanda (LC-GOB-08; PAT-GOB-REN-06).

El acta es el contenido estructurado del expediente; el PDF es solo una **vista** que se genera
cada vez que se pide y **no se almacena** (BR-GOB-50, NFR-GOB-69). Sale de la misma vista que
``GET /commissioning-records/{id}`` en dos pasos:

1. ``record_html``: la plantilla Jinja2 ``templates/commissioning_record.html`` con
   ``autoescape`` (todo valor se escapa, también ``reason_es``) y sin JavaScript. Cada hoja de la
   vista aparece en un elemento con ``data-field="<ruta>"`` (``matrix_results.3.detected``), así
   que la coincidencia campo a campo con la respuesta JSON se comprueba sobre este HTML
   intermedio. Las enumeraciones se muestran por su etiqueta en español (NFR-GOB-67): las de
   ``labels.platform.es.json`` y, para el reloj y el nombre de los tramos de latencia, que no son
   enumeraciones publicadas, los rótulos del documento (``DOCUMENT_LABELS``). Un valor sin
   etiqueta detiene el render (fallo cerrado, nunca el valor crudo).
2. ``render_pdf``: WeasyPrint con ``PackagedFontFetcher`` como ``url_fetcher``. Registra cada
   petición de recurso y **solo** sirve los archivos de fuente empaquetados en
   ``backend/resources/fonts/noto-sans/`` (Noto Sans, SIL OFL 1.1, A-53): cualquier otra URL
   (red, ``data:``, otra ruta del disco) detiene el render. No hay red ni lectura de rutas
   arbitrarias (NFR-GOB-32).

``RecordDocumentRenderer.render`` corre los dos pasos en el pool de CPU heredado
(``shared.cpu_pool``: ``cpu_pool_wait_ms``, ``VIGIA_THREADPOOL_SIZE``) envuelto en
``asyncio.wait_for`` con **10 s** (NFR-GOB-43, inyectable). Si se agota, ``DocumentTimedOut``:
la ruta responde ``temporarily_unavailable`` y nunca un cuerpo parcial, porque el PDF solo existe
cuando ``write_pdf`` termina entero. Como en ``asyncio.to_thread``, el hilo que ya empezó no se
detiene: termina y su resultado se descarta.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import jinja2
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration
from weasyprint.urls import FatalURLFetchingError, URLFetcher, URLFetcherResponse

from vigia_platform.catalog.domain.latency import MeasuredBy, TrancheName
from vigia_platform.shared.api.labels import MissingLabel, PlatformLabels
from vigia_platform.shared.cpu_pool import CpuPool
from vigia_platform.shared.observability.logging import get_logger

__all__ = [
    "DOCUMENT_LABELS",
    "DOCUMENT_RETRY_AFTER_SECONDS",
    "DOCUMENT_TIMEOUT_SECONDS",
    "ENUMERATED_FIELDS",
    "FONT_DIRECTORY",
    "FONT_FILES",
    "NULL_TEXT",
    "DocumentRenderFailed",
    "DocumentTimedOut",
    "Leaf",
    "PackagedFontFetcher",
    "RecordDocumentRenderer",
    "display",
    "record_html",
    "render_pdf",
]

DOCUMENT_TIMEOUT_SECONDS: Final = 10.0
"""Tiempo de espera duro de la generación (NFR-GOB-43); el objetivo es 3 s p95 (NFR-GOB-05)."""
DOCUMENT_RETRY_AFTER_SECONDS: Final = 10
"""``retry_after_seconds`` del documento que no llegó a tiempo ``[objetivo propio]``."""

RENDERING: Final = Path(__file__).resolve().parent
TEMPLATES: Final = RENDERING / "templates"
STYLESHEET: Final = TEMPLATES / "commissioning_record.css"
FONT_DIRECTORY: Final = Path(__file__).resolve().parents[5] / "resources" / "fonts" / "noto-sans"
"""``backend/resources/fonts/noto-sans`` (la imagen la copia con ``backend/resources``)."""
FONT_FILES: Final = ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf")
"""Los únicos recursos que un render puede pedir (los cita ``commissioning_record.css``)."""

NULL_TEXT: Final = "—"
"""Lo que muestra el documento para un valor nulo (``null`` en la respuesta JSON)."""
YES_NO: Final = {True: "Sí", False: "No"}

DOCUMENT_LABELS: Final[Mapping[type[enum.Enum], Mapping[str, str]]] = {
    MeasuredBy: {
        MeasuredBy.INSTALLER.value: "Instalador",
        MeasuredBy.PLATFORM.value: "Plataforma",
        MeasuredBy.BROWSER.value: "Navegador",
        MeasuredBy.NODE.value: "Nodo",
    },
    TrancheName: {
        TrancheName.NODE.value: "Tramo 1: del hecho al aviso del nodo",
        TrancheName.PLATFORM.value: "Tramo 2: de la concesión a la recepción del clip",
        TrancheName.EXPOSURE.value: "Tramo 3a: exposición en la consola",
        TrancheName.SERVED.value: "Tramo 3b: del clip recibido al primer servicio",
    },
}
"""Rótulos del documento para los relojes y los tramos de latencia (no son enumeraciones de
``labels.platform.es.json``)."""

ENUMERATED_FIELDS: Final[Mapping[str, str | type[enum.Enum]]] = {
    "kind": "walk_test_kind",
    "steps_summary.*.step_kind": "step_kind",
    "occlusion_summary.*.verification": "occlusion_verification",
    "signatures.*.role_in_use": "role",
    "latency.node_tranche.measured_by": MeasuredBy,
    "latency.platform_tranche.measured_by": MeasuredBy,
    "latency.exposure_tranche.measured_by": MeasuredBy,
    "latency.served_tranche.measured_by": MeasuredBy,
    "latency.not_measured.*": TrancheName,
    "installer_measurements.measured_by": MeasuredBy,
}
"""Ruta de la hoja (``*`` por posición en una lista) → enumeración de su etiqueta."""

_INDEX: Final = re.compile(r"\.\d+(?=\.|$)")

_log = get_logger("catalog.rendering")


class DocumentTimedOut(Exception):
    """La generación no terminó dentro del tiempo de espera: no hay documento (transitorio)."""


class DocumentRenderFailed(Exception):
    """El render pidió un recurso que no es una fuente empaquetada (error del código)."""


class _ResourceDenied(FatalURLFetchingError):
    """Detiene WeasyPrint: ``FatalURLFetchingError`` es lo único que no convierte en aviso."""


@dataclass(frozen=True, slots=True)
class Leaf:
    """Una hoja de la vista del acta: su ruta en la respuesta JSON y el texto que se muestra."""

    path: str
    text: str


# --- Valores -------------------------------------------------------------------------------------


def _pattern(path: str) -> str:
    return _INDEX.sub(".*", path)


def _label(labels: PlatformLabels, enumeration: str | type[enum.Enum], value: object) -> str:
    if isinstance(enumeration, str):
        return labels.label(enumeration, value)
    names = DOCUMENT_LABELS[enumeration]
    if not isinstance(value, str) or value not in names:
        raise MissingLabel(enumeration.__name__, repr(value))
    return names[value]


def display(path: str, value: object, labels: PlatformLabels) -> str:
    """El texto de la hoja ``path``: etiqueta, número, marca o texto tal cual (que se escapa)."""
    enumeration = ENUMERATED_FIELDS.get(_pattern(path))
    if value is None:
        return NULL_TEXT
    if enumeration is not None:
        return _label(labels, enumeration, value)
    if isinstance(value, bool):
        return YES_NO[value]
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value).replace(".", ",")
    if isinstance(value, str):
        return value
    raise TypeError(f"valor no representable en el documento en {path}")


def _tree(value: object, path: str, labels: PlatformLabels) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _tree(item, f"{path}.{key}" if path else str(key), labels)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_tree(item, f"{path}.{index}", labels) for index, item in enumerate(value)]
    return Leaf(path, display(path, value, labels))


@functools.cache
def _environment() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=False,
    )


def record_html(record: Mapping[str, Any], labels: PlatformLabels) -> str:
    """El HTML intermedio del acta (``record``: el cuerpo de ``GET /commissioning-records/…``)."""
    template = _environment().get_template("commissioning_record.html")
    tranches = [(name.value, DOCUMENT_LABELS[TrancheName][name.value]) for name in TrancheName]
    return template.render(record=_tree(record, "", labels), tranches=tranches)


# --- Fuentes -------------------------------------------------------------------------------------


@functools.cache
def _font_bytes(path: Path) -> bytes:
    return path.read_bytes()


class PackagedFontFetcher(URLFetcher):
    """``url_fetcher`` de un render: registra cada URL y solo sirve las fuentes empaquetadas.

    Se admite una URL ``file:`` sin máquina, consulta ni fragmento cuya ruta sea **exactamente**
    uno de ``FONT_FILES`` en ``FONT_DIRECTORY``. Todo lo demás se deniega con un error fatal que
    detiene el render: nunca se abre una conexión ni se lee otro archivo.
    """

    def __init__(
        self, directory: Path = FONT_DIRECTORY, files: tuple[str, ...] = FONT_FILES
    ) -> None:
        super().__init__(allowed_protocols=frozenset({"file"}), allow_redirects=False)
        self._allowed = {str(directory / name): directory / name for name in files}
        self.requested: list[str] = []
        """Cada URL pedida durante el render, en orden (también las denegadas)."""

    def _packaged(self, url: str) -> Path | None:
        parts = urlsplit(url)
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            return None
        if parts.query or parts.fragment or "?" in url or "#" in url:
            return None
        return self._allowed.get(url2pathname(unquote(parts.path)))

    def fetch(self, url: str, headers: Mapping[str, str] | None = None) -> URLFetcherResponse:
        self.requested.append(url)
        path = self._packaged(url)
        if path is None:
            raise _ResourceDenied("recurso denegado: solo se sirven las fuentes empaquetadas")
        return URLFetcherResponse(url, _font_bytes(path), {"Content-Type": "font/ttf"})


def render_pdf(html: str, fetcher: PackagedFontFetcher | None = None) -> bytes:
    """El PDF de ``html`` con la hoja de estilos del acta y solo las fuentes empaquetadas."""
    fetcher = fetcher if fetcher is not None else PackagedFontFetcher()
    base_url = str(FONT_DIRECTORY) + "/"
    fonts = FontConfiguration()
    try:
        stylesheet = CSS(
            string=STYLESHEET.read_text(encoding="utf-8"),
            base_url=base_url,
            url_fetcher=fetcher,
            font_config=fonts,
        )
        document = HTML(string=html, base_url=base_url, url_fetcher=fetcher)
        return document.write_pdf(stylesheets=[stylesheet], font_config=fonts)
    except FatalURLFetchingError:
        # BaseException de WeasyPrint: aquí pasa a ser un error normal (``internal_error``).
        raise DocumentRenderFailed("el documento pidió un recurso no empaquetado") from None


# --- Servicio ------------------------------------------------------------------------------------


class RecordDocumentRenderer:
    """Genera el PDF del acta en el pool de CPU con tiempo de espera; nunca lo guarda."""

    def __init__(
        self,
        *,
        pool: CpuPool,
        labels: PlatformLabels | None = None,
        timeout_seconds: float = DOCUMENT_TIMEOUT_SECONDS,
        pdf: Callable[[str], bytes] = render_pdf,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not timeout_seconds > 0:
            raise ValueError("el tiempo de espera del documento debe ser positivo")
        self._pool = pool
        self._labels = labels if labels is not None else PlatformLabels.load()
        self._timeout = float(timeout_seconds)
        self._pdf = pdf

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    def _document(self, record: Mapping[str, Any]) -> bytes:
        return self._pdf(record_html(record, self._labels))

    async def render(self, record: Mapping[str, Any]) -> bytes:
        """El PDF de ``record`` (la vista de ``GET /commissioning-records/{id}``).

        ``DocumentTimedOut`` si no termina en ``timeout_seconds`` (espera en cola incluida).
        """
        try:
            return await asyncio.wait_for(
                self._pool.run(self._document, dict(record)), timeout=self._timeout
            )
        except TimeoutError:
            _log.warning("el documento del acta no se generó dentro del tiempo de espera")
            raise DocumentTimedOut from None
