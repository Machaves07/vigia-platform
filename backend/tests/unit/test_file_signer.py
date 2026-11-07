"""Firmante de ``vigia-node-ca`` por archivo y su selector (TASK-543; Q6).

La parte de procesos y contenedores (arranque real, ``bootstrap``, reinicio de LocalStack y
sumideros) es ``tests/integration/test_node_ca_backend_guard.py``.

Solo claves generadas en cada prueba.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import io
import ipaddress
import logging
import os
import stat
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from tests.admin_support import MemoryStorage, issue_node_certificate, validates
from vigia_platform.fleet.adapters.ca import file_signer as file_signer_module
from vigia_platform.fleet.adapters.ca.certificate_profiles import NodeCaIssuer
from vigia_platform.fleet.adapters.ca.file_signer import FileBackedKms, FileNodeCaSigner
from vigia_platform.identity.application.admin_cli import AdminConfig
from vigia_platform.identity.application.admin_cli import run as admin_run
from vigia_platform.node_api.certificate_profile import NodeSubject
from vigia_platform.shared.api.app import AppConfig
from vigia_platform.shared.clock import SystemClock
from vigia_platform.shared.node_ca import (
    NodeCaError,
    NodeCaPublisher,
    build_root_certificate,
    read_bundle,
    sign_revocation_list,
)
from vigia_platform.shared.runtime.config import (
    NodeCaBackendForbidden,
    RuntimeConfig,
    RuntimeConfigInvalid,
)
from vigia_platform.shared.secrets import AwsSettings
from vigia_platform.shared.worker.main import WorkerConfig

NOW: Final = dt.datetime(2026, 11, 6, 9, 30, tzinfo=dt.UTC)
KEY_ID: Final = "file:node-ca"
CREATABLE: Final = frozenset({KEY_ID, "file:node-ca-2"})
WORKERS: Final = 8
WAIT_SECONDS: Final = 60.0
FIFO_SECONDS: Final = 15.0
"""Tope para ver que abrir una tubería no se cuelga: solo se agota si la guarda falta."""
OVERLAP_SECONDS: Final = 10.0
"""Cuánto retiene la lectura a un hilo esperando a otro: solo decide con el candado quitado."""
DB_SECRET_NAME: Final = "/".join(("vigia", "integracion", "db", "app"))
"""El nombre de un secreto, no un valor: gitleaks lo confundiría si fuera un literal."""


def _sync_public(signer: FileNodeCaSigner, key_id: str = KEY_ID) -> bytes:
    return asyncio.run(signer.get_public_key(key_id))


def _sync_sign(signer: FileNodeCaSigner, message: bytes, key_id: str = KEY_ID) -> bytes:
    return asyncio.run(signer.sign(key_id, message))


def _root(signer: FileNodeCaSigner, key_id: str = KEY_ID) -> x509.Certificate:
    return asyncio.run(
        build_root_certificate(
            signer, key_id, now=NOW, environment="integration", random_bytes=os.urandom
        )
    )


def _write_key(path: Path, key: Any, mode: int = 0o600) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(mode)


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    return tmp_path / "node-ca.pem"


@pytest.fixture
def signer(key_file: Path) -> FileNodeCaSigner:
    return FileNodeCaSigner(key_file, creatable=CREATABLE)


# --- Firma: certificado, lista y raíz ------------------------------------------------------------


def test_a_certificate_issued_with_the_file_key_verifies_with_the_root(
    signer: FileNodeCaSigner,
) -> None:
    root = _root(signer)
    root.verify_directly_issued_by(root)
    leaf = asyncio.run(issue_node_certificate(signer, KEY_ID, root, NOW))
    leaf.verify_directly_issued_by(root)
    assert validates(leaf, (root,), NOW + dt.timedelta(minutes=5))


def test_a_revocation_list_signed_with_the_file_key_verifies(signer: FileNodeCaSigner) -> None:
    root = _root(signer)
    public = root.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(root.subject)
        .last_update(NOW)
        .next_update(NOW + dt.timedelta(days=7))
    )
    revocation_list = asyncio.run(
        sign_revocation_list(builder, signer, KEY_ID, issuer_public_key=public)
    )
    assert revocation_list.is_signature_valid(public)


def test_an_enrollment_issues_both_certificates_with_the_file_key(key_file: Path) -> None:
    """La emisión real del alta (``NodeCaIssuer``) con el firmante por archivo."""
    signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
    storage = MemoryStorage()
    publisher = NodeCaPublisher(
        storage=storage, kms=signer, environment="integration", random_bytes=os.urandom
    )
    published = asyncio.run(publisher.publish_root(KEY_ID, now=NOW))
    issuer = NodeCaIssuer(
        kms=signer, key_id=KEY_ID, roots=storage, clock=SystemClock(), random_bytes=os.urandom
    )
    subject = NodeSubject(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    issued = asyncio.run(
        issuer.issue(
            subject,
            ec.generate_private_key(ec.SECP256R1()).public_key(),
            ec.generate_private_key(ec.SECP256R1()).public_key(),
            ipaddress.ip_address("192.168.20.69"),
            now=NOW,
        )
    )
    issued.client.verify_directly_issued_by(published.root)
    issued.server.verify_directly_issued_by(published.root)
    assert issued.root == published.root


def test_the_root_of_the_same_key_keeps_its_name_so_old_leaves_keep_validating(
    key_file: Path,
) -> None:
    """Tras perder ``ca/root.pem`` (LocalStack), otra raíz de la misma clave valida lo emitido."""
    signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
    first = _root(signer)
    leaf = asyncio.run(issue_node_certificate(signer, KEY_ID, first, NOW))
    again = _root(FileNodeCaSigner(key_file))
    assert again.subject == first.subject
    assert again.serial_number != first.serial_number
    assert validates(leaf, (again,), NOW + dt.timedelta(minutes=5))


def test_bootstrap_and_rotation_publish_the_two_root_bundle(key_file: Path) -> None:
    signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
    storage = MemoryStorage()
    publisher = NodeCaPublisher(
        storage=storage, kms=signer, environment="integration", random_bytes=os.urandom
    )
    published = asyncio.run(publisher.publish_root(KEY_ID, now=NOW))
    rotated = asyncio.run(publisher.rotate_root("file:node-ca-2", now=NOW))
    assert len(rotated.bundle) == 2
    assert rotated.bundle[0] == published.root
    assert (key_file.parent / "node-ca-2.pem").is_file()


# --- La clave en disco ---------------------------------------------------------------------------


def test_the_key_is_created_0600_only_when_it_first_signs(
    signer: FileNodeCaSigner, key_file: Path
) -> None:
    public = _sync_public(signer)
    assert list(key_file.parent.iterdir()) == []  # --dry-run y check_key no escriben
    _sync_sign(signer, b"mensaje")
    assert [path.name for path in key_file.parent.iterdir()] == [key_file.name]
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert _sync_public(signer) == public


def test_the_key_survives_a_new_process(signer: FileNodeCaSigner, key_file: Path) -> None:
    _sync_sign(signer, b"mensaje")
    reopened = FileNodeCaSigner(key_file)
    assert _sync_public(reopened) == _sync_public(signer)
    signature = _sync_sign(reopened, b"otro")
    public = serialization.load_der_public_key(_sync_public(signer))
    assert isinstance(public, ec.EllipticCurvePublicKey)
    public.verify(signature, b"otro", ec.ECDSA(hashes.SHA256()))


@pytest.mark.parametrize("operation", ["public", "sign"])
def test_a_process_that_cannot_create_never_creates_the_key(key_file: Path, operation: str) -> None:
    """``vigia-api`` y ``vigia-worker`` sin archivo: su comprobación de arranque falla."""
    reader = FileNodeCaSigner(key_file)
    with pytest.raises(NodeCaError):
        if operation == "public":
            _sync_public(reader)
        else:
            _sync_sign(reader, b"mensaje")
    assert list(key_file.parent.iterdir()) == []


@pytest.mark.parametrize(
    "key_id",
    [
        "alias/vigia-node-ca",
        "arn:aws:kms:us-east-1:000000000000:key/x",
        "file:",
        "file:../fuera",
        "file:/etc/x",
        "file:Mayusculas",
        "file:a/b",
    ],
)
def test_only_a_plain_file_key_id_is_accepted(key_file: Path, key_id: str) -> None:
    """Aunque se nombre como creable: solo el patrón lo rechaza (sin rutas fuera del directorio)."""
    signer = FileNodeCaSigner(key_file, creatable=frozenset({key_id}))
    with pytest.raises(NodeCaError):
        _sync_public(signer, key_id)


@pytest.mark.parametrize("mode", [0o602, 0o604, 0o620, 0o640, 0o644, 0o660])
def test_a_key_others_can_read_is_refused(key_file: Path, mode: int) -> None:
    _write_key(key_file, ec.generate_private_key(ec.SECP256R1()), mode=mode)
    with pytest.raises(NodeCaError):
        _sync_public(FileNodeCaSigner(key_file))


def test_a_fifo_is_refused_without_hanging(key_file: Path) -> None:
    """Sin ``O_NONBLOCK``, abrir una tubería sin escritor bloquearía el proceso para siempre."""
    os.mkfifo(key_file, 0o600)
    outcome: list[object] = []

    def read() -> None:
        try:
            outcome.append(FileNodeCaSigner(key_file)._public_der(KEY_ID))
        except NodeCaError as error:
            outcome.append(error)

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(timeout=FIFO_SECONDS)
    try:
        assert not worker.is_alive(), "abrir el archivo de la clave se quedó colgado"
    finally:
        if worker.is_alive():  # suelta el hilo: un lector bloqueado admite un escritor
            with contextlib.suppress(OSError):
                os.close(os.open(key_file, os.O_WRONLY | os.O_NONBLOCK))
    assert isinstance(outcome[0], NodeCaError)


def test_an_encrypted_key_is_refused(key_file: Path) -> None:
    """``load_pem_private_key`` responde ``TypeError`` si el PEM pide una contraseña."""
    key_file.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"no-es-secreta"),
        )
    )
    key_file.chmod(0o600)
    with pytest.raises(NodeCaError):
        _sync_public(FileNodeCaSigner(key_file))


def test_a_symbolic_link_is_refused(key_file: Path, tmp_path: Path) -> None:
    target = tmp_path / "real.key"
    _write_key(target, ec.generate_private_key(ec.SECP256R1()))
    key_file.symlink_to(target)
    with pytest.raises(NodeCaError):
        _sync_public(FileNodeCaSigner(key_file))


def test_a_directory_is_refused(key_file: Path) -> None:
    key_file.mkdir(mode=0o700)
    with pytest.raises(NodeCaError):
        _sync_public(FileNodeCaSigner(key_file))


def test_a_file_that_is_not_a_key_is_refused(key_file: Path) -> None:
    key_file.write_bytes(b"no es una clave")
    key_file.chmod(0o600)
    with pytest.raises(NodeCaError):
        _sync_public(FileNodeCaSigner(key_file))


def test_a_key_that_is_not_elliptic_is_refused(key_file: Path) -> None:
    _write_key(key_file, rsa.generate_private_key(public_exponent=65_537, key_size=2_048))
    with pytest.raises(NodeCaError):
        _sync_public(FileNodeCaSigner(key_file))


def test_a_rotation_refused_with_two_roots_writes_no_key(key_file: Path) -> None:
    """El paquete completo se comprueba antes de firmar: la clave nueva no llega a existir."""
    signer = FileNodeCaSigner(key_file, creatable=frozenset({*CREATABLE, "file:node-ca-3"}))
    publisher = NodeCaPublisher(
        storage=MemoryStorage(), kms=signer, environment="integration", random_bytes=os.urandom
    )
    asyncio.run(publisher.publish_root(KEY_ID, now=NOW))
    asyncio.run(publisher.rotate_root("file:node-ca-2", now=NOW))
    with pytest.raises(NodeCaError):
        asyncio.run(publisher.rotate_root("file:node-ca-3", now=NOW))
    assert not (key_file.parent / "node-ca-3.pem").exists()


def test_a_signer_only_creates_the_keys_it_was_told(key_file: Path) -> None:
    signer = FileNodeCaSigner(key_file, creatable=frozenset({"file:otra"}))
    with pytest.raises(NodeCaError):
        _sync_sign(signer, b"mensaje")
    assert list(key_file.parent.iterdir()) == []
    _sync_sign(signer, b"mensaje", "file:otra")
    assert [path.name for path in key_file.parent.iterdir()] == ["otra.pem"]


def test_a_creatable_key_that_exists_is_used_and_never_replaced(key_file: Path) -> None:
    """Con la marca y una clave existente (A-89): la misma clave, los mismos bytes en disco."""
    existing = ec.generate_private_key(ec.SECP256R1())
    _write_key(key_file, existing)
    before = key_file.read_bytes()
    signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
    public = serialization.load_der_public_key(_sync_public(signer))
    assert public == existing.public_key()
    signature = _sync_sign(signer, b"mensaje")
    existing.public_key().verify(signature, b"mensaje", ec.ECDSA(hashes.SHA256()))
    assert key_file.read_bytes() == before


def test_a_directory_that_does_not_exist_is_a_node_ca_error(tmp_path: Path) -> None:
    missing = FileNodeCaSigner(tmp_path / "no-existe" / "node-ca.pem", creatable=CREATABLE)
    with pytest.raises(NodeCaError):
        _sync_sign(missing, b"mensaje")


def test_a_key_that_vanishes_after_being_written_is_a_node_ca_error(
    signer: FileNodeCaSigner, key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tras publicar, la clave se relee del disco: nunca se firma con una que no quedó escrita."""
    monkeypatch.setattr(os, "link", lambda *_: None)
    with pytest.raises(NodeCaError):
        _sync_sign(signer, b"mensaje")
    assert list(key_file.parent.iterdir()) == []


