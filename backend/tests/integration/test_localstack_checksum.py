"""Prueba de humo: el almacén local rechaza un ``PUT`` con suma SHA-256 incorrecta (TASK-103).

La plataforma exige ``x-amz-checksum-sha256`` en toda subida (tech-stack-decisions.md §2,
NFR-CTR-26; entrada A-05 de la adenda) y el almacén comprueba que la suma coincide con los bytes
recibidos. Esta prueba fija que LocalStack, el almacén de ``docker-compose.yml`` y de
testcontainers, hace esa comprobación: un ``PUT`` cuya suma no coincide termina en
``400 BadDigest`` y no deja objeto. Cubre los dos caminos de subida:

- ``PutObject`` de boto3, como sube la plataforma (exportaciones, archivado);
- ``PUT`` HTTP sobre una URL prefirmada con SigV4 y exactamente las cabeceras del mapa de
  ``ClipUploadGrant.required_headers``, como sube el nodo.

Los bytes son generados (NFR-CTR-43).
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from tests.integration.conftest import LocalStackEndpoint

pytestmark = pytest.mark.integration

BODY = hashlib.sha256(b"vigia-task-103-clip-sintetico").digest() * 64
"""2 KiB generados: suficientes para mutar un byte en medio, al principio y al final."""

ANONYMIZED_HEADER = "x-amz-meta-vigia-anonymized"
CHECKSUM_HEADER = "x-amz-checksum-sha256"
HTTP_TIMEOUT_SECONDS = 30.0


def sha256_b64(data: bytes) -> str:
    """Valor de ``x-amz-checksum-sha256``: SHA-256 de los bytes en base64."""
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def _flip_first_byte(data: bytes) -> bytes:
    return bytes([data[0] ^ 0x01]) + data[1:]


def _flip_middle_byte(data: bytes) -> bytes:
    middle = len(data) // 2
    return data[:middle] + bytes([data[middle] ^ 0x80]) + data[middle + 1 :]


def _flip_last_byte(data: bytes) -> bytes:
    return data[:-1] + bytes([data[-1] ^ 0x01])


MUTATIONS: dict[str, Callable[[bytes], bytes]] = {
    "primer_byte_cambiado": _flip_first_byte,
    "byte_central_cambiado": _flip_middle_byte,
    "ultimo_byte_cambiado": _flip_last_byte,
    "un_byte_de_menos": lambda data: data[:-1],
    "un_byte_de_mas": lambda data: data + b"\x00",
    "cuerpo_vacio": lambda data: b"",
}
"""Bytes enviados en lugar de ``BODY``, cuya suma es la que se declara."""


@pytest.fixture
def s3(localstack_endpoint: LocalStackEndpoint) -> Any:
    return localstack_endpoint.aws_client("s3")


@pytest.fixture
def bucket(s3: Any) -> Iterator[str]:
    """Depósito propio de la prueba, con versionado como ``vigia-evidence``.

    Se vacía y se borra al terminar. Sin versionado, LocalStack 4.14 deja ilegible el objeto
    anterior cuando rechaza un ``PUT`` sobre la misma clave (responde 200 sin cuerpo); con
    versionado, que es como está el depósito real (infrastructure-design.md §6.2), no.
    """
    name = f"vigia-checksum-{uuid.uuid4().hex[:16]}"
    s3.create_bucket(Bucket=name)
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    try:
        yield name
    finally:
        listing = s3.list_object_versions(Bucket=name)
        for item in listing.get("Versions", []) + listing.get("DeleteMarkers", []):
            s3.delete_object(Bucket=name, Key=item["Key"], VersionId=item["VersionId"])
        s3.delete_bucket(Bucket=name)


def _object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            return False
        raise
    return True


def _presigned_put(s3: Any, bucket: str, key: str, checksum: str) -> str:
    """URL prefirmada con la suma y el metadato de anonimización como cabeceras firmadas."""
    url: str = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ChecksumSHA256": checksum,
            "Metadata": {"vigia-anonymized": "1"},
        },
        ExpiresIn=300,
    )
    return url


# --- control: con la suma correcta la subida se acepta ---------------------------------------


@pytest.mark.parametrize("body", [BODY, b""], ids=["cuerpo_de_2_KiB", "cuerpo_vacio"])
def test_put_object_with_matching_checksum_is_accepted(s3: Any, bucket: str, body: bytes) -> None:
    checksum = sha256_b64(body)
    response = s3.put_object(Bucket=bucket, Key="ok", Body=body, ChecksumSHA256=checksum)
    assert response["ChecksumSHA256"] == checksum
    stored = s3.head_object(Bucket=bucket, Key="ok", ChecksumMode="ENABLED")
    assert stored["ChecksumSHA256"] == checksum
    assert stored["ContentLength"] == len(body)


def test_presigned_put_with_matching_checksum_is_accepted(s3: Any, bucket: str) -> None:
    checksum = sha256_b64(BODY)
    url = _presigned_put(s3, bucket, "clip.mp4", checksum)
    response = httpx.put(
        url,
        content=BODY,
        headers={CHECKSUM_HEADER: checksum, ANONYMIZED_HEADER: "1"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    assert response.status_code == 200, response.text
    stored = s3.head_object(Bucket=bucket, Key="clip.mp4", ChecksumMode="ENABLED")
    assert stored["ChecksumSHA256"] == checksum
    assert stored["Metadata"] == {"vigia-anonymized": "1"}


# --- criterio: con la suma incorrecta la subida se rechaza y no deja objeto -------------------


@pytest.mark.parametrize("mutation", list(MUTATIONS.values()), ids=list(MUTATIONS))
def test_put_object_with_mismatching_checksum_is_rejected(
    s3: Any, bucket: str, mutation: Callable[[bytes], bytes]
) -> None:
    sent = mutation(BODY)
    assert sha256_b64(sent) != sha256_b64(BODY)
    with pytest.raises(ClientError) as raised:
        s3.put_object(Bucket=bucket, Key="rechazado", Body=sent, ChecksumSHA256=sha256_b64(BODY))
    error = raised.value.response
    assert error["ResponseMetadata"]["HTTPStatusCode"] == 400
    assert error["Error"]["Code"] == "BadDigest"
    assert not _object_exists(s3, bucket, "rechazado")


def test_put_object_rejection_keeps_previous_version(s3: Any, bucket: str) -> None:
    """Un ``PUT`` rechazado sobre una clave existente no crea versión ni altera la que había."""
    original = sha256_b64(BODY)
    s3.put_object(Bucket=bucket, Key="clip", Body=BODY, ChecksumSHA256=original)
    with pytest.raises(ClientError) as raised:
        s3.put_object(
            Bucket=bucket, Key="clip", Body=_flip_middle_byte(BODY), ChecksumSHA256=original
        )
    assert raised.value.response["Error"]["Code"] == "BadDigest"
    assert len(s3.list_object_versions(Bucket=bucket, Prefix="clip")["Versions"]) == 1
    stored = s3.get_object(Bucket=bucket, Key="clip", ChecksumMode="ENABLED")
    assert stored["Body"].read() == BODY
    assert stored["ChecksumSHA256"] == original


@pytest.mark.parametrize("mutation", list(MUTATIONS.values()), ids=list(MUTATIONS))
def test_presigned_put_with_mismatching_checksum_is_rejected(
    s3: Any, bucket: str, mutation: Callable[[bytes], bytes]
) -> None:
    """El nodo sube con la URL prefirmada; los bytes que llegan no son los de la suma firmada."""
    checksum = sha256_b64(BODY)
    url = _presigned_put(s3, bucket, "clip.mp4", checksum)
    response = httpx.put(
        url,
        content=mutation(BODY),
        headers={CHECKSUM_HEADER: checksum, ANONYMIZED_HEADER: "1"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    assert response.status_code == 400, response.text
    assert "<Code>BadDigest</Code>" in response.text
    assert not _object_exists(s3, bucket, "clip.mp4")
