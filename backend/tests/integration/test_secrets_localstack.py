"""Secrets Manager y KMS reales (LocalStack) bajo ``shared.secrets`` y ``shared.signing``
(TASK-115; LC-NUC-25 y 28; PAT-NUC-SEG-04; FS-NUC-05).

- ``SecretsManagerAdapter``: alta y lectura de secretos binarios, caché de 5 minutos, secreto
  inexistente, nombre repetido.
- ``KmsAdapter``: clave de datos AES-256 que se descifra, y firma ECDSA P-256 con SHA-256 que
  verifica con la clave pública de KMS (el perfil de ``vigia-node-ca``).
- ``SigningService`` con el material en Secrets Manager: un proceso nuevo carga sus claves del
  gestor, firma y rota; el material solo está en el gestor.
- **Criterio de aceptación**: con Secrets Manager inaccesible al arrancar (conexión rechazada o
  servicio que no responde), el proceso no queda listo y el arranque termina dentro del tope de
  5 s. En operación, si el gestor cae, se sigue firmando con lo que hay en memoria.

Solo datos generados (NFR-CTR-43). Cada prueba usa nombres propios en el LocalStack compartido.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key
from vigia_contracts.clock import SimulatedClock

from tests.integration.conftest import (
    LOCALSTACK_ACCESS_KEY_ID,
    LOCALSTACK_SECRET_ACCESS_KEY,
    LocalStackEndpoint,
)
from tests.signing_support import (
    BOOTSTRAP_ORDER,
    PROVIDER_ORGANIZATION_ID,
    START,
    InMemoryKeyStore,
    RecordingEvents,
    provider_context,
)
from vigia_platform.shared.secrets import (
    CALL_TIMEOUT_SECONDS,
    AwsCredentials,
    AwsSettings,
    KmsAdapter,
    SecretNotFound,
    SecretsManagerAdapter,
    SecretsStartupError,
    SecretsUnavailable,
    load_required,
)
from vigia_platform.shared.signing import (
    SigningPurpose,
    SigningService,
    SigningStartupError,
    verify_platform_envelope,
)

pytestmark = pytest.mark.integration

STARTUP_MARGIN_SECONDS = 1.5
"""Holgura sobre el tope de 5 s: planificación del hilo y del bucle de eventos."""


def _settings(url: str, region: str = "us-east-1") -> AwsSettings:
    return AwsSettings(
        region=region,
        endpoint_url=url,
        credentials=AwsCredentials(LOCALSTACK_ACCESS_KEY_ID, LOCALSTACK_SECRET_ACCESS_KEY),
    )


def _environment() -> str:
    return f"it-{uuid.uuid4().hex[:12]}"


def _service(
    secrets: Any, store: InMemoryKeyStore, events: RecordingEvents, clock: Any, environment: str
) -> SigningService:
    return SigningService(
        provider_organization_id=PROVIDER_ORGANIZATION_ID,
        store=store,
        secrets=secrets,
        events=events,
        clock=clock,
        environment=environment,
    )


async def _bootstrap(localstack: LocalStackEndpoint) -> tuple[InMemoryKeyStore, str]:
    """Alta de las cinco claves con el material en el Secrets Manager de LocalStack."""
    clock = SimulatedClock(START)
    store, environment = InMemoryKeyStore(), _environment()
    secrets = SecretsManagerAdapter(_settings(localstack.url), clock)
    bootstrap = _service(secrets, store, RecordingEvents(), clock, environment)
    await bootstrap.start(required=())
    for purpose in BOOTSTRAP_ORDER:
        await bootstrap.rotate(purpose, context=provider_context())
    return store, environment


@contextlib.contextmanager
def _silent_server() -> Iterator[str]:
    """Servidor TCP que acepta conexiones y nunca responde: un gestor que no contesta."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            with contextlib.suppress(TimeoutError, OSError):
                connection, _ = listener.accept()
                held.append(connection)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stop.set()
        thread.join(timeout=2)
        for connection in held:
            connection.close()
        listener.close()


