"""Primer arranque del entorno de integración: secretos y volúmenes (TASK-545, VIG-393).

Corre en el servicio ``init`` de ``compose.integration.yml`` (como root, solo para esto) antes que
PostgreSQL y LocalStack. Es **idempotente y nunca sustituye** lo que ya existe: cambiar la
contraseña de PostgreSQL o el material de la clave KMS en un entorno con datos lo dejaría roto sin
aviso.

En ``/state`` (volumen ``estado``, ``0700``, del usuario 10001) deja, cada archivo ``0600``:

- ``postgres.pw``, ``app.pw``, ``migrate.pw``: contraseñas del superusuario ``vigia`` y de los roles
  ``vigia_app`` y ``vigia_migrate`` (las crea la migración ``nuc_0001``);
- ``pgpass``: el archivo de contraseñas de libpq del superusuario (``PGPASSFILE``);
- ``kms-secrets.material`` y ``kms-secrets.id``: 32 bytes en base64 y un UUID, el material y el
  identificador de la clave KMS ``vigia-secrets`` (``localstack/state.py`` la recrea con los mismos
  tras cada reinicio; el resto de la plataforma la nombra por su alias, ``alias/vigia-secrets``);
- ``enrollment-source.key``: 32 bytes en base64, la clave estable del hash de origen del alta.

Y prepara ``/ca`` (clave de la autoridad de nodos), ``/edge`` (certificado de servidor del
balanceador) y ``/shared`` (identificador de la organización proveedora, que no es secreto) con
propietario 10001. Nada de esto sale por la salida estándar: solo nombres de archivo.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import signal
import sys
import threading
import uuid
from pathlib import Path
from typing import Final

__all__ = [
    "OWNER",
    "ensure_breach_list",
    "ensure_directory",
    "ensure_state",
    "main",
    "prepare_directories",
]

OWNER: Final = 10001
_SECRET_FILES: Final = ("postgres.pw", "app.pw", "migrate.pw")
MATERIAL_FILE: Final = "kms-secrets.material"
ENROLLMENT_KEY_FILE: Final = "enrollment-source.key"
SNAPSHOT_FILE: Final = "localstack-snapshot.json"
_ID_FILE: Final = "kms-secrets.id"
_SHA1_LINE: Final = re.compile(r"[0-9A-F]{40}")
_FALLBACK_WORDS: Final = ("password", "123456", "qwerty")
BREACH_LIST_NAME: Final = "pwned-top100k.txt"
BREACH_KIND_FILE: Final = "breach_kind"


def _chown(path: Path, owner: int | None) -> None:
    if owner is not None:
        os.chown(path, owner, owner)


def ensure_directory(path: Path, mode: int, owner: int | None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    _chown(path, owner)


def _create_private(path: Path, content: str, owner: int | None) -> bool:
    """Crea ``path`` ``0600`` **entero o nada**. ``False`` si ya existía (no se toca).

    Se escribe un temporal ``0600`` y se publica con ``os.link``, que falla si ``path`` existe: de
    dos ``init`` a la vez gana uno, y nadie lee nunca un archivo a medio escribir.
    """
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(content + "\n")
            output.flush()
            os.fsync(output.fileno())
        _chown(temporary, owner)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(temporary)


def _replace_private(path: Path, content: str, owner: int | None) -> None:
    """Sustituye ``path`` de una vez (``0600``): quien lo lee ve el anterior o el nuevo."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        output.write(content + "\n")
    _chown(temporary, owner)
    os.replace(temporary, path)


