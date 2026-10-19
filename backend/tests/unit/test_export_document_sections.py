"""Las siete secciones y la cobertura de fuente del documento portátil (LC-LAZ-23; NFR-LAZ-54;
VIG-236).

- Las secciones del HTML intermedio se comparan por **igualdad de la estructura**
  (``data-section`` de cada ``<section>``, con un analizador de HTML, nunca una subcadena en el
  texto) contra ``document.DOCUMENT_SECTIONS``.
- La fuente empaquetada (Noto Sans) cubre cada carácter de las etiquetas en español que el
  documento usa (``labels.platform.es.json`` y ``labels.loop.es.json``, lo que carga
  ``PlatformLabels.load()``), carácter por carácter, nunca por una muestra.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser

from fontTools.ttLib import TTFont  # type: ignore[import-untyped]

from tests.export_document_support import sample_manifest_inputs
from vigia_platform.accreditation.export.document import (
    DOCUMENT_SECTIONS,
    FONT_DIRECTORY,
    FONT_FILES,
    document_html,
)
from vigia_platform.shared.api.labels import DEFAULT_LABELS_PATH, DEFAULT_LOOP_LABELS_PATH


class _SectionCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sections: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "section":
            return
        for name, value in attrs:
            if name == "data-section" and value is not None:
                self.sections.append(value)


def test_every_declared_section_appears_exactly_once_in_order() -> None:
    html = document_html(sample_manifest_inputs())
    collector = _SectionCollector()
    collector.feed(html)
    collector.close()
    assert tuple(collector.sections) == tuple(numeral for numeral, _ in DOCUMENT_SECTIONS)


def _covered_codepoints(path: object) -> set[int]:
    font = TTFont(str(path))
    cmap = font.getBestCmap()
    return set(cmap)


def _label_characters() -> set[str]:
    characters: set[str] = set()
    for path in (DEFAULT_LABELS_PATH, DEFAULT_LOOP_LABELS_PATH):
        document = json.loads(path.read_text(encoding="utf-8"))
        for values in document.values():
            for text in values.values():
                characters.update(text)
    return characters


def test_the_embedded_font_covers_every_character_of_the_spanish_labels() -> None:
    covered: set[int] = set()
    for name in FONT_FILES:
        covered |= _covered_codepoints(FONT_DIRECTORY / name)
    missing = {char for char in _label_characters() if ord(char) not in covered}
    assert missing == set()