def _closed_port_url() -> str:
    """Un puerto sin nadie escuchando: la conexión se rechaza."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


# --- Secrets Manager ---------------------------------------------------------------------------


def test_secrets_round_trip_cache_and_missing(localstack_endpoint: LocalStackEndpoint) -> None:
    async def scenario() -> None:
        clock = SimulatedClock(START)
        name = f"vigia/{_environment()}/signing/gate/k1"
        value = uuid.uuid4().bytes * 2
        writer = SecretsManagerAdapter(_settings(localstack_endpoint.url), clock)
        arn = await writer.create(name, value)
        assert arn.startswith("arn:aws:secretsmanager:") and name in arn
        with pytest.raises(ValueError, match="ya existe"):
            await writer.create(name, value)

        reader = SecretsManagerAdapter(_settings(localstack_endpoint.url), clock)
        assert await reader.get(arn) == value
        assert await reader.get(name) == value
        # Borrado fuera de la plataforma: la caché lo sirve 5 minutos y después no existe.
        raw = localstack_endpoint.aws_client("secretsmanager")
        raw.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
        clock.advance(299)
        assert await reader.get(arn) == value
        clock.advance(2)
        with pytest.raises(SecretNotFound):
            await reader.get(arn)
        with pytest.raises(SecretNotFound):
            await reader.get(f"vigia/{_environment()}/signing/gate/no-existe")

    asyncio.run(scenario())


def test_secrets_are_encrypted_with_the_given_kms_key(
    localstack_endpoint: LocalStackEndpoint,
) -> None:
    """Como ``vigia-secrets`` (``infrastructure-design.md`` §7.2): el secreto de cada versión de
    clave se cifra con la clave KMS indicada y se lee de vuelta."""
    kms = localstack_endpoint.aws_client("kms")
    key = kms.create_key(Description="vigia-secrets de prueba")["KeyMetadata"]
    raw = localstack_endpoint.aws_client("secretsmanager")

    async def scenario() -> None:
        clock = SimulatedClock(START)
        adapter = SecretsManagerAdapter(
            _settings(localstack_endpoint.url), clock, kms_key_id=key["Arn"]
        )
        name = f"vigia/{_environment()}/signing/catalog/k1"
        value = uuid.uuid4().bytes * 2
        arn = await adapter.create(name, value)
        described = raw.describe_secret(SecretId=arn)
        assert described["KmsKeyId"] in (key["Arn"], key["KeyId"])
        reader = SecretsManagerAdapter(_settings(localstack_endpoint.url), clock)
        assert await reader.get(arn) == value

    asyncio.run(scenario())


# --- KMS ---------------------------------------------------------------------------------------


def test_kms_data_key_and_node_ca_signature(localstack_endpoint: LocalStackEndpoint) -> None:
    raw = localstack_endpoint.aws_client("kms")
    symmetric = raw.create_key(Description="vigia-secrets de prueba")["KeyMetadata"]["KeyId"]
    node_ca = raw.create_key(
        Description="vigia-node-ca de prueba", KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256"
    )["KeyMetadata"]["KeyId"]

    async def scenario() -> None:
        kms = KmsAdapter(_settings(localstack_endpoint.url))
        context = {"vigia_purpose": "envelope"}
        data_key = await kms.generate_data_key(symmetric, context=context)
        assert len(data_key.plaintext) == 32 and data_key.wrapped != data_key.plaintext
        assert await kms.decrypt(data_key.wrapped, key_id=symmetric, context=context) == (
            data_key.plaintext
        )
        # La clave de datos está ligada a su propósito y a su clave maestra (seguimiento de
        # VIG-66): otro contexto u otra clave maestra no la descifran, y no es transitorio.
        other_master = raw.create_key(Description="otra clave maestra")["KeyMetadata"]["KeyId"]
        for key_id, other_context in (
            (symmetric, {"vigia_purpose": "startup_check"}),
            (symmetric, {"vigia_purpose": "envelope", "extra": "x"}),
            (other_master, context),
        ):
            with pytest.raises(ValueError, match="no corresponde"):
                await kms.decrypt(data_key.wrapped, key_id=key_id, context=other_context)

        message = b"TBSCertificate sintetico " + uuid.uuid4().bytes
        signature = await kms.sign(node_ca, message)
        public_key = load_der_public_key(await kms.get_public_key(node_ca))
        assert isinstance(public_key, ec.EllipticCurvePublicKey)
        assert public_key.curve.name == "secp256r1"
        public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        with pytest.raises(InvalidSignature):
            public_key.verify(signature, message + b"x", ec.ECDSA(hashes.SHA256()))

    asyncio.run(scenario())


def test_kms_unreachable_is_unavailable_within_the_timeout() -> None:
    async def scenario() -> None:
        kms = KmsAdapter(_settings(_closed_port_url()))
        with pytest.raises(SecretsUnavailable):
            await kms.generate_data_key("alias/vigia-secrets", context={"vigia_purpose": "x"})

    asyncio.run(scenario())


# --- SigningService con el material en Secrets Manager -----------------------------------------


def test_a_new_process_loads_its_keys_from_secrets_manager_and_signs(
    localstack_endpoint: LocalStackEndpoint,
) -> None:
    async def scenario() -> None:
        store, environment = await _bootstrap(localstack_endpoint)
        clock = SimulatedClock(START)
        events = RecordingEvents()
        secrets = SecretsManagerAdapter(_settings(localstack_endpoint.url), clock)
        service = _service(secrets, store, events, clock, environment)
        await service.start()
        assert service.ready

        envelope = service.sign(SigningPurpose.CHECKPOINT, {"covered_sequence": 7})
        published = service.public_keys(SigningPurpose.CHECKPOINT)
        assert verify_platform_envelope(envelope, published, SigningPurpose.CHECKPOINT)

        result = await service.rotate(SigningPurpose.KEY_SET, context=provider_context())
        reference = result.new_key.private_key_ref
        assert f"vigia/{environment}/signing/key_set/{result.new_key.key_id}" in reference
        raw = localstack_endpoint.aws_client("secretsmanager")
        material = raw.get_secret_value(SecretId=reference)["SecretBinary"]
        assert len(material) == 32
        assert material.hex() not in repr(store.keys) + repr(events.rotated)
        assert result.publication is not None
        assert result.publication.signed_by_key_id == result.previous_key_id

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["conexion_rechazada", "sin_respuesta"])
def test_with_secrets_manager_unreachable_at_startup_the_process_is_not_ready(
    localstack_endpoint: LocalStackEndpoint, failure: str
) -> None:
    """Criterio de aceptación: sin gestor de secretos al arrancar, el proceso no queda listo."""

    async def scenario(url: str) -> float:
        store, environment = await _bootstrap(localstack_endpoint)
        clock = SimulatedClock(START)
        secrets = SecretsManagerAdapter(_settings(url), clock)
        service = _service(secrets, store, RecordingEvents(), clock, environment)
        started = time.perf_counter()  # noqa: TID251 - la prueba mide el tope real
        with pytest.raises(SigningStartupError) as raised:
            await service.start()
        elapsed = time.perf_counter() - started  # noqa: TID251
        assert set(raised.value.causes) == {"secret_unavailable"}
        assert service.ready is False
        with pytest.raises(SecretsStartupError) as required:
            await load_required(secrets, [k.private_key_ref for k in store.keys.values()])
        assert set(required.value.causes) == {"unavailable"}
        return elapsed

    if failure == "conexion_rechazada":
        elapsed = asyncio.run(scenario(_closed_port_url()))
    else:
        with _silent_server() as url:
            elapsed = asyncio.run(scenario(url))
    # Las cinco lecturas van en paralelo: sin respuesta, el arranque falla en un solo tope de
    # llamada y nunca queda colgado.
    assert elapsed <= CALL_TIMEOUT_SECONDS + STARTUP_MARGIN_SECONDS


class _SwitchableClient:
    """El cliente real de LocalStack con un interruptor: ``blocked`` corta la conexión."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.blocked = False

    def get_secret_value(self, **params: Any) -> Any:
        if self.blocked:
            raise EndpointConnectionError(endpoint_url="http://localstack")
        return self._client.get_secret_value(**params)

    def create_secret(self, **params: Any) -> Any:
        if self.blocked:
            raise EndpointConnectionError(endpoint_url="http://localstack")
        return self._client.create_secret(**params)


def test_in_operation_a_secrets_outage_keeps_signing_and_blocks_rotation(
    localstack_endpoint: LocalStackEndpoint,
) -> None:
    async def scenario() -> None:
        store, environment = await _bootstrap(localstack_endpoint)
        clock = SimulatedClock(START)
        settings = _settings(localstack_endpoint.url)
        client = _SwitchableClient(settings.make_client("secretsmanager"))
        secrets = SecretsManagerAdapter(settings, clock, client=client)
        service = _service(secrets, store, RecordingEvents(), clock, environment)
        await service.start()

        client.blocked = True
        clock.advance(301)
        assert await service.refresh() is True  # nada nuevo que leer: todo está en memoria
        service.sign(SigningPurpose.CHECKPOINT, {"n": 1})
        before = dict(store.keys)
        with pytest.raises(SecretsUnavailable):
            await service.rotate(SigningPurpose.GATE, context=provider_context())
        assert store.keys == before

        client.blocked = False
        await service.rotate(SigningPurpose.GATE, context=provider_context())

    asyncio.run(scenario())