def _random_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def ensure_state(state: Path, *, owner: int | None = OWNER) -> list[str]:
    """Crea los secretos que falten en ``state``; devuelve los nombres de los creados.

    El material de la clave KMS se crea **primero** y solo en una primera instalación: si ya hay
    otros archivos de ``state`` (contraseñas, identificador de la clave, copia de LocalStack) y
    falta ``kms-secrets.material``, se pierde ese material y crear otro dejaría la clave con el
    mismo identificador y otro material: todo texto cifrado antes (el segundo factor de las
    personas) daría ``InvalidCiphertextException`` sin ningún error de arranque. Se detiene.
    """
    ensure_directory(state, 0o700, owner)
    material = state / MATERIAL_FILE
    established = [
        name for name in (*_SECRET_FILES, _ID_FILE, SNAPSHOT_FILE) if (state / name).exists()
    ]
    if established and not material.exists():
        raise SystemExit(
            f"init: kms_material_missing: falta {MATERIAL_FILE} y ya hay {', '.join(established)}. "
            "Crear otro material dejaría ilegible el segundo factor de las personas: restaura el "
            "volumen estado o, si es una primera instalación deliberada, vacíalo entero"
        )
    created: list[str] = []
    if _create_private(material, _random_key(), owner):
        created.append(MATERIAL_FILE)
    if _create_private(state / _ID_FILE, str(uuid.uuid4()), owner):
        created.append(_ID_FILE)
    for name in _SECRET_FILES:
        if _create_private(state / name, secrets.token_urlsafe(32), owner):
            created.append(name)
    if _create_private(state / ENROLLMENT_KEY_FILE, _random_key(), owner):
        created.append(ENROLLMENT_KEY_FILE)
    # El archivo de libpq se rehace siempre a partir de postgres.pw: es derivado, no un original.
    password = (state / "postgres.pw").read_text(encoding="ascii").strip()
    _replace_private(state / "pgpass", f"postgres:5432:vigia:vigia:{password}", owner)
    return created


def prepare_directories(root: Path, owner: int | None = OWNER) -> None:
    """``/ca`` y ``/edge`` (0700, del usuario 10001) y ``/shared`` (0755), bajo ``root``."""
    ensure_directory(root / "ca", 0o700, owner)
    ensure_directory(root / "edge", 0o700, owner)
    ensure_directory(root / "shared", 0o755, owner)


def ensure_breach_list(shared: Path, source: Path, *, owner: int | None = OWNER) -> str:
    """El respaldo local de contraseñas filtradas de ``vigia-api`` (``VIGIA_BREACH_LIST_PATH``).

    ``vigia-api`` no arranca sin él, y la imagen no lo trae: se genera aparte con
    ``tools/build_pwned_top100k.py`` (necesita red y una fuente de SecLists o de HIBP). Si el
    archivo ``source`` existe, se valida (un SHA-1 en mayúsculas por línea, ordenado) y se copia a
    ``shared/breach.txt``. Si no, se escribe una lista **sintética de tres entradas**, la de las
    pruebas: basta para un entorno de datos sintéticos y no protege a personas reales. Devuelve
    ``real`` o ``sintetica``.
    """
    target = shared / "breach.txt"
    if source.is_file():
        lines = source.read_text(encoding="ascii").splitlines()
        if not lines or lines != sorted(lines) or not all(_SHA1_LINE.fullmatch(x) for x in lines):
            raise SystemExit(
                f"{source.name} no es un respaldo válido (SHA-1 en mayúsculas, ordenado)"
            )
        content, kind = "\n".join(lines) + "\n", "real"
    else:
        digests = sorted(
            hashlib.sha1(word.encode(), usedforsecurity=False).hexdigest().upper()
            for word in _FALLBACK_WORDS
        )
        content, kind = "\n".join(digests) + "\n", "sintetica"
    temporary = shared / ".breach.tmp"
    temporary.write_text(content, encoding="ascii")
    os.chmod(temporary, 0o644)
    _chown(temporary, owner)
    os.replace(temporary, target)
    # Lo que `seed.py provider-id` y `bootstrap.sh` muestran: una lista sintética no se oculta.
    _replace_private(shared / BREACH_KIND_FILE, kind, owner)
    os.chmod(shared / BREACH_KIND_FILE, 0o644)
    return kind


def main(argv: list[str] | None = None) -> int:
    hold = "--hold" in (sys.argv[1:] if argv is None else argv)
    created = ensure_state(Path("/state"))
    prepare_directories(Path("/"))
    kind = ensure_breach_list(Path("/shared"), Path("/integration") / BREACH_LIST_NAME)
    print("init: " + (", ".join(created) if created else "nada que crear"))
    print(f"init: lista de contraseñas filtradas {kind}", flush=True)
    if hold:
        # Sano y dormido: ver el comentario del servicio `init` en compose.integration.yml.
        Path("/tmp/init-done").write_text("listo\n", encoding="ascii")  # noqa: S108
        stop = threading.Event()
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: stop.set())
        stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
