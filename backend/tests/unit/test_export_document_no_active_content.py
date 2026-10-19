"""Sin contenido activo ni referencias externas en el documento portátil (NFR-LAZ-18; VIG-236).

Inspección **estructural** del grafo de objetos del PDF (``pypdf``, BSD-3-Clause, solo en
pruebas): nunca una búsqueda de subcadenas en el texto. Se recorre todo lo que un lector de PDF
seguiría desde la raíz (``/Root``) — catálogo, páginas, recursos, anotaciones — y se comprueba
que ninguna de esas rutas lleva JavaScript, una acción (lanzar un programa, enviar un formulario,
abrir otro archivo o una URI), un formulario (``/AcroForm``) ni un adjunto embebido
(``/EmbeddedFiles``).
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject

from tests.export_document_support import sample_manifest_inputs
from vigia_platform.accreditation.export.document import generate_document

_FORBIDDEN_PRESENCE: frozenset[str] = frozenset(
    {"/JS", "/JavaScript", "/Launch", "/AA", "/OpenAction", "/AcroForm", "/EmbeddedFiles"}
)
"""Una llave con cualquiera de estos nombres, en cualquier objeto del grafo, es contenido
activo o un formulario: nunca aparece en un documento sin contenido activo (NFR-LAZ-18)."""

_FORBIDDEN_ACTION_SUBTYPES: frozenset[str] = frozenset(
    {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/RichMedia", "/URI"}
)
"""``/S`` de una acción (``/A``): cualquiera de estas sigue una referencia externa o ejecuta
algo. ``/URI`` incluido: ningún enlace sale del documento (NFR-LAZ-18)."""


def _resolved_dict(value: object) -> DictionaryObject | None:
    if isinstance(value, IndirectObject):
        value = value.get_object()
    return value if isinstance(value, DictionaryObject) else None


def _walk(value: object, seen: set[tuple[int, int]]) -> Iterator[DictionaryObject]:
    """Cada diccionario alcanzable desde ``value``, una sola vez por objeto indirecto."""
    if isinstance(value, IndirectObject):
        key = (value.idnum, value.generation)
        if key in seen:
            return
        seen.add(key)
        value = value.get_object()
    if isinstance(value, DictionaryObject):
        yield value
        for item in value.values():
            yield from _walk(item, seen)
    elif isinstance(value, ArrayObject):
        for item in value:
            yield from _walk(item, seen)


def findings(reader: PdfReader) -> list[str]:
    """Lo que un documento sin contenido activo nunca lleva: vacío si está limpio."""
    problems: list[str] = []
    seen: set[tuple[int, int]] = set()
    for node in _walk(reader.trailer["/Root"], seen):
        for key in _FORBIDDEN_PRESENCE:
            if key in node:
                problems.append(f"llave prohibida: {key}")
        action = _resolved_dict(node.get("/A"))
        if action is not None:
            subtype = action.get("/S")
            if str(subtype) in _FORBIDDEN_ACTION_SUBTYPES:
                problems.append(f"acción prohibida: {subtype}")
    return problems


def test_the_generated_document_has_no_active_content_or_external_reference() -> None:
    pdf_bytes = generate_document(sample_manifest_inputs())
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert findings(reader) == []


def test_the_generated_document_has_no_forms_and_no_embedded_attachments() -> None:
    pdf_bytes = generate_document(sample_manifest_inputs())
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert reader.get_fields() is None
    assert list(reader.attachments) == []


@pytest.mark.parametrize(
    "poisoned",
    [
        DictionaryObject({"/OpenAction": DictionaryObject({"/S": "/JavaScript"})}),
        DictionaryObject({"/AcroForm": DictionaryObject()}),
        DictionaryObject({"/Names": DictionaryObject({"/EmbeddedFiles": DictionaryObject()})}),
        DictionaryObject(
            {
                "/Pages": DictionaryObject(
                    {
                        "/Kids": ArrayObject(
                            [DictionaryObject({"/AA": DictionaryObject({"/O": "x"})})]
                        )
                    }
                )
            }
        ),
        DictionaryObject(
            {
                "/Pages": DictionaryObject(
                    {
                        "/Kids": ArrayObject(
                            [
                                DictionaryObject(
                                    {
                                        "/Annots": ArrayObject(
                                            [
                                                DictionaryObject(
                                                    {
                                                        "/A": DictionaryObject(
                                                            {
                                                                "/S": "/URI",
                                                                "/URI": "https://example.invalid",
                                                            }
                                                        )
                                                    }
                                                )
                                            ]
                                        )
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    ],
)
def test_the_detector_catches_each_kind_of_active_content(poisoned: DictionaryObject) -> None:
    """Sondeo negativo: sin esta detección, un catálogo con contenido activo pasaría limpio."""
    fake_reader = SimpleNamespace(trailer={"/Root": poisoned})
    assert findings(fake_reader) != []  # type: ignore[arg-type]


def test_a_clean_catalog_has_no_findings() -> None:
    fake_reader = SimpleNamespace(trailer={"/Root": DictionaryObject({"/Type": "/Catalog"})})
    assert findings(fake_reader) == []  # type: ignore[arg-type]
