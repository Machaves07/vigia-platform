"""Verificador de paquetes exportados: lectura, resultado y línea de órdenes (LC-NUC-18, BR-NUC-57).

Módulo **puro**: solo biblioteca estándar y compatible con Python 3.10. ``tools/build_verifier.py``
lo copia, detrás de ``pure_rfc8785``, ``pure_ed25519`` y ``chain_walk``, en el verificador de un
archivo ``tools/vigia_verify.py`` que U-04 incluye en cada exportación y paquete mensual
(NFR-NUC-53): funciona sin red, sin acceso al sistema y sin ningún secreto del proveedor.

Uso: ``python vigia_verify.py <paquete> [--previous-checkpoint <ruta> ...] [--out resultado.json]``.

- ``<paquete>``: un directorio o un ``.zip`` con ``manifest.json`` y un archivo JSON Lines por
  cadena (formato en ``docs/package-format.md``).
- ``--previous-checkpoint``: un punto de control de un paquete anterior (la línea del registro, en
  un archivo ``.json``) o el paquete anterior entero (se toma el último punto de control de cada
  cadena). Se comprueba su firma y que el paquete actual contiene en esa secuencia el mismo
  registro: si difiere, alguien reescribió el prefijo de la cadena.
- ``--out``: archivo de resultado JSON. El resumen legible, en español, va a la salida estándar.

Código de salida: 0 solo si el paquete está íntegro (``intact``); 1 si alguna cadena está rota o
el paquete no se puede leer (``broken``); 2 si la orden está mal escrita.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import dataclasses
import itertools
import json
import math
import re
import sys
import zipfile
from collections.abc import Generator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from vigia_platform.ledger.chain.chain_walk import (
    CHAIN_KINDS,
    MESSAGES,
    Break,
    ChainRef,
    ChainResult,
    ChainWalker,
)

__all__ = [
    "EXIT_BROKEN",
    "EXIT_INTACT",
    "EXIT_USAGE",
    "FORMAT",
    "FORMAT_VERSION",
    "PackageError",
    "main",
    "parse_json",
    "render_summary",
    "verify_package",
]

FORMAT: Final = "vigia-package"
FORMAT_VERSION: Final = 1
"""Versión del formato del paquete que este verificador lee."""

MANIFEST: Final = "manifest.json"
MAX_LINE_BYTES: Final = 4 * 1024 * 1024
"""Tope de una línea del paquete: un registro lleva a lo sumo 256 KB de contenido canónico."""
MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024
"""Tope del manifiesto."""

EXIT_INTACT: Final = 0
EXIT_BROKEN: Final = 1
EXIT_USAGE: Final = 2

_MAX_SAFE_INTEGER: Final = 2**53 - 1
_UUID_TEXT: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64_TEXT: Final = re.compile(r"[0-9a-f]{64}")
_PUBLIC_KEY: Final = re.compile(r"[A-Za-z0-9+/]{43}=")
_KEY_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_MEMBER: Final = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}){0,3}"
)
"""Ruta relativa de un archivo del paquete: sin ``..``, sin raíz y sin barra inversa."""
_RAW_ID: Final = re.compile(
    rb'"(?:record_id|entry_id)"\s*:\s*"'
    rb'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
)
"""Identificador de una línea ilegible, para nombrar el registro aunque la línea no se lea."""
_CHAIN_KEYS: Final = frozenset(
    {"file", "first_sequence", "kind", "last_hash", "last_sequence", "plant_id"}
)
_KEY_KEYS: Final = frozenset({"key_id", "public_key"})

PACKAGE_MESSAGES: Final = {
    "package_unreadable": "el paquete no se puede leer",
    "manifest_invalid": "el manifiesto del paquete no es válido",
    "unsupported_format": "el formato del paquete no es el que lee este verificador",
    "previous_checkpoint_invalid": "el punto de control anterior no es válido",
    "previous_checkpoint_chain_missing": (
        "el paquete no contiene la cadena del punto de control anterior"
    ),
}


class PackageError(Exception):
    """El paquete o un punto de control anterior no se pueden leer."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail

    def message(self) -> str:
        text = PACKAGE_MESSAGES.get(self.reason, self.reason)
        return f"{text}: {self.detail}" if self.detail else text


