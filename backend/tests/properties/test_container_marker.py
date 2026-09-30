"""Marca del contenedor MP4 (TASK-121; BR-CTR-14, NFR-NUC-33) y selección de la muestra diaria.

- ``read_container_marker`` coincide con el oráculo de U-01 (``container_marked`` del stub de
  conformidad) en todo contenedor sintético legible, y **nunca** lanza: bytes arbitrarios,
  tamaños imposibles, cajas de 64 bits, truncados y anidamiento dan ``UNREADABLE`` o
  ``ABSENT``, jamás una excepción ni ``PRESENT`` sin la marca exacta.
- Variantes hostiles de la marca (espacios, mayúsculas, homoglifos, caracteres invisibles,
  ``=0``, prefijo o sufijo) no cuentan como marca.
- ``select_sample`` es reproducible con la semilla: no depende del orden ni de repetidos, devuelve
  exactamente ``min(size, población)`` identificadores de la población y cambia con la semilla.
- ``sample_size``: 1 % redondeado hacia arriba, al menos 10, nunca más que la población.

Solo datos generados: contenedores sintéticos de ``synthetic_clip`` y bytes aleatorios.
"""

from __future__ import annotations

import struct
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st
from vigia_contracts.conformance.stub_platform.objects import container_marked, synthetic_clip

from vigia_platform.ledger.application.evidence_sample import (
    SAMPLE_MINIMUM,
    SEED_BYTES,
    sample_size,
    select_sample,
)
from vigia_platform.ledger.container_marker import (
    CONTAINER_MARK,
    ContainerMarker,
    read_container_marker,
)