@pytest.mark.parametrize("operation", ["public", "sign"])
def test_one_signer_serves_its_threads_one_at_a_time(
    key_file: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """El candado protege la clave pendiente: sin él, dos hilos de un mismo firmante generarían dos
    claves y la pública leída no sería la de la firma. La lectura retiene a cada hilo hasta que
    llega otro: con el candado no llega ninguno, y sin él se encuentran siempre."""
    signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
    meeting = threading.Barrier(2, timeout=OVERLAP_SECONDS)
    counter = threading.Lock()
    inside = {"active": 0, "peak": 0}
    real_read = file_signer_module._read_key

    def watched(path: Path) -> Any:
        with counter:
            inside["active"] += 1
            inside["peak"] = max(inside["peak"], inside["active"])
        with contextlib.suppress(threading.BrokenBarrierError):
            meeting.wait()
        try:
            return real_read(path)
        finally:
            with counter:
                inside["active"] -= 1

    monkeypatch.setattr(file_signer_module, "_read_key", watched)
    start = threading.Barrier(2, timeout=WAIT_SECONDS)
    results: list[bytes] = []

    def call() -> None:
        start.wait()
        if operation == "public":
            results.append(signer._public_der(KEY_ID))
        else:
            results.append(signer._sign(KEY_ID, b"mensaje"))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_SECONDS)
    assert inside["peak"] == 1
    assert len(results) == 2
    if operation == "public":
        assert results[0] == results[1]


