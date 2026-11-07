"""Firmante de ``vigia-node-ca`` por archivo, **solo para integración** (TASK-543; Q6; A-76 D3).

LocalStack comunitario no persiste KMS: cada reinicio recrearía la clave de la autoridad y dejaría
inválidas todas las credenciales de nodo. En el entorno de integración la clave vive en un archivo
``0600`` (``VIGIA_NODE_CA_KEY_FILE``) y sobrevive. En producción sigue valiendo U-02 §7.1: la clave
privada nunca existe fuera de KMS. ``RuntimeConfig.from_environ`` detiene el arranque con
``node_ca_backend_forbidden`` si ``VIGIA_NODE_CA_BACKEND=file`` fuera de ``local`` e
``integration``.

``FileNodeCaSigner`` cumple el puerto ``shared.node_ca.NodeCaSigner``, el mismo que ``KmsAdapter``:
``sign_certificate`` y ``sign_revocation_list`` siguen firmando en dos pasadas y comprobando la
firma, así que emisión, rotación y lista de revocación no divergen. ``FileBackedKms`` lo enchufa
en la composición: ``sign`` y ``get_public_key`` van al archivo y el resto de KMS (el cifrado de
sobre de ``vigia-secrets``) sigue en KMS.

**Identificador de clave**: ``file:<nombre>`` es ``<nombre>`` con el sufijo y en el directorio de
``VIGIA_NODE_CA_KEY_FILE``, sin rutas: ``file:node-ca`` para ``/ca/node-ca.pem``, y
``--new-key-id file:node-ca-2`` en una rotación (D-6).

**Creación** (A-89): solo las claves de ``creatable``, y solo al firmar por primera vez.
``vigia-admin`` las nombra: la configurada con ``bootstrap --first-install`` y la de
``--new-key-id`` en ``rotate-node-ca``. Sin eso, una clave que no existe es un error, también con
el depósito vacío: un volumen mal montado no crea otra autoridad. La clave
pública de una clave aún por crear sale de una clave en memoria: ``--dry-run`` y ``check_key`` no
escriben nada. Se publica por ``os.link`` desde un temporal ``0600``: el archivo aparece entero o no
aparece. Entre dos procesos a la vez queda una sola clave: el perdedor firma con la del ganador, así
que su raíz no verifica con la pública que leyó (``NodeCaError``, fallo cerrado) y basta repetir. Un
``vigia-api`` o un ``vigia-worker`` sin archivo no crean ninguno: su comprobación de arranque falla
(NFR-GOB-20).

La clave se lee en cada operación, sin caché. Ningún mensaje, ``repr`` ni registro lleva el PEM ni
la ruta. El archivo debe ser regular (``O_NOFOLLOW``) y sin permisos para el grupo ni para otros.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from vigia_platform.shared.node_ca import NodeCaError
from vigia_platform.shared.secrets import AwsSettings, KmsAdapter

__all__ = ["FileBackedKms", "FileNodeCaSigner"]

_KEY_ID: Final = re.compile(r"file:([a-z0-9][a-z0-9-]{0,62})")
_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _read_key(path: Path) -> ec.EllipticCurvePrivateKey | None:
    """La clave de ``path``, o ``None`` si no existe. ``NodeCaError`` si no se puede usar."""
    try:
        descriptor = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return None
    except OSError:
        raise NodeCaError("el archivo de la clave de la autoridad no se puede abrir") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise NodeCaError("el archivo de la clave de la autoridad debe ser regular y 0600")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read()
    finally:
        os.close(descriptor)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise NodeCaError("el archivo de la clave de la autoridad no es una clave PEM") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise NodeCaError("la clave de la autoridad debe ser de curva elíptica")
    return key


class FileNodeCaSigner:
    """``NodeCaSigner`` sobre claves EC en archivos ``0600`` (ver el módulo)."""

    def __init__(self, key_file: Path, *, creatable: frozenset[str] = frozenset()) -> None:
        self._directory = key_file.parent
        self._suffix = key_file.suffix
        self._creatable = creatable
        self._pending: dict[Path, ec.EllipticCurvePrivateKey] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "FileNodeCaSigner()"

    async def get_public_key(self, key_id: str) -> bytes:
        return await asyncio.to_thread(self._public_der, key_id)

    async def sign(self, key_id: str, message: bytes) -> bytes:
        return await asyncio.to_thread(self._sign, key_id, message)

    def _path(self, key_id: str) -> Path:
        match = _KEY_ID.fullmatch(key_id)
        if match is None:
            raise NodeCaError("con el backend file la clave es file:<nombre>, sin rutas")
        return self._directory / f"{match.group(1)}{self._suffix}"

    def _pending_key(self, key_id: str, path: Path) -> ec.EllipticCurvePrivateKey:
        if key_id not in self._creatable:
            raise NodeCaError(
                "la clave de la autoridad no existe. En una primera instalación usa "
                "bootstrap --first-install; si ya había nodos, comprueba el volumen de la clave"
            )
        key = self._pending.get(path)
        if key is None:
            key = ec.generate_private_key(ec.SECP256R1())
            self._pending[path] = key
        return key

    def _public_der(self, key_id: str) -> bytes:
        path = self._path(key_id)
        with self._lock:
            key = _read_key(path)
            if key is None:
                key = self._pending_key(key_id, path)
        return key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def _sign(self, key_id: str, message: bytes) -> bytes:
        path = self._path(key_id)
        with self._lock:
            key = _read_key(path)
            if key is None:
                key = self._persist(key_id, path)
        return key.sign(message, ec.ECDSA(hashes.SHA256()))

    def _persist(self, key_id: str, path: Path) -> ec.EllipticCurvePrivateKey:
        """Publica la clave pendiente, o la que otro proceso publicó antes."""
        pending = self._pending_key(key_id, path)
        pem = pending.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        try:
            descriptor, temporary = tempfile.mkstemp(dir=self._directory, prefix=".node-ca-")
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(pem)
                    output.flush()
                    os.fsync(output.fileno())
                with contextlib.suppress(FileExistsError):
                    os.link(temporary, path)
            finally:
                os.unlink(temporary)
        except OSError:
            raise NodeCaError("no se pudo escribir la clave de la autoridad") from None
        del self._pending[path]
        stored = _read_key(path)
        if stored is None:
            raise NodeCaError("no se pudo escribir la clave de la autoridad")
        return stored


class FileBackedKms(KmsAdapter):
    """``KmsAdapter`` cuya firma de la autoridad va al archivo; el cifrado de sobre sigue en KMS."""

    def __init__(self, settings: AwsSettings, signer: FileNodeCaSigner, **options: Any) -> None:
        super().__init__(settings, **options)
        self._signer = signer

    async def sign(self, key_id: str, message: bytes) -> bytes:
        return await self._signer.sign(key_id, message)

    async def get_public_key(self, key_id: str) -> bytes:
        return await self._signer.get_public_key(key_id)