def _box(kind: bytes, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


def _itunes_clip(comment: bytes, *, iso_meta: bool = True, trailing: bytes = b"") -> bytes:
    """Contenedor mínimo con ``moov/udta/meta/ilst/©cmt/data`` (la forma de ``ffmpeg``)."""
    value = _box(b"data", struct.pack(">II", 1, 0), comment)
    handler = _box(b"hdlr", bytes(8), b"mdirappl", bytes(9))
    meta = _box(
        b"meta", *((bytes(4),) if iso_meta else ()), handler, _box(b"ilst", _box(b"\xa9cmt", value))
    )
    return b"".join(
        (
            _box(b"ftyp", b"isom", struct.pack(">I", 512), b"isomiso2mp41"),
            _box(b"mdat", b"sintetico"),
            _box(b"moov", _box(b"udta", meta)),
            trailing,
        )
    )


def _quicktime_clip(comment: bytes) -> bytes:
    """``moov/udta/©cmt`` de QuickTime: longitud, idioma y texto."""
    item = _box(b"\xa9cmt", struct.pack(">HH", len(comment), 0), comment)
    return b"".join(
        (
            _box(b"ftyp", b"qt  ", bytes(4)),
            _box(b"moov", _box(b"udta", item)),
        )
    )


def test_marked_and_unmarked_synthetic_clips() -> None:
    assert read_container_marker(synthetic_clip("a")) is ContainerMarker.PRESENT
    assert read_container_marker(synthetic_clip("a", marked=False)) is ContainerMarker.ABSENT
    assert read_container_marker(_itunes_clip(CONTAINER_MARK)) is ContainerMarker.PRESENT
    assert read_container_marker(_itunes_clip(CONTAINER_MARK, iso_meta=False)) is (
        ContainerMarker.PRESENT
    )
    assert read_container_marker(_quicktime_clip(CONTAINER_MARK)) is ContainerMarker.PRESENT
    assert read_container_marker(_itunes_clip(CONTAINER_MARK + b"\x00\x00")) is (
        ContainerMarker.PRESENT
    )


@pytest.mark.parametrize(
    "comment",
    [
        b"",
        b"vigia_anonymized=0",
        b"vigia_anonymized=1 ",
        b" vigia_anonymized=1",
        b"VIGIA_ANONYMIZED=1",
        b"vigia-anonymized=1",
        b"vigia_anonymized=11",
        b"xvigia_anonymized=1",
        b"vigia_anonym\xd1\x96zed=1",  # i cirílica en UTF-8
        b"vigia_anonymized=1\xe2\x80\x8b",  # espacio de ancho cero en UTF-8
        b"vigia_anonymized=\xef\xbc\x91",  # dígito 1 de ancho completo en UTF-8
        b"vigia_anonymized=1\x00x",
    ],
)
def test_hostile_variants_of_the_marker_are_absent(comment: bytes) -> None:
    assert read_container_marker(_itunes_clip(comment)) is ContainerMarker.ABSENT
    assert read_container_marker(_quicktime_clip(comment)) is ContainerMarker.ABSENT


def test_marker_outside_the_comment_tag_does_not_count() -> None:
    # La marca en el mdat o en otra etiqueta no es la etiqueta comment.
    other_tag = _itunes_clip(CONTAINER_MARK).replace(b"\xa9cmt", b"\xa9nam")
    in_mdat = b"".join(
        (
            _box(b"ftyp", b"isom", bytes(4)),
            _box(b"mdat", CONTAINER_MARK),
            _box(b"moov", _box(b"udta")),
        )
    )
    assert read_container_marker(other_tag) is ContainerMarker.ABSENT
    assert read_container_marker(in_mdat) is ContainerMarker.ABSENT


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xff\xd8\xff\xfe\x00\x14vigia_anonymized=1\xff\xd9",  # JPEG con la marca: no es MP4
        _box(b"moov", _box(b"udta")),  # sin ftyp
        _box(b"ftyp", b"isom", bytes(4)),  # sin moov
        _box(b"ftyp", b"isom") + b"\x00\x00\x00",  # resto de menos de 8 bytes
        struct.pack(">I4s", 4, b"ftyp"),  # tamaño menor que la cabecera
        struct.pack(">I4s", 1000, b"ftyp") + bytes(8),  # tamaño mayor que el archivo
        struct.pack(">I4sQ", 1, b"ftyp", 2**63),  # 64 bits imposible
        struct.pack(">I4s", 1, b"ftyp") + bytes(4),  # 64 bits truncado
        _box(b"ftyp", b"isom", bytes(4))
        + struct.pack(">I4s", 0, b"moov")
        + _box(b"udta"),  # size 0 válido al final: moov con udta vacío → legible
        _itunes_clip(CONTAINER_MARK)[:-5],  # truncado a mitad de la marca
        _box(b"ftyp", b"isom", bytes(4))
        + _box(b"moov", _box(b"udta", struct.pack(">I4s", 99, b"\xa9cmt"))),  # hija fuera
        _box(b"ftyp", b"isom", bytes(4))
        + _box(b"moov", _box(b"udta", _box(b"\xa9cmt", struct.pack(">HH", 50, 0), b"corto"))),
    ],
)
def test_malformed_containers_are_unreadable_or_absent_never_present(content: bytes) -> None:
    assert read_container_marker(content) in {ContainerMarker.UNREADABLE, ContainerMarker.ABSENT}


def test_size_zero_is_only_valid_for_the_last_top_level_box() -> None:
    last = _box(b"ftyp", b"isom", bytes(4)) + struct.pack(">I4s", 0, b"moov") + _box(b"udta")
    assert read_container_marker(last) is ContainerMarker.ABSENT
    nested = _box(b"ftyp", b"isom", bytes(4)) + _box(
        b"moov", struct.pack(">I4s", 0, b"udta"), bytes(8)
    )
    assert read_container_marker(nested) is ContainerMarker.UNREADABLE


def test_deep_nesting_and_huge_input_do_not_blow_up() -> None:
    deep = b"x"
    for _ in range(2000):
        deep = _box(b"udta", deep)
    content = _box(b"ftyp", b"isom", bytes(4)) + _box(b"moov", deep)
    assert read_container_marker(content) in {ContainerMarker.UNREADABLE, ContainerMarker.ABSENT}
    big = _itunes_clip(CONTAINER_MARK, trailing=_box(b"free", bytes(8 * 1024 * 1024)))
    assert read_container_marker(big) is ContainerMarker.PRESENT