# --- Lectura estricta de JSON ---------------------------------------------------------------------


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} no es un número JSON")


def _read_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("número fuera del rango de un doble")
    return value


def _read_int(text: str) -> int | float:
    value = int(text)
    if -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
        return value
    return _read_float(text)  # RFC 8785 lee todo número como doble


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("el objeto repite una clave")
    return document


def parse_json(data: bytes) -> object:
    """Documento JSON de ``data`` (UTF-8) con la lectura de RFC 8785.

    Rechaza con ``ValueError`` lo que no se puede volver a canonicalizar: UTF-8 inválido,
    ``NaN`` e infinitos, números fuera del doble y claves repetidas. Un entero de magnitud mayor
    que ``2**53 - 1`` se lee como doble, igual que lo escribió RFC 8785.
    """
    try:
        text = data.decode("utf-8")
        document: object = json.loads(
            text,
            parse_constant=_reject_constant,
            parse_float=_read_float,
            parse_int=_read_int,
            object_pairs_hook=_unique_keys,
        )
    except RecursionError:
        raise ValueError("anidamiento demasiado profundo") from None
    except UnicodeDecodeError:
        raise ValueError("no es UTF-8 válido") from None
    return document


# --- Fuente del paquete: directorio o zip -------------------------------------------------------


class _Source:
    """Archivos de un paquete por nombre relativo."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._zip: zipfile.ZipFile | None = None
        if path.is_dir():
            return
        try:
            self._zip = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as error:
            raise PackageError(
                "package_unreadable", f"no es un directorio ni un zip ({error})"
            ) from None

    def names(self) -> list[str]:
        if self._zip is not None:
            return sorted(name for name in self._zip.namelist() if not name.endswith("/"))
        return sorted(
            child.relative_to(self.path).as_posix()
            for child in self.path.rglob("*")
            if child.is_file()
        )

    def open(self, name: str) -> IO[bytes]:
        if _MEMBER.fullmatch(name) is None:
            raise PackageError("manifest_invalid", f"ruta de archivo no permitida {name!r}")
        try:
            if self._zip is not None:
                return self._zip.open(name)
            root = self.path.resolve()
            target = (root / name).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise PackageError("package_unreadable", f"falta el archivo {name!r}")
            return target.open("rb")
        except KeyError:
            raise PackageError("package_unreadable", f"falta el archivo {name!r}") from None
        except OSError as error:
            raise PackageError("package_unreadable", f"{name!r}: {error}") from None

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def _read_all(source: _Source, name: str, limit: int) -> bytes:
    with source.open(name) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise PackageError("manifest_invalid", f"{name!r} supera {limit} bytes")
    return data


# --- Manifiesto -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainSpec:
    """Una cadena declarada en el manifiesto."""

    ref: ChainRef
    file: str
    first_sequence: int
    last_sequence: int
    last_hash: str


@dataclass(frozen=True)
class Manifest:
    organization_id: str
    chains: tuple[ChainSpec, ...]
    public_keys: dict[str, bytes]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _chain_spec(organization_id: str, item: object) -> ChainSpec:
    if not isinstance(item, dict) or item.keys() != _CHAIN_KEYS:
        raise PackageError("manifest_invalid", "una cadena no tiene las claves esperadas")
    kind, plant_id, name = item["kind"], item["plant_id"], item["file"]
    first, last, last_hash = item["first_sequence"], item["last_sequence"], item["last_hash"]
    if kind not in CHAIN_KINDS or not isinstance(kind, str):
        raise PackageError("manifest_invalid", f"tipo de cadena desconocido {kind!r}")
    if not (plant_id is None or (isinstance(plant_id, str) and _UUID_TEXT.fullmatch(plant_id))):
        raise PackageError("manifest_invalid", "plant_id no es un UUID")
    if kind == "audit" and plant_id is not None:
        raise PackageError("manifest_invalid", "la cadena de auditoría no lleva planta")
    if not isinstance(name, str) or _MEMBER.fullmatch(name) is None or name == MANIFEST:
        raise PackageError("manifest_invalid", f"ruta de archivo no permitida {name!r}")
    if not (_is_int(first) and _is_int(last) and isinstance(first, int) and isinstance(last, int)):
        raise PackageError("manifest_invalid", "secuencias de la cadena no enteras")
    if first < 1 or last < first - 1:
        raise PackageError("manifest_invalid", "rango de secuencias de la cadena no válido")
    if not isinstance(last_hash, str) or _HEX64_TEXT.fullmatch(last_hash) is None:
        raise PackageError("manifest_invalid", "last_hash no es un SHA-256 hexadecimal")
    return ChainSpec(ChainRef(kind, organization_id, plant_id), name, first, last, last_hash)


def _public_keys(items: object) -> dict[str, bytes]:
    if not isinstance(items, list):
        raise PackageError("manifest_invalid", "checkpoint_keys no es una lista")
    keys: dict[str, bytes] = {}
    for item in items:
        if not isinstance(item, dict) or item.keys() != _KEY_KEYS:
            raise PackageError("manifest_invalid", "una clave no tiene las claves esperadas")
        key_id, text = item["key_id"], item["public_key"]
        if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
            raise PackageError("manifest_invalid", "key_id no válido")
        if not isinstance(text, str) or _PUBLIC_KEY.fullmatch(text) is None:
            raise PackageError("manifest_invalid", f"clave pública no válida para {key_id!r}")
        if key_id in keys:
            raise PackageError("manifest_invalid", f"key_id repetido {key_id!r}")
        try:
            key = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise PackageError(
                "manifest_invalid", f"clave pública no válida para {key_id!r}"
            ) from None
        if base64.b64encode(key).decode("ascii") != text:
            # Base64 no canónico: otro texto para la misma clave no es la clave publicada.
            raise PackageError(
                "manifest_invalid", f"clave pública en base64 no canónico para {key_id!r}"
            )
        keys[key_id] = key
    return keys


def read_manifest(source: _Source) -> Manifest:
    """El manifiesto del paquete, validado; ``PackageError`` si no se puede usar."""
    try:
        document = parse_json(_read_all(source, MANIFEST, MAX_MANIFEST_BYTES))
    except ValueError as error:
        raise PackageError("manifest_invalid", str(error)) from None
    if not isinstance(document, dict):
        raise PackageError("manifest_invalid", "no es un objeto JSON")
    if document.get("format") != FORMAT:
        raise PackageError("unsupported_format", f"se esperaba format={FORMAT!r}")
    version = document.get("format_version")
    if not _is_int(version) or version != FORMAT_VERSION:
        raise PackageError(
            "unsupported_format",
            f"format_version {version!r}; este verificador lee la versión {FORMAT_VERSION}:"
            " use el verificador incluido en el paquete",
        )
    organization_id = document.get("organization_id")
    if not isinstance(organization_id, str) or _UUID_TEXT.fullmatch(organization_id) is None:
        raise PackageError("manifest_invalid", "organization_id no es un UUID")
    chains_document = document.get("chains")
    if not isinstance(chains_document, list) or not chains_document:
        raise PackageError("manifest_invalid", "chains debe ser una lista no vacía")
    chains = tuple(_chain_spec(organization_id, item) for item in chains_document)
    if len({spec.ref for spec in chains}) != len(chains):
        raise PackageError("manifest_invalid", "una cadena aparece dos veces")
    if len({spec.file for spec in chains}) != len(chains):
        raise PackageError("manifest_invalid", "dos cadenas comparten archivo")
    return Manifest(organization_id, chains, _public_keys(document.get("checkpoint_keys")))


# --- Entradas -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Unreadable:
    """Línea que no es un documento JSON legible."""

    raw_id: str | None
    detail: str


def _iter_lines(source: _Source, name: str) -> Generator[tuple[int, object], None, None]:
    """``(número de línea, entrada)`` de un archivo JSON Lines; las líneas vacías se saltan."""
    with source.open(name) as stream:
        number = 0
        while True:
            line = stream.readline(MAX_LINE_BYTES + 1)
            if not line:
                return
            number += 1
            if len(line) > MAX_LINE_BYTES:
                yield number, _Unreadable(None, f"la línea {number} supera {MAX_LINE_BYTES} bytes")
                return
            stripped = line.strip(b" \t\r\n")
            if not stripped:
                continue
            try:
                yield number, parse_json(stripped)
            except ValueError as error:
                match = _RAW_ID.search(stripped)
                raw_id = match.group(1).decode("ascii") if match else None
                yield number, _Unreadable(raw_id, f"línea {number}: {error}")


# --- Puntos de control anteriores ---------------------------------------------------------------


@dataclass
class PreviousCheckpoint:
    """Un punto de control de un paquete anterior, que ancla el prefijo de su cadena."""

    source: str
    chain: ChainRef | None
    sequence: int | None
    entry_id: str | None
    entry_hash: str | None
    status: str = "pending"
    reason: str | None = None
    detail: str = ""


def _is_checkpoint(entry: object) -> bool:
    return isinstance(entry, dict) and (
        entry.get("record_type") == "checkpoint" or entry.get("operation") == "checkpoint"
    )


def _checkpoint_from_entry(
    origin: str, entry: object, public_keys: dict[str, bytes]
) -> PreviousCheckpoint:
    """Valida por sí sola la entrada de un punto de control y devuelve su ancla."""
    invalid = PreviousCheckpoint(
        origin, None, None, None, None, "invalid", "previous_checkpoint_invalid"
    )
    if not isinstance(entry, dict) or not _is_checkpoint(entry):
        invalid.detail = "no es un registro de punto de control"
        return invalid
    audit = "entry_id" in entry
    organization_id, plant_id = entry.get("organization_id"), entry.get("plant_id")
    sequence, previous_hash = entry.get("chain_sequence"), entry.get("previous_hash")
    if not (
        isinstance(organization_id, str)
        and (audit or plant_id is None or isinstance(plant_id, str))
        and _is_int(sequence)
        and isinstance(sequence, int)
        and sequence >= 1
        and isinstance(previous_hash, str)
    ):
        invalid.detail = "le faltan la cadena, la secuencia o el enlace"
        return invalid
    chain = ChainRef(
        "audit" if audit else "ledger",
        organization_id,
        None if audit or not isinstance(plant_id, str) else plant_id,
    )
    walker = ChainWalker(chain, public_keys, start_sequence=sequence - 1, start_hash=previous_hash)
    failure = walker.feed(entry)
    invalid.chain, invalid.sequence = chain, sequence
    if failure is not None:
        invalid.entry_id = failure.entry_id
        invalid.detail = failure.message()
        return invalid
    result = walker.finish()
    seen = result.checkpoints[0]
    return PreviousCheckpoint(origin, chain, sequence, seen.entry_id, seen.entry_hash)


def load_previous_checkpoints(
    path: Path, public_keys: dict[str, bytes]
) -> list[PreviousCheckpoint]:
    """Puntos de control de ``path``: un ``.json`` con una entrada (o una lista) o un paquete.

    Falla cerrado: si ``path`` no se puede leer o no contiene ningún punto de control, devuelve
    una sola ancla ``invalid`` (quien pidió comprobar el prefijo no recibe un ``intact`` vacío).
    """
    origin = str(path)
    found: list[PreviousCheckpoint] = []
    try:
        if path.is_file() and not zipfile.is_zipfile(path):
            try:
                document = parse_json(path.read_bytes())
            except (OSError, ValueError) as error:
                raise PackageError("previous_checkpoint_invalid", str(error)) from None
            entries = document if isinstance(document, list) else [document]
            found = [_checkpoint_from_entry(origin, entry, public_keys) for entry in entries]
        else:
            source = _Source(path)
            try:
                manifest = read_manifest(source)
                for spec in manifest.chains:
                    last: object = None
                    for _, entry in _iter_lines(source, spec.file):
                        if _is_checkpoint(entry):
                            last = entry
                    if last is not None:
                        found.append(
                            _checkpoint_from_entry(f"{origin}:{spec.file}", last, public_keys)
                        )
            finally:
                source.close()
    except PackageError as error:
        detail = error.message()
    else:
        if found:
            return found
        detail = "no contiene ningún punto de control"
    return [
        PreviousCheckpoint(
            origin, None, None, None, None, "invalid", "previous_checkpoint_invalid", detail
        )
    ]


# --- Verificación ---------------------------------------------------------------------------------


@dataclass
class ChainReport:
    spec: ChainSpec
    result: ChainResult
    broken_line: int | None = None


@dataclass
class PackageReport:
    """Resultado de verificar un paquete."""

    package: str
    organization_id: str | None = None
    chains: list[ChainReport] = field(default_factory=list)
    previous: list[PreviousCheckpoint] = field(default_factory=list)
    package_error: PackageError | None = None
    unlisted_files: list[str] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return (
            self.package_error is None
            and bool(self.chains)
            and all(chain.result.intact for chain in self.chains)
            and all(previous.status == "matched" for previous in self.previous)
        )


def _walk_chain(
    source: _Source, spec: ChainSpec, public_keys: dict[str, bytes], anchors: dict[int, str]
) -> ChainReport:
    lines = _iter_lines(source, spec.file)
    try:
        first = next(lines, None)
        start_hash: str | None = None
        if spec.first_sequence > 1 and first is not None and isinstance(first[1], dict):
            # Paquete que no empieza en la génesis: el enlace con el prefijo viene del paquete.
            candidate = first[1].get("previous_hash")
            start_hash = candidate if isinstance(candidate, str) else None
        walker = ChainWalker(
            spec.ref,
            public_keys,
            start_sequence=spec.first_sequence - 1,
            start_hash=start_hash,
            anchors=anchors,
        )
        for number, entry in itertools.chain([first] if first is not None else [], lines):
            failure = walker.feed(entry)
            if failure is None:
                continue
            result = walker.finish()
            if isinstance(entry, _Unreadable):
                # La línea no se pudo leer: se nombra el registro si su identificador se lee.
                failure = Break(failure.sequence, entry.raw_id, "malformed", entry.detail)
                result = dataclasses.replace(result, broken=failure)
            return ChainReport(spec, result, number)
        return ChainReport(spec, walker.finish((spec.last_sequence, spec.last_hash)))
    finally:
        lines.close()


def verify_package(path: Path, previous_paths: Sequence[Path] = ()) -> PackageReport:
    """Verifica el paquete de ``path`` y, si se dan, los puntos de control anteriores."""
    report = PackageReport(str(path))
    try:
        source = _Source(path)
    except PackageError as error:
        report.package_error = error
        return report
    try:
        manifest = read_manifest(source)
        report.organization_id = manifest.organization_id
        for previous_path in previous_paths:
            report.previous.extend(load_previous_checkpoints(previous_path, manifest.public_keys))
        by_chain = {spec.ref: spec for spec in manifest.chains}
        anchors: dict[ChainRef, dict[int, str]] = {spec.ref: {} for spec in manifest.chains}
        for previous in report.previous:
            if previous.status != "pending" or previous.chain is None:
                continue
            if previous.chain not in by_chain:
                previous.status, previous.reason = (
                    "chain_missing",
                    "previous_checkpoint_chain_missing",
                )
                continue
            if previous.sequence is None or previous.entry_hash is None:
                continue
            chain_anchors = anchors[previous.chain]
            if chain_anchors.setdefault(previous.sequence, previous.entry_hash) != (
                previous.entry_hash
            ):
                previous.status, previous.reason = "invalid", "previous_checkpoint_invalid"
                previous.detail = "dos puntos de control anteriores discrepan en la misma secuencia"
        for spec in manifest.chains:
            report.chains.append(_walk_chain(source, spec, manifest.public_keys, anchors[spec.ref]))
        results = {chain.spec.ref: chain.result for chain in report.chains}
        for previous in report.previous:
            if previous.status != "pending" or previous.chain is None:
                continue
            if previous.sequence in results[previous.chain].anchors_matched:
                previous.status = "matched"
            else:
                previous.status, previous.reason = "not_matched", "previous_checkpoint_mismatch"
        listed = {MANIFEST, *(spec.file for spec in manifest.chains)}
        report.unlisted_files = [name for name in source.names() if name not in listed]
    except PackageError as error:
        report.package_error = error
    finally:
        source.close()
    return report


# --- Resultado ------------------------------------------------------------------------------------


def _break_document(failure: Break | None, line: int | None) -> dict[str, object] | None:
    if failure is None:
        return None
    return {
        "sequence": failure.sequence,
        "entry_id": failure.entry_id,
        "reason": failure.reason,
        "message": failure.message(),
        "line": line,
    }


def _chain_document(chain: ChainReport) -> dict[str, object]:
    result, spec = chain.result, chain.spec
    last_checkpoint = result.checkpoints[-1].sequence if result.checkpoints else None
    return {
        "kind": spec.ref.kind,
        "organization_id": spec.ref.organization_id,
        "plant_id": spec.ref.plant_id,
        "file": spec.file,
        "status": result.status,
        "from_genesis": spec.first_sequence == 1,
        "first_sequence": result.first_sequence,
        "last_sequence": result.last_sequence,
        "last_hash": result.last_hash,
        "entries": result.entries,
        "checkpoints_verified": len(result.checkpoints),
        "last_checkpoint_sequence": last_checkpoint,
        "entries_after_last_checkpoint": (
            result.last_sequence - last_checkpoint
            if last_checkpoint is not None
            else result.entries
        ),
        "previous_checkpoints_matched": list(result.anchors_matched),
        "broken": _break_document(result.broken, chain.broken_line),
    }


def _previous_document(previous: PreviousCheckpoint) -> dict[str, object]:
    reason = previous.reason
    message = None
    if reason is not None:
        message = PACKAGE_MESSAGES.get(reason) or MESSAGES.get(reason, reason)
        if previous.detail:
            message = f"{message}: {previous.detail}"
    return {
        "source": previous.source,
        "kind": previous.chain.kind if previous.chain else None,
        "organization_id": previous.chain.organization_id if previous.chain else None,
        "plant_id": previous.chain.plant_id if previous.chain else None,
        "sequence": previous.sequence,
        "entry_id": previous.entry_id,
        "status": previous.status,
        "reason": reason,
        "message": message,
    }


def report_document(report: PackageReport) -> dict[str, object]:
    """El resultado como documento JSON (el archivo de ``--out``)."""
    first_broken: dict[str, object] | None = None
    for chain in report.chains:
        if chain.result.broken is not None:
            first_broken = {
                "kind": chain.spec.ref.kind,
                "plant_id": chain.spec.ref.plant_id,
                **(_break_document(chain.result.broken, chain.broken_line) or {}),
            }
            break
    error = report.package_error
    return {
        "verifier": {"name": "vigia_verify", "format_version": FORMAT_VERSION},
        "package": report.package,
        "status": "intact" if report.intact else "broken",
        "organization_id": report.organization_id,
        "package_error": (
            None if error is None else {"reason": error.reason, "message": error.message()}
        ),
        "chains": [_chain_document(chain) for chain in report.chains],
        "previous_checkpoints": [_previous_document(previous) for previous in report.previous],
        "first_broken": first_broken,
        "unlisted_files": report.unlisted_files,
    }


def render_summary(report: PackageReport) -> str:
    """Resumen legible en español."""
    lines = [
        f"Verificador de paquetes de Vigía (formato {FORMAT_VERSION})",
        f"Paquete: {report.package}",
    ]
    if report.package_error is not None:
        lines.append(f"ERROR: {report.package_error.message()}")
    for chain in report.chains:
        result, name = chain.result, chain.spec.ref.describe()
        if result.intact:
            span = (
                f"secuencias {result.first_sequence} a {result.last_sequence}"
                if result.entries
                else "sin registros"
            )
            lines.append(
                f"- {name}: íntegra ({span}; {result.entries} registros; "
                f"{len(result.checkpoints)} puntos de control verificados)"
            )
            if chain.spec.first_sequence > 1:
                lines.append(
                    f"  El paquete empieza en la secuencia {chain.spec.first_sequence}: el enlace "
                    "con el prefijo se toma del propio paquete."
                )
        elif result.broken is not None:
            failure = result.broken
            where = f"en la secuencia {failure.sequence}" if failure.sequence is not None else ""
            who = f", registro {failure.entry_id}" if failure.entry_id else ""
            line = f", línea {chain.broken_line}" if chain.broken_line else ""
            lines.append(f"- {name}: ROTA {where}{who}{line}: {failure.message()}")
    for previous in report.previous:
        document = _previous_document(previous)
        chain_name = previous.chain.describe() if previous.chain else "cadena desconocida"
        if previous.status == "matched":
            lines.append(
                f"- Punto de control anterior ({chain_name}, secuencia {previous.sequence}): "
                "coincide; el prefijo no cambió."
            )
        else:
            lines.append(
                f"- Punto de control anterior ({chain_name}, secuencia {previous.sequence}): "
                f"NO COINCIDE: {document['message']}"
            )
    if report.unlisted_files:
        lines.append(
            "Aviso: archivos del paquete que el manifiesto no declara: "
            + ", ".join(report.unlisted_files)
        )
    lines.append("Resultado: ÍNTEGRO" if report.intact else "Resultado: ROTO")
    return "\n".join(lines) + "\n"


# --- Línea de órdenes -----------------------------------------------------------------------------


class _Formatter(argparse.HelpFormatter):
    """Ayuda con el prefijo de uso en español."""

    def add_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[Any],
        prefix: str | None = None,
    ) -> None:
        super().add_usage(usage, actions, groups, "uso: " if prefix is None else prefix)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vigia_verify.py",
        description=(
            "Verifica, sin red y sin acceso a la plataforma, la integridad de un paquete exportado "
            "de Vigía: hashes, enlaces de cada cadena y firmas de los puntos de control."
        ),
        epilog=(
            "Código de salida: 0 si el paquete está íntegro; 1 si está roto o no se puede leer; "
            "2 si la orden es incorrecta."
        ),
        formatter_class=_Formatter,
        add_help=False,
    )
    parser._positionals.title = "argumentos"
    parser._optionals.title = "opciones"
    parser.add_argument(
        "package", metavar="paquete", type=Path, help="directorio o .zip del paquete"
    )
    parser.add_argument(
        "--previous-checkpoint",
        dest="previous",
        metavar="RUTA",
        type=Path,
        action="append",
        default=[],
        help=(
            "punto de control de un paquete anterior (archivo .json con el registro) o el paquete "
            "anterior entero; se puede repetir"
        ),
    )
    parser.add_argument(
        "--out", metavar="resultado.json", type=Path, help="archivo de resultado JSON"
    )
    parser.add_argument("-h", "--help", action="help", help="muestra esta ayuda y termina")
    parser.add_argument(
        "--version",
        action="version",
        version=f"vigia_verify (formato de paquete {FORMAT_VERSION})",
        help="muestra la versión y termina",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Punto de entrada: 0 si íntegro, 1 si roto, 2 si la orden es incorrecta."""
    arguments = _parser().parse_args(argv)
    for path in (arguments.package, *arguments.previous):
        if not path.exists():
            print(f"vigia_verify.py: error: no existe {str(path)!r}", file=sys.stderr)
            return EXIT_USAGE
    report = verify_package(arguments.package, arguments.previous)
    sys.stdout.write(render_summary(report))
    if arguments.out is not None:
        text = json.dumps(report_document(report), ensure_ascii=False, indent=2) + "\n"
        try:
            arguments.out.write_text(text, encoding="utf-8")
        except OSError as error:
            print(
                f"vigia_verify.py: error: no se pudo escribir {str(arguments.out)!r}: {error}",
                file=sys.stderr,
            )
            return EXIT_USAGE
    return EXIT_INTACT if report.intact else EXIT_BROKEN


if __name__ == "__main__":
    raise SystemExit(main())