def test_concurrent_creators_converge_on_one_key(tmp_path: Path) -> None:
    """Varios ``bootstrap`` a la vez: todas las firmas verifican con la única clave publicada."""
    for number in range(5):
        directory = tmp_path / f"ronda-{number}"
        directory.mkdir()
        key_file = directory / "node-ca.pem"
        barrier = threading.Barrier(WORKERS)
        signatures: list[bytes] = []

        def attempt(
            barrier: threading.Barrier = barrier,
            key_file: Path = key_file,
            signatures: list[bytes] = signatures,
        ) -> None:
            signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
            _sync_public(signer)
            barrier.wait(timeout=WAIT_SECONDS)
            signatures.append(_sync_sign(signer, b"mensaje"))

        threads = [threading.Thread(target=attempt) for _ in range(WORKERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=WAIT_SECONDS)
        assert len(signatures) == WORKERS
        public = serialization.load_der_public_key(_sync_public(FileNodeCaSigner(key_file)))
        assert isinstance(public, ec.EllipticCurvePublicKey)
        for signature in signatures:
            public.verify(signature, b"mensaje", ec.ECDSA(hashes.SHA256()))
        assert [path.name for path in directory.iterdir()] == [key_file.name]


# --- La clave no sale ----------------------------------------------------------------------------


def _key_lines(key_file: Path) -> list[str]:
    return [line for line in key_file.read_text().splitlines() if not line.startswith("-----")]


def test_the_key_never_appears_in_logs_or_errors(
    key_file: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
    root = _root(signer)
    asyncio.run(issue_node_certificate(signer, KEY_ID, root, NOW))
    key_file.chmod(0o640)
    with pytest.raises(NodeCaError) as refused:
        _sync_public(signer)
    key_file.chmod(0o600)
    lines = _key_lines(key_file)
    captured = capsys.readouterr()
    sinks = (captured.out, captured.err, caplog.text, repr(signer), str(refused.value))
    assert lines
    for line in lines:
        assert not any(line in sink for sink in sinks)
    assert str(key_file) not in str(refused.value)
    body = base64.b64encode(base64.b64decode("".join(lines))).decode()
    assert not any(body in sink for sink in sinks)


# --- El adaptador de KMS -------------------------------------------------------------------------


class _Client:
    """Lo único que llega a KMS con el backend ``file``: el cifrado de sobre."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., dict[str, bytes]]:
        def call(**_: Any) -> dict[str, bytes]:
            self.calls.append(name)
            return {"Plaintext": b"\0" * 32, "CiphertextBlob": b"sobre"}

        return call


def test_the_node_ca_goes_to_the_file_and_the_envelope_stays_in_kms(
    signer: FileNodeCaSigner,
) -> None:
    client = _Client()
    kms = FileBackedKms(AwsSettings(region="us-east-1"), signer, client=client)
    public = asyncio.run(kms.get_public_key(KEY_ID))
    signature = asyncio.run(kms.sign(KEY_ID, b"mensaje"))
    key = serialization.load_der_public_key(public)
    assert isinstance(key, ec.EllipticCurvePublicKey)
    key.verify(signature, b"mensaje", ec.ECDSA(hashes.SHA256()))
    assert client.calls == []
    asyncio.run(kms.generate_data_key("alias/vigia-secrets", context={"purpose": "prueba"}))
    assert client.calls == ["generate_data_key"]


# --- El selector ---------------------------------------------------------------------------------


def _environ(**changes: str) -> dict[str, str]:
    values = {
        "VIGIA_ENVIRONMENT": "integration",
        "AWS_REGION": "us-east-1",
        "VIGIA_DB_APP_SECRET": DB_SECRET_NAME,
        "VIGIA_SIGNING_SECRET_PREFIX": "vigia/integracion/signing/",
        "VIGIA_SECRETS_KEY_ARN": "alias/vigia-secrets",
        "VIGIA_NODE_CA_BACKEND": "file",
        "VIGIA_NODE_CA_KEY_FILE": "/ca/node-ca.pem",
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("environment", ["local", "integration"])
def test_the_file_backend_is_accepted_in_local_and_integration(environment: str) -> None:
    config = RuntimeConfig.from_environ(_environ(VIGIA_ENVIRONMENT=environment))
    assert config.node_ca_backend == "file"
    assert config.node_ca_key_file == "/ca/node-ca.pem"


@pytest.mark.parametrize("environment", ["pilot", "staging-1", "test"])
def test_the_file_backend_is_forbidden_elsewhere(environment: str) -> None:
    with pytest.raises(NodeCaBackendForbidden) as forbidden:
        RuntimeConfig.from_environ(_environ(VIGIA_ENVIRONMENT=environment))
    assert forbidden.value.code == "node_ca_backend_forbidden"
    assert forbidden.value.variable == "VIGIA_NODE_CA_BACKEND"


def test_the_default_backend_is_kms() -> None:
    environ = _environ(VIGIA_ENVIRONMENT="pilot")
    del environ["VIGIA_NODE_CA_BACKEND"]
    assert RuntimeConfig.from_environ(environ).node_ca_backend == "kms"


def test_the_kms_backend_is_accepted_everywhere() -> None:
    config = RuntimeConfig.from_environ(
        _environ(VIGIA_ENVIRONMENT="pilot", VIGIA_NODE_CA_BACKEND="kms")
    )
    assert config.node_ca_backend == "kms"


def test_the_file_backend_needs_its_key_file() -> None:
    environ = _environ()
    del environ["VIGIA_NODE_CA_KEY_FILE"]
    with pytest.raises(RuntimeConfigInvalid) as invalid:
        RuntimeConfig.from_environ(environ)
    assert invalid.value.variable == "VIGIA_NODE_CA_KEY_FILE"
    assert invalid.value.code == "config_invalid"


@pytest.mark.parametrize(
    "value", ["clave.pem", "ca/clave.pem", "/ca/clave con espacio.pem", "/ca/$(clave).pem"]
)
def test_the_key_file_must_be_a_plain_absolute_path(value: str) -> None:
    with pytest.raises(RuntimeConfigInvalid) as invalid:
        RuntimeConfig.from_environ(_environ(VIGIA_NODE_CA_KEY_FILE=value))
    assert invalid.value.variable == "VIGIA_NODE_CA_KEY_FILE"


@pytest.mark.parametrize("value", ["hsm", "FILE", "file ", "kms,file"])
def test_an_unknown_backend_stops_the_process(value: str) -> None:
    with pytest.raises(RuntimeConfigInvalid) as invalid:
        RuntimeConfig.from_environ(_environ(VIGIA_NODE_CA_BACKEND=value))
    assert invalid.value.variable == "VIGIA_NODE_CA_BACKEND"


def test_integration_admits_the_local_endpoint_and_pilot_still_does_not() -> None:
    endpoint = {"VIGIA_AWS_ENDPOINT_URL": "http://localstack:4566"}
    assert (
        RuntimeConfig.from_environ(_environ(**endpoint)).aws_endpoint_url
        == (endpoint["VIGIA_AWS_ENDPOINT_URL"])
    )
    with pytest.raises(RuntimeConfigInvalid):
        RuntimeConfig.from_environ(
            _environ(VIGIA_ENVIRONMENT="pilot", VIGIA_NODE_CA_BACKEND="kms", **endpoint)
        )


def test_every_process_accepts_the_integration_environment() -> None:
    """``vigia-api``, ``vigia-worker`` y ``vigia-admin``; sin la documentación de ``local``."""
    api = AppConfig(environment="integration", data_key_id="alias/vigia-secrets")
    assert not api.docs_enabled
    worker = WorkerConfig(environment="integration", data_key_id="alias/vigia-secrets")
    assert worker.environment == "integration"
    admin = AdminConfig.from_environ({"VIGIA_ENVIRONMENT": "integration"})
    assert admin.environment == "integration"


OPERATOR: Final = "0192f0c4-0000-7000-8000-000000000001"
NEW_KEY_ID: Final = "file:node-ca-2"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("bootstrap", "--first-install", "--dry-run"), frozenset({KEY_ID})),
        (("bootstrap", "--dry-run"), frozenset()),
        (("bootstrap", "--resume", "--publish-root", "--dry-run"), frozenset()),
        (("bootstrap", "--resume", "--publish-root", "--first-install"), frozenset({KEY_ID})),
        (
            ("rotate-node-ca", "--new-key-id", NEW_KEY_ID, "--operator", OPERATOR, "--dry-run"),
            frozenset({NEW_KEY_ID}),
        ),
        (("set-node-rate-brake", "--off", "--operator", OPERATOR), frozenset()),
    ],
    ids=["marca", "sin-marca", "reanudar", "reanudar-con-marca", "rotacion", "otra-orden"],
)
def test_only_the_mark_and_the_rotation_name_a_creatable_key(
    argv: tuple[str, ...], expected: frozenset[str]
) -> None:
    """A-89: lo que ``vigia-admin`` entrega a la composición, orden por orden."""
    seen: list[frozenset[str]] = []

    async def builder(config: AdminConfig, _provider: uuid.UUID) -> Any:
        seen.append(config.node_ca_creatable_keys)
        raise RuntimeConfigInvalid("VIGIA_ENVIRONMENT")

    admin_run(
        list(argv),
        environ={
            "VIGIA_ENVIRONMENT": "integration",
            "VIGIA_NODE_CA_KEY_ARN": KEY_ID,
            "VIGIA_PROVIDER_ORGANIZATION_ID": OPERATOR,
        },
        builder=builder,
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert seen == [expected]


def test_a_two_root_bundle_read_from_the_file_rotation_parses(key_file: Path) -> None:
    signer = FileNodeCaSigner(key_file, creatable=CREATABLE)
    storage = MemoryStorage()
    publisher = NodeCaPublisher(
        storage=storage, kms=signer, environment="integration", random_bytes=os.urandom
    )
    asyncio.run(publisher.publish_root(KEY_ID, now=NOW))
    asyncio.run(publisher.rotate_root("file:node-ca-2", now=NOW))
    assert len(read_bundle(next(iter(storage.objects.values())))) == 2


def test_a_foreign_name_does_not_validate(signer: FileNodeCaSigner) -> None:
    """Sonda: una raíz de otro nombre no valida lo emitido (la prueba de arriba no es trivial)."""
    root = _root(signer)
    leaf = asyncio.run(issue_node_certificate(signer, KEY_ID, root, NOW))
    other_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Otra")])
    other = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(other_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW)
        .not_valid_after(NOW + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(other_key, hashes.SHA256())
    )
    assert not validates(leaf, (other,), NOW + dt.timedelta(minutes=5))
