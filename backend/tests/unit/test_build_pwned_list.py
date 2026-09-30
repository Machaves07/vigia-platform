"""Generador del respaldo local de filtradas (``tools/build_pwned_top100k.py``, TASK-122).

Con fuentes sintéticas: elige las de mayor cuenta (volcado ``<SHA-1>:<cuenta>``) o las primeras
de una lista ordenada, no escribe ninguna contraseña en claro, es reproducible (``--check``) y
lo que escribe lo carga ``LocalBreachList``.
"""

from __future__ import annotations

import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from tools.build_pwned_top100k import SourceError, main, top_from_hibp, top_from_plaintext
from vigia_platform.identity.adapters.hibp import LocalBreachList, sha1_hex


def _digest(n: int) -> str:
    return sha1_hex(f"sintetica-{n:04d}")


def test_hibp_dump_keeps_the_highest_counts_and_breaks_ties_by_hash() -> None:
    entries = {_digest(n): n % 7 for n in range(200)}
    lines = [f"{digest}:{entries[digest]}\r\n" for digest in sorted(entries)]
    top = top_from_hibp(lines, 50)
    expected = sorted(sorted(entries, key=lambda d: (-entries[d], d))[:50])
    assert top == expected
    assert top == sorted(top)


def test_hibp_dump_accepts_lowercase_and_rejects_garbage() -> None:
    assert top_from_hibp([f"{_digest(1).lower()}:3"], 1) == [_digest(1)]
    for bad in ("hola", f"{_digest(1)}", f"{_digest(1)}:x", f"{_digest(1)[:39]}:1"):
        with pytest.raises(SourceError):
            top_from_hibp([bad], 1)
    with pytest.raises(SourceError, match="repetido"):
        top_from_hibp([f"{_digest(1)}:1", f"{_digest(1)}:2"], 1)
    with pytest.raises(SourceError, match="repetido"):  # mayúsculas y minúsculas: el mismo hash
        top_from_hibp([f"{_digest(1)}:1", f"{_digest(1).lower()}:2"], 1)


def test_hibp_dump_out_of_order_is_rejected() -> None:
    """Sin orden no se detectarían los repetidos por adyacencia: se rechaza."""
    low, high = sorted((_digest(1), _digest(2)))
    with pytest.raises(SourceError, match="ordenado"):
        top_from_hibp([f"{high}:1", f"{low}:1"], 1)
    with pytest.raises(SourceError, match="ordenado"):  # un repetido no adyacente
        top_from_hibp([f"{low}:1", f"{high}:1", f"{low}:1"], 1)


def test_hibp_dump_is_read_with_bounded_memory() -> None:
    """Un volcado largo leído en flujo: la memoria no crece con el número de líneas (el volcado
    oficial tiene del orden de 10⁹; guardar cada hash para ver repetidos pediría > 100 GiB)."""
    lines_total = 300_000

    def dump() -> Iterator[str]:
        for n in range(lines_total):
            yield f"{n:040X}:{(n * 7919) % 1000}\n"

    tracemalloc.start()
    try:
        top = top_from_hibp(dump(), 10)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(top) == 10
    # Un conjunto con 300 000 hashes ocuparía unos 40 MB; el tope deja margen al intérprete.
    assert peak < 2 * 1024 * 1024


def test_plaintext_takes_the_first_distinct_entries_hashed() -> None:
    lines = ["123456\n", "123456\r\n", "\n", "clave\n", "qwerty\n", "otra\n"]
    top = top_from_plaintext(lines, 3)
    assert top == sorted(sha1_hex(p) for p in ("123456", "clave", "qwerty"))


@pytest.mark.parametrize("builder", [top_from_hibp, top_from_plaintext])
def test_short_sources_fail(builder: object) -> None:
    with pytest.raises(SourceError):
        builder(["solo-una\n" if builder is top_from_plaintext else f"{_digest(1)}:1"], 2)  # type: ignore[operator]


def test_cli_writes_a_loadable_file_without_plaintext(tmp_path: Path) -> None:
    source = tmp_path / "lista.txt"
    secrets = [f"sintetica-{n:04d}" for n in range(30)]
    source.write_text("".join(f"{s}\n" for s in secrets), encoding="utf-8")
    output = tmp_path / "resources" / "top.txt"
    args = ["--source", str(source), "--format", "plaintext", "--count", "20"]
    assert main([*args, "--output", str(output)]) == 0
    content = output.read_bytes()
    assert b"\r" not in content
    assert all(s.encode() not in content for s in secrets)
    loaded = LocalBreachList.from_file(output)
    assert len(loaded) == 20
    assert sha1_hex(secrets[0]) in loaded
    assert sha1_hex(secrets[25]) not in loaded
    # Reproducible: --check no ve diferencias; tras tocar el archivo, sí.
    assert main([*args, "--output", str(output), "--check"]) == 0
    output.write_text(content.decode()[:-41], encoding="ascii")
    assert main([*args, "--output", str(output), "--check"]) == 1


def test_cli_fails_without_writing_when_the_source_is_short(tmp_path: Path) -> None:
    source = tmp_path / "lista.txt"
    source.write_text("a\nb\n", encoding="utf-8")
    output = tmp_path / "top.txt"
    assert main(["--source", str(source), "--format", "plaintext", "--output", str(output)]) == 1
    assert not output.exists()
    missing = tmp_path / "no-existe.txt"
    assert main(["--source", str(missing), "--format", "hibp", "--output", str(output)]) == 1
