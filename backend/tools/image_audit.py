"""Auditoría de una imagen guardada con ``docker save`` (TASK-143; NFR-NUC-23, 24; SECURITY-09).

Comprueba, sin arrancar la imagen, lo que la construcción promete:

- **Usuario sin privilegios**: ``Config.User`` existe y no es ``root`` ni ``0`` (ni ``0:…``).
- **Ninguna clave privada en el historial**: ni el texto de ``--needle-file`` ni un bloque PEM de
  clave privada en las órdenes de ``history`` (``docker history`` muestra lo mismo).
- **Ninguna clave privada en las capas**: ningún archivo de ninguna capa (también los que una capa
  posterior borra o tapa) contiene una línea del material de ``--needle-file`` ni un bloque PEM
  completo de clave privada (``-----BEGIN … PRIVATE KEY-----`` + cuerpo + ``-----END``); ningún
  archivo se llama como una clave de SSH (``id_rsa``, ``id_ed25519``…) ni está en un directorio
  ``.ssh`` salvo las huellas de servidores conocidos.

El material de ``--needle-file`` es la propia clave de despliegue que usó la construcción: se
buscan sus líneas de base64 (de 16 caracteres o más), no la cabecera, que también aparece como
constante en bibliotecas de criptografía. Las líneas nunca se imprimen.

Uso::

    docker save <imagen> -o imagen.tar
    uv run python tools/image_audit.py imagen.tar [--needle-file CLAVE ...] [--needle-env VAR ...]

Termina en 0 si todo cumple, en 1 si hay hallazgos (los nombra, sin mostrar el secreto) y en 2 si
el archivo no es una imagen guardada.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Final

__all__ = ["AuditError", "Finding", "audit_image", "main", "needles_from_key"]

MAX_SCANNED_BYTES: Final = 256 * 1024 * 1024
"""Tope de lectura por archivo: uno mayor se lee por trozos solapados, nunca entero."""
_CHUNK: Final = 4 * 1024 * 1024
MIN_NEEDLE_LENGTH: Final = 16
_PEM_PRIVATE_BLOCK: Final = re.compile(
    rb"-----BEGIN ((?:[A-Z0-9]+ )*)PRIVATE KEY-----[\r\n]+"
    rb"(?:[A-Za-z0-9+/=:, -]*[\r\n]+){0,8}?"
    rb"[A-Za-z0-9+/=]{40,}[\r\n]+"
    rb"(?:[A-Za-z0-9+/=]*[\r\n]+)*?"
    rb"-----END \1PRIVATE KEY-----"
)
"""Un bloque PEM completo con cuerpo; la cabecera sola (una constante) no cuenta."""
_SSH_KEY_NAME: Final = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(_sk)?$|^ssh_host_.*_key$")
_ALLOWED_IN_SSH_DIR: Final = frozenset(
    {"known_hosts", "ssh_known_hosts", "ssh_config", "moduli", "authorized_keys"}
)
_ROOT_USERS: Final = frozenset({"", "root", "0"})


class AuditError(Exception):
    """El archivo no es una imagen guardada con ``docker save`` (código 2)."""


@dataclass(frozen=True, slots=True)
class Finding:
    """Un incumplimiento; ``where`` nunca contiene el secreto."""

    where: str
    problem: str

    def __str__(self) -> str:
        return f"{self.where}: {self.problem}"


def needles_from_key(text: str) -> list[bytes]:
    """Líneas del material de una clave que delatan su presencia (sin cabeceras PEM)."""
    needles = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("-----") or len(line) < MIN_NEEDLE_LENGTH:
            continue
        needles.append(line.encode("ascii", errors="ignore"))
    return [needle for needle in dict.fromkeys(needles) if len(needle) >= MIN_NEEDLE_LENGTH]


def _scan_bytes(data: bytes, needles: Sequence[bytes]) -> str | None:
    for index, needle in enumerate(needles, start=1):
        if needle in data:
            return f"contiene la línea {index} del material de la clave"
    match = _PEM_PRIVATE_BLOCK.search(data)
    if match is not None:
        return "contiene un bloque PEM de clave privada"
    return None


def _scan_stream(stream: IO[bytes], needles: Sequence[bytes]) -> str | None:
    """Busca por trozos solapados (un bloque PEM o una línea partida entre dos trozos cuenta)."""
    overlap = max([len(n) for n in needles] + [16 * 1024])
    previous = b""
    read = 0
    while read < MAX_SCANNED_BYTES:
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        read += len(chunk)
        window = previous + chunk
        problem = _scan_bytes(window, needles)
        if problem is not None:
            return problem
        previous = window[-overlap:]
    return None


def _ssh_name_problem(path: PurePosixPath) -> str | None:
    if _SSH_KEY_NAME.match(path.name):
        return "archivo con nombre de clave de SSH"
    if ".ssh" in path.parts[:-1] and path.name not in _ALLOWED_IN_SSH_DIR:
        return "archivo dentro de un directorio .ssh"
    return None


def _layer_findings(
    layer: tarfile.TarFile, label: str, needles: Sequence[bytes]
) -> Iterator[Finding]:
    for member in layer:
        path = PurePosixPath(member.name.removeprefix("./"))
        if path.name.startswith(".wh."):
            continue  # marca de borrado de la capa: no tiene contenido
        if member.isfile() or member.issym() or member.islnk():
            name_problem = _ssh_name_problem(path)
            if name_problem is not None:
                yield Finding(f"{label}:/{path}", name_problem)
        if not member.isfile():
            continue
        handle = layer.extractfile(member)
        if handle is None:
            continue
        with handle:
            problem = _scan_stream(handle, needles)
        if problem is not None:
            yield Finding(f"{label}:/{path}", problem)


def _read_json(archive: tarfile.TarFile, name: str) -> Any:
    try:
        member = archive.getmember(name)
    except KeyError:
        raise AuditError(f"falta {name}: no es una imagen guardada con docker save") from None
    handle = archive.extractfile(member)
    if handle is None:
        raise AuditError(f"{name} no es un archivo")
    with handle:
        try:
            return json.loads(handle.read())
        except json.JSONDecodeError:
            raise AuditError(f"{name} no es JSON") from None


def _is_root(name: str) -> bool:
    """``root``, vacío o un UID numérico 0 en cualquier forma (``0``, ``00``, ``+0``)."""
    name = name.strip()
    if name in _ROOT_USERS:
        return True
    return name.lstrip("+").isdigit() and int(name) == 0


def _user_findings(config: dict[str, Any]) -> Iterable[Finding]:
    user = str((config.get("config") or {}).get("User") or "")
    if _is_root(user.split(":", 1)[0]):
        shown = user or "sin declarar (root)"
        yield Finding("config.User", f"la imagen corre como root ({shown})")


def _history_findings(config: dict[str, Any], needles: Sequence[bytes]) -> Iterable[Finding]:
    for index, entry in enumerate(config.get("history") or []):
        text = " ".join(str(entry.get(field) or "") for field in ("created_by", "comment")).encode(
            "utf-8", errors="replace"
        )
        problem = _scan_bytes(text, needles)
        if problem is not None:
            yield Finding(f"history[{index}]", problem)


def _audit_archive(archive: tarfile.TarFile, needles: Sequence[bytes]) -> list[Finding]:
    manifest = _read_json(archive, "manifest.json")
    if not isinstance(manifest, list) or len(manifest) != 1:
        raise AuditError("manifest.json debe describir exactamente una imagen")
    entry = manifest[0]
    config = _read_json(archive, str(entry.get("Config")))
    if not isinstance(config, dict):
        raise AuditError("la configuración de la imagen no es un objeto")
    layers = entry.get("Layers")
    if not isinstance(layers, list) or not layers:
        raise AuditError("la imagen no tiene capas")
    findings = [*_user_findings(config), *_history_findings(config, needles)]
    for number, name in enumerate(layers, start=1):
        try:
            member = archive.getmember(str(name))
        except KeyError:
            raise AuditError(f"falta la capa {name}") from None
        handle = archive.extractfile(member)
        if handle is None:
            raise AuditError(f"la capa {name} no es un archivo")
        with handle, tarfile.open(fileobj=handle, mode="r|*") as layer:
            findings.extend(_layer_findings(layer, f"capa {number}", needles))
    return findings


def audit_image(path: Path, needles: Sequence[bytes] = ()) -> list[Finding]:
    """Hallazgos de la imagen guardada en ``path`` (vacío si cumple)."""
    try:
        with tarfile.open(path, mode="r:*") as archive:
            return _audit_archive(archive, needles)
    except (OSError, tarfile.TarError) as error:
        raise AuditError(f"no se pudo leer {path}: {error}") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audita una imagen guardada: usuario no root y ninguna clave privada."
    )
    parser.add_argument("image", type=Path, help="archivo de docker save")
    parser.add_argument(
        "--needle-file",
        type=Path,
        action="append",
        default=[],
        metavar="ARCHIVO",
        help="clave cuyo material no debe aparecer (se puede repetir)",
    )
    parser.add_argument(
        "--needle-env",
        action="append",
        default=[],
        metavar="VARIABLE",
        help="variable de entorno con la clave (la canalización no la escribe en disco)",
    )
    args = parser.parse_args(argv)
    try:
        keys = [key.read_text(encoding="ascii", errors="ignore") for key in args.needle_file]
        for name in args.needle_env:
            if not os.environ.get(name):
                raise AuditError(f"la variable {name} está vacía o no existe")
            keys.append(os.environ[name])
        needles = [needle for key in keys for needle in needles_from_key(key)]
        if keys and not needles:
            raise AuditError("la clave no tiene material que buscar")
        findings = audit_image(args.image, needles)
    except (AuditError, OSError) as error:
        print(f"image_audit: {error}", file=sys.stderr)
        return 2
    for finding in findings:
        print(f"  FALLA  {finding}", file=sys.stderr)
    if findings:
        print(f"image_audit: {len(findings)} hallazgo(s)", file=sys.stderr)
        return 1
    print(
        f"image_audit: usuario no root; ninguna clave privada en el historial ni en las capas"
        f" ({len(needles)} líneas de material buscadas)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
