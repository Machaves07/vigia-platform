"""Las siete secciones y la cobertura de fuente del documento portátil (LC-LAZ-23; NFR-LAZ-54;
VIG-236).

- Las secciones del HTML intermedio se comparan por **igualdad de la estructura**: el numeral
  (``data-section`` de cada ``<section>``) y el título (texto del ``<h2>`` dentro de esa
  sección), con un analizador de HTML, nunca una subcadena en el texto. Contra
  ``document.DOCUMENT_SECTIONS`` entero, no solo los numerales.
- La fuente que **el PDF incrusta** (no el archivo ``.ttf`` del paquete: con
  ``full_fonts=True`` son hoy idénticos, pero la prueba no depende de que sigan siéndolo) cubre
  cada carácter de las etiquetas en español que el documento usa (``labels.platform.es.json`` y
  ``labels.loop.es.json``, lo que carga ``PlatformLabels.load()``), carácter por carácter, nunca
  por una muestra.
"""

from __future__ import annotations

import dataclasses
import io
import json
from html.parser import HTMLParser
from typing import Any

import pytest
from fontTools.ttLib import TTFont  # type: ignore[import-untyped]
from pypdf import PdfReader
from pypdf.generic import IndirectObject

from tests.export_document_support import sample_manifest_inputs
from vigia_platform.accreditation.document import (
    DOCUMENT_SECTIONS,
    document_html,
    generate_document,
)
from vigia_platform.shared.api.labels import (
    DEFAULT_LABELS_PATH,
    DEFAULT_LOOP_LABELS_PATH,
    MissingLabel,
)


class _SectionCollector(HTMLParser):
    """Cada ``(numeral, título)`` en orden: el numeral de ``data-section``, el título del
    ``<h2>`` que abre esa misma sección (nunca el texto completo de la sección)."""

    def __init__(self) -> None:
        super().__init__()
        self.sections: list[tuple[str, str]] = []
        self._current_numeral: str | None = None
        self._in_h2 = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "section":
            for name, value in attrs:
                if name == "data-section" and value is not None:
                    self._current_numeral = value
        elif tag == "h2" and self._current_numeral is not None:
            self._in_h2 = True
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_h2:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
            assert self._current_numeral is not None
            # La plantilla escribe "<numeral>. <título>"; se separa por el primer ". ".
            full = "".join(self._title_parts)
            _, _, title = full.partition(". ")
            self.sections.append((self._current_numeral, title))
            self._current_numeral = None


def test_every_declared_section_appears_exactly_once_in_order() -> None:
    html = document_html(sample_manifest_inputs())
    collector = _SectionCollector()
    collector.feed(html)
    collector.close()
    assert tuple(collector.sections) == DOCUMENT_SECTIONS


def _resolve(value: Any) -> Any:
    return value.get_object() if isinstance(value, IndirectObject) else value


def _font_descriptors(font_dict: Any) -> list[Any]:
    """``/FontDescriptor`` directo (fuente simple) o el de cada ``/DescendantFonts`` (Type0,
    lo que WeasyPrint escribe aquí: un contenedor compuesto por glifo, CID)."""
    descendants = _resolve(_resolve(font_dict).get("/DescendantFonts"))
    if descendants is not None:
        return [
            descriptor
            for child in descendants
            if (descriptor := _resolve(_resolve(child).get("/FontDescriptor"))) is not None
        ]
    descriptor = _resolve(_resolve(font_dict).get("/FontDescriptor"))
    return [] if descriptor is None else [descriptor]


def _embedded_font_codepoints(pdf_bytes: bytes) -> set[int]:
    """Los puntos de código que cubre cada fuente ``FontFile2`` que el PDF incrusta de verdad."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    covered: set[int] = set()
    found_any = False
    for page in reader.pages:
        resources: Any = _resolve(page.get("/Resources"))
        if resources is None:
            continue
        fonts = _resolve(resources.get("/Font"))
        if fonts is None:
            continue
        for ref in fonts.values():
            for descriptor in _font_descriptors(ref):
                font_file = _resolve(descriptor.get("/FontFile2"))
                if font_file is None:
                    continue
                found_any = True
                ttfont = TTFont(io.BytesIO(font_file.get_data()))
                covered |= set(ttfont.getBestCmap())
    assert found_any, "el PDF no incrustó ninguna fuente FontFile2: la prueba no prueba nada"
    return covered


def _label_characters() -> set[str]:
    characters: set[str] = set()
    for path in (DEFAULT_LABELS_PATH, DEFAULT_LOOP_LABELS_PATH):
        document = json.loads(path.read_text(encoding="utf-8"))
        for values in document.values():
            for text in values.values():
                characters.update(text)
    return characters


def test_the_embedded_font_covers_every_character_of_the_spanish_labels() -> None:
    pdf_bytes = generate_document(sample_manifest_inputs())
    covered = _embedded_font_codepoints(pdf_bytes)
    missing = {char for char in _label_characters() if ord(char) not in covered}
    assert missing == set()


def _with_regression_cause(cause: str) -> Any:
    inputs = sample_manifest_inputs()
    (period,) = inputs.regression_periods
    return dataclasses.replace(
        inputs, regression_periods=(dataclasses.replace(period, cause=cause),)
    )


def test_a_regression_cause_shows_its_label_never_the_raw_enumeration_value() -> None:
    """LC-LAZ-23: ninguna enumeración cruda. Mutación: si ``document_content`` volviera a poner
    ``item.cause`` tal cual (el defecto que encontró la revisión automática), este valor crudo
    aparecería en el HTML y la prueba caería."""
    html = document_html(_with_regression_cause("model_version_change"))
    assert "cambio de versión del modelo" in html
    assert "model_version_change" not in html


def test_an_unlabeled_regression_cause_stops_the_render() -> None:
    with pytest.raises(MissingLabel):
        document_html(_with_regression_cause("not_a_real_cause"))