def test_non_bytes_input_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        read_container_marker("vigia_anonymized=1")  # type: ignore[arg-type]


@given(st.binary(max_size=512))
def test_arbitrary_bytes_never_raise(content: bytes) -> None:
    result = read_container_marker(content)
    if result is ContainerMarker.PRESENT:
        assert CONTAINER_MARK in content


@given(label=st.text(max_size=40), marked=st.booleans())
def test_agrees_with_the_u01_stub_oracle(label: str, marked: bool) -> None:
    content = synthetic_clip(label, marked=marked)
    expected = ContainerMarker.PRESENT if container_marked(content) else ContainerMarker.ABSENT
    assert read_container_marker(content) is expected


@given(comment=st.binary(max_size=40), iso=st.booleans())
def test_itunes_form_agrees_with_the_oracle(comment: bytes, iso: bool) -> None:
    content = _itunes_clip(comment, iso_meta=iso)
    expected = ContainerMarker.PRESENT if container_marked(content) else ContainerMarker.ABSENT
    assert read_container_marker(content) is expected


@given(st.binary(max_size=256), st.integers(min_value=0, max_value=400))
def test_truncating_a_marked_clip_never_reports_present_without_the_mark(
    tail: bytes, cut: int
) -> None:
    content = (_itunes_clip(CONTAINER_MARK) + tail)[:cut]
    result = read_container_marker(content)
    if result is ContainerMarker.PRESENT:
        assert CONTAINER_MARK in content


# --- Selección de la muestra ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("population", "expected"),
    [
        (0, 0),
        (1, 1),
        (9, 9),
        (10, 10),
        (11, 10),
        (1_000, 10),
        (1_001, 11),
        (1_100, 11),
        (1_101, 12),
        (100_000, 1_000),
    ],
)
def test_sample_size_is_one_percent_rounded_up_and_at_least_ten(
    population: int, expected: int
) -> None:
    assert sample_size(population) == expected


@pytest.mark.parametrize("population", [-1, 1.5, True, "10"])
def test_sample_size_rejects_non_counts(population: object) -> None:
    with pytest.raises(ValueError):
        sample_size(population)  # type: ignore[arg-type]


@given(
    seed=st.binary(min_size=SEED_BYTES, max_size=SEED_BYTES),
    ids=st.lists(st.uuids(), max_size=60),
    size=st.integers(min_value=0, max_value=70),
    order=st.randoms(use_true_random=False),
)
def test_selection_is_reproducible_and_order_independent(
    seed: bytes, ids: list[uuid.UUID], size: int, order: object
) -> None:
    selected = select_sample(seed, ids, size)
    shuffled = [*ids, *ids[:3]]
    order.shuffle(shuffled)  # type: ignore[attr-defined]
    assert select_sample(seed, shuffled, size) == selected
    assert len(selected) == min(size, len(set(ids)))
    assert len(set(selected)) == len(selected)
    assert set(selected) <= set(ids)
    # Un prefijo de la muestra mayor es la muestra menor (orden por rango fijo).
    assert select_sample(seed, ids, size + 5)[: len(selected)] == selected


def test_selection_changes_with_the_seed() -> None:
    ids = [uuid.UUID(int=n) for n in range(1, 1_001)]
    first = select_sample(bytes(32), ids, SAMPLE_MINIMUM)
    second = select_sample(bytes(31) + b"\x01", ids, SAMPLE_MINIMUM)
    assert first != second


@pytest.mark.parametrize("seed", [b"", bytes(31), bytes(33), "0" * 32])
def test_selection_rejects_a_seed_of_the_wrong_size(seed: object) -> None:
    with pytest.raises(ValueError):
        select_sample(seed, [uuid.uuid4()], 1)  # type: ignore[arg-type]


def test_selection_rejects_a_negative_size() -> None:
    with pytest.raises(ValueError):
        select_sample(bytes(32), [uuid.uuid4()], -1)
