"""Verificación de los enlaces relativos de la documentación en Markdown (NFR-NUC-50, TASK-152).

Recorre los archivos ``.md`` indicados (o los de los directorios indicados, de forma recursiva)
y comprueba que cada enlace relativo resuelve:

- **Destino**: el archivo o directorio existe, con las mayúsculas exactas (GitHub y la
  canalización distinguen mayúsculas aunque Windows no lo haga) y sin salir de la raíz del
  repositorio.
- **Ancla**: ``#sección`` existe en el Markdown de destino, con el identificador que GitHub da a
  cada encabezado (minúsculas, sin puntuación, espacios a guiones, ``-1``, ``-2``… en los
  repetidos) o a un ``<a id="…">``/``<a name="…">``. En un archivo que no es Markdown solo se
  admiten anclas de línea (``#L10``, ``#L10-L20``).

Se revisan los enlaces en línea (``[texto](destino)``, también imágenes) y las definiciones de
referencia (``[id]: destino``); los en línea, por párrafo, así que también los que parten su
texto en varias líneas. No se revisan: los enlaces con esquema (``https:``, ``mailto:``…),
que no son relativos, ni lo que está dentro de bloques cercados (```` ``` ````, ``~~~``) o tramos
de código en línea. Los bloques sangrados sí se revisan (en una lista son párrafo). Un enlace
absoluto (``/ruta``) es un error: solo resuelve en la web de GitHub, no en un clon.

Uso: ``uv run --project backend python backend/tools/check_links.py README.md docs/`` desde la
raíz del repositorio. Sale con 0 si todo resuelve, 1 si hay enlaces rotos y 2 si un argumento no
existe o no es Markdown.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import unquote

_FENCE: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN: Final = re.compile(r"(`+)(?:(?!\1).)+?\1")
_INLINE_LINK: Final = re.compile(
    r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]"  # texto, con corchetes anidados de un nivel
    r"\(\s*(<[^<>\n]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"  # destino
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"  # título opcional
)
_REFERENCE: Final = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(<[^<>\n]*>|\S+)")
_HEADING: Final = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_HTML_ANCHOR: Final = re.compile(r"<a\s+[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.I)
_SCHEME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LINE_ANCHOR: Final = re.compile(r"L[0-9]+(?:-L[0-9]+)?")
_HEADING_LINK: Final = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG: Final = re.compile(r"<[^>]+>")


class UsageError(Exception):
    """Un argumento no existe o no es Markdown."""


@dataclass(frozen=True, slots=True)
class Link:
    source: Path
    line: int
    target: str


@dataclass(frozen=True, slots=True)
class Broken:
    link: Link
    reason: str

    def render(self, root: Path) -> str:
        where = f"{_display(self.link.source, root)}:{self.link.line}"
        return f"{where}: {self.link.target} ({self.reason})"


def _display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _prose_lines(text: str) -> Iterator[tuple[int, str]]:
    """Líneas fuera de bloques de código cercados, tal cual (con sus tramos de código)."""
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        match = _FENCE.match(line)
        if fence is not None:
            # Cierra el bloque una línea solo con el mismo carácter, al menos tantas veces.
            if (
                match
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                and not line.strip().lstrip(fence[0])
            ):
                fence = None
            continue
        if match:
            fence = match.group(1)
            continue
        # Los bloques sangrados no se saltan: en una lista son continuación de párrafo y sus
        # enlaces cuentan. Saltarlos dejaría enlaces sin revisar en silencio.
        yield number, line


def _without_code_spans(line: str) -> str:
    """``line`` con los tramos de código en línea en blanco: sus enlaces no cuentan."""
    return _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)


def extract_links(path: Path, text: str) -> list[Link]:
    """Enlaces en línea y definiciones de referencia de ``text``, fuera del código.

    Los enlaces en línea se buscan por **párrafo** (líneas seguidas no vacías), no por línea:
    GitHub reconoce un enlace cuyo texto ocupa varias líneas (``[texto\\nmás](destino)``). Un
    encabezado, una definición de referencia, una línea vacía o un bloque cercado cierran el
    párrafo. El enlace se informa en la línea donde empieza su ``[``.
    """
    links: list[Link] = []
    paragraph: list[tuple[int, str]] = []

    def flush() -> None:
        if not paragraph:
            return
        joined = "\n".join(line for _, line in paragraph)
        for match in _INLINE_LINK.finditer(joined):
            number = paragraph[0][0] + joined.count("\n", 0, match.start())
            links.append(Link(path, number, _strip_angle(match.group(1))))
        paragraph.clear()

    previous: int | None = None
    for number, raw in _prose_lines(text):
        line = _without_code_spans(raw)
        reference = _REFERENCE.match(line)
        heading = _HEADING.match(line)
        if previous is not None and number != previous + 1:
            flush()  # entre medias había un bloque cercado
        previous = number
        if reference:
            flush()
            links.append(Link(path, number, _strip_angle(reference.group(1))))
        elif heading or not line.strip():
            flush()
            if heading:
                paragraph.append((number, line))
                flush()
        else:
            paragraph.append((number, line))
    flush()
    return links


def _strip_angle(target: str) -> str:
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1]
    return target


def github_slug(heading: str) -> str:
    """Identificador que GitHub da a un encabezado (sin el sufijo de los repetidos)."""
    text = _HEADING_LINK.sub(r"\1", heading)
    text = _HTML_TAG.sub("", text)
    kept = "".join(ch for ch in text.lower() if ch.isalnum() or ch in " -_")
    return kept.replace(" ", "-")


def anchors_of(text: str) -> set[str]:
    """Anclas de un Markdown: encabezados (con ``-1``, ``-2``… si se repiten) y ``<a id>``."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    # Con la línea tal cual: GitHub conserva el texto de los tramos de código en el ancla.
    for _, line in _prose_lines(text):
        heading = _HEADING.match(line)
        if heading:
            slug = github_slug(heading.group(1) or "")
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
        anchors.update(match.group(1) for match in _HTML_ANCHOR.finditer(line))
    return anchors


def _exists(path: Path, root: Path, *, exact: bool) -> bool:
    """``path`` existe bajo ``root``, con las mayúsculas exactas o sin distinguirlas.

    Se recorre componente a componente con ``iterdir`` para dar el mismo resultado en un
    sistema de archivos que distingue mayúsculas (Linux, la canalización) y en uno que no
    (Windows, macOS).
    """
    current = root
    for part in path.relative_to(root).parts:
        try:
            names = {entry.name: entry for entry in current.iterdir()}
        except (NotADirectoryError, FileNotFoundError, PermissionError):
            return False
        if part in names:
            current = names[part]
            continue
        if exact:
            return False
        folded = [entry for name, entry in names.items() if name.casefold() == part.casefold()]
        if not folded:
            return False
        current = folded[0]
    return True


class Checker:
    """Comprueba enlaces con una caché de anclas por archivo de destino."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._anchors: dict[Path, set[str]] = {}

    def _anchors_for(self, path: Path) -> set[str]:
        if path not in self._anchors:
            self._anchors[path] = anchors_of(path.read_text(encoding="utf-8"))
        return self._anchors[path]

    def check(self, link: Link) -> str | None:
        """El motivo por el que ``link`` no resuelve, o ``None`` si resuelve o no es relativo."""
        target = link.target
        if not target or _SCHEME.match(target) or target.startswith("//"):
            return None if target else "destino vacío"
        path_part, _, fragment = target.partition("#")
        path_part = unquote(path_part.partition("?")[0])
        fragment = unquote(fragment)
        if path_part.startswith("/"):
            return "enlace absoluto: usa una ruta relativa"
        if path_part:
            destination = (link.source.parent / path_part).resolve()
            if not destination.is_relative_to(self.root):
                return "sale del repositorio"
            if not _exists(destination, self.root, exact=True):
                if _exists(destination, self.root, exact=False):
                    return "las mayúsculas no coinciden"
                return "no existe"
        else:
            destination = link.source
        if not fragment:
            return None
        if destination.is_dir():
            return "ancla sobre un directorio"
        if destination.suffix.lower() != ".md":
            return None if _LINE_ANCHOR.fullmatch(fragment) else "ancla en un archivo no Markdown"
        if fragment not in self._anchors_for(destination):
            return f"no existe el ancla #{fragment}"
        return None


def markdown_files(arguments: Sequence[str], cwd: Path) -> list[Path]:
    """Los ``.md`` de los argumentos: archivos tal cual, directorios de forma recursiva."""
    files: list[Path] = []
    for argument in arguments:
        path = (cwd / argument).resolve()
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.md") if p.is_file()))
        elif path.is_file() and path.suffix.lower() == ".md":
            files.append(path)
        elif path.exists():
            raise UsageError(f"no es Markdown: {argument}")
        else:
            raise UsageError(f"no existe: {argument}")
    return list(dict.fromkeys(files))


def check_files(files: Sequence[Path], root: Path) -> tuple[int, list[Broken]]:
    """Número de enlaces relativos revisados y enlaces rotos de ``files``."""
    checker = Checker(root)
    checked = 0
    broken: list[Broken] = []
    for path in files:
        for link in extract_links(path, path.read_text(encoding="utf-8")):
            if _SCHEME.match(link.target) or link.target.startswith("//"):
                continue
            checked += 1
            reason = checker.check(link)
            if reason is not None:
                broken.append(Broken(link, reason))
    return checked, broken


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_links.py",
        description="Comprueba que los enlaces relativos de la documentación resuelven.",
    )
    parser.add_argument("paths", nargs="+", metavar="RUTA", help="archivos .md o directorios")
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="raíz del repositorio (por omisión, el directorio actual)",
    )
    args = parser.parse_args(argv)
    cwd = Path.cwd()
    root = (args.root or cwd).resolve()
    try:
        files = markdown_files(args.paths, cwd)
    except UsageError as error:
        print(f"check_links: {error}", file=sys.stderr)
        return 2
    checked, broken = check_files(files, root)
    for item in broken:
        print(item.render(root))
    print(
        f"check_links: {len(files)} archivos, {checked} enlaces relativos, {len(broken)} rotos",
        file=sys.stderr,
    )
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
