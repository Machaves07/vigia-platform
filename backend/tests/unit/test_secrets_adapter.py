"""Adaptadores de ``shared.secrets`` con un cliente de boto3 simulado (TASK-115; LC-NUC-28;
PAT-NUC-RES-03).

Caché de 5 minutos, respaldo con el último valor y ``secrets_refresh_failed`` cuando la relectura
falla, traducción de errores (inexistente frente a inaccesible), tope por llamada y validación de
entradas. La versión contra Secrets Manager y KMS de LocalStack está en
``tests/integration/test_secrets_localstack.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from vigia_contracts.clock import SimulatedClock

from tests.hibp_service import metric_total, metrics_with_reader
from tests.signing_support import START
from vigia_platform.shared.observability.metrics import MetricName
from vigia_platform.shared.secrets import (
    AwsCredentials,
    AwsSettings,
    Dependency,
    KmsAdapter,
    SecretNotFound,
    SecretsManagerAdapter,
    SecretsStartupError,
    SecretsUnavailable,
    load_required,
)

pytestmark = pytest.mark.asyncio

ARN_PREFIX = ":".join(("arn", "aws", "secretsmanager", "us-east-1", "000000000000", "secret"))
"""ARN sintético de LocalStack, armado por partes (no es una credencial)."""
ARN = ARN_PREFIX + ":" + "/".join(("vigia", "pilot", "signing", "gate", "k1"))
SETTINGS = AwsSettings(region="us-east-1", endpoint_url="http://localhost:1")


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetSecretValue",
    )


@dataclass
class FakeSecretsClient:
    values: dict[str, bytes] = field(default_factory=dict)
    error: Exception | None = None
    delay: float = 0.0
    calls: int = 0

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if SecretId not in self.values:
            raise _client_error("ResourceNotFoundException")
        return {"ARN": SecretId, "SecretBinary": self.values[SecretId]}

    def create_secret(self, **params: Any) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        arn = f"arn:aws:secretsmanager:us-east-1:000000000000:secret:{params['Name']}"
        if arn in self.values:
            raise _client_error("ResourceExistsException")
        self.values[arn] = params["SecretBinary"]
        self.last_params = params
        return {"ARN": arn, "Name": params["Name"]}


def _adapter(
    client: FakeSecretsClient, clock: SimulatedClock, **kwargs: Any
) -> SecretsManagerAdapter:
    return SecretsManagerAdapter(SETTINGS, clock, client=client, **kwargs)


async def test_values_are_cached_for_five_minutes() -> None:
    clock = SimulatedClock(START)
    client = FakeSecretsClient({ARN: b"\x01" * 32})
    adapter = _adapter(client, clock)
    assert await adapter.get(ARN) == b"\x01" * 32
    clock.advance(299.999)
    assert await adapter.get(ARN) == b"\x01" * 32
    assert client.calls == 1
    clock.advance(0.001)
    await adapter.get(ARN)
    assert client.calls == 2


async def test_failed_reread_serves_the_last_value_and_counts_it() -> None:
    metrics, reader = metrics_with_reader()
    clock = SimulatedClock(START)
    client = FakeSecretsClient({ARN: b"\x02" * 32})
    adapter = _adapter(client, clock, metrics=metrics)
    await adapter.get(ARN)
    clock.advance(301)
    client.error = EndpointConnectionError(endpoint_url="http://localhost:1")
    assert await adapter.get(ARN) == b"\x02" * 32
    assert metric_total(reader, MetricName.SECRETS_REFRESH_FAILED) == 1
    # Sin valor anterior no hay respaldo.
    with pytest.raises(SecretsUnavailable) as raised:
        await adapter.get(ARN + "-otro")
    assert raised.value.dependency is Dependency.SECRETS_MANAGER
    assert "arn:" not in str(raised.value)


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://localhost:1"),
        ReadTimeoutError(endpoint_url="http://localhost:1"),
        _client_error("AccessDeniedException"),
        _client_error("InternalServiceError", 500),
        _client_error("ThrottlingException"),
        _client_error("DecryptionFailure"),
    ],
    ids=["conexion", "lectura", "acceso", "5xx", "limitacion", "descifrado"],
)
async def test_every_aws_failure_is_unavailable(error: Exception) -> None:
    client = FakeSecretsClient(error=error)
    with pytest.raises(SecretsUnavailable):
        await _adapter(client, SimulatedClock(START)).get(ARN)


async def test_a_missing_secret_is_not_found_and_a_text_secret_is_rejected() -> None:
    client = FakeSecretsClient()
    adapter = _adapter(client, SimulatedClock(START))
    with pytest.raises(SecretNotFound):
        await adapter.get(ARN)

    class TextClient(FakeSecretsClient):
        def get_secret_value(self, SecretId: str) -> dict[str, Any]:
            return {"ARN": SecretId, "SecretString": "x"}

    with pytest.raises(ValueError, match="binario"):
        await _adapter(TextClient(), SimulatedClock(START)).get(ARN)


async def test_a_call_that_hangs_ends_at_the_timeout() -> None:
    settings = AwsSettings(region="us-east-1", call_timeout_seconds=0.2)
    client = FakeSecretsClient({ARN: b"x"}, delay=1.0)
    adapter = SecretsManagerAdapter(settings, SimulatedClock(START), client=client)
    loop_start = time.perf_counter()  # noqa: TID251 - la prueba mide el tope real
    with pytest.raises(SecretsUnavailable):
        await adapter.get(ARN)
    assert time.perf_counter() - loop_start < 0.9  # noqa: TID251


@pytest.mark.parametrize(
    "arn", ["", " ", "vigia/pilot\n", "vigia pilot", "-vigia", "a" * 2049, "vigia/ñ"]
)
async def test_secret_identifiers_are_closed(arn: str) -> None:
    client = FakeSecretsClient()
    adapter = _adapter(client, SimulatedClock(START))
    with pytest.raises(ValueError):
        await adapter.get(arn)
    with pytest.raises(ValueError):
        await adapter.create(arn, b"x")
    assert client.calls == 0


async def test_create_never_overwrites_and_encrypts_with_the_given_key() -> None:
    client = FakeSecretsClient()
    adapter = _adapter(client, SimulatedClock(START), kms_key_id="alias/vigia-secrets")
    arn = await adapter.create("vigia/pilot/signing/gate/k1", b"\x03" * 32)
    assert client.last_params["KmsKeyId"] == "alias/vigia-secrets"
    assert await adapter.get(arn) == b"\x03" * 32
    assert client.calls == 1  # la lectura sale de la caché
    with pytest.raises(ValueError, match="ya existe"):
        await adapter.create("vigia/pilot/signing/gate/k1", b"\x04" * 32)
    for value in (b"", b"x" * 65_537):
        with pytest.raises(ValueError):
            await adapter.create("vigia/pilot/signing/gate/k2", value)


async def test_load_required_fails_closed_with_the_cause_of_each_secret() -> None:
    client = FakeSecretsClient({ARN: b"\x01"})
    adapter = _adapter(client, SimulatedClock(START))
    assert await load_required(adapter, [ARN, ARN]) == {ARN: b"\x01"}
    with pytest.raises(SecretsStartupError) as raised:
        await load_required(adapter, [ARN, ARN + "-falta"])
    assert raised.value.causes == ("not_found",)
    client.error = EndpointConnectionError(endpoint_url="http://localhost:1")
    fresh = _adapter(client, SimulatedClock(START))
    with pytest.raises(SecretsStartupError) as raised:
        await load_required(fresh, [ARN])
    assert raised.value.causes == ("unavailable",)
    assert "arn:" not in str(raised.value)


async def test_settings_cap_the_timeout_at_five_seconds() -> None:
    with pytest.raises(ValueError):
        AwsSettings(region="us-east-1", call_timeout_seconds=5.1)
    with pytest.raises(ValueError):
        AwsSettings(region="us-east-1", read_timeout_seconds=0)
    with pytest.raises(ValueError):
        AwsSettings(region="")
    config = SETTINGS.botocore_config()
    assert config.connect_timeout == 5.0 and config.read_timeout == 5.0
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}


@pytest.mark.parametrize("service", ["secretsmanager", "kms"])
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_the_clients_really_carry_the_timeouts_and_a_single_attempt(
    service: str, with_credentials: bool
) -> None:
    """``make_client`` construye el cliente con ``botocore_config()``: sin ella, boto3 volvería a
    60 s y 5 intentos por hilo (sonda M09 de la revisión del PR #21)."""
    settings = AwsSettings(
        region="us-east-1",
        endpoint_url="http://localhost:1",
        credentials=AwsCredentials("prueba", "prueba") if with_credentials else None,
    )
    config = settings.make_client(service).meta.config
    assert config.connect_timeout == 5.0 and config.read_timeout == 5.0
    assert config.retries["total_max_attempts"] == 1
    assert config.region_name == "us-east-1"
    for adapter_client in (
        SecretsManagerAdapter(settings, SimulatedClock(START))._client,
        KmsAdapter(settings)._client,
    ):
        assert adapter_client.meta.config.read_timeout == 5.0
        assert adapter_client.meta.config.retries["total_max_attempts"] == 1


async def test_cache_ttl_cannot_exceed_five_minutes() -> None:
    with pytest.raises(ValueError):
        _adapter(FakeSecretsClient(), SimulatedClock(START), cache_ttl_seconds=301)
    with pytest.raises(ValueError):
        _adapter(FakeSecretsClient(), SimulatedClock(START), cache_ttl_seconds=0)


# --- KMS ----------------------------------------------------------------------------------------


@dataclass
class FakeKmsClient:
    error: Exception | None = None
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _answer(self, name: str) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.responses[name]

    def generate_data_key(self, **params: Any) -> dict[str, Any]:
        assert params["KeySpec"] == "AES_256"
        return self._answer("generate_data_key")

    def decrypt(self, **params: Any) -> dict[str, Any]:
        return self._answer("decrypt")

    def sign(self, **params: Any) -> dict[str, Any]:
        assert params["SigningAlgorithm"] == "ECDSA_SHA_256" and params["MessageType"] == "RAW"
        return self._answer("sign")

    def get_public_key(self, **params: Any) -> dict[str, Any]:
        return self._answer("get_public_key")


async def test_kms_operations_return_bytes_and_fail_as_unavailable() -> None:
    client = FakeKmsClient(
        responses={
            "generate_data_key": {
                "Plaintext": b"p" * 32,
                "CiphertextBlob": b"w" * 60,
                "KeyId": "k",
            },
            "decrypt": {"Plaintext": b"p" * 32},
            "sign": {"Signature": b"s" * 70},
            "get_public_key": {"PublicKey": b"der"},
        }
    )
    kms = KmsAdapter(SETTINGS, client=client)
    data_key = await kms.generate_data_key("alias/vigia-secrets")
    assert data_key.plaintext == b"p" * 32 and data_key.wrapped == b"w" * 60
    assert "p" * 32 not in repr(data_key)
    assert await kms.decrypt(b"w" * 60) == b"p" * 32
    assert await kms.sign("alias/vigia-node-ca", b"tbs") == b"s" * 70
    assert await kms.get_public_key("alias/vigia-node-ca") == b"der"

    client.responses["decrypt"] = {}
    with pytest.raises(SecretsUnavailable) as raised:
        await kms.decrypt(b"w")
    assert raised.value.dependency is Dependency.KMS
    client.error = _client_error("KMSInvalidStateException")
    with pytest.raises(SecretsUnavailable):
        await kms.sign("alias/vigia-node-ca", b"tbs")


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("sign", ("alias/vigia-node-ca", b"")),
        ("sign", ("alias/vigia-node-ca", b"x" * 4097)),
        ("sign", ("alias/vigia node", b"x")),
        ("decrypt", (b"",)),
        ("generate_data_key", ("",)),
        ("get_public_key", ("alias/x\n",)),
    ],
)
async def test_kms_inputs_are_validated_before_calling(
    operation: str, arguments: tuple[Any, ...]
) -> None:
    kms = KmsAdapter(SETTINGS, client=FakeKmsClient(error=AssertionError("no se llama")))
    with pytest.raises(ValueError):
        await getattr(kms, operation)(*arguments)
